from pathlib import Path
import tempfile
import unittest

from multi_shadow_clone.orchestration.infrastructure.build_identity import BuildIdentity


class BuildIdentityIntegrationTest(unittest.TestCase):
    def test_global_instruction_content_creation_and_removal_change_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            (root / "orchestration").mkdir(parents=True)
            (root / "orchestration/roles.json").write_text("[]")
            instructions = Path(folder) / "AGENTS.md"
            identity = BuildIdentity(root, instruction_paths=(instructions,))
            absent = identity.current()
            instructions.write_text("owned instruction fixture")
            first = identity.current()
            self.assertNotEqual(absent, first)
            instructions.write_text("different owned instruction fixture")
            second = identity.current()
            self.assertNotEqual(first, second)
            instructions.unlink()
            self.assertEqual(absent, identity.current())

    def test_actual_files_new_modules_and_executable_are_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            (root / "orchestration").mkdir(parents=True)
            (root / "orchestration/roles.json").write_text("[]")
            code = root / "validator.py"; code.write_text("version = 1")
            binary = Path(folder) / "fake-codex"; binary.write_bytes(b"owned fixture binary")
            identity = BuildIdentity(root, binary)
            first = identity.current()
            self.assertEqual(first, identity.current())
            code.write_text("version = 2")
            self.assertNotEqual(first, identity.current())
            current = identity.current()
            (root / "new_validator.py").write_text("x = 1")
            self.assertNotEqual(current, identity.current())
            current = identity.current()
            binary.write_bytes(b"owned replacement binary")
            self.assertNotEqual(current, identity.current())
