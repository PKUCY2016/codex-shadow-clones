"""Quota-only client of the official Codex app-server (no model requests).

Public API: read_quota(codex_home, timeout=20, codex_binary=None) -> dict.
Unknown quota stays None. Email identity conservatively groups same-email logins.
The CLI owns authentication; this module never reads or copies credential files.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import threading
import time

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class QuotaError(Exception):
    """Only static error codes may be carried by this exception."""


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def normalize_limits(payload):
    """Keep multi-bucket identity and never treat missing usage as zero."""
    multiple = payload.get('rateLimitsByLimitId')
    if isinstance(multiple, dict) and multiple:
        entries = multiple.items()
    else:
        legacy = payload.get('rateLimits')
        entries = [(legacy.get('limitId') or 'codex', legacy)] if isinstance(legacy, dict) else []
    result = []
    for key, bucket in entries:
        if not isinstance(bucket, dict):
            continue
        ident = str(key)
        windows = []
        for label in ('primary', 'secondary'):
            window = bucket.get(label)
            if not isinstance(window, dict):
                continue
            used = _number(window.get('usedPercent'))
            windows.append({'name': label, 'usedPercent': used,
                            'remainingPercent': None if used is None else max(0, min(100, 100 - used)),
                            'windowDurationMins': _number(window.get('windowDurationMins')),
                            'resetsAt': _number(window.get('resetsAt'))})
        reached = bucket.get('rateLimitReachedType')
        result.append({'id': ident, 'name': bucket.get('limitName'), 'isCodex': ident == 'codex',
                       'windows': windows, 'limitReached': bool(reached) or bucket.get('spendControlReached') is True})
    return result


class _RPC:
    def __init__(self, process, deadline):
        self.process = process
        self.deadline = deadline
        self.pending = bytearray()
        self.next_id = 0
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)

    def send(self, message):
        try:
            self.process.stdin.write((json.dumps(message) + '\n').encode())
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise QuotaError('cli_closed') from None

    def call(self, method, params=None):
        self.next_id += 1
        ident = self.next_id
        request = {'id': ident, 'method': method}
        if params is not None:
            request['params'] = params
        self.send(request)
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise QuotaError('timeout')
            if b'\n' not in self.pending:
                if not self.selector.select(remaining):
                    raise QuotaError('timeout')
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise QuotaError('cli_closed')
                self.pending.extend(chunk)
                if len(self.pending) > 4 * 1024 * 1024:
                    raise QuotaError('response_too_large')
                continue
            line, _, rest = self.pending.partition(b'\n')
            self.pending = bytearray(rest)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                raise QuotaError('invalid_protocol') from None
            if not isinstance(message, dict):
                raise QuotaError('invalid_protocol')
            if 'method' in message:
                if 'id' in message:
                    # Never service token refresh / approval / attestation requests.
                    self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Read-only quota client'}})
                continue
            if message.get('id') != ident:
                continue
            if 'error' in message:
                raise QuotaError('quota_request_failed' if method == 'account/rateLimits/read' else 'account_request_failed')
            result = message.get('result')
            if not isinstance(result, dict):
                raise QuotaError('invalid_protocol')
            return result


def _find_cli():
    bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return shutil.which('codex')


def _stop(process):
    # Own process group only; never touches an existing desktop app-server.
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
    for pipe in (process.stdin, process.stdout):
        if pipe:
            pipe.close()


def read_quota(codex_home, timeout=20, codex_binary=None):
    """Fetch fresh quota with a bounded official CLI subprocess; JSON-safe result.

    status: ok, not_logged_in, unknown, error. All errors are sanitized codes.
    coreRemainingPercent: minimum known Codex window, None when unavailable.
    No forced token refresh, login, logout, reset-credit use, or model turn.
    """
    result = {'status': 'unknown', 'account': None, 'limits': [],
              'coreRemainingPercent': None, 'checkedAt': int(time.time()), 'error': None}
    home = Path(codex_home).expanduser().resolve()
    if not home.is_dir():
        result.update(status='not_logged_in', error='profile_missing')
        return result
    binary = codex_binary or _find_cli()
    if not binary:
        result.update(status='error', error='cli_not_found')
        return result
    timeout = max(0.1, min(float(timeout), 120))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(home), threading.Lock())
    if not lock.acquire(timeout=timeout):
        result.update(status='error', error='profile_busy')
        return result
    process = None
    rpc = None
    try:
        env = dict(os.environ)
        for key in ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_ELECTRON_USER_DATA_PATH',
                    'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE'):
            env.pop(key, None)
        env['CODEX_HOME'] = str(home)
        env['RUST_LOG'] = 'off'
        process = subprocess.Popen([str(binary), 'app-server', '--stdio',
                                    '-c', 'analytics.enabled=false', '-c', 'feedback.enabled=false',
                                    '-c', 'otel.exporter="none"', '-c', 'otel.trace_exporter="none"'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   cwd=home, env=env, start_new_session=True)
        rpc = _RPC(process, time.monotonic() + timeout)
        rpc.call('initialize', {'clientInfo': {'name': 'codex_shadow_clones', 'title': 'Codex Shadow Clones', 'version': '0.1.0'}})
        rpc.send({'method': 'initialized'})
        data = rpc.call('account/read', {'refreshToken': False})
        account = data.get('account')
        if not isinstance(account, dict):
            result['status'] = 'not_logged_in'
            return result
        email = account.get('email')
        email = email if isinstance(email, str) and email.strip() else None
        result['account'] = {'type': account.get('type'), 'email': email, 'planType': account.get('planType'),
                             'id': hashlib.sha256(email.strip().casefold().encode()).hexdigest() if email else None,
                             'identitySource': 'email' if email else None}
        if account.get('type') != 'chatgpt':
            result['error'] = 'subscription_quota_unavailable'
            return result
        result['limits'] = normalize_limits(rpc.call('account/rateLimits/read'))
        core = next((bucket for bucket in result['limits'] if bucket['isCodex']), None)
        if core:
            values = [window['remainingPercent'] for window in core['windows']]
            if core['limitReached']:
                result['coreRemainingPercent'] = 0
            elif any(window['name'] == 'primary' for window in core['windows']) and values and all(value is not None for value in values):
                result['coreRemainingPercent'] = min(values)
        result['status'] = 'ok' if result['coreRemainingPercent'] is not None else 'unknown'
        return result
    except QuotaError as exc:
        result.update(status='error', error=str(exc))
        return result
    except (OSError, ValueError):
        result.update(status='error', error='cli_unavailable')
        return result
    finally:
        if rpc:
            rpc.selector.close()
        if process:
            _stop(process)
        lock.release()
        result['checkedAt'] = int(time.time())
