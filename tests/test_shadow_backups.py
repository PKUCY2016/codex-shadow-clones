from pathlib import Path
import tempfile
import unittest

from shadow_backups import prune_backups


class BackupRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'codex-home'
        self.root = self.home / '.shadow-backups'
        self.root.mkdir(parents=True)

    def backup(self, name, files):
        folder = self.root / name
        folder.mkdir()
        for filename, content in files.items():
            (folder / filename).write_bytes(content)
        return folder

    def test_keeps_latest_complete_database_and_latest_per_rollout(self):
        sqlite = b'SQLite format 3\x00' + b'x' * 100
        pair = {'state_5.sqlite': sqlite, 'thread_history_1.sqlite': sqlite}
        old_db = self.backup('history-1', pair)
        latest_db = self.backup('history-2', pair)
        incomplete = self.backup('history-3', {'state_5.sqlite': sqlite})
        old_a = self.backup('rollout-1', {'a.jsonl': b'old'})
        latest_a = self.backup('rollout-2', {'a.jsonl': b'new'})
        other_task = self.backup('rollout-3', {'b.jsonl': b'different task'})
        manual = self.backup('manual-4', {'notes': b'keep'})

        preview = prune_backups(self.home, dry_run=True)
        self.assertEqual(preview['deleted'], 2)
        self.assertTrue(old_db.exists())
        self.assertTrue(old_a.exists())

        applied = prune_backups(self.home)
        self.assertEqual(applied['deleted'], 2)
        self.assertFalse(old_db.exists())
        self.assertFalse(old_a.exists())
        for retained in (latest_db, incomplete, latest_a, other_task, manual):
            self.assertTrue(retained.exists())
        self.assertEqual(prune_backups(self.home)['deleted'], 0)

    def test_rejects_linked_backup_root(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        self.root.rmdir()
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'backup_path_symlink'):
            prune_backups(self.home)
        self.assertTrue(outside.exists())


if __name__ == '__main__':
    unittest.main()
