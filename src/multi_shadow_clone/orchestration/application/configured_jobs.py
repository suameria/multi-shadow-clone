"""Resolve each job against its saved settings, not today's defaults."""
from ..domain.agent_settings import AgentSettings
from ..domain.contracts import InvalidContract, digest
from .team import Team


class ConfiguredJobs:
    def __init__(self, base_engine, settings, engine_factory, graph_factory):
        self.base = base_engine
        self.settings = settings
        self.engine_factory, self.graph_factory = engine_factory, graph_factory

    def for_run(self, run_id):
        run = self.base.status(run_id)
        saved = run.get("execution_settings")
        if saved is None:
            return self.base
        if digest(saved["settings"]) != saved["settings_hash"]:
            raise InvalidContract("saved model settings changed")
        record = saved["settings"]
        def editable(choice):
            if choice.get("service_tier") != "default":
                raise InvalidContract("saved speed is unsupported")
            return {"model": choice["model"], "effort": choice["effort"]}
        value = {"default": editable(record["default"]),
                 "overrides": {r: editable(c) for r, c in record["overrides"].items()}}
        settings = AgentSettings.parse(value, self.settings.role_ids, self.settings.catalog)
        return self.engine_factory(settings)

    def submit(self, objective, sources, limits):
        settings, frozen = self.settings.capture()
        engine = self.engine_factory(settings)
        return Team(engine, self.graph_factory(engine)).submit(objective, sources, limits, execution_settings=frozen)

    def advance(self, run_id):
        engine = self.for_run(run_id)
        return Team(engine, self.graph_factory(engine)).advance(run_id)
