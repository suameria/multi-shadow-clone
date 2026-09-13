"""Truthful operator projection: a role definition is not a live worker."""
from ..domain.contracts import InvalidContract


class Overview:
    def __init__(self, engine, clock, engine_for_run=None):
        self.engine, self.clock = engine, clock
        self.engine_for_run = engine_for_run

    def snapshot(self):
        now = self.clock()
        roles = {r.id: {"id": r.id, "name": r.name, "group": r.group, "lifecycle": r.lifecycle,
                        "trigger": r.trigger, "output": r.output, "behavior": r.behavior,
                        "activity": "idle", "running": 0, "unknown": 0} for r in self.engine.roles.values()}
        jobs = []
        for item in self.engine.list_runs():
            run = self.engine.status(item["id"])
            unavailable = False
            try:
                engine = self.engine_for_run(item["id"]) if self.engine_for_run else self.engine
            except InvalidContract:
                engine = self.engine
                unavailable = True
            active = []
            for attempt in run["attempts"]:
                if attempt["state"] == "terminal":
                    continue
                age = now - attempt.get("heartbeat_at", 0)
                fresh = (not run["stopped"] and attempt["state"] in {"running", "starting"}
                         and attempt["epoch"] == run["stop_epoch"] and 0 <= age <= 15)
                activity = "running" if fresh else "unknown"
                role = roles.get(attempt["role_id"])
                if role:
                    role[activity] += 1
                    role["activity"] = "running" if role["running"] else "unknown"
                active.append({"role_id": attempt["role_id"], "node_id": attempt["node_id"],
                               "activity": "stopping" if run["stopped"] else activity,
                               "heartbeat_age_seconds": max(0, round(age, 1))})
            acceptance = ({"compatible": False, "reason": "settings_unavailable"}
                          if unavailable else engine.execution_status(run["id"]))
            depended = {d for n in self._nodes(run) for d in n.dependencies}
            results = []
            if acceptance["compatible"] and not run["stopped"]:
                for node in self._nodes(run):
                    if node.id not in depended and run["nodes"][node.id]["state"] == "accepted":
                        result = engine.accepted_result(run["id"], node.id)
                        results.append({"node_id": node.id, "output": result["output"]})
            cleanup = [{"thread_id": thread, "state": value["state"]}
                       for thread, value in run.get("task_cleanup", {}).items()]
            can_cleanup = (not unavailable and run["state"] != "running" and (callable(getattr(engine.provider, "archive_owned", None)) or callable(getattr(engine.provider, "archive_for", None)))
                           and any(a.get("state") == "terminal" and a.get("thread_id")
                                   and run.get("task_cleanup", {}).get(a["thread_id"], {}).get("state")
                                   not in {"archived", "released_unmaterialized"} for a in run["attempts"]))
            retirement=run.get("workspace_retirement")
            can_retire=(not unavailable and engine.tool_sessions is not None
                        and bool(run.get("execution_policy")) and not active
                        and all(a["state"]=="terminal" for a in run["attempts"])
                        and (retirement or {}).get("state")!="retired")
            jobs.append({"workspace_retirement": retirement, "can_retire_workspace": can_retire,
                         "workspaces": sorted({s["workspace_id"] for s in run.get("execution_policy",{}).get("scopes",[])}),
                         "cleanup": cleanup, "can_cleanup": can_cleanup,"id": run["id"], "objective": run["plan"]["objective"], "state": run["state"],
                         "execution_available": not unavailable,
                         "next_step": ("この仕事の保存済みモデル設定を復元できません。履歴と停止は利用できますが、実行・照合・後片付けには元の設定の復元が必要です。" if unavailable else self.next_step(run)),
                         "can_resume": not retirement and bool(run["stopped"] or (run["state"] == "blocked_preflight" and not active)),
                         "acceptance": acceptance, "results": results, "erased": bool(run.get("erased")),
                         "stopped": run["stopped"], "turns": run["turns"], "limits": run["plan"]["limits"],
                         "provider": run.get("provider_contract", {"kind": "historical"}),
                         "execution_settings": run.get("execution_settings"),
                         "nodes": [{"id": n.id, "role_id": n.role_id, "audit_role": n.audit_role, "state": run["nodes"][n.id]["state"],
                                    "dependencies": list(n.dependencies)} for n in self._nodes(run)],
                         "active": active})
        return {"at": now, "heartbeat_expiry_seconds": 15, "roles": list(roles.values()), "jobs": jobs,
                "live_roles": sum(r["running"] > 0 for r in roles.values()),
                "unknown_attempts": sum(r["unknown"] for r in roles.values())}

    @staticmethod
    def next_step(run):
        retirement = run.get("workspace_retirement", {}).get("state")
        if retirement == "retired":
            return "作業場所の退役を確認しました。この仕事は再開できません。記録を確認し、続きは新しい仕事として準備してください。"
        if retirement == "retiring":
            return "作業場所の退役を開始しました。再開はできません。片付けが中断した場合は退役を再試行してください。"
        guidance = {
            "blocked_preflight": "通常枠・認証・対応版・設定を確認できず停止しました。前提が戻ったら「停止を解除」、続いて「実行」で再確認します。条件を確認できるまで送信しません。",
            "paused_budget": "この依頼の送信上限に達しました。再起動や停止解除で回数は増えません。成果と残件を確認し、続きは新しい依頼として保存してください。",
            "paused_deadline": "この依頼の期限に達しました。再起動や停止解除で期限は延びません。成果と残件を確認してください。",
            "blocked": "完了できない担当があります。合格した担当の成果と、不合格・不明の状態を確認してください。",
        }
        if run["stopped"]:
            return "停止を受け付けました。不明な外部実行があれば照合を続けます。停止解除だけでは新しい処理を始めません。"
        return guidance.get(run["state"], "")

    @staticmethod
    def _nodes(run):
        from ..domain.contracts import plan_from_dict
        return plan_from_dict(run["plan"]).nodes
