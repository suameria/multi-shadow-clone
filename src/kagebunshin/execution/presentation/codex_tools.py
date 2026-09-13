"""Translate an exact Codex dynamic call into a bounded execution reservation."""
import json
from dataclasses import replace

from ..domain.admission import Rejected


OPERATIONS={'kagebunshin_read_files':'read_files','kagebunshin_apply_changes':'apply_changes',
            'kagebunshin_run_check':'run_check'}


def declarations(grant):
    def obj(properties):
        return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
    digest={'type':'string','pattern':'^[0-9a-f]{64}$'}
    path={'type':'string','enum':sorted(grant.paths)}
    schemas={
        'read_files':obj({'files':{'type':'array','minItems':1,'maxItems':min(256,len(grant.paths)),
            'items':obj({'path':path,'expected_hash':digest})},
            'max_bytes':{'type':'integer','minimum':1,'maximum':grant.max_bytes}}),
        'apply_changes':obj({'path':path,'before_hash':{'anyOf':[digest,{'type':'null'}]},
            'content':{'type':['string','null'],'maxLength':grant.max_bytes}}),
        'run_check':obj({'check_id':{'type':'string','enum':sorted(grant.checks)}})}
    descriptions={'read_files':'Read only granted files matching supplied SHA256 hashes.',
        'apply_changes':'Apply one granted file change only if its previous SHA256 matches. Null content deletes.',
        'run_check':'Run one host-registered check in its owned isolated environment.'}
    return [{'type':'function','name':name,'description':descriptions[operation],'inputSchema':schemas[operation]}
            for name,operation in OPERATIONS.items() if operation in grant.operations
            and (grant.checks if operation=='run_check' else grant.paths)]


class CodexTools:
    def __init__(self,binding,reservations,executor,*,max_reply_bytes=262144,may_continue=None):
        if type(max_reply_bytes) is not int or not 1024<=max_reply_bytes<=1048576:
            raise Rejected('invalid tool response bound')
        self.binding=binding
        self.reservations=reservations
        self.executor=executor
        self.max_reply_bytes=max_reply_bytes
        self.may_continue=may_continue

    def handle(self,params):
        required={'threadId','turnId','callId','tool','arguments'}
        if (type(params) is not dict or set(params) not in (required,required|{'namespace'})
            or params.get('namespace') is not None or params['threadId']!=self.binding.thread_id
            or params['turnId']!=self.binding.turn_id or type(params['tool']) is not str
            or params['tool'] not in OPERATIONS):
            raise Rejected('dynamic tool call differs from its owned binding')
        if self.may_continue is not None:
            try:
                allowed = self.may_continue() is True
            except Exception:
                allowed = False
            if not allowed:
                self.reservations.stop(self.binding.grant_id)
                raise Rejected('host job stopped or continuation unavailable')
        proposal={'call_id':params['callId'],'operation':OPERATIONS[params['tool']],
                  'arguments':params['arguments']}
        saved=self.reservations.reserve(self.binding,[proposal])
        call=saved['calls'][params['callId']]
        if call['state']=='reserved':
            call=self.executor.execute(self.binding,params['callId'],may_continue=self.may_continue)
        # Unknown calls are never rerun. Recovery is a separate host operation.
        value={'state':call['state']}
        if call['state']=='completed':
            value['receipt']=call['receipt']
        text=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        success=call['state']=='completed'
        if len(text.encode())>self.max_reply_bytes:
            text=json.dumps({'state':call['state'],'result':'response_exceeds_bound'})
            success=False
        return {'success':success,'contentItems':[{'type':'inputText','text':text}]}


class PreparedCodexTools:
    """Host-prepared grant, activated once actual provider identities are known."""
    def __init__(self,grant,reservations,executor,*,check_contracts=None,may_continue=None):
        self.grant=grant
        self.reservations=reservations
        self.executor=executor
        self.check_contracts=check_contracts
        self.may_continue=may_continue
        self.gateway=None

    def definitions(self):
        return declarations(self.grant)

    def validate_request(self,attempt_id,run_id,node_id,binding_hash):
        binding=self.grant.binding
        if (self.gateway is not None or (attempt_id,run_id,node_id,binding_hash)!=
            (binding.attempt_id,binding.run_id,binding.node_id,binding.contract_hash)):
            raise Rejected('tool grant differs from the model attempt')

    def bind(self,thread_id,turn_id):
        if self.gateway is not None:
            raise Rejected('tool grant already bound')
        binding=replace(self.grant.binding,thread_id=thread_id,turn_id=turn_id)
        grant=replace(self.grant,binding=binding)
        self.reservations.register(grant,check_contracts=self.check_contracts)
        self.gateway=CodexTools(binding,self.reservations,self.executor,may_continue=self.may_continue)

    def handle(self,params):
        if self.gateway is None:
            raise Rejected('tool grant has no confirmed turn')
        return self.gateway.handle(params)
