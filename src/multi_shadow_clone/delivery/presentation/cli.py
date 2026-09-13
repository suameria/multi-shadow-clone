import argparse
import json


def main(outbox, argv=None):
    parser = argparse.ArgumentParser(prog="multi-shadow-clone-delivery")
    sub = parser.add_subparsers(dest="operation", required=True)
    send = sub.add_parser("send", help="deliver an independently accepted result to the owned local worktree")
    send.add_argument("run_id")
    send.add_argument("node_id")
    send.add_argument("--business-key", required=True)
    batch = sub.add_parser("send-batch", help="admit the entire bounded list before sending any owned private posts")
    batch.add_argument("--item", action="append", nargs=3, required=True, metavar=("BUSINESS_KEY", "RUN_ID", "NODE_ID"))
    sub.add_parser("reconcile").add_argument("operation_key")
    for name in ("status", "stop", "resume"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    if args.operation == "send":
        result = outbox.submit(args.business_key, {"run_id": args.run_id, "node_id": args.node_id})
    elif args.operation == "send-batch":
        result = outbox.submit_batch([{"business_key": key, "reference": {"run_id": run, "node_id": node}}
                                      for key, run, node in args.item])
    elif args.operation == "reconcile":
        result = outbox.reconcile(args.operation_key)
    else:
        result = getattr(outbox, args.operation)()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
