#!/usr/bin/env python3
"""Codex Shadow Clones: loopback-only multi-profile desktop manager."""
from __future__ import annotations
import argparse
import concurrent.futures
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from desktop_second import APP, CHECKED
from shadow_seed import seed_home
from shadow_quota import read_quota

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
                match = command == executable + ' --user-data-dir=' + profile['ui']
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
        self.quota = {}
        if REGISTRY.exists():
            self.data = json.loads(REGISTRY.read_text())
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

    def create(self):
        number = max(int(p['id'][5:]) for p in self.data['profiles']) + 1
        identifier = 'codex'+str(number)
        base = RUNTIME/'clones'/identifier
        for directory in (base, base/'codex-home', base/'electron-data'):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            if isinstance(value, dict):
                self.message = '正在复制历史快照：'+str(value.get('done', 0))+' / '+str(value.get('total', '?'))
            else:
                self.message = '正在复制历史快照，请勿关闭管理服务。'

    def import_profile_history(self, identifier):
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

    def background_action(self, action, data):
        try:
            if action == 'create':
                self.create()
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
        if action in ('create', 'history'):
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
                elif action == 'settings':
                    threshold = float(data.get('threshold', 10))
                    if not 0 <= threshold <= 90 or not isinstance(data.get('auto_switch'), bool):
                        raise ValueError('invalid settings')
                    self.data.update(auto_switch=data['auto_switch'], threshold=threshold)
                    self.save()
                else:
                    raise ValueError('invalid action')
        finally:
            self.operation.release()


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
            manager.refresh()
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
            with urllib.request.urlopen(request, timeout=2) as response:
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
