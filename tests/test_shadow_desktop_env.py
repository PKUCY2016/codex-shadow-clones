import os
import unittest
from unittest.mock import patch
import shadow_clones


class DesktopEnvironmentTests(unittest.TestCase):
    def test_launch_does_not_borrow_parent_desktop_bridge_or_task_identity(self):
        parent = {'CODEX_APP_TOOLS_PIPE_PATH':'/tmp/parent-desktop.sock',
                  'CODEX_THREAD_ID':'parent-task','CODEX_SESSION_ID':'parent-session',
                  'NODE_REPL_HOST_SERVICES_PIPE_PATH':'/parent/node.sock',
                  'SKY_CUA_SERVICE_NATIVE_PIPE_PATH':'/parent/sky.sock',
                  'CODEX_HOME':'/parent/home','CODEX_SQLITE_HOME':'/parent/db',
                  'CODEX_ELECTRON_USER_DATA_PATH':'/parent/ui','OPENAI_API_KEY':'fixture',
                  'OPENAI_BASE_URL':'https://fixture.invalid','PATH':'/usr/bin'}
        profile = {'source':False,'home':'/fixture/clone/home','ui':'/fixture/clone/ui'}
        with patch.dict(os.environ,parent,clear=True), \
                patch.object(shadow_clones,'check_version'), \
                patch.object(shadow_clones.subprocess,'run') as run:
            shadow_clones.launch_profile(profile)
        args = run.call_args.args[0];env = run.call_args.kwargs['env']
        for key in parent:
            if key != 'PATH':
                self.assertNotIn(key,env)
                self.assertFalse(any(arg.startswith(key+'=/parent') for arg in args))
        self.assertFalse(any('CODEX_APP_TOOLS_PIPE_PATH=' in arg for arg in args))
        self.assertIn('CODEX_HOME='+profile['home'],args)
        self.assertIn('CODEX_ELECTRON_USER_DATA_PATH='+profile['ui'],args)
        self.assertEqual(env['PATH'],'/usr/bin')

    def test_isolation_preserves_parent_mapping_and_unrelated_system_settings(self):
        from shadow_desktop_env import desktop_environment
        original = {'CODEX_APP_TOOLS_PIPE_PATH':'/tmp/parent.sock','HOME':'/user',
                    'HTTPS_PROXY':'http://127.0.0.1:8888','LANG':'en_US.UTF-8'}
        isolated = desktop_environment(original)
        self.assertIn('CODEX_APP_TOOLS_PIPE_PATH',original)
        self.assertEqual(isolated,{'HOME':'/user','HTTPS_PROXY':'http://127.0.0.1:8888','LANG':'en_US.UTF-8'})
