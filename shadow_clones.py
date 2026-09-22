#!/usr/bin/env python3
"""Codex Shadow Clones: loopback-only multi-profile desktop manager."""
from __future__ import annotations
import argparse
import copy
import concurrent.futures
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from desktop_second import APP, CHECKED
from shadow_seed import seed_home
from shadow_quota import read_quota
from shadow_updates import UpdateChecker

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'
REGISTRY = RUNTIME / 'shadow-clones.json'
SERVER_INFO = RUNTIME / 'shadow-server.json'
PORT = 18318
POLL_SECONDS = 120


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    path.chmod(0o600)


def merge_imported_sidebar(source, target):
    from shadow_history_state import merge_history_state
    manifest = target/'.shadow-history-imports.json'
    if not manifest.exists():
        return
    imports = json.loads(manifest.read_text()).get('imports', [])
    mapping = {item['source_id']: item['target_id'] for item in imports
               if item['source_home'] == str(source.resolve())}
    source_state = source/'.codex-global-state.json'
    target_state = target/'.codex-global-state.json'
    original = json.loads(target_state.read_text()) if target_state.exists() else {}
    private_json(target/'.shadow-backups'/('sidebar-'+str(time.time_ns()))/'.codex-global-state.json', original)
    merged = merge_history_state(json.loads(source_state.read_text()), original, mapping)
    private_json(target_state, merged)


def check_version():
    import plistlib
    with (APP / 'Contents/Info.plist').open('rb') as handle:
        info = plistlib.load(handle)
    if info.get('CFBundleIdentifier') != 'com.openai.codex' or (
        info.get('CFBundleShortVersionString'), info.get('CFBundleVersion')) != CHECKED:
        raise RuntimeError('Codex 版本改变，请重新核查分身启动参数。')


def process_map(profiles):
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,command='], capture_output=True, text=True, check=True)
    executable = str(APP / 'Contents/MacOS/ChatGPT')
    found = {p['id']: None for p in profiles}
    for line in result.stdout.splitlines():
        pair = line.strip().split(None, 1)
        if len(pair) != 2 or not pair[1].startswith(executable):
            continue
        command = pair[1]
        for profile in profiles:
            if profile['source']:
                match = '--user-data-dir=' not in command
            else:
                prefix = executable + ' --user-data-dir=' + profile['ui']
                match = command == prefix or command.startswith(prefix + ' --')
            if match:
                found[profile['id']] = int(pair[0])
    return found


