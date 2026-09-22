#!/usr/bin/env python3
"""Build the native menu-bar companion; --open also launches it.

Quitting this companion leaves the manager and all Codex instances running.
The app never embeds account credentials or the local API bearer token.
"""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
APP = ROOT / '.runtime' / 'Codex Shadow Clones.app'


def build():
    contents = APP / 'Contents'
    binary = contents / 'MacOS' / 'ShadowMenu'
    binary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = ROOT / '.runtime' / 'swift-cache'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Stage compilation before replacing an existing runnable build.
    temporary = binary.with_suffix('.new')
    subprocess.run(['/usr/bin/swiftc', '-O', '-module-cache-path', str(cache),
                    str(ROOT / 'shadow_menubar.swift'), '-o', str(temporary)], check=True)
    os.replace(temporary, binary)
    resources = contents / 'Resources'
    resources.mkdir(exist_ok=True)
    shutil.copyfile(ROOT/'assets'/'AppIcon.icns', resources/'AppIcon.icns')
    with (contents / 'Info.plist').open('wb') as stream:
        plistlib.dump({
            'CFBundleIdentifier': 'local.codex.shadow-clones.menubar',
            'CFBundleName': 'Codex Shadow Clones',
            'CFBundleDisplayName': 'Codex 影分身',
            'CFBundleExecutable': 'ShadowMenu',
            'CFBundlePackageType': 'APPL',
            'CFBundleShortVersionString': '0.2.0',
            'CFBundleVersion': '3',
            'CFBundleIconFile': 'AppIcon',
            'LSUIElement': True,
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': True,
            'ShadowProjectRoot': str(ROOT),
            'ShadowPython': sys.executable,
            'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
        }, stream)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(APP)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(APP)], check=True)
    print(f'菜单栏应用已构建：{APP}')
    return APP


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--open', action='store_true', help='build then launch the menu-bar app')
    args = parser.parse_args()
    application = build()
    if args.open:
        subprocess.run(['/usr/bin/open', str(application)], check=True)
