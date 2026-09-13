import argparse
import json


def main(lifecycle, argv=None):
    parser = argparse.ArgumentParser(prog="kagebunshin-worktree")
    parser.add_argument("operation", choices=["start", "status", "recover", "retire"])
    args = parser.parse_args(argv)
    operation = {"start": lifecycle.start, "status": lifecycle.state, "recover": lifecycle.recover, "retire": lifecycle.retire}[args.operation]
    result = operation()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
