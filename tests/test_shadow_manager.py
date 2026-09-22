"""Offline contract tests: quota selection, profile isolation, safe focus/sync."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import threading
import time
from unittest.mock import patch

import shadow_clones as manager_module

NOW = 2_000_000_000


def quota(identity, remaining, *, status='ok', age=0):
    return {'status': status, 'account': {'id': identity},
            'coreRemainingPercent': remaining, 'checkedAt': NOW-age}


def profile(number, value=None):
    result = {'id': f'codex{number}', 'name': f'Codex {number}',
              'source': number == 1, 'home': f'/fake/home{number}',
              'ui': f'/fake/ui {number}', 'pending_sync': False}
    if value is not None:
        result['quota'] = value
    return result


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(manager_module.time, 'time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_highest_remaining_distinct_account_selected(self):
        profiles = [profile(1, quota('a', 5)), profile(2, quota('b', 70)),
                    profile(3, quota('c', 90))]
        self.assertEqual(manager_module.recommended_profile(profiles, 'codex1'), 'codex3')

    def test_same_account_unknown_error_expired_empty_never_selected(self):
        bad = [quota('a', 99), quota('b', None), quota('b', 99, status='error'),
               quota('b', 99, status='unknown'), quota('b', 99, age=300),
               quota(None, 99), quota('b', 0)]
        for candidate in bad:
            with self.subTest(candidate=candidate):
                self.assertIsNone(manager_module.recommended_profile(
                    [profile(1, quota('a', 5)), profile(2, candidate)], 'codex1'))

    def test_missing_current_identity_cannot_select_different_account(self):
        self.assertIsNone(manager_module.recommended_profile(
            [profile(1, quota(None, 5)), profile(2, quota('b', 90))], 'codex1'))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patches = [patch.object(manager_module, 'RUNTIME', root),
                   patch.object(manager_module, 'REGISTRY', root/'registry.json'),
                   patch.object(manager_module.time, 'time', return_value=NOW),
                   patch.object(manager_module, 'process_map', return_value={'codex1':111, 'codex2':222})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.manager = manager_module.Manager()
        self.manager.data = {'selected':'codex1', 'auto_switch':True, 'threshold':10,
                             'profiles':[profile(1), profile(2)]}

    def refresh_with(self, current, target):
        by_home = {'/fake/home1':current, '/fake/home2':target}
        with patch.object(manager_module, 'read_quota', side_effect=lambda home: copy.deepcopy(by_home[str(home)])):
            self.manager.refresh()

    def test_history_job_does_not_block_http_and_excludes_other_writes(self):
        started, finish = threading.Event(), threading.Event()
        def slow_import(_):
            started.set()
            finish.wait(2)
        with patch.object(self.manager, 'import_profile_history', side_effect=slow_import), \
             patch.object(manager_module, 'process_map', return_value={'codex1': 111, 'codex2': None}):
            self.manager.action({'action': 'history', 'id': 'codex2'})
            self.assertTrue(started.wait(1))
            self.assertTrue(self.manager.working)
            try:
                with self.assertRaises(RuntimeError):
                    self.manager.action({'action': 'sync', 'id': 'codex2'})
            finally:
                finish.set()
            self.assertTrue(self.manager.operation.acquire(timeout=1))
            self.manager.operation.release()
            self.assertFalse(self.manager.working)

    def test_history_job_rejects_live_profile_before_import(self):
        with self.assertRaisesRegex(RuntimeError, '退出目标分身'):
            self.manager.import_profile_history('codex2')

    def test_update_history_refreshes_existing_and_reports_conflicts(self):
        with patch.object(manager_module, 'process_map', return_value={'codex1':111,'codex2':None}), \
             patch('shadow_history.repair_history', return_value={'repaired':2,'conflicts':1,'failed':0,'status':'partial'}) as refresh, \
             patch('shadow_history.import_history', return_value={'imported':1,'skipped':3,'failed':0,'status':'complete'}) as add, \
             patch.object(manager_module, 'merge_imported_sidebar'):
            self.manager.import_profile_history('codex2')
        refresh.assert_called_once()
        add.assert_called_once()
        result=self.manager.profile('codex2')['history']
        self.assertEqual(result['updated'],2)
        self.assertEqual(result['conflicts'],1)
        self.assertEqual(result['status'],'partial')

    def test_low_quota_auto_switch_only_if_other_account_above_threshold(self):
        with patch.object(self.manager, 'open') as opened:
            self.refresh_with(quota('a', 10), quota('b', 70))
            opened.assert_called_once_with('codex2')
        self.assertEqual(self.manager.last_switch, NOW)
        self.assertFalse(self.manager.refreshing)

    def test_auto_switch_guards(self):
        cases = [
            (quota('a', 11), quota('b', 80), True, 0),
            (quota('a', 1), quota('b', 10), True, 0),
            (quota('a', 1), quota('a', 80), True, 0),
            (quota('a', 1, status='error'), quota('b', 80), True, 0),
            (quota('a', 1), quota('b', 80), False, 0),
            (quota('a', 1), quota('b', 80), True, NOW-599),
        ]
        for current,target,automatic,last in cases:
            with self.subTest(current=current,target=target,automatic=automatic,last=last):
                self.manager.data['auto_switch'] = automatic
                self.manager.last_switch = last
                with patch.object(self.manager, 'open') as opened:
                    self.refresh_with(current, target)
                    opened.assert_not_called()

    def test_running_profile_sync_is_queued_never_written(self):
        with patch.object(manager_module, 'seed_home') as seed:
            self.manager.action({'action':'sync', 'id':'codex2'})
            seed.assert_not_called()
        self.assertTrue(self.manager.profile('codex2')['pending_sync'])

    def test_original_profile_cannot_be_seed_target(self):
        with patch.object(manager_module, 'seed_home') as seed:
            with self.assertRaises(RuntimeError):
                self.manager.action({'action':'sync', 'id':'codex1'})
            seed.assert_not_called()

    def test_snapshot_omits_internal_paths(self):
        self.manager.quota['codex2'] = quota('b', 70)
        state = self.manager.snapshot()
        for p in state['profiles']:
            self.assertNotIn('home', p)
            self.assertNotIn('ui', p)
        self.assertEqual(self.manager.profile('codex2')['home'], '/fake/home2')

    def test_invalid_settings_rejected_without_registry_mutation(self):
        before = copy.deepcopy(self.manager.data)
        for threshold,enabled in [(float('nan'), True), (float('inf'), True), (-1,True), (91,True),(10,'true')]:
            with self.subTest(threshold=threshold,enabled=enabled):
                with self.assertRaises(ValueError):
                    self.manager.action({'action':'settings','threshold':threshold,'auto_switch':enabled})
                self.assertEqual(self.manager.data, before)


class ProcessIsolationTests(unittest.TestCase):
    def test_launch_uses_independent_homes_and_removes_inherited_auth(self):
        with patch.object(manager_module, 'check_version'), patch.object(manager_module.subprocess, 'run') as run:
            with patch.dict(os.environ, {'CODEX_HOME':'/original','CODEX_SQLITE_HOME':'/original/db',
                 'CODEX_ELECTRON_USER_DATA_PATH':'/original/ui','OPENAI_API_KEY':'test-only','CODEX_API_KEY':'test-only'}):
                manager_module.launch_profile(profile(2))
        args = run.call_args.args[0]
        env = run.call_args.kwargs['env']
        self.assertIn('CODEX_HOME=/fake/home2', args)
        self.assertIn('CODEX_ELECTRON_USER_DATA_PATH=/fake/ui 2', args)
        self.assertIn('--user-data-dir=/fake/ui 2', args)
        for key in ('CODEX_HOME','CODEX_SQLITE_HOME','CODEX_ELECTRON_USER_DATA_PATH','OPENAI_API_KEY','CODEX_API_KEY'):
            self.assertNotIn(key, env)

    def test_live_profile_focus_does_not_launch_or_switch_credentials(self):
        with patch.object(manager_module, 'check_version'), patch.object(manager_module, 'focus_pid') as focus, patch.object(manager_module.subprocess,'run') as run:
            manager_module.launch_profile(profile(2), 222)
            focus.assert_called_once_with(222)
            run.assert_not_called()

    def test_stopped_original_never_launched_with_shared_db(self):
        with patch.object(manager_module, 'check_version'), patch.object(manager_module.subprocess,'run') as run:
            with self.assertRaises(RuntimeError):
                manager_module.launch_profile(profile(1))
            run.assert_not_called()

    def test_process_matching_keeps_distinct_profiles_with_spaces(self):
        executable = str(manager_module.APP/'Contents/MacOS/ChatGPT')
        rows = f'111 {executable}\n222 {executable} --user-data-dir=/fake/ui 2\n333 {executable} --user-data-dir=/fake/ui 20\n444 unrelated {executable}\n'
        with patch.object(manager_module.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=rows)):
            found = manager_module.process_map([profile(1),profile(2)])
        self.assertEqual(found, {'codex1':111,'codex2':222})


if __name__ == '__main__':
    unittest.main()
