"""Versioned, host-registered check contracts; no executable commands from models."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re

from .admission import Rejected, content_hash, relative_path


class CheckOutcomeUnknown(RuntimeError):
    """A check may still own resources; do not rerun or delete its workspace."""


@dataclass(frozen=True)
class PythonCheck:
    check_id: str
    entrypoint: str
    inputs: tuple[tuple[str, str], ...]
    timeout: float
    max_input_bytes: int
    max_output_bytes: int

    def validate(self):
        if type(self.check_id) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', self.check_id):
            raise Rejected('invalid registered check identity')
        relative_path(self.entrypoint)
        if type(self.inputs) is not tuple or not 0 < len(self.inputs) <= 256:
            raise Rejected('invalid check input contract')
        names = set()
        for item in self.inputs:
            if type(item) is not tuple or len(item) != 2:
                raise Rejected('invalid check input binding')
            path, digest = item
            relative_path(path)
            content_hash(digest)
            if path in names:
                raise Rejected('duplicate check input')
            names.add(path)
        if self.entrypoint not in names:
            raise Rejected('check entrypoint must be a declared input')
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
            or not 0 < self.timeout <= 300
            or type(self.max_input_bytes) is not int or not 0 < self.max_input_bytes <= 16_777_216
            or type(self.max_output_bytes) is not int or not 0 < self.max_output_bytes <= 1_048_576):
            raise Rejected('invalid registered check limits')

    def fingerprint(self):
        self.validate()
        return sha256(json.dumps(asdict(self),sort_keys=True,separators=(',', ':'),ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class NodeCheck(PythonCheck):
    """Registered CommonJS check; its identity cannot alias a Python runner."""
    def validate(self):
        super().validate()
        if not self.entrypoint.endswith(('.js','.cjs')):
            raise Rejected('registered Node entrypoint must be CommonJS')

    def fingerprint(self):
        self.validate()
        value={'runtime':'node-commonjs-v1',**asdict(self)}
        return sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class NodeCheckTemplate:
    """Fixed host test code with explicitly named, attempt-bound source inputs."""
    check_id: str
    entrypoint: str
    fixed_inputs: tuple[tuple[str, str], ...]
    mutable_paths: tuple[str, ...]
    timeout: float
    max_input_bytes: int
    max_output_bytes: int

    def validate(self):
        if type(self.fixed_inputs) is not tuple or type(self.mutable_paths) is not tuple:
            raise Rejected('template paths must be immutable tuples')
        if self.entrypoint not in dict(self.fixed_inputs):
            raise Rejected('test entrypoint must have a fixed host hash')
        NodeCheck(self.check_id, self.entrypoint,
                  self.fixed_inputs + tuple((p, '0' * 64) for p in self.mutable_paths),
                  self.timeout, self.max_input_bytes, self.max_output_bytes).validate()

    def fingerprint(self):
        self.validate()
        return sha256(json.dumps({'kind': 'node-check-template-v1', **asdict(self)},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

    def bind(self, observations):
        self.validate()
        fixed = dict(self.fixed_inputs)
        expected = set(fixed) | set(self.mutable_paths)
        if type(observations) is not dict or set(observations) != expected:
            raise Rejected('template snapshot input set differs')
        inputs, size = [], 0
        for path in sorted(expected):
            value = observations[path]
            if (type(value) is not dict or set(value) != {'exists', 'sha256', 'bytes'}
                or value['exists'] is not True or type(value['bytes']) is not int or value['bytes'] < 0):
                raise Rejected('template input unavailable')
            content_hash(value['sha256'])
            if path in fixed and value['sha256'] != fixed[path]:
                raise Rejected('host test input changed')
            size += value['bytes']
            inputs.append((path, value['sha256']))
        if size > self.max_input_bytes:
            raise Rejected('template snapshot exceeds input bound')
        return NodeCheck(self.check_id, self.entrypoint, tuple(inputs), self.timeout,
                         self.max_input_bytes, self.max_output_bytes)