def focus_pid(pid):
    helper = RUNTIME / 'bin/shadow-focus'
    if not helper.exists():
        helper.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(['swiftc', '-module-cache-path', str(RUNTIME/'swift-cache'),
                        str(ROOT/'shadow_focus.swift'), '-o', str(helper)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([str(helper), str(pid)], check=True)


def launch_profile(profile, pid=None):
    check_version()
    if pid:
        focus_pid(pid)
        return
    if profile['source']:
        # Do not spawn another original instance with shared databases.
        raise RuntimeError('原实例未运行，请从应用程序打开原 Codex。')
    env = dict(os.environ)
    for key in ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_ELECTRON_USER_DATA_PATH',
                'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL'):
        env.pop(key, None)
    subprocess.run(['/usr/bin/open', '-n', '--env', 'CODEX_HOME='+profile['home'],
                    '--env', 'CODEX_ELECTRON_USER_DATA_PATH='+profile['ui'], str(APP),
                    '--args', '--user-data-dir='+profile['ui']], env=env, check=True)


def recommended_profile(profiles, selected):
    current = next((p for p in profiles if p['id'] == selected), None)
    identity = (current or {}).get('quota', {}).get('account') or {}
    candidates = []
    for profile in profiles:
        quota = profile.get('quota', {})
        account = quota.get('account') or {}
        remaining = quota.get('coreRemainingPercent')
        if (profile['id'] != selected and quota.get('status') == 'ok'
                and isinstance(remaining, (int, float)) and remaining > 0
                and account.get('id') and identity.get('id')
                and account['id'] != identity['id']
                and time.time() - quota.get('checkedAt', 0) < 300):
            candidates.append((remaining, profile['id']))
    return max(candidates)[1] if candidates else None


class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.refreshing = False
        self.working = False
        self.message = ''
        self.last_switch = 0
        self.last_history_sync = 0
        self.quota = {}
        self.updates = UpdateChecker()
        if REGISTRY.exists():
            self.data = json.loads(REGISTRY.read_text())
            self.last_history_sync = self.data.get('history_sync_at', 0)
        else:
            self.data = {'selected': 'codex1', 'auto_switch': False, 'threshold': 10,
                         'profiles': [{'id': 'codex1', 'name': 'Codex 1 · 原实例',
                                       'home': str(Path.home()/'.codex'), 'ui': '', 'source': True}]}
            old = RUNTIME / 'desktop-b'
            if (old/'codex-home').exists():
                self.data['profiles'].append({'id': 'codex2', 'name': 'Codex 2',
                     'home': str(old/'codex-home'), 'ui': str(old/'electron-data'),
                     'source': False, 'pending_sync': False})
            self.save()

    def save(self):
        private_json(REGISTRY, self.data)

    def profile(self, identifier):
        return next(p for p in self.data['profiles'] if p['id'] == identifier)

    def snapshot(self):
        with self.lock:
            result = json.loads(json.dumps(self.data))
            try:
                processes = process_map(result['profiles'])
            except (OSError, subprocess.SubprocessError):
                processes = None
            for p in result['profiles']:
                p['running'] = bool(processes.get(p['id'])) if processes is not None else None
                p['quota'] = self.quota.get(p['id'], {'status': 'unknown'})
                p.pop('home', None)
                p.pop('ui', None)
            result.update(refreshing=self.refreshing, working=self.working, message=self.message,
                          poll_seconds=POLL_SECONDS,
                          update=self.updates.snapshot(),
                          recommended=recommended_profile(result['profiles'], result['selected']))
            return result

    def refresh(self):
        if not self.operation.acquire(blocking=False):
            return
        with self.lock:
            self.refreshing = True
            profiles = list(self.data['profiles'])
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                jobs = {executor.submit(read_quota, Path(p['home'])): p['id'] for p in profiles}
                for job in concurrent.futures.as_completed(jobs):
                    try:
                        value = job.result()
                    except Exception:
                        value = {'status': 'error', 'error': '额度查询失败', 'checkedAt': time.time()}
                    with self.lock:
                        self.quota[jobs[job]] = value
            with self.lock:
                state = self.snapshot()
                selected = self.quota.get(self.data['selected'], {})
                remaining = selected.get('coreRemainingPercent')
                target = state['recommended']
                if (self.data['auto_switch'] and target and selected.get('status') == 'ok'
                        and isinstance(remaining, (int, float))
                        and remaining <= self.data['threshold']
                        and self.quota[target]['coreRemainingPercent'] > self.data['threshold']
                        and time.time()-self.last_switch >= 600):
                    self.open(target)
                    self.last_switch = time.time()
                    self.message = '低额度：已打开 '+self.profile(target)['name']+'；原任务仍留在原窗口。'
        except Exception:
            with self.lock:
                self.message = '本次监控或自动切换失败，可手动刷新重试。'
        finally:
            with self.lock:
                self.refreshing = False
            self.operation.release()

    def open(self, identifier):
        with self.lock:
            profile = self.profile(identifier)
            pid = process_map(self.data['profiles'])[identifier]
            if profile.get('pending_sync') and not pid:
                self.sync(identifier)
            launch_profile(profile, pid)
            self.data['selected'] = identifier
            self.save()

    def sync(self, identifier):
        profile = self.profile(identifier)
        if profile['source']:
            raise RuntimeError('原实例是复制来源，无需同步到自身。')
        if process_map(self.data['profiles'])[identifier]:
            profile['pending_sync'] = True
            self.save()
            self.message = '已排队：退出 '+profile['name']+' 后，从面板重新打开即可导入。'
            return
        source = self.profile('codex1')
        profile['seed'] = seed_home(Path(source['home']), Path(profile['home']))
        profile['pending_sync'] = False
        self.save()
        self.message = '项目入口和配置已复制；该分身的账号保留。'

    def next_profile_number(self):
        """Never reuse an ID, including directories left by failed creates/deletes."""
        numbers = [1]
        for profile in self.data['profiles']:
            match = re.fullmatch(r'codex([1-9][0-9]*)', profile['id'])
            if match:
                numbers.append(int(match[1]))
        for parent in (RUNTIME/'clones', RUNTIME/'deleted-clones'):
            self.validate_managed_path(parent)
            if parent.exists():
                for child in parent.iterdir():
                    match = re.match(r'^codex([1-9][0-9]*)(?:-|$)', child.name)
                    if match:
                        numbers.append(int(match[1]))
        return max(max(numbers) + 1, int(self.data.get('next_profile_number', 2)))

    @staticmethod
    def validate_managed_path(path):
        """Check the managed ancestors without following directory symlinks."""
        try:
            relative = path.relative_to(RUNTIME)
        except ValueError:
            raise RuntimeError('分身路径不在受管目录内，已拒绝操作。') from None
        if '..' in relative.parts:
            raise RuntimeError('分身路径包含上级目录，已拒绝操作。')
        current = RUNTIME
        for part in (None, *relative.parts):
            if part is not None:
                current = current/part
            if current.is_symlink():
                raise RuntimeError('分身路径包含符号链接，已拒绝操作。')

    def clone_directory(self, profile):
        identifier = profile['id']
        if profile.get('source') is not False or identifier == 'codex1':
            raise RuntimeError('原实例不能删除。')
        if not re.fullmatch(r'codex[1-9][0-9]*', identifier):
            raise RuntimeError('分身标识无效，已拒绝删除。')
        base = RUNTIME/'clones'/identifier
        if identifier == 'codex2' and Path(profile['home']) == RUNTIME/'desktop-b/codex-home':
            base = RUNTIME/'desktop-b'
        if (Path(profile['home']) != base/'codex-home' or
                Path(profile['ui']) != base/'electron-data'):
            raise RuntimeError('分身路径不是独立受管目录，已拒绝删除。')
        for path in (base, Path(profile['home']), Path(profile['ui'])):
            self.validate_managed_path(path)
        if not base.is_dir():
            raise RuntimeError('分身数据目录不存在，已拒绝删除。')
        resolved = base.resolve()
        for other in self.data['profiles']:
            if other is profile:
                continue
            if other['id'] == identifier:
                raise RuntimeError('分身标识重复，已拒绝删除。')
            for key in ('home', 'ui'):
                if not other.get(key):
                    continue
                path = Path(other[key]).resolve()
                if path == resolved or resolved in path.parents or path in resolved.parents:
                    raise RuntimeError('分身目录与其他实例重叠，已拒绝删除。')
        return base

    def require_stopped(self, identifier):
        try:
            processes = process_map(self.data['profiles'])
            if not isinstance(processes, dict) or identifier not in processes:
                raise ValueError('unknown process state')
            pid = processes[identifier]
            if pid is not None and not isinstance(pid, int):
                raise ValueError('unknown process state')
        except (OSError, ValueError, subprocess.SubprocessError):
            raise RuntimeError('无法确认分身已退出，已拒绝删除。请稍后重试。') from None
        if pid is not None:
            raise RuntimeError('请先用 ⌘Q 退出目标分身，再删除；关闭面板不等于退出分身。')

    def delete(self, identifier, confirmed=False):
        if confirmed is not True:
            raise RuntimeError('请先确认删除分身；数据会保留在 deleted-clones 中。')
        profile = self.profile(identifier)
        base = self.clone_directory(profile)
        self.require_stopped(identifier)
        number = self.next_profile_number()
        destination_root = RUNTIME/'deleted-clones'
        destination_root.mkdir(mode=0o700, exist_ok=True)
        destination = destination_root/(identifier+'-'+secrets.token_hex(12))
        receipt_path = destination.with_suffix('.json')
        if os.path.lexists(destination) or os.path.lexists(receipt_path):
            raise RuntimeError('恢复目录已存在，已拒绝覆盖。请重试。')
        previous = copy.deepcopy(self.data)
        receipt = {'profile': copy.deepcopy(profile), 'selected': previous['selected'],
                   'originalPath': str(base), 'archivedPath': str(destination),
                   'deletedAt': time.time(), 'status': 'prepared'}
        private_json(receipt_path, receipt)
        # Recheck after preparation: an external app launch does not take our lock.
        self.clone_directory(profile)
        self.require_stopped(identifier)
        # Rename the directory itself; embedded links are never traversed.
        try:
            base.rename(destination)
        except OSError:
            raise RuntimeError('无法移动分身数据，注册记录和原始目录均已保留。') from None
        self.data = copy.deepcopy(previous)
        self.data['profiles'] = [p for p in self.data['profiles'] if p['id'] != identifier]
        self.data['next_profile_number'] = number
        if self.data['selected'] == identifier:
            self.data['selected'] = 'codex1'
        history = self.data.get('history_sync', {})
        for key in ('profiles', 'errors'):
            if isinstance(history.get(key), dict):
                history[key].pop(identifier, None)
        try:
            self.save()
        except Exception:
            self.data = previous
            restored = False
            try:
                if os.path.lexists(base):
                    raise OSError('original directory was recreated')
                destination.rename(base)
                restored = True
                if not REGISTRY.exists() or json.loads(REGISTRY.read_text()) != previous:
                    private_json(REGISTRY, previous)
                receipt['status'] = 'rolled_back'
            except Exception:
                receipt['status'] = 'recovery_required'
            with contextlib.suppress(OSError):
                private_json(receipt_path, receipt)
            location = base if restored else destination
            raise RuntimeError('删除未完成；数据保留在 '+str(location)+'，恢复记录：'+str(receipt_path)) from None
        self.quota.pop(identifier, None)
        receipt['status'] = 'deleted'
        with contextlib.suppress(OSError):
            private_json(receipt_path, receipt)
        self.message = profile['name']+' 已移除；账号和历史保留在 '+str(destination)+'。'

    def create(self):
        number = self.next_profile_number()
        identifier = 'codex'+str(number)
        base = RUNTIME/'clones'/identifier
        # Reserve the number before filesystem writes so a partial create cannot reuse it.
        self.data['next_profile_number'] = number + 1
        self.save()
        base.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (base, base/'codex-home', base/'electron-data'):
            directory.mkdir(exist_ok=False, mode=0o700)
            directory.chmod(0o700)
        profile = {'id': identifier, 'name': 'Codex '+str(number), 'home': str(base/'codex-home'),
                   'ui': str(base/'electron-data'), 'source': False, 'pending_sync': False}
        profile['seed'] = seed_home(Path(self.profile('codex1')['home']), Path(profile['home']))
        from shadow_history import import_history
        profile['history'] = import_history(Path(self.profile('codex1')['home']), Path(profile['home']),
                                            progress=self.history_progress)
        merge_imported_sidebar(Path(self.profile('codex1')['home']), Path(profile['home']))
        self.data['profiles'].append(profile)
        self.save()
        self.open(identifier)
        self.message = profile['name']+' 已创建，请在新窗口登录该分身的账号。'
        if profile['history'].get('failed'):
            self.message += ' 有 '+str(profile['history']['failed'])+' 条历史未导入，请查看快照结果。'
        return identifier

    def history_progress(self, value):
        with self.lock:
            if isinstance(value, dict) and value.get('phase'):
                phase = '汇总已结束历史' if value['phase'] == 'archive' else '分发历史快照'
                self.message = phase+'：'+str(value.get('profile', ''))
            elif isinstance(value, dict):
                self.message = '正在复制历史快照：'+str(value.get('done', 0))+' / '+str(value.get('total', '?'))
            else:
                self.message = '正在复制历史快照，请勿关闭管理服务。'

    def import_profile_history(self, identifier):
        if self.data.get('history_sync'):
            # Never regress a multi-source history through the legacy updater.
            return self.unified_history()
        profile = self.profile(identifier)
        if profile['source']:
            raise RuntimeError('原实例是历史来源，无需导入到自身。')
        if process_map(self.data['profiles'])[identifier]:
            raise RuntimeError('请先退出目标分身，再导入历史快照。')
        from shadow_history import import_history, repair_history
        source_home, target_home = Path(self.profile('codex1')['home']), Path(profile['home'])
        refreshed = repair_history(source_home, target_home, progress=self.history_progress)
        result = import_history(source_home, target_home,
                                progress=self.history_progress)
        result['updated'] = refreshed['repaired']
        result['conflicts'] = refreshed['conflicts']
        result['update_failed'] = refreshed['failed']
        if refreshed['status'] != 'complete':
            result['status'] = 'partial'
        merge_imported_sidebar(Path(self.profile('codex1')['home']), Path(profile['home']))
        with self.lock:
            profile['history'] = result
            self.message = ('历史更新结束：更新 '+str(result['updated'])+'，新增 '+str(result['imported'])+
                            '，冲突保留 '+str(result['conflicts'])+'，失败 '+
                            str(result['failed']+result['update_failed'])+'。只复制已结束的回合。')
            self.save()

    def unified_history(self):
        """Collect completed histories from every instance; defer live destinations."""
        from shadow_sync import sync_profiles
        profiles = [dict(p) for p in self.data['profiles']]
        def is_running(profile):
            # Fail closed when process inspection fails. No live destination writes.
            return bool(process_map(profiles).get(profile['id']))
        result = sync_profiles(profiles, RUNTIME/'history-hub', is_running,
                               progress=self.history_progress)
        with self.lock:
            pending = []
            for identifier, outcome in result.get('profiles', {}).items():
                profile = self.profile(identifier)
                profile['unified_history'] = outcome
                profile['pending_history'] = bool(outcome.get('pending'))
                if profile['pending_history']:
                    pending.append(identifier)
            self.data['history_sync'] = result
            self.data['history_sync_at'] = time.time()
            self.last_history_sync = time.time()
            self.message = ('统一历史同步结束；运行中的实例保持原状，退出后补齐。'
                            if pending else '统一历史同步结束。')
            if result.get('status') != 'complete':
                self.message += ' 部分记录待同步或有冲突，请查看各实例结果。'
            self.save()
        return result

    def maybe_sync_history(self):
        """Called by the monitor; pending destinations retry only after exit."""
        with self.lock:
            profiles = list(self.data['profiles'])
            auto_due = (self.data.get('auto_history', False) and
                        time.time() - self.last_history_sync >= 300)
            pending = [p for p in profiles if p.get('pending_history')]
        try:
            processes = process_map(profiles) if pending else {}
            ready = any(not processes.get(p['id']) for p in pending)
            if auto_due or ready:
                self.action({'action': 'history_all'})
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # Busy service or unverifiable process state: retry on the next poll.
            return

    def background_action(self, action, data):
        try:
            if action == 'create':
                self.create()
            elif action == 'history_all':
                self.unified_history()
            else:
                self.import_profile_history(data['id'])
        except Exception:
            with self.lock:
                self.message = '历史复制或创建未完成；已保存的进度和原账号均保留，可重新操作继续。'
        finally:
            with self.lock:
                self.working = False
            self.operation.release()

    def action(self, data):
        action = data.get('action')
        if action == 'check_updates':
            self.updates.check_async(force=True)
            return
        if action == 'history':
            with self.lock:
                profile = self.profile(data['id'])
                if profile['source']:
                    raise RuntimeError('原实例是历史来源，无需导入到自身。')
                if process_map(self.data['profiles'])[profile['id']]:
                    raise RuntimeError('请先退出目标分身，再更新历史；关闭面板不等于退出分身。')
        if action == 'refresh':
            threading.Thread(target=self.refresh, daemon=True).start()
            return
        if not self.operation.acquire(blocking=False):
            raise RuntimeError('正在查询额度或复制配置，请稍后重试。')
        if action in ('create', 'history', 'history_all'):
            with self.lock:
                self.working = True
                self.message = '正在准备项目与本地历史快照…'
            threading.Thread(target=self.background_action, args=(action, data), daemon=True).start()
            return
        try:
            with self.lock:
                if action == 'open':
                    self.open(data['id'])
                elif action == 'sync':
                    self.sync(data['id'])
                elif action == 'delete':
                    self.delete(data['id'], data.get('confirmed'))
                elif action == 'settings':
                    threshold = float(data.get('threshold', 10))
                    if not 0 <= threshold <= 90 or not isinstance(data.get('auto_switch'), bool):
                        raise ValueError('invalid settings')
                    automatic = data.get('auto_history', self.data.get('auto_history', False))
                    if not isinstance(automatic, bool):
                        raise ValueError('invalid history setting')
                    self.data.update(auto_switch=data['auto_switch'], threshold=threshold, auto_history=automatic)
                    self.save()
                else:
                    raise ValueError('invalid action')
        finally:
            self.operation.release()

    def prepare_shutdown(self, confirmed=False):
        if confirmed is not True:
            raise RuntimeError('请先确认重启管理服务。')
        if not self.operation.acquire(blocking=False):
            raise RuntimeError('正在查询额度或复制历史，请等待完成后重启。')
        # Keep the operation lock until process exit to exclude all new mutations.
        with self.lock:
            self.working = True
            self.message = '正在重启管理服务…'


def serve():
    check_version()
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    lock_handle = (RUNTIME/'shadow-server.lock').open('a')
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('管理面板已在运行。')
    manager = Manager()
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, value, status=200, content_type='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if self.headers.get('Host') != f'127.0.0.1:{PORT}':
                return False
            origin = self.headers.get('Origin')
            if origin and origin != f'http://127.0.0.1:{PORT}':
                return False
            return secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+token)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/':
                self.respond((ROOT/'shadow_dashboard.html').read_bytes(), content_type='text/html; charset=utf-8')
            elif path == '/assets/logo.svg':
                self.respond((ROOT/'assets'/'logo.svg').read_bytes(), content_type='image/svg+xml')
            elif path == '/api/state' and self.authorized():
                self.respond(manager.snapshot())
            else:
                self.respond({'error': 'Unauthorized'}, 401)

        def do_POST(self):
            if self.path != '/api/action' or not self.authorized():
                self.respond({'error': 'Unauthorized'}, 401)
                return
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size < 8192:
                    raise ValueError('body size')
                payload = json.loads(self.rfile.read(size))
                if payload.get('action') == 'shutdown_manager':
                    manager.prepare_shutdown(payload.get('confirmed'))
                    try:
                        self.respond({'ok': True})
                    finally:
                        threading.Thread(target=server.shutdown, daemon=True).start()
                    return
                manager.action(payload)
                self.respond({'ok': True})
            except RuntimeError as error:
                # Only application-generated RuntimeErrors are exposed.
                self.respond({'error': str(error)}, 409)
            except Exception:
                self.respond({'error': '操作未完成；未修改账号。请检查本地状态后重试。'}, 400)

    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    private_json(SERVER_INFO, {'pid': os.getpid(), 'port': PORT, 'token': token})
    def monitor():
        while True:
            manager.updates.check_async()
            manager.refresh()
            manager.maybe_sync_history()
            time.sleep(POLL_SECONDS)
    threading.Thread(target=monitor, daemon=True).start()
    print(f'Codex 影分身面板：http://127.0.0.1:{PORT}（通过启动入口打开）', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        SERVER_INFO.unlink(missing_ok=True)


def dashboard():
    import urllib.request
    def current():
        try:
            info = json.loads(SERVER_INFO.read_text())
            request = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/state',
                headers={'Authorization': 'Bearer '+info['token']})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
                if response.status == 200:
                    return info
        except Exception:
            return None
    info = current()
    if not info:
        import sys
        RUNTIME.mkdir(exist_ok=True, mode=0o700)
        fd = os.open(RUNTIME/'shadow-server.log', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            subprocess.Popen([sys.executable, str(Path(__file__)), 'serve'], cwd=ROOT,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(40):
            time.sleep(.25)
            info = current()
            if info:
                break
    if not info:
        raise RuntimeError('面板未启动，请查看 .runtime/shadow-server.log。')
    subprocess.run(['/usr/bin/open', f"http://127.0.0.1:{PORT}/#{info['token']}"], check=True)
    print('已打开 Codex 影分身管理面板。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['serve', 'dashboard'], default='dashboard', nargs='?')
    arguments = parser.parse_args()
    try:
        serve() if arguments.action == 'serve' else dashboard()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('未完成：'+str(error))
        raise SystemExit(1)
