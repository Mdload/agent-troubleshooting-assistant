"""File-backed shared troubleshooting skills."""
import os
from pathlib import Path
import re
import stat
import tempfile
import threading


ROOT = Path(__file__).resolve().parent / 'agent_skills'
MAX_BYTES = 16 * 1024
_SLUG = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
_lock = threading.RLock()


def _regular(path, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_nlink > 1 and not directory
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('unsafe_skill_path')
    return True


def _directory(create=False):
    folder = ROOT / 'skills'
    for directory in (ROOT, folder):
        if not _regular(directory, directory=True):
            if not create:
                return folder
            directory.mkdir()
    return folder


def _path(slug, create=False):
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError('invalid_skill_slug')
    return _directory(create) / (slug + '.md')


def _read(path):
    if not _regular(path):
        raise FileNotFoundError(path)
    with path.open('rb') as stream:
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError('skill_too_large')
    return payload.decode('utf-8')


def _replace(path, payload):
    _regular(path)
    with tempfile.NamedTemporaryFile(dir=str(path.parent), suffix='.md', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_bytes(payload)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def read_skill(slug):
    with _lock:
        heading, trigger, content = _read(_path(slug)).split('\n', 2)
        if not heading.startswith('# ') or not trigger.startswith('Trigger: '):
            raise ValueError('invalid_skill_format')
        return {'slug': slug, 'title': heading[2:], 'trigger': trigger[9:], 'content': content.lstrip('\n').rstrip('\n')}


def list_skills():
    with _lock:
        directory = _directory()
        items = []
        for file in sorted(directory.glob('*.md')):
            try:
                items.append(read_skill(file.stem))
            except (ValueError, FileNotFoundError):
                continue
        return items


def render_index_summary():
    with _lock:
        _directory()
        try:
            return _read(ROOT / 'INDEX.md').strip()
        except FileNotFoundError:
            return ''


def _index(items):
    payload = ''.join(
        '- {slug} | {title} | {trigger}\n'.format(**item) for item in items
    ).encode('utf-8')
    if len(payload) > MAX_BYTES:
        raise ValueError('skill_index_full')
    _regular(ROOT / 'INDEX.md')
    return payload


def write_skill(slug, title, trigger, content):
    with _lock:
        path = _path(slug)
        if any(not isinstance(value, str) or not value.strip() for value in (title, trigger, content)):
            raise ValueError('invalid_skill_fields')
        title, trigger = (' '.join(value.split()).replace('|', '/') for value in (title, trigger))
        if len(title) > 80 or len(trigger) > 160:
            raise ValueError('skill_metadata_too_long')
        item = {'slug': slug, 'title': title, 'trigger': trigger, 'content': content.strip()}
        payload = '# {title}\nTrigger: {trigger}\n\n{content}\n'.format(**item).encode('utf-8')
        if len(payload) > MAX_BYTES:
            raise ValueError('skill_too_large')
        items = {entry['slug']: entry for entry in list_skills()}
        items[slug] = item
        index = _index([items[key] for key in sorted(items)])
        _directory(create=True)
        _replace(path, payload)
        _replace(ROOT / 'INDEX.md', index)
        return item


def delete_skill(slug):
    with _lock:
        path = _path(slug)
        if not _regular(path):
            return False
        index = _index(item for item in list_skills() if item['slug'] != slug)
        path.unlink()
        _replace(ROOT / 'INDEX.md', index)
        return True
