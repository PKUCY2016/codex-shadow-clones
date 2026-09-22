import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import shadow_history as h
from shadow_sync import sync_profiles


def record(kind, **extra):
    return (json.dumps({'type': 'event_msg', 'payload': {'type': kind, **extra}})+'\n').encode()


def home(path):
    path.mkdir()
    with h.connect(path/h.STATE) as c:
        c.executescript('CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT,source TEXT,updated_at INTEGER,title TEXT,name TEXT,is_pinned INTEGER,project_id TEXT,thread_section_id TEXT,section_position INTEGER,section_entered_at_ms INTEGER);CREATE TABLE thread_dynamic_tools(thread_id TEXT);')
    with h.connect(path/h.HISTORY) as c:
        c.executescript('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,rollout_ordinal INTEGER);CREATE TABLE thread_items(thread_id TEXT,rollout_ordinal INTEGER);CREATE TABLE thread_realtime_items(thread_id TEXT,rollout_ordinal INTEGER);CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);')
    return path


def task(directory, ident, text='first', base=None):
    path=directory/(ident+'.jsonl')
    metadata={'id':ident}
    if base: metadata['history_base']=base
    path.write_bytes((json.dumps({'type':'session_meta','payload':metadata})+'\n').encode()+record('task_started')+record('agent_message',text=text)+record('task_complete'))
    row={'id':ident,'rollout_path':str(path),'source':'vscode','updated_at':1,'title':ident,'name':None,'is_pinned':1,'project_id':None,'thread_section_id':None,'section_position':None,'section_entered_at_ms':None}
    with h.connect(directory/h.STATE) as c:h.insert(c,'threads',row)
    return path


def log(directory, ident):
    with h.connect(directory/h.STATE,True) as c:
        return Path(c.execute('SELECT rollout_path FROM threads WHERE id=?',(ident,)).fetchone()[0])


class UnifiedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.a=home(self.root/'a');self.b=home(self.root/'b');self.hub=self.root/'hub'
        self.profiles=[{'id':'a','codex_home':str(self.a)},{'id':'b','codex_home':str(self.b)}]
    def sync(self, running=lambda p:False):
        with patch.object(h,'HistoryRPC',side_effect=AssertionError('RPC must never start')):
            return sync_profiles(self.profiles,self.hub,running)
    def test_two_sources_distribute_and_second_pass_is_idempotent(self):
        task(self.a,'one');task(self.b,'two')
        result=self.sync()
        self.assertEqual(result['profiles']['a']['imported'],1)
        self.assertEqual(result['profiles']['b']['imported'],1)
        self.assertEqual(log(self.a,'two').read_bytes(),log(self.b,'two').read_bytes())
        again=self.sync()
        self.assertEqual(again['profiles']['a']['updated'],0)
        self.assertEqual(again['profiles']['b']['imported'],0)
    def test_running_target_is_unchanged_but_its_completed_history_exports(self):
        source=task(self.a,'one');task(self.b,'two')
        before={p.name:p.read_bytes() for p in self.a.iterdir() if p.is_file()}
        result=self.sync(lambda p:p['id']=='a')
        self.assertTrue(result['profiles']['a']['pending'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.a.iterdir() if p.is_file()})
        self.assertEqual(log(self.b,'one').read_bytes(),source.read_bytes())
    def test_latest_completed_prefix_propagates_from_clone_back_to_original(self):
        task(self.a,'one');self.sync()
        path=log(self.b,'one')
        with path.open('ab') as f:f.write(record('thread_settings_applied')+record('task_started')+record('agent_message',text='second')+record('task_complete')+record('task_started'))
        result=self.sync(lambda p:p['id']=='b')
        self.assertEqual(result['profiles']['a']['updated'],1)
        self.assertEqual(h.completed_prefix(log(self.a,'one'))['completed_events'],2)
        self.assertTrue(log(self.a,'one').read_bytes().endswith(record('task_complete')))
    def test_divergence_keeps_both_originals_and_archives(self):
        task(self.a,'one');self.sync()
        for directory,text in ((self.a,'a'),(self.b,'b')):
            with log(directory,'one').open('ab') as f:f.write(record('task_started')+record('agent_message',text=text)+record('task_complete'))
        before=[log(p,'one').read_bytes() for p in (self.a,self.b)]
        result=self.sync()
        self.assertEqual(result['profiles']['a']['branches'],1)
        self.assertEqual(result['profiles']['b']['branches'],1)
        self.assertEqual(before,[log(p,'one').read_bytes() for p in (self.a,self.b)])
        self.assertGreaterEqual(len(list(self.hub.rglob('*.jsonl'))),3)
    def test_branches_are_visible_idempotent_and_propagate_to_third_profile(self):
        task(self.a,'one');self.sync()
        for directory,text in ((self.a,'a'),(self.b,'b')):
            with log(directory,'one').open('ab') as f:
                f.write(record('task_started')+record('agent_message',text=text)+record('task_complete'))
        first=self.sync()
        self.assertEqual(first['profiles']['a']['branches'],1)
        again=self.sync()
        self.assertEqual(again['profiles']['a']['imported'],0)
        self.assertEqual(again['profiles']['b']['imported'],0)
        third=home(self.root/'c')
        self.profiles.append({'id':'c','codex_home':str(third)})
        result=self.sync()
        self.assertEqual(result['profiles']['c']['imported'],2)
        with h.connect(third/h.STATE,True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM threads').fetchone()[0],2)
        with log(self.a,'one').open('ab') as f:
            f.write(record('task_started')+record('agent_message',text='new a')+record('task_complete'))
        updated=self.sync()
        self.assertEqual(updated['profiles']['b']['updated'],1)
        self.assertEqual(updated['profiles']['b']['imported'],0)
    def test_branch_rewrite_does_not_change_uuid_in_user_text(self):
        task(self.a,'one');self.sync()
        with log(self.a,'one').open('ab') as f:
            f.write(record('task_started')+record('agent_message',text='one')+record('task_complete'))
        with log(self.b,'one').open('ab') as f:
            f.write(record('task_started')+record('agent_message',text='other')+record('task_complete'))
        self.sync()
        with h.connect(self.b/h.STATE,True) as db:
            row=db.execute("SELECT * FROM threads WHERE id!='one'").fetchone()
        events=[json.loads(line) for line in Path(row['rollout_path']).read_bytes().splitlines()]
        self.assertEqual(events[0]['payload']['id'],row['id'])
        self.assertIn('one',[x.get('payload',{}).get('text') for x in events])
    def test_alias_fast_forward_keeps_other_source_branch_tools_for_later_target(self):
        for directory in (self.a, self.b):
            with h.connect(directory/h.STATE) as db:
                db.execute('ALTER TABLE thread_dynamic_tools ADD COLUMN name TEXT')
        task(self.a, 'one')
        self.sync()
        for directory, label in ((self.a, 'A'), (self.b, 'B')):
            with log(directory, 'one').open('ab') as stream:
                stream.write(record('task_started')+record('agent_message', text=label)+record('task_complete'))
            with h.connect(directory/h.STATE) as db:
                db.execute('INSERT INTO thread_dynamic_tools VALUES(?,?)', ('one', 'tool_'+label))
        # A has not received B's branch yet. B continues its imported A alias.
        self.sync(lambda profile: profile['id'] == 'a')
        with h.connect(self.b/h.STATE, True) as db:
            alias_a = db.execute("SELECT id FROM threads WHERE id!='one'").fetchone()[0]
        with log(self.b, alias_a).open('ab') as stream:
            stream.write(record('task_started')+record('agent_message', text='A continued in B')+record('task_complete'))
        third = home(self.root/'c')
        with h.connect(third/h.STATE) as db:
            db.execute('ALTER TABLE thread_dynamic_tools ADD COLUMN name TEXT')
        self.profiles.append({'id': 'c', 'codex_home': str(third)})
        result = self.sync()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['profiles']['a']['updated'], 1)
        # Rewriting B's A alias back to A's root UUID must not replace the
        # separate B candidate's tool definitions before C consumes it.
        with h.connect(third/h.STATE, True) as db:
            rows = list(db.execute('SELECT id,rollout_path FROM threads'))
            self.assertEqual(len(rows), 2)
            for row in rows:
                events = [json.loads(line) for line in Path(row['rollout_path']).read_bytes().splitlines()]
                messages = [event['payload']['text'] for event in events
                            if event.get('payload', {}).get('type') == 'agent_message']
                expected = 'tool_B' if messages[-1] == 'B' else 'tool_A'
                actual = [item[0] for item in db.execute(
                    'SELECT name FROM thread_dynamic_tools WHERE thread_id=?', (row['id'],))]
                self.assertEqual(actual, [expected], messages)
    def test_same_batch_alias_rewrite_refreshes_content_and_clears_projection(self):
        from shadow_sync import _branch_row, relation
        source=task(self.a,'one')
        with h.connect(self.a/h.STATE,True) as db:
            row=dict(db.execute("SELECT * FROM threads WHERE id='one'").fetchone())
        paths=[]
        for i in range(3):
            with source.open('ab') as stream:
                stream.write(record('task_started')+record('agent_message',text=str(i))+record('task_complete'))
            with h.connect(self.a/h.HISTORY) as db:
                db.execute("INSERT INTO thread_history_projection_state VALUES('alias',999,999)")
            alias=_branch_row(self.a,row,self.a,'alias','source')
            paths.append(alias['rollout_path'])
            self.assertEqual(relation(alias['rollout_path'],source),'equal')
            self.assertEqual(h.completed_prefix(alias['rollout_path'])['completed_events'],i+2)
            with h.connect(self.a/h.STATE,True) as db:
                self.assertEqual(db.execute("SELECT rollout_path FROM threads WHERE id='alias'").fetchone()[0],alias['rollout_path'])
            with h.connect(self.a/h.HISTORY,True) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM thread_history_projection_state WHERE thread_id='alias'").fetchone()[0],0)
        self.assertEqual(len(set(paths)),3)
    def test_relation_byte_prefix_fast_path_avoids_json_parsing(self):
        from shadow_sync import relation
        a=self.root/'fast-a';b=self.root/'fast-b'
        prefix=record('agent_message',text='x'*1100000)
        a.write_bytes(prefix);b.write_bytes(prefix)
        with patch('shadow_sync._events',side_effect=AssertionError('unnecessary JSON parse')):
            self.assertEqual(relation(a,b),'equal')
            with b.open('ab') as stream:stream.write(record('task_complete'))
            self.assertEqual(relation(a,b),'behind')
            self.assertEqual(relation(b,a),'ahead')
    def test_relation_byte_difference_falls_back_to_canonical_events(self):
        from shadow_sync import relation
        a=self.root/'canon-a';b=self.root/'canon-b'
        meta={'type':'session_meta','payload':{'id':'fixture'}}
        a.write_text(json.dumps(meta)+'\n')
        b.write_text(json.dumps(meta,separators=(',',':'))+'\n')
        self.assertEqual(relation(a,b),'equal')
    def test_null_in_structured_tool_results_preserves_divergent_history(self):
        from shadow_sync import relation
        for directory,structured in ((self.a,{'measurement':None}),(self.b,{})):
            path=task(directory,'one')
            events=path.read_bytes().splitlines(keepends=True)
            event=record('mcp_tool_call_end',call_id='call_1',
                invocation={'server':'fixture','tool':'measurement','arguments':{}},
                result={'Ok':{'content':[],'structuredContent':structured}})
            path.write_bytes(b''.join(events[:-1])+event+events[-1])
        before=log(self.b,'one').read_bytes()
        self.assertEqual(relation(log(self.a,'one'),log(self.b,'one')),'diverged')
        with log(self.a,'one').open('ab') as stream:
            stream.write(record('task_started')+record('task_complete'))
        result=self.sync()
        self.assertEqual(result['profiles']['b']['updated'],0)
        self.assertEqual(result['profiles']['b']['branches'],1)
        self.assertEqual(log(self.b,'one').read_bytes(),before)
    def test_native_storage_defaults_do_not_create_false_branches(self):
        from shadow_sync import relation, _write_lineage
        a=self.root/'before.jsonl';b=self.root/'after.jsonl'
        original=[{'type':'session_meta','payload':{'id':'original','history_mode':'legacy'}},
                  {'type':'response_item','payload':{'type':'message','role':'assistant',
                    'content':[{'type':'output_text','text':'Keep null and original in user text'}],
                    'internal_chat_message_metadata_passthrough':{'opaque':True}}},
                  {'type':'compacted','payload':{'message':'summary','guardian_history':[]}}]
        migrated=json.loads(json.dumps(original))
        migrated[0]['payload']={'id':'alias','history_mode':'paginated','session_id':'alias'}
        migrated[1]['payload'].pop('internal_chat_message_metadata_passthrough')
        migrated[1]['payload']['phase']=None
        migrated[2]['payload'].pop('guardian_history')
        def write(path,events):path.write_text(''.join(json.dumps(x)+'\n' for x in events))
        write(a,original);write(b,migrated);_write_lineage(b,'alias','original')
        self.assertEqual(relation(a,b),'equal')
        migrated[1]['payload']['content'][0]['text']='A different user-visible message'
        write(b,migrated)
        self.assertEqual(relation(a,b),'diverged')
    def test_installed_rollout_names_are_canonical_for_originals_and_branches(self):
        from shadow_sync import _rollout_name
        import re
        ident='11111111-1111-4111-8111-111111111111'
        task(self.a,ident);self.sync()
        self.assertEqual(_rollout_name({'id':ident,'created_at':1}),
                         'rollout-1970-01-01T00-00-01-'+ident+'.jsonl')
        for directory,text in ((self.a,'a'),(self.b,'b')):
            with log(directory,ident).open('ab') as stream:
                stream.write(record('task_started')+record('agent_message',text=text)+record('task_complete'))
        self.sync()
        with h.connect(self.b/h.STATE,True) as db:
            rows=list(db.execute('SELECT id,rollout_path FROM threads'))
        self.assertEqual(len(rows),2)
        for row in rows:
            self.assertRegex(Path(row['rollout_path']).name,
                r'^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-'+re.escape(row['id'])+r'\.jsonl$')
    def test_branch_with_official_schema_uses_staged_offline_migration(self):
        from shadow_sync import _branch_row
        task(self.a,'one')
        with h.connect(self.a/h.STATE) as db:
            db.execute("ALTER TABLE threads ADD COLUMN history_mode TEXT DEFAULT 'paginated'")
            row=dict(db.execute("SELECT * FROM threads WHERE id='one'").fetchone())
        marker={'id':'alias','history_mode':'paginated','rollout_path':str(self.a/'mock-migrated.jsonl')}
        with patch('shadow_sync._migrate_branch',return_value=marker) as migrate:
            self.assertEqual(_branch_row(self.a,row,self.a,'alias','source'),marker)
        migrate.assert_called_once_with(self.a,'alias')
        with h.connect(self.a/h.STATE,True) as db:
            staged=db.execute("SELECT history_mode,rollout_path FROM threads WHERE id='alias'").fetchone()
            self.assertEqual(staged['history_mode'],'legacy')
            self.assertTrue(Path(staged['rollout_path']).is_relative_to(self.a/'sessions'))
    def test_offline_migration_environment_and_output_validation(self):
        import os
        from types import SimpleNamespace
        from shadow_sync import _migrate_branch
        task(self.a,'one')
        with h.connect(self.a/h.STATE) as db:
            db.execute("ALTER TABLE threads ADD COLUMN history_mode TEXT DEFAULT 'paginated'")
        with h.connect(self.a/h.HISTORY) as db:
            db.execute("INSERT INTO thread_turns VALUES('one','turn','completed',2)")
        with patch.dict(os.environ,{'OPENAI_API_KEY':'secret','CODEX_SQLITE_HOME':'elsewhere'}), patch('shadow_sync.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(_migrate_branch(self.a,'one')['id'],'one')
        kwargs=run.call_args.kwargs
        self.assertEqual(kwargs['env']['CODEX_HOME'],str(self.a.resolve()))
        self.assertNotIn('OPENAI_API_KEY',kwargs['env'])
        self.assertNotIn('CODEX_SQLITE_HOME',kwargs['env'])
        self.assertEqual(run.call_args.args[0][1:],['migrate-rollouts','--apply','--thread','one','--json'])
        with patch('shadow_sync.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError,'migration failed'):_migrate_branch(self.a,'one')
    def test_native_migration_removing_metadata_keeps_lineage_and_dedup(self):
        from shadow_sync import _root, relation
        for directory in (self.a,self.b):
            with h.connect(directory/h.STATE) as db:
                db.execute("ALTER TABLE threads ADD COLUMN history_mode TEXT DEFAULT 'paginated'")
        original=task(self.a,'one')
        lines=original.read_text().splitlines()
        meta=json.loads(lines[0]);meta['payload'].update(session_id='one',history_mode='paginated')
        original.write_text(json.dumps(meta)+'\n'+'\n'.join(lines[1:])+'\n')
        self.sync()
        for directory,text in ((self.a,'a'),(self.b,'b')):
            with log(directory,'one').open('ab') as stream:
                stream.write(record('task_started')+record('agent_message',text=text)+record('task_complete'))
        def migrate(directory,ident):
            with h.connect(directory/h.STATE) as db:
                row=dict(db.execute('SELECT * FROM threads WHERE id=?',(ident,)).fetchone())
                db.execute("UPDATE threads SET history_mode='paginated' WHERE id=?",(ident,))
            path=Path(row['rollout_path']);lines=path.read_text().splitlines()
            meta=json.loads(lines[0]);self.assertEqual(meta['payload']['history_mode'],'legacy')
            self.assertEqual(meta['payload']['session_id'],ident)
            meta['payload'].pop('shadow_sync',None)
            meta['payload']['history_mode']='paginated';meta['payload']['storage_default']=None
            path.write_text(json.dumps(meta)+'\n'+'\n'.join(lines[1:])+'\n')
            row['history_mode']='paginated';return row
        with patch('shadow_sync._migrate_branch',side_effect=migrate):
            first=self.sync()
            self.assertEqual(first['profiles']['b']['branches'],1)
            with h.connect(self.b/h.STATE,True) as db:
                alias=dict(db.execute("SELECT * FROM threads WHERE id!='one'").fetchone())
            self.assertEqual(_root(alias['rollout_path']),'one')
            self.assertEqual(relation(alias['rollout_path'],log(self.a,'one')),'equal')
            again=self.sync()
            self.assertEqual(again['profiles']['a']['imported'],0)
            self.assertEqual(again['profiles']['b']['imported'],0)
            self.assertEqual(self.sync()['profiles']['b']['imported'],0)
    def test_paginated_logs_are_archived_but_not_installed(self):
        task(self.a,'one',base={'thread_id':'one','end_byte_offset':80,'end_ordinal_exclusive':2})
        result=self.sync()
        self.assertEqual(result['profiles']['b']['unsupported'],1)
        with h.connect(self.b/h.STATE,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM threads').fetchone()[0],0)
    def test_profile_starting_mid_sync_prevents_writes(self):
        task(self.a,'one');task(self.b,'two')
        checks={'b':0}
        def running(p):
            if p['id']=='a':return True
            checks['b']+=1
            return checks['b']>=3
        result=self.sync(running)
        self.assertTrue(result['profiles']['b']['pending'])
        with h.connect(self.b/h.STATE,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM threads').fetchone()[0],1)
    def test_archives_are_bounded_and_unchanged_logs_reused(self):
        task(self.a,'one')
        self.sync(lambda p: True)
        with patch.object(h, 'frozen_rollout', wraps=h.frozen_rollout) as freeze:
            self.sync(lambda p: True)
            self.assertEqual(freeze.call_count, 0)
        self.sync(lambda p: True)
        self.assertEqual(len(list(self.hub.glob('snapshot-*'))), 2)
    def test_subagent_logs_are_not_archived(self):
        task(self.a,'child')
        with h.connect(self.a/h.STATE) as c:
            c.execute("UPDATE threads SET source='subagent' WHERE id='child'")
        result=self.sync()
        self.assertEqual(result['archived'],0)
        self.assertEqual(list(self.hub.rglob('*.jsonl')),[])
    def test_unfinished_destination_tail_is_preserved(self):
        task(self.a,'one');self.sync()
        with log(self.a,'one').open('ab') as f:f.write(record('task_started')+record('task_complete'))
        with log(self.b,'one').open('ab') as f:f.write(record('task_started'))
        before=log(self.b,'one').read_bytes()
        result=self.sync()
        self.assertEqual(result['profiles']['b']['branches'],1)
        self.assertEqual(log(self.b,'one').read_bytes(),before)

if __name__=='__main__':unittest.main()
