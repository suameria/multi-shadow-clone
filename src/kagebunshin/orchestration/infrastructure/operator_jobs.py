"""One operator job driver; role concurrency remains controlled by the engine."""

from concurrent.futures import ThreadPoolExecutor
import threading


class OperatorJobs:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.Lock()
        self.current = None
        self.run_id = None
        self.closed = False
        self.cancel = None

    def start(self, run_id, action, cancel):
        with self.lock:
            if self.closed:
                raise ValueError("operator is shutting down")
            if self.current is not None and not self.current.done():
                raise ValueError("an operator action is already running; inspect or stop it first")
            self.run_id, self.cancel = run_id, cancel
            self.current = self.pool.submit(action)
        return {"id": run_id, "state": "accepted_for_execution"}

    def status(self):
        with self.lock:
            if self.current is None:
                return {"state": "idle"}
            if not self.current.done():
                return {"id": self.run_id, "state": "driving"}
            if self.current.cancelled():
                return {"id": self.run_id, "state": "cancelled"}
            error = self.current.exception()
            return {"id": self.run_id, "state": "failed" if error else "returned",
                    "error": type(error).__name__ if error else None}

    def close(self):
        with self.lock:
            self.closed = True
            cancel = self.cancel if self.current is not None and not self.current.done() else None
        try:
            if cancel:
                cancel()
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)
