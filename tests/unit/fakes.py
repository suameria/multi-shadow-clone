"""I/O-free doubles for the owned application ports."""

from copy import deepcopy
from dataclasses import asdict
import json

from multi_shadow_clone.orchestration.domain.contracts import Role
from multi_shadow_clone.orchestration.ports import Conflict, ProviderUnknown, Result


ROLES = {r.id: r for r in [Role("R01", "Main", "new task", "plan", "overdelegation", "bounded"),
                          Role("R07", "Research", "sources", "summary", "invented sources", "careful"),
                          Role("R12", "Audit", "candidate", "defects", "rubber stamp", "skeptical"),
                          Role("R19", "Statistics", "numbers", "calculation", "wrong math", "precise")]}


def candidate(value=5, sources=None):
    return {"text": "Synthetic result", "source_ids": sources or [], "limits": ["synthetic"], "values": {"mean": value}}


class MemoryStore:
    def __init__(self):
        self.data = {}

    def create(self, record):
        if record["id"] in self.data:
            raise Conflict()
        self.data[record["id"]] = deepcopy(record)

    def read(self, run_id):
        return deepcopy(self.data[run_id])

    def save(self, record, expected_revision):
        if self.data[record["id"]]["revision"] != expected_revision:
            raise Conflict()
        self.data[record["id"]] = deepcopy({**record, "revision": expected_revision + 1})

    def list_runs(self):
        return [self.read(k) for k in self.data]

    def erase(self, tombstone, expected_revision):
        self.save(tombstone, expected_revision)
        return {"logical": "erased", "scope": "memory test double"}


class ScriptedProvider:
    def __init__(self, handler=None):
        self.requests = []
        self.preflights = 0
        self.handler = handler
        self.reconciled = None

    def preflight(self):
        self.preflights += 1

    def execute(self, request):
        self.requests.append(request)
        if self.handler:
            return self.handler(request)
        if request.stage == "audit":
            return Result("completed", {"approved": True, "reason": "test evidence", "defects": []})
        context = json.loads(request.prompt.split("\nDATA_JSON\n")[1])
        return Result("completed", candidate(sources=context["contract"]["node"]["source_ids"]))

    def reconcile(self, attempt):
        return self.reconciled
