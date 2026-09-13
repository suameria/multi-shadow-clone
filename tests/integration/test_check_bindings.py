from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from multi_shadow_clone.execution.infrastructure.check_bindings import CheckBindings
from multi_shadow_clone.execution.domain.checks import NodeCheck
from multi_shadow_clone.execution.domain.admission import Rejected

class CheckBindingsTest(unittest.TestCase):
    def test_reopen_reuses_exact_inputs_and_rebinding_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bindings.sqlite3'
            definition=NodeCheck('test','check.js',(('check.js','a'*64),('code.js','b'*64)),2,1000,1000)
            store=CheckBindings(path)
            self.assertIsNone(store.read('attempt','c'*64))
            self.assertEqual(store.save('attempt','c'*64,[definition]),(definition,))
            reopened=CheckBindings(path)
            self.assertEqual(reopened.read('attempt','c'*64),(definition,))
            self.assertEqual(reopened.save('attempt','c'*64,[definition]),(definition,))
            with self.assertRaises(Rejected):reopened.read('attempt','d'*64)
            with self.assertRaises(Rejected):reopened.save('attempt','c'*64,[replace(definition,inputs=(('check.js','a'*64),('code.js','e'*64)))])
            self.assertEqual(reopened.read('attempt','c'*64),(definition,))

    def test_corrupt_record_is_not_replaced_by_new_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bindings.sqlite3'
            store=CheckBindings(path)
            definition=NodeCheck('test','check.js',(('check.js','a'*64),),2,1000,1000)
            store.save('attempt','c'*64,[definition])
            with sqlite3.connect(path) as db:db.execute("UPDATE check_bindings SET body='{}'")
            with self.assertRaises(Rejected):store.read('attempt','c'*64)
            with self.assertRaises(Rejected):store.save('attempt','c'*64,[definition])
