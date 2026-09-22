import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1]/'scripts/check_clone_capabilities.py'
spec = importlib.util.spec_from_file_location('check_clone_capabilities', SCRIPT)
capabilities = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capabilities)


class CloneCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shadow-caps-', dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/'home'
        self.home.mkdir()

    def write_plugin(self, server, version='1.0'):
        path = self.home/'plugins/cache/openai-bundled/unified-computer-use'/version/'.mcp.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'mcpServers': {'cua_repl': server}}))
        return path

    def test_plugin_can_configure_computer_without_top_level_node_sky(self):
        self.home.joinpath('config.toml').write_text('''
[plugins."computer-use@openai-bundled"]
enabled = true
[mcp_servers.node_repl.env]
NODE_REPL_TRUSTED_SERVICES = '{"browser":"/private/hidden-browser"}'
''')
        self.write_plugin({'enabled': True, 'env': {
            'NODE_REPL_TRUSTED_SERVICES': json.dumps({'sky': '/private/hidden-sky', 'browser': '/private/hidden-browser'}),
            'CUA_REPL_ENABLED_SURFACES': 'browser,computer',
        }})
        result = capabilities.inspect_home(self.home)
        self.assertFalse(result['computer_service_in_saved_node_config'])
        plugin = result['unified_computer_use_plugin']['configurations'][0]
        self.assertTrue(plugin['cua_repl_enabled'])
        self.assertTrue(plugin['trusted_services']['sky_endpoint_configured'])
        self.assertTrue(plugin['trusted_services']['browser_endpoint_configured'])
        self.assertEqual(plugin['surfaces']['enabled'], ['browser', 'computer'])
        self.assertEqual(result['runtime_tools_and_os_permissions'], 'not_verified')
        self.assertNotIn('/private/hidden', json.dumps(result))

    def test_null_or_malformed_services_never_become_a_runtime_claim(self):
        cases = [None, [], {'NODE_REPL_TRUSTED_SERVICES': 'null'},
                 {'NODE_REPL_TRUSTED_SERVICES': '[]'},
                 {'NODE_REPL_TRUSTED_SERVICES': '{private-secret-invalid'},
                 {'NODE_REPL_TRUSTED_SERVICES': {'sky': 'private-secret'}}]
        for env in cases:
            with self.subTest(env=env):
                self.write_plugin({'enabled': True, 'env': env})
                result = capabilities.inspect_home(self.home)
                plugin = result['unified_computer_use_plugin']['configurations'][0]
                self.assertFalse(plugin['trusted_services']['sky_endpoint_configured'])
                self.assertFalse(plugin['trusted_services']['browser_endpoint_configured'])
                self.assertEqual(result['runtime_tools_and_os_permissions'], 'not_verified')
                self.assertNotIn('private-secret', json.dumps(result))

    def test_declared_null_service_is_distinct_from_configured_endpoint(self):
        self.write_plugin({'enabled': False, 'env': {
            'NODE_REPL_TRUSTED_SERVICES': '{"sky":null,"browser":42}',
            'CUA_REPL_ENABLED_SURFACES': ['computer'],
        }})
        plugin = capabilities.inspect_home(self.home)['unified_computer_use_plugin']['configurations'][0]
        self.assertFalse(plugin['cua_repl_enabled'])
        self.assertTrue(plugin['trusted_services']['sky_declared'])
        self.assertFalse(plugin['trusted_services']['sky_endpoint_configured'])
        self.assertFalse(plugin['trusted_services']['browser_endpoint_configured'])
        self.assertEqual(plugin['surfaces']['status'], 'invalid')

    def test_missing_or_bad_config_still_reports_other_evidence_without_contents(self):
        manifest = self.write_plugin(None)
        cases = [(None, 'missing'), ('private-secret = [malformed', 'unreadable_or_invalid'),
                 ('plugins = "private-secret"', 'readable')]
        for contents, expected_status in cases:
            with self.subTest(contents=contents):
                config = self.home/'config.toml'
                if contents is None:
                    config.unlink(missing_ok=True)
                else:
                    config.write_text(contents)
                manifest.write_text('{"private-secret": invalid}')
                result = capabilities.inspect_home(self.home)
                self.assertEqual(result['configuration_status'], expected_status)
                plugin = result['unified_computer_use_plugin']['configurations'][0]
                self.assertEqual(plugin['configuration_status'], 'unreadable_or_invalid')
                self.assertFalse(plugin['cua_repl_present'])
                self.assertEqual(result['daemon_socket_endpoint'], 'missing')
                self.assertNotIn('private-secret', json.dumps(result))
                self.assertNotIn(str(self.home), json.dumps(result))

    def test_short_alias_reports_canonical_utf8_socket_length(self):
        actual = Path(self.temp.name)/('影'*24)
        actual.mkdir()
        alias = Path(self.temp.name)/'short'
        alias.symlink_to(actual, target_is_directory=True)
        suffix = 'app-server-control/app-server-control.sock'
        self.assertLess(len(str(alias/suffix).encode()), 104)
        result = capabilities.inspect_home(alias)
        self.assertEqual(result['daemon_socket_path_bytes'], len(str(actual/suffix).encode()))
        self.assertTrue(result['daemon_socket_path_exceeds_macos_limit'])
        self.assertEqual(result['daemon_socket_endpoint'], 'missing')

    def test_regular_file_at_endpoint_is_not_a_socket_or_runtime_validation(self):
        endpoint = self.home/'app-server-control/app-server-control.sock'
        endpoint.parent.mkdir()
        endpoint.write_text('private-secret')
        result = capabilities.inspect_home(self.home)
        self.assertEqual(result['daemon_socket_endpoint'], 'non_socket_present')
        self.assertEqual(result['runtime_tools_and_os_permissions'], 'not_verified')
        self.assertNotIn('private-secret', json.dumps(result))

    def test_inherited_bundled_marketplace_is_reported_without_private_paths(self):
        config = self.home/'config.toml'
        for source, expected in (
            ('/private/other-user/.tmp/bundled-marketplaces/openai-bundled','foreign_runtime'),
            (str(self.home.resolve()/'.tmp/bundled-marketplaces/openai-bundled'),'target_runtime')):
            config.write_text('[marketplaces.openai-bundled]\nsource_type="local"\nsource='+json.dumps(source)+'\n')
            result = capabilities.inspect_home(self.home)
            self.assertEqual(result['bundled_marketplace_source'],expected)
            self.assertNotIn(source,json.dumps(result))

    def test_surfaces_report_only_known_names_without_leaking_unknown_values(self):
        self.write_plugin({'env': {'CUA_REPL_ENABLED_SURFACES': 'computer,computer,browser,private-secret'}})
        surfaces = capabilities.inspect_home(self.home)['unified_computer_use_plugin']['configurations'][0]['surfaces']
        self.assertEqual(surfaces, {'status': 'readable', 'enabled': ['browser', 'computer'], 'unrecognized_count': 1})

    def test_audit_reports_paused_automation_and_memory_snapshot_counts(self):
        automation = self.home/'automations/demo/automation.toml'
        automation.parent.mkdir(parents=True)
        automation.write_text('status = "PAUSED"\n')
        (self.home/'memories/rollout_summaries').mkdir(parents=True)
        (self.home/'memories/MEMORY.md').write_text('fixture')
        (self.home/'memories/rollout_summaries/run.md').write_text('fixture')
        result = capabilities.inspect_home(self.home)
        self.assertEqual(result['automations'], {'definitions': 1, 'states': {'PAUSED': 1}, 'invalid': 0})
        self.assertEqual(result['memories'], {'present': True, 'snapshot_files': 2})


if __name__ == '__main__':
    unittest.main()
