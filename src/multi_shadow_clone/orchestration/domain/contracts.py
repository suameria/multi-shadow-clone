"""Pure contracts and graph invariants. No host or provider I/O."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
import re
from typing import Any


class InvalidContract(ValueError):
    pass


def canonical(value: Any) -> str:
    """Project JSON-UTF8-v1, not an implementation of RFC 8785."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def positive_int(value: Any, name: str, ceiling: int) -> None:
    if type(value) is not int or not 1 <= value <= ceiling:
        raise InvalidContract(f"{name}: expected integer in 1..{ceiling}")


def identifier(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        raise InvalidContract("invalid identifier")


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    trigger: str
    output: str
    reject_if: str
    behavior: str
    group: str = ""
    version: int = 1
    lifecycle: str = "candidate"


@dataclass(frozen=True)
class Rule:
    """Limited, documented checker language. Never evaluates generated code."""
    path: str
    kind: str
    expected: Any


@dataclass(frozen=True)
class Node:
    id: str
    role_id: str
    instruction: str
    dependencies: tuple[str, ...] = ()
    input_types: dict[str, str] = field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    output_type: str = "response.v1"
    rules: tuple[Rule, ...] = ()
    audit_role: str | None = None
    max_attempts: int = 3
    response_fields: dict[str, str] = field(default_factory=dict)
    response_format: str = "standard"


@dataclass(frozen=True)
class Limits:
    max_turns: int = 12
    max_concurrent: int = 2
    deadline_seconds: int = 900

    def validate(self) -> None:
        positive_int(self.max_turns, "max_turns", 100)
        positive_int(self.max_concurrent, "max_concurrent", 2)
        positive_int(self.deadline_seconds, "deadline_seconds", 3600)


@dataclass(frozen=True)
class Plan:
    objective: str
    nodes: tuple[Node, ...]
    sources: dict[str, str]
    limits: Limits = field(default_factory=Limits)
    version: int = 1

    def validate(self, roles: dict[str, Role]) -> None:
        self.limits.validate()
        if self.version != 1 or not self.objective.strip() or not 1 <= len(self.nodes) <= 20:
            raise InvalidContract("invalid plan version, objective or node count")
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            raise InvalidContract("duplicate node id")
        if any(not isinstance(v, str) for v in self.sources.values()):
            raise InvalidContract("source snapshots must be text")
        for key in self.sources:
            identifier(key)
        by_id = {n.id: n for n in self.nodes}
        ancestors: dict[str, set[str]] = {}

        def visit(key: str, active: set[str]) -> set[str]:
            if key in active:
                raise InvalidContract("dependency cycle")
            if key not in by_id:
                raise InvalidContract("missing dependency")
            if key not in ancestors:
                parents = set(by_id[key].dependencies)
                if len(parents) != len(by_id[key].dependencies):
                    raise InvalidContract("duplicate dependency")
                ancestors[key] = parents.union(*(visit(p, active | {key}) for p in parents))
            return ancestors[key]

        for node in self.nodes:
            identifier(node.id)
            if node.role_id not in roles or (node.audit_role and node.audit_role not in roles):
                raise InvalidContract("unknown role")
            if node.audit_role == node.role_id:
                raise InvalidContract("auditor must have a separate role")
            if not node.instruction.strip() or node.output_type != "response.v1":
                raise InvalidContract("unsupported node contract")
            if not isinstance(node.response_format, str) or node.response_format not in {"standard", "plan"} or (node.response_format == "plan" and node.response_fields):
                raise InvalidContract("unsupported response format combination")
            from .output_format import validate_fields
            try:
                validate_fields(node.response_fields)
            except ValueError as exc:
                raise InvalidContract(str(exc)) from exc
            positive_int(node.max_attempts, "max_attempts", 3)
            visit(node.id, set())
            if set(node.input_types) != set(node.dependencies):
                raise InvalidContract("every dependency needs an input type")
            for parent, type_name in node.input_types.items():
                if by_id[parent].output_type != type_name:
                    raise InvalidContract("dependency type mismatch")
            if not set(node.source_ids) <= self.sources.keys():
                raise InvalidContract("unregistered source")
            for resource in node.writes:
                identifier(resource)
            for rule in node.rules:
                if rule.kind not in {"equals", "contains", "minimum", "maximum"}:
                    raise InvalidContract("unknown checker")
                if not re.fullmatch(r"(?:text|limits|values(?:\.[A-Za-z][A-Za-z0-9_]*)+)", rule.path):
                    raise InvalidContract("unsupported checker path")
                if rule.kind in {"minimum", "maximum"} and (
                    type(rule.expected) not in {int, float} or not math.isfinite(rule.expected)
                ):
                    raise InvalidContract("numeric checker requires a finite bound")
        for i, a in enumerate(self.nodes):
            for b in self.nodes[i + 1:]:
                if set(a.writes) & set(b.writes) and a.id not in ancestors[b.id] and b.id not in ancestors[a.id]:
                    raise InvalidContract("unordered writers share a resource")
        if len(canonical(asdict(self)).encode()) > 500_000:
            raise InvalidContract("plan exceeds local context bound")


def plan_from_dict(raw: dict) -> Plan:
    """Reject unknown fields; coercing malformed provider data would hide defects."""
    try:
        nodes = []
        for item in raw["nodes"]:
            n = dict(item)
            for key in ("dependencies", "source_ids", "writes"):
                n[key] = tuple(n.get(key, []))
            n["rules"] = tuple(Rule(**r) for r in n.get("rules", []))
            nodes.append(Node(**n))
        return Plan(**{**raw, "nodes": tuple(nodes), "limits": Limits(**raw.get("limits", {}))})
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidContract("malformed plan") from exc
