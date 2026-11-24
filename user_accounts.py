"""普通用户账号与可撤销会话；不会授予管理员或设备权限。"""
import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ITERATIONS=600000
SESSION_SECONDS=12*3600
IDLE_SECONDS=3600
MAX_ACCOUNTS=10


class AccountError(ValueError):
    pass


def username(value):
    if not isinstance(value,str) or not value:
        raise ValueError('invalid_account_name')
    return value.lower()


def password_bytes(value):
    if not isinstance(value,str) or not value:
        raise ValueError('invalid_account_password')
    return value.encode('utf-8')


def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class AccountStore:
    def __init__(self,path,clock=time.time):
        self.path=Path(path);self.clock=clock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.touch(mode=0o600,exist_ok=True);self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS user_accounts ('
                'id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,digest TEXT NOT NULL,'
                'iterations INTEGER NOT NULL,created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS account_sessions ('
                'token_hash TEXT PRIMARY KEY,account TEXT NOT NULL,expires REAL NOT NULL,last_used REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS account_attempts (name_hash TEXT NOT NULL,created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS lab_registration ('
                'id INTEGER PRIMARY KEY CHECK(id=1),salt TEXT NOT NULL,digest TEXT NOT NULL,'
                'iterations INTEGER NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            # 与其他存储连接使用相同的锁等待时间。
            db.execute('PRAGMA busy_timeout=5000')
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def reserve_attempt(self,name):
        key=token_hash(name)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM account_attempts WHERE created<=?',(self.clock()-900,))
            if (db.execute('SELECT count(*) FROM account_attempts WHERE name_hash=?',(key,)).fetchone()[0]>=10
                or db.execute('SELECT count(*) FROM account_attempts').fetchone()[0]>=100):
                raise AccountError('account_login_limited')
            db.execute('INSERT INTO account_attempts VALUES (?,?)',(key,self.clock()))

    def registration(self):
        with self.connect() as db:row=db.execute('SELECT revision FROM lab_registration WHERE id=1').fetchone()
        return {'configured':row is not None,'revision':row['revision'] if row else 0}

    def configure_registration(self,password,expected_revision,session_token=None):
        raw=password_bytes(password)
        if type(expected_revision) is not int or expected_revision<0:raise ValueError('invalid_revision')
        salt=secrets.token_hex(32)
        digest=hashlib.pbkdf2_hmac('sha256',raw,bytes.fromhex(salt),ITERATIONS).hex()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if session_token is not None:
                active=db.execute('SELECT account FROM account_sessions WHERE token_hash=? AND expires>? AND last_used>?',
                                  (token_hash(session_token),self.clock(),self.clock()-IDLE_SECONDS)).fetchone()
                if active is None:raise AccountError('account_session_expired')
            row=db.execute('SELECT revision FROM lab_registration WHERE id=1').fetchone()
            revision=row['revision'] if row else 0
            if revision!=expected_revision:raise AccountError('lab_password_changed')
            db.execute('INSERT OR REPLACE INTO lab_registration VALUES (1,?,?,?,?,?)',
                       (salt,digest,ITERATIONS,revision+1,self.clock()))
        return {'configured':True,'revision':revision+1}

    def register(self,name,password,consent):
        """可信本地调用的账号创建原语；所有HTTP注册必须使用register_invited。"""
        return self._register(name,password,consent)

    def register_invited(self,name,password,consent,lab_password):
        return self._register(name,password,consent,invited=True,lab_password=lab_password)

    def _register(self,name,password,consent,invited=False,lab_password=None):
        name=username(name);raw=password_bytes(password)
        if consent is not True:raise AccountError('account_storage_consent')
        self.reserve_attempt(name)
        gate=None
        if invited:
            if not isinstance(lab_password,str) or not lab_password:raise AccountError('lab_password_required')
            with self.connect() as db:gate=db.execute('SELECT * FROM lab_registration WHERE id=1').fetchone()
            if gate is None:raise AccountError('lab_password_not_configured')
            supplied=hashlib.pbkdf2_hmac('sha256',password_bytes(lab_password),bytes.fromhex(gate['salt']),gate['iterations']).hex()
            if not secrets.compare_digest(supplied,gate['digest']):raise AccountError('lab_password_invalid')
        salt=secrets.token_hex(32)
        digest=hashlib.pbkdf2_hmac('sha256',raw,bytes.fromhex(salt),ITERATIONS).hex()
        ident=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if gate is not None:
                current=db.execute('SELECT revision FROM lab_registration WHERE id=1').fetchone()
                if current is None or current['revision']!=gate['revision']:raise AccountError('lab_password_changed')
            if db.execute('SELECT count(*) FROM user_accounts').fetchone()[0]>=MAX_ACCOUNTS:
                raise AccountError('account_limit')
            try:db.execute('INSERT INTO user_accounts VALUES (?,?,?,?,?,?)',
                (ident,name,salt,digest,ITERATIONS,self.clock()))
            except sqlite3.IntegrityError:raise AccountError('account_unavailable') from None
        return self.new_session(ident,name)

    def login(self,name,password):
        name=username(name);raw=password_bytes(password);self.reserve_attempt(name)
        with self.connect() as db:row=db.execute('SELECT * FROM user_accounts WHERE name=?',(name,)).fetchone()
        # 不存在的账号也做相同成本的计算，不在登录错误中区分是否注册。
        salt=row['salt'] if row else '0'*64
        work=row['iterations'] if row else ITERATIONS
        supplied=hashlib.pbkdf2_hmac('sha256',raw,bytes.fromhex(salt),work).hex()
        expected=row['digest'] if row else '0'*64
        valid=secrets.compare_digest(supplied,expected)
        if not row or not valid:raise AccountError('account_invalid_credentials')
        return self.new_session(row['id'],row['name'])

    def new_session(self,ident,name):
        token=secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM account_sessions WHERE expires<=? OR last_used<=?',
                       (self.clock(),self.clock()-IDLE_SECONDS))
            previous=db.execute('SELECT token_hash FROM account_sessions WHERE account=? ORDER BY last_used DESC',(ident,)).fetchall()
            for row in previous[9:]:db.execute('DELETE FROM account_sessions WHERE token_hash=?',(row['token_hash'],))
            db.execute('INSERT INTO account_sessions VALUES (?,?,?,?)',
                (token_hash(token),ident,self.clock()+SESSION_SECONDS,self.clock()))
        return token,{'id':ident,'name':name}

    def resolve(self,token):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):return None
        with self.connect() as db:
            row=db.execute('SELECT a.id,a.name FROM account_sessions s JOIN user_accounts a ON a.id=s.account '
                'WHERE s.token_hash=? AND s.expires>? AND s.last_used>?',
                (token_hash(token),self.clock(),self.clock()-IDLE_SECONDS)).fetchone()
            if row:db.execute('UPDATE account_sessions SET last_used=? WHERE token_hash=?',(self.clock(),token_hash(token)))
            return dict(row) if row else None

    def logout(self,token):
        if isinstance(token,str) and re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
            with self.connect() as db:db.execute('DELETE FROM account_sessions WHERE token_hash=?',(token_hash(token),))

    @staticmethod
    def owner(account):
        return token_hash('account:'+account['id'])
