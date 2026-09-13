"""Bounded stdio JSON-RPC; never forwards arbitrary commands from model output."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from ..ports import ProviderBlocked, ProviderUnknown


ALLOWED_METHODS = frozenset({"initialize", "config/read", "account/read", "account/rateLimits/read",
                           "model/list", "experimentalFeature/list", "thread/start", "thread/read",
                           "thread/archive", "thread/list", "thread/turns/list", "thread/items/list", "turn/start", "turn/interrupt"})

# Recovery reads owned history; it never restores a prior tool-bearing thread.
# Mutating requests are constructed by the adapter, not accepted as raw model
# output. Keep their extension points closed if a future caller adds fields.
MUTATION_FIELDS = {
    "thread/archive": frozenset({"threadId"}),
    "thread/start": frozenset({"model", "modelProvider", "allowProviderModelFallback", "serviceTier", "cwd",
                               "approvalPolicy", "approvalsReviewer", "sandbox", "environments",
                               "selectedCapabilityRoots", "ephemeral", "serviceName", "baseInstructions",
                               "developerInstructions"}),
    "turn/start": frozenset({"threadId", "input", "model", "effort", "serviceTier", "environments",
                             "approvalPolicy", "approvalsReviewer", "outputSchema"}),
    "turn/interrupt": frozenset({"threadId", "turnId"}),
}

DISABLED_FEATURES = frozenset({
    "hooks", "plugins", "apps", "multi_agent", "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot",
    "js_repl", "image_generation", "computer_use", "tool_suggest", "remote_plugin", "skill_search",
    "skill_mcp_dependency_install", "view_image", "sleep_tool", "workspace_dependencies", "goals",
    "auth_elicitation", "memories", "code_mode_host", "code_mode", "code_mode_only", "code_mode_prewarm",
    "browser_use", "browser_use_full_cdp_access", "browser_use_external", "in_app_browser",
    "in_app_local_automation", "plugin_sharing", "tool_call_mcp_elicitation", "unbounded_connection_retries",
    "multi_agent_v2", "send_async_message", "request_permissions_tool", "standalone_web_search",
    "search_tool", "recommended_plugins",
    "current_time_reminder", "token_budget", "deferred_executor", "enable_mcp_apps",
    "context_management", "rollout_budget", "artifact",
})


class RPCRejected(ProviderBlocked):
    """Wire details stay out of ordinary logs; callers may inspect for diagnosis."""
    def __init__(self, method: str, error: dict):
        super().__init__("RPC rejected: " + method)
        self.code = error.get("code")
        self.detail = error.get("message", "")


def codex_command(binary: Path) -> list[str]:
    args = [str(binary), "app-server", "--stdio"]
    for name in sorted(DISABLED_FEATURES):
        args.extend(["--disable", name])
    for value in ('forced_login_method="chatgpt"', 'model_provider="openai"', 'agents.enabled=false',
                  'web_search="disabled"', 'approval_policy="never"', 'approvals_reviewer="user"', 'mcp_servers={}',
                  'orchestrator.skills.enabled=false', 'orchestrator.mcp.enabled=false',
                  'skills.include_instructions=false', 'tools.experimental_request_user_input.enabled=false',
                  'tools.update_plan.enabled=false'):
        args.extend(["-c", value])
    return args


def host_environment():
    """Child process essentials; no API keys, proxy, diagnostics or provider env."""
    return {k: v for k, v in os.environ.items() if k in {
        "HOME", "PATH", "TMPDIR", "LANG", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR"}}


class StdioRPC:
    def __init__(self, command: list[str], cwd: Path, timeout: float = 25):
        # Only host essentials; never propagate API keys or arbitrary provider env.
        env = host_environment()
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.notifications = []
        self.next_id = 1
        self.timeout = timeout
        self.methods = []
        self.dynamic_tools = None
        self.pending_tools = {}
        self.seen_tool_requests = set()

    def configure_dynamic_tools(self, definitions):
        if self.dynamic_tools is not None or 'thread/start' in self.methods:
            raise ProviderBlocked('dynamic tools must be fixed before thread creation')
        allowed={'multi_shadow_clone_read_files','multi_shadow_clone_apply_changes','multi_shadow_clone_run_check'}
        if type(definitions) is not list or not 0<len(definitions)<=3:
            raise ProviderBlocked('invalid dynamic tool declarations')
        names=set()
        for definition in definitions:
            if (type(definition) is not dict or set(definition)!={'type','name','description','inputSchema'}
                or definition['type']!='function' or type(definition['name']) is not str
                or definition['name'] not in allowed or definition['name'] in names
                or type(definition['description']) is not str or type(definition['inputSchema']) is not dict):
                raise ProviderBlocked('dynamic tool declaration is outside owned capabilities')
            names.add(definition['name'])
        encoded=json.dumps(definitions,sort_keys=True,allow_nan=False)
        if len(encoded.encode())>65536:
            raise ProviderBlocked('dynamic tool declaration exceeds bound')
        self.dynamic_tools=encoded

    def respond_tool(self,request_id,result):
        if type(request_id) not in (str,int) or request_id not in self.pending_tools:
            raise ProviderBlocked('dynamic tool response has no pending request')
        if (type(result) is not dict or set(result)!={'success','contentItems'}
            or type(result['success']) is not bool or type(result['contentItems']) is not list
            or len(result['contentItems'])!=1):
            raise ProviderBlocked('invalid dynamic tool response')
        item=result['contentItems'][0]
        if (type(item) is not dict or set(item)!={'type','text'} or item['type']!='inputText'
            or type(item['text']) is not str or len(item['text'].encode())>1048576):
            raise ProviderBlocked('dynamic tool response is not bounded text')
        self._send({'id':request_id,'result':result})
        del self.pending_tools[request_id]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()

    def _send(self, message: dict):
        try:
            data = memoryview((json.dumps(message, allow_nan=False) + "\n").encode())
            while data:
                count = self.process.stdin.write(data)
                if not count:
                    raise OSError("zero byte write")
                data = data[count:]
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProviderUnknown("stdio write outcome unknown") from exc

    def initialize(self):
        result = self.call("initialize", {"clientInfo": {"name": "multi-shadow-clone", "version": "0.1.0"},
                                          "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized", "params": {}})
        return result

    def _message(self, deadline: float) -> dict:
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(timeout=remaining):
                raise ProviderUnknown("stdio deadline exceeded")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ProviderUnknown("stdio ended")
            self.buffer += chunk
            if len(self.buffer) > 2_000_000:
                raise ProviderUnknown("stdio message exceeds bound")
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            def pairs(items):
                obj = {}
                for key, value in items:
                    if key in obj:
                        raise ValueError("duplicate key")
                    obj[key] = value
                return obj
            def invalid_constant(value):
                raise ValueError("nonfinite JSON")
            message = json.loads(line, object_pairs_hook=pairs, parse_constant=invalid_constant)
            if not isinstance(message, dict):
                raise ValueError("not an object")
            return message
        except (ValueError, UnicodeError) as exc:
            raise ProviderUnknown("malformed stdio JSON") from exc

    def receive(self, deadline: float) -> dict:
        message = self._message(deadline)
        if "method" in message and "id" in message:
            if self.dynamic_tools is not None and message['method']=='item/tool/call':
                ident=message['id']
                params=message.get('params')
                names={item['name'] for item in json.loads(self.dynamic_tools)}
                if (type(ident) not in (str,int) or ident in self.seen_tool_requests
                    or len(self.seen_tool_requests)>=128 or len(self.pending_tools)>=32
                    or type(params) is not dict or type(params.get('tool')) is not str
                    or params['tool'] not in names):
                    raise ProviderBlocked('dynamic request is foreign, repeated or exceeds bounds')
                self.seen_tool_requests.add(ident)
                self.pending_tools[ident]=params
                return message
            self._send({"id": message["id"], "error": {"code": -32601, "message": "Client capabilities denied"}})
            raise ProviderBlocked("unexpected server tool or approval request")
        return message

    def call(self, method: str, params: dict, *, deadline: float | None = None) -> dict:
        if method not in ALLOWED_METHODS:
            raise ProviderBlocked("RPC method is outside controller capabilities")
        fields=MUTATION_FIELDS.get(method)
        if method=='thread/start' and self.dynamic_tools is not None:
            fields=fields|{'dynamicTools'}
            if (type(params) is not dict or json.dumps(params.get('dynamicTools'),sort_keys=True,allow_nan=False)!=self.dynamic_tools):
                raise ProviderBlocked('dynamic tool declarations differ from fixed capabilities')
        if not isinstance(params, dict) or (fields is not None and not params.keys() <= fields):
            raise ProviderBlocked("RPC parameters are outside controller capabilities")
        if deadline is not None and time.monotonic() >= deadline:
            raise ProviderUnknown("stdio deadline exceeded")
        ident = self.next_id
        self.next_id += 1
        self.methods.append(method)
        self._send({"id": ident, "method": method, "params": params})
        deadline = min(deadline, time.monotonic() + self.timeout) if deadline is not None else time.monotonic() + self.timeout
        while True:
            message = self.receive(deadline)
            if "method" in message:
                self.notifications.append(message)
                if len(self.notifications) > 10000:
                    raise ProviderUnknown("notification queue exceeds bound")
            elif type(message.get("id")) is not int or message["id"] != ident:
                # This transport has one synchronous outstanding request. A
                # future, repeated or foreign response cannot satisfy it later.
                raise ProviderUnknown("stdio response does not match outstanding request")
            elif "error" in message:
                raise RPCRejected(method, message["error"])
            else:
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ProviderUnknown("invalid RPC result")
                return result
