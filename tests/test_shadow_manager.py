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

    def test_open_running_instances_never_repairs_their_configuration(self):
        with patch.object(manager_module,'repair_bundled_marketplace') as repair, \
                patch.object(manager_module,'launch_profile') as launch:
            self.manager.open('codex1')
            self.manager.open('codex2')
        repair.assert_not_called()
        self.assertEqual([call.args[1] for call in launch.call_args_list],[111,222])

    def test_managed_clone_prunes_old_history_backups(self):
        base = manager_module.RUNTIME/'clones'/'codex2'
        home = base/'codex-home'
        (base/'electron-data').mkdir(parents=True)
        backup_root = home/'.shadow-backups'
        sqlite = b'SQLite format 3\x00' + b'x'
        for stamp in (1, 2):
            folder = backup_root/f'history-{stamp}'
            folder.mkdir(parents=True)
            for name in ('state_5.sqlite', 'thread_history_1.sqlite'):
                (folder/name).write_bytes(sqlite)
        self.manager.data['profiles'][1].update(home=str(home), ui=str(base/'electron-data'))
        deleted, errors = self.manager.prune_old_backups()
        self.assertEqual((deleted, errors), (1, 0))
        self.assertFalse((backup_root/'history-1').exists())
        self.assertTrue((backup_root/'history-2').exists())

    def test_history_job_prunes_before_releasing_operation_lock(self):
        self.manager.operation.acquire()
        self.manager.working = True
        with patch.object(self.manager, 'unified_history'), \
                patch.object(self.manager, 'prune_old_backups', return_value=(2, 0)) as prune:
            self.manager.background_action('history_all', {})
        prune.assert_called_once_with()
        self.assertFalse(self.manager.working)
        self.assertTrue(self.manager.operation.acquire(blocking=False))
        self.manager.operation.release()

    def test_broken_backup_link_is_reported_without_following_it(self):
        base = manager_module.RUNTIME/'clones'/'codex2'
        home = base/'codex-home'
        (base/'electron-data').mkdir(parents=True)
        home.mkdir()
        (home/'.shadow-backups').symlink_to(base/'missing', target_is_directory=True)
        self.manager.data['profiles'][1].update(home=str(home), ui=str(base/'electron-data'))
        self.assertEqual(self.manager.prune_old_backups(), (0, 1))
        self.assertTrue((home/'.shadow-backups').is_symlink())

    def test_open_stopped_clone_repairs_marketplace_before_launch(self):
        events=[]
        def repair(home,can_write):
            self.assertEqual(home,Path('/fake/home2'))
            self.assertTrue(can_write())
            events.append('repair')
        with patch.object(manager_module,'process_map',return_value={'codex1':111,'codex2':None}), \
                patch.object(manager_module,'repair_bundled_marketplace',side_effect=repair), \
                patch.object(manager_module,'launch_profile',side_effect=lambda *_:events.append('launch')):
            self.manager.open('codex2')
        self.assertEqual(events,['repair','launch'])

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

    def test_stopped_original_opens_official_app_without_shared_db_flags(self):
        with patch.object(manager_module, 'check_version'), patch.object(manager_module.subprocess,'run') as run:
            manager_module.launch_profile(profile(1))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-a', str(manager_module.APP)])
        self.assertNotIn('CODEX_HOME', run.call_args.kwargs['env'])
        self.assertNotIn('CODEX_ELECTRON_USER_DATA_PATH', run.call_args.kwargs['env'])

    def test_process_matching_keeps_distinct_profiles_with_spaces(self):
        executable = str(manager_module.APP/'Contents/MacOS/ChatGPT')
        rows = f'111 {executable}\n222 {executable} --user-data-dir=/fake/ui 2\n333 {executable} --user-data-dir=/fake/ui 20\n444 unrelated {executable}\n'
        with patch.object(manager_module.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=rows)):
            found = manager_module.process_map([profile(1),profile(2)])
        self.assertEqual(found, {'codex1':111,'codex2':222})

    def test_process_with_extra_flags_is_still_running(self):
        executable = str(manager_module.APP/'Contents/MacOS/ChatGPT')
        rows = f'222 {executable} --user-data-dir=/fake/ui 2 --some-flag\n'
        with patch.object(manager_module.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0, stdout=rows)):
            self.assertEqual(manager_module.process_map([profile(2)]), {'codex2': 222})


class DeleteCloneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.runtime = root/'runtime'
        self.clone = self.runtime/'clones/codex2'
        self.source = root/'original'
        self.source.mkdir()
        (self.clone/'codex-home').mkdir(parents=True)
        (self.clone/'electron-data').mkdir()
        (self.clone/'codex-home/history.jsonl').write_text('original history')
        (self.clone/'electron-data/account').write_text('fixture account')
        for item in (patch.object(manager_module, 'RUNTIME', self.runtime),
                     patch.object(manager_module, 'REGISTRY', self.runtime/'registry.json'),
                     patch.object(manager_module, 'process_map',
                                  return_value={'codex1': 111, 'codex2': None})):
            item.start()
            self.addCleanup(item.stop)
        self.manager = manager_module.Manager()
        first, second = profile(1), profile(2)
        first.update(home=str(self.source), ui='')
        second.update(home=str(self.clone/'codex-home'), ui=str(self.clone/'electron-data'),
                      pending_sync=True, pending_history=True)
        self.manager.data = {'selected': 'codex2', 'auto_switch': False, 'threshold': 10,
                             'profiles': [first, second],
                             'history_sync': {'profiles': {'codex1': {}, 'codex2': {'pending': True}},
                                              'errors': {'codex2': 'fixture'}}}
        self.manager.quota['codex2'] = quota('fixture', 50)
        self.manager.save()

    def delete(self, **kwargs):
        self.manager.action({'action': 'delete', 'id': 'codex2', 'confirmed': True, **kwargs})

    def test_deletion_preserves_data_and_clears_selection_quota_and_pending(self):
        self.delete()
        self.assertFalse(self.clone.exists())
        self.assertEqual([p['id'] for p in self.manager.data['profiles']], ['codex1'])
        self.assertEqual(self.manager.data['selected'], 'codex1')
        self.assertNotIn('codex2', self.manager.quota)
        self.assertNotIn('codex2', self.manager.data['history_sync']['profiles'])
        self.assertNotIn('codex2', self.manager.data['history_sync']['errors'])
        receipt_path = next((self.runtime/'deleted-clones').glob('*.json'))
        receipt = json.loads(receipt_path.read_text())
        archive = Path(receipt['archivedPath'])
        self.assertEqual((archive/'codex-home/history.jsonl').read_text(), 'original history')
        self.assertEqual((archive/'electron-data/account').read_text(), 'fixture account')
        self.assertEqual(receipt['status'], 'deleted')
        self.assertEqual(receipt['profile']['id'], 'codex2')
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.source.is_dir())
        self.assertEqual(json.loads(manager_module.REGISTRY.read_text()), self.manager.data)

    def test_confirmation_must_be_literal_true(self):
        before = copy.deepcopy(self.manager.data)
        for confirmed in (False, None, 'true', 1):
            with self.subTest(confirmed=confirmed), self.assertRaisesRegex(RuntimeError, '确认'):
                self.delete(confirmed=confirmed)
        self.assertEqual(self.manager.data, before)
        self.assertTrue(self.clone.is_dir())

    def test_source_cannot_be_deleted_even_if_source_flag_is_corrupted(self):
        for value in (True, False):
            self.manager.profile('codex1')['source'] = value
            with self.assertRaisesRegex(RuntimeError, '原实例'):
                self.delete(id='codex1')
        self.assertTrue(self.source.is_dir())

    def test_running_or_unknown_process_state_cannot_be_deleted(self):
        for state in ({'codex2': 222}, {}, None, {'codex2': 'unknown'}):
            with self.subTest(state=state), patch.object(manager_module, 'process_map', return_value=state):
                with self.assertRaises(RuntimeError):
                    self.delete()
        with patch.object(manager_module, 'process_map', side_effect=OSError('ps failed')):
            with self.assertRaisesRegex(RuntimeError, '无法确认'):
                self.delete()
        self.assertTrue(self.clone.is_dir())

    def test_operation_in_progress_prevents_deletion(self):
        self.manager.operation.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, '稍后重试'):
                self.delete()
        finally:
            self.manager.operation.release()
        self.assertTrue(self.clone.is_dir())

    def test_external_launch_during_preparation_prevents_move(self):
        with patch.object(manager_module, 'process_map',
                          side_effect=[{'codex2': None}, {'codex2': 222}]):
            with self.assertRaisesRegex(RuntimeError, '退出目标分身'):
                self.delete()
        self.assertTrue(self.clone.is_dir())
        self.assertEqual(self.manager.data['selected'], 'codex2')

    def test_unmanaged_or_escaping_paths_are_rejected(self):
        original = copy.deepcopy(self.manager.profile('codex2'))
        for changes in ({'home': str(self.source)}, {'ui': str(self.source)},
                        {'home': str(self.clone/'codex-home/../codex-home')},
                        {'id': 'codex2/../../original'}):
            self.manager.data['profiles'][1] = {**original, **changes}
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.delete(id=self.manager.data['profiles'][1]['id'])
        self.assertTrue(self.clone.is_dir())

    def test_shared_profile_paths_cannot_be_archived(self):
        for other in (self.clone, self.clone/'codex-home/subdir', self.clone.parent):
            with self.subTest(other=other):
                self.manager.profile('codex1')['home'] = str(other)
                with self.assertRaisesRegex(RuntimeError, '重叠'):
                    self.delete()
        self.assertTrue(self.clone.is_dir())

    def test_managed_directory_symlinks_are_rejected(self):
        for component in ('codex-home', 'electron-data'):
            path = self.clone/component
            parked = self.clone/(component+'-real')
            path.rename(parked)
            path.symlink_to(parked, target_is_directory=True)
            try:
                with self.subTest(component=component), self.assertRaisesRegex(RuntimeError, '符号链接'):
                    self.delete()
            finally:
                path.unlink()
                parked.rename(path)
        parked = self.clone.with_name('codex2-real')
        self.clone.rename(parked)
        self.clone.symlink_to(parked, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, '符号链接'):
            self.delete()
        self.assertTrue((parked/'codex-home/history.jsonl').exists())

    def test_archive_parent_symlink_is_rejected(self):
        (self.runtime/'deleted-clones').symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, '符号链接'):
            self.delete()
        self.assertTrue(self.clone.is_dir())
        self.assertEqual(list(self.source.iterdir()), [])

    def test_embedded_symlink_is_moved_without_touching_target(self):
        sentinel = self.source/'untouched'
        sentinel.write_text('keep')
        (self.clone/'codex-home/external').symlink_to(sentinel)
        self.delete()
        self.assertEqual(sentinel.read_text(), 'keep')
        archive = next(p for p in (self.runtime/'deleted-clones').iterdir() if p.is_dir())
        self.assertTrue((archive/'codex-home/external').is_symlink())

    def test_move_failure_keeps_registry_and_data(self):
        before = copy.deepcopy(self.manager.data)
        with patch.object(Path, 'rename', side_effect=OSError('move failed')):
            with self.assertRaisesRegex(RuntimeError, '原始目录'):
                self.delete()
        self.assertEqual(self.manager.data, before)
        self.assertEqual(json.loads(manager_module.REGISTRY.read_text()), before)
        self.assertTrue(self.clone.is_dir())

    def test_registry_failure_rolls_back_data(self):
        before = copy.deepcopy(self.manager.data)
        with patch.object(self.manager, 'save', side_effect=OSError('write failed')):
            with self.assertRaisesRegex(RuntimeError, '删除未完成'):
                self.delete()
        self.assertEqual(self.manager.data, before)
        self.assertEqual(json.loads(manager_module.REGISTRY.read_text()), before)
        self.assertEqual((self.clone/'codex-home/history.jsonl').read_text(), 'original history')
        receipt = json.loads(next((self.runtime/'deleted-clones').glob('*.json')).read_text())
        self.assertEqual(receipt['status'], 'rolled_back')
        self.assertIn('codex2', self.manager.quota)

    def test_error_after_registry_replace_also_restores_original_registry(self):
        before = copy.deepcopy(self.manager.data)
        def late_failure():
            manager_module.private_json(manager_module.REGISTRY, self.manager.data)
            raise OSError('chmod failed after replace')
        with patch.object(self.manager, 'save', side_effect=late_failure):
            with self.assertRaisesRegex(RuntimeError, '删除未完成'):
                self.delete()
        self.assertEqual(json.loads(manager_module.REGISTRY.read_text()), before)
        self.assertTrue(self.clone.is_dir())

    def test_failed_rollback_leaves_data_and_recovery_receipt(self):
        rename = Path.rename
        def fail_rollback(source, destination):
            if source == self.clone:
                return rename(source, destination)
            raise OSError('rollback failed')
        with patch.object(self.manager, 'save', side_effect=OSError('write failed')), \
             patch.object(Path, 'rename', fail_rollback):
            with self.assertRaisesRegex(RuntimeError, '恢复记录'):
                self.delete()
        receipt = json.loads(next((self.runtime/'deleted-clones').glob('*.json')).read_text())
        self.assertEqual(receipt['status'], 'recovery_required')
        self.assertEqual((Path(receipt['archivedPath'])/'codex-home/history.jsonl').read_text(), 'original history')

    def test_legacy_desktop_b_can_only_be_removed_when_stopped(self):
        legacy = self.runtime/'desktop-b'
        self.clone.rename(legacy)
        self.manager.profile('codex2').update(home=str(legacy/'codex-home'), ui=str(legacy/'electron-data'))
        with patch.object(manager_module, 'process_map', return_value={'codex1': 111, 'codex2': 222}):
            with self.assertRaisesRegex(RuntimeError, '退出目标分身'):
                self.delete()
        self.assertTrue(legacy.is_dir())
        self.delete()
        self.assertFalse(legacy.exists())

    def test_deleted_number_is_not_reused_after_restart(self):
        self.delete()
        manager = manager_module.Manager()
        with patch.object(manager_module, 'seed_home', return_value={}), \
             patch('shadow_history.import_history', return_value={}), \
             patch.object(manager_module, 'merge_imported_sidebar'), patch.object(manager, 'open'):
            self.assertEqual(manager.create(), 'codex3')
        self.assertEqual(manager.data['next_profile_number'], 4)

    def test_new_profiles_use_short_physical_root(self):
        short_root = self.runtime/'short-root'
        with patch.object(manager_module, 'profile_storage_root', return_value=short_root), \
             patch.object(manager_module, 'seed_home', return_value={}), \
             patch('shadow_history.import_history', return_value={}), \
             patch.object(manager_module, 'merge_imported_sidebar'), patch.object(self.manager, 'open'):
            self.assertEqual(self.manager.create(), 'codex3')
        created = self.manager.profile('codex3')
        self.assertEqual(Path(created['home']), short_root/'codex3'/'codex-home')
        self.assertEqual(Path(created['ui']), short_root/'codex3'/'electron-data')
        self.assertFalse((self.runtime/'clones/codex3').exists())

    def test_old_registry_skips_existing_and_archived_directories(self):
        (self.runtime/'clones/codex8').mkdir()
        (self.runtime/'deleted-clones/codex12-fixture').mkdir(parents=True)
        self.assertEqual(self.manager.next_profile_number(), 13)
        self.manager.data['next_profile_number'] = 20
        self.assertEqual(self.manager.next_profile_number(), 20)

    def test_short_root_profile_deletes_to_short_root_archive(self):
        short = self.runtime/'short-root'
        short_clone = short/'codex2'
        short_clone.mkdir(parents=True)
        (short_clone/'codex-home').mkdir()
        (short_clone/'electron-data').mkdir()
        self.manager.profile('codex2').update(home=str(short_clone/'codex-home'),
                                              ui=str(short_clone/'electron-data'))
        with patch.object(manager_module, 'profile_storage_root', return_value=short):
            self.delete()
        receipt_path = next((short/'deleted-clones').glob('*.json'))
        self.assertTrue(Path(json.loads(receipt_path.read_text())['archivedPath']).is_dir())


