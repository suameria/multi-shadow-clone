"""Route a frozen role contract to host-created providers, never model names from output."""
from types import MappingProxyType

from ..ports import ProviderBlocked


class RoleProvider:
    def __init__(self, settings, providers):
        self.settings = settings
        self.providers = MappingProxyType(dict(providers))
        if not self.providers:
            raise ProviderBlocked("no role providers")
        for role_id, provider in self.providers.items():
            contract = provider.contract()
            choice = settings.for_role(role_id).record()
            if any(contract.get(key) != value for key, value in choice.items()):
                raise ProviderBlocked("provider differs from frozen role settings")

    def _for(self, role_id):
        if role_id not in self.providers:
            raise ProviderBlocked("role provider is not registered")
        return self.providers[role_id]

    def contract(self):
        return {"kind": "codex-role-routing", "settings": self.settings.record(),
                "roles": {key: value.contract() for key, value in sorted(self.providers.items())}}

    def preflight(self):
        # For explicit whole-team diagnostics only; dispatch uses preflight_for.
        for provider in {id(p): p for p in self.providers.values()}.values():
            provider.preflight()

    def preflight_for(self, role_id):
        self._for(role_id).preflight()

    def execute(self, request):
        return self._for(request.role_id).execute(request)

    def reconcile(self, attempt):
        return self._for(attempt["role_id"]).reconcile(attempt)

    def archive_for(self, role_id, thread_id, turn_id):
        archive = getattr(self._for(role_id), "archive_owned", None)
        if not callable(archive):
            raise ProviderBlocked("selected provider cannot confirm archiving")
        return archive(thread_id, turn_id)

    def usage_snapshot(self):
        snapshots = []
        for provider in {id(p): p for p in self.providers.values()}.values():
            read = getattr(provider, "usage_snapshot", None)
            if callable(read):
                value = read()
                if type(value.get("observed_at")) in (int, float):
                    snapshots.append(value)
        # All providers use one account. Never add shared percentages together.
        return dict(max(snapshots, key=lambda v: v["observed_at"])) if snapshots else {
            "available": False, "scope": "account", "observed_at": None}

    def close(self):
        errors = []
        for provider in {id(p): p for p in self.providers.values()}.values():
            try:
                provider.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("one or more role providers failed to close") from errors[0]
