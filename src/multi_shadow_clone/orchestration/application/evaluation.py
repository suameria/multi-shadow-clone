"""Public candidate execution boundary, with no access to scoring answers."""
from .team import Team
from ..domain.contracts import Limits, Node, Plan


class EvaluationJobs:
    def __init__(self, engine, run_graph, role_id="R01", response_fields=None):
        self.engine, self.team = engine, Team(engine, run_graph)
        self.role_id = role_id
        self.response_fields = response_fields or {}

    def create(self, objective, sources, arm, maximum_turns):
        limits = Limits(max_turns=maximum_turns, max_concurrent=1, deadline_seconds=900)
        if arm == "single":
            node = Node("answer", self.role_id, objective, source_ids=tuple(sources), max_attempts=1, response_fields=self.response_fields)
            return self.engine.create(Plan(objective, (node,), sources, limits))
        return self.team.submit(objective, sources, limits)

    def run(self, run_id):
        run = self.team.advance(run_id)
        parents = {p for n in run["plan"]["nodes"] for p in n["dependencies"]}
        leaves = [n["id"] for n in run["plan"]["nodes"] if n["id"] not in parents]
        output = self.engine.accepted_result(run_id, leaves[0])["output"] if run["state"] == "completed" and len(leaves) == 1 else None
        return {"state": run["state"], "turns": run["turns"], "output": output,
                "provider_contract": run["provider_contract"], "role_hash": run["role_hash"],
                "roles": [n["role_id"] for n in run["plan"]["nodes"]], "attempts": run["attempts"]}

    def stop(self, run_id):
        self.engine.stop(run_id)
