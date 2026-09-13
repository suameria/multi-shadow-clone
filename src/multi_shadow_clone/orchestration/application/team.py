"""Main-role planning and bounded admission of a specialist DAG."""

from dataclasses import asdict

from ..domain.contracts import InvalidContract, Limits, Node, Plan, Rule, canonical, digest
from ..domain.planning import PLANNER_NODE, ROLE_INDEX, proposed_plan
from ..ports import Conflict
from .engine import Engine


class Team:
    def __init__(self, engine: Engine, run_graph=None):
        self.engine = engine
        self.run_graph = run_graph or engine.run_until_idle

    def submit(self, objective: str, sources: dict[str, str], limits: Limits = Limits(),
               final_rules: tuple[Rule, ...] = (), *, evidence_refs: dict | None = None, execution_settings: dict | None = None) -> str:
        if ROLE_INDEX in sources:
            raise InvalidContract("reserved source id")
        index = [{"id": r.id, "name": r.name, "trigger": r.trigger,
                  "output": r.output, "status": r.lifecycle} for r in self.engine.roles.values()]
        instruction = (
            f"Total model turn budget is {limits.max_turns}, including this planner and repairs. Each specialist node needs one generation plus one independent audit. "
            "Select only needed roles from the index for this objective. Candidate roles are experimental, "
            "not verified experts. Return response.v1 with values.nodes: an array of 1..8 nodes. "
            "Each node has exactly id, role_id, instruction, dependencies:string[], source_ids:string[]. "
            "The top-level response.source_ids MUST include EVERY supplied source, including multiShadowCloneRoleIndex. "
            "Inside values.nodes, each node.source_ids may refer only to the task sources, NEVER multiShadowCloneRoleIndex. "
            "Use exactly one final node consuming all branches through dependencies. "
            "The final node is one of values.nodes, not an implicit controller or an extra future role. "
            "Respect the current output schema's maxItems; repairs also consume the total turn budget. "
            "With space for only two nodes, use A then B with B.dependencies=[A.id], where B integrates A's accepted work, "
            "or use one specialist for the whole objective. Two independent nodes require a third joining node and its generation/audit budget. "
            "Before returning, check that every other node has a dependency path into the final node, "
            "and that the final node's instruction requests the complete deliverable, not only its own subtask. "
            "Do not create tools, write permissions, limits or audit policies. No duplicate management layer. "
            "For a simple task use one specialist. The controller supplies independent auditing. "
            "If no role fits, return values.nodes: [] and explain the capability gap. "
            "The final candidate must meet these controller-owned checks: " + canonical([asdict(r) for r in final_rules])
        )
        planning = Plan(objective, (Node(PLANNER_NODE, "R01", instruction,
                                        source_ids=tuple([*sources, ROLE_INDEX]), max_attempts=2, response_format="plan"),),
                        {**sources, ROLE_INDEX: canonical(index)}, limits)
        return self.engine.create(planning, evidence_refs=evidence_refs, execution_settings=execution_settings, planning={"stage": "proposal", "sources": sources,
                                                     "final_rules": [asdict(r) for r in final_rules]})

    def advance(self, run_id: str) -> dict:
        if self.engine.status(run_id).get("planning", {}).get("stage") != "proposal":
            return self.run_graph(run_id)
        run = self.engine.run_until_idle(run_id)
        if run.get("planning", {}).get("stage") == "proposal" and run["state"] == "completed":
            self._admit(run_id)
            run = self.run_graph(run_id)
        return run

    def _admit(self, run_id: str) -> None:
        while True:
            run = self.engine.status(run_id)
            if run["stopped"] or run["planning"]["stage"] != "proposal" or run["state"] != "completed":
                return
            from ..domain.contracts import plan_from_dict
            if not self.engine._compatible(run) or not self.engine._accepted_integrity(run, plan_from_dict(run["plan"])):
                self.engine._halt(run, "blocked_artifact_changed")
                return
            current = run["nodes"][PLANNER_NODE]
            raw = current["output"]["values"].get("nodes")
            try:
                plan = self._plan(run, raw)
            except (InvalidContract, KeyError, TypeError, AttributeError) as exc:
                run["state"] = "blocked_plan"
                self.engine._event(run, "plan_rejected", reason=str(exc))
            else:
                run["planning"].update(stage="admitted", planning_contract=run["plan"],
                                       planning_contract_hash=run["plan_hash"], planner_result=current)
                run["plan"] = asdict(plan)
                run["plan_hash"] = digest(run["plan"])
                run["nodes"] = {n.id: {"state": "pending", "attempts": 0, "feedback": [],
                                        "candidate": None, "output": None, "active": None,
                                        "last_defect_hash": None, "receipt": None} for n in plan.nodes}
                run["state"] = "running"
                self.engine._event(run, "plan_admitted", nodes=[n.id for n in plan.nodes],
                                   roles=[n.role_id for n in plan.nodes], contract_hash=run["plan_hash"])
            try:
                self.engine._save(run)
                return
            except Conflict:
                continue

    def _plan(self, run: dict, raw: object) -> Plan:
        return proposed_plan(run, raw, self.engine.roles)
