#!/usr/bin/env python3
"""Explicit bounded product acceptance. Uses only checked included Codex usage."""

from datetime import datetime, timezone
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.application.team import Team
from multi_shadow_clone.orchestration.domain.contracts import Limits, Node, Plan, Rule
from multi_shadow_clone.orchestration.infrastructure.catalog import load_roles
from multi_shadow_clone.orchestration.infrastructure.codex_provider import CodexProfile, CodexProvider
from multi_shadow_clone.orchestration.infrastructure.codex_rpc import StdioRPC, codex_command
from multi_shadow_clone.orchestration.infrastructure.sqlite_store import SQLiteRunStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["arithmetic", "team", "team-repaired", "team-v2"], default="arithmetic")
    case = parser.parse_args().case
    max_turns = 2 if case == "arithmetic" else (7 if case == "team-repaired" else 8)
    output = ROOT / f"evidence/foundation/live-{case}.json"
    if output.exists():
        raise SystemExit("This acceptance has a receipt. Review it before creating another attempt.")
    verification = json.loads((ROOT / "evidence/foundation/test-results.json").read_text())
    if verification.get("success") is not True:
        raise SystemExit("Current verification did not pass. No live turn will be submitted.")
    for name, expected in verification["source_sha256"].items():
        if sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise SystemExit("Source changed after verification: " + name)
    if case == "team-repaired":
        previous = json.loads((ROOT / "evidence/foundation/live-team.json").read_text())
        if previous.get("state") != "stopped" or previous.get("turn_reservations") != 1 or any(a.get("terminal_status") != "interrupted" for a in previous.get("attempts", [])):
            raise SystemExit("The previous one-turn pilot has not been confirmed interrupted.")
    def closure_hashes():
        paths = [*(ROOT / "src/multi_shadow_clone/orchestration").rglob("*.py"),
                 ROOT / "src/multi_shadow_clone/orchestration/roles.json", Path(__file__).resolve()]
        return {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    initial_hashes = closure_hashes()
    binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    with StdioRPC(codex_command(binary), ROOT) as rpc:
        rpc.initialize()
        conf = rpc.call("config/read", {"includeLayers": False})["config"]
        mcp_names = tuple(conf.get("mcp_servers", {}))
    receipt = {"at": datetime.now(timezone.utc).isoformat(), "scope": "product " + case + " acceptance, synthetic data only; not a held-out expertise evaluation",
               "maximum_turn_submissions": max_turns, "purchases": 0, "other_provider_calls": 0,
               "research_subagents": 0, "worktrees_created": 0, "source_sha256_at_start": initial_hashes,
               "hash_scope": "orchestration implementation, role catalog and this live entrypoint; unrelated contexts excluded",
               "test_receipt_sha256": sha256((ROOT / "evidence/foundation/test-results.json").read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix="multi-shadow-clone-live-contract-") as tmp:
        provider = CodexProvider(CodexProfile(binary, Path(tmp)), mcp_names)
        store = SQLiteRunStore(ROOT / "runtime/foundation-live/jobs.sqlite3")
        engine = Engine(store, provider, load_roles(), time.time)
        try:
            provider.preflight()
            receipt["preflight"] = provider.last_receipt
            node = Node("arithmetic", "R19", "Compute the arithmetic mean of the supplied synthetic numbers. Return values.mean as integer 5 if computed correctly. Explain the calculation and its limits. Use no tools.",
                        source_ids=("numbers",), rules=(Rule("values.mean", "equals", 5),), audit_role="R12", max_attempts=1)
            if case == "arithmetic":
                run_id = engine.create(Plan("Verify a bounded specialist and independent audit using Codex included usage.",
                                        (node,), {"numbers": "Synthetic numbers: [2,4,4,4,5,5,7,9]. Sum 40; count 8."},
                                        Limits(max_turns=2, max_concurrent=1, deadline_seconds=420)))
            else:
                run_id = Team(engine).submit(
                    "2つの専門担当を名簿から選んでください。一人はnumbersの平均を計算し、一人はstudyの主張と根拠の限界を点検します。独立した2つの成果が合格した後、統合担当が要点を短く日本語でまとめ、values.meanも返してください。監査はcontrollerが付けるので監査ノードを計画に増やさないでください。専門2ノードと統合1ノードを基本にします。",
                    {"numbers": "合成データ：[2,4,4,4,5,5,7,9]。これは説明用の8個の数値。",
                     "study": "合成の観測記録：試作品Aを10人、Bを15人が選んだ。無作為割付はなく、母集団も未定義。Bが人気を高めたという因果効果は、この人数だけでは判定できない。"},
                    Limits(max_turns=max_turns, max_concurrent=2, deadline_seconds=1200), (Rule("values.mean", "equals", 5),))
            receipt["run_id"] = run_id
            output.write_text(json.dumps({**receipt, "state": "started"}, indent=2) + "\n")
            print(json.dumps({"state": "started", "max_turns": max_turns, "mode": "subscription-only"}), flush=True)
            while engine.step(run_id):
                state = engine.status(run_id)
                print(json.dumps({"state": state["state"], "turn_reservations": state["turns"],
                                  "nodes": {k:v["state"] for k,v in state["nodes"].items()} }), flush=True)
            result = engine.status(run_id)
            if case != "arithmetic" and result.get("planning", {}).get("stage") == "proposal" and result["state"] == "completed":
                Team(engine)._admit(run_id)
                print(json.dumps({"state": "plan_admitted", "roles": [n['role_id'] for n in engine.status(run_id)['plan']['nodes']]}),flush=True)
                def worker():
                    while engine.step(run_id):
                        state = engine.status(run_id)
                        print(json.dumps({"state":state["state"],"turn_reservations":state["turns"],"nodes":{k:v["state"] for k,v in state["nodes"].items()}}),flush=True)
                if case == "team-v2":
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=2) as workers:
                        futures = [workers.submit(worker) for _ in range(2)]
                        for future in futures:
                            future.result()
                else:
                    worker()
                result = engine.status(run_id)
            receipt.update(state=result["state"], turn_reservations=result["turns"],
                           nodes=result["nodes"], attempts=result["attempts"], events=result["events"])
            if "planning" in result:
                receipt['planning'] = result['planning']
        except Exception as exc:
            receipt.update(state="blocked", reason=str(exc))
        finally:
            provider.close()
    receipt["owned_processes_stopped"] = True
    receipt["temporary_directory_removed"] = True
    receipt["source_sha256_at_end"] = closure_hashes()
    receipt["source_unchanged_during_run"] = receipt["source_sha256_at_start"] == receipt["source_sha256_at_end"]
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"state":receipt["state"], "turn_reservations":receipt.get("turn_reservations",0),
                      "reason":receipt.get("reason"), "receipt":str(output)}),flush=True)


if __name__ == "__main__":
    main()
