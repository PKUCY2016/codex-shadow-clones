import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from shadow_quota import normalize_limits, read_quota


FAKE = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
scenario = json.loads((home/'scenario.json').read_text())
(home/'child.pid').write_text(str(os.getpid()))
for line in sys.stdin:
    req = json.loads(line)
    with (home/'calls.jsonl').open('a') as out:
        out.write(json.dumps(req)+'\\n')
    method=req.get('method')
    if method == 'initialize':
        result={}
    elif method == 'account/read':
        result={'account':scenario.get('account', {'type':'chatgpt','email':'user@example.com','planType':'pro'})}
    elif method == 'account/rateLimits/read':
        if scenario.get('timeout'):
            time.sleep(60)
        if scenario.get('error'):
            print(json.dumps({'id':req['id'],'error':{'message':'SECRET_TOKEN_FROM_UPSTREAM'}}),flush=True)
            continue
        result=scenario.get('limits',{'rateLimits': {'primary': {'usedPercent':25,'windowDurationMins':300,'resetsAt':2000000000}}})
    else:
        continue
    print(json.dumps({'method':'unrelated/notification','params':{}}),flush=True)
    print(json.dumps({'id':req['id'],'result':result}),flush=True)
'''


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.binary = self.home/'fake-codex'
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, scenario, timeout=2):
        (self.home/'scenario.json').write_text(json.dumps(scenario))
        return read_quota(self.home, timeout=timeout, codex_binary=self.binary)

    def test_protocol_no_model_or_auth_mutations(self):
        result = self.run_scenario({})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['coreRemainingPercent'], 75)
        self.assertEqual(len(result['account']['id']), 64)
        calls = [json.loads(line) for line in (self.home/'calls.jsonl').read_text().splitlines()]
        self.assertEqual([call['method'] for call in calls], ['initialize','initialized','account/read','account/rateLimits/read'])
        self.assertEqual(calls[2]['params'], {'refreshToken': False})

    def test_multibucket_prefers_core_not_image_or_legacy(self):
        result = self.run_scenario({'limits': {'rateLimits': {'primary': {'usedPercent':0}}, 'rateLimitsByLimitId': {
            'image': {'primary': {'usedPercent':0}},
            'codex': {'primary': {'usedPercent':80}, 'secondary': {'usedPercent':95}}}}})
        self.assertEqual(result['coreRemainingPercent'], 5)
        self.assertEqual([x['isCodex'] for x in result['limits']], [False, True])

    def test_unknown_usage_never_becomes_full_quota(self):
        for windows in ({'primary': {'usedPercent': None}}, {'primary': {'usedPercent':20}, 'secondary': {}}, {'primary': None, 'secondary': {'usedPercent': 10}}, {}):
            result = self.run_scenario({'limits': {'rateLimits': windows}})
            self.assertEqual(result['status'], 'unknown')
            self.assertIsNone(result['coreRemainingPercent'])
        result = self.run_scenario({'limits': {'rateLimitsByLimitId': {'image': {'primary': {'usedPercent':1}}}}})
        self.assertIsNone(result['coreRemainingPercent'])

    def test_absent_login_does_not_fetch_limits(self):
        result = self.run_scenario({'account': None})
        self.assertEqual(result['status'], 'not_logged_in')
        self.assertNotIn('account/rateLimits/read', (self.home/'calls.jsonl').read_text())

    def test_error_is_sanitized(self):
        result = self.run_scenario({'error': True})
        self.assertEqual(result['error'], 'quota_request_failed')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_timeout_terminates_owned_child(self):
        start=time.monotonic()
        result = self.run_scenario({'timeout': True}, timeout=1.5)
        self.assertEqual(result['error'], 'timeout')
        self.assertLess(time.monotonic()-start, 4)
        pid = int((self.home/'child.pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_limit_reached_and_invalid_numbers(self):
        result = self.run_scenario({'limits': {'rateLimits': {'rateLimitReachedType':'workspace_owner_credits_depleted'}}})
        self.assertEqual(result['coreRemainingPercent'], 0)
        windows = normalize_limits({'rateLimits': {'primary': {'usedPercent': True},'secondary':{'usedPercent':float('nan')}}})[0]['windows']
        self.assertTrue(all(window['remainingPercent'] is None for window in windows))


if __name__ == '__main__':
    unittest.main()
