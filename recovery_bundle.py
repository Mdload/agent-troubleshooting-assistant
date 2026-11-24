"""Offline data recovery from a quiesced six-database snapshot; never starts services."""
import datetime
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

FORMAT = 1
MAX_DATABASE_BYTES = 128 * 1024 * 1024
DATABASES = {
    'accounts.sqlite3': {'user_accounts', 'account_sessions', 'account_attempts'},
    'chats.sqlite3': {'chat_histories'},
    'diagnostics.sqlite3': {'events'},
    'experiences.sqlite3': {'experiences', 'experience_events'},
    'materials.sqlite3': {'text_materials'},
    'workflow.sqlite3': {'workflow_tasks', 'workflow_events', 'workflow_reads',
                         'workflow_progress', 'workflow_sessions', 'workflow_topics', 'workflow_modes'},
}


class RecoveryError(ValueError):
    pass


def timestamp():
    return datetime.datetime.utcnow().isoformat() + 'Z'


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def checked_path(value):
    path = Path(os.path.abspath(str(value)))
    for part in (path,) + tuple(path.parents):
        if part.is_symlink():
            raise RecoveryError('symlink_not_allowed')
    if path.resolve() != path:
        raise RecoveryError('redirected_path_not_allowed')
    return path


def fresh_destination(value, source):
    target = checked_path(value)
    if target.exists() or target == source or source in target.parents or target in source.parents:
        raise RecoveryError('destination_must_be_new_and_separate')
    if not target.parent.is_dir():
        raise RecoveryError('destination_parent_missing')
    return target


def read_database(path, name):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_DATABASE_BYTES:
        raise RecoveryError('invalid_database_file')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    db.execute('PRAGMA query_only=ON')
    try:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RecoveryError('database_integrity_failed')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not DATABASES[name] <= tables:
            raise RecoveryError('database_schema_not_supported')
        return db
    except BaseException:
        db.close()
        raise


def snapshot(source, destination, name, timeout=10):
    deadline = time.monotonic() + timeout
    src = read_database(source, name)
    page_size = src.execute('PRAGMA page_size').fetchone()[0]
    def progress(status, remaining, total):
        if time.monotonic() >= deadline or total * page_size > MAX_DATABASE_BYTES:
            raise RecoveryError('snapshot_limit_reached')
    try:
        dst = sqlite3.connect(str(destination), timeout=1)
        try:
            src.backup(dst, pages=128, progress=progress, sleep=0.02)
            # A copied WAL header must not make verification depend on live sidecars.
            if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                raise RecoveryError('snapshot_not_standalone')
        finally:
            dst.close()
    finally:
        src.close()
    destination.chmod(0o600)
    checked = read_database(destination, name)
    checked.close()


