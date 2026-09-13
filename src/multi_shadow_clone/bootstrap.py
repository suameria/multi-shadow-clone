"""Composition root. The only place that selects concrete runtime adapters."""

from pathlib import Path
from contextlib import contextmanager
import argparse
import tempfile
import time

from .orchestration.application.engine import Engine
from .orchestration.application.team import Team
from .orchestration.infrastructure.catalog import load_roles
from .orchestration.infrastructure.build_identity import BuildIdentity
from .orchestration.infrastructure.offline_provider import OfflineProvider
from .orchestration.infrastructure.sqlite_store import SQLiteRunStore
from .orchestration.infrastructure.workers import Workers
from .orchestration.presentation.cli import main


@contextmanager
def owned_job_sessions(*, data_dir, workspace_id, workspace_root, paths,
                       check_definitions=None, check_templates=None, container_profile=None, clock=time.time, lease_path=None):
    """Host-only registration. Model output cannot select roots or runtimes."""
    from .execution.application.executor import Executor
    from .execution.application.reservations import Reservations
    from .execution.infrastructure.owned_files import OwnedFiles
    from .execution.infrastructure.sqlite_store import SQLiteOperationStore
    from .execution.infrastructure.write_journal import WriteJournal, JournaledWriter
    from .execution.presentation.job_sessions import JobSessions
    from .execution.infrastructure.workspace_leases import WorkspaceLeases
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = OwnedFiles(workspace_root, paths)
    try:
        reservations = Reservations(SQLiteOperationStore(data_dir / 'operations.sqlite3'), clock)
        writer = JournaledWriter(files, WriteJournal(data_dir / 'writes.sqlite3'))
        checkers, attempt_checkers = {}, {}
        if check_templates is not None:
            if check_definitions is not None or container_profile is None:
                raise ValueError('templates require a profile and cannot mix with fixed definitions')
            from .execution.application.attempt_checks import AttemptChecks
            from .execution.infrastructure.check_bindings import CheckBindings
            from .execution.presentation.job_sessions import fingerprint
            from copy import deepcopy
            profile = deepcopy(container_profile)
            def build_checks(definitions):
                return owned_node_checks(definitions=definitions, files=files, profile=profile,
                                         journal_path=data_dir / 'containers.sqlite3')
            attempt_checkers[workspace_id] = AttemptChecks(check_templates, files,
                CheckBindings(data_dir / 'check-bindings.sqlite3'), build_checks, fingerprint(profile))
        if check_definitions is not None:
            if container_profile is None:
                raise ValueError('registered checks require an explicit container profile')
            checkers[workspace_id] = owned_node_checks(definitions=check_definitions, files=files,
                profile=container_profile, journal_path=data_dir / 'containers.sqlite3')
        executor = Executor(reservations, {workspace_id: files},
                            writers={workspace_id: writer}, checkers=checkers)
        yield JobSessions(reservations, executor, {workspace_id: files.contract}, attempt_checkers=attempt_checkers,
                          leases=WorkspaceLeases(lease_path or Path.home() / ".local/share/multi-shadow-clone/workspace-leases.sqlite3"))
    finally:
        files.close()


def owned_container_service(contract,journal_path):
    """Internal host-only composition; not a model tool or automatic fallback."""
    from .execution.application.containers import Containers
    from .execution.domain.admission import Rejected
    from .execution.infrastructure.container_journal import ContainerJournal
    from .execution.infrastructure.docker_sandbox import NodeContainer, SnapshotMount
    from .execution.infrastructure.local_containers import LocalContainers
    required={'kind','executable','executable_hash','socket','server_id','server_version','spec'}
    if type(contract) is not dict or set(contract)!=required or contract['kind']!='local-container-v1':
        raise Rejected('invalid container runtime contract')
    spec=dict(contract['spec'])
    spec['arguments']=tuple(spec['arguments'])
    if spec.get('snapshot') is not None:
        spec['snapshot']=SnapshotMount(**spec['snapshot'])
    runtime=LocalContainers(executable=contract['executable'],executable_hash=contract['executable_hash'],
        socket_path=contract['socket'],server_id=contract['server_id'],server_version=contract['server_version'],
        spec=NodeContainer(**spec))
    if runtime.contract()!=contract:
        raise Rejected('container runtime changed on reconstruction')
    return Containers(ContainerJournal(journal_path),runtime)


