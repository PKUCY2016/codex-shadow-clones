import tempfile
from pathlib import Path
import unittest
from shadow_history import frozen_rollout,validate_parent_prefix


class HistorySnapshotTest(unittest.TestCase):
    def test_snapshot_is_independent_and_excludes_incomplete_final_record(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source';target=Path(d)/'target'
            source.write_bytes(b'{"event":1}\n{"incomplete":')
            frozen_rollout(source,target)
            self.assertEqual(target.read_bytes(),b'{"event":1}\n')
            self.assertNotEqual(source.stat().st_ino,target.stat().st_ino)
            with source.open('ab') as f:f.write(b'2}\n')
            self.assertEqual(target.read_bytes(),b'{"event":1}\n')
            target.write_bytes(b'independent continuation\n')
            self.assertTrue(source.read_bytes().endswith(b'2}\n'))

    def test_later_child_cannot_reference_beyond_existing_parent_snapshot(self):
        # The source parent may have advanced, but the already imported parent
        # must retain its earlier frozen prefix and its independent continuation.
        captured={'parent':(100,10)}
        validate_parent_prefix({'thread_id':'parent','end_byte_offset':100},'parent',captured)
        with self.assertRaisesRegex(RuntimeError,'shorter than fork dependency'):
            validate_parent_prefix({'thread_id':'parent','end_byte_offset':200},'parent',captured)

if __name__=='__main__':unittest.main()


class RefreshRegressionTest(unittest.TestCase):
    def make_homes(self,d):
        import json,sqlite3
        import shadow_history as h
        source,target=Path(d).resolve()/'source',Path(d).resolve()/'target'
        source.mkdir();target.mkdir()
        for home in (source,target):
            with h.connect(home/h.STATE) as c:
                c.executescript('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT,updated_at INTEGER,title TEXT,name TEXT,is_pinned INTEGER,project_id TEXT,thread_section_id TEXT,section_position INTEGER,section_entered_at_ms INTEGER); CREATE TABLE thread_dynamic_tools(thread_id TEXT); CREATE TABLE shadow_history_imports(source_home TEXT,source_id TEXT,target_id TEXT,imported_at INTEGER,end_byte_offset INTEGER,end_ordinal INTEGER,PRIMARY KEY(source_home,source_id));')
            with h.connect(home/h.HISTORY) as c:
                c.executescript('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,rollout_ordinal INTEGER);CREATE TABLE thread_items(thread_id TEXT,rollout_ordinal INTEGER);CREATE TABLE thread_realtime_items(thread_id TEXT,rollout_ordinal INTEGER);CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);')
        log=source/'rollout-fixture.jsonl'
        record=lambda kind:json.dumps({'type':'event_msg','payload':{'type':kind}}).encode()+b'\n'
        log.write_bytes(json.dumps({'type':'session_meta','payload':{'id':'fixture'}}).encode()+b'\n'+record('task_started')+record('task_complete'))
        row={'id':'fixture','rollout_path':str(log),'source':'vscode','updated_at':1,'title':'Fixture','name':None,'is_pinned':0,'project_id':None,'thread_section_id':None,'section_position':None,'section_entered_at_ms':None}
        with h.connect(source/h.STATE) as c:h.insert(c,'threads',row)
        h._import_one(row,target,source/h.STATE,source/h.HISTORY,None,source)
        return source,target,log,record

    def test_refresh_copies_new_completed_turn_despite_stale_projection(self):
        import shadow_history as h
        with tempfile.TemporaryDirectory() as d:
            source,target,log,record=self.make_homes(d)
            old=(target/'sessions/shadow-imports'/log.name).stat().st_size
            with log.open('ab') as f:f.write(record('task_started')+record('task_complete')+record('task_started'))
            result=h.repair_history(source,target)
            snapshot=target/'sessions/shadow-imports'/log.name
            self.assertEqual(result['repaired'],1)
            self.assertGreater(snapshot.stat().st_size,old)
            self.assertEqual(h.completed_prefix(snapshot)['completed_events'],2)
            self.assertTrue(snapshot.read_bytes().endswith(record('task_complete')))

    def test_refresh_preserves_independent_destination_continuation(self):
        import shadow_history as h
        with tempfile.TemporaryDirectory() as d:
            source,target,log,record=self.make_homes(d)
            snapshot=target/'sessions/shadow-imports'/log.name
            with snapshot.open('ab') as f:f.write(record('task_started')+b'{"independent":true}\n')
            before=snapshot.read_bytes()
            with log.open('ab') as f:f.write(record('task_started')+record('task_complete'))
            result=h.repair_history(source,target)
            self.assertEqual(result['conflicts'],1)
            self.assertEqual(result['repaired'],0)
            self.assertEqual(snapshot.read_bytes(),before)

    def test_refresh_ignores_desktop_settings_tail_with_and_without_baseline(self):
        import shadow_history as h
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as d:
                source,target,log,record=self.make_homes(d)
                snapshot=target/'sessions/shadow-imports'/log.name
                with snapshot.open('ab') as f:f.write(record('thread_settings_applied'))
                if legacy:
                    with h.connect(target/h.STATE) as c:c.execute('DROP TABLE shadow_history_files')
                with log.open('ab') as f:f.write(record('task_started')+record('task_complete'))
                result=h.repair_history(source,target)
                self.assertEqual(result['repaired'],1)
                self.assertEqual(result['conflicts'],0)
                self.assertEqual(h.completed_prefix(snapshot)['completed_events'],2)

    def test_refresh_preserves_destination_sidebar_membership_and_order(self):
        import shadow_history as h
        with tempfile.TemporaryDirectory() as d:
            source,target,log,record=self.make_homes(d)
            with h.connect(target/h.STATE) as c:
                c.execute("UPDATE threads SET project_id='local-project', thread_section_id='pinned-section', section_position=2, section_entered_at_ms=123, is_pinned=1 WHERE id='fixture'")
            with log.open('ab') as f:f.write(record('task_started')+record('task_complete'))
            result=h.repair_history(source,target)
            self.assertEqual(result['repaired'],1)
            with h.connect(target/h.STATE) as c:
                row=c.execute("SELECT project_id,thread_section_id,section_position,section_entered_at_ms,is_pinned FROM threads WHERE id='fixture'").fetchone()
            self.assertEqual(tuple(row),('local-project','pinned-section',2,123,1))
