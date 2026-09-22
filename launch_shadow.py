#!/usr/bin/env python3
"""Open the native menu companion and authenticated local dashboard."""
from pathlib import Path
import subprocess
import build_menubar
from shadow_clones import dashboard


def launch():
    binary = build_menubar.APP/'Contents/MacOS/ShadowMenu'
    sources = [Path(__file__).with_name('shadow_menubar.swift'),
               Path(__file__).with_name('build_menubar.py'),
               Path(__file__).parent/'assets'/'AppIcon.icns']
    if not binary.exists() or any(p.stat().st_mtime > binary.stat().st_mtime for p in sources):
        build_menubar.build()
    dashboard()
    subprocess.run(['/usr/bin/open', str(build_menubar.APP)], check=True)
    print('菜单栏入口已启动：点击 macOS 顶部的「影」。')


if __name__ == '__main__':
    launch()
