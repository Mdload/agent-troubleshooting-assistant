"""Create/verify/restore an offline data backup. No service or network operations."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.recovery_bundle import create_bundle, verify_bundle, restore_bundle, RecoveryError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    backup = actions.add_parser('backup')
    backup.add_argument('--source', required=True, help='Stopped application data directory or completed release backup/data')
    backup.add_argument('--output', required=True, help='New directory outside the source')
    backup.add_argument('--source-stopped', action='store_true', help='Operator confirms all application writers are stopped')
    backup.add_argument('--source-version', default='unknown', help='Operator-supplied data version; never guessed')
    verify = actions.add_parser('verify')
    verify.add_argument('--bundle', required=True)
    restore = actions.add_parser('restore')
    restore.add_argument('--bundle', required=True)
    restore.add_argument('--output', required=True, help='New isolated data-only directory; never overwrite production')
    args = parser.parse_args()
    try:
        if args.action == 'backup':
            result = create_bundle(args.source, args.output, args.source_stopped, args.source_version)
        elif args.action == 'verify':
            manifest = verify_bundle(args.bundle)
            result = {'kind': 'verified_offline_data_backup', 'databases': len(manifest['files']),
                      'created_at_utc': manifest['created_at_utc'], 'encrypted': False}
        else:
            result = restore_bundle(args.bundle, args.output)
    except (RecoveryError, sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'kind': 'recovery_stopped', 'details_suppressed': True}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
