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


def without_bundled_marketplace(text: str) -> str:
    """Discard the desktop-owned runtime source, preserving all other settings.

    Each desktop registers its own openai-bundled market on startup. Carrying
    the source home's temporary marketplace creates a same-name/source conflict.
    Reject uncommon inline layouts rather than rewriting arbitrary user TOML.
    """
    original = tomllib.loads(text)
    markets = original.get('marketplaces', {})
    if 'openai-bundled' not in markets:
        return text
    headers = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('['):
            try:
                # A complete TOML prefix proves this is not a line in a string.
                tomllib.loads(text[:offset])
                tree = tomllib.loads(line+'\n__shadow_table_probe__ = true\n')
                path = []
                while isinstance(tree, (dict, list)):
                    if isinstance(tree, list):
                        tree = tree[-1]
                    elif '__shadow_table_probe__' in tree:
                        break
                    else:
                        key, tree = next(iter(tree.items()))
                        path.append(key)
                headers.append((offset, tuple(path)))
            except (ValueError, StopIteration):
                pass
        offset += len(line)
    result = []
    start = 0
    for index, (position, path) in enumerate(headers):
        if path[:2] != ('marketplaces', 'openai-bundled'):
            continue
        end = headers[index+1][0] if index+1 < len(headers) else len(text)
        result.append(text[start:position])
        start = end
    result.append(text[start:])
    cleaned = ''.join(result)
    expected = tomllib.loads(text)
    expected['marketplaces'].pop('openai-bundled')
    actual = tomllib.loads(cleaned)
    # An empty namespace can be implicit or have an explicit [marketplaces].
    for value in (expected, actual):
        if value.get('marketplaces') == {}:
            value.pop('marketplaces')
    if actual != expected:
        raise RuntimeError('内置插件市场配置不是独立 TOML 表，无法安全复制；未修改目标配置。')
    return cleaned


def repair_bundled_marketplace(home: Path, can_write) -> bool:
    """Repair an inherited desktop market in a stopped target, with a backup.

    This leaves login, plugin permissions, and other preferences unchanged.
    The caller must serialize launches and recheck target process state.
    """
    home = Path(home)
    config = home/'config.toml'
    if config.is_symlink():
        raise RuntimeError('配置文件是符号链接，未修复插件市场。')
    if not config.exists():
        return False
    before = config.read_text()
    market = tomllib.loads(before).get('marketplaces', {}).get('openai-bundled')
    if not isinstance(market, dict):
        return False
    expected = home.resolve()/'.tmp/bundled-marketplaces/openai-bundled'
    if market.get('source_type') == 'local' and market.get('source') == str(expected):
        return False
    cleaned = without_bundled_marketplace(before)
    if cleaned == before:
        return False
    if not can_write():
        raise RuntimeError('目标分身正在运行，未修复插件市场。')
    parent = home/'.shadow-backups'
    if parent.is_symlink():
        raise RuntimeError('备份目录是符号链接，未修复插件市场。')
    parent.mkdir(exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix='desktop-marketplace-', dir=parent))
    previous = backup/'config.toml'
    fd = os.open(previous, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(before)
    fd, name = tempfile.mkstemp(prefix='.shadow-config-', suffix='.tmp', dir=home)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(cleaned)
        if not can_write() or config.is_symlink() or config.read_text() != before:
            raise RuntimeError('分身或配置状态已改变，保留原配置，未修复插件市场。')
        os.replace(temporary, config)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def safe_config(text: str) -> str:
    """Retain TOML formatting while replacing identity/storage top-level settings."""
    text = without_bundled_marketplace(text)
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


def _paused_automation(text: str) -> str:
    """Copy an automation as a paused template for an independent profile.

    Automation definitions are local user data, but enabling the same schedule
    in every clone would execute it more than once.  Keep the prompt and
    schedule intact so the user can enable it deliberately in the destination.
    """
    parsed = tomllib.loads(text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get('status'), str):
        raise ValueError('automation definition has no supported status')
    replaced, count = re.subn(
        r'(?m)^(\s*status\s*=\s*)(["\']).*?\2(\s*)$',
        r'\1"PAUSED"\3', text, count=1)
    if count != 1:
        raise ValueError('automation status is not a top-level TOML field')
    # Validate the rewritten document before it reaches the destination.
    result = tomllib.loads(replaced)
    if result.get('status') != 'PAUSED':
        raise ValueError('automation pause rewrite failed')
    return replaced


def _copy_safe_data_tree(source: Path, put, *, automations=False) -> int:
    """Copy user-authored markdown/TOML data without hidden stores or symlinks."""
    if not source.is_dir() or source.is_symlink():
        return 0
    copied = 0
    for item in sorted(source.rglob('*')):
        if item.is_symlink() or not item.is_file() or any(part.startswith('.') for part in item.relative_to(source).parts):
            continue
        relative = item.relative_to(source)
        if automations:
            if relative.name != 'automation.toml' or len(relative.parts) != 2:
                continue
            data = _paused_automation(item.read_text()).encode()
        else:
            if item.suffix.lower() not in {'.md', '.jsonl'}:
                continue
            data = item.read_bytes()
        put(Path(source.name) / relative, data)
        copied += 1
    return copied


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
    # Memories are plain local notes, never credentials or the memory repo's
    # hidden git store.  Each clone receives an independent snapshot.
    memories_added = _copy_safe_data_tree(source_home/'memories', put)
    # Schedules are copied as paused templates.  They must be explicitly
    # enabled in a clone to avoid duplicate execution across accounts.
    automations_added = _copy_safe_data_tree(source_home/'automations', put, automations=True)
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
            'memories_added':memories_added,'automations_added':automations_added,
            'files':count,'status':'synced'}
