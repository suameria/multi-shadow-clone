"""Canonical repository commands, ownership read-back and receipt interpretation."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from ..domain.model import InvalidDelivery
from .lifecycle_files import CommandJournal, private_read


class RepositoryEnvironment:
    def __init__(self, primary: Path, worktrees: Path, files):
        self.primary, self.worktrees = primary.resolve(), worktrees.resolve()
        self.journal = CommandJournal(files)

    def _run(self, args, cwd):
        env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "LANG"}}
        result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode or len(result.stdout) > 2_000_000:
            raise InvalidDelivery("repository read-back failed")
        return result.stdout.decode("utf-8").strip()

    def new_identity(self, task, environment_id):
        if (self._run(["git", "status", "--porcelain"], self.primary)
            or self._run(["git", "branch", "--show-current"], self.primary) != "develop"):
            raise InvalidDelivery("primary develop must be clean")
        path = self.worktrees / ("granskypolis-" + task)
        return {"id": environment_id, "task": task, "path": str(path), "branch": "codex/" + path.name,
                "head": self._run(["git", "rev-parse", "HEAD"], self.primary), "seeded": False}

    def _command(self, state, operation):
        path = self._shape(state)
        if operation == "create":
            return ["make", "worktree-new", "TARGET=granskypolis", "TASK=" + state["task"]], self.primary
        path = self.validate(state)
        commands = {
            "start": (["make", "service-up-worktree", "SERVICE=granskypolis"], path),
            "seed": (["make", "seed-worktree", "CONFIRM=1"], path / "services/granskypolis"),
            "stop": (["make", "service-down-worktree", "SERVICE=granskypolis"], path),
            "retire": (["make", "task-integrate-local", "MESSAGE=chore(granskypolis): finish owned BOT acceptance", "CONFIRM=1"], path),
            "resume_cleanup": (["make", "branch-integration-resume-cleanup", "TARGET_BRANCH=develop",
                                "COMMIT=" + state["head"], "BASE_COMMIT=" + state["head"], "CONFIRM=1",
                                "REMOTE_SYNC_REQUIRED=0", "SOURCE_WORKTREE=" + str(path), "SOURCE_BRANCH=" + state["branch"]], self.primary),
        }
        if operation not in commands:
            raise InvalidDelivery("operation is outside the canonical workflow")
        return commands[operation]

    def execute(self, state, operation, operation_id):
        if operation in {"retire", "resume_cleanup"}:
            self.no_change_retirement(state)
        args, cwd = self._command(state, operation)
        return self.journal.execute(state, operation, operation_id, args, cwd)

    def receipt(self, state):
        operation, operation_id = state.get("operation"), state.get("operation_id")
        if not operation_id:
            return None
        args, cwd = self._command(state, operation)
        return self.journal.read(state, operation, operation_id, args, cwd)

    def _common(self):
        path = Path(self._run(["git", "rev-parse", "--git-common-dir"], self.primary))
        return (self.primary / path).resolve() if not path.is_absolute() else path.resolve()

    def creation_ready(self, state):
        path = self.validate(state)
        identity = json.loads(private_read(self._common() / "suameria-worktree-lifecycle" /
                                          ("worktree-identity-" + path.name + ".json")))
        expected = {"canonical_slug": path.name, "classification": "canonical", "format": "1", "immutable": "true",
                    "source_branch": state["branch"], "source_worktree_hex": os.fsencode(path).hex(),
                    "target_provenance": "service-registry", "target_slug": "granskypolis", "task_slug": state["task"]}
        if identity != expected:
            raise InvalidDelivery("canonical immutable creation identity mismatch")
        plan = path / ".local/plan.md"
        if plan.parent.is_symlink() or plan.parent.resolve() != plan.parent:
            raise InvalidDelivery("creation plan directory changed")
        # This is our disposable local plan, not the repository's tracked docs.
        fd = os.open(plan, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
        with os.fdopen(fd, "r+") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise InvalidDelivery("creation plan is not an owned regular file")
            text = stream.read(200_001)
            if len(text) > 200_000 or "Creation HEAD: `" + state["head"] + "`" not in text:
                raise InvalidDelivery("creation plan or HEAD does not match")
            if "## multi-shadow-clone acceptance" not in text:
                stream.write("\n## multi-shadow-clone acceptance\nPrivate synthetic posts only. No tracked code changes. "
                             "Use official service, seed and no-change local retirement. Keep evidence outside this worktree. "
                             "No remote Git or non-owned data effects.\n")
                stream.flush()
                os.fsync(stream.fileno())
        return path

    def creation_rejection(self, state):
        """Only an observed canonical input-validation rejection proves no creation.

        A generic nonzero exit never proves absence of resources.
        """
        path = self._shape(state)
        result = self.receipt(state)
        if (not result or result["returncode"] == 0
            or not re.fullmatch(r"TASK must not contain an unstable numeric component\.\nmake: \*\*\* \[worktree-new\] Error 2\n?", result["output"])):
            return None
        identity = self._common() / "suameria-worktree-lifecycle" / ("worktree-identity-" + path.name + ".json")
        if (path.exists() or identity.exists()
            or self._run(["git", "branch", "--list", state["branch"]], self.primary)
            or "worktree " + str(path) + "\n" in self._run(["git", "worktree", "list", "--porcelain"], self.primary) + "\n"
            or self._run(["git", "status", "--porcelain"], self.primary)
            or self._run(["git", "rev-parse", "HEAD"], self.primary) != state["head"]):
            raise InvalidDelivery("creation rejection does not match absence of owned resources")
        return {"status": "rejected_before_creation", "proof_kind": "canonical-input-validation-and-absence-readback",
                "resources_created": False, "commit_created": "no", "remote_sync": "not-run"}

    def _shape(self, state: dict) -> Path:
        path = Path(state["path"])
        if (not re.fullmatch(r"multi-shadow-clone-(?:[a-f0-9]{8}|local-[a-p]{8})", state["task"])
            or not re.fullmatch(r"environment-[a-f0-9]{32}", state["id"])
            or not re.fullmatch(r"[a-f0-9]{40}", state["head"])
            or path.is_symlink() or path.resolve() != path or path.parent != self.worktrees
            or state["branch"] != "codex/" + path.name
            or path.name != "granskypolis-" + state["task"]):
            raise InvalidDelivery("noncanonical owned worktree")
        return path

    def validate(self, state: dict) -> Path:
        path = self._shape(state)
        registered = self._run(["git", "worktree", "list", "--porcelain"], self.primary)
        if "worktree " + str(path) + "\n" not in registered + "\n":
            raise InvalidDelivery("worktree registration missing")
        if (self._run(["git", "branch", "--show-current"], path) != state["branch"]
            or self._run(["git", "rev-parse", "HEAD"], path) != state["head"]):
            raise InvalidDelivery("worktree identity or HEAD changed")
        return path

    def describe(self, state):
        values = self._service_env(self.validate(state))
        return {"port": int(values["APP_PORT"]), "project": values["COMPOSE_PROJECT_NAME"],
                "engine_hash": sha256(self._run(["docker", "info", "--format", "{{.ID}}"], self.primary).encode()).hexdigest()}

    @staticmethod
    def _service_env(path):
        env = path / "services/granskypolis/docker/.env"
        if env.is_symlink() or env.stat().st_uid != os.getuid() or env.stat().st_mode & 0o077:
            raise InvalidDelivery("service environment must be owned and private")
        values = dict(re.findall(r"^(APP_PORT|COMPOSE_PROJECT_NAME)=(.+)$", env.read_text(), re.M))
        if (set(values) != {"APP_PORT", "COMPOSE_PROJECT_NAME"} or not values["APP_PORT"].isdigit()
            or not 1024 <= int(values["APP_PORT"]) <= 65535
            or not re.fullmatch(r"[a-z0-9_-]+", values["COMPOSE_PROJECT_NAME"])):
            raise InvalidDelivery("invalid repository-assigned service identity")
        return values

    def endpoint(self, state: dict) -> str:
        path = self.validate(state)
        values = self._service_env(path)
        if int(values["APP_PORT"]) != state["port"] or values["COMPOSE_PROJECT_NAME"] != state["project"]:
            raise InvalidDelivery("assigned port or project changed")
        engine = self._run(["docker", "info", "--format", "{{.ID}}"], self.primary)
        if sha256(engine.encode()).hexdigest() != state["engine_hash"]:
            raise InvalidDelivery("Docker engine changed")
        ids = self._run(["docker", "ps", "--filter", "label=com.docker.compose.project=" + state["project"],
                         "--filter", "label=com.docker.compose.service=nginx", "--format", "{{.ID}}"], self.primary).splitlines()
        if len(ids) != 1:
            raise InvalidDelivery("expected one owned nginx")
        info = json.loads(self._run(["docker", "inspect", ids[0]], self.primary))[0]
        labels = info["Config"]["Labels"]
        if Path(labels["com.docker.compose.project.working_dir"]).resolve() != path / "services/granskypolis/docker":
            raise InvalidDelivery("container belongs to a different worktree")
        bindings = info["NetworkSettings"]["Ports"].get("80/tcp") or []
        if not any(p["HostPort"] == str(state["port"]) for p in bindings):
            raise InvalidDelivery("owned port binding missing")
        return "http://127.0.0.1:" + str(state["port"])

    def no_change_retirement(self, state):
        path = self.validate(state)
        if (self._run(["git", "status", "--porcelain"], path)
            or self._run(["git", "status", "--porcelain"], self.primary)
            or self._run(["git", "branch", "--show-current"], self.primary) != "develop"
            or self._run(["git", "rev-parse", "HEAD"], self.primary) != state["head"]
            or self._run(["git", "rev-parse", "develop"], self.primary) != state["head"]):
            raise InvalidDelivery("no-change retirement preconditions changed; retain worktree")

    def retirement_proof(self, state):
        path = self._shape(state)
        if path.exists():
            return None
        receipt_path = self._common() / "suameria-execution-harness/integration-receipts" / ("integration-" + path.name + ".json")
        if not receipt_path.exists():
            return None
        proof = json.loads(private_read(receipt_path))
        expected = {"schema_version": 1, "status": "passed", "mutation_state": "integrated", "target_branch": "develop",
                    "source_branch": state["branch"], "commit": state["head"], "cleanup": "passed", "source_retired": "yes", "remote_sync": "not-run"}
        if any(proof.get(k) != v for k, v in expected.items()):
            raise InvalidDelivery("official retirement receipt differs from exact no-change contract")
        if (self._run(["git", "branch", "--list", state["branch"]], self.primary)
            or "worktree " + str(path) + "\n" in self._run(["git", "worktree", "list", "--porcelain"], self.primary) + "\n"
            or self._run(["git", "rev-parse", "HEAD"], self.primary) != state["head"]
            or self._run(["git", "status", "--porcelain"], self.primary)):
            raise InvalidDelivery("retirement read-back failed")
        return {**proof, "proof_kind": "official-durable-receipt-and-git-readback"}
