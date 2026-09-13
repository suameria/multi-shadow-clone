from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import unicodedata


class InvalidDelivery(ValueError):
    pass


def fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Capabilities:
    environment_id: str
    actor_id: str
    lookup: bool = False
    idempotent_replay: bool = False
    max_effects: int = 20
    max_replays: int = 2

    def validate(self):
        if (not self.environment_id or not self.actor_id or type(self.lookup) is not bool
            or type(self.idempotent_replay) is not bool or type(self.max_effects) is not int
            or not 1 <= self.max_effects <= 20 or type(self.max_replays) is not int
            or not 0 <= self.max_replays <= 2):
            raise InvalidDelivery("invalid destination capabilities")


@dataclass(frozen=True)
class Receipt:
    remote_id: str
    payload_hash: str


def approved_payload(result: dict) -> dict:
    if not result.get("audit_role") or not result.get("receipt", {}).get("audit_hash"):
        raise InvalidDelivery("delivery needs an independently audited result")
    payload = result.get("output", {}).get("values", {}).get("delivery")
    if not isinstance(payload, dict) or not payload or len(str(payload).encode()) > 100_000:
        raise InvalidDelivery("no bounded delivery payload in accepted result")
    fingerprint(payload)
    return payload


def content_identity(payload: dict) -> str:
    def normalize(value):
        if isinstance(value, str):
            return " ".join(unicodedata.normalize("NFKC", value).split())
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        return value
    return fingerprint(normalize(payload))
