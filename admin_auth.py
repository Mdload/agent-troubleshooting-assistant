"""单管理员认证：只保存口令派生值；网页不接受自行声明的角色。"""
import argparse
import getpass
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

ITERATIONS = 200000
ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / '.secrets/admin-auth.json'


def password_record(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValueError('管理员口令需要8～128个字符')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                               bytes.fromhex(salt), ITERATIONS).hex()
    return {'salt': salt, 'digest': digest, 'iterations': ITERATIONS}


class AdminAuth:
    def __init__(self, path=DEFAULT_PATH, clock=time.time):
        self.path = Path(path); self.clock = clock
        self.sessions = {}; self.failures = []; self.lock = threading.Lock()

    def record(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (set(data) != {'salt','digest','iterations'} or
                data['iterations'] != ITERATIONS or len(data['salt']) != 32 or len(data['digest']) != 64):
                return None
            bytes.fromhex(data['salt']); bytes.fromhex(data['digest'])
            return data
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def configured(self):
        return self.record() is not None

    def login(self, password):
        with self.lock:
            now = self.clock()
            self.failures = [at for at in self.failures if now-at < 900]
            if len(self.failures) >= 5:
                return None, 'login_limited'
            record = self.record()
            if record is None:
                return None, 'admin_not_configured'
            if not isinstance(password, str) or not 1 <= len(password) <= 128:
                self.failures.append(now)
                return None, 'invalid_credentials'
            digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                bytes.fromhex(record['salt']), ITERATIONS).hex()
            if not secrets.compare_digest(digest, record['digest']):
                self.failures.append(now)
                return None, 'invalid_credentials'
            self.sessions = {key:value for key,value in self.sessions.items() if value[0] > now}
            if len(self.sessions) >= 20:
                return None, 'login_limited'
            token = secrets.token_urlsafe(32)
            self.sessions[self.token_hash(token)] = (now+3600, record['digest'])
            return token, None

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def valid(self, token):
        if not token or len(token) > 128: return False
        with self.lock:
            session = self.sessions.get(self.token_hash(token))
            record = self.record()
            return bool(session and session[0] > self.clock() and record and
                        secrets.compare_digest(session[1], record['digest']))

    def logout(self, token):
        with self.lock:
            self.sessions.pop(self.token_hash(token or ''), None)


def main():
    parser = argparse.ArgumentParser(description='在本项目终端交互设置管理员口令，不放入命令行。')
    parser.add_argument('--reset', action='store_true', help='更换已经设置的管理员口令')
    args = parser.parse_args()
    if DEFAULT_PATH.exists() and not args.reset:
        parser.error('管理员已设置；需要更换时加 --reset')
    password = getpass.getpass('新管理员口令（至少8字符）：')
    if password != getpass.getpass('再次输入：'):
        parser.error('两次输入不一致')
    try: record = password_record(password)
    except ValueError as error: parser.error(str(error))
    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DEFAULT_PATH.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(record, stream); stream.write('\n')
    if os.name != 'nt': os.chmod(str(temporary), 0o600)
    os.replace(str(temporary), str(DEFAULT_PATH))
    print('管理员已设置。在正式页面点击“诊断审批”登录；服务重启后需重新登录。')


if __name__ == '__main__': main()
