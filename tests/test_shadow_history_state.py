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
