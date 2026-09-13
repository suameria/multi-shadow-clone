"""Real filesystem integrity for the process-local tool catalog; zero models."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kagebunshin.orchestration.infrastructure.codex_catalog import TextOnlyCatalog
from kagebunshin.orchestration.ports import ProviderBlocked


class CatalogFileTest(unittest.TestCase):
    def test_changed_deleted_or_symlinked_catalog_is_rejected(self):
        with TemporaryDirectory(prefix="kagebunshin-catalog-test-") as directory:
            path = Path(directory) / "catalog.json"
            data = b'{"models": []}\n'
            path.write_bytes(data)
            catalog = TextOnlyCatalog(path, "a" * 64, sha256(data).hexdigest())
            catalog.verify()
            path.write_bytes(b'{"models": ["unexpected"]}\n')
            with self.assertRaises(ProviderBlocked):
                catalog.verify()
            path.unlink()
            with self.assertRaises(ProviderBlocked):
                catalog.verify()
            sibling = Path(directory) / "replacement.json"
            sibling.write_bytes(data)
            path.symlink_to(sibling)
            with self.assertRaises(ProviderBlocked):
                catalog.verify()
