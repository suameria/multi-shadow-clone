from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from multi_shadow_clone.orchestration.application.settings import Settings
from multi_shadow_clone.orchestration.infrastructure.settings_store import SQLiteSettingsStore
from multi_shadow_clone.orchestration.ports import Conflict
from multi_shadow_clone.orchestration.domain.contracts import InvalidContract


class SettingsStoreTest(unittest.TestCase):
    def test_restart_old_revision_and_stale_writer(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.sqlite3"
            store = SQLiteSettingsStore(path)
            app = Settings(store, {"R01"}, {"gpt-5.6-luna": ("low", "medium")})
            value = {"default": {"model": "gpt-5.6-luna", "effort": "low"}, "overrides": {}}
            first = app.save(value, 0)
            value["default"]["effort"] = "medium"
            second = app.save(value, 1)
            with self.assertRaises(Conflict): app.save(value, 1)
            reopened = SQLiteSettingsStore(path)
            self.assertEqual(reopened.read(1), first)
            self.assertEqual(reopened.read(), second)
            value["default"]["model"] = "unavailable"
            with self.assertRaises(InvalidContract): app.save(value, 2)
            self.assertEqual(reopened.read(), second)
