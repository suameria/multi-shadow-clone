"""Owned interfaces. Implementations may not weaken these atomicity contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


class Conflict(RuntimeError):
    """The stored revision changed; discard and re-read the snapshot."""


class CapacityExhausted(RuntimeError):
    """The shared store has no free execution slot; no reservation was made."""


class ProviderBlocked(RuntimeError):
    """No new turn was sent. Preconditions are not satisfied."""


class ProviderUnknown(RuntimeError):
    """Dispatch or completion may have happened. Do not blindly retry."""


class ToolSession(Protocol):
    def definitions(self) -> list[dict]: ...
    def validate_request(self,attempt_id:str,run_id:str,node_id:str,binding_hash:str) -> None: ...
    def bind(self,thread_id:str,turn_id:str) -> None: ...
    def handle(self,params:dict) -> dict: ...


class ToolSessions(Protocol):
    def prepare_policy(self, draft: dict) -> dict:
        """Fill runtime hashes from host registrations without broadening scopes."""
        ...

    def retire(self, run: dict) -> dict:
        """Verify stopped operations and resource retirement before releasing leases."""
        ...

    def recover_checks(self, run_id: str, attempt: dict) -> dict:
        """Read existing check completions only; never execute a new check."""
        ...

    def outcome(self, run_id: str, attempt: dict) -> dict:
        """Read bound durable operations; unknown effects cannot approve work."""
        ...

    def contract(self, policy: dict) -> dict:
        """Check host-owned workspace/runtime bindings; never grant from model data."""
        ...

    def prepare(self, scope: dict, request: Request, deadline: float, stop_epoch: int) -> ToolSession:
        """Prepare one saved attempt; do not send a model turn or perform effects.

        The session must recheck request.may_continue before every effect and
        preserve STOP/unknown reservations across process restarts.
        """
        ...


@dataclass(frozen=True)
class Request:
    attempt_id: str
    run_id: str
    node_id: str
    stage: str
    role_id: str
    prompt: str
    binding_hash: str
    output_schema: dict | None = None
    progress: Callable[[str, str | None], bool] | None = field(default=None, compare=False, repr=False)
    may_continue: Callable[[], bool] | None = field(default=None, compare=False, repr=False)
    tool_session: ToolSession | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Result:
    status: str
    payload: dict | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    cleanup_state: str | None = None
    diagnostic_code: str | None = None


class Provider(Protocol):
    def preflight(self) -> None:
        """Raise ProviderBlocked unless same-connection cost/capability checks pass."""
        ...

    def execute(self, request: Request) -> Result: ...

    def reconcile(self, attempt: dict) -> Result | None:
        """None means unknown. This method must never submit a new turn."""
        ...


class RunStore(Protocol):
    def create(self, record: dict) -> None: ...
    def read(self, run_id: str) -> dict: ...
    def save(self, record: dict, expected_revision: int) -> None:
        """Atomically compare revision, replace snapshot, and append new events."""
        ...

    def list_runs(self) -> list[dict]: ...

    def erase(self, tombstone: dict, expected_revision: int) -> dict:
        """Atomic explicit text erasure, retaining identity and unresolved execution reservations."""
        ...


class EvidenceValidity(Protocol):
    def evidence_valid(self, references: dict[str, str]) -> bool: ...


class BuildIdentity(Protocol):
    def current(self) -> dict: ...


Clock = Callable[[], float]


class SettingsStore(Protocol):
    def read(self, revision: int | None = None) -> dict: ...
    def save(self, value: dict, expected_revision: int) -> dict:
        """Append a version only if the current revision still matches."""
        ...
