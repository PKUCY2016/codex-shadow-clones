import json
from pathlib import Path
import sqlite3
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import shadow_seed


class FakeRPC:
    entries = []
    def __init__(self,home): pass
    def call(self,method,params):
        if method == 'project/list':
            return {'data':self.entries[:],'nextCursor':None}
        self.entries.append(params)
        return {}
    def close(self): pass


class SeedTest(unittest.TestCase):
    def test_keeps_independent_login_and_projects_and_has_no_history_copy(self):
        with tempfile.TemporaryDirectory() as d:
            source,target = Path(d)/'source',Path(d)/'target'
            source.mkdir(); target.mkdir()
            (source/'config.toml').write_text('sqlite_home = "/private/source-db"\ncli_auth_credentials_store = "keyring"\nmodel = "example"\n[desktop]\ncodeFontSize = 14\n')
            (target/'config.toml').write_text('model = "before"\n')
            for name in ('auth.json','installation_id'):
                (source/name).write_text('SOURCE')
                (target/name).write_text('DESTINATION')
            (source/'skills/example').mkdir(parents=True)
            (source/'skills/example/SKILL.md').write_text('Example')
            (source/'sessions').mkdir()
            (source/'sessions/private.jsonl').write_text('Do not copy')
            (source/'.codex-global-state.json').write_text(json.dumps({'electron-mac-push-deregistration-token':'SECRET','electron-persisted-atom-state':{'sidebar-width':321,'prompt-history':{'x':'secret'}}}))
            db=sqlite3.connect(source/'state_5.sqlite')
            db.executescript("CREATE TABLE projects(id TEXT,name TEXT,metadata TEXT,position INTEGER); CREATE TABLE project_roots(project_id TEXT,path TEXT,position INTEGER); INSERT INTO projects VALUES('p','Fixture','{}',0); INSERT INTO project_roots VALUES('p','/tmp/workspace',0);")
            db.commit(); db.close()
            FakeRPC.entries=[{'roots':[{'path':'/tmp/existing'}]}]
            with patch.object(shadow_seed,'ProjectRPC',FakeRPC):
                first=shadow_seed.seed_home(source,target)
                second=shadow_seed.seed_home(source,target)
            self.assertEqual(first['projects_added'],1)
            self.assertEqual(second['projects_added'],0)
            self.assertEqual(len(FakeRPC.entries),2)
            for name in ('auth.json','installation_id'):
                self.assertEqual((target/name).read_text(),'DESTINATION')
            self.assertFalse((target/'sessions').exists())
            self.assertFalse((target/'state_5.sqlite').exists())
            conf=tomllib.loads((target/'config.toml').read_text())
            self.assertNotIn('sqlite_home',conf)
            self.assertEqual(conf['cli_auth_credentials_store'],'file')
            self.assertEqual(conf['desktop']['codeFontSize'],14)
            state=json.loads((target/'.codex-global-state.json').read_text())
            self.assertEqual(state['electron-persisted-atom-state'],{'sidebar-width':321})
            self.assertNotIn('electron-mac-push-deregistration-token',state)
            self.assertTrue(list((target/'.shadow-backups').glob('*/config.toml')))

    def test_desktop_cache_is_seeded_and_linked_to_core_without_overwriting_target(self):
        project = lambda ident, path, name: {'id':ident, 'name':name,
            'rootPaths':[path], 'createdAt':1, 'updatedAt':2, 'secret':'must not copy'}
        source = {'local-projects': {'old': project('old','/work/a','A'),
                                   'new': project('new','/work/b','B')},
                  'project-order':['new','old'],
                  'selected-project':{'type':'local','projectId':'new'}}
        target = {'local-projects': {'own':project('own','/work/a','Own label')},
                  'project-order':['own'], 'auth-marker':'preserve'}
        server = [{'id':'core-a','roots':[{'path':'/work/a'}]},
                  {'id':'core-b','roots':[{'path':'/work/b'}]}]
        home = Path('/tmp/independent-home')
        self.assertEqual(shadow_seed.merge_project_state(source,target,server,home),1)
        self.assertEqual(shadow_seed.merge_project_state(source,target,server,home),0)
        self.assertEqual(set(target['local-projects']), {'own','new'})
        self.assertEqual(target['local-projects']['own']['name'],'Own label')
        self.assertNotIn('secret',target['local-projects']['new'])
        self.assertEqual(target['project-order'],['own','new'])
        self.assertEqual(target['auth-marker'],'preserve')
        self.assertNotIn('selected-project',target)
        mapping = target['app-server-project-id-by-legacy-project-id-by-host']['local:'+str(home.resolve())]
        self.assertEqual(mapping,{'own':'core-a','new':'core-b'})

    def test_seed_copies_memories_and_pauses_automation_templates(self):
        with tempfile.TemporaryDirectory() as d:
            source, target = Path(d)/'source', Path(d)/'target'
            source.mkdir(); target.mkdir()
            (source/'memories/rollout_summaries').mkdir(parents=True)
            (source/'memories/MEMORY.md').write_text('local memory')
            (source/'memories/rollout_summaries/run.md').write_text('summary')
            automation = source/'automations/demo/automation.toml'
            automation.parent.mkdir(parents=True)
            automation.write_text('status = "ACTIVE"\nname = "Demo"\nprompt = "keep"\n')
            FakeRPC.entries=[]
            with patch.object(shadow_seed,'ProjectRPC',FakeRPC):
                result=shadow_seed.seed_home(source,target)
            self.assertEqual(result['memories_added'],2)
            self.assertEqual(result['automations_added'],1)
            self.assertEqual((target/'memories/MEMORY.md').read_text(),'local memory')
            copied=tomllib.loads((target/'automations/demo/automation.toml').read_text())
            self.assertEqual(copied['status'],'PAUSED')
            self.assertEqual(copied['prompt'],'keep')

    def test_seed_drops_only_desktop_owned_marketplace_registration(self):
        source = '''model = "example"
[marketplaces."openai-bundled"]
source_type = "local"
source = "/parent/home/.tmp/bundled-marketplaces/openai-bundled"
[marketplaces.community]
source_type = "git"
source = "https://example.invalid/team/plugins"
[plugins."unified-computer-use@openai-bundled"]
enabled = true
[plugins."team-tool@community"]
enabled = true
[mcp_servers.team]
command = "team-tool"
'''
        result = tomllib.loads(shadow_seed.safe_config(source))
        self.assertNotIn('openai-bundled',result['marketplaces'])
        self.assertEqual(result['marketplaces']['community'],tomllib.loads(source)['marketplaces']['community'])
        self.assertEqual(result['plugins'],tomllib.loads(source)['plugins'])
        self.assertEqual(result['mcp_servers'],tomllib.loads(source)['mcp_servers'])

    def test_marketplace_header_inside_multiline_value_is_not_removed(self):
        source = '''notes = """
[marketplaces.openai-bundled]
this is a note, not configuration
"""
[marketplaces.openai-bundled]
source_type = "local"
source = "/parent/runtime"
[desktop]
codeFontSize = 14
'''
        result = tomllib.loads(shadow_seed.safe_config(source))
        self.assertEqual(result['notes'],tomllib.loads(source)['notes'])
        self.assertNotIn('openai-bundled',result.get('marketplaces',{}))
        self.assertEqual(result['desktop']['codeFontSize'],14)

    def test_same_home_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                shadow_seed.seed_home(Path(d),Path(d))

if __name__=='__main__':unittest.main()
