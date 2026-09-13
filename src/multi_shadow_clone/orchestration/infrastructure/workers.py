"""Bounded host workers call the same Application step and durable reservations."""

from concurrent.futures import ThreadPoolExecutor


class Workers:
    def __init__(self, engine):
        self.engine = engine

    def run(self, run_id):
        def work():
            while self.engine.step(run_id):
                pass
        count = self.engine.status(run_id)["plan"]["limits"]["max_concurrent"]
        with ThreadPoolExecutor(max_workers=count) as pool:
            tasks = [pool.submit(work) for _ in range(count)]
            for task in tasks:
                task.result()
        return self.engine.cleanup(run_id)
