"""Multi-source completed-history snapshots; never starts an app-server.

The hub is an archive, not a shared live database. Ordinary divergent branches
become separate task copies; paginated histories remain explicitly unsupported. Callers serialize sync and launches; external launches are
rechecked before every destination write. No credentials are copied.
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import time
import uuid
import shadow_history as history
from shadow_seed import BINARY


def _home(profile):
    return Path(profile.get('home') or profile.get('codex_home')).resolve()


def _safe_log(home, path):
    path = Path(path)
    if path.is_symlink() or home not in path.resolve().parents:
        raise ValueError('Rollout outside profile')
    return path


def _events(path):
    """Canonical events allow harmless settings records between completed turns."""
    meta = history._metadata(path)
    own_id = meta['id']; root_id = _root(path)
    with Path(path).open('rb') as stream:
        for line in stream:
            value = json.loads(line)
            payload = value.get('payload', {})
            if value.get('type') == 'session_meta':
                value = {'type': 'session_meta', 'payload': {'id': root_id}}
                payload = value['payload']
            for obj in (value, payload):
                if isinstance(obj, dict):
                    for key in ('thread_id', 'threadId'):
                        if obj.get(key) == own_id: obj[key] = root_id
            if (value.get('type') == 'event_msg' and
                    value.get('payload', {}).get('type') == 'thread_settings_applied'):
                continue
            if value.get('type') == 'response_item':
                payload.pop('internal_chat_message_metadata_passthrough', None)
            if value.get('type') == 'compacted':
                payload.pop('guardian_history', None)
            # Native migration materializes these optional protocol defaults.
            # Never normalize arbitrary tool arguments or structured results.
            if value.get('type') == 'response_item':
                optional = {'reasoning': ('content',), 'message': ('phase',)}.get(payload.get('type'), ())
                for key in optional:
                    if payload.get(key) is None: payload.pop(key, None)
            yield hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).digest()


def relation(a, b):
    """Return equal/ahead/behind/diverged for a relative to b, without timestamps."""
    # Most copies retain identical bytes. Avoid parsing whole conversation logs
    # for equal snapshots and straightforward append-only continuations.
    size_a, size_b = Path(a).stat().st_size, Path(b).stat().st_size
    remaining = min(size_a, size_b)
    with Path(a).open('rb') as left, Path(b).open('rb') as right:
        while remaining:
            count = min(1024 * 1024, remaining)
            block = left.read(count)
            if len(block) != count or block != right.read(count):
                break
            remaining -= count
        else:
            return 'equal' if size_a == size_b else ('ahead' if size_a > size_b else 'behind')
    sentinel = object()
    ia, ib = iter(_events(a)), iter(_events(b))
    while True:
        va, vb = next(ia, sentinel), next(ib, sentinel)
        if va is sentinel and vb is sentinel: return 'equal'
        if va is sentinel: return 'behind'
        if vb is sentinel: return 'ahead'
        if va != vb: return 'diverged'


def _complex(path):
    meta = history._metadata(path)
    return bool(meta.get('history_base') or meta.get('forked_from_id'))


def _archive_conflict(hub, origin, ident, path):
    directory = hub/'conflicts'/hashlib.sha256(ident.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = history.file_hash(path)
    dest = directory/(digest+'.jsonl')
    if not dest.exists(): history.frozen_rollout(path, dest)
    info = directory/(digest+'.json')
    if not info.exists():
        info.write_text(json.dumps({'thread_id': ident, 'source_home': str(origin)}))
        info.chmod(0o600)


def _root(path):
    meta = history._metadata(path)
    sidecar = Path(path).with_suffix('.shadow-lineage.json')
    if sidecar.exists():
        if sidecar.is_symlink(): raise ValueError('Lineage symlink forbidden')
        lineage = json.loads(sidecar.read_text())
        if lineage.get('thread_id') != meta['id'] or not isinstance(lineage.get('root_id'), str):
            raise ValueError('Invalid lineage identity')
        return lineage['root_id']
    return meta.get('shadow_sync', {}).get('root_id', meta['id'])


def _write_lineage(path, ident, root_id):
    sidecar = Path(path).with_suffix('.shadow-lineage.json')
    temporary = sidecar.with_suffix('.tmp')
    temporary.write_text(json.dumps({'thread_id': ident, 'root_id': root_id}))
    temporary.chmod(0o600)
    temporary.replace(sidecar)


def _unfinished(path):
    try: boundary = history.completed_prefix(path)['physical_end']
    except ValueError: return True
    with Path(path).open('rb') as stream:
        stream.seek(boundary)
        for line in stream:
            value = json.loads(line)
            if not (value.get('type') == 'event_msg' and value.get('payload', {}).get('type') == 'thread_settings_applied'):
                return True
    return False


def _rollout_name(row, ident=None):
    """Official paginated readers require the canonical rollout basename."""
    timestamp = datetime.fromtimestamp(int(row.get('created_at') or 0), timezone.utc)
    return 'rollout-'+timestamp.strftime('%Y-%m-%dT%H-%M-%S')+'-'+str(ident or row['id'])+'.jsonl'


def _migrate_branch(source, ident):
    """Hydrate only an isolated staged branch using the bundled offline CLI."""
    source = Path(source).resolve()
    # Keep credentials and storage overrides out of this offline command.
    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE')}
    env['CODEX_HOME'] = str(source)
    try:
        result = subprocess.run(
            [str(BINARY), 'migrate-rollouts', '--apply', '--thread', ident, '--json'],
            cwd=source, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Offline branch migration did not finish') from None
    if result.returncode:
        raise RuntimeError('Offline branch migration failed')
    with history.connect(source/history.STATE, True) as db:
        row = db.execute('SELECT * FROM threads WHERE id=?', (ident,)).fetchone()
    if row is None or row['history_mode'] != 'paginated':
        raise RuntimeError('Offline branch migration did not produce paginated history')
    _safe_log(source, row['rollout_path'])
    with history.connect(source/history.HISTORY, True) as db:
        if not db.execute('SELECT 1 FROM thread_turns WHERE thread_id=? LIMIT 1', (ident,)).fetchone():
            raise RuntimeError('Offline branch migration produced no completed turns')
    return dict(row)


def _branch_row(source, row, origin, ident, label):
    """Rewrite only known structural identity fields; never chat text or IDs in text."""
    directory = source/'sessions'/'shadow-branches'/history.file_hash(row['rollout_path'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory/_rollout_name(row, ident)
    root_id = _root(row['rollout_path'])
    # A prior migration can have changed this header to paginated; rebuild the
    # legacy input before clearing and regenerating its projection again.
    path.unlink(missing_ok=True)
    if not path.exists():
        with Path(row['rollout_path']).open('rb') as src, path.open('xb') as dst:
            for line in src:
                value = json.loads(line)
                payload = value.get('payload', {})
                if value.get('type') == 'session_meta':
                    payload['id'] = ident
                    if payload.get('session_id') == row['id']: payload['session_id'] = ident
                    if 'history_mode' in row: payload['history_mode'] = 'legacy'
                    payload['shadow_sync'] = {'root_id': root_id, 'origin': str(origin)}
                for obj in (value, payload):
                    if isinstance(obj, dict):
                        for key in ('thread_id', 'threadId'):
                            if obj.get(key) == row['id']: obj[key] = ident
                dst.write((json.dumps(value, ensure_ascii=False, separators=(',', ':'))+'\n').encode())
        path.chmod(0o600)
    staged = dict(row); staged['id'] = ident; staged['rollout_path'] = str(path)
    staged['title'] = row['title']+' ['+label+']'
    if 'history_mode' in staged:
        # A new UUID has no migrated projection. Its complete, non-paginated
        # log must go through the official legacy-to-index hydration path.
        staged['history_mode'] = 'legacy'
    with history.connect(source/history.STATE) as db:
        tools = [dict(r) for r in db.execute('SELECT * FROM thread_dynamic_tools WHERE thread_id=?', (row['id'],))]
        db.execute('DELETE FROM thread_dynamic_tools WHERE thread_id=?', (ident,))
        db.execute('DELETE FROM threads WHERE id=?', (ident,))
        history.insert(db, 'threads', staged)
        for item in tools:
            item['thread_id'] = ident
            history.insert(db, 'thread_dynamic_tools', item)
    with history.connect(source/history.HISTORY) as db:
        for table in history.PROJECTIONS:
            db.execute('DELETE FROM '+table+' WHERE thread_id=?', (ident,))
    # Rebuild changed offsets offline in the staging home only, never by opening
    # a live profile or issuing a model/resume request.
    migrated = _migrate_branch(source, ident) if 'history_mode' in staged else staged
    _write_lineage(migrated['rollout_path'], ident, root_id)
    return migrated


def _isolated_branch(source, row, origin, ident, label):
    temporary = tempfile.TemporaryDirectory(prefix='branch-stage-', dir=source.parent)
    stage = Path(temporary.name)
    try:
        for name in (history.STATE, history.HISTORY):
            with history.connect(source/name, True) as src, history.connect(stage/name) as dst:
                src.backup(dst)
            (stage/name).chmod(0o600)
        rewritten = _branch_row(stage, row, origin, ident, label)
        return temporary, stage, rewritten
    except BaseException:
        temporary.cleanup()
        raise


def sync_profiles(profiles, hub_home, is_running, progress=None):
    """Archive every source; fast-forward only closed, initialized profiles.

    is_running receives a profile dict and must fail closed on unknown state.
    Running profiles are read only and reported pending. Unsupported paginated
    and forked logs remain archived, never installed as apparently merged tasks.
    """
    profiles = list(profiles)
    homes = [_home(p) for p in profiles]
    hub = Path(hub_home).resolve()
    if len(set(homes)) != len(homes): raise ValueError('Duplicate profile homes')
    if any(hub == h or hub in h.parents or h in hub.parents for h in homes):
        raise ValueError('Hub and profile homes must be independent')
    result = {'status': 'complete', 'archived': 0, 'failed': 0, 'errors': {}, 'profiles': {}}
    def error(exc):
        result['failed'] += 1
        kind = type(exc).__name__
        result['errors'][kind] = result['errors'].get(kind, 0) + 1
    hub.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous_runs = sorted(hub.glob('snapshot-*'))
    previous = previous_runs[-1] if previous_runs else None
    run = hub / ('snapshot-' + str(time.time_ns()))
    run.mkdir(mode=0o700)
    candidates = []
    for profile, home in zip(profiles, homes):
        source = run / hashlib.sha256(str(home).encode()).hexdigest()[:20]
        source.mkdir(mode=0o700)
        cached = {}
        if previous:
            cache_path = previous/source.name/'cache.json'
            try: cached = json.loads(cache_path.read_text())
            except (OSError, ValueError): pass
        next_cache = {}
        try:
            for name in (history.STATE, history.HISTORY):
                with history.connect(home/name, True) as src, history.connect(source/name) as dest:
                    src.backup(dest)
                (source/name).chmod(0o600)
            with history.connect(source/history.STATE, True) as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM threads WHERE source NOT LIKE '%subagent%'")]
            for row in rows:
                try:
                    log = _safe_log(home, row['rollout_path'])
                    frozen = source / _rollout_name(row)
                    stat = log.stat()
                    signature = [str(log), stat.st_size, stat.st_mtime_ns]
                    old = cached.get(row['id'], {})
                    old_file = previous/source.name/frozen.name if previous else None
                    reused = old.get('signature') == signature and old_file and old_file.is_file()
                    if reused:
                        # Link only immutable hub files, never live profile files.
                        os.link(old_file, frozen)
                    else:
                        history.frozen_rollout(log, frozen)
                    meta = history._metadata(frozen)
                    if meta.get('id') != row['id']: raise ValueError('Rollout identity mismatch')
                    complex_log = _complex(frozen)
                    if not complex_log and not reused:
                        boundary = history.completed_prefix(frozen)
                        with frozen.open('r+b') as stream: stream.truncate(boundary['physical_end'])
                    _write_lineage(frozen, row['id'], _root(log))
                    next_cache[row['id']] = {'signature': signature}
                    staged = dict(row); staged['rollout_path'] = str(frozen)
                    with history.connect(source/history.STATE) as db:
                        db.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(frozen), row['id']))
                    result['archived'] += 1
                    if 'subagent' not in row['source']:
                        candidates.append((home, source, staged, complex_log))
                except (OSError, ValueError, RuntimeError, KeyError) as exc:
                    error(exc)
        except (OSError, ValueError, RuntimeError, history.sqlite3.Error) as exc:
            error(exc)
        cache_path = source/'cache.json'
        cache_path.write_text(json.dumps(next_cache)); cache_path.chmod(0o600)
        if progress: progress({'phase': 'archive', 'profile': profile['id'], 'archived': result['archived']})
    for profile, target in zip(profiles, homes):
        stats = {'imported': 0, 'updated': 0, 'conflicts': 0, 'failed': 0,
                 'skipped': 0, 'unsupported': 0, 'branches': 0, 'pending': False}
        result['profiles'][profile['id']] = stats
        unsupported_ids = set()
        try:
            if is_running(profile):
                stats['pending'] = True
                continue
            if any((target/n).is_symlink() for n in (history.STATE, history.HISTORY)):
                raise ValueError('Destination database symlink')
            with history.connect(target/history.STATE, True) as db:
                if not history.columns(db, 'threads'): raise ValueError('Uninitialized target')
                target_rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM threads')}
                synced_ids = ({r[0] for r in db.execute('SELECT target_id FROM shadow_history_imports')}
                              if history.columns(db, 'shadow_history_imports') else set())
            families = {}; broken = set()
            for item in target_rows.values():
                try:
                    candidate_path = _safe_log(target, item['rollout_path'])
                    candidate_root = _root(candidate_path)
                    families.setdefault(candidate_root, {})[item['id']] = (item, candidate_path)
                except (OSError, ValueError):
                    broken.add(item['id'])
            initialized = False
            installed_ids = []
            for origin, source, row, complex_log in candidates:
                if origin == target: continue
                if is_running(profile):
                    stats['pending'] = True
                    break
                branch_stage = None
                try:
                    if complex_log:
                        unsupported_ids.add(row['id'])
                        stats['unsupported'] = len(unsupported_ids)
                        continue
                    root_id = _root(row['rollout_path'])
                    if row['id'] in broken or root_id in broken:
                        raise ValueError('Colliding destination history unavailable')
                    family = list(families.get(root_id, {}).values())
                    matches = []
                    covered = False
                    for item, candidate_path in family:
                        if _complex(candidate_path): continue
                        comparison = relation(row['rollout_path'], candidate_path)
                        if comparison in ('equal', 'behind'):
                            if item['id'] in synced_ids:
                                installed_ids.append(item['id'])
                            covered = True; break
                        if comparison == 'ahead' and not _unfinished(candidate_path):
                            matches.append((item, candidate_path))
                    if covered:
                        stats['skipped'] += 1
                        continue
                    existing = None; path = None; branch = False
                    if matches:
                        # Prefer the existing same-ID task; any other same-lineage
                        # prefix can also fast-forward without duplicating a branch.
                        existing, path = next((m for m in matches if m[0]['id'] == row['id']), matches[0])
                        if existing['id'] != row['id']:
                            branch_stage, source, row = _isolated_branch(source, row, origin, existing['id'], 'sync')
                    elif family:
                        if any(_complex(p) for _, p in family):
                            unsupported_ids.add(row['id'])
                            stats['unsupported'] = len(unsupported_ids)
                            continue
                        _archive_conflict(hub, origin, row['id'], row['rollout_path'])
                        branch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, root_id+'|'+str(origin)))
                        occupied = set(target_rows)
                        # A prior branch itself may have been continued independently.
                        # Derive a distinct stable ID; next runs find its prefix above.
                        while branch_id in occupied:
                            branch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, branch_id+'|'+history.file_hash(row['rollout_path'])))
                        label = next((p.get('name') or p['id'] for p in profiles if _home(p)==origin), 'sync')
                        branch_stage, source, row = _isolated_branch(source, row, origin, branch_id, label)
                        branch = True
                    elif row['id'] != root_id:
                        # Keep the propagated alias identity; canonical comparisons
                        # against root_id prevent copies cycling back to their owner.
                        pass
                    # All destination writes, including backups/schema, follow this check.
                    if is_running(profile):
                        stats['pending'] = True
                        break
                    if not initialized:
                        history._backup(target)
                        history._history_schema(source/history.HISTORY, target/history.HISTORY)
                        with history.connect(target/history.STATE) as db:
                            db.execute('CREATE TABLE IF NOT EXISTS shadow_history_imports (source_home TEXT NOT NULL,source_id TEXT NOT NULL,target_id TEXT NOT NULL,imported_at INTEGER NOT NULL,end_byte_offset INTEGER NOT NULL,end_ordinal INTEGER NOT NULL,PRIMARY KEY(source_home,source_id))')
                        initialized = True
                    if is_running(profile):
                        stats['pending'] = True
                        break
                    staged = dict(row)
                    if existing:
                        for key in ('title', 'name', 'is_pinned'): staged[key] = existing[key]
                    history._import_one(staged, target, source/history.STATE, source/history.HISTORY,
                                        None, origin, replace_existing=bool(existing), existing_path=path)
                    with history.connect(target/history.STATE, True) as db:
                        installed = dict(db.execute('SELECT * FROM threads WHERE id=?', (staged['id'],)).fetchone())
                    if is_running(profile):
                        stats['pending'] = True
                        break
                    _write_lineage(installed['rollout_path'], installed['id'], root_id)
                    target_rows[installed['id']] = installed
                    families.setdefault(root_id, {})[installed['id']] = (installed, Path(installed['rollout_path']))
                    stats['updated' if existing else 'imported'] += 1
                    installed_ids.append(staged['id'])
                    if branch: stats['branches'] += 1
                except (OSError, ValueError, RuntimeError, KeyError, history.sqlite3.Error) as exc:
                    stats['failed'] += 1
                    error(exc)
                finally:
                    if branch_stage is not None: branch_stage.cleanup()
            if installed_ids and not is_running(profile):
                from shadow_history_state import assign_synced_projects
                stats['projects_assigned'] = assign_synced_projects(
                    target, installed_ids, lambda: not is_running(profile))
            # _import_one records provenance transactionally; a manifest isn't
            # required by sync, so do not write one after a profile starts.
        except (OSError, ValueError, RuntimeError, history.sqlite3.Error) as exc:
            stats['failed'] += 1
            error(exc)
        finally:
            if progress: progress({'phase': 'distribute', 'profile': profile['id'], **stats})
    if result['failed'] or any(s['pending'] or s['conflicts'] or s['unsupported'] for s in result['profiles'].values()):
        result['status'] = 'partial'
    receipt = run / 'receipt.json'
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    receipt.chmod(0o600)
    for obsolete in sorted(hub.glob('snapshot-*'))[:-2]:
        if obsolete.is_dir() and not obsolete.is_symlink(): shutil.rmtree(obsolete)
    return result
