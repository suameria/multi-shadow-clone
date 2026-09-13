"""Read packaged, versioned role contracts; never import research scripts."""

import json
from pathlib import Path

from ..domain.contracts import InvalidContract, Role


def load_roles(path: Path | None = None) -> dict[str, Role]:
    path = path or Path(__file__).parents[1] / "roles.json"
    items = json.loads(path.read_text())
    roles = {item["id"]: Role(**item) for item in items}
    if len(roles) != len(items):
        raise InvalidContract("duplicate role id")
    return roles
