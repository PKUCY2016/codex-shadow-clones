#!/usr/bin/env python3
"""Launch a separate state/profile using the inspected desktop version."""
import json
import os
from pathlib import Path
import subprocess
from shadow_desktop_env import desktop_environment
from shadow_compat import APP, check_compatibility

ROOT = Path(__file__).resolve().parent
BASE = ROOT / '.runtime' / 'desktop-b'
def prepare():
    check_compatibility(APP)
    home = BASE / 'codex-home'
    ui = BASE / 'electron-data'
    for directory in (BASE, home, ui):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    config = home / 'config.toml'
    if not config.exists():
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write('cli_auth_credentials_store = "file"\n')
    return home, ui


def launch():
    home, ui = prepare()
    # Remove inherited overrides. HOME and the original app are unchanged.
    env = desktop_environment(os.environ)
    args = ['/usr/bin/open', '-n', '--env', 'CODEX_HOME=' + str(home),
            '--env', 'CODEX_ELECTRON_USER_DATA_PATH=' + str(ui),
            str(APP), '--args', '--user-data-dir=' + str(ui)]
    subprocess.run(args, env=env, check=True)
    print('已请求打开独立的第二个 Codex。请在新窗口登录另一个账号。')
    print('本入口不使用账号池；第二实例使用自己的账号、配置和历史。')


if __name__ == '__main__':
    try:
        launch()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print('未启动：' + str(error))
        raise SystemExit(1)
