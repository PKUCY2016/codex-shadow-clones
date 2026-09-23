#!/usr/bin/env python3
"""Open the local manager; --restart loads updated code without stopping clones."""
import argparse
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import build_menubar
from shadow_clones import ROOT, RUNTIME, SERVER_INFO, dashboard


def _request(info, body=None):
    path = '/api/action' if body else '/api/state'
    request = urllib.request.Request('http://127.0.0.1:18318'+path,
        data=json.dumps(body).encode() if body else None,
        headers={'Authorization': 'Bearer '+info['token'], 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        return json.load(response)


def _manager():
    try:
        metadata = SERVER_INFO.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077 or metadata.st_size > 8192):
        raise RuntimeError('管理服务记录权限异常，未重启任何进程。')
    try:
        info = json.loads(SERVER_INFO.read_text())
        if (type(info.get('pid')) is not int or info['pid'] <= 1 or info.get('port') != 18318
                or not isinstance(info.get('token'), str) or not 32 <= len(info['token']) < 256):
            raise ValueError('invalid metadata')
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('管理服务记录无效，未重启任何进程。') from None
    try:
        state = _request(info)
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError('管理服务认证或响应异常，未重启任何进程。') from None
    except (urllib.error.URLError, OSError) as error:
        cause = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(cause, ConnectionRefusedError):
            return None
        raise RuntimeError('暂时无法确认管理服务状态，未重启任何进程。请稍后重试。') from None
    except (ValueError, TypeError):
        raise RuntimeError('管理服务响应无效，未重启任何进程。') from None
    if not isinstance(state, dict) or not isinstance(state.get('profiles'), list):
        raise RuntimeError('无法确认管理服务身份，未重启任何进程。')
    return info, state


def _stop_legacy_manager(info):
    # One-time compatibility for versions without the authenticated shutdown action.
    result = subprocess.run(['/bin/ps', '-p', str(info['pid']), '-o', 'uid=,command='],
                            capture_output=True, text=True, check=True)
    fields = result.stdout.strip().split(None, 1)
    if (len(fields) != 2 or fields[0] != str(os.getuid())
            or not fields[1].endswith(' '+str(ROOT/'shadow_clones.py')+' serve')):
        raise RuntimeError('旧服务进程与本项目不匹配，未重启任何进程。')
    current = _manager()
    if not current or current[0] != info or current[1].get('working'):
        raise RuntimeError('服务状态已改变或正在同步，请等待完成后重试。')
    os.kill(info['pid'], signal.SIGTERM)


def stop_manager():
    current = _manager()
    if current is None:
        return
    info, state = current
    if state.get('working'):
        raise RuntimeError('正在创建分身或同步历史，请等待完成后再更新。')
    deadline = time.monotonic() + 25
    while True:
        try:
            _request(info, {'action': 'shutdown_manager', 'confirmed': True})
            break
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 400 and 'update' not in state:
                _stop_legacy_manager(info)
                break
            if error.code != 409 or time.monotonic() >= deadline:
                raise RuntimeError('管理服务忙碌或不支持安全重启，请稍后重试。') from None
            time.sleep(.5)
            state = _request(info)
            if state.get('working'):
                raise RuntimeError('正在创建分身或同步历史，请等待完成后再更新。')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(info['pid'], 0)
        except ProcessLookupError:
            return
        time.sleep(.1)
    raise RuntimeError('旧管理服务尚未退出，未启动第二个服务。')


def _reloader():
    helper = RUNTIME/'bin'/'shadow-reload'
    source = ROOT/'shadow_reload.swift'
    if not helper.exists() or source.stat().st_mtime > helper.stat().st_mtime:
        helper.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache = RUNTIME/'swift-cache'
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = helper.with_suffix('.new')
        subprocess.run(['/usr/bin/swiftc', '-module-cache-path', str(cache),
                        str(source), '-o', str(temporary)], check=True)
        os.replace(temporary, helper)
    return helper


def launch(restart=False):
    if sys.version_info < (3, 11):
        raise RuntimeError('请使用 Python 3.11 或更新版本运行启动器。')
    binary = build_menubar.APP/'Contents/MacOS/ShadowMenu'
    sources = [Path(__file__).with_name('shadow_menubar.swift'),
               Path(__file__).with_name('build_menubar.py'),
               Path(__file__).with_name('version.json'),
               Path(__file__).parent/'assets'/'AppIcon.icns']
    if not binary.exists() or any(p.stat().st_mtime > binary.stat().st_mtime for p in sources):
        build_menubar.build()
    if not restart:
        current = _manager()
        if current and current[1].get('update', {}).get('currentVersion') != build_menubar.CURRENT_VERSION:
            # A normal click after updating must not leave the old manager
            # serving the dashboard. stop_manager still refuses busy work.
            restart = True
    helper = _reloader() if restart else None
    if restart:
        stop_manager()
    dashboard()
    if restart:
        current = _manager()
        if not current or current[1].get('update', {}).get('currentVersion') != build_menubar.CURRENT_VERSION:
            raise RuntimeError('管理服务尚未加载当前版本，请检查本地日志后重试。')
    if helper:
        subprocess.run([str(helper), str(build_menubar.APP)], check=True)
    else:
        subprocess.run(['/usr/bin/open', str(build_menubar.APP)], check=True)
    print('菜单栏入口已启动：点击 macOS 顶部的忍者头像。关闭面板后仍会在后台运行。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart', action='store_true', help='更新后安全重启管理服务和菜单栏，保留分身')
    args = parser.parse_args()
    try:
        launch(restart=args.restart)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('未完成：'+str(error))
        raise SystemExit(1)
