"""Adapter contract tests with fake transport; no actual runtime connection."""

from copy import deepcopy
import json
from pathlib import Path
import time
import unittest

from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.domain.contracts import Node, Plan
from kagebunshin.orchestration.infrastructure.codex_provider import CodexProfile, CodexProvider, DISABLED
from kagebunshin.orchestration.ports import ProviderBlocked, ProviderUnknown
from tests.unit.fakes import MemoryStore, ROLES, candidate


class FakeRPC:
    def __init__(self):
        self.methods = []
        self.requests = []
        self.notifications = []
        self.closed = False
        self.account = {"type": "chatgpt", "planType": "pro"}
        self.config = {"model": "gpt-6-astra", "model_provider": "openai", "model_reasoning_effort": "low",
                       "service_tier": "default", "forced_login_method": "chatgpt", "approval_policy": "never",
                       "approvals_reviewer": "user", "web_search": "disabled", "agents": {"enabled": False},
                       "features": {k: False for k in DISABLED}, "mcp_servers": {},
                       "model_catalog_json": "/fake/tool-catalog.json",
                       "orchestrator": {"skills": {"enabled": False}, "mcp": {"enabled": False}},
                       "skills": {"include_instructions": False},
                       "tools": {"experimental_request_user_input": {"enabled": False}, "update_plan": {"enabled": False}}}
        self.tool_flags = deepcopy(self.config["tools"])
        self.tool_origin_type = "sessionFlags"
        self.rates = {"ordinaryUsageAllowed": True, "rateLimitsByLimitId": {"codex": {
            "limitId": "codex", "planType": "pro", "spendControlReached": False,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": int(time.time()) + 1000}, "secondary": None}}}
        self.response = {"model": "gpt-6-astra", "modelProvider": "openai", "reasoningEffort": "low",
                         "serviceTier": "default", "approvalPolicy": "never", "approvalsReviewer": "user",
                         "sandbox": {"type": "readOnly", "networkAccess": False},
                         "thread": {"id": "thread1", "environments": []}}
        self.on_start = None
        self.items = [{"id": "item1", "type": "agentMessage", "phase": "final_answer", "text": json.dumps(candidate())}]
        self.status = "completed"
        self.turn_start_error = False
        self.history_mode = "legacy"
        self.history_pages = []
        self.item_pages = []
        self.history_thread = "thread1"
        self.runtime = "kagebunshin/0.154.0-alpha.6.2 (test)"
        self.features = {k: False for k in DISABLED}
        self.features["unified_exec"] = True

    def initialize(self):
        self.methods.append("initialize")
        return {"userAgent": self.runtime}

    def close(self):
        self.closed = True

    def call(self, method, params, *, deadline=None):
        self.methods.append(method)
        self.requests.append((method, deepcopy(params)))
        if method == "config/read":
            return {"config": deepcopy(self.config),
                    "layers": [{"name": {"type": "sessionFlags"}, "version": "sha256:fixture",
                                "config": {"tools": deepcopy(self.tool_flags)}}],
                    "origins": {"tools." + k + ".enabled": {"name": {"type": self.tool_origin_type}, "version": "sha256:fixture"}
                                for k in self.tool_flags}}
        if method == "experimentalFeature/list": return {"data": [{"name": k, "enabled": v} for k, v in self.features.items()]}
        if method == "account/read": return {"account": self.account}
        if method == "account/rateLimits/read": return self.rates
        if method == "thread/start": return self.response
        if method == "turn/start":
            if self.turn_start_error: raise ProviderUnknown("lost start response")
            if self.on_start: self.on_start()
            self.notifications.extend([{"method": "item/completed", "params": {"threadId": "thread1", "turnId": "turn1", "item": i}} for i in self.items])
            self.notifications.append({"method": "turn/completed", "params": {"threadId": "thread1", "turn": {"id": "turn1", "status": self.status}}})
            return {"turn": {"id": "turn1"}}
        if method == "turn/interrupt": return {}
        if method == "thread/read":
            return {"thread": {"id": self.history_thread, "historyMode": self.history_mode, "turns": [{"id": "turn1", "status": self.status, "items": self.items}]}}
        if method == "thread/turns/list": return self.history_pages.pop(0)
        if method == "thread/items/list": return self.item_pages.pop(0)
        raise AssertionError(method)


class FakeCatalog:
    path = Path("/fake/tool-catalog.json")
    valid = True

    def contract(self):
        return {"policy": "text-only-model-catalog-v1", "original_sha256": "a" * 64, "restricted_sha256": "b" * 64}

    def verify(self):
        if not self.valid:
            raise ProviderBlocked("catalog changed")


class CodexProviderTest(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeRPC()
        self.catalog = FakeCatalog()
        self.provider = CodexProvider(CodexProfile(Path("/fake/codex"), Path("/fake/workspace"), model="gpt-6-astra"), (), lambda *args: self.rpc, catalog=self.catalog)
        self.engine = Engine(MemoryStore(), self.provider, ROLES, time.time)
        self.run_id = self.engine.create(Plan("task", (Node("a", "R07", "explain"),), {}))
        self.addCleanup(self.provider.close)

    def test_dynamic_call_is_dispatched_only_after_actual_turn_binding(self):
        from kagebunshin.orchestration.ports import Request
        events=[]
        definition={'type':'function','name':'kagebunshin_run_check','description':'Registered',
                    'inputSchema':{'type':'object'}}
        class Session:
            def definitions(self): return [definition]
            def validate_request(self,*args): events.append(('validate',args))
            def bind(self,thread,turn): events.append(('bind',thread,turn))
            def handle(self,params):
                if events[-1]!=('bind','thread1','turn1'): raise AssertionError('unbound execution')
                events.append(('handle',params['callId']))
                return {'success':True,'contentItems':[{'type':'inputText','text':'saved'}]}
        self.rpc.configure_dynamic_tools=lambda value:events.append(('declarations',value))
        self.rpc.respond_tool=lambda ident,result:events.append(('reply',ident,result['success']))
        self.rpc.on_start=lambda:self.rpc.notifications.append({'id':77,'method':'item/tool/call','params':{
            'threadId':'thread1','turnId':'turn1','callId':'call1','tool':'kagebunshin_run_check','arguments':{}}})
        result=self.provider.execute(Request('attempt','run','node','produce','R07','prompt','a'*64,tool_session=Session()))
        self.assertEqual(result.status,'completed')
        self.assertEqual(events[-3:],[('bind','thread1','turn1'),('handle','call1'),('reply',77,True)])
        sent=next(params for method,params in self.rpc.requests if method=='thread/start')
        self.assertEqual(sent['dynamicTools'],[definition])
        self.assertEqual(sent['sandbox'],'read-only')
        self.assertEqual(sent['environments'],[])

    def test_typed_contract_reaches_transport_and_invalid_values_remain_blocked(self):
        run_id = self.engine.create(Plan("typed task", (Node("typed", "R07", "count", max_attempts=1,
            response_fields={"count": "number"}),), {}))
        result = self.engine.run_until_idle(run_id)
        self.assertEqual(result["state"], "blocked")
        turns = [p for method,p in self.rpc.requests if method == "turn/start"]
        self.assertEqual(len(turns), 1)
        schema = turns[0]["outputSchema"]
        self.assertEqual(schema["properties"]["values"]["required"], ["count"])
        self.assertEqual(schema["properties"]["values"]["properties"]["count"], {"type": "number"})
        self.assertFalse(schema["additionalProperties"])

    def test_audit_format_is_sent_but_contradictory_approval_is_rejected(self):
        run_id = self.engine.create(Plan("audited task", (Node("audited", "R07", "explain", audit_role="R12"),), {}))
        self.assertTrue(self.engine.step(run_id))
        self.rpc.items = [{"id": "audit", "type": "agentMessage", "phase": "final_answer", "text": json.dumps({
            "approved": True, "reason": "contradictory", "defects": [{"code": "missing", "target_path": "text",
            "expected": "source fidelity", "observed": "unsupported", "evidence": "record", "repairable": True}]})}]
        result = self.engine.run_until_idle(run_id)
        self.assertEqual(result["state"], "blocked")
        turns = [p for method,p in self.rpc.requests if method == "turn/start"]
        self.assertEqual(len(turns), 2)
        self.assertNotIn("outputSchema", turns[0])
        self.assertEqual(turns[1]["outputSchema"]["required"], ["approved", "reason", "defects"])
        self.assertFalse(turns[1]["outputSchema"]["properties"]["defects"]["items"]["additionalProperties"])

    def test_planner_schema_preserves_empty_gap_and_blocks_execution(self):
        from kagebunshin.orchestration.application.team import Team
        from kagebunshin.orchestration.domain.planning import ROLE_INDEX
        self.rpc.items = [{"id": "plan", "type": "agentMessage", "phase": "final_answer", "text": json.dumps({
            "text": "No suitable role", "source_ids": [ROLE_INDEX], "limits": ["capability gap"], "values": {"nodes": []}})}]
        team = Team(self.engine)
        run_id = team.submit("unsupported task", {})
        result = team.advance(run_id)
        self.assertNotEqual(result["state"], "completed")
        self.assertEqual(result["planning"]["stage"], "proposal")
        turns = [p for method,p in self.rpc.requests if method == "turn/start"]
        self.assertEqual(len(turns), 1)
        for turn in turns:
            nodes = turn["outputSchema"]["properties"]["values"]["properties"]["nodes"]
            self.assertNotIn("minItems", nodes)
            self.assertEqual(set(nodes["items"]["properties"]),
                             {"id", "role_id", "instruction", "dependencies", "source_ids"})
            self.assertFalse(nodes["items"]["additionalProperties"])

    def test_common_low_profile_is_explicit_for_the_child_and_each_turn(self):
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["state"], "completed")
        for flag in ('model="gpt-6-astra"', 'model_reasoning_effort="low"', 'service_tier="default"'):
            self.assertIn(flag, self.provider.command)
        turns = [p for method, p in self.rpc.requests if method == "turn/start"]
        self.assertEqual(len(turns), 1)
        self.assertEqual((turns[0]["model"], turns[0]["effort"], turns[0]["serviceTier"]),
                         ("gpt-6-astra", "low", "default"))
        self.rpc.response["reasoningEffort"] = "max"
        other = self.engine.create(Plan("new task", (Node("b", "R07", "explain"),), {}))
        self.engine.run_until_idle(other)
        self.assertEqual(sum(method == "turn/start" for method, _ in self.rpc.requests), 1)

    def test_checked_connection_is_used_and_handles_are_durable_before_completion(self):
        def on_start():
            attempt = self.engine.status(self.run_id)["attempts"][0]
            self.assertEqual(attempt["thread_id"], "thread1")
            self.assertIsNone(attempt["turn_id"])
        self.rpc.on_start = on_start
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["attempts"][0]["turn_id"], "turn1")
        self.assertEqual(self.rpc.methods.count("initialize"), 2)  # generation and archive readback connections
        self.assertEqual(self.rpc.methods.count("turn/start"), 1)
        self.assertTrue(self.rpc.closed)

    def test_bad_auth_has_zero_turn_submissions(self):
        self.rpc.account = {"type": "apiKey"}
        self.assertEqual(self.engine.run_until_idle(self.run_id)["state"], "blocked_preflight")
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_tool_catalog_and_orchestrator_discrepancies_block_before_dispatch(self):
        for conf in ({"model_catalog_json": "/wrong/catalog.json"},
                     {"orchestrator": {"skills": {"enabled": True}, "mcp": {"enabled": False}}},
                     {"tools": {"experimental_request_user_input": {"enabled": True}, "update_plan": {"enabled": False}}}):
            with self.subTest(conf=conf):
                original = deepcopy(self.rpc.config)
                self.rpc.config.update(conf)
                with self.assertRaises(ProviderBlocked):
                    self.provider.preflight()
                self.assertNotIn("turn/start", self.rpc.methods)
                self.rpc.config = original
        self.catalog.valid = False
        with self.assertRaises(ProviderBlocked):
            self.provider.preflight()
        self.assertNotIn("turn/start", self.rpc.methods)
        self.provider.catalog = None
        with self.assertRaises(ProviderBlocked):
            self.provider.preflight()

    def test_omitted_typed_tool_fields_require_winning_explicit_false_flags(self):
        self.rpc.config["tools"] = {"web_search": None}
        self.provider.preflight()
        self.rpc.tool_origin_type = "user"
        with self.assertRaises(ProviderBlocked):
            self.provider.preflight()
        self.rpc.tool_origin_type = "sessionFlags"
        self.rpc.tool_flags["update_plan"]["enabled"] = True
        with self.assertRaises(ProviderBlocked):
            self.provider.preflight()
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_unexpected_capability_or_model_blocks_before_dispatch(self):
        for conf in [{"mcp_servers": {"unexpected": {"enabled": True}}}, {"model": "other-model"}]:
            with self.subTest(conf=conf):
                original = deepcopy(self.rpc.config)
                self.rpc.config.update(conf)
                with self.assertRaises(ProviderBlocked): self.provider.preflight()
                self.assertNotIn("turn/start", self.rpc.methods)
                self.rpc.config = original

    def test_nonempty_environment_is_rejected_before_model_turn(self):
        self.rpc.response["thread"]["environments"] = [{"id": "host"}]
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "blocked_provider")
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_unexpected_instruction_source_is_rejected_before_model_turn(self):
        self.rpc.response["instructionSources"] = ["/unexpected/instructions.md"]
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "blocked_provider")
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_runtime_gate_overrides_and_unknown_cli_version_block_before_dispatch(self):
        self.rpc.features["browser_use"] = True
        with self.assertRaises(ProviderBlocked): self.provider.preflight()
        self.assertNotIn("turn/start", self.rpc.methods)
        self.rpc.features["browser_use"] = False
        self.rpc.runtime = "kagebunshin/999.0.0 (test)"
        with self.assertRaises(ProviderBlocked): self.provider.preflight()
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_start_response_loss_retains_thread_handle_and_never_resubmits(self):
        self.rpc.turn_start_error = True
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "unknown")
        self.assertEqual(result["attempts"][0]["thread_id"], "thread1")
        self.engine.reconcile(self.run_id)
        self.engine.run_until_idle(self.run_id)
        self.assertEqual(self.rpc.methods.count("turn/start"), 1)

    def test_failed_terminal_does_not_accept_partial_output(self):
        self.rpc.status = "failed"
        result = self.engine.run_until_idle(self.run_id)
        self.assertIsNone(result["nodes"]["a"]["output"])
        self.assertEqual(result["nodes"]["a"]["state"], "blocked_provider")

    def test_stop_requests_interrupt_and_quarantines_racing_completed(self):
        self.rpc.on_start = lambda: self.engine.stop(self.run_id)
        self.engine.step(self.run_id)
        self.assertIn("turn/interrupt", self.rpc.methods)
        self.assertEqual(self.engine.status(self.run_id)["nodes"]["a"]["state"], "quarantined")

    def test_unexpected_tool_item_interrupts_and_remains_unknown(self):
        self.rpc.items.insert(0, {"id": "tool1", "type": "commandExecution"})
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "unknown")
        self.assertIn("turn/interrupt", self.rpc.methods)
        self.assertEqual(result["attempts"][0]["provider_diagnostics"], ["unexpected_item_type"])

    def test_paginated_summary_fetches_full_items_before_acceptance(self):
        self.rpc.history_mode = "paginated"
        self.rpc.history_pages = [{"data": [], "nextCursor": "next"}, {"data": [{"id": "turn1", "status": "completed", "itemsView": "summary", "items": []}], "nextCursor": None}]
        self.rpc.item_pages = [{"data": [{"turnId": "turn1", "item": self.rpc.items[0]}], "nextCursor": None}]
        result = self.provider.reconcile({"thread_id": "thread1", "turn_id": "turn1"})
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.rpc.methods.count("thread/items/list"), 1)
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_cyclic_history_cursor_does_not_resubmit(self):
        self.rpc.history_mode = "paginated"
        self.rpc.history_pages = [{"data": [], "nextCursor": "same"}, {"data": [], "nextCursor": "same"}]
        with self.assertRaises(ProviderUnknown):
            self.provider.reconcile({"thread_id": "thread1", "turn_id": "turn1"})
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_history_from_another_thread_is_not_accepted(self):
        self.rpc.history_thread = "another-thread"
        self.assertIsNone(self.provider.reconcile({"thread_id": "thread1", "turn_id": "turn1"}))
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_conflicting_duplicate_history_is_not_silently_overwritten(self):
        self.rpc.history_mode = "paginated"
        first = {"id": "turn1", "status": "completed", "itemsView": "full", "items": self.rpc.items}
        self.rpc.history_pages = [{"data": [first, {**first, "status": "failed"}], "nextCursor": None}]
        self.assertIsNone(self.provider.reconcile({"thread_id": "thread1", "turn_id": "turn1"}))
        self.assertNotIn("turn/start", self.rpc.methods)

    def test_sparse_quota_update_uses_fresh_snapshot_and_stops_without_fallback(self):
        for allowed in (None, False):
            with self.subTest(allowed=allowed):
                self.setUp()
                def update():
                    self.rpc.rates["ordinaryUsageAllowed"] = allowed
                    self.rpc.notifications.append({"method": "account/rateLimits/updated", "params": {}})
                self.rpc.on_start = update
                result = self.engine.run_until_idle(self.run_id)
                self.engine.run_until_idle(self.run_id)
                self.assertEqual(result["nodes"]["a"]["state"], "unknown")
                self.assertIsNone(result["nodes"]["a"]["output"])
                self.assertIn("turn/interrupt", self.rpc.methods)
                self.assertEqual(self.rpc.methods.count("turn/start"), 1)
                self.assertGreaterEqual(self.rpc.methods.count("account/rateLimits/read"), 3)

    def test_account_or_configuration_drift_interrupts_active_turn(self):
        for method in ("account/updated", "configWarning"):
            with self.subTest(method=method):
                self.setUp()
                self.rpc.on_start = lambda: self.rpc.notifications.append({"method": method, "params": {}})
                result = self.engine.run_until_idle(self.run_id)
                self.assertEqual(result["nodes"]["a"]["state"], "unknown")
                self.assertIn("turn/interrupt", self.rpc.methods)
                self.assertIsNone(result["nodes"]["a"]["output"])

    def test_terminal_before_final_item_is_not_accepted_as_empty_success(self):
        self.rpc.on_start = lambda: self.rpc.notifications.append({"method": "turn/completed", "params": {
            "threadId": "thread1", "turn": {"id": "turn1", "status": "completed"}}})
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "unknown")
        self.assertIsNone(result["nodes"]["a"]["output"])
        self.assertEqual(self.rpc.methods.count("turn/start"), 1)

    def test_interrupt_reply_loss_keeps_stop_and_unknown_reservation(self):
        original = self.rpc.call
        def call(method, params, **kwargs):
            if method == "turn/interrupt":
                self.rpc.methods.append(method)
                raise ProviderUnknown("interrupt acknowledgment lost")
            return original(method, params, **kwargs)
        self.rpc.call = call
        self.rpc.on_start = lambda: self.engine.stop(self.run_id)
        self.engine.step(self.run_id)
        result = self.engine.run_until_idle(self.run_id)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["nodes"]["a"]["state"], "unknown")
        self.assertIsNotNone(result["nodes"]["a"]["active"])
        self.assertIsNone(result["nodes"]["a"]["output"])
        self.assertEqual(self.rpc.methods.count("turn/start"), 1)

    def test_empty_archive_requires_explicit_empty_turn_history(self):
        original = self.rpc.call
        history = {'id': 'empty', 'turns': []}
        def call(method, params, **kwargs):
            if method == 'thread/read':
                return {'thread': deepcopy(history)}
            if method == 'thread/archive':
                archives.append(params['threadId'])
                return {}
            return original(method, params, **kwargs)
        self.rpc.call = call
        archives = []
        self.provider.archive_owned('empty', None)
        self.assertEqual(archives, ['empty'])
        for invalid in ({'id': 'empty'}, {'id': 'empty', 'turns': None},
                        {'id': 'other', 'turns': []},
                        {'id': 'empty', 'turns': [{'id': 't', 'status': 'completed'}]},
                        {'id': 'empty', 'turns': [{'id': 't', 'status': 'inProgress'}]}):
            history = invalid
            with self.assertRaises(ProviderBlocked):
                self.provider.archive_owned('empty', None)
        self.assertEqual(archives, ['empty'])

    def test_archive_checks_all_paginated_turns_and_latest_identity(self):
        original = self.rpc.call
        archives = []
        def call(method, params, **kwargs):
            if method == 'thread/archive':
                archives.append(params['threadId'])
                return {}
            return original(method, params, **kwargs)
        self.rpc.call = call
        self.rpc.history_mode = 'paginated'
        self.rpc.history_pages = [
            {'data': [{'id': 'latest', 'status': 'completed'}], 'nextCursor': 'next'},
            {'data': [{'id': 'old', 'status': 'completed'}], 'nextCursor': None}]
        self.provider.archive_owned('thread1', 'latest')
        self.assertEqual(archives, ['thread1'])
        for rows, expected in [([{'id': 'latest', 'status': 'completed'}, {'id': 'old', 'status': 'inProgress'}], 'latest'),
                               ([{'id': 'latest', 'status': 'completed'}], 'other')]:
            self.rpc.history_pages = [{'data': rows, 'nextCursor': None}]
            with self.assertRaises(ProviderBlocked):
                self.provider.archive_owned('thread1', expected)
        self.assertEqual(archives, ['thread1'])

    def test_unsent_release_requires_exact_error_and_process_exit(self):
        from types import SimpleNamespace
        from kagebunshin.orchestration.infrastructure.codex_rpc import RPCRejected
        exact = 'thread thread1 is not materialized yet; thread/turns/list is unavailable before first user message'
        for code, detail, poll, expected in [(-32600, exact, 0, 'released_unmaterialized'),
                                            (-32600, 'thread not loaded', 0, None),
                                            (-32600, exact.replace('thread1', 'other'), 0, None),
                                            (-1, exact, 0, None), (-32600, exact, None, None)]:
            def call(*args, **kwargs):
                raise RPCRejected('thread/turns/list', {'code': code, 'message': detail})
            self.rpc.call = call
            self.rpc.process = SimpleNamespace(poll=lambda: poll)
            result = self.provider._unsent(self.rpc, 'thread1')
            self.assertEqual(result.status, 'not_sent')
            self.assertEqual(result.cleanup_state, expected)

    def test_usage_refresh_never_starts_thread_and_closes_reader(self):
        self.rpc.rates['rateLimitsByLimitId']['codex']['secondary'] = {'usedPercent': 21, 'windowDurationMins': 10080, 'resetsAt': int(time.time()) + 1000}
        result = self.provider.refresh_usage()
        self.assertTrue(result['available'])
        self.assertEqual(result['remaining_percent'], 79)
        self.assertTrue(self.rpc.closed)
        self.assertNotIn('thread/start', self.rpc.methods)
        self.assertNotIn('turn/start', self.rpc.methods)
        self.rpc.account = {'type': 'apiKey'}
        result = self.provider.refresh_usage()
        self.assertFalse(result['available'])
        self.assertEqual(self.provider.usage_snapshot()['error'], 'usage_read_failed')

    def test_shutdown_during_usage_read_closes_once_and_prevents_reopen(self):
        original_call = self.rpc.call
        closes = []
        original_close = self.rpc.close
        def close():
            closes.append(True)
            original_close()
        self.rpc.close = close
        def call(method, params, **kwargs):
            if method == 'account/rateLimits/read':
                self.provider.close()
            return original_call(method, params, **kwargs)
        self.rpc.call = call
        result = self.provider.refresh_usage()
        self.assertFalse(result['available'])
        self.assertEqual(result['error'], 'provider_closed')
        self.assertEqual(len(closes), 1)
        count = len(self.rpc.methods)
        self.assertFalse(self.provider.refresh_usage()['available'])
        with self.assertRaises(ProviderBlocked):
            self.provider._connection()
        self.assertEqual(len(self.rpc.methods), count)
        self.assertFalse(self.provider.connections)

    def test_failed_reader_cleanup_is_retained_and_other_connections_close(self):
        def fail():
            raise OSError('synthetic close failure')
        self.rpc.close = fail
        result = self.provider.refresh_usage()
        self.assertEqual(result['error'], 'usage_cleanup_failed')
        self.assertIn(self.rpc, self.provider.connections)
        other = FakeRPC()
        self.provider.connections.add(other)
        with self.assertRaises(ProviderUnknown):
            self.provider.close()
        self.assertTrue(other.closed)
        self.assertIn(self.rpc, self.provider.connections)
        self.rpc.close = lambda: None
        self.provider.close()
        self.assertFalse(self.provider.connections)
