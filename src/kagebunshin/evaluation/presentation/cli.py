import argparse
import json
import re


def study_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) or len(value) > 64:
        raise argparse.ArgumentTypeError("study must be a lowercase name, at most 64 characters, with optional hyphens")
    return value


def main(execute, report, argv=None):
    parser = argparse.ArgumentParser(prog="kagebunshin-evaluate")
    parser.add_argument("--suite", choices=["public-pilot-v1", "role-contracts-v1", "role-contracts-v2", "workflow-review-v1"], default="public-pilot-v1")
    parser.add_argument("--study", type=study_name,
                        help="explicit evaluation edition; preserves earlier editions and their failed trials")
    sub = parser.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run", help="explicit product comparison using checked Codex subscription usage")
    run.add_argument("--case", help="case identifier in the suite; all is bounded to at most twenty cases")
    run.add_argument("--arm", choices=["single", "team", "neutral-label", "all"], default="single")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    study = args.study or args.suite
    selected = getattr(args, "case", None) or ("R01-C01" if args.suite.startswith("role-contracts-") else "E01")
    result = execute(selected, args.arm, study, args.suite) if args.operation == "run" else report(study, args.suite)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0