def container_supervisor_main():
    from .execution.infrastructure.container_supervisor import serve
    serve(owned_container_service)


def owned_node_checks(*,definitions,files,profile,journal_path,snapshot_parent=None):
    """Assemble an explicitly selected local Node checker, without model calls."""
    from dataclasses import asdict
    from .execution.infrastructure.container_journal import ContainerJournal
    from .execution.infrastructure.node_checks import NodeChecks
    journal=ContainerJournal(journal_path)
    def service(spec):
        contract={key:value for key,value in profile.items() if key!='image_id'}
        contract['spec']=asdict(spec)
        # JSON-normalize tuples like the persisted runtime contract does.
        import json
        return owned_container_service(json.loads(json.dumps(contract)),journal_path)
    return NodeChecks(definitions=definitions,files=files,profile=profile,service_factory=service,
        journal=journal,snapshot_parent=snapshot_parent)


def worktree_run(argv=None) -> int:
    from .delivery.presentation.worktree_cli import main as worktree_main
    return worktree_main(owned_environment(), argv)


def owned_environment():
    from .delivery.application.environment import EnvironmentLifecycle
    from .delivery.infrastructure.worktree import RepositoryEnvironment
    from .delivery.infrastructure.lifecycle_files import LifecycleFiles
    root = Path(__file__).resolve().parents[2]
    files = LifecycleFiles(root / "runtime/granskypolis-worktree")
    repository = RepositoryEnvironment(Path.home() / "workspaces/code/suameria-services",
                                       Path.home() / ".codex/worktrees/suameria-services", files)
    return EnvironmentLifecycle(files, repository)


def operator_run(argv=None) -> int:
    from .orchestration.application.operator import Operator
    from .orchestration.infrastructure.operator_jobs import OperatorJobs
    from .orchestration.infrastructure.operator_server import OperatorServer
    from .orchestration.presentation.operator_cli import main as operator_main
    from .delivery.application.overview import DeliveryOverview
    from .delivery.infrastructure.sqlite_store import SQLiteDeliveryStore
    root = Path(__file__).resolve().parents[2]
    from contextlib import ExitStack
    from threading import RLock
    from .orchestration.application.configured_jobs import ConfiguredJobs
    with ExitStack() as scopes:
        engines, mutex = {}, RLock()
        def operator_factory(engine, team, jobs, clock, delivery=None, settings=None):
            configured = None
            if settings is not None:
                def create_engine(choices):
                    key = choices.fingerprint()
                    with mutex:
                        if key not in engines:
                            engines[key] = scopes.enter_context(codex_engine(engine.store.path.parent, agent_settings=choices, tool_sessions=engine.tool_sessions))
                        return engines[key]
                configured = ConfiguredJobs(engine, settings, create_engine, lambda e: Workers(e).run)
            return Operator(engine, team, jobs, clock, delivery=delivery, settings=settings, configured_jobs=configured)
        return operator_main(engine_scope, lambda e: Team(e, Workers(e).run), operator_factory, OperatorJobs, OperatorServer, argv,
                             delivery_factory=lambda: DeliveryOverview(SQLiteDeliveryStore(root / "runtime/delivery/outbox.sqlite3"), owned_environment()),
                             settings_factory=operator_settings)


def operator_settings():
    from .orchestration.application.settings import Settings
    from .orchestration.domain.agent_settings import ACCEPTED_MODELS
    from .orchestration.infrastructure.settings_store import SQLiteSettingsStore
    from .orchestration.infrastructure.codex_rpc import StdioRPC, codex_command
    binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="multi-shadow-clone-settings-catalog-") as folder:
        with StdioRPC(codex_command(binary), Path(folder)) as rpc:
            rpc.initialize()
            models = rpc.call("model/list", {})
            catalog = {m["model"]: tuple(e["reasoningEffort"] for e in m["supportedReasoningEfforts"]
                                         if e["reasoningEffort"] != "ultra")
                       for m in models.get("data", []) if m.get("model") in ACCEPTED_MODELS}
    return Settings(SQLiteSettingsStore(root / "runtime/settings/models.sqlite3"), load_roles().keys(), catalog)


