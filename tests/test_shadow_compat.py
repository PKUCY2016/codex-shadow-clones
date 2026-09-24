"""Unknown desktop builds must prove local isolation before admission."""
import os
from contextlib import closing
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import shadow_compat
import shadow_seed


CONTRACT = (b'CODEX_ELECTRON_USER_DATA_PATH?.trim()?CODEX_HOME '
            b'hasExplicitUserDataPath:!!process.env.CODEX_ELECTRON_USER_DATA_PATH?.trim() '
            b'app.setPath(`userData` --user-data-dir= requestSingleInstanceLock()')


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)/'ChatGPT.app'
        contents = self.app/'Contents'
        (contents/'MacOS').mkdir(parents=True)
        (contents/'Resources').mkdir()
        (contents/'MacOS/ChatGPT').touch()
        (contents/'Resources/codex').touch()
        (contents/'Resources/app.asar').write_bytes(CONTRACT)
        self.plist = contents/'Info.plist'
        self.info = {'CFBundleIdentifier':'com.openai.codex',
                     'CFBundleShortVersionString':'26.918.1', 'CFBundleVersion':'11000'}
        self.write_plist()

    def write_plist(self):
        self.plist.write_bytes(plistlib.dumps(self.info))

    def test_unknown_build_passes_only_after_signature_contract_and_isolated_rpc(self):
        with patch.object(shadow_compat, '_verify_signature') as signature, \
             patch.object(shadow_compat, '_rpc_smoke') as rpc:
            report = shadow_compat.check_compatibility(self.app)
        self.assertEqual(report.method, 'auto_probe')
        signature.assert_called_once_with(self.app)
        rpc.assert_called_once_with(self.app/'Contents/Resources/codex')

    def test_known_build_keeps_fast_path(self):
        self.info['CFBundleShortVersionString'], self.info['CFBundleVersion'] = shadow_compat.CHECKED
        self.write_plist()
        with patch.object(shadow_compat, '_verify_signature') as signature, \
             patch.object(shadow_compat, '_rpc_smoke') as rpc:
            report = shadow_compat.check_compatibility(self.app)
        self.assertEqual(report.method, 'checked')
        signature.assert_not_called()
        rpc.assert_not_called()

    def test_known_build_can_be_explicitly_reprobed(self):
        self.info['CFBundleShortVersionString'], self.info['CFBundleVersion'] = shadow_compat.CHECKED
        self.write_plist()
        with patch.object(shadow_compat, '_verify_signature') as signature, \
             patch.object(shadow_compat, '_rpc_smoke') as rpc:
            report = shadow_compat.check_compatibility(self.app, force_probe=True)
        self.assertEqual(report.method, 'auto_probe')
        signature.assert_called_once_with(self.app)
        rpc.assert_called_once()

    def test_wrong_app_and_changed_isolation_contract_remain_blocked(self):
        self.info['CFBundleIdentifier'] = 'example.other'
        self.write_plist()
        with self.assertRaisesRegex(shadow_compat.CompatibilityError, '应用身份不符'):
            shadow_compat.check_compatibility(self.app)
        self.info['CFBundleIdentifier'] = 'com.openai.codex'
        self.write_plist()
        (self.app/'Contents/Resources/app.asar').write_bytes(
            CONTRACT.replace(b'CODEX_HOME', b'OTHER_HOME'))
        with patch.object(shadow_compat, '_verify_signature'), \
             patch.object(shadow_compat, '_rpc_smoke') as rpc:
            with self.assertRaisesRegex(shadow_compat.CompatibilityError, '独立数据目录'):
                shadow_compat.check_compatibility(self.app)
        rpc.assert_not_called()

    def test_older_build_is_not_admitted_by_a_compatible_looking_bundle(self):
        self.info['CFBundleShortVersionString'] = '26.916.1'
        self.write_plist()
        with patch.object(shadow_compat, '_verify_signature') as signature:
            with self.assertRaisesRegex(shadow_compat.CompatibilityError, '早于已核查'):
                shadow_compat.check_compatibility(self.app)
        signature.assert_not_called()

    def test_failed_signature_or_rpc_never_admits_unknown_build(self):
        with patch.object(shadow_compat, '_verify_signature',
                          side_effect=shadow_compat.CompatibilityError('bad signature')), \
             patch.object(shadow_compat, '_rpc_smoke') as rpc:
            with self.assertRaises(shadow_compat.CompatibilityError):
                shadow_compat.check_compatibility(self.app)
        rpc.assert_not_called()
        with patch.object(shadow_compat, '_verify_signature'), \
             patch.object(shadow_compat, '_rpc_smoke',
                          side_effect=shadow_compat.CompatibilityError('bad rpc')):
            with self.assertRaises(shadow_compat.CompatibilityError):
                shadow_compat.check_compatibility(self.app)

    def test_signature_requires_verified_openai_team(self):
        good = subprocess.CompletedProcess([], 0, b'', b'')
        wrong = subprocess.CompletedProcess([], 0, b'', b'TeamIdentifier=OTHER\n')
        with patch.object(shadow_compat.subprocess, 'run', side_effect=[good, wrong]):
            with self.assertRaisesRegex(shadow_compat.CompatibilityError, '团队不符'):
                shadow_compat._verify_signature(self.app)

    def test_rpc_smoke_uses_disposable_home_without_parent_credentials(self):
        observed = {}
        class FakeRPC:
            def __init__(self, home, **kwargs):
                observed['home'] = home
                observed.update(kwargs)
            def call(self, method, params):
                observed['method'] = method
                return {'data':[], 'nextCursor':None}
            def close(self):
                observed['closed'] = True
        with patch.dict(os.environ, {'OPENAI_API_KEY':'secret-fixture',
                                      'CODEX_HOME':'/parent/home',
                                      'CODEX_THREAD_ID':'parent-task'}), \
             patch.object(shadow_seed, 'ProjectRPC', FakeRPC), \
             patch.object(shadow_compat, '_check_state_schema') as schema:
            shadow_compat._rpc_smoke(self.app/'Contents/Resources/codex')
        self.assertEqual(set(observed['environment']), {'HOME','PATH','TMPDIR'})
        self.assertEqual(observed['method'], 'project/list')
        self.assertNotIn('secret-fixture', repr(observed))
        self.assertTrue(observed['closed'])
        self.assertFalse(observed['home'].exists())
        schema.assert_called_once_with(observed['home'])

    def test_new_state_schema_is_rejected_before_existing_profiles_are_touched(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with closing(sqlite3.connect(home/'state_5.sqlite')) as database:
                database.execute('CREATE TABLE projects (id TEXT)')
            with self.assertRaisesRegex(shadow_compat.CompatibilityError, '结构已改变'):
                shadow_compat._check_state_schema(home)


if __name__ == '__main__':
    unittest.main()
