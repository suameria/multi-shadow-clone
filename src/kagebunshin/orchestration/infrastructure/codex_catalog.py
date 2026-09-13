"""Restrict model-advertised tools without changing the inference model.

Feature flags alone do not override ModelInfo.tool_mode or its advertised
experimental tools. Build a process-local catalog from this exact binary.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from ..ports import ProviderBlocked
from .codex_rpc import host_environment


POLICY = "text-only-model-catalog-v1"
TOOL_FIELDS = {"tool_mode": "direct", "experimental_supported_tools": [],
               "shell_type": "disabled", "apply_patch_tool_type": None}


def restricted_catalog(original: dict, model: str) -> dict:
    models = original.get("models") if isinstance(original, dict) else None
    if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
        raise ProviderBlocked("unsupported bundled model catalog")
    selected = [m for m in models if m.get("slug") == model]
    if len(selected) != 1:
        raise ProviderBlocked("accepted model is absent or duplicated in bundled catalog")
    source = selected[0]
    if (source.get("tool_mode") not in {"direct", "code_mode", "code_mode_only"}
        or not isinstance(source.get("experimental_supported_tools"), list)
        or any(not isinstance(t, str) for t in source["experimental_supported_tools"])
        or source.get("shell_type") not in {"disabled", "unified_exec", "default"}
        or "apply_patch_tool_type" not in source):
        raise ProviderBlocked("model tool metadata requires new compatibility acceptance")
    selected = deepcopy(source)
    selected.update(deepcopy(TOOL_FIELDS))
    # Keep weights/model id, reasoning capabilities, context and service policy.
    # No fallback model is included in the role process catalog.
    return {"models": [selected]}


@dataclass(frozen=True)
class TextOnlyCatalog:
    path: Path
    original_sha256: str
    restricted_sha256: str

    def contract(self):
        return {"policy": POLICY, "original_sha256": self.original_sha256,
                "restricted_sha256": self.restricted_sha256}

    def verify(self):
        try:
            if self.path.is_symlink() or sha256(self.path.read_bytes()).hexdigest() != self.restricted_sha256:
                raise ProviderBlocked("role tool catalog changed")
        except OSError as error:
            raise ProviderBlocked("role tool catalog is unavailable") from error


def prepare_catalog(binary: Path, directory: Path, model: str) -> TextOnlyCatalog:
    # --bundled reads the binary's catalog without a model request or refresh.
    result = subprocess.run([str(binary), "debug", "models", "--bundled"],
                            env=host_environment(), cwd=directory, capture_output=True, timeout=20)
    if result.returncode or len(result.stdout) > 2_000_000:
        raise ProviderBlocked("bundled model catalog could not be read")
    try:
        original = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise ProviderBlocked("bundled model catalog is not JSON") from error
    data = (json.dumps(restricted_catalog(original, model), ensure_ascii=False, sort_keys=True) + "\n").encode()
    path = directory / "text-only-model-catalog.json"
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o600)
    catalog = TextOnlyCatalog(path, sha256(result.stdout).hexdigest(), sha256(data).hexdigest())
    catalog.verify()
    return catalog
