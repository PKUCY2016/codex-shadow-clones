"""Snapshot preferences and project references into a stopped, independent Codex home.

No login files, Electron cookies, installation IDs or task databases are copied.
Destination projects are created through the bundled official app-server protocol.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import threading
import time
import tempfile
import tomllib

BINARY = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
# Only presentation preferences whose values do not refer to identities/threads.
ATOM_KEYS = {'sidebar-width', 'app-shell:right-panel-width:v3',
             'sidebar-project-list-expanded-v1', 'composer-auto-context-enabled',
             'composer-model-picker-selection-mode-v1'}
ISOLATION_KEYS = {'cli_auth_credentials_store', 'sqlite_home',
                  'forced_chatgpt_workspace_id', 'forced_login_method'}


def safe_config(text: str) -> str:
    """Retain TOML formatting while replacing identity/storage top-level settings."""
    tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    result = []
    in_table = False
    for line in lines:
        if line.lstrip().startswith('['):
            in_table = True
        m = re.match(r'^\s*([A-Za-z0-9_-]+)\s*=', line)
        if not in_table and m and m.group(1) in ISOLATION_KEYS:
            continue
        result.append(line)
    text = 'cli_auth_credentials_store = "file"\n' + ''.join(result)
    parsed = tomllib.loads(text)
    if any(k in parsed for k in ISOLATION_KEYS - {'cli_auth_credentials_store'}):
        raise RuntimeError('Unsupported configuration storage override; seed stopped')
    return text


class ProjectRPC:
    def __init__(self, home: Path):
        env = dict(os.environ)
        for k in ('CODEX_SQLITE_HOME', 'CODEX_ELECTRON_USER_DATA_PATH',
                  'OPENAI_API_KEY', 'CODEX_API_KEY'):
            env.pop(k, None)
        env['CODEX_HOME'] = str(home)
        self.proc = subprocess.Popen([str(BINARY), 'app-server'], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        self.lines = queue.Queue()
        self.serial = 0
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.call('initialize', {'clientInfo': {'name':'shadow_clones_seed','version':'0.1'},
                'capabilities': {'experimentalApi':True}})
            self.proc.stdin.write('{"method":"initialized","params":{}}\n')
            self.proc.stdin.flush()
        except BaseException:
            self.close()
            raise

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def call(self, method, params):
        self.serial += 1
        self.proc.stdin.write(json.dumps({'id':self.serial,'method':method,'params':params})+'\n')
        self.proc.stdin.flush()
        deadline = time.monotonic()+20
        while True:
            try:
                line = self.lines.get(timeout=max(0.01,deadline-time.monotonic()))
            except queue.Empty:
                raise RuntimeError('Codex project RPC timed out') from None
            if line is None:
                raise RuntimeError('Codex project RPC exited')
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get('id') != self.serial:
                continue
            if 'error' in value:
                raise RuntimeError('Codex project RPC failed: '+method)
            return value.get('result',{})

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                stream.close()


def source_projects(home: Path) -> list[dict]:
    db = home/'state_5.sqlite'
    if not db.exists():
        return []
    conn = sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        conn.execute('BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'projects','project_roots'}.issubset(tables):
            return []
        projects = []
        for ident,name,metadata in conn.execute('SELECT id,name,metadata FROM projects ORDER BY position'):
            roots = [{'path':r[0]} for r in conn.execute(
                'SELECT path FROM project_roots WHERE project_id=? ORDER BY position',(ident,))]
            projects.append({'id':ident,'name':name,'metadata':json.loads(metadata),'roots':roots})
        return projects
    finally:
        conn.close()



def merge_project_state(source: dict, target: dict, server_projects: list[dict], target_home: Path) -> int:
    """Seed the desktop cache as well as Core projects; keep destination identities.

    The installed desktop still reads local-projects for its sidebar. Core RPC
    project/create alone does not publish entries into this desktop-owned cache.
    """
    local = target.setdefault('local-projects', {})
    by_roots = {tuple(p.get('rootPaths', [])): ident for ident, p in local.items()}
    server_ids = {tuple(r['path'] for r in p['roots']): p['id']
                  for p in server_projects if p.get('id')}
    host_key = 'local:' + str(target_home.resolve())
    mappings = target.setdefault('app-server-project-id-by-legacy-project-id-by-host', {}).setdefault(host_key, {})
    source_local = source.get('local-projects', {})
    source_order = source.get('project-order', [])
    ordered = list(dict.fromkeys([*source_order, *source_local]))
    order = list(target.get('project-order', []))
    added = 0
    for ident in ordered:
        p = source_local.get(ident)
        if not isinstance(p, dict) or not isinstance(p.get('name'), str):
            continue
        roots = p.get('rootPaths')
        if not isinstance(roots, list) or not roots or not all(isinstance(r, str) for r in roots):
            continue
        root_key = tuple(roots)
        target_id = by_roots.get(root_key)
        if target_id is None:
            # UUID collision with a different destination project must not replace it.
            if ident in local:
                import uuid
                target_id = str(uuid.uuid4())
            else:
                target_id = ident
            local[target_id] = {'id': target_id, 'name': p['name'], 'rootPaths': roots[:],
                'createdAt': p.get('createdAt', 0), 'updatedAt': p.get('updatedAt', 0)}
            by_roots[root_key] = target_id
            added += 1
        if target_id not in order:
            order.append(target_id)
        if root_key in server_ids:
            mappings[target_id] = server_ids[root_key]
    target['project-order'] = order
    return added


def seed_home(source_home: Path, target_home: Path) -> dict:
    """Caller must hold profile lock and ensure destination desktop is stopped."""
    source_home, target_home = Path(source_home).resolve(), Path(target_home).resolve()
    if source_home == target_home or target_home in source_home.parents:
        raise ValueError('Source and target must be independent homes')
    config = safe_config((source_home/'config.toml').read_text()) if (source_home/'config.toml').exists() else 'cli_auth_credentials_store = "file"\n'
    projects = source_projects(source_home)
    target_home.mkdir(parents=True,exist_ok=True,mode=0o700)
    backups = target_home/'.shadow-backups'/str(time.time_ns())
    count = 0
    def put(relative: Path, data: bytes):
        nonlocal count
        target = target_home/relative
        if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != target_home.parent):
            raise ValueError('Destination symlinks cannot be overwritten')
        if target.exists():
            if target.read_bytes() == data:
                return
            backup = backups/relative
            backup.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            backup.write_bytes(target.read_bytes())
            backup.chmod(0o600)
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.shadow-', dir=target.parent)
        tmp = Path(temporary)
        with os.fdopen(fd,'wb') as f:
            f.write(data)
        tmp.replace(target)
        count += 1
    put(Path('config.toml'),config.encode())
    if (source_home/'AGENTS.md').is_file():
        put(Path('AGENTS.md'),(source_home/'AGENTS.md').read_bytes())
    for directory in ('skills','rules'):
        base = source_home/directory
        if not base.is_dir():
            continue
        for f in sorted(base.rglob('*')):
            if f.is_symlink() or not f.is_file() or '__pycache__' in f.parts or '.git' in f.parts:
                continue
            put(f.relative_to(source_home), f.read_bytes())
            # Preserve executable scripts without preserving broad source permissions.
            if f.stat().st_mode & 0o111:
                (target_home/f.relative_to(source_home)).chmod(0o700)
    source = {}
    state_path = target_home/'.codex-global-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    source_state = source_home/'.codex-global-state.json'
    if source_state.exists():
        source = json.loads(source_state.read_text())
        atoms = state.setdefault('electron-persisted-atom-state',{})
        for k in ATOM_KEYS:
            if k in source.get('electron-persisted-atom-state',{}):
                atoms[k] = source['electron-persisted-atom-state'][k]
    rpc = ProjectRPC(target_home)
    try:
        existing = []
        cursor = None
        while True:
            page = rpc.call('project/list',{'limit':100,'cursor':cursor})
            existing.extend(page.get('data',[]))
            cursor = page.get('nextCursor')
            if not cursor:
                break
        def key(p):
            return tuple(r['path'] for r in p['roots'])
        known = {key(p) for p in existing}
        created = 0
        for p in projects:
            if key(p) in known:
                continue
            rpc.call('project/create',{
                'idempotencyKey':'shadow-seed-'+hashlib.sha256((str(source_home)+p['id']).encode()).hexdigest(),
                'name':p['name'],'roots':p['roots'],'metadata':{}})
            known.add(key(p))
            created += 1
        # Re-read assigned Core IDs instead of assuming they match source IDs.
        server_projects = []
        cursor = None
        while True:
            page = rpc.call('project/list', {'limit': 100, 'cursor': cursor})
            server_projects.extend(page.get('data', []))
            cursor = page.get('nextCursor')
            if not cursor:
                break
    finally:
        rpc.close()
    desktop_added = merge_project_state(source, state, server_projects, target_home)
    put(Path('.codex-global-state.json'), json.dumps(state,ensure_ascii=False).encode())
    return {'projects':len(projects),'projects_added':created,'desktop_projects_added':desktop_added,
            'files':count,'status':'synced'}