class ManagerUpdateAndShutdownTests(unittest.TestCase):
    setUp = ManagerTests.setUp

    def test_update_check_is_available_while_quota_operation_is_busy(self):
        self.manager.operation.acquire()
        try:
            with patch.object(self.manager.updates, 'check_async') as check:
                self.manager.action({'action': 'check_updates'})
                check.assert_called_once_with(force=True)
        finally:
            self.manager.operation.release()

    def test_snapshot_includes_update_status(self):
        with patch.object(self.manager.updates, 'snapshot', return_value={'status': 'available'}) as snapshot:
            self.assertEqual(self.manager.snapshot()['update'], {'status': 'available'})
            snapshot.assert_called_once_with()

    def test_shutdown_requires_confirmation_without_taking_operation_lock(self):
        for confirmed in (None, False, 'true', 1):
            with self.subTest(confirmed=confirmed), self.assertRaisesRegex(RuntimeError, '确认'):
                self.manager.prepare_shutdown(confirmed)
        self.assertFalse(self.manager.operation.locked())

    def test_shutdown_rejects_busy_service(self):
        self.manager.operation.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, '等待完成'):
                self.manager.prepare_shutdown(True)
        finally:
            self.manager.operation.release()
        self.assertFalse(self.manager.working)

    def test_shutdown_reserves_operation_until_exit(self):
        self.manager.prepare_shutdown(True)
        self.assertTrue(self.manager.operation.locked())
        self.assertTrue(self.manager.working)
        with self.assertRaises(RuntimeError):
            self.manager.action({'action': 'open', 'id': 'codex2'})
        self.manager.operation.release()


