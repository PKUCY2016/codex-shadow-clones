"""Independent local history snapshots with separate files and SQLite indexes.

Caller owns the profile lock and must stop the destination desktop/app-server.
No auth, goal, queue, remote-control or source databases are imported.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
import queue
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from shadow_seed import ProjectRPC


class HistoryRPC(ProjectRPC):
    def call(self, method, params):
        self.serial += 1
        serial=self.serial
        self.proc.stdin.write(json.dumps({'id':serial,'method':method,'params':params})+'\n');self.proc.stdin.flush()
        deadline=time.monotonic()+180
        while True:
            try:line=self.lines.get(timeout=max(0.01,deadline-time.monotonic()))
            except queue.Empty:raise RuntimeError('History RPC timed out') from None
            if line is None:raise RuntimeError('History RPC exited')
            try:value=json.loads(line)
            except json.JSONDecodeError:continue
            if value.get('id')!=serial:continue
            if 'error' in value:raise RuntimeError('History RPC failed: '+method)
            return value.get('result',{})


STATE = 'state_5.sqlite'
HISTORY = 'thread_history_1.sqlite'
PROJECTIONS = ('thread_turns','thread_items','thread_history_projection_state','thread_realtime_items')


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:return super().__exit__(exc_type, exc_value, traceback)
        finally:self.close()


def connect(path, readonly=False):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,factory=ClosingConnection) if readonly else sqlite3.connect(path,factory=ClosingConnection)
    c.row_factory=sqlite3.Row
    return c


def source_threads(home):
    with connect(Path(home)/STATE,True) as c:
        return [dict(r) for r in c.execute("SELECT * FROM threads WHERE source NOT LIKE '%subagent%' ORDER BY updated_at DESC")]


def columns(c,table):
    return tuple(r['name'] for r in c.execute('PRAGMA table_info('+table+')'))


def insert(c,table,row,prefix=''):
    names=tuple(row)
    c.execute('INSERT INTO '+prefix+table+' ('+','.join(names)+') VALUES ('+','.join('?' for _ in names)+')',tuple(row.values()))


def frozen_rollout(source,target):
    """Independent point-in-time file; trim only incomplete final JSONL line."""
    source,target=Path(source),Path(target)
    if source.is_symlink() or not source.is_file():
        raise ValueError('Source rollout is not a regular file')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    cloned=False
    if sys.platform=='darwin':
        lib=ctypes.CDLL(None,use_errno=True)
        cloned=lib.clonefile(os.fsencode(source),os.fsencode(target),0)==0
    if not cloned:
        # Bound the copy at the size observed before reading an actively appended file.
        remaining=source.stat().st_size
        with source.open('rb') as src,target.open('xb') as dst:
            while remaining:
                chunk=src.read(min(1024*1024,remaining))
                if not chunk: raise RuntimeError('Source rollout changed while copying')
                dst.write(chunk); remaining-=len(chunk)
    target.chmod(0o600)
    with target.open('r+b') as f:
        f.seek(0,2);end=f.tell()
        if end:
            f.seek(end-1)
            if f.read(1)!=b'\n':
                cursor=end
                while cursor:
                    start=max(0,cursor-65536);f.seek(start);chunk=f.read(cursor-start)
                    newline=chunk.rfind(b'\n')
                    if newline>=0:
                        f.truncate(start+newline+1);break
                    cursor=start
                else: raise ValueError('Rollout has no complete JSONL record')
    return target


def _backup(target):
    dest=target/'.shadow-backups'/('history-'+str(time.time_ns()))
    dest.mkdir(parents=True,mode=0o700)
    for name in (STATE,HISTORY):
        if (target/name).exists():
            with connect(target/name,True) as src,connect(dest/name) as dst:src.backup(dst)
            (dest/name).chmod(0o600)
    return dest


def _write_manifest(target):
    with connect(target/STATE,True) as c:
        rows=[dict(r) for r in c.execute('SELECT source_home,source_id,target_id,imported_at,end_byte_offset,end_ordinal FROM shadow_history_imports')]
    path=target/'.shadow-history-imports.json';temporary=path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'version':1,'imports':rows},ensure_ascii=False));temporary.chmod(0o600);temporary.replace(path)


def _history_schema(source_history,target_history):
    with connect(source_history,True) as src,connect(target_history) as dest:
        if not columns(dest,'thread_turns'):
            for r in src.execute("SELECT sql FROM sqlite_master WHERE type IN ('table','index') AND sql IS NOT NULL ORDER BY type DESC"):
                dest.execute(r[0])
            for r in src.execute('SELECT * FROM _sqlx_migrations'):
                insert(dest,'_sqlx_migrations',dict(r))
        for table in PROJECTIONS:
            if columns(src,table)!=columns(dest,table):raise RuntimeError('History schema mismatch')


def _metadata(path):
    with Path(path).open('rb') as f:
        line=f.readline(32*1024*1024)
    if not line.endswith(b'\n'):raise ValueError('Invalid session metadata')
    value=json.loads(line)
    if value.get('type')!='session_meta':raise ValueError('Missing session metadata')
    return value.get('payload',{})


def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def unchanged_snapshot(target, source, baseline=None):
    """Ignore only trailing desktop settings events, never conversation events."""
    target = Path(target)
    boundary = 0
    with target.open('rb') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                return False
            settings = (event.get('type') == 'event_msg' and
                        event.get('payload', {}).get('type') == 'thread_settings_applied')
            if not settings:
                boundary = stream.tell()
    if baseline:
        expected, size = baseline
        # Imported snapshots end at a terminal event. Only settings may follow.
        if boundary != size:
            return False
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            remaining = size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest() == expected
    with target.open('rb') as a, Path(source).open('rb') as b:
        remaining = boundary
        while remaining:
            chunk = a.read(min(1024 * 1024, remaining))
            if not chunk or chunk != b.read(len(chunk)):
                return False
            remaining -= len(chunk)
    return boundary > 0


def completed_prefix(path):
    """Derive the frozen boundary from the log, never its lazy SQLite cache."""
    end=0;count=0;header_end=0;metadata={};completed=0
    with Path(path).open('rb') as f:
        for index,line in enumerate(f):
            value=json.loads(line)
            if index==0:
                if value.get('type')!='session_meta':raise ValueError('Missing session metadata')
                metadata=value.get('payload',{});header_end=f.tell()
            if value.get('type')=='event_msg' and value.get('payload',{}).get('type') in ('task_complete','turn_aborted'):
                end=f.tell();count=index+1;completed+=1
    base=metadata.get('history_base') or {}
    if not end:
        if not base:raise ValueError('No completed source turn')
        end=header_end;count=1
    # Paginated suffixes address the inherited prefix using logical offsets.
    logical_end=end;logical_count=count
    if base:
        logical_end=int(base['end_byte_offset'])+end-header_end
        logical_count=int(base['end_ordinal_exclusive'])+count-1
    return {'physical_end':end,'logical_end':logical_end,'logical_ordinal':logical_count,
            'completed_events':completed,'metadata':metadata}


def _import_one(row,target,source_state,source_history,bound,source_home,raw_snapshot=False,replace_existing=False,existing_path=None):
    ident=row['id']
    final=Path(existing_path) if replace_existing and existing_path else target/'sessions'/'shadow-imports'/Path(row['rollout_path']).name
    if final.exists() and not replace_existing:raise RuntimeError('Destination rollout conflict')
    destination=final
    old_backup=None
    if replace_existing:
        old_backup=target/'.shadow-backups'/('rollout-'+str(time.time_ns()))/final.name
        frozen_rollout(final,old_backup)
        final=final.with_name('.repair-'+str(time.time_ns())+'-'+final.name)
    created=[]
    try:
        frozen_rollout(row['rollout_path'],final);created.append(final)
        prefix=completed_prefix(final)
        with final.open('r+b') as f:f.truncate(prefix['physical_end'])
        if raw_snapshot:
            copied_base=False
            for directory in ('sessions','archived_sessions'):
                for base_file in (Path(source_home)/directory).rglob('*'+ident+'*.jsonl'):
                    if base_file.resolve()==Path(row['rollout_path']).resolve():continue
                    if _metadata(base_file).get('id')!=ident:continue
                    base_target=target/'sessions'/'shadow-imports'/base_file.name
                    if not base_target.exists():
                        frozen_rollout(base_file,base_target);created.append(base_target)
                    copied_base=True
            if not copied_base:raise RuntimeError('Self-base source rollout unavailable')
        staged=dict(row);staged['rollout_path']=str(destination.resolve())
        staged['project_id']=None;staged['thread_section_id']=None;staged['section_position']=None;staged['section_entered_at_ms']=None
        with connect(target/STATE) as dest,connect(source_state,True) as state,connect(source_history,True) as hist:
            if tuple(staged)!=columns(dest,'threads'):raise RuntimeError('Codex thread schema mismatch')
            dest.execute('ATTACH DATABASE ? AS history',(str(target/HISTORY),))
            if replace_existing:
                # History refresh must not reset the destination's sidebar.
                current = dest.execute('SELECT * FROM threads WHERE id=?', (ident,)).fetchone()
                if current is None:
                    raise RuntimeError('Destination thread disappeared during refresh')
                for key in ('project_id', 'thread_section_id', 'section_position',
                            'section_entered_at_ms', 'is_pinned'):
                    staged[key] = current[key]
                for table in PROJECTIONS:dest.execute('DELETE FROM history.'+table+' WHERE thread_id=?',(ident,))
                dest.execute('DELETE FROM thread_dynamic_tools WHERE thread_id=?',(ident,))
                dest.execute('DELETE FROM threads WHERE id=?',(ident,))
                dest.execute('DELETE FROM shadow_history_imports WHERE source_home=? AND source_id=?',(str(source_home),ident))
            insert(dest,'threads',staged)
            for r in state.execute('SELECT * FROM thread_dynamic_tools WHERE thread_id=?',(ident,)):
                insert(dest,'thread_dynamic_tools',dict(r))
            for table in ('thread_turns','thread_items','thread_realtime_items'):
                for cached in hist.execute('SELECT * FROM '+table+' WHERE thread_id=? AND rollout_ordinal < ?',(ident,prefix['logical_ordinal'])):
                    data=dict(cached)
                    if table=='thread_turns' and data['status']=='inProgress':continue
                    insert(dest,table,data,prefix='history.')
            cached=hist.execute('SELECT * FROM thread_history_projection_state WHERE thread_id=?',(ident,)).fetchone()
            if cached:
                data=dict(cached)
                if data['next_rollout_byte_offset']>prefix['logical_end'] or data['next_rollout_ordinal']>prefix['logical_ordinal']:
                    data['next_rollout_byte_offset']=prefix['logical_end'];data['next_rollout_ordinal']=prefix['logical_ordinal']
                insert(dest,'thread_history_projection_state',data,prefix='history.')
            # Preserve the cache's actual checkpoint; never pretend a stale cache
            # has already projected all newly copied completed log records.
            dest.execute('INSERT INTO shadow_history_imports VALUES(?,?,?,?,?,?)',(str(source_home),ident,ident,int(time.time()),prefix['logical_end'],prefix['logical_ordinal']))
            dest.execute('CREATE TABLE IF NOT EXISTS shadow_history_files (thread_id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,physical_bytes INTEGER NOT NULL)')
            dest.execute('INSERT OR REPLACE INTO shadow_history_files VALUES(?,?,?)',(ident,file_hash(final),final.stat().st_size))
            if replace_existing:final.replace(destination)
        return prefix['logical_end'],prefix['logical_ordinal']
    except BaseException:
        for path in created:path.unlink(missing_ok=True)
        if old_backup and old_backup.exists():shutil.copyfile(old_backup,destination)
        raise


def validate_parent_prefix(base,parent,frozen_bounds):
    if parent and base.get('thread_id')==parent and int(base.get('end_byte_offset',0))>frozen_bounds.get(parent,(0,0))[0]:
        raise RuntimeError('Ancestor completed snapshot is shorter than fork dependency')


def primary_threads(allrows):
    return [r for r in allrows.values() if 'subagent' not in r['source']]


def import_history(source_home,target_home,progress=None):
    """Snapshot completed primary task history and required ancestors.

    IDs intentionally stay stable: each profile owns separate files and databases.
    Existing destination tasks are never replaced. No model or resume call occurs.
    """
    source,target=Path(source_home).resolve(),Path(target_home).resolve()
    if source==target or target in source.parents or source in target.parents:
        raise ValueError('Source and target homes must be independent')
    if any((target/name).is_symlink() for name in (STATE,HISTORY)):
        raise ValueError('Destination database symlinks are forbidden')
    target.mkdir(parents=True,exist_ok=True,mode=0o700)
    rpc=HistoryRPC(target);rpc.close()
    # Online SQLite backups include WAL and avoid copying a live DB file unsafely.
    with tempfile.TemporaryDirectory(prefix='shadow-history-',dir=target.parent) as temp:
        stage=Path(temp);stage.chmod(0o700)
        for name in (STATE,HISTORY):
            with connect(source/name,True) as src,connect(stage/name) as dst:src.backup(dst)
            (stage/name).chmod(0o600)
        with connect(stage/STATE,True) as c:
            allrows={r['id']:dict(r) for r in c.execute('SELECT * FROM threads')}
        primaries=primary_threads(allrows)
        _backup(target)
        _history_schema(stage/HISTORY,target/HISTORY)
        with connect(target/STATE) as c:
            c.execute('CREATE TABLE IF NOT EXISTS shadow_history_imports (source_home TEXT NOT NULL,source_id TEXT NOT NULL,target_id TEXT NOT NULL,imported_at INTEGER NOT NULL,end_byte_offset INTEGER NOT NULL,end_ordinal INTEGER NOT NULL,PRIMARY KEY(source_home,source_id))')
            frozen_bounds={r['source_id']:(r['end_byte_offset'],r['end_ordinal']) for r in c.execute('SELECT source_id,end_byte_offset,end_ordinal FROM shadow_history_imports WHERE source_home=?',(str(source),))}
            known=set(frozen_bounds)
            existing={r['id'] for r in c.execute('SELECT id FROM threads')}
        with connect(stage/HISTORY,True) as c:
            in_progress={r['thread_id'] for r in c.execute("SELECT DISTINCT thread_id FROM thread_turns WHERE status='inProgress'")}
            projection_bounds={r['thread_id']:(r['next_rollout_byte_offset'],r['next_rollout_ordinal']) for r in c.execute('SELECT * FROM thread_history_projection_state')}
            bounds={r['thread_id']:(r['rollout_end_byte_offset'],r['rollout_end_ordinal']) for r in c.execute("SELECT thread_id,rollout_end_byte_offset,rollout_end_ordinal FROM thread_turns WHERE status != 'inProgress' AND rollout_end_byte_offset IS NOT NULL AND rollout_end_ordinal IS NOT NULL ORDER BY rollout_ordinal")}
        result={'total':len(primaries),'imported':0,'skipped':0,'failed':0,'dependencies_imported':0,'status':'complete','errors':{}}
        originally_known=set(known)
        finished={};visiting=set()
        def add(ident):
            if ident in known:return 'skipped'
            if ident in finished:
                if finished[ident]=='failed':raise RuntimeError('History ancestor unavailable')
                return 'skipped'
            if ident in visiting:raise RuntimeError('History lineage cycle')
            if ident in existing:raise RuntimeError('Destination ID conflict')
            if ident not in allrows:raise FileNotFoundError('Source ancestor index missing')
            row=allrows[ident]
            visiting.add(ident)
            try:
                metadata=_metadata(row['rollout_path'])
                base=metadata.get('history_base') or {}
                parent=metadata.get('forked_from_id') or (base.get('thread_id') if base.get('thread_id')!=ident else None)
                if parent:add(parent)
                raw_snapshot=base.get('thread_id')==ident
                bound=None
                # Reject descendants whose required parent prefix was not frozen in full.
                validate_parent_prefix(base,parent,frozen_bounds)
                original=dict(row)
                # source_home is transaction metadata, not a column in the official thread table.
                bound=_import_one(original,target,stage/STATE,stage/HISTORY,bound,source,raw_snapshot=raw_snapshot)
                known.add(ident);frozen_bounds[ident]=bound;existing.add(ident);finished[ident]='imported'
                if 'subagent' in row['source']:result['dependencies_imported']+=1
                _write_manifest(target)
                return 'imported'
            except BaseException:
                finished[ident]='failed';raise
            finally:visiting.discard(ident)
        for index,row in enumerate(sorted(primaries,key=lambda r:r['updated_at'],reverse=True)):
            try:
                status=add(row['id'])
                result['skipped' if row['id'] in originally_known else 'imported']+=1
            except (OSError,ValueError,RuntimeError,sqlite3.Error,KeyError) as exc:
                result['failed']+=1
                key=type(exc).__name__+(':'+str(exc) if isinstance(exc,RuntimeError) else '')
                result['errors'][key]=result['errors'].get(key,0)+1
            if progress:progress({'done':index+1,'total':len(primaries),**{k:result[k] for k in ('imported','skipped','failed')}})
        if result['failed']:result['status']='partial'
        _write_manifest(target)
        return result


def repair_history(source_home,target_home,progress=None):
    """Repair only unchanged imported log snapshots; caller must stop the target."""
    source,target=Path(source_home).resolve(),Path(target_home).resolve()
    if source==target:raise ValueError('Source and target must differ')
    if not (target/STATE).exists():return {'total':0,'repaired':0,'conflicts':0,'failed':0,'status':'complete','errors':{}}
    with connect(target/STATE,True) as check:
        if not columns(check,'shadow_history_imports'):return {'total':0,'repaired':0,'conflicts':0,'failed':0,'status':'complete','errors':{}}
    result={'total':0,'repaired':0,'conflicts':0,'failed':0,'status':'complete','errors':{}}
    with tempfile.TemporaryDirectory(prefix='shadow-history-repair-',dir=target.parent) as temp:
        stage=Path(temp)
        for name in (STATE,HISTORY):
            with connect(source/name,True) as src,connect(stage/name) as dst:src.backup(dst)
        with connect(stage/STATE,True) as c:rows={r['id']:dict(r) for r in c.execute('SELECT * FROM threads')}
        with connect(target/STATE,True) as c:
            imports=[dict(r) for r in c.execute('SELECT * FROM shadow_history_imports WHERE source_home=?',(str(source),))]
            existing={r['id']:dict(r) for r in c.execute('SELECT * FROM threads')}
            baseline={r['thread_id']:(r['sha256'],r['physical_bytes']) for r in c.execute('SELECT * FROM shadow_history_files')} if columns(c,'shadow_history_files') else {}
        # Refresh ancestors first; never point a newer child at an older frozen parent.
        indexed={item['source_id']:item for item in imports}
        ordered=[];visited=set();pending=set()
        def order(ident):
            if ident in visited:return
            if ident in pending:raise RuntimeError('History lineage cycle')
            pending.add(ident)
            try:
                meta=_metadata(rows[ident]['rollout_path'])
                parent=meta.get('forked_from_id')
                if parent in indexed:order(parent)
            except (OSError,ValueError,KeyError):pass
            pending.remove(ident);visited.add(ident);ordered.append(indexed[ident])
        for ident in indexed:order(ident)
        imports=ordered
        frozen_bounds={x['source_id']:(x['end_byte_offset'],x['end_ordinal']) for x in imports}
        result['total']=len(imports);_backup(target)
        for i,item in enumerate(imports):
            ident=item['source_id']
            try:
                original=rows[ident];current=existing[ident]
                path=Path(current['rollout_path'])
                unchanged=unchanged_snapshot(path,original['rollout_path'],baseline.get(ident))
                if not unchanged:
                    result['conflicts']+=1
                else:
                    row=dict(original)
                    for key in ('title','name','is_pinned'):row[key]=current[key]
                    metadata=_metadata(original['rollout_path'])
                    base=metadata.get('history_base') or {}
                    raw=base.get('thread_id')==ident
                    parent=metadata.get('forked_from_id') or (base.get('thread_id') if base.get('thread_id')!=ident else None)
                    validate_parent_prefix(base,parent,frozen_bounds)
                    frozen_bounds[ident]=_import_one(row,target,stage/STATE,stage/HISTORY,None,source,raw_snapshot=raw,replace_existing=True,existing_path=path)
                    result['repaired']+=1
                    _write_manifest(target)
            except (OSError,ValueError,RuntimeError,sqlite3.Error,KeyError) as exc:
                result['failed']+=1
                key=type(exc).__name__+(':'+str(exc) if isinstance(exc,RuntimeError) else '')
                result['errors'][key]=result['errors'].get(key,0)+1
            if progress:progress({'done':i+1,'total':len(imports),**result})
        if result['failed'] or result['conflicts']:result['status']='partial'
        return result
