"""Validate an entire proposal before any reservation or effect is possible."""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re


class Rejected(ValueError):
    pass


@dataclass(frozen=True)
class Binding:
    grant_id: str
    run_id: str
    node_id: str
    attempt_id: str
    thread_id: str
    turn_id: str
    stop_epoch: int
    workspace_id: str
    contract_hash: str


@dataclass(frozen=True)
class Grant:
    binding: Binding
    operations: frozenset[str]
    paths: frozenset[str]
    checks: frozenset[str]
    remaining: int
    deadline: float
    max_bytes: int


@dataclass(frozen=True)
class Admitted:
    call_id: str
    operation: str
    payload_json: str
    payload_hash: str
    replay: bool


def relative_path(value):
    if (type(value) is not str or not value or value.startswith('/') or '\\' in value
        or any(ord(c) < 32 for c in value) or any(p in {'', '.', '..'} for p in value.split('/'))):
        raise Rejected('non-canonical relative path')
    return value


def content_hash(value, *, missing=False):
    if missing and value is None:
        return
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise Rejected('expected SHA-256')


def validate_grant(grant, now):
    if not isinstance(grant, Grant) or not isinstance(grant.binding, Binding):
        raise Rejected('invalid grant type')
    b = grant.binding
    for value in (b.grant_id, b.run_id, b.node_id, b.attempt_id, b.thread_id, b.turn_id, b.workspace_id):
        if type(value) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value):
            raise Rejected('invalid binding identity')
    content_hash(b.contract_hash)
    if type(b.stop_epoch) is not int or b.stop_epoch < 0:
        raise Rejected('invalid stop epoch')
    if (type(now) not in (int, float) or not math.isfinite(now)
        or type(grant.deadline) not in (int, float) or not math.isfinite(grant.deadline)
        or now >= grant.deadline):
        raise Rejected('deadline unavailable or expired')
    if type(grant.remaining) is not int or grant.remaining < 0 or type(grant.max_bytes) is not int or grant.max_bytes < 1:
        raise Rejected('invalid grant budget')
    if (type(grant.operations) is not frozenset or not grant.operations
        or not grant.operations <= {'read_files', 'apply_changes', 'run_check'}
        or type(grant.paths) is not frozenset or type(grant.checks) is not frozenset):
        raise Rejected('invalid capability sets')
    for path in grant.paths:
        relative_path(path)
    for check in grant.checks:
        if type(check) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', check):
            raise Rejected('invalid check ID')


def admit_batch(grant, binding, proposals, existing, *, now, stopped=False):
    """existing maps call IDs to immutable payload hashes within this grant.

    The caller must reserve returned non-replays in one transaction against the
    same grant version. Path syntax checks here do not establish OS confinement.
    """
    validate_grant(grant, now)
    if stopped or binding != grant.binding:
        raise Rejected('stopped or mismatched execution binding')
    if (type(now) not in (int, float) or not math.isfinite(now)
        or type(grant.deadline) not in (int, float) or not math.isfinite(grant.deadline)
        or now >= grant.deadline):
        raise Rejected('deadline unavailable or expired')
    if type(grant.remaining) is not int or grant.remaining < 0 or type(grant.max_bytes) is not int or grant.max_bytes < 1:
        raise Rejected('invalid grant budget')
    if type(proposals) is not list or not proposals or len(proposals) > 1000:
        raise Rejected('invalid batch size')
    admitted = []
    seen = set()
    total_bytes = 0
    for proposal in proposals:
        if type(proposal) is not dict or set(proposal) != {'call_id', 'operation', 'arguments'}:
            raise Rejected('unexpected proposal fields')
        call_id, operation, args = (proposal[k] for k in ('call_id', 'operation', 'arguments'))
        if type(call_id) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', call_id) or call_id in seen:
            raise Rejected('invalid or duplicated call ID')
        seen.add(call_id)
        if type(operation) is not str or operation not in grant.operations or type(args) is not dict:
            raise Rejected('operation not granted')
        if operation == 'read_files':
            if set(args) != {'files', 'max_bytes'} or type(args['files']) is not list or not args['files']:
                raise Rejected('invalid read request')
            if type(args['max_bytes']) is not int or not 0 < args['max_bytes'] <= grant.max_bytes:
                raise Rejected('read budget exceeded')
            paths = set()
            for item in args['files']:
                if type(item) is not dict or set(item) != {'path', 'expected_hash'}:
                    raise Rejected('invalid file expectation')
                path = relative_path(item['path'])
                if path not in grant.paths or path in paths:
                    raise Rejected('path not granted or duplicated')
                paths.add(path)
                content_hash(item['expected_hash'])
            total_bytes += args['max_bytes']
        elif operation == 'apply_changes':
            if set(args) != {'path', 'before_hash', 'content'}:
                raise Rejected('invalid change request')
            if relative_path(args['path']) not in grant.paths:
                raise Rejected('path not granted')
            content_hash(args['before_hash'], missing=True)
            if args['content'] is None:
                if args['before_hash'] is None:
                    raise Rejected('deletion requires existing content hash')
            elif type(args['content']) is str:
                total_bytes += len(args['content'].encode('utf-8'))
            else:
                raise Rejected('content must be text or explicit deletion')
        elif operation == 'run_check':
            if set(args) != {'check_id'} or type(args['check_id']) is not str or args['check_id'] not in grant.checks:
                raise Rejected('check is not registered')
        else:
            raise Rejected('unsupported operation')
        payload = json.dumps({'operation': operation, 'arguments': args}, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        fingerprint = sha256(payload.encode('utf-8')).hexdigest()
        if call_id in existing and existing[call_id] != fingerprint:
            raise Rejected('call ID rebound to different payload')
        admitted.append(Admitted(call_id, operation, payload, fingerprint, call_id in existing))
    if total_bytes > grant.max_bytes or sum(not a.replay for a in admitted) > grant.remaining:
        raise Rejected('whole batch exceeds remaining budget')
    return tuple(admitted)
