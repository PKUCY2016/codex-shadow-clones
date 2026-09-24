#!/usr/bin/env python3
"""Installation checks; --probe also exercises the current build in isolation."""
import argparse
import platform
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shadow_compat import APP, CompatibilityError, check_compatibility


def main(force_probe=False):
    root = Path(__file__).resolve().parents[1]
    checks = [('macOS', platform.system() == 'Darwin'),
              ('Python >= 3.11', sys.version_info >= (3, 11)),
              ('Swift compiler (Xcode Command Line Tools)', bool(shutil.which('swiftc')))]
    try:
        report = check_compatibility(APP, force_probe=force_probe)
        checks.append((f'Codex {report.version} ({report.build}; {report.method})', True))
    except CompatibilityError as error:
        checks.append((str(error), False))
    for name in ('assets/logo.svg', 'assets/AppIcon.icns', 'shadow_menubar.swift', 'shadow_dashboard.html',
                 'version.json', 'shadow_updates.py', 'shadow_reload.swift'):
        checks.append((name, (root/name).is_file()))
    for label, okay in checks:
        print(('PASS' if okay else 'FAIL') + ': ' + label)
    return 0 if all(okay for _, okay in checks) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true', help='run isolated compatibility checks even for the known build')
    raise SystemExit(main(force_probe=parser.parse_args().probe))