def write_json(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    path.chmod(0o600)


def create_bundle(source, output, source_stopped=False, source_version='unknown'):
    """source_stopped is an operator attestation, not a process-state detector."""
    if source_stopped is not True:
        raise RecoveryError('quiesced_source_confirmation_required')
    if not isinstance(source_version, str) or not 1 <= len(source_version) <= 40:
        raise RecoveryError('invalid_source_version')
    source = checked_path(source)
    if not source.is_dir():
        raise RecoveryError('source_missing')
    target = fresh_destination(output, source)
    # Check the entire input before creating an output. Only fixed database names are read.
    for name in DATABASES:
        checked = read_database(source/name, name)
        checked.close()
    started = time.monotonic()
    target.mkdir(mode=0o700)
    (target/'data').mkdir(mode=0o700)
    manifest = {'format': FORMAT, 'kind': 'ata_offline_data_backup', 'created_at_utc': timestamp(),
                'source_version': source_version, 'quiesced_source_attested': True,
                'contains_private_data': True, 'encrypted': False, 'files': {}}
    try:
        for name in DATABASES:
            saved = target/'data'/name
            snapshot(source/name, saved, name)
            manifest['files'][name] = {'sha256': digest(saved), 'bytes': saved.stat().st_size}
        manifest['backup_seconds'] = round(time.monotonic() - started, 4)
        # The manifest is the completion marker. Failed partial directories remain for diagnosis.
        write_json(target/'BACKUP.json', manifest)
    except BaseException:
        write_json(target/'INCOMPLETE.json', {'kind': 'incomplete_backup', 'created_at_utc': timestamp()})
        raise
    return {'kind': manifest['kind'], 'databases': len(DATABASES),
            'backup_seconds': manifest['backup_seconds'], 'encrypted': False}


def verify_bundle(value):
    root = checked_path(value)
    marker = root/'BACKUP.json'
    if (not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 65536
            or (root/'INCOMPLETE.json').exists()):
        raise RecoveryError('backup_not_complete')
    manifest = json.loads(marker.read_text(encoding='utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('kind') != 'ata_offline_data_backup'
            or manifest.get('quiesced_source_attested') is not True
            or not isinstance(manifest.get('files'), dict)
            or not isinstance(manifest.get('source_version'), str)
            or set(manifest.get('files', {})) != set(DATABASES)):
        raise RecoveryError('backup_manifest_not_supported')
    if {p.name for p in root.iterdir()} != {'BACKUP.json', 'data'} or (root/'data').is_symlink():
        raise RecoveryError('unexpected_backup_file')
    if {p.name for p in (root/'data').iterdir()} != set(DATABASES):
        raise RecoveryError('unexpected_backup_file')
    for name, info in manifest['files'].items():
        if not isinstance(info, dict):
            raise RecoveryError('backup_manifest_not_supported')
        path = root/'data'/name
        checked = read_database(path, name)
        checked.close()
        if path.stat().st_size != info.get('bytes') or digest(path) != info.get('sha256'):
            raise RecoveryError('backup_content_changed')
    return manifest


def quarantine_authority(target):
    """Only edits the newly restored copy. Historical evidence/results remain intact."""
    with closing(sqlite3.connect(str(target/'data/chats.sqlite3'))) as db, db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rolling_contexts'").fetchone():
            # 自动外发的对话偏好须重新开启，避免旧备份恢复撤回前的授权。
            db.execute('DELETE FROM rolling_contexts')
    with closing(sqlite3.connect(str(target/'data/accounts.sqlite3'))) as db, db:
        sessions = db.execute('SELECT count(*) FROM account_sessions').fetchone()[0]
        db.execute('DELETE FROM account_sessions')
    with closing(sqlite3.connect(str(target/'data/workflow.sqlite3'))) as db, db:
        db.execute('BEGIN IMMEDIATE')
        has_stages=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_stages'").fetchone()
        rows = db.execute("SELECT id FROM workflow_tasks WHERE status IN ('pending','approved','running')").fetchall()
        for row in rows:
            ident = row[0]
            if has_stages:
                db.execute("UPDATE workflow_stages SET status='cancelled',updated=? WHERE task=? AND status='running'",
                           (time.time(),ident))
            db.execute("UPDATE workflow_tasks SET status='cancelled',approved_scope=NULL,approver=NULL WHERE id=?", (ident,))
            progress = db.execute('SELECT body FROM workflow_progress WHERE task=?', (ident,)).fetchone()
            if progress:
                body = json.loads(progress[0]); body['stage'] = 'cancelled'
                db.execute('UPDATE workflow_progress SET body=?,updated=? WHERE task=?', (json.dumps(body), time.time(), ident))
            db.execute('INSERT INTO workflow_events VALUES (?,?,?,?)',
                       (ident, 'cancelled', json.dumps({'reason': 'offline_recovery_requires_new_approval'}), time.time()))
        db.execute('DELETE FROM workflow_sessions')
        db.execute('DELETE FROM workflow_topics')
        db.execute("UPDATE workflow_modes SET mode='qa',scene=NULL,revision=revision+1")
    return {'account_sessions_revoked': sessions, 'unfinished_tasks_cancelled': len(rows)}


def restore_bundle(bundle, destination):
    source = checked_path(bundle)
    manifest = verify_bundle(source)
    target = fresh_destination(destination, source)
    started = time.monotonic()
    target.mkdir(mode=0o700)
    (target/'data').mkdir(mode=0o700)
    try:
        for name in DATABASES:
            if digest(source/'data'/name) != manifest['files'][name]['sha256']:
                raise RecoveryError('backup_content_changed')
            snapshot(source/'data'/name, target/'data'/name, name)
            if digest(source/'data'/name) != manifest['files'][name]['sha256']:
                raise RecoveryError('backup_content_changed')
        quarantined = quarantine_authority(target)
        for name in DATABASES:
            checked = read_database(target/'data'/name, name)
            checked.close()
        report = {'kind': 'ata_offline_data_restore', 'completed_at_utc': timestamp(),
                  'backup_created_at_utc': manifest['created_at_utc'],
                  'source_version': manifest['source_version'], 'databases_verified': len(DATABASES),
                  'restore_seconds': round(time.monotonic() - started, 4),
                  'data_only': True, 'services_started': 0, 'model_api_calls': 0,
                  'live_device_reads': 0, 'service_keys_or_configuration_restored': False,
                  'account_password_records_preserved': True,
                  'production_promotion_performed': False, **quarantined}
        write_json(target/'RESTORE-REPORT.json', report)
        return report
    except BaseException:
        write_json(target/'INCOMPLETE.json', {'kind': 'incomplete_restore', 'created_at_utc': timestamp()})
        raise
