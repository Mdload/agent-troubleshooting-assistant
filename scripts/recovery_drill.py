"""One-command synthetic cold-recovery drill; never uses production data or services."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from approval_store import ApprovalStore, TaskError
from chat_history import HistoryStore
from recovery_bundle import create_bundle, restore_bundle, verify_bundle, RecoveryError, checked_path, write_json
from recovery_fixture import seed_fixture
from user_accounts import AccountStore
from version import VERSION


def run(output):
    root = checked_path(output)
    if root.exists() or not root.parent.is_dir():
        raise RecoveryError('drill_output_must_be_new')
    root.mkdir(mode=0o700)
    start = time.monotonic()
    try:
        fixture = seed_fixture(root/'synthetic-source')
        backup = create_bundle(root/'synthetic-source/data', root/'bundle', True, VERSION)
        manifest = verify_bundle(root/'bundle')
        # Make the snapshot boundary observable: a later write is intentionally not in the backup.
        with sqlite3.connect(str(root/'synthetic-source/data/chats.sqlite3')) as db:
            db.execute("UPDATE chat_histories SET title='AFTER_BACKUP_ONLY'")
        restored = restore_bundle(root/'bundle', root/'restored')
        history = HistoryStore(root/'restored/data/chats.sqlite3')
        actual = history.get(fixture['owner'], 'recovery-chat')
        if actual['messages'] != fixture['messages'] or actual['title'] != 'Synthetic recovery conversation':
            raise RecoveryError('restored_history_mismatch')
        accounts = AccountStore(root/'restored/data/accounts.sqlite3')
        if accounts.resolve(fixture['token']) is not None:
            raise RecoveryError('old_session_reactivated')
        store = ApprovalStore(root/'restored/data/workflow.sqlite3')
        denied = 0
        for state in ('pending', 'approved', 'running'):
            try:
                store.claim(fixture['tasks'][state], fixture['owner'])
            except TaskError:
                denied += 1
        if denied != 3:
            raise RecoveryError('old_task_reactivated')
        completed = store.get(fixture['tasks']['completed'], fixture['owner'])
        if (completed['status'] != 'completed' or completed['attempts'] != 1
                or completed['result']['answer'] != 'SYNTHETIC_HISTORICAL_RESULT'):
            raise RecoveryError('completed_task_not_retained')
        shutil.copytree(str(root/'bundle'), str(root/'corrupt-bundle'))
        (root/'corrupt-bundle/data/chats.sqlite3').write_bytes(b'synthetic corruption')
        try:
            restore_bundle(root/'corrupt-bundle', root/'must-not-exist')
        except (RecoveryError, sqlite3.Error):
            corruption_denied = not (root/'must-not-exist').exists()
        else:
            corruption_denied = False
        if not corruption_denied:
            raise RecoveryError('corrupt_backup_accepted')
        result = {'kind': 'synthetic_cold_data_recovery_drill', 'successful': True,
                  'databases_verified': restored['databases_verified'],
                  'backup_bytes': sum(item['bytes'] for item in manifest['files'].values()),
                  'backup_seconds': backup['backup_seconds'], 'data_restore_seconds': restored['restore_seconds'],
                  'total_drill_seconds': round(time.monotonic() - start, 4),
                  'old_account_session_denied': True, 'old_unfinished_tasks_denied': denied,
                  'historical_text_and_completed_results_retained': True,
                  'corrupt_backup_denied_before_destination_creation': corruption_denied,
                  'writes_after_backup_not_recovered': True, 'zero_data_loss_claimed': False,
                  'production_data_used': False, 'service_failover_tested': False,
                  'model_api_calls': 0, 'live_device_reads': 0, 'not_model_accuracy': True}
        write_json(root/'DRILL.json', result)
        return result
    except BaseException:
        write_json(root/'INCOMPLETE.json', {'kind': 'incomplete_synthetic_drill'})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New local evidence directory')
    args = parser.parse_args()
    try:
        result = run(args.output)
    except (RecoveryError, sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'kind': 'drill_stopped', 'details_suppressed': True}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
