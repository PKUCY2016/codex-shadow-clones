import unittest
from copy import deepcopy
from shadow_history_state import merge_history_state


class HistoryStateTest(unittest.TestCase):
    def test_remaps_projects_and_preserves_destination_without_execution_state(self):
        source = {
            'local-projects': {'src-p': {'rootPaths':['/work/project']}},
            'thread-project-assignments': {
                'old': {'projectKind':'local','projectId':'src-p','pendingCoreUpdate':True},
                'remote': {'projectKind':'remote','projectId':'remote-project','hostId':'secret-host'},
                'absent': {'projectKind':'local','projectId':'src-p'}},
            'sidebar-project-thread-orders': {'src-p':{'threadIds':['old','absent'],'sortKey':'source-sort'}},
            'pinned-thread-ids':['old','absent'],
            'projectless-thread-ids':['old','loose','absent'],
            'thread-workspace-root-hints':{'old':'/work/project','absent':'/private/unused'},
            'queued-follow-ups':{'old':'NEVER'}, 'thread-writable-roots':{'old':['/']},
            'account-id':'NEVER', 'active-thread-id':'old'}
        target = {
            'local-projects': {'dest-p': {'rootPaths':['/work/project']}},
            'thread-project-assignments': {'owned':{'projectKind':'local','projectId':'dest-p'}},
            'sidebar-project-thread-orders':{'dest-p':{'threadIds':['owned'],'sortKey':'own-sort'}},
            'pinned-thread-ids':['owned'], 'account-id':'OWN'}
        original_source, original_target = deepcopy(source), deepcopy(target)
        result = merge_history_state(source,target,{'old':'new','remote':'new-remote','loose':'new-loose'})
        self.assertEqual(result['thread-project-assignments'],{
            'owned':{'projectKind':'local','projectId':'dest-p'},
            'new':{'projectKind':'local','projectId':'dest-p'}})
        self.assertEqual(result['sidebar-project-thread-orders']['dest-p'],
                         {'threadIds':['owned','new'],'sortKey':'own-sort'})
        self.assertEqual(result['pinned-thread-ids'],['owned','new'])
        self.assertEqual(result['projectless-thread-ids'],['new-loose'])
        self.assertEqual(result['thread-workspace-root-hints'],{'new':'/work/project'})
        self.assertEqual(result['account-id'],'OWN')
        for key in ('queued-follow-ups','thread-writable-roots','active-thread-id'):
            self.assertNotIn(key,result)
        self.assertEqual(source,original_source)
        self.assertEqual(target,original_target)
        self.assertEqual(merge_history_state(source,result,{'old':'new','remote':'new-remote','loose':'new-loose'}), result)

    def test_existing_assignment_wins_and_unmatched_project_is_skipped(self):
        source = {'local-projects':{'s':{'rootPaths':['/a']}},
                  'thread-project-assignments':{'old':{'projectKind':'local','projectId':'s'},
                                                'missing':{'projectKind':'local','projectId':'unknown'}}}
        target = {'local-projects':{'d':{'rootPaths':['/a']}},
                  'thread-project-assignments':{'new':{'projectKind':'local','projectId':'own'}}}
        result = merge_history_state(source,target,{'old':'new','missing':'new-missing'})
        self.assertEqual(result['thread-project-assignments'],target['thread-project-assignments'])


if __name__ == '__main__':
    unittest.main()

class SyncedProjectTests(unittest.TestCase):
    def test_project_roots_match_without_changing_existing_pins_or_assignments(self):
        import tempfile,json,sqlite3
        from pathlib import Path
        from shadow_history_state import assign_synced_projects
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);db=sqlite3.connect(home/'state_5.sqlite')
            db.executescript("CREATE TABLE projects(id TEXT,name TEXT,metadata TEXT,position INTEGER);CREATE TABLE project_roots(project_id TEXT,path TEXT,position INTEGER);CREATE TABLE threads(id TEXT,cwd TEXT,project_id TEXT,is_pinned INTEGER);INSERT INTO projects VALUES('core','Work','{}',0);INSERT INTO project_roots VALUES('core','/work',0);INSERT INTO threads VALUES('new','/work/src',NULL,0);INSERT INTO threads VALUES('own','/work/src','other',1);")
            db.commit();db.close()
            state={'local-projects':{'local':{'rootPaths':['/work']}},'pinned-thread-ids':['own']}
            path=home/'.codex-global-state.json';path.write_text(json.dumps(state))
            self.assertEqual(assign_synced_projects(home,['new','own'],lambda:False),0)
            self.assertEqual(assign_synced_projects(home,['new','own'],lambda:True),1)
            result=json.loads(path.read_text())
            self.assertEqual(result['pinned-thread-ids'],['own'])
            self.assertEqual(result['thread-project-assignments']['new']['projectId'],'local')
            db=sqlite3.connect(home/'state_5.sqlite')
            self.assertEqual(db.execute("SELECT project_id,is_pinned FROM threads WHERE id='own'").fetchone(),('other',1));db.close()
