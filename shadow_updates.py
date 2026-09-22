"""Non-blocking, unauthenticated update notices from the project's manifest.

Only version metadata is downloaded. Installation always remains a user action
on the fixed project README; remote data cannot supply executable code or URLs.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time
import urllib.request


MANIFEST_URL = 'https://raw.githubusercontent.com/PKUCY2016/codex-shadow-clones/main/version.json'
UPDATE_URL = 'https://github.com/PKUCY2016/codex-shadow-clones/blob/main/README.md'
CHECK_INTERVAL = 6 * 60 * 60
NETWORK_TIMEOUT = 6
MAX_RESPONSE_BYTES = 64 * 1024
_SEMVER = re.compile(
    r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
)


class _UpdateError(Exception):
    """Static, display-safe error codes only."""


def _parse_version(version):
    if not isinstance(version, str) or len(version) > 128:
        raise _UpdateError('invalid_manifest')
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise _UpdateError('invalid_manifest')
    prerelease = tuple(match.group(4).split('.')) if match.group(4) else ()
    if any(part.isdigit() and len(part) > 1 and part[0] == '0' for part in prerelease):
        raise _UpdateError('invalid_manifest')
    return tuple(int(match.group(index)) for index in (1, 2, 3)), prerelease


def _is_newer(latest, current):
    latest_core, latest_pre = _parse_version(latest)
    current_core, current_pre = _parse_version(current)
    if latest_core != current_core:
        return latest_core > current_core
    if not latest_pre or not current_pre:
        return bool(current_pre) and not latest_pre
    for left, right in zip(latest_pre, current_pre):
        if left == right:
            continue
        if left.isdigit() and right.isdigit():
            return int(left) > int(right)
        if left.isdigit() != right.isdigit():
            return not left.isdigit()
        return left > right
    return len(latest_pre) > len(current_pre)


def _manifest_version(payload):
    if not isinstance(payload, bytes):
        raise _UpdateError('invalid_manifest')
    if len(payload) > MAX_RESPONSE_BYTES:
        raise _UpdateError('response_too_large')
    try:
        manifest = json.loads(payload.decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError):
        raise _UpdateError('invalid_manifest') from None
    if not isinstance(manifest, dict):
        raise _UpdateError('invalid_manifest')
    version = manifest.get('version')
    _parse_version(version)
    return version


CURRENT_VERSION = _manifest_version(Path(__file__).with_name('version.json').read_bytes())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_remote():
    # A private opener preserves existing proxy configuration without changing
    # urllib's global opener, environment variables, or account credentials.
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(MANIFEST_URL, headers={
        'Accept': 'application/json',
        'User-Agent': 'codex-shadow-clones-update-check',
    })
    with opener.open(request, timeout=NETWORK_TIMEOUT) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


class UpdateChecker:
    """Thread-safe snapshot plus a throttled background metadata check.

    ``reader`` optionally supplies manifest bytes for offline testing. Neither
    snapshot nor check_async performs network I/O on the caller's thread.
    ``checkedAt`` is the Unix timestamp of the last completed attempt.
    """

    def __init__(self, reader=None, clock=None, monotonic=None):
        self._reader = reader if reader is not None else _read_remote
        self._clock = clock if clock is not None else time.time
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._lock = threading.Lock()
        self._last_attempt = None
        self._checking = False
        self._state = {
            'currentVersion': CURRENT_VERSION,
            'latestVersion': None,
            'status': 'idle',
            'checkedAt': None,
            'error': None,
            'url': UPDATE_URL,
        }

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def check_async(self, force=False):
        with self._lock:
            now = self._monotonic()
            if self._checking:
                return
            if not force and self._last_attempt is not None and now - self._last_attempt < CHECK_INTERVAL:
                return
            self._last_attempt = now
            self._checking = True
            self._state.update(status='checking', error=None)
            try:
                threading.Thread(target=self._check, name='shadow-update-check', daemon=True).start()
            except RuntimeError:
                self._checking = False
                self._state.update(status='error', checkedAt=int(self._clock()), error='check_start_failed')

    def _check(self):
        latest = None
        error = None
        try:
            latest = _manifest_version(self._reader())
            status = 'available' if _is_newer(latest, CURRENT_VERSION) else 'up_to_date'
        except _UpdateError as exc:
            status, error = 'error', str(exc)
        except Exception:
            # Upstream network/OS errors can contain proxy URLs or other private
            # machine details. Never surface their messages in the UI or logs.
            status, error = 'error', 'check_failed'
        with self._lock:
            self._state.update(status=status, latestVersion=latest,
                               checkedAt=int(self._clock()), error=error)
            self._checking = False
