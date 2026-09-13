"""Host templates produce distinct, durable checker instances per attempt."""
from types import MappingProxyType
from .bind_checks import persisted_node_checks
from ..domain.admission import Rejected


class AttemptChecks:
    def __init__(self,templates,files,store,build,profile_hash):
        templates=tuple(templates)
        for template in templates:template.validate()
        if not templates or len({t.check_id for t in templates})!=len(templates):
            raise Rejected('invalid attempt template registry')
        self.templates=MappingProxyType({t.check_id:t for t in templates})
        self.files,self.store,self.build,self.profile_hash=files,store,build,profile_hash

    def contract(self,check_id):
        template=self.templates.get(check_id)
        if template is None:raise Rejected('unregistered check template')
        return {'template_hash':template.fingerprint(),'profile_hash':self.profile_hash}

    def protected_paths(self):
        return frozenset(path for t in self.templates.values() for path,_ in t.fixed_inputs)

    def prepare(self,check_ids,attempt_id,contract_hash):
        templates=[self.templates[key] for key in check_ids]
        definitions=persisted_node_checks(self.store,attempt_id,contract_hash,templates,self.files)
        if {d.check_id for d in definitions}!=set(check_ids):
            raise Rejected('saved attempt check set changed')
        return self.build(definitions)

    def restore(self,attempt_id,contract_hash):
        definitions=self.store.read(attempt_id,contract_hash)
        if definitions is None:
            raise Rejected('no saved attempt checks to restore')
        return self.build(definitions)