@contextmanager
def engine_scope(mode, data_dir=None, workspace_config=None):
    from contextlib import ExitStack
    directory = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parents[2] / "runtime" / ("multi-shadow-clone-" + mode)
    with ExitStack() as stack:
        sessions = None
        if workspace_config is not None:
            from .execution.presentation.workspace_config import load_workspace_config
            sessions = stack.enter_context(owned_job_sessions(
                data_dir=directory / "execution", **load_workspace_config(workspace_config)))
        if mode == "offline":
            yield Engine(SQLiteRunStore(directory / "jobs.sqlite3"), OfflineProvider(), load_roles(), time.time,
                         build_identity=BuildIdentity(Path(__file__).resolve().parent), tool_sessions=sessions)
        elif mode == "codex":
            yield stack.enter_context(codex_engine(directory, tool_sessions=sessions))
        else:
            raise ValueError("unsupported mode")


def knowledge_run(argv=None) -> int:
    from .knowledge.application.library import Library
    from .knowledge.infrastructure.sqlite_store import SQLiteKnowledgeStore
    from .knowledge.presentation.cli import main as knowledge_main
    from .knowledge.application.workflow import KnowledgeWorkflow
    from .knowledge.infrastructure.accepted_review import AcceptedClaimReview
    from .knowledge.infrastructure.job_bridge import EvidenceJobs
    root = Path(__file__).resolve().parents[2]
    library = Library(SQLiteKnowledgeStore(root / "runtime/knowledge/library.sqlite3"))
    with codex_engine(root / "runtime/multi-shadow-clone-codex") as engine:
        engine.evidence = library
        library.review = AcceptedClaimReview(engine)
        return knowledge_main(library, argv, KnowledgeWorkflow(library, EvidenceJobs(engine)))


