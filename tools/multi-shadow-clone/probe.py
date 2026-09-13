#!/usr/bin/env python3
"""Read-only subscription/config probe. Creates no thread or model turn."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from multi_shadow_clone.orchestration.domain.subscription import included_usage
from multi_shadow_clone.orchestration.infrastructure.codex_rpc import StdioRPC, codex_command


def main():
    binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    report = {"at": datetime.now(timezone.utc).isoformat(), "model_turns": 0,
              "thread_creations": 0, "purchases": 0, "persistent_config_changes": 0,
              "capability_enforcement_verified": False}
    with StdioRPC(codex_command(binary), ROOT) as rpc:
        try:
            init = rpc.initialize()
            report["runtime"] = init.get("userAgent")
            account = rpc.call("account/read", {"refreshToken": False}).get("account")
            rates = rpc.call("account/rateLimits/read", {})
            conf = rpc.call("config/read", {"includeLayers": False})["config"]
            report["ordinary_usage_allowed"] = rates.get("ordinaryUsageAllowed")
            report["rate_buckets"] = {key: {k: value.get(k) for k in ("limitId", "normalModelSlug", "limitName", "planType", "spendControlReached", "credits", "primary", "secondary")}
                                      for key, value in (rates.get("rateLimitsByLimitId") or {}).items()}
            try:
                report["included_usage"] = included_usage(account, rates, time.time(), model=conf.get("model"))
            except Exception as exc:
                report["included_usage_blocked"] = str(exc)
            report["config"] = {k: conf.get(k) for k in ("model", "model_provider", "model_reasoning_effort", "service_tier", "forced_login_method", "approval_policy", "approvals_reviewer", "web_search")}
            report["config"]["features"] = conf.get("features")
            report["config"]["agents_enabled"] = conf.get("agents", {}).get("enabled")
            report["mcp_server_count"] = len(conf.get("mcp_servers", {}))
            features = rpc.call("experimentalFeature/list", {})
            report["feature_reply_keys"] = list(features)
            report["features"] = [{k: item.get(k) for k in ("name", "enabled", "defaultEnabled", "stage")} for item in features.get("data", [])]
            report["status"] = "read_only_probe_completed"
        except Exception as exc:
            report["status"] = "blocked"
            report["reason"] = str(exc)
        report["methods_sent"] = rpc.methods
        report["notification_kinds"] = sorted({n.get("method") for n in rpc.notifications})
    report["owned_process_stopped"] = rpc.process.poll() is not None
    path = ROOT / "evidence" / "foundation" / "codex-read-only-probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report.get(k) for k in ("status", "reason", "included_usage_blocked", "rate_buckets", "mcp_server_count", "model_turns", "owned_process_stopped")}, ensure_ascii=False, indent=2))
    print(json.dumps([f for f in report.get("features", []) if f["name"] in {"unified_exec", "code_mode_host", "computer_use", "multi_agent", "plugins", "apps"}], indent=2))


if __name__ == "__main__":
    main()
