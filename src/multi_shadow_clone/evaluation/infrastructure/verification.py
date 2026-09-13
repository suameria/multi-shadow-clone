"""Bind evaluation evidence to verified source and publish immutable snapshots."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import stat
import tempfile


class VerifiedStudy:
    def __init__(self, root, case_path, receipt_path):
        self.root, self.case_path, self.receipt_path = root, case_path, receipt_path
        self.initial = None
        self.manifest_hash = None

    def begin(self):
        verified = json.loads((self.root / "evidence/foundation/test-results.json").read_text())
        if verified.get("success") is not True:
            raise ValueError("current verification has not passed")
        self.initial = verified["source_sha256"]
        if str(self.case_path.relative_to(self.root)) not in self.initial:
            raise ValueError("case definition is missing from the verified source manifest")
        self.check()
        self.manifest_hash = sha256(json.dumps(self.initial, sort_keys=True).encode()).hexdigest()
        self._existing(self.receipt_path)
        definition = json.loads(self.case_path.read_text())
        assigned = [case for case in definition["cases"] if "role_id" in case]
        if assigned:
            roles = json.loads((self.root / "src/multi_shadow_clone/orchestration/roles.json").read_text())
            hashes = {r["id"]: sha256(json.dumps(r, sort_keys=True, ensure_ascii=False).encode()).hexdigest() for r in roles}
            if any(c["role_id"] not in hashes or hashes[c["role_id"]] != c.get("role_contract_sha256") for c in assigned):
                raise ValueError("role contract differs from the authored probe; review a new suite version")
        self.receipt = {"at": datetime.now(timezone.utc).isoformat(), "scope": definition["scope"],
                        "source_sha256_at_start": self.initial, "source_manifest_hash": self.manifest_hash,
                        "case_file_sha256": sha256(self.case_path.read_bytes()).hexdigest(),
                        "maximum_turns_per_condition": 6, "purchases": 0, "research_subagents": 0}
        return definition

    def unchanged(self):
        return all(sha256((self.root / p).read_bytes()).hexdigest() == h for p, h in self.initial.items())

    def check(self):
        if not self.unchanged():
            raise ValueError("source changed since verification; no further evaluation started")

    def finish(self, report):
        if any(r.get("source_manifest_hash") != self.manifest_hash for r in report["records"]):
            raise ValueError("evaluation report mixes source versions; preserve the original study")
        receipt = dict(self.receipt, report=report, source_unchanged_during_run=self.unchanged(),
                       owned_processes_stopped=True, temporary_workspaces_removed=True)
        # The first receipt remains a historical observation. Later progress is
        # another snapshot, never a replacement of a failed or partial result.
        digest = self._identity(receipt)
        snapshot = self.receipt_path.with_name(f"{self.receipt_path.stem}.{digest}{self.receipt_path.suffix}")
        for path in (self.receipt_path, snapshot):
            previous = self._existing(path)
            if previous is None:
                if self._publish(path, receipt):
                    return path
                previous = self._existing(path)  # Another writer won the exclusive link.
            if previous is not None and self._identity(previous) == digest:
                return path
        raise ValueError("conflicting evaluation snapshot; existing evidence was preserved")

    @staticmethod
    def _identity(receipt):
        content = {k: v for k, v in receipt.items() if k != "at"}
        return sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def _existing(self, path):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError("evaluation receipt cannot be read without following a link") from exc
        with os.fdopen(descriptor, "r") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("evaluation receipt must be a regular file")
            previous = json.load(stream)
        if not isinstance(previous, dict) or previous.get("source_manifest_hash") != self.manifest_hash:
            raise ValueError("implementation changed after study freeze; use a versioned study")
        if previous.get("case_file_sha256") != sha256(self.case_path.read_bytes()).hexdigest():
            raise ValueError("case suite changed; use a separately named study")
        return previous

    @staticmethod
    def _publish(path, receipt):
        # A reader sees either no file or the complete file. os.link cannot
        # replace an existing destination, including a competing writer's file.
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".study-", delete=False) as stream:
            temporary = stream.name
            try:
                stream.write(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    return False
                return True
            finally:
                os.unlink(temporary)
