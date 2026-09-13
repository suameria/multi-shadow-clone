"""Thin local CLI; execution policy remains in Application."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from ..application.engine import Engine
from ..application.team import Team
from ..domain.contracts import Limits, Rule, plan_from_dict


def main(engine: Engine, argv: list[str] | None = None, *, mode="offline", team=None) -> int:
    team = team or Team(engine)
    parser = argparse.ArgumentParser(prog="kagebunshin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("roles", help="60 role candidates; not verified expertise")
    sub.add_parser("list")
    create = sub.add_parser("create", help="persist a validated plan; no model turn yet")
    create.add_argument("plan", type=Path)
    create.add_argument("--execution-policy", type=Path)
    prepare = sub.add_parser("prepare-policy", help="bind host runtime hashes; no model turn")
    prepare.add_argument("plan", type=Path)
    prepare.add_argument("policy", type=Path)
    submit = sub.add_parser("submit", help="persist a bounded main-role request; no model turn yet")
    submit.add_argument("request", type=Path)
    for command in ("run", "status", "stop", "resume", "reconcile", "cleanup", "retire-workspace"):
        p = sub.add_parser(command)
        p.add_argument("run_id")
    args = parser.parse_args(argv)
    if args.command == "roles":
        value = [asdict(r) for r in engine.roles.values()]
    elif args.command == "list":
        value = engine.list_runs()
    elif args.command == "create":
        value = {"run_id": engine.create(plan_from_dict(json.loads(args.plan.read_text())), execution_policy=json.loads(args.execution_policy.read_text()) if args.execution_policy else None), "mode": mode}
    elif args.command == "prepare-policy":
        value = engine.prepare_execution_policy(plan_from_dict(json.loads(args.plan.read_text())),
                                                 json.loads(args.policy.read_text()))
    elif args.command == "submit":
        request = json.loads(args.request.read_text())
        value = {"run_id": team.submit(request["objective"], request["sources"],
                 Limits(**request.get("limits", {})), tuple(Rule(**r) for r in request.get("final_rules", []))), "mode": mode}
    else:
        operation = {"run": team.advance, "status": engine.status,
                     "cleanup": engine.cleanup, "retire-workspace": engine.retire_workspace, "stop": engine.stop, "resume": engine.resume, "reconcile": engine.reconcile}[args.command]
        value = operation(args.run_id)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0