def run(argv: list[str] | None = None, data_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=["offline", "codex"], default="offline")
    parser.add_argument("--workspace-config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    settings, remaining = parser.parse_known_args(argv)
    data_dir = data_dir or settings.data_dir or Path(__file__).resolve().parents[2] / "runtime" / ("multi-shadow-clone-" + settings.mode)
    from contextlib import ExitStack
    with ExitStack() as stack:
        sessions = None
        if settings.workspace_config:
            from .execution.presentation.workspace_config import load_workspace_config
            config = load_workspace_config(settings.workspace_config)
            sessions = stack.enter_context(owned_job_sessions(data_dir=data_dir / 'execution', **config))
        if settings.mode == "offline":
            engine = Engine(SQLiteRunStore(data_dir / "jobs.sqlite3"), OfflineProvider(), load_roles(), time.time,
                            build_identity=BuildIdentity(Path(__file__).resolve().parent), tool_sessions=sessions)
        else:
            engine = stack.enter_context(codex_engine(data_dir, tool_sessions=sessions))
        return main(engine, remaining, mode=settings.mode, team=Team(engine, Workers(engine).run))



@contextmanager
def codex_provider_scope(choice=None):
    from .orchestration.domain.agent_settings import ModelChoice
    from .orchestration.infrastructure.codex_provider import CodexProfile, CodexProvider
    from .orchestration.infrastructure.codex_rpc import StdioRPC, codex_command
    from .orchestration.infrastructure.codex_catalog import prepare_catalog
    binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    choice = choice or ModelChoice()
    with tempfile.TemporaryDirectory(prefix="multi-shadow-clone-text-only-") as folder:
        workspace = Path(folder)
        instructions = Path.home() / ".codex/AGENTS.md"
        profile = CodexProfile(binary, workspace, model=choice.model, effort=choice.effort,
                               instruction_sources=(str(instructions),) if instructions.exists() else ())
        catalog = prepare_catalog(binary, workspace, profile.model)
        with StdioRPC(codex_command(binary), workspace) as rpc:
            rpc.initialize()
            conf = rpc.call("config/read", {"includeLayers": False})["config"]
            names = tuple(conf.get("mcp_servers", {}))
            models = rpc.call("model/list", {})
            available = {m["model"]: tuple(e["reasoningEffort"] for e in m["supportedReasoningEfforts"])
                         for m in models.get("data", [])}
            choice.validate(available)
        provider = CodexProvider(profile, names, catalog=catalog)
        try:
            yield provider
        finally:
            provider.close()


@contextmanager
def codex_engine(data_dir: Path, *, agent_settings=None, tool_sessions=None):
    from contextlib import ExitStack
    from .orchestration.application.role_provider import RoleProvider
    from .knowledge.application.library import Library
    from .knowledge.infrastructure.sqlite_store import SQLiteKnowledgeStore
    binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    instructions = Path.home() / ".codex/AGENTS.md"
    roles = load_roles()
    with ExitStack() as stack:
        if agent_settings is None:
            provider = stack.enter_context(codex_provider_scope())
        else:
            if not {r for r, _ in agent_settings.overrides} <= roles.keys():
                raise ValueError("unknown role override")
            pool, routing = {}, {}
            for role_id in roles:
                choice = agent_settings.for_role(role_id)
                if choice not in pool:
                    pool[choice] = stack.enter_context(codex_provider_scope(choice))
                routing[role_id] = pool[choice]
            provider = RoleProvider(agent_settings, routing)
        engine = Engine(SQLiteRunStore(data_dir / "jobs.sqlite3"), provider, roles, time.time,
                        build_identity=BuildIdentity(Path(__file__).resolve().parent, binary, (instructions,)),
                        tool_sessions=tool_sessions)
        engine.evidence = Library(SQLiteKnowledgeStore(Path(__file__).resolve().parents[2] / "runtime/knowledge/library.sqlite3"))
        for source_id in engine.evidence.status()["erasures"]:
            engine.erase_evidence(source_id)
        yield engine


def delivery_run(argv=None) -> int:
    from .delivery.application.outbox import Outbox
    from .delivery.infrastructure.granskypolis import GranSkypolisDestination
    from .delivery.infrastructure.sqlite_store import SQLiteDeliveryStore
    from .delivery.presentation.cli import main as delivery_main
    root = Path(__file__).resolve().parents[2]
    lifecycle = owned_environment()
    class LazyDestination:
        def __init__(self): self.client = None
        def get(self):
            if self.client is None:
                self.client = GranSkypolisDestination(lifecycle)
            return self.client
        def capabilities(self): return self.get().capabilities()
        def validate(self, payload): return self.get().validate(payload)
        def find(self, key, payload_hash): return self.get().find(key, payload_hash)
        def send(self, payload, key):
            client = self.get()
            if client.csrf is None:
                client.login_fixture()
            return client.send(payload, key)
    with codex_engine(root / "runtime/multi-shadow-clone-codex") as engine:
        outbox = Outbox(SQLiteDeliveryStore(root / "runtime/delivery/outbox.sqlite3"), engine, LazyDestination())
        return delivery_main(outbox, argv)


def evaluation_run(argv=None) -> int:
    from dataclasses import replace
    import json
    from .evaluation.application.batch import Batch
    from .evaluation.application.study import Study
    from .evaluation.infrastructure.sqlite_store import SQLiteStudyStore
    from .evaluation.infrastructure.verification import VerifiedStudy
    from .evaluation.presentation.cli import main as evaluation_main
    from .orchestration.application.evaluation import EvaluationJobs
    root = Path(__file__).resolve().parents[2]
    def edition(study_id, suite):
        directory = root / "runtime/evaluation" / study_id
        store = SQLiteStudyStore(directory / "study.sqlite3")
        evidence = VerifiedStudy(root, root / "examples/evaluation" / (suite + ".json"),
                                 root / "evidence/foundation" / ("evaluation-" + study_id + ".json"))
        def report():
            return {**Study(store, None, time.time).report(), "study_id": study_id,
                    "requested_case_definition": suite, "new_edition_is_not_a_new_held_out_dataset": True}
        @contextmanager
        def study_scope(arm, case):
            with codex_engine(directory / arm) as engine:
                if arm == "neutral-label":
                    engine.roles = {k: replace(r, name=r.id) for k, r in engine.roles.items()}
                engine.provider.preflight()
                yield Study(store, EvaluationJobs(engine, Workers(engine).run, case.get("role_id", "R01"),
                      {k: v.partition(":")[0] for k,v in case["fields"].items()} if "role_id" in case else None), time.time, evidence.manifest_hash)
        return Batch(study_scope, report, evidence, lambda event: print(json.dumps(event), flush=True)), report
    def execute(case, arm, study_id, suite):
        batch, _ = edition(study_id, suite)
        return {**batch.run(case, arm), "study_id": study_id}
    return evaluation_main(execute, lambda study_id, suite: edition(study_id, suite)[1](), argv)
