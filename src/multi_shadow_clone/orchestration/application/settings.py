"""Validate user choices before publishing a new settings revision."""
from ..domain.agent_settings import AgentSettings
from ..ports import SettingsStore


class Settings:
    def __init__(self, store: SettingsStore, role_ids, catalog):
        self.store, self.role_ids, self.catalog = store, set(role_ids), catalog

    def read(self, revision=None):
        return self.store.read(revision)

    def capture(self):
        saved = self.read()
        value = saved["value"] or {"default": {"model": "gpt-5.6-luna", "effort": "low"}, "overrides": {}}
        settings = AgentSettings.parse(value, self.role_ids, self.catalog)
        return settings, {"revision": saved["revision"], "settings": settings.record(), "settings_hash": settings.fingerprint()}

    def save(self, value, expected_revision):
        settings = AgentSettings.parse(value, self.role_ids, self.catalog)
        # Serialize only editable fields; service tier is controller-owned.
        editable = {"default": {"model": settings.default.model, "effort": settings.default.effort},
                    "overrides": {r: {"model": c.model, "effort": c.effort} for r, c in settings.overrides}}
        return self.store.save(editable, expected_revision)
