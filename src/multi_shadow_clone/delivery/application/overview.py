"""Delivery-owned projection; no model execution or destination connections."""


class DeliveryOverview:
    def __init__(self, store, environment):
        self.store, self.environment = store, environment

    def snapshot(self):
        ledger, environment = self.store.read(), self.environment.state() or {}
        operations = [{"id": op["id"], "state": op["state"], "submissions": op["submissions"],
                       "reference": op["reference"], "remote_id": (op.get("receipt") or {}).get("remote_id"),
                       "environment_current": op["capabilities"]["environment_id"] == environment.get("id") and environment.get("phase") == "running"}
                      for op in ledger["operations"].values()]
        return {"stopped": ledger["stopped"], "operations": operations,
                "environment_phase": environment.get("phase", "not_created")}
