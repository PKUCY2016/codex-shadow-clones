import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from shadow_updates import (CHECK_INTERVAL, CURRENT_VERSION, MANIFEST_URL,
                            MAX_RESPONSE_BYTES, NETWORK_TIMEOUT, UPDATE_URL,
                            UpdateChecker, _is_newer, _parse_version, _read_remote)


def manifest(version, **extra):
    return json.dumps({'version': version, 'released': '2026-09-22', **extra}).encode()


def wait_finished(checker):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = checker.snapshot()
        if state['status'] != 'checking':
            return state
        time.sleep(0.001)
    raise AssertionError('update check did not finish')


class UpdateCheckerTests(unittest.TestCase):
    def check(self, payload):
        checker = UpdateChecker(reader=lambda: payload, clock=lambda: 1700000000)
        checker.check_async()
        return wait_finished(checker)

    def test_initial_snapshot_is_independent(self):
        checker = UpdateChecker()
        state = checker.snapshot()
        self.assertEqual(state, {
            'currentVersion': CURRENT_VERSION, 'latestVersion': None,
            'status': 'idle', 'checkedAt': None, 'error': None, 'url': UPDATE_URL,
        })
        state['status'] = 'modified'
        self.assertEqual(checker.snapshot()['status'], 'idle')

    def test_new_version_has_fixed_installation_url(self):
        state = self.check(manifest('99.0.0', url='https://untrusted.example/install.sh'))
        self.assertEqual(state['status'], 'available')
        self.assertEqual(state['latestVersion'], '99.0.0')
        self.assertEqual(state['url'], UPDATE_URL)
        self.assertEqual(state['checkedAt'], 1700000000)
        self.assertIsNone(state['error'])

    def test_current_and_older_remote_versions_do_not_offer_downgrade(self):
        for version in (CURRENT_VERSION, '0.0.0'):
            with self.subTest(version=version):
                state = self.check(manifest(version))
                self.assertEqual(state['status'], 'up_to_date')
                self.assertEqual(state['latestVersion'], version)

    def test_strict_semver_rejects_malformed_versions(self):
        for version in ('1.2', 'v1.2.3', '01.2.3', '1.2.3.4', '1.2.3-01',
                        '1.2.3+', '1.2.3-', '1.2.3\n', '1.2.3 beta',
                        '1.2.3-💥', '', None, 1.2, True, '9' * 129 + '.0.0'):
            with self.subTest(version=version):
                state = self.check(manifest(version))
                self.assertEqual(state['status'], 'error')
                self.assertEqual(state['error'], 'invalid_manifest')
                self.assertIsNone(state['latestVersion'])

    def test_semver_comparison_preserves_integer_and_prerelease_order(self):
        ordered = ['1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta',
                   '1.0.0-beta', '1.0.0-beta.2', '1.0.0-beta.11',
                   '1.0.0-rc.1', '1.0.0', '1.0.1', '1.9.0', '1.10.0', '2.0.0']
        for current, latest in zip(ordered, ordered[1:]):
            with self.subTest(current=current, latest=latest):
                self.assertTrue(_is_newer(latest, current))
                self.assertFalse(_is_newer(current, latest))
        self.assertFalse(_is_newer('1.0.0+build.2', '1.0.0+build.1'))
        self.assertEqual(_parse_version('1.0.0-alpha-1+001')[0], (1, 0, 0))

    def test_invalid_and_oversized_responses_are_bounded_errors(self):
        for payload, error in ((b'{', 'invalid_manifest'), (b'[]', 'invalid_manifest'),
                               (b'{}', 'invalid_manifest'), (b'\xff', 'invalid_manifest'),
                               (b'[' * 2000, 'invalid_manifest'),
                               ('not bytes', 'invalid_manifest'),
                               (b'x' * (MAX_RESPONSE_BYTES + 1), 'response_too_large')):
            with self.subTest(error=error, size=len(payload)):
                state = self.check(payload)
                self.assertEqual(state['status'], 'error')
                self.assertEqual(state['error'], error)

    def test_network_failure_is_sanitized_and_can_be_retried(self):
        reader = MagicMock(side_effect=[OSError('SECRET_PROXY_PASSWORD'), manifest('99.0.0')])
        checker = UpdateChecker(reader=reader)
        checker.check_async()
        failed = wait_finished(checker)
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['error'], 'check_failed')
        self.assertNotIn('SECRET', json.dumps(failed))
        checker.check_async()
        self.assertEqual(reader.call_count, 1)
        checker.check_async(force=True)
        self.assertEqual(wait_finished(checker)['status'], 'available')
        self.assertEqual(reader.call_count, 2)

    def test_checks_are_throttled_for_six_hours_and_manual_check_bypasses_cache(self):
        now = [0.0]
        reader = MagicMock(return_value=manifest(CURRENT_VERSION))
        checker = UpdateChecker(reader=reader, monotonic=lambda: now[0])
        checker.check_async()
        wait_finished(checker)
        now[0] = CHECK_INTERVAL - 1
        checker.check_async()
        self.assertEqual(reader.call_count, 1)
        now[0] = CHECK_INTERVAL
        checker.check_async()
        wait_finished(checker)
        self.assertEqual(reader.call_count, 2)
        checker.check_async(force=True)
        wait_finished(checker)
        self.assertEqual(reader.call_count, 3)

    def test_background_check_is_nonblocking_daemon_and_never_reentrant(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def reader():
            calls.append(threading.current_thread().daemon)
            entered.set()
            release.wait(2)
            return manifest(CURRENT_VERSION)

        checker = UpdateChecker(reader=reader)
        checker.check_async()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(checker.snapshot()['status'], 'checking')
            requesters = [threading.Thread(target=checker.check_async, kwargs={'force': True}) for _ in range(8)]
            for requester in requesters:
                requester.start()
            for requester in requesters:
                requester.join(1)
                self.assertFalse(requester.is_alive())
            self.assertEqual(calls, [True])
        finally:
            release.set()
        self.assertEqual(wait_finished(checker)['status'], 'up_to_date')

    def test_network_request_uses_fixed_url_timeout_and_bounded_read(self):
        response = MagicMock()
        response.read.return_value = manifest(CURRENT_VERSION)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch('shadow_updates.urllib.request.build_opener', return_value=opener) as build:
            self.assertEqual(_read_remote(), manifest(CURRENT_VERSION))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, MANIFEST_URL)
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(opener.open.call_args.kwargs['timeout'], NETWORK_TIMEOUT)
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
        redirect_handler = build.call_args.args[0]
        self.assertIsNone(redirect_handler.redirect_request(None, None, 302, '', {}, 'https://untrusted.example'))


if __name__ == '__main__':
    unittest.main()
