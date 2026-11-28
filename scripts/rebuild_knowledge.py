"""原始资料更新后，一次重建各模块整合手册；不调用模型或设备。"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from retrieval.knowledge_modules import compile_archive


def rebuild(root=ROOT, check=False):
    files = compile_archive(root)
    changed = [name for name, text in files.items() if not (root/name).is_file()
               or (root/name).read_text(encoding='utf-8') != text]
    if not check:
        for name in changed:
            path = root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(files[name], encoding='utf-8')
    return changed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只检查整合版是否跟原资料同步')
    args = parser.parse_args()
    changed = rebuild(check=args.check)
    print(('Outdated: ' if args.check else 'Updated: ') + ', '.join(changed) if changed else 'Module knowledge is current.')
    sys.exit(1 if args.check and changed else 0)
