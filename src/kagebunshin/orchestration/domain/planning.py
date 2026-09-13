"""Validate an untrusted role proposal without granting it execution authority."""

from dataclasses import replace

from .contracts import InvalidContract, Limits, Node, Plan, Role, Rule


PLANNER_NODE = "mainPlanner"
ROLE_INDEX = "kagebunshinRoleIndex"


def specialist_plan(objective: str, sources: dict[str, str], limits: Limits,
                    final_rules: tuple[Rule, ...], raw: object, roles: dict[str, Role]) -> Plan:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise InvalidContract("expected 1..8 nodes; an empty plan is a capability gap")
    nodes = []
    for n in raw:
        if not isinstance(n, dict) or set(n) != {"id", "role_id", "instruction", "dependencies", "source_ids"}:
            raise InvalidContract("planner may only propose role, instruction and dependencies")
        if any(not isinstance(n[k], str) for k in ("id", "role_id", "instruction")):
            raise InvalidContract("invalid planner text")
        if any(not isinstance(n[k], list) or any(not isinstance(v, str) for v in n[k]) for k in ("dependencies", "source_ids")):
            raise InvalidContract("invalid planner references")
        if n["id"] == PLANNER_NODE:
            raise InvalidContract("reserved node id")
        if not set(n["source_ids"]) <= sources.keys():
            raise InvalidContract("planner proposed an unregistered source")
        nodes.append(Node(n["id"], n["role_id"], n["instruction"], tuple(n["dependencies"]),
                          {d: "response.v1" for d in n["dependencies"]}, tuple(n["source_ids"]),
                          audit_role="R11" if n["role_id"] == "R12" else "R12"))
    used = {d for n in nodes for d in n.dependencies}
    leaves = [n for n in nodes if n.id not in used]
    if len(leaves) != 1:
        raise InvalidContract("exactly one final output must join all branches")
    nodes = [replace(n, rules=final_rules, source_ids=tuple(sources)) if n.id == leaves[0].id else n for n in nodes]
    plan = Plan(objective, tuple(nodes), sources, limits)
    plan.validate(roles)
    return plan


def proposed_plan(run: dict, raw: object, roles: dict[str, Role]) -> Plan:
    plan = specialist_plan(run["plan"]["objective"], run["planning"]["sources"],
                           Limits(**run["plan"]["limits"]),
                           tuple(Rule(**r) for r in run["planning"]["final_rules"]), raw, roles)

    remaining = plan.limits.max_turns - run["turns"]
    minimum = sum(1 + bool(n.audit_role) for n in plan.nodes)
    if minimum > remaining:
        raise InvalidContract(f"plan needs at least {minimum} specialist/audit turns, only {remaining} remain; reduce redundant roles while preserving the objective")
    return plan
