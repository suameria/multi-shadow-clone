"""Host-authored, exact-node tool authority; role selection is not authority."""
from copy import deepcopy
import re

from .contracts import InvalidContract, positive_int


def validate_policy(policy, plan):
    """Return a detached JSON contract. No wildcard or inferred grants.

    max_calls is per attempt; the worst-case job bound is checked explicitly.
    Runtime hashes are supplied by the host and must also be checked by its
    adapter before dispatch. This pure validator does not establish ownership.
    """
    if type(policy) is not dict or set(policy) != {'version', 'max_calls', 'scopes'}:
        raise InvalidContract('invalid execution policy fields')
    if type(policy['version']) is not int or policy['version'] != 1:
        raise InvalidContract('unsupported execution policy')
    positive_int(policy['max_calls'], 'job tool calls', 1000)
    scopes = policy['scopes']
    if type(scopes) is not list or not 1 <= len(scopes) <= 100:
        raise InvalidContract('invalid execution scopes')
    nodes = {node.id: node for node in plan.nodes}
    seen, worst_case = set(), 0
    for scope in scopes:
        fields = {'node_id', 'stage', 'role_id', 'workspace_id', 'runtime_hash',
                  'operations', 'paths', 'checks', 'max_calls', 'max_bytes'}
        if type(scope) is not dict or set(scope) not in (fields, fields | {'required_checks'}):
            raise InvalidContract('invalid execution scope fields')
        for key in ('node_id', 'role_id', 'workspace_id'):
            if type(scope[key]) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', scope[key]):
                raise InvalidContract('invalid execution identity')
        node = nodes.get(scope['node_id'])
        stage = scope['stage']
        if type(stage) is not str or stage not in {'generate', 'audit'} or node is None:
            raise InvalidContract('unknown execution node or stage')
        if scope['role_id'] != (node.role_id if stage == 'generate' else node.audit_role):
            raise InvalidContract('execution role does not match exact node stage')
        if node.response_format == 'plan':
            raise InvalidContract('planner cannot acquire execution authority')
        key = (node.id, stage)
        if key in seen:
            raise InvalidContract('duplicate execution scope')
        seen.add(key)
        if type(scope['runtime_hash']) is not str or not re.fullmatch('[0-9a-f]{64}', scope['runtime_hash']):
            raise InvalidContract('invalid execution runtime hash')
        for key in ('operations', 'paths', 'checks'):
            values = scope[key]
            if (type(values) is not list or len(values) > 256
                or any(type(v) is not str for v in values) or len(set(values)) != len(values)):
                raise InvalidContract('invalid execution capability list')
        operations = set(scope['operations'])
        if not operations or not operations <= {'read_files', 'apply_changes', 'run_check'}:
            raise InvalidContract('unsupported execution operation')
        if bool(scope['paths']) != bool(operations & {'read_files', 'apply_changes'}):
            raise InvalidContract('file operation requires explicit paths')
        if bool(scope['checks']) != ('run_check' in operations):
            raise InvalidContract('check operation requires registered checks')
        for path in scope['paths']:
            if (not path or '\\' in path or any(ord(c) < 32 for c in path)
                or any(p in {'', '.', '..'} for p in path.split('/'))):
                raise InvalidContract('invalid execution path')
        for check in scope['checks']:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', check):
                raise InvalidContract('invalid registered check identity')
        required = scope.get('required_checks', [])
        if (type(required) is not list or any(type(v) is not str for v in required)
            or len(set(required)) != len(required) or not set(required) <= set(scope['checks'])):
            raise InvalidContract('required checks must be uniquely registered in this scope')
        positive_int(scope['max_calls'], 'attempt tool calls', 100)
        if len(required) > scope['max_calls']:
            raise InvalidContract('required checks exceed attempt call budget')
        positive_int(scope['max_bytes'], 'tool bytes', 1048576)
        worst_case += scope['max_calls'] * node.max_attempts
    if worst_case > policy['max_calls']:
        raise InvalidContract('retry scopes exceed job tool call budget')
    def ancestors(node_id, seen=None):
        visited = set() if seen is None else seen
        for dependency in nodes[node_id].dependencies:
            if dependency not in visited:
                visited.add(dependency)
                ancestors(dependency, visited)
        return visited
    for index, left in enumerate(scopes):
        for right in scopes[index + 1:]:
            if left['workspace_id'] != right['workspace_id'] or left['node_id'] == right['node_id']:
                continue
            if 'apply_changes' not in left['operations'] + right['operations']:
                continue
            overlap = (set(left['paths']) & set(right['paths'])
                       or 'run_check' in left['operations'] + right['operations'])
            if overlap and (left['node_id'] not in ancestors(right['node_id'])
                            and right['node_id'] not in ancestors(left['node_id'])):
                raise InvalidContract('workspace mutation requires an explicit dependency')
    return deepcopy(policy)
