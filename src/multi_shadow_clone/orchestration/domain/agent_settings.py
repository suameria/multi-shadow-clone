"""Immutable per-job model choices. A role prompt cannot change these values."""
from dataclasses import dataclass

from .contracts import InvalidContract, digest


ACCEPTED_MODELS = frozenset({"gpt-5.6-luna", "gpt-6-astra"})


@dataclass(frozen=True)
class ModelChoice:
    model: str = "gpt-5.6-luna"
    effort: str = "low"

    def validate(self, catalog: dict[str, tuple[str, ...]]) -> None:
        if self.model not in ACCEPTED_MODELS or self.effort not in catalog.get(self.model, ()):
            raise InvalidContract("model/effort is not in the accepted runtime catalog")
        if self.effort == "ultra":
            raise InvalidContract("automatic subagent mode is not enabled")

    def record(self):
        return {"model": self.model, "effort": self.effort, "service_tier": "default"}


@dataclass(frozen=True)
class AgentSettings:
    default: ModelChoice = ModelChoice()
    overrides: tuple[tuple[str, ModelChoice], ...] = ()

    @classmethod
    def parse(cls, value: dict, role_ids: set[str], catalog: dict[str, tuple[str, ...]]):
        if not isinstance(value, dict) or set(value) != {"default", "overrides"}:
            raise InvalidContract("expected default and overrides")
        def choice(raw):
            if not isinstance(raw, dict) or set(raw) != {"model", "effort"} or any(type(v) is not str for v in raw.values()):
                raise InvalidContract("expected exact model and effort strings")
            result = ModelChoice(**raw)
            result.validate(catalog)
            return result
        default = choice(value["default"])
        raw = value["overrides"]
        if not isinstance(raw, dict) or not set(raw) <= role_ids:
            raise InvalidContract("unknown role override")
        return cls(default, tuple(sorted((role_id, choice(item)) for role_id, item in raw.items())))

    def for_role(self, role_id):
        return dict(self.overrides).get(role_id, self.default)

    def record(self):
        return {"default": self.default.record(), "overrides": {key: value.record() for key, value in self.overrides}}

    def fingerprint(self):
        return digest(self.record())
