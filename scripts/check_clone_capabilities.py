#!/usr/bin/env python3
"""Read-only configuration audit, not an end-to-end capability claim.

Does not open credentials, initialize app servers, connect to pipes, or alter
running instances. Output contains only profile IDs, counts and status flags.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _read_config(path, parser):
    try:
        value = parser(path.read_text())
    except FileNotFoundError:
        return {}, 'missing'
    except (OSError, ValueError, TypeError):
        return {}, 'unreadable_or_invalid'
    return (value, 'readable') if isinstance(value, dict) else ({}, 'unreadable_or_invalid')


def _services(env):
    env = _mapping(env)
    raw = env.get('NODE_REPL_TRUSTED_SERVICES')
    status = 'missing' if raw is None else 'invalid'
    services = {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            services = parsed
            status = 'readable'
    return {
        'status': status,
        'sky_declared': 'sky' in services,
        'browser_declared': 'browser' in services,
        'sky_endpoint_configured': isinstance(services.get('sky'), str) and bool(services['sky'].strip()),
        'browser_endpoint_configured': isinstance(services.get('browser'), str) and bool(services['browser'].strip()),
    }


def _surfaces(env):
    raw = _mapping(env).get('CUA_REPL_ENABLED_SURFACES')
    if raw is None:
        return {'status': 'missing', 'enabled': [], 'unrecognized_count': 0}
    if not isinstance(raw, str):
        return {'status': 'invalid', 'enabled': [], 'unrecognized_count': 0}
    names = {name.strip() for name in raw.split(',') if name.strip()}
    known = {'browser', 'computer', 'chrome'}
    return {'status': 'readable', 'enabled': sorted(names & known),
            'unrecognized_count': len(names - known)}


def _computer_plugin_configs(home):
    try:
        manifests = sorted((home/'plugins/cache/openai-bundled/unified-computer-use').glob('*/.mcp.json'))
    except OSError:
        return {'scan_status': 'unreadable', 'configurations': []}
    configurations = []
    for path in manifests:
        config, status = _read_config(path, json.loads)
        servers = _mapping(config.get('mcpServers'))
        server = _mapping(servers.get('cua_repl'))
        configurations.append({
            'configuration_status': status,
            'cua_repl_present': isinstance(servers.get('cua_repl'), dict),
            'cua_repl_enabled': server.get('enabled') is True,
            'trusted_services': _services(server.get('env')),
            'surfaces': _surfaces(server.get('env')),
        })
    return {'scan_status': 'readable', 'configurations': configurations}


def _daemon_evidence(home):
    try:
        # Codex canonicalizes CODEX_HOME; a short alias can still lead to an
        # overlong default address. Keep endpoint existence a separate fact.
        path = home.resolve()/'app-server-control/app-server-control.sock'
        path_bytes = len(str(path).encode('utf-8'))
    except (OSError, RuntimeError):
        return {'daemon_socket_path_bytes': None,
                'daemon_socket_path_exceeds_macos_limit': None,
                'daemon_socket_endpoint': 'path_unresolvable'}
    try:
        endpoint = 'socket_present' if stat.S_ISSOCK(path.stat().st_mode) else 'non_socket_present'
    except FileNotFoundError:
        endpoint = 'missing'
    except OSError:
        endpoint = 'unreadable'
    return {'daemon_socket_path_bytes': path_bytes,
            'daemon_socket_path_exceeds_macos_limit': path_bytes >= 104,
            'daemon_socket_endpoint': endpoint}


def _bundled_marketplace(config, home):
    market = _mapping(_mapping(config.get('marketplaces')).get('openai-bundled'))
    if not market:
        return 'not_registered'
    source = market.get('source')
    if market.get('source_type') != 'local' or not isinstance(source, str):
        return 'unexpected_source'
    try:
        actual = Path(source).resolve()
        expected = home.resolve()/'.tmp/bundled-marketplaces/openai-bundled'
        if actual == expected:
            return 'target_runtime'
        if actual.parts[-3:] == ('.tmp', 'bundled-marketplaces', 'openai-bundled'):
            return 'foreign_runtime'
    except (OSError, RuntimeError):
        pass
    return 'unexpected_source'


def _automation_evidence(home):
    states = {}
    invalid = 0
    try:
        paths = sorted((home/'automations').glob('*/automation.toml'))
    except OSError:
        return {'definitions': 0, 'states': {}, 'invalid': 1}
    for path in paths:
        try:
            status = tomllib.loads(path.read_text()).get('status')
        except (OSError, ValueError, TypeError):
            invalid += 1
            continue
        if isinstance(status, str):
            states[status] = states.get(status, 0) + 1
        else:
            invalid += 1
    return {'definitions': len(paths), 'states': states, 'invalid': invalid}


def _memory_evidence(home):
    try:
        files = [p for p in (home/'memories').rglob('*')
                 if p.is_file() and not any(part.startswith('.') for part in p.relative_to(home/'memories').parts)]
    except OSError:
        return {'present': False, 'snapshot_files': 0}
    return {'present': (home/'memories').is_dir(),
            'snapshot_files': sum(p.suffix.lower() in {'.md', '.jsonl'} for p in files)}


def inspect_home(home):
    home = Path(home)
    config, config_status = _read_config(home/'config.toml', tomllib.loads)
    plugins = _mapping(config.get('plugins'))
    node = _mapping(_mapping(config.get('mcp_servers')).get('node_repl'))
    services = _services(node.get('env'))
    return {
        'configuration_status': config_status,
        'bundled_marketplace_source': _bundled_marketplace(config, home),
        'computer_plugin_enabled': _mapping(plugins.get('computer-use@openai-bundled')).get('enabled') is True,
        'computer_helper_present': (home/'computer-use/Codex Computer Use.app').is_dir(),
        'computer_service_in_saved_node_config': services['sky_declared'],
        'browser_service_in_saved_node_config': services['browser_declared'],
        'saved_node_trusted_services': services,
        'unified_computer_use_plugin': _computer_plugin_configs(home),
        'app_tools_plugin_enabled': _mapping(plugins.get('codex-app-tools@openai-bundled')).get('enabled') is True,
        **_daemon_evidence(home),
        'automations': _automation_evidence(home),
        'skills_present': (home/'skills').is_dir(),
        'memories': _memory_evidence(home),
        'runtime_tools_and_os_permissions': 'not_verified',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=ROOT/'.runtime/shadow-clones.json')
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text())
    result = {}
    for profile in registry['profiles']:
        try:
            result[profile['id']] = inspect_home(profile['home'])
        except (OSError, ValueError, TypeError):
            result[profile['id']] = {'error': 'configuration_unreadable'}
    print(json.dumps({'evidence': 'configuration_only', 'profiles': result}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
