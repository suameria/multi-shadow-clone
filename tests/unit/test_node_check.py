import unittest

from multi_shadow_clone.execution.domain.admission import Rejected
from multi_shadow_clone.execution.domain.checks import NodeCheck, PythonCheck
from multi_shadow_clone.execution.infrastructure.node_inputs import node_arguments
from multi_shadow_clone.execution.infrastructure.python_checks import PythonChecks


class NodeCheckTest(unittest.TestCase):
    def test_runtime_fingerprint_cannot_alias_python(self):
        arguments=('sample','check.js',(('check.js','a'*64),),2,1024,1024)
        node=NodeCheck(*arguments)
        python=PythonCheck(*arguments)
        self.assertNotEqual(node.fingerprint(),python.fingerprint())
        with self.assertRaises(Rejected):
            PythonChecks(definitions=[node],files=None,runtime='.',executable='.',executable_sha256='a'*64)

    def test_manifest_rejects_missing_entry_duplicates_and_oversize(self):
        for inputs,entry in [([('other.js','a'*64,1)],'check.js'),
            ([('check.js','a'*64,1)]*2,'check.js'),([('check.js','a'*64,16777217)],'check.js')]:
            with self.assertRaises(Rejected): node_arguments(inputs,entry)

    def test_template_pins_test_code_and_changes_only_explicit_source_hash(self):
        from multi_shadow_clone.execution.domain.checks import NodeCheckTemplate
        template=NodeCheckTemplate('sum','check.js',(('check.js','a'*64),),('calc.js',),2,1024,1024)
        observed={'check.js':{'exists':True,'sha256':'a'*64,'bytes':10},'calc.js':{'exists':True,'sha256':'b'*64,'bytes':10}}
        first=template.bind(observed)
        identity=template.fingerprint()
        observed['calc.js']['sha256']='c'*64
        second=template.bind(observed)
        self.assertNotEqual(first.fingerprint(),second.fingerprint())
        self.assertEqual(template.fingerprint(),identity)
        self.assertEqual(dict(first.inputs)['calc.js'],'b'*64)
        observed['check.js']['sha256']='c'*64
        with self.assertRaises(Rejected):template.bind(observed)

    def test_template_rejects_mutable_entrypoint_missing_and_extra_inputs(self):
        from multi_shadow_clone.execution.domain.checks import NodeCheckTemplate
        template=NodeCheckTemplate('sum','check.js',(),('check.js',),2,1024,1024)
        with self.assertRaises(Rejected):template.validate()
        template=NodeCheckTemplate('sum','check.js',(('check.js','a'*64),),('calc.js',),2,1024,1024)
        for observed in ({}, {'extra':{}}, {'check.js':{'exists':True,'sha256':'a'*64,'bytes':10}}):
            with self.assertRaises(Rejected):template.bind(observed)
