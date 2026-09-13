"""Bounded comparison order and stop-on-incomplete policy."""


class Batch:
    def __init__(self, study_scope, report, evidence, progress):
        self.study_scope, self.report, self.evidence, self.progress = study_scope, report, evidence, progress

    def run(self, selected_case, selected_arm):
        definition = self.evidence.begin()
        arms = ["single", "team", "neutral-label"] if selected_arm == "all" else [selected_arm]
        cases = [c for c in definition["cases"] if selected_case in {"all", c["id"]}]
        if not cases:
            raise ValueError("case is not present in the selected suite")
        if len(cases) > 20:
            raise ValueError("select a bounded subset: at most twenty cases per invocation")
        if any("role_id" in c for c in cases) and selected_arm != "single":
            raise ValueError("role contract probes use the direct single-role condition")
        try:
            for index, case in enumerate(cases):
                order = arms[index % len(arms):] + arms[:index % len(arms)]
                for arm in order:
                    self.evidence.check()
                    with self.study_scope(arm, case) as study:
                        row = study.run(case, arm)
                    self.progress({"case": case["id"], "arm": arm, "state": row["state"],
                                   "factual_pass": row.get("score", {}).get("factual_pass"), "turns": row.get("turns")})
                    if row.get("result", {}).get("state") != "completed":
                        raise ValueError("comparison stopped on incomplete trial; inspect the durable ledger")
        finally:
            self.evidence.finish(self.report())
        report = self.report()
        return {"trials": report["trials"], "factual_passes": report["factual_passes"]}
