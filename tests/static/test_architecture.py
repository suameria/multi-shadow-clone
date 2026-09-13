"""Static architecture evidence, separate from behavioral unit tests."""

import ast
from pathlib import Path
import unittest


class ArchitectureTest(unittest.TestCase):
    def test_inward_dependencies(self):
        root = Path(__file__).resolve().parents[2] / "src" / "kagebunshin"
        domain_forbidden = {"os", "sys", "pathlib", "subprocess", "sqlite3", "socket", "urllib", "http", "requests", "time"}
        for path in root.rglob("*.py"):
            parts = set(path.relative_to(root).parts)
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [n.name for n in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    names = set(module.split("."))
                    with self.subTest(path=path.name, module=module):
                        self.assertFalse(names & {"botteam", "poc", "integration"})
                        if "domain" in parts:
                            self.assertFalse(names & (domain_forbidden | {"application", "infrastructure", "presentation"}))
                        if "application" in parts or path.name == "ports.py":
                            self.assertFalse(names & {"infrastructure", "presentation", "bootstrap"})
                        if "presentation" in parts:
                            self.assertFalse(names & {"infrastructure", "bootstrap"})
