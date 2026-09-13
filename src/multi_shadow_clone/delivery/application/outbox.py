"""Reserve before I/O. Recovery follows actual adapter capabilities."""

from copy import deepcopy
from dataclasses import asdict

from ..domain.model import Capabilities, InvalidDelivery, Receipt, approved_payload, content_identity, fingerprint
from ..ports import AcceptedResults, Conflict, DeliveryStore, Destination


class Outbox:
    def __init__(self, store: DeliveryStore, results: AcceptedResults, destination: Destination):
        self.store, self.results, self.destination = store, results, destination

    def _capabilities(self) -> Capabilities:
        capabilities = self.destination.capabilities()
        capabilities.validate()
        return capabilities

    @staticmethod
    def _integrity(operation: dict) -> None:
        if (operation["state"] not in {"reserved", "cancelled", "unknown", "confirmed"}
            or type(operation["submissions"]) is not int
            or (operation["state"] in {"reserved", "cancelled"} and operation["submissions"] != 0)
            or (operation["state"] not in {"reserved", "cancelled"} and not 1 <= operation["submissions"] <= 3)):
            raise InvalidDelivery("persisted delivery state or attempt count changed")
        if (fingerprint(operation["payload"]) != operation["payload_hash"]
            or fingerprint([operation["identity"], operation["payload"]]) != operation["binding"]
            or fingerprint([operation["identity"], content_identity(operation["payload"])]) != operation["content_key"]):
            raise InvalidDelivery("persisted delivery payload or binding changed")
        if operation["state"] == "confirmed" and (
            not operation.get("receipt") or operation["receipt"].get("payload_hash") != operation["payload_hash"]
        ):
            raise InvalidDelivery("persisted destination receipt changed")

    def submit(self, business_key: str, reference: dict) -> dict:
        return self.submit_batch([{"business_key": business_key, "reference": reference}])[0]

    def submit_batch(self, requests: list[dict]) -> list[dict]:
        """Admit the entire list before any send; remote sends are not atomic."""
        cap = self._capabilities()
        if not isinstance(requests, list) or not 1 <= len(requests) <= cap.max_effects:
            raise InvalidDelivery("entire delivery batch exceeds the operation bound")
        identity = {"environment_id": cap.environment_id, "actor_id": cap.actor_id}
        prepared = []
        for request in requests:
            if not isinstance(request, dict) or set(request) != {"business_key", "reference"}:
                raise InvalidDelivery("exact delivery request fields required")
            business_key, reference = request["business_key"], request["reference"]
            if (not isinstance(business_key, str) or not 1 <= len(business_key) <= 200
                or not isinstance(reference, dict) or set(reference) != {"run_id", "node_id"}
                or any(not isinstance(v, str) or not v for v in reference.values())):
                raise InvalidDelivery("bounded business key and exact accepted-result reference required")
            payload = approved_payload(self.results.accepted_result(**reference))
            self.destination.validate(payload)
            prepared.append({"key": "multi-shadow-clone-" + fingerprint([identity, business_key]),
                             "business_key": business_key, "reference": deepcopy(reference),
                             "binding": fingerprint([identity, payload]), "payload": deepcopy(payload),
                             "content_key": fingerprint([identity, content_identity(payload)])})
        while True:
            ledger = self.store.read()
            if ledger["stopped"]:
                raise InvalidDelivery("delivery is stopped")
            planned, added, changed = [], [], False
            for item in prepared:
                key, binding, content_key = item["key"], item["binding"], item["content_key"]
                previous = ledger["bindings"].get(key)
                if previous:
                    if previous["binding"] != binding:
                        raise InvalidDelivery("same business key cannot bind a different payload")
                    target = ledger["operations"].get(previous["target"])
                    if not target or target["identity"] != identity or target["content_key"] != content_key:
                        raise InvalidDelivery("persisted business key target changed")
                    self._integrity(target)
                else:
                    target = next((p for p in ledger["operations"].values()
                                   if p["content_key"] == content_key and p["state"] != "cancelled"), None)
                    if target:
                        self._integrity(target)
                    else:
                        payload = item["payload"]
                        target = {"id": key, "business_key": item["business_key"], "identity": identity,
                                  "capabilities": asdict(cap), "reference": item["reference"], "binding": binding,
                                  "content_key": content_key, "payload": payload, "payload_hash": fingerprint(payload),
                                  "epoch": ledger["epoch"], "state": "reserved", "submissions": 0, "receipt": None}
                        ledger["operations"][key] = target
                        added.append(key)
                    # A deduplicated key still binds its exact submitted content.
                    ledger["bindings"][key] = {"binding": binding, "target": target["id"]}
                    changed = True
                planned.append((target["id"], key != target["id"]))
            sends = self._effect_slots(ledger, identity)
            if sends > cap.max_effects:
                raise InvalidDelivery("entire delivery batch exceeds remaining effect budget")
            if not changed:
                return self._batch_results(planned)
            try:
                self.store.save(ledger, ledger["revision"])
                break
            except Conflict:
                continue
        # Check the complete newly reserved set before the first remote action.
        # Later cancellation can still race a remote send; never call it rollback.
        if all(self._current(ledger["operations"][key]) for key in added):
            for key in added:
                if self._dispatch_reserved(key)["state"] != "confirmed":
                    break
        return self._batch_results(planned)

    def _effect_slots(self, ledger, identity):
        slots = 0
        for operation in ledger["operations"].values():
            if operation["identity"] == identity:
                self._integrity(operation)
                slots += operation["submissions"] + (operation["state"] == "reserved")
        return slots

    def _dispatch_reserved(self, operation_key):
        while True:
            ledger = self.store.read()
            current = ledger["operations"][operation_key]
            self._integrity(current)
            if current["state"] != "reserved" or not self._current(current):
                return deepcopy(current)
            current.update(state="unknown", submissions=1)
            try:
                self.store.save(ledger, ledger["revision"])
                return self._send(current)
            except Conflict:
                continue

    def _batch_results(self, planned):
        ledger = self.store.read()
        output = []
        for key, alias in planned:
            operation = deepcopy(ledger["operations"][key])
            self._integrity(operation)
            if alias:
                operation["deduplicated_by"] = "normalized_content"
            output.append(operation)
        return output

    def _current(self, operation: dict) -> bool:
        self._integrity(operation)
        ledger = self.store.read()
        if ledger["stopped"] or operation["epoch"] != ledger["epoch"]:
            return False
        if asdict(self._capabilities()) != operation["capabilities"]:
            return False
        result = self.results.accepted_result(**operation["reference"])
        payload = approved_payload(result)
        if fingerprint(payload) != operation["payload_hash"]:
            return False
        self.destination.validate(payload)
        return True

    def _send(self, operation: dict) -> dict:
        # The external call can race cancellation; unknown/confirmed reflect
        # what the destination did, not a claim of instantaneous cancellation.
        if not self._current(operation):
            return self.status(operation["id"])
        try:
            result = self.destination.send(deepcopy(operation["payload"]), operation["id"])
        except Exception:
            return self.status(operation["id"])
        return self._confirm(operation, result)

    def _confirm(self, operation: dict, receipt: Receipt) -> dict:
        self._integrity(operation)
        if not isinstance(receipt, Receipt) or not receipt.remote_id or receipt.payload_hash != operation["payload_hash"]:
            raise InvalidDelivery("destination receipt does not match payload")
        while True:
            ledger = self.store.read()
            current = ledger["operations"][operation["id"]]
            self._integrity(current)
            if current["binding"] != operation["binding"]:
                raise InvalidDelivery("delivery binding changed")
            if current["state"] == "confirmed":
                if current["receipt"] != asdict(receipt):
                    raise InvalidDelivery("conflicting destination receipts")
                return deepcopy(current)
            current.update(state="confirmed", receipt=asdict(receipt),
                           completed_after_stop=ledger["stopped"] or current["epoch"] != ledger["epoch"])
            try:
                self.store.save(ledger, ledger["revision"])
                return deepcopy(current)
            except Conflict:
                continue

    def reconcile(self, operation_key: str) -> dict:
        operation = self.status(operation_key)
        self._integrity(operation)
        if operation["state"] in {"confirmed", "cancelled"}:
            return operation
        if operation["state"] == "reserved":
            # Durable evidence says no send was attempted. Claim it once before
            # any I/O; this does not require an unsafe replay capability.
            return self._dispatch_reserved(operation_key)
        cap = self._capabilities()
        if asdict(cap) != operation["capabilities"]:
            raise InvalidDelivery("destination identity or capability changed")
        if cap.lookup:
            try:
                result = self.destination.find(operation_key, operation["payload_hash"])
            except Exception:
                return operation
            if result is not None:
                return self._confirm(operation, result)
        if not cap.idempotent_replay or not self._current(operation):
            return operation
        while True:
            ledger = self.store.read()
            current = ledger["operations"][operation_key]
            if current["state"] == "confirmed" or ledger["stopped"] or current["epoch"] != ledger["epoch"]:
                return deepcopy(current)
            if current["submissions"] >= 1 + cap.max_replays:
                return deepcopy(current)
            sends = self._effect_slots(ledger, current["identity"])
            if sends >= cap.max_effects:
                return deepcopy(current)
            current["submissions"] += 1
            try:
                self.store.save(ledger, ledger["revision"])
                return self._send(current)
            except Conflict:
                continue

    def stop(self) -> dict:
        return self._control(True)

    def resume(self) -> dict:
        # Past unknown operations keep their original epoch: no new replay
        # authority is created by resuming new deliveries.
        return self._control(False)

    def _control(self, stopped: bool) -> dict:
        while True:
            ledger = self.store.read()
            if ledger["stopped"] == stopped:
                return ledger
            ledger.update(stopped=stopped, epoch=ledger["epoch"] + 1)
            if stopped:
                for operation in ledger["operations"].values():
                    # STOP and the send claim use the same CAS. Only a row
                    # with no send claim can release its reserved slot.
                    # Leave malformed rows untouched; do not obstruct STOP.
                    if operation["state"] == "reserved" and operation["submissions"] == 0:
                        operation["state"] = "cancelled"
            try:
                self.store.save(ledger, ledger["revision"])
                return self.store.read()
            except Conflict:
                continue

    def status(self, operation_key: str | None = None) -> dict:
        ledger = self.store.read()
        return ledger if operation_key is None else ledger["operations"][operation_key]
