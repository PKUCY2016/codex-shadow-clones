"""Updater restart boundaries: only the verified, idle manager may be stopped."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import launch_shadow as launcher


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.info = {'pid': 12345, 'port': 18318, 'token': 'x' * 40}
        self.state = {'profiles': [], 'working': False, 'update': {'currentVersion': '0.3.0'}}

    def test_busy_history_is_never_interrupted(self):
        self.state['working'] = True
        with patch.object(launcher, '_manager', return_value=(self.info, self.state)), \
             patch.object(launcher, '_request') as request, patch.object(launcher.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '同步历史'):
                launcher.stop_manager()
            request.assert_not_called()
            kill.assert_not_called()

    def test_authenticated_shutdown_then_wait_for_exit(self):
        with patch.object(launcher, '_manager', return_value=(self.info, self.state)), \
             patch.object(launcher, '_request', return_value={'ok': True}) as request, \
             patch.object(launcher.os, 'kill', side_effect=ProcessLookupError) as kill:
            launcher.stop_manager()
            request.assert_called_once_with(self.info, {'action': 'shutdown_manager', 'confirmed': True})
            kill.assert_called_once_with(12345, 0)

    def test_refresh_busy_retries_but_new_history_aborts(self):
        busy = urllib.error.HTTPError('http://127.0.0.1:18318', 409, 'busy', {}, None)
        with patch.object(launcher, '_manager', return_value=(self.info, self.state)), \
             patch.object(launcher, '_request', side_effect=[busy, {'working': True}]), \
             patch.object(launcher.time, 'sleep'), patch.object(launcher.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '同步历史'):
                launcher.stop_manager()
            kill.assert_not_called()

    def test_modern_service_error_never_uses_legacy_signal(self):
        bad = urllib.error.HTTPError('http://127.0.0.1:18318', 400, 'bad', {}, None)
        with patch.object(launcher, '_manager', return_value=(self.info, self.state)), \
             patch.object(launcher, '_request', side_effect=bad), \
             patch.object(launcher, '_stop_legacy_manager') as legacy:
            with self.assertRaises(RuntimeError):
                launcher.stop_manager()
            legacy.assert_not_called()

    def test_legacy_signal_rejects_unrelated_process_and_changed_state(self):
        for output, current in [
            (f'{os.getuid()} python3 /other/shadow_clones.py serve', (self.info, self.state)),
            (f'{os.getuid()} python3 {launcher.ROOT}/shadow_clones.py serve', (self.info, {'working': True})),
        ]:
            with self.subTest(output=output), \
                 patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)), \
                 patch.object(launcher, '_manager', return_value=current), \
                 patch.object(launcher.os, 'kill') as kill:
                with self.assertRaises(RuntimeError):
                    launcher._stop_legacy_manager(self.info)
                kill.assert_not_called()

    def test_legacy_signal_targets_only_verified_pid(self):
        output = f'{os.getuid()} python3 {launcher.ROOT}/shadow_clones.py serve'
        with patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)), \
             patch.object(launcher, '_manager', return_value=(self.info, self.state)), \
             patch.object(launcher.os, 'kill') as kill:
            launcher._stop_legacy_manager(self.info)
            kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_metadata_permissions_and_authentication_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            info_path = Path(directory)/'server.json'
            info_path.write_text(json.dumps(self.info))
            info_path.chmod(0o644)
            with patch.object(launcher, 'SERVER_INFO', info_path), patch.object(launcher, '_request') as request:
                with self.assertRaisesRegex(RuntimeError, '权限异常'):
                    launcher._manager()
                request.assert_not_called()
                info_path.chmod(0o600)
                request.side_effect = urllib.error.HTTPError('http://127.0.0.1:18318', 401, 'no', {}, None)
                with self.assertRaisesRegex(RuntimeError, '认证'):
                    launcher._manager()

    def test_timeout_does_not_masquerade_as_a_stopped_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            info_path = Path(directory)/'server.json'
            info_path.write_text(json.dumps(self.info))
            info_path.chmod(0o600)
            with patch.object(launcher, 'SERVER_INFO', info_path), patch.object(launcher, '_request') as request:
                request.side_effect = urllib.error.URLError(TimeoutError())
                with self.assertRaisesRegex(RuntimeError, '暂时无法确认'):
                    launcher._manager()
                request.side_effect = urllib.error.URLError(ConnectionRefusedError())
                self.assertIsNone(launcher._manager())

    def test_plain_launch_reloads_stale_manager_before_opening_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)/'Codex Shadow Clones.app'
            binary = app/'Contents/MacOS/ShadowMenu'
            binary.parent.mkdir(parents=True)
            binary.touch()
            helper = Path(directory)/'shadow-reload'
            fresh = {'profiles': [], 'working': False,
                     'update': {'currentVersion': launcher.build_menubar.CURRENT_VERSION}}
            with patch.object(launcher.build_menubar, 'APP', app), \
                 patch.object(launcher, '_manager', side_effect=[(self.info, self.state),
                                                                   (self.info, fresh)]), \
                 patch.object(launcher, 'stop_manager') as stop, \
                 patch.object(launcher, 'dashboard'), \
                 patch.object(launcher, '_reloader', return_value=helper), \
                 patch.object(launcher.subprocess, 'run') as run:
                launcher.launch()
            stop.assert_called_once_with()
            self.assertEqual(run.call_args.args[0], [str(helper), str(app)])


if __name__ == '__main__':
    unittest.main()


class LocalDashboardRequestTests(unittest.TestCase):
    def test_local_dashboard_authentication_bypasses_environment_proxy(self):
        import shadow_clones
        with patch.object(shadow_clones, 'SERVER_INFO') as info, \
             patch('urllib.request.build_opener') as opener, \
             patch.object(shadow_clones.subprocess, 'run'), patch('builtins.print'):
            info.read_text.return_value = '{"token":"local-fixture"}'
            opener.return_value.open.return_value.__enter__.return_value.status = 200
            shadow_clones.dashboard()
        self.assertEqual(opener.call_args.args[0].proxies, {})
        request = opener.return_value.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('http://127.0.0.1:'))
