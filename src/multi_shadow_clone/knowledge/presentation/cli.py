import argparse
import json
from pathlib import Path


def main(library, argv=None, workflow=None):
    parser = argparse.ArgumentParser(prog="multi-shadow-clone-knowledge")
    sub = parser.add_subparsers(dest="operation", required=True)
    register = sub.add_parser("register", help="register a supplied snapshot JSON; no URL fetch")
    register.add_argument("snapshot", type=Path)
    claim = sub.add_parser("claim")
    claim.add_argument("statement")
    claim.add_argument("source_ids", nargs="+")
    relate = sub.add_parser("relate")
    relate.add_argument("source")
    relate.add_argument("target")
    relate.add_argument("kind", choices=["supports", "contradicts", "supersedes"])
    relate.add_argument("reason")
    bundle = sub.add_parser("bundle")
    bundle.add_argument("claim_ids", nargs="+")
    support = sub.add_parser("support")
    support.add_argument("claim_id")
    support.add_argument("run_id")
    support.add_argument("node_id")
    handoff = sub.add_parser("handoff", help="save an evidence-bound job; does not execute")
    handoff.add_argument("objective")
    handoff.add_argument("claim_ids", nargs="+")
    sub.add_parser("prepare-review", help="save an independent claim review; no model dispatch").add_argument("claim_id")
    for name in ("retract", "erase"):
        sub.add_parser(name).add_argument("source_id")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.operation == "register":
        if args.snapshot.stat().st_size > 110_000:
            raise ValueError("snapshot exceeds input bound")
        result = {"source_id": library.register(**json.loads(args.snapshot.read_text()))}
    elif args.operation == "claim":
        result = {"claim_id": library.claim(args.statement, args.source_ids)}
    elif args.operation == "relate":
        result = {"relation_id": library.relate(args.source, args.target, args.kind, args.reason)}
    elif args.operation == "bundle":
        result = library.bundle(args.claim_ids)
    elif args.operation == "support":
        library.support(args.claim_id, {"run_id": args.run_id, "node_id": args.node_id})
        result = {"claim_id": args.claim_id, "state": "supported"}
    elif args.operation == "handoff":
        if workflow is None:
            raise ValueError("knowledge job boundary is unavailable")
        result = workflow.handoff(args.objective, args.claim_ids)
    elif args.operation == "prepare-review":
        if workflow is None:
            raise ValueError("knowledge job boundary is unavailable")
        result = workflow.prepare_review(args.claim_id)
    elif args.operation in {"erase", "retract"}:
        result = workflow.erase(args.source_id) if args.operation == "erase" and workflow else getattr(library, args.operation)(args.source_id)
    else:
        result = library.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