if __name__ == '__main__':
    unittest.main()

class UnifiedHistoryManagerTests(unittest.TestCase):
    setUp = ManagerTests.setUp

    def test_unified_sync_includes_original_and_queues_running_instances(self):
        result = {'status': 'partial', 'profiles': {
            'codex1': {'pending': True, 'imported': 0},
            'codex2': {'pending': False, 'imported': 1}}}
        with patch('shadow_sync.sync_profiles', return_value=result) as sync:
            self.manager.unified_history()
        self.assertEqual([p['id'] for p in sync.call_args.args[0]], ['codex1','codex2'])
        self.assertTrue(self.manager.profile('codex1')['pending_history'])
        self.assertFalse(self.manager.profile('codex2')['pending_history'])
        self.assertEqual(self.manager.data['history_sync'], result)

    def test_pending_history_retries_only_after_target_exit(self):
        self.manager.profile('codex2')['pending_history'] = True
        with patch.object(self.manager, 'action') as action:
            self.manager.maybe_sync_history()
            action.assert_not_called()
            with patch.object(manager_module, 'process_map', return_value={'codex1':111,'codex2':None}):
                self.manager.maybe_sync_history()
            action.assert_called_once_with({'action':'history_all'})

    def test_automatic_sync_respects_interval(self):
        self.manager.data['auto_history'] = True
        with patch.object(self.manager, 'action') as action:
            self.manager.last_history_sync = NOW-299
            self.manager.maybe_sync_history()
            action.assert_not_called()
            self.manager.last_history_sync = NOW-300
            self.manager.maybe_sync_history()
            action.assert_called_once_with({'action':'history_all'})

    def test_sync_enabled_setting_requires_boolean(self):
        before = copy.deepcopy(self.manager.data)
        with self.assertRaises(ValueError):
            self.manager.action({'action':'settings','auto_switch':False,'threshold':10,'auto_history':'true'})
        self.assertEqual(before,self.manager.data)
