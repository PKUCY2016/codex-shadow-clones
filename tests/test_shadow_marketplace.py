import copy
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest
from unittest.mock import Mock

from shadow_seed import repair_bundled_marketplace


class BundledMarketplaceRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shadow-marketplace-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/'clone'
        self.home.mkdir()
        self.config = self.home/'config.toml'
        self.before = '''# Keep these preferences and comments.
model = "fixture-model"
cli_auth_credentials_store = "file"

[plugins."computer-use@openai-bundled"]
enabled = true
[plugins."computer-use@openai-bundled".mcp_servers.cua_repl]
enabled_tools = ["node_repl"]
default_tools_approval_mode = "prompt"

[marketplaces.openai-bundled]
source_type = "local"
source = "/fixture/source-home/.tmp/bundled-marketplaces/openai-bundled"
[marketplaces.openai-bundled.installation]
display_name = "fixture bundled"

[marketplaces."my-private-market"]
source_type = "git"
source = "https://fixture.invalid/private-market"

[mcp_servers.custom.env]
FIXTURE_SETTING = "keep-me"
'''
        self.config.write_text(self.before)
        self.auth = self.home/'auth.json'
        self.auth.write_bytes(b'{"fixture-only":"never-copy-or-rewrite"}\n')
        self.auth_before = self.auth.read_bytes()

    def backups(self):
        return list((self.home/'.shadow-backups').glob('desktop-marketplace-*/config.toml'))

    def assert_unchanged(self):
        self.assertEqual(self.config.read_text(), self.before)
        self.assertEqual(self.auth.read_bytes(), self.auth_before)
        self.assertEqual(list(self.home.glob('.shadow-config-*.tmp')), [])

    def test_repair_preserves_login_plugin_permissions_and_other_settings(self):
        before_stat = self.auth.stat()
        expected = copy.deepcopy(tomllib.loads(self.before))
        expected['marketplaces'].pop('openai-bundled')
        can_write = Mock(return_value=True)
        self.assertTrue(repair_bundled_marketplace(self.home, can_write))
        self.assertEqual(tomllib.loads(self.config.read_text()), expected)
        self.assertIn('# Keep these preferences and comments.', self.config.read_text())
        self.assertEqual(self.auth.read_bytes(), self.auth_before)
        self.assertEqual(self.auth.stat().st_mtime_ns, before_stat.st_mtime_ns)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.before)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(list(self.home.glob('.shadow-config-*.tmp')), [])
        self.assertGreaterEqual(can_write.call_count, 2)
        self.assertFalse(repair_bundled_marketplace(self.home, lambda: True))
        self.assertEqual(len(self.backups()), 1)

    def test_running_target_does_not_change_config_or_create_backup(self):
        with self.assertRaises(RuntimeError):
            repair_bundled_marketplace(self.home, lambda: False)
        self.assert_unchanged()
        self.assertFalse((self.home/'.shadow-backups').exists())

    def test_target_starting_during_repair_keeps_config_and_original_backup(self):
        can_write = Mock(side_effect=[True, False])
        with self.assertRaises(RuntimeError):
            repair_bundled_marketplace(self.home, can_write)
        self.assert_unchanged()
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_text(), self.before)

    def test_concurrent_config_edit_is_preserved(self):
        changed = self.before.replace('fixture-model', 'new-user-model')
        calls = 0

        def can_write():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.config.write_text(changed)
            return True

        with self.assertRaises(RuntimeError):
            repair_bundled_marketplace(self.home, can_write)
        self.assertEqual(self.config.read_text(), changed)
        self.assertEqual(self.auth.read_bytes(), self.auth_before)
        self.assertEqual(self.backups()[0].read_text(), self.before)
        self.assertEqual(list(self.home.glob('.shadow-config-*.tmp')), [])

    def test_existing_correct_target_market_is_exact_noop(self):
        correct_source = str(self.home.resolve()/'.tmp/bundled-marketplaces/openai-bundled')
        correct = self.before.replace('/fixture/source-home/.tmp/bundled-marketplaces/openai-bundled', correct_source)
        self.config.write_text(correct)
        before_stat = self.config.stat()
        can_write = Mock(return_value=False)
        self.assertFalse(repair_bundled_marketplace(self.home, can_write))
        self.assertEqual(self.config.read_text(), correct)
        self.assertEqual(self.config.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertFalse((self.home/'.shadow-backups').exists())
        can_write.assert_not_called()

    def test_config_symlink_is_rejected_without_touching_target(self):
        target = Path(self.temp.name)/'source-config.toml'
        target.write_text(self.before)
        self.config.unlink()
        self.config.symlink_to(target)
        with self.assertRaises(RuntimeError):
            repair_bundled_marketplace(self.home, lambda: True)
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(target.read_text(), self.before)
        self.assertFalse((self.home/'.shadow-backups').exists())
        self.assertEqual(self.auth.read_bytes(), self.auth_before)

    def test_backup_directory_symlink_is_rejected_without_writing_outside_home(self):
        target = Path(self.temp.name)/'outside-backups'
        target.mkdir()
        (self.home/'.shadow-backups').symlink_to(target, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            repair_bundled_marketplace(self.home, lambda: True)
        self.assert_unchanged()
        self.assertEqual(list(target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
