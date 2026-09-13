"""Private durable lifecycle and command receipts, outside disposable worktrees."""

from contextlib import contextmanager
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
from uuid import uuid4

from ..domain.model import InvalidDelivery


def private_read(path, limit=2_000_000):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise InvalidDelivery("receipt must be a bounded owned private regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise InvalidDelivery("receipt exceeds limit")
    return data


def atomic_json(path, data):
    temporary = path.parent / (".write-" + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class LifecycleFiles:
    def __init__(self, directory: Path):
        if directory.is_symlink():
            raise InvalidDelivery("lifecycle directory cannot be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InvalidDelivery("lifecycle directory must be owned and private")
        self.directory, self.lock_fd = directory.resolve(), None

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / "lifecycle.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd = fd
            yield
        finally:
            self.lock_fd = None
            os.close(fd)

    def read(self):
        path = self.directory / "lifecycle.json"
        return json.loads(private_read(path)) if path.exists() else None

    def save(self, state):
        previous = self.read()
        for item in (state, previous):
            if item is not None and not re.fullmatch(r"environment-[a-f0-9]{32}", item.get("id", "")):
                raise InvalidDelivery("invalid lifecycle identity")
        if previous and previous["id"] != state["id"]:
            # Preserve evidence of earlier owned environments.
            if previous.get("phase") != "retired":
                raise InvalidDelivery("unfinished environment cannot be replaced")
            atomic_json(self.directory / (previous["id"] + ".json"), previous)
        atomic_json(self.directory / "lifecycle.json", state)


class CommandJournal:
    MAX_LOG = 32 * 1024 * 1024

    def __init__(self, files):
        self.files = files

    def _path(self, operation_id, suffix):
        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise InvalidDelivery("invalid command receipt identity")
        return self.files.directory / ("command-" + operation_id + suffix)

    @staticmethod
    def binding(state, operation, args, cwd):
        return {"environment_id": state["id"], "operation": operation, "args": args, "cwd": str(cwd)}

    def read(self, state, operation, operation_id, args, cwd):
        path = self._path(operation_id, ".json")
        if not path.exists():
            return None
        receipt = json.loads(private_read(path))
        expected = self.binding(state, operation, args, cwd)
        if receipt.get("binding") != expected:
            raise InvalidDelivery("command receipt belongs to another operation")
        if receipt.get("state") != "finished":
            return None
        raw = private_read(self._path(operation_id, ".log"), self.MAX_LOG)
        if sha256(raw).hexdigest() != receipt["output_hash"]:
            raise InvalidDelivery("command output changed after receipt")
        return {**receipt, "output": raw.decode("utf-8", errors="replace")}

    def execute(self, state, operation, operation_id, args, cwd, timeout=1800):
        path = self._path(operation_id, ".json")
        if path.exists():
            raise InvalidDelivery("command identity was already dispatched; reconcile it instead")
        binding = self.binding(state, operation, args, cwd)
        log = self._path(operation_id, ".log")
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "LANG"}}
        env["PYTHONUNBUFFERED"] = "1"
        try:
            atomic_json(path, {"state": "intent", "binding": binding})
            process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=fd, stderr=subprocess.STDOUT, start_new_session=True,
                                       pass_fds=(() if self.files.lock_fd is None else (self.files.lock_fd,)))
            try:
                process.wait(timeout=timeout)
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                # Never convert interruption into proof of successful completion.
                raise
            finally:
                os.fsync(fd)
            raw = private_read(log, self.MAX_LOG)
            atomic_json(path, {"state": "finished", "binding": binding,
                              "returncode": process.returncode, "output_hash": sha256(raw).hexdigest()})
        finally:
            os.close(fd)
        if process.returncode:
            raise InvalidDelivery("repository command failed; preserve its private command receipt")
        return raw.decode("utf-8", errors="replace").strip()
