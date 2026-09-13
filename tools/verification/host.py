#!/usr/bin/env python3
"""Run the new foundation suites, separately from historical PoC tests."""

from pathlib import Path
from datetime import datetime, timezone
from hashlib import sha256
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    suite = unittest.TestSuite()
    for name in ("unit", "contracts", "integration", "static"):
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / "tests" / name), top_level_dir=str(ROOT)))
    class RecordedResult(unittest.TextTestResult):
        def __init__(self, *args):
            super().__init__(*args)
            self.passed = []

        def addSuccess(self, test):
            self.passed.append(test.id())
            super().addSuccess(test)

    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    directory = ROOT / "evidence" / "foundation"
    directory.mkdir(parents=True, exist_ok=True)
    files = [*sorted((ROOT / "src" / "kagebunshin").rglob("*.py")),
             *sorted((ROOT / "src" / "kagebunshin").rglob("*.html")),
             *sorted((ROOT / "src" / "kagebunshin").rglob("*.css")),
             *sorted((ROOT / "src" / "kagebunshin").rglob("*.js")),
             *sorted((ROOT / "tests" / "unit").rglob("*.py")),
             *sorted((ROOT / "tests" / "integration").rglob("*.py")),
             *sorted((ROOT / "tests" / "contracts").rglob("*.py")),
             *sorted((ROOT / "tests" / "static").rglob("*.py")),
             *sorted((ROOT / "tests" / "fixtures").rglob("*.json")),
             *sorted((ROOT / "tools" / "kagebunshin").glob("*.py")),
             *sorted((ROOT / "tools" / "verification").glob("*.py")),
             *sorted((ROOT / "examples/evaluation").glob("*.json")),
             ROOT / "src/kagebunshin/orchestration/roles.json"]
    record = {"at": datetime.now(timezone.utc).isoformat(), "success": result.wasSuccessful(),
              "unit_passed": sum("tests.unit." in t and "test_architecture" not in t for t in result.passed),
              "integration_passed": sum("tests.integration." in t for t in result.passed),
              "contract_passed": sum("tests.contracts." in t for t in result.passed),
              "static_passed": sum("test_architecture" in t for t in result.passed),
              "passed": result.passed, "failures": [t.id() for t, _ in result.failures],
              "errors": [t.id() for t, _ in result.errors], "actual_model_calls": 0,
              "source_sha256": {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in files}}
    (directory / "test-results.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(not result.wasSuccessful())
