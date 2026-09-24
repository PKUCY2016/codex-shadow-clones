"""Fail-closed compatibility admission for the installed Codex desktop app.

Known builds retain their checked fast path. New builds are admitted only after
local, bounded checks that never read a profile or start a model request.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import tempfile

APP = Path('/Applications/ChatGPT.app')
CHECKED = ('26.917.71314', '10954')
TEAM_ID = '2DC432GLL2'
MAX_ASAR_BYTES = 600 * 1024 * 1024
EXPECTED_STATE_COLUMNS = {
    'projects': ('id', 'name', 'metadata', 'position', 'created_at_ms', 'updated_at_ms'),
    'project_roots': ('project_id', 'position', 'path'),
    'threads': ('id', 'rollout_path', 'created_at', 'updated_at', 'source',
                'model_provider', 'cwd', 'title', 'sandbox_policy', 'approval_mode',
                'tokens_used', 'has_user_event', 'archived', 'archived_at', 'git_sha',
                'git_branch', 'git_origin_url', 'cli_version', 'first_user_message',
                'agent_nickname', 'agent_role', 'memory_mode', 'model',
                'reasoning_effort', 'agent_path', 'created_at_ms', 'updated_at_ms',
                'thread_source', 'preview', 'recency_at', 'recency_at_ms',
                'history_mode', 'name', 'is_pinned', 'thread_section_id',
                'section_position', 'section_entered_at_ms', 'project_id',
                'originator', 'daybreak_enabled'),
}


class CompatibilityError(RuntimeError):
    """A display-safe reason for refusing an unverified desktop build."""


@dataclass(frozen=True)
class CompatibilityReport:
    version: str
    build: str
    method: str  # checked or auto_probe


def _fail(reason: str):
    raise CompatibilityError('Codex 版本改变，自动兼容检查未通过（'+reason+'）；未启动分身。')


def _version_parts(value: str):
    parts = value.split('.')
    if len(value) > 64 or len(parts) != 3 or any(
            not part.isascii() or not part.isdecimal() or len(part) > 16 for part in parts):
        _fail('应用版本格式不可识别')
    return tuple(int(part) for part in parts)


def _verify_signature(app: Path):
    try:
        verified = subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)],
                                  capture_output=True, timeout=20, check=False)
        details = subprocess.run(['/usr/bin/codesign', '-dv', '--verbose=2', str(app)],
                                 capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        _fail('应用签名检查不可用')
    if verified.returncode or details.returncode:
        _fail('应用签名无效')
    if b'TeamIdentifier='+TEAM_ID.encode() not in details.stderr.splitlines():
        _fail('应用签名团队不符')


def _desktop_contract(asar: Path):
    """Look for the exact isolation interfaces, without loading a large asar."""
    try:
        if not asar.is_file() or asar.stat().st_size > MAX_ASAR_BYTES:
            _fail('桌面程序资源缺失或过大')
        markers = {
            'profile': b'CODEX_ELECTRON_USER_DATA_PATH',
            'user_data': b'.setPath(`userData`',
            'backend': b'CODEX_HOME',
            'launch': b'--user-data-dir=',
            'single_instance': b'requestSingleInstanceLock()',
        }
        found = set()
        coupled_home = False
        coupled_lock = False
        tail = b''
        with asar.open('rb') as stream:
            for chunk in iter(lambda: stream.read(2 * 1024 * 1024), b''):
                window = tail + chunk
                for name, marker in markers.items():
                    if marker in window:
                        found.add(name)
                # A profile-specific home must still be tied to the explicit
                # Electron data path; a bare CODEX_HOME string is insufficient.
                for marker in (b'CODEX_ELECTRON_USER_DATA_PATH?.trim()',
                               b'CODEX_ELECTRON_USER_DATA_PATH?.trim()?'):
                    pos = window.find(marker)
                    while pos >= 0:
                        nearby = window[max(0, pos-160):pos+len(marker)+160]
                        coupled_home |= b'CODEX_HOME' in nearby
                        coupled_lock |= b'hasExplicitUserDataPath' in nearby
                        pos = window.find(marker, pos+len(marker))
                tail = window[-1024:]
        if set(markers) != found or not coupled_home or not coupled_lock:
            _fail('独立数据目录启动接口已改变')
    except OSError:
        _fail('无法读取桌面程序资源')


def _check_state_schema(home: Path):
    with closing(sqlite3.connect((home/'state_5.sqlite').resolve().as_uri()+'?mode=ro',
                                 uri=True)) as database:
        for table, expected in EXPECTED_STATE_COLUMNS.items():
            actual = tuple(row[1] for row in database.execute('PRAGMA table_info('+table+')'))
            if actual != expected:
                _fail('项目或历史索引结构已改变')


def _rpc_smoke(binary: Path):
    from shadow_seed import ProjectRPC
    try:
        with tempfile.TemporaryDirectory(prefix='codex-shadow-compat-') as directory:
            root = Path(directory)
            home = root/'codex-home'
            home.mkdir(mode=0o700)
            config = home/'config.toml'
            config.write_text('cli_auth_credentials_store = "file"\n')
            config.chmod(0o600)
            # No parent task identity, API key, proxy, profile, or auth store
            # enters the throwaway server. HOME also points at the temp root.
            env = {'HOME': str(root), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                   'TMPDIR': str(root)}
            rpc = ProjectRPC(home, binary=binary, environment=env, timeout=8)
            try:
                page = rpc.call('project/list', {'limit': 1, 'cursor': None})
                if not isinstance(page, dict) or page.get('data') != [] or 'nextCursor' not in page:
                    _fail('独立项目接口响应异常')
            finally:
                rpc.close()
            _check_state_schema(home)
    except CompatibilityError:
        raise
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
        _fail('独立项目接口未通过')


def check_compatibility(app: Path = APP, *, force_probe=False) -> CompatibilityReport:
    app = Path(app)
    try:
        with (app/'Contents/Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
    except (OSError, ValueError, TypeError):
        _fail('应用版本信息不可读')
    version, build = info.get('CFBundleShortVersionString'), info.get('CFBundleVersion')
    if info.get('CFBundleIdentifier') != 'com.openai.codex' or not isinstance(version, str) or not isinstance(build, str):
        _fail('应用身份不符')
    if (version, build) == CHECKED and not force_probe:
        return CompatibilityReport(version, build, 'checked')
    if len(build) > 16 or not build.isascii() or not build.isdecimal():
        _fail('应用 build 格式不可识别')
    if (_version_parts(version), int(build)) < (_version_parts(CHECKED[0]), int(CHECKED[1])):
        _fail('应用版本早于已核查版本')
    binary = app/'Contents/Resources/codex'
    if not (app/'Contents/MacOS/ChatGPT').is_file() or not binary.is_file():
        _fail('桌面程序或本地接口缺失')
    _verify_signature(app)
    _desktop_contract(app/'Contents/Resources/app.asar')
    _rpc_smoke(binary)
    return CompatibilityReport(version, build, 'auto_probe')
