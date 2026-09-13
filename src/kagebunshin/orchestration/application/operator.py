"""Operator use cases; HTTP and host scheduling stay behind their boundaries."""

from .overview import Overview
from ..domain.contracts import Limits


class Operator:
    def __init__(self, engine, team, jobs, clock, delivery=None, settings=None, configured_jobs=None):
        self.engine, self.team, self.jobs = engine, team, jobs
        self.overview = Overview(engine, clock, configured_jobs.for_run if configured_jobs else None)
        self.delivery = delivery
        self.clock = clock
        self.settings = settings
        self.configured_jobs = configured_jobs

    def snapshot(self):
        read_usage = getattr(self.engine.provider, "usage_snapshot", None)
        usage = read_usage() if callable(read_usage) else {"available": False, "scope": "account", "observed_at": None}
        observed = usage.get("observed_at")
        usage = {**usage, "stale": observed is None or not 0 <= self.clock() - observed <= 60}
        return {**self.overview.snapshot(), "driver": self.jobs.status(), "usage": usage,
                "delivery": self.delivery.snapshot() if self.delivery else {"operations": [], "environment_phase": "not_created"}}

    def refresh_usage(self):
        refresh = getattr(self.engine.provider, "refresh_usage", None)
        if not callable(refresh):
            raise ValueError("この実行方式では利用枠を取得できません")
        return refresh()

    def settings_snapshot(self):
        if self.settings is None:
            return {"available": False}
        return {"available": True, **self.settings.read(),
                "catalog": {key: list(value) for key, value in self.settings.catalog.items()},
                "active_contract": self.engine._provider_contract(),
                "application_policy": "frozen per job" if self.configured_jobs else "not connected"}

    def save_settings(self, value, expected_revision):
        if self.settings is None:
            raise ValueError("model settings are unavailable")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid settings revision")
        return self.settings.save(value, expected_revision)

    def submit(self, objective, source, max_turns):
        if (not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 2000
            or not isinstance(source, str) or len(source) > 12000
            or type(max_turns) is not int or not 3 <= max_turns <= 20):
            raise ValueError("目的は1〜2000字、資料は12000字以内、送信上限は3〜20回で指定してください")
        run_id = (self.configured_jobs or self.team).submit(objective.strip(), {"userMaterial": source} if source.strip() else {},
                                  Limits(max_turns=max_turns, max_concurrent=2, deadline_seconds=1800))
        return {"id": run_id, "state": "saved"}

    def act(self, operation: str, run_id: str):
        # STOP records the shared epoch without needing a model catalog or provider.
        if operation == "stop":
            result = self.engine.stop(run_id)
            return {"id": result["id"], "state": result["state"]}
        engine = self.configured_jobs.for_run(run_id) if self.configured_jobs else self.engine
        state = engine.status(run_id)
        if operation in {"run", "resume"} and not engine.execution_status(run_id)["compatible"]:
            raise ValueError("契約または検査結果が現在と異なります。履歴を確認し、新しい依頼を保存してください")
        if operation == "run":
            if state["stopped"] or state["state"] != "running":
                raise ValueError("現在は実行待ちではありません。仕事に表示された復旧手順を確認してください")
            return self.jobs.start(run_id, lambda: (self.configured_jobs or self.team).advance(run_id), lambda: engine.stop(run_id))
        if operation == "retire-workspace":
            return self.jobs.start(run_id, lambda: engine.retire_workspace(run_id), lambda: None)
        if operation == "cleanup":
            if state["state"] == "running":
                raise ValueError("仕事が終了してから後片付けを再試行してください")
            return self.jobs.start(run_id, lambda: engine.cleanup(run_id), lambda: None)
        if operation == "reconcile":
            return self.jobs.start(run_id, lambda: engine.reconcile(run_id), lambda: engine.stop(run_id))
        if operation in {"stop", "resume"}:
            result = getattr(engine, operation)(run_id)
            return {"id": result["id"], "state": result["state"]}
        raise ValueError("unknown operator action")
