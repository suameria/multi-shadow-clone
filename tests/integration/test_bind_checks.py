from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from kagebunshin.execution.application.bind_checks import bind_node_checks
from kagebunshin.execution.domain.checks import NodeCheckTemplate
from kagebunshin.execution.domain.admission import Rejected
from kagebunshin.execution.infrastructure.owned_files import OwnedFiles

class BindChecksTest(unittest.TestCase):
    def test_changed_source_rebinds_but_changed_host_test_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'check.js').write_text('fixed test')
            (root/'code.js').write_text('before')
            template=NodeCheckTemplate('test','check.js',(('check.js',sha256(b'fixed test').hexdigest()),),('code.js',),2,1000,1000)
            files=OwnedFiles(root,{'check.js','code.js'})
            try:
                first,=bind_node_checks([template],files)
                (root/'code.js').write_text('after')
                second,=bind_node_checks([template],files)
                self.assertNotEqual(first.fingerprint(),second.fingerprint())
                self.assertEqual(dict(second.inputs)['code.js'],sha256(b'after').hexdigest())
                (root/'check.js').write_text('changed test')
                with self.assertRaises(Rejected):bind_node_checks([template],files)
            finally:files.close()

    def test_change_after_individual_observation_is_not_silently_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'check.js').write_text('test')
            (root/'code.js').write_text('before')
            template=NodeCheckTemplate('test','check.js',(('check.js',sha256(b'test').hexdigest()),),('code.js',),2,1000,1000)
            files=OwnedFiles(root,{'check.js','code.js'})
            original=files.read_files
            def changed(*args):
                (root/'code.js').write_text('after')
                return original(*args)
            files.read_files=changed
            try:
                with self.assertRaises(Rejected):bind_node_checks([template],files)
            finally:files.close()

    def test_saved_attempt_recovery_never_recaptures_changed_sources(self):
        from kagebunshin.execution.application.bind_checks import persisted_node_checks
        from kagebunshin.execution.infrastructure.check_bindings import CheckBindings
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'check.js').write_text('test')
            (root/'code.js').write_text('before')
            template=NodeCheckTemplate('test','check.js',(('check.js',sha256(b'test').hexdigest()),),('code.js',),2,1000,1000)
            files=OwnedFiles(root,{'check.js','code.js'})
            store=CheckBindings(root/'bindings.sqlite3')
            try:
                original=persisted_node_checks(store,'attempt','a'*64,[template],files)
                (root/'code.js').unlink()
                files.close()
                self.assertEqual(persisted_node_checks(CheckBindings(root/'bindings.sqlite3'),'attempt','a'*64,[template],files),original)
            finally:files.close()

    def test_attempt_registry_builds_distinct_immutable_checker_inputs(self):
        from kagebunshin.execution.application.attempt_checks import AttemptChecks
        from kagebunshin.execution.infrastructure.check_bindings import CheckBindings
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'check.js').write_text('test');(root/'code.js').write_text('before')
            template=NodeCheckTemplate('test','check.js',(('check.js',sha256(b'test').hexdigest()),),('code.js',),2,1000,1000)
            files=OwnedFiles(root,{'check.js','code.js'})
            try:
                registry=AttemptChecks([template],files,CheckBindings(root/'bindings.sqlite3'),lambda definitions:definitions,'a'*64)
                contract=registry.contract('test')
                first=registry.prepare(['test'],'one','b'*64)
                (root/'code.js').write_text('after')
                second=registry.prepare(['test'],'two','b'*64)
                self.assertNotEqual(first,second)
                self.assertEqual(registry.prepare(['test'],'one','b'*64),first)
                self.assertEqual(registry.contract('test'),contract)
                self.assertEqual(registry.protected_paths(),frozenset({'check.js'}))
            finally:files.close()

    def test_restore_missing_attempt_never_observes_current_files(self):
        from kagebunshin.execution.application.attempt_checks import AttemptChecks
        from kagebunshin.execution.infrastructure.check_bindings import CheckBindings
        with tempfile.TemporaryDirectory() as directory:
            template=NodeCheckTemplate('test','check.js',(('check.js','a'*64),),('code.js',),2,1000,1000)
            registry=AttemptChecks([template],None,CheckBindings(Path(directory)/'bindings.sqlite3'),lambda value:value,'b'*64)
            with self.assertRaises(Rejected):registry.restore('missing','c'*64)
