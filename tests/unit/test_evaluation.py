from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import unittest

from multi_shadow_clone.evaluation.application.study import Study
from multi_shadow_clone.evaluation.application.batch import Batch
from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.application.evaluation import EvaluationJobs
from tests.unit.fakes import MemoryStore, ScriptedProvider, ROLES
from multi_shadow_clone.evaluation.domain.scoring import candidate_pack, score
from multi_shadow_clone.evaluation.presentation.cli import main as evaluation_cli


CASE = {"id": "fixture", "input": {"request": "compute", "numbers": "[2,6]"},
        "fields": {"mean": "number", "unknown": "null when unavailable"},
        "expected": {"mean": 4, "unknown": None}, "basis": "owned unit fixture", "held_out": False}


class MemoryStudy:
    def __init__(self): self.records = {}
    def reserve(self, key, record):
        if key in self.records: return False
        self.records[key] = deepcopy(record); return True
    def save(self, key, record): self.records[key] = deepcopy(record)
    def read(self, key): return deepcopy(self.records.get(key))
    def list_records(self): return deepcopy(list(self.records.values()))


class EvaluationTest(unittest.TestCase):
    def test_explicit_study_selection_reaches_only_the_requested_operation(self):
        calls = []
        def execute(case, arm, study, suite):
            calls.append(("run", case, arm, study, suite)); return {}
        def report(study, suite):
            calls.append(("status", study, suite)); return {}
        with redirect_stdout(io.StringIO()):
            evaluation_cli(execute, report, ["--study", "astra-low-v1", "run", "--case", "E02", "--arm", "team"])
            evaluation_cli(execute, report, ["status"])
            evaluation_cli(execute, report, ["--suite", "role-contracts-v1", "run"])
        self.assertEqual(calls, [("run", "E02", "team", "astra-low-v1", "public-pilot-v1"),
                                 ("status", "public-pilot-v1", "public-pilot-v1"),
                                 ("run", "R01-C01", "single", "role-contracts-v1", "role-contracts-v1")])

    def test_target_role_receives_input_without_evaluator_answers(self):
        provider = ScriptedProvider()
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000)
        jobs = EvaluationJobs(engine, engine.run_until_idle, "R19")
        case = {**CASE, "role_id": "R19", "role_contract_sha256": "evaluator-only-contract-hash",
                "expected": {"mean": "answer-marker-never-send", "unknown": None}}
        study = Study(MemoryStudy(), jobs, lambda: 1000)
        row = study.run(case, "single")
        self.assertEqual([r.role_id for r in provider.requests], ["R19"])
        self.assertEqual(row["result"]["roles"], ["R19"])
        self.assertFalse(row["score"]["factual_pass"])
        self.assertNotIn("answer-marker-never-send", provider.requests[0].prompt)
        self.assertNotIn("evaluator-only-contract-hash", provider.requests[0].prompt)

    def test_unknown_oversized_and_indirect_role_selection_stops_before_scope(self):
        class Evidence:
            def begin(self): return {"cases": [{**CASE, "id": str(i), "role_id": "R19"} for i in range(60)]}
        def unexpected(*args): self.fail("invalid selection opened a model scope")
        batch = Batch(unexpected, unexpected, Evidence(), unexpected)
        for case, arm in [("missing", "single"), ("all", "single"), ("0", "team")]:
            with self.subTest(case=case, arm=arm), self.assertRaises(ValueError):
                batch.run(case, arm)

    def test_invalid_study_paths_are_rejected_before_callbacks(self):
        def unexpected(*args): self.fail("invalid study reached persistence or model code")
        for value in ["../old", "/tmp/old", "a/b", "A", "a..b", "a-", "a" * 65, ""]:
            with self.subTest(value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    evaluation_cli(unexpected, unexpected, ["--study", value, "status"])

    def test_answers_are_not_in_candidate_pack_and_missing_null_fails(self):
        pack = candidate_pack(CASE)
        self.assertEqual(set(pack), {"request", "numbers", "responseFields"})
        self.assertNotIn("expected", str(pack))
        self.assertFalse(score(CASE, {"values": {"mean": 4}})["factual_pass"])
        self.assertTrue(score(CASE, {"values": {"mean": 4, "unknown": None}})["factual_pass"])
        self.assertFalse(score(CASE, {"values": {"mean": True, "unknown": None}})["factual_pass"])

    def test_failed_trials_remain_and_cannot_be_silently_repeated(self):
        class Jobs:
            calls = 0
            def create(self, objective, sources, arm, maximum_turns):
                self.calls += 1
                return "owned"
            def run(self, run_id): return {"state": "blocked", "turns": 1}
        jobs = Jobs()
        study = Study(MemoryStudy(), jobs, lambda: 1000)
        study.run(CASE, "single")
        study.run(CASE, "single")
        self.assertEqual(jobs.calls, 1)
        self.assertEqual(study.report()["trials"], 1)
        self.assertEqual(study.report()["factual_passes"], 0)
        changed = {**CASE, "expected": {"mean": 8, "unknown": None}}
        with self.assertRaises(ValueError): study.run(changed, "single")

    def test_interrupt_stops_owned_job_and_retains_unfinished_sample(self):
        class Jobs:
            stopped = False
            def create(self, *args): return "owned"
            def run(self, run_id): raise KeyboardInterrupt()
            def stop(self, run_id): self.stopped = True
        jobs = Jobs()
        study = Study(MemoryStudy(), jobs, lambda: 1000)
        with self.assertRaises(KeyboardInterrupt): study.run(CASE, "team")
        self.assertTrue(jobs.stopped)
        self.assertEqual(study.report()["records"][0]["state"], "interrupted")
        self.assertEqual(study.report()["trials"], 1)
