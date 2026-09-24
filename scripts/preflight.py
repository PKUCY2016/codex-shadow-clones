#!/usr/bin/env python3
"""Read-only installation checks. Never opens account files or starts inference."""
import platform
import plistlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_second import APP, CHECKED


def main():
    root = Path(__file__).resolve().parents[1]
    checks = [('macOS', platform.system() == 'Darwin'),
              ('Python >= 3.11', sys.version_info >= (3, 11)),
              ('Swift compiler (Xcode Command Line Tools)', bool(shutil.which('swiftc')))]
    app = APP
    try:
        with (app/'Contents/Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
        checks.append((f'Codex {CHECKED[0]} ({CHECKED[1]})',
            (info.get('CFBundleIdentifier'), info.get('CFBundleShortVersionString'), info.get('CFBundleVersion'))
            == ('com.openai.codex', *CHECKED)))
    except OSError:
        checks.append(('Codex at /Applications/ChatGPT.app', False))
    for name in ('assets/logo.svg', 'assets/AppIcon.icns', 'shadow_menubar.swift', 'shadow_dashboard.html',
                 'version.json', 'shadow_updates.py', 'shadow_reload.swift'):
        checks.append((name, (root/name).is_file()))
    for label, okay in checks:
        print(('PASS' if okay else 'FAIL') + ': ' + label)
    return 0 if all(okay for _, okay in checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
