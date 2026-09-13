"""Bind acceptance to the actual source checkout and optional Codex executable."""
from hashlib import sha256
import json
from pathlib import Path


class BuildIdentity:
    def __init__(self, root: Path, binary: Path | None = None, instruction_paths: tuple[Path, ...] = ()):
        self.root, self.binary = root, binary
        self.instruction_paths = instruction_paths
        self.cache = {}

    def _hash(self, path):
        stat = path.stat()
        key = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        previous = self.cache.get(path)
        if previous is not None and previous[0] == key:
            return previous[1]
        h = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        end = path.stat()
        if key != (end.st_dev, end.st_ino, end.st_size, end.st_mtime_ns, end.st_ctime_ns):
            raise ValueError("build changed during identity check")
        self.cache[path] = (key, h.hexdigest())
        return h.hexdigest()

    def current(self):
        paths = [*self.root.rglob("*.py"), self.root / "orchestration/roles.json"]
        manifest = {str(p.relative_to(self.root)): self._hash(p) for p in sorted(paths)}
        instructions = []
        for path in self.instruction_paths:
            if path.is_symlink():
                raise ValueError("instruction source symlinks require explicit acceptance")
            instructions.append(self._hash(path) if path.exists() else None)
        return {"kind": "source-checkout-v1", "source_manifest_hash": sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
                "codex_executable_hash": self._hash(self.binary) if self.binary else None,
                "instruction_source_hashes": instructions}
