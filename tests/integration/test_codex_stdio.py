"""Real subprocess/pipe integration with a local fake server; no Codex model."""

import json
from pathlib import Path
import sys
import unittest

from kagebunshin.orchestration.infrastructure.codex_rpc import StdioRPC
from kagebunshin.orchestration.ports import ProviderBlocked, ProviderUnknown


class StdioIntegrationTest(unittest.TestCase):
    def server(self, code):
        return StdioRPC([sys.executable, "-I", "-u", "-c", code], Path.cwd(), timeout=0.3)

    def test_opted_in_tool_request_can_wait_for_turn_reply_and_is_answered_once(self):
        definition={'type':'function','name':'kagebunshin_run_check','description':'Registered check',
                    'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}
        code='''import json,sys
r=json.loads(sys.stdin.readline())
print(json.dumps({'id':77,'method':'item/tool/call','params':{'threadId':'t','turnId':'u','callId':'c','tool':'kagebunshin_run_check','arguments':{}}}),flush=True)
print(json.dumps({'id':r['id'],'result':{'thread':{'id':'t'}}}),flush=True)
answer=json.loads(sys.stdin.readline())
sys.exit(0 if answer['id']==77 and answer['result']['success'] is True else 4)
'''
        with self.server(code) as rpc:
            rpc.configure_dynamic_tools([definition])
            with self.assertRaises(ProviderBlocked): rpc.call('thread/start',{'dynamicTools':[]})
            self.assertEqual(rpc.methods,[])
            rpc.call('thread/start',{'dynamicTools':[definition]})
            self.assertEqual(rpc.notifications[0]['id'],77)
            result={'success':True,'contentItems':[{'type':'inputText','text':'saved receipt'}]}
            with self.assertRaises(ProviderBlocked): rpc.respond_tool(78,result)
            rpc.respond_tool(77,result)
            with self.assertRaises(ProviderBlocked): rpc.respond_tool(77,result)
            self.assertEqual(rpc.process.wait(timeout=1),0)

    def test_enabled_tools_still_deny_approval_requests(self):
        code='''import sys,json
sys.stdin.readline()
print(json.dumps({'id':4,'method':'item/permissions/requestApproval','params':{}}),flush=True)
answer=json.loads(sys.stdin.readline())
sys.exit(0 if 'error' in answer else 4)
'''
        with self.server(code) as rpc:
            rpc.configure_dynamic_tools([{'type':'function','name':'kagebunshin_run_check',
                'description':'Registered','inputSchema':{'type':'object'}}])
            with self.assertRaises(ProviderBlocked): rpc.call('account/read',{})
            self.assertEqual(rpc.process.wait(timeout=1),0)

    def test_split_jsonl_and_interleaved_notifications_are_preserved(self):
        code = '''import json,sys,time
r=json.loads(sys.stdin.readline())
sys.stdout.write('{"method":"test/notice",');sys.stdout.flush()
time.sleep(.01)
sys.stdout.write('"params":{}}\\n');sys.stdout.flush()
print(json.dumps({"id":r["id"],"result":{"ok":True}}),flush=True)
'''
        with self.server(code) as rpc:
            self.assertEqual(rpc.call("account/read", {}), {"ok": True})
            self.assertEqual(len(rpc.notifications), 1)
        self.assertIsNotNone(rpc.process.poll())

    def test_malformed_duplicate_or_nonfinite_json_is_not_success(self):
        for output in ['{"id":1,"id":2,"result":{}}', '{"id":1,"result":{"value":NaN}}', 'broken']:
            code = "import sys;sys.stdin.readline();print(" + repr(output) + ",flush=True)"
            with self.subTest(output=output), self.server(code) as rpc:
                with self.assertRaises(ProviderUnknown):
                    rpc.call("account/read", {})

    def test_server_tool_request_is_denied_without_executing(self):
        code = '''import sys,json
sys.stdin.readline()
print(json.dumps({"id":77,"method":"item/tool/call","params":{"name":"shell"}}),flush=True)
reply=json.loads(sys.stdin.readline())
sys.exit(0 if reply.get("error",{}).get("code")==-32601 else 3)
'''
        with self.server(code) as rpc:
            with self.assertRaises(ProviderBlocked):
                rpc.call("account/read", {})
            self.assertEqual(rpc.process.wait(timeout=1), 0)

    def test_arbitrary_process_method_is_not_sent(self):
        with self.server("import sys;sys.stdin.readline()") as rpc:
            with self.assertRaises(ProviderBlocked):
                rpc.call("process/spawn", {"command": "not allowed"})
            self.assertEqual(rpc.methods, [])

    def test_resume_tool_registration_config_overrides_and_batch_are_not_sent(self):
        probes = [("thread/resume", {"threadId": "owned-legacy-thread"}),
                  ("thread/start", {"dynamicTools": [{"name": "legacy_tool"}]}),
                  ("thread/start", {"config": {"features": {"shell_tool": True}}}),
                  ("turn/start", {"permissions": {"type": "fullAccess"}}),
                  ("turn/start", {"collaborationMode": {"mode": "delegate"}}),
                  ("turn/start", [{"method": "process/spawn", "params": {"index": n}} for n in range(900)])]
        with self.server("import sys;sys.stdin.readline()") as rpc:
            for method, params in probes:
                with self.subTest(method=method, params_type=type(params).__name__):
                    with self.assertRaises(ProviderBlocked):
                        rpc.call(method, params)
            self.assertEqual(rpc.methods, [])
            self.assertEqual(rpc.next_id, 1)

    def test_future_foreign_and_boolean_response_ids_are_not_reused(self):
        for ident in (2, "1", True, None, {"id": 1}):
            response = {"id": ident, "result": {"accepted": "wrong response"}}
            code = "import sys;sys.stdin.readline();print(" + repr(json.dumps(response)) + ",flush=True)"
            with self.subTest(ident=ident), self.server(code) as rpc:
                with self.assertRaises(ProviderUnknown):
                    rpc.call("account/read", {})
                self.assertEqual(rpc.methods, ["account/read"])

    def test_repeated_response_cannot_satisfy_the_next_call(self):
        code = '''import json,sys
r=json.loads(sys.stdin.readline())
print(json.dumps({"id":r["id"],"result":{"first":True}}),flush=True)
print(json.dumps({"id":r["id"],"result":{"forged":True}}),flush=True)
sys.stdin.readline()
'''
        with self.server(code) as rpc:
            self.assertEqual(rpc.call("account/read", {}), {"first": True})
            with self.assertRaises(ProviderUnknown):
                rpc.call("account/rateLimits/read", {})

    def test_deadline_does_not_become_empty_success(self):
        with self.server("import sys,time;sys.stdin.readline();time.sleep(2)") as rpc:
            with self.assertRaises(ProviderUnknown):
                rpc.call("account/read", {})
