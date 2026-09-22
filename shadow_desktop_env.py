"""Keep desktop launches independent of the parent task's identity and bridge.

The official desktop creates its own app-tools server at startup. A valid Unix
socket owned by this user does not establish that it belongs to the target home.
Never forward or discover another desktop's bridge as a capability workaround.
"""
from __future__ import annotations


_INSTANCE_ENV = (
    'CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_ELECTRON_USER_DATA_PATH',
    'CODEX_APP_TOOLS_PIPE_PATH', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
    'NODE_REPL_HOST_SERVICES_PIPE_PATH', 'SKY_CUA_SERVICE_NATIVE_PIPE_PATH',
    'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL',
)


def desktop_environment(environment):
    result = dict(environment)
    for key in _INSTANCE_ENV:
        result.pop(key, None)
    return result
