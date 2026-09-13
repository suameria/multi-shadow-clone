"""Codex subscription adapter. All inference uses its checked stdio connection."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
import time

from ..domain.contracts import InvalidContract, digest
from ..domain.subscription import included_usage
from ..domain.usage_display import weekly_usage
from ..ports import ProviderBlocked, ProviderUnknown, Request, Result
from .codex_rpc import DISABLED_FEATURES, RPCRejected, StdioRPC, codex_command


@dataclass(frozen=True)
class CodexProfile:
    """Shared agent settings; role prompts never select models or effort."""
    binary: Path
    cwd: Path
    model: str = "gpt-5.6-luna"
    effort: str = "low"
    service_tier: str = "default"
    max_seconds: int = 180
    cli_version: str = "0.154.0-alpha.6.2"
    instruction_sources: tuple[str, ...] = ()


DISABLED = DISABLED_FEATURES


class CodexProvider:
    """One checked connection per worker; close() also owns unused preflights."""

    def __init__(self, profile: CodexProfile, mcp_names: tuple[str, ...], rpc_factory=StdioRPC, *, catalog=None):
        self.profile, self.rpc_factory = profile, rpc_factory
        self.catalog = catalog
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in mcp_names):
            raise ProviderBlocked("unsupported MCP override identifier")
        self.command = codex_command(profile.binary)
        # Scope the owner's common settings to this child process. Do not
        # depend on, or rewrite, the desktop app's current conversation setup.
        for key, value in (("model", profile.model), ("model_reasoning_effort", profile.effort),
                           ("service_tier", profile.service_tier)):
            self.command.extend(["-c", key + "=" + json.dumps(value)])
        if catalog is not None:
            self.command.extend(["-c", "model_catalog_json=" + json.dumps(str(catalog.path))])
        for name in mcp_names:
            self.command.extend(["-c", f"mcp_servers.{name}.enabled=false"])
        self.local = threading.local()
        self.lock = threading.Lock()
        self.connections = set()
        self.closed = False
        self.last_receipt = None
        self.last_usage_display = {"available": False, "scope": "account", "observed_at": None}
        self.effective_features = {}
        self.observed_item_types = {}

    def close(self):
        with self.lock:
            self.closed = True
            failures = []
            for rpc in tuple(self.connections):
                try:
                    rpc.close()
                    self.connections.discard(rpc)
                except Exception as exc:
                    failures.append(exc)
        self.local.rpc = None
        if failures:
            raise ProviderUnknown("owned connection cleanup failed") from failures[0]

    def _new_connection(self):
        with self.lock:
            if self.closed:
                raise ProviderBlocked("provider is closed")
            rpc = self.rpc_factory(self.command, self.profile.cwd)
            self.connections.add(rpc)
            return rpc

    def _close_connection(self, rpc):
        with self.lock:
            if rpc in self.connections:
                rpc.close()
                self.connections.discard(rpc)

    def refresh_usage(self):
        rpc = None
        try:
            rpc = self._new_connection()
            self._checked_runtime(rpc.initialize())
            account = rpc.call("account/read", {"refreshToken": False}).get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise ProviderBlocked("subscription account unavailable")
            result = weekly_usage(rpc.call("account/rateLimits/read", {}), time.time())
        except Exception:
            result = {"available": False, "scope": "account", "observed_at": time.time(), "error": "usage_read_failed"}
        finally:
            if rpc is not None:
                try:
                    self._close_connection(rpc)
                except Exception:
                    result = {"available": False, "scope": "account", "observed_at": time.time(), "error": "usage_cleanup_failed"}
        with self.lock:
            if self.closed:
                result = {"available": False, "scope": "account", "observed_at": time.time(), "error": "provider_closed"}
            self.last_usage_display = result
        return dict(result)

    def usage_snapshot(self):
        with self.lock:
            return dict(self.last_usage_display)

    def contract(self):
        return {"kind": "codex-subscription", "model": self.profile.model,
                "effort": self.profile.effort, "service_tier": self.profile.service_tier,
                "cli_version": self.profile.cli_version,
                "instruction_sources_hash": digest(self.profile.instruction_sources),
                "capability_policy": "native-tools-denied-explicit-job-tools-v4",
                "tool_catalog": self.catalog.contract() if self.catalog is not None else None}

    def _release(self, rpc):
        self._close_connection(rpc)
        self.local.rpc = None

    def _connection(self):
        with self.lock:
            if self.closed:
                raise ProviderBlocked("provider is closed")
        if self.catalog is None:
            raise ProviderBlocked("restricted role tool catalog is required")
        self.catalog.verify()
        rpc = getattr(self.local, "rpc", None)
        if rpc is None:
            rpc = self._new_connection()
            self.local.rpc = rpc
            self._checked_runtime(rpc.initialize())
        return rpc

    def _checked_runtime(self, response):
        version = re.search(r"^kagebunshin/([^ ]+) ", response.get("userAgent", ""))
        if version is None or version.group(1) != self.profile.cli_version:
            raise ProviderBlocked("Codex CLI version needs new compatibility acceptance")

    def _check_features(self, response):
        features = {x["name"]: x.get("enabled") for x in response.get("data", [])}
        # The official runtime normalizes unified_exec on. Shell-tool availability
        # is separately gated by ShellTool and the turn's environment count.
        required = DISABLED - {"unified_exec"}
        if any(features.get(k) is not False for k in required):
            raise ProviderBlocked("runtime capability gates are missing or enabled")
        self.effective_features = {k: features.get(k) for k in sorted(DISABLED)}

    def _check_config(self, response: dict):
        conf = response["config"]
        p = self.profile
        required = {"model": p.model, "model_provider": "openai", "model_reasoning_effort": p.effort,
                    "service_tier": p.service_tier, "forced_login_method": "chatgpt",
                    "approval_policy": "never", "approvals_reviewer": "user", "web_search": "disabled"}
        if any(conf.get(k) != v for k, v in required.items()):
            raise ProviderBlocked("effective Codex profile differs from accepted profile")
        if conf.get("agents", {}).get("enabled") is not False:
            raise ProviderBlocked("native delegation must be disabled")
        if any(conf.get("features", {}).get(k) is not False for k in DISABLED):
            raise ProviderBlocked("required feature override missing")
        if any(v.get("enabled") is not False for v in conf.get("mcp_servers", {}).values()):
            raise ProviderBlocked("configured MCP server remains enabled")
        if self.catalog is None or conf.get("model_catalog_json") != str(self.catalog.path):
            raise ProviderBlocked("restricted model tool catalog was not applied")
        for name in ("skills", "mcp"):
            if conf.get("orchestrator", {}).get(name, {}).get("enabled") is not False:
                raise ProviderBlocked("orchestrator capabilities must be disabled")
        if conf.get("skills", {}).get("include_instructions") is not False:
            raise ProviderBlocked("automatic skill instructions must be disabled")
        # This CLI's typed Config.tools projection omits these fields. Verify
        # their winning session-flag layer and origin; absence alone is no proof.
        flags = [x for x in response.get("layers", []) if x.get("name", {}).get("type") == "sessionFlags"]
        if len(flags) != 1 or not isinstance(flags[0].get("version"), str):
            raise ProviderBlocked("tool override provenance is unavailable")
        for name in ("experimental_request_user_input", "update_plan"):
            if (name in conf.get("tools", {}) and conf["tools"][name].get("enabled") is not False):
                raise ProviderBlocked("native interaction tools must be disabled")
            key = "tools." + name + ".enabled"
            origin = response.get("origins", {}).get(key, {})
            if (flags[0].get("config", {}).get("tools", {}).get(name, {}).get("enabled") is not False
                or origin.get("name", {}).get("type") != "sessionFlags"
                or origin.get("version") != flags[0]["version"]):
                raise ProviderBlocked("native interaction tool override is missing or superseded")
        self.catalog.verify()

    def preflight(self) -> None:
        try:
            rpc = self._connection()
            self._check_config(rpc.call("config/read", {"includeLayers": True}))
            self._check_features(rpc.call("experimentalFeature/list", {}))
            account = rpc.call("account/read", {"refreshToken": False}).get("account")
            rates = rpc.call("account/rateLimits/read", {})
            with self.lock:
                self.last_usage_display = weekly_usage(rates, time.time())
            self.last_receipt = included_usage(account, rates, time.time(), model=self.profile.model)
        except Exception as exc:
            rpc = getattr(self.local, "rpc", None)
            if rpc:
                self._release(rpc)
            raise ProviderBlocked("Codex preflight: " + str(exc)) from exc

    def _checked_thread(self, response: dict) -> str:
        p = self.profile
        if (response.get("model") != p.model or response.get("modelProvider") != "openai"
            or response.get("reasoningEffort") != p.effort or response.get("serviceTier") != p.service_tier
            or response.get("approvalPolicy") != "never" or response.get("approvalsReviewer") != "user"):
            raise ProviderBlocked("thread profile differs from accepted profile")
        sandbox = response.get("sandbox", {})
        thread = response.get("thread", {})
        if response.get("instructionSources", []) != list(p.instruction_sources):
            raise ProviderBlocked("loaded instruction sources differ from the accepted profile")
        if sandbox.get("type") != "readOnly" or sandbox.get("networkAccess") is not False or thread.get("environments") != []:
            raise ProviderBlocked("empty-environment read-only contract was not applied")
        if not isinstance(thread.get("id"), str) or not thread["id"]:
            raise ProviderBlocked("missing thread identity")
        return thread["id"]

    def archive_owned(self, thread_id: str, turn_id: str | None) -> None:
        """Host-only lifecycle action; IDs come from the saved run, not model text."""
        rpc = self._connection()
        try:
            thread = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread", {})
            if thread.get("id") != thread_id:
                raise ProviderBlocked("owned task identity mismatch")
            if thread.get("historyMode") == "paginated":
                # Descending pages: reverse only after collecting the bounded full history.
                turns = list(reversed(self._pages(rpc, "thread/turns/list", {
                    "threadId": thread_id, "itemsView": "full", "sortDirection": "desc", "limit": 50})))
            else:
                thread = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": True}).get("thread", {})
                turns = thread.get("turns")
            if thread.get("id") != thread_id or not isinstance(turns, list):
                raise ProviderBlocked("owned task history is not confirmed")
            if turn_id is None:
                if turns:
                    raise ProviderBlocked("unsent owned task has unexpected turns")
            elif (not turns or turns[-1].get("id") != turn_id
                  or any(t.get("status") not in {"completed", "failed", "interrupted"} for t in turns)):
                raise ProviderBlocked("owned task is not confirmed terminal")
            try:
                if rpc.call("thread/archive", {"threadId": thread_id}) != {}:
                    raise ProviderBlocked("archive acknowledgment missing")
            except Exception:
                # A lost acknowledgement or a previous archive is not a reason
                # to regenerate. Confirm this exact owned ID in archived history.
                cursor = None
                for _ in range(10):
                    page = rpc.call("thread/list", {"archived": True, "limit": 100,
                        **({"cursor": cursor} if cursor else {})})
                    if any(t.get("id") == thread_id for t in page.get("data", [])):
                        return
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                raise ProviderBlocked("archive completion not confirmed")
        finally:
            self._release(rpc)

    def _unsent(self, rpc, thread_id: str) -> Result:
        """Only called on the creating connection before turn/start is attempted."""
        cleanup_state = None
        try:
            rows = self._pages(rpc, "thread/turns/list", {
                "threadId": thread_id, "itemsView": "full", "sortDirection": "desc", "limit": 50})
            if rows == []:
                self.archive_owned(thread_id, None)
                cleanup_state = "archived"
        except RPCRejected as exc:
            expected = f"thread {thread_id} is not materialized yet; thread/turns/list is unavailable before first user message"
            if exc.code == -32600 and exc.detail == expected:
                cleanup_state = "released_unmaterialized"
        except Exception:
            pass
        # Do not report release before the owned process is confirmed gone.
        try:
            self._release(rpc)
            if rpc.process.poll() is None:
                cleanup_state = None
        except Exception:
            cleanup_state = None
        return Result("not_sent", thread_id=thread_id, cleanup_state=cleanup_state)

    def execute(self, request: Request) -> Result:
        # Recheck on the same connection immediately before starting a role.
        self.preflight()
        rpc = self.local.rpc
        p = self.profile
        dispatched, thread_id, turn_id = False, None, None
        try:
            tools=request.tool_session
            definitions=None
            if tools is not None:
                tools.validate_request(request.attempt_id,request.run_id,request.node_id,request.binding_hash)
                definitions=tools.definitions()
                rpc.configure_dynamic_tools(definitions)
            response = rpc.call("thread/start", {
                "model": p.model, "modelProvider": "openai", "allowProviderModelFallback": False,
                "serviceTier": p.service_tier, "cwd": str(p.cwd), "approvalPolicy": "never",
                "approvalsReviewer": "user", "sandbox": "read-only", "environments": [],
                "selectedCapabilityRoots": [], "ephemeral": False,
                "serviceName": "kagebunshin", "baseInstructions": ("You are a bounded Kagebunshin specialist."
                    if tools is not None else "You are a text-only bounded Kagebunshin specialist."),
                "developerInstructions": ("Use only supplied data and the explicitly provided Kagebunshin tools. No other filesystem, external service, delegation or configuration changes. Tool outcomes marked unknown must not be retried as new calls."
                    if tools is not None else "Use only supplied data. No tools, filesystem, external service, delegation or configuration changes."),
                **({'dynamicTools':definitions} if tools is not None else {}),
            })
            thread_id = self._checked_thread(response)
            if request.progress and not request.progress(thread_id, None):
                return self._unsent(rpc, thread_id)
            if request.may_continue and not request.may_continue():
                return self._unsent(rpc, thread_id)
            # Any exception from this point may follow a received turn/start.
            dispatched = True
            turn = rpc.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": request.prompt}],
                                           "model": p.model, "effort": p.effort, "serviceTier": p.service_tier,
                                           "environments": [], "approvalPolicy": "never", "approvalsReviewer": "user",
                                           **({"outputSchema": request.output_schema} if request.output_schema is not None else {})})["turn"]
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise ProviderUnknown("turn identity missing")
            if request.progress:
                request.progress(thread_id, turn_id)
            if tools is not None:
                tools.bind(thread_id,turn_id)
            return self._wait(rpc, request, thread_id, turn_id)
        except Exception as exc:
            if dispatched:
                if thread_id and turn_id:
                    try:
                        rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    except Exception:
                        pass
                return Result("unknown", thread_id=thread_id, turn_id=turn_id, diagnostic_code="provider_exception")
            raise ProviderBlocked("Codex role not dispatched: " + str(exc)) from exc
        finally:
            self._release(rpc)

    def _wait(self, rpc, request, thread_id, turn_id):
        deadline = time.monotonic() + self.profile.max_seconds
        items = {}
        with self.lock:
            self.observed_item_types[request.attempt_id] = set()
        interrupted = False
        while time.monotonic() < deadline:
            if request.may_continue and not request.may_continue() and not interrupted:
                rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                interrupted = True
            if rpc.notifications:
                message = rpc.notifications.pop(0)
            else:
                try:
                    message = rpc.receive(min(deadline, time.monotonic() + 1))
                except ProviderUnknown as exc:
                    if str(exc) == "stdio deadline exceeded":
                        continue
                    raise
            method, params = message.get("method"), message.get("params", {})
            if method=='item/tool/call' and 'id' in message:
                if request.tool_session is None or interrupted or (request.may_continue and not request.may_continue()):
                    raise ProviderUnknown('tool call arrived outside active execution')
                result=request.tool_session.handle(params)
                rpc.respond_tool(message['id'],result)
                continue
            if method == "account/rateLimits/updated":
                # A sparse update triggers a complete snapshot on this connection.
                account = rpc.call("account/read", {"refreshToken": False}).get("account")
                rates = rpc.call("account/rateLimits/read", {})
                included_usage(account, rates, time.time(), model=self.profile.model)
                continue
            if method in {"account/updated", "configWarning"}:
                rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                return Result("unknown", thread_id=thread_id, turn_id=turn_id, diagnostic_code="provider_configuration_changed")
            observed_turn = params.get("turnId") or params.get("turn", {}).get("id")
            if params.get("threadId") != thread_id or observed_turn != turn_id:
                continue
            if method in {"item/started", "item/completed"}:
                item = params.get("item", {})
                with self.lock:
                    self.observed_item_types[request.attempt_id].add(item.get("type", "missing"))
                allowed_types={"userMessage", "agentMessage", "reasoning"}
                if request.tool_session is not None:
                    allowed_types.add('dynamicToolCall')
                if item.get("type") not in allowed_types:
                    rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    return Result("unknown", thread_id=thread_id, turn_id=turn_id, diagnostic_code="unexpected_item_type")
                if method == "item/completed" and item.get("type") == "agentMessage":
                    if item["id"] in items and items[item["id"]] != item:
                        rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                        return Result("unknown", thread_id=thread_id, turn_id=turn_id, diagnostic_code="conflicting_agent_message")
                    items[item["id"]] = item
            if method == "turn/completed":
                # Some versions identify the turn in params.turn, not params.turnId.
                return self._terminal(params.get("turn", {}), items.values(), thread_id, turn_id)
        rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return Result("unknown", thread_id=thread_id, turn_id=turn_id, diagnostic_code="provider_deadline")

    @staticmethod
    def _terminal(turn, items, thread_id, turn_id):
        items = list(items)
        if turn.get("id") != turn_id or turn.get("status") not in {"completed", "failed", "interrupted"}:
            return Result("unknown", thread_id=thread_id, turn_id=turn_id)
        if turn["status"] != "completed":
            return Result(turn["status"], thread_id=thread_id, turn_id=turn_id)
        if any(i.get("type") not in {"userMessage", "agentMessage", "reasoning"} for i in items):
            return Result("unknown", thread_id=thread_id, turn_id=turn_id)
        finals = [i for i in items if i.get("type") == "agentMessage" and i.get("phase") == "final_answer"]
        if len(finals) != 1:
            return Result("unknown", thread_id=thread_id, turn_id=turn_id)
        try:
            text = finals[0]["text"]
            if len(text.encode()) > 100_000:
                raise ValueError()
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            def invalid_constant(value):
                raise ValueError("nonfinite JSON")
            payload = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
            if not isinstance(payload, dict):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            # Normal provider completion with an invalid candidate is a loop defect.
            payload = {"invalid_output": str(finals[0].get("text", ""))[:100_000]}
        return Result("completed", payload, thread_id, turn_id)

    def reconcile(self, attempt: dict) -> Result | None:
        # An owned thread is created per attempt. A lost turn/start response can
        # only be recovered by one uniquely matching persisted user message.
        if not attempt.get("thread_id"):
            return None
        rpc = self.rpc_factory(self.command, self.profile.cwd)
        try:
            self._checked_runtime(rpc.initialize())
            deadline = time.monotonic() + 60
            response = rpc.call("thread/read", {"threadId": attempt["thread_id"], "includeTurns": False}, deadline=deadline)
            thread = response.get("thread", {})
            if thread.get("id") != attempt["thread_id"]:
                return None
            if thread.get("historyMode") == "paginated":
                turns = self._pages(rpc, "thread/turns/list", {"threadId": attempt["thread_id"], "itemsView": "full", "sortDirection": "desc", "limit": 50}, deadline)
            else:
                full = rpc.call("thread/read", {"threadId": attempt["thread_id"], "includeTurns": True}, deadline=deadline)
                if full.get("thread", {}).get("id") != attempt["thread_id"]:
                    return None
                turns = full.get("thread", {}).get("turns", [])
            unique_turns = {}
            for t in turns:
                if not isinstance(t, dict) or not isinstance(t.get("id"), str):
                    return None
                if t["id"] in unique_turns and unique_turns[t["id"]] != t:
                    return None
                unique_turns[t["id"]] = t
            turns = list(unique_turns.values())
            if not attempt.get("turn_id"):
                if len(turns) != 1:
                    return None
                turn = turns[0]
            else:
                turn = next((t for t in turns if t.get("id") == attempt["turn_id"]), None)
            if not turn:
                return None
            items = turn.get("items", [])
            if turn.get("itemsView", "full") != "full":
                entries = self._pages(rpc, "thread/items/list", {"threadId": attempt["thread_id"], "turnId": turn["id"], "sortDirection": "asc", "limit": 100}, deadline)
                if any(e.get("turnId") != turn["id"] for e in entries):
                    return None
                items = [e["item"] for e in entries]
            if not attempt.get("turn_id") or attempt.get("prompt_hash"):
                texts = [i.get("content", []) for i in items if i.get("type") == "userMessage"]
                if len(texts) != 1 or len(texts[0]) != 1 or texts[0][0].get("type") != "text" or digest(texts[0][0].get("text")) != attempt.get("prompt_hash"):
                    return None
            unique = {}
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    return None
                if item["id"] in unique and unique[item["id"]] != item:
                    return None
                unique[item["id"]] = item
            return self._terminal(turn, unique.values(), attempt["thread_id"], turn["id"])
        finally:
            rpc.close()

    @staticmethod
    def _pages(rpc, method, params, deadline=None):
        deadline = deadline or time.monotonic() + 60
        cursor, seen, rows = None, set(), []
        for _ in range(50):
            if time.monotonic() >= deadline:
                raise ProviderUnknown("history deadline exceeded")
            page = rpc.call(method, {**params, **({"cursor": cursor} if cursor else {})}, deadline=deadline)
            if time.monotonic() >= deadline:
                raise ProviderUnknown("history deadline exceeded")
            if not isinstance(page.get("data"), list):
                raise ProviderUnknown("malformed history page")
            rows.extend(page["data"])
            if len(rows) > 5000:
                raise ProviderUnknown("history exceeds bound")
            cursor = page.get("nextCursor")
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ProviderUnknown("invalid or cyclic history cursor")
            seen.add(cursor)
        raise ProviderUnknown("history page limit exceeded")
