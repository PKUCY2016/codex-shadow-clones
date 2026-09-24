"""Keep the newest complete local recovery copy for each backed-up resource.

Only directories with the exact shape written by this project are pruned.
Unknown or incomplete directories stay in place for manual inspection.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import shutil


_BACKUP_NAME = re.compile(r'^(history|rollout|sidebar|sync-projects|desktop-marketplace)-([0-9]+)$')
_SINGLE_FILES = {
    'sidebar': '.codex-global-state.json',
    'sync-projects': '.codex-global-state.json',
    'desktop-marketplace': 'config.toml',
}
_SQLITE_FILES = {'state_5.sqlite', 'thread_history_1.sqlite'}
_KINDS = {'history', 'rollout', *_SINGLE_FILES}


def _root(home):
    home = Path(home)
    root = home / '.shadow-backups'
    if home.is_symlink() or root.is_symlink():
        raise ValueError('backup_path_symlink')
    return root


def _candidate(folder):
    match = _BACKUP_NAME.fullmatch(folder.name)
    if not match or not folder.is_dir() or folder.is_symlink():
        return None
    kind, stamp = match.group(1), int(match.group(2))
    files = list(folder.iterdir())
    if not files or any(not item.is_file() or item.is_symlink() for item in files):
        return None
    names = {item.name for item in files}
    if kind == 'history':
        if names != _SQLITE_FILES:
            return None
        for item in files:
            with item.open('rb') as stream:
                if stream.read(16) != b'SQLite format 3\x00':
                    return None
        key = 'database'
    elif kind == 'rollout':
        if len(files) != 1 or files[0].suffix != '.jsonl':
            return None
        key = files[0].name
    else:
        if names != {_SINGLE_FILES[kind]}:
            return None
        key = _SINGLE_FILES[kind]
    return kind, key, stamp


def prune_backups(home, *, dry_run=False, kinds=None):
    """Prune older recognized versions; return only counts and estimated bytes.

    Call while the manager's operation lock is held, or while it is stopped.
    The newest complete history pair and newest rollout for each file remain.
    """
    root = _root(home)
    result = {'deleted': 0, 'estimated_bytes': 0, 'retained': 0, 'skipped': 0}
    if not root.exists():
        return result
    if not root.is_dir():
        raise ValueError('backup_path_not_directory')
    allowed = set(kinds) if kinds is not None else _KINDS
    if not allowed <= _KINDS:
        raise ValueError('unsupported_backup_kind')
    groups = defaultdict(list)
    for folder in root.iterdir():
        candidate = _candidate(folder)
        if candidate is None or candidate[0] not in allowed:
            result['skipped'] += 1
            continue
        kind, key, stamp = candidate
        groups[(kind, key)].append((stamp, folder, candidate))
    for entries in groups.values():
        entries.sort(key=lambda row: row[0])
        result['retained'] += 1
        for _, folder, candidate in entries[:-1]:
            if (folder.parent != root or _candidate(folder) != candidate):
                result['skipped'] += 1
                continue
            size = sum(item.stat().st_blocks * 512 for item in folder.iterdir())
            if not dry_run:
                shutil.rmtree(folder)
            result['deleted'] += 1
            result['estimated_bytes'] += size
    return result
