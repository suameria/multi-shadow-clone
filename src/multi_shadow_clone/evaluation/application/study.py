"""Frozen case/arm ledger; failed or interrupted trials remain in the denominator."""
from ..domain.scoring import candidate_pack, fingerprint, score
from ..ports import CandidateJobs, StudyStore


class Study:
    def __init__(self, store: StudyStore, jobs: CandidateJobs, clock, contract_hash=None):
        self.store, self.jobs, self.clock = store, jobs, clock
        self.contract_hash = contract_hash

    def run(self, case, arm, maximum_turns=6):
        if arm not in {"single", "team", "neutral-label"}:
            raise ValueError("unsupported comparison arm")
        if type(maximum_turns) is not int or not 1 <= maximum_turns <= 6:
            raise ValueError("evaluation uses at most six turns per condition")
        sources = candidate_pack(case)
        key = case["id"] + ":" + arm
        record = {"key": key, "case_id": case["id"], "case_hash": fingerprint(case), "arm": arm,
                  "candidate_hash": fingerprint(sources), "state": "preparing", "started_at": self.clock(),
                  "held_out": case["held_out"], "max_turns": maximum_turns, "turns": 0}
        record["source_manifest_hash"] = self.contract_hash
        if not self.store.reserve(key, record):
            existing = self.store.read(key)
            if existing["case_hash"] != record["case_hash"]:
                raise ValueError("case changed after study freeze; start an explicitly versioned study")
            if existing.get("source_manifest_hash") != self.contract_hash:
                raise ValueError("implementation changed after study freeze; use a versioned study")
            return existing  # Never silently re-run a failed or incomplete sample.
        run_id = None
        try:
            run_id = self.jobs.create("与えられた合成の依頼を解き、根拠・限界と指定された構造化フィールドを返してください。", sources, arm, maximum_turns)
            record.update(state="started", run_id=run_id)
            self.store.save(key, record)
            result = self.jobs.run(run_id)
            record.update(result=result, state="finished", turns=result["turns"],
                          score=score(case, result.get("output")))
        except BaseException as exc:
            record.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                          error=type(exc).__name__)
            if run_id is not None:
                self.jobs.stop(run_id)
            raise
        finally:
            record["elapsed_seconds"] = round(self.clock() - record["started_at"], 3)
            self.store.save(key, record)
        return record

    def report(self):
        records = self.store.list_records()
        return {"records": records, "trials": len(records),
                "factual_passes": sum(r.get("score", {}).get("factual_pass", False) for r in records),
                "turns_recorded": sum(r.get("turns", 0) for r in records),
                "human_edit_time": None, "broad_expertise_demonstrated": False,
                "scope": "public synthetic pilot; all reserved samples retained, no statistical superiority claim"}
