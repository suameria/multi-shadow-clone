"""Local operator lifetime. Starting the server never starts a model turn."""
from pathlib import Path
import argparse
import signal
import time

def main(engine_scope, team_factory, operator_type, jobs_type, server_type, argv=None, delivery_factory=None, settings_factory=None):
    parser = argparse.ArgumentParser(prog="kagebunshin-operator")
    parser.add_argument("--mode", choices=["offline", "codex"], default="offline")
    parser.add_argument("--port", type=int, default=0, help="0 selects an unused local port")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--workspace-config", type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0..65535")
    scope_options = {key: value for key, value in {"data_dir": args.data_dir,
                     "workspace_config": args.workspace_config}.items() if value is not None}
    with engine_scope(args.mode, **scope_options) as engine:
        jobs = jobs_type()
        server, previous = None, {}
        def stop_signal(signum, frame):
            raise KeyboardInterrupt
        try:
            # Construction failures also close the owned executor and engine scope.
            operator = operator_type(engine, team_factory(engine), jobs, time.time,
                                     delivery=delivery_factory() if args.mode == "codex" and delivery_factory else None,
                                     settings=settings_factory() if args.mode == "codex" and settings_factory else None)
            server = server_type(operator, args.mode, args.port)
            for signum in (signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.signal(signum, stop_signal)
            print(f"{server.origin}  mode={args.mode}  Ctrl+C: stop owned job and close", flush=True)
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                if server is not None: server.server_close()
                jobs.close()
            finally:
                for signum, handler in previous.items(): signal.signal(signum, handler)
    return 0
