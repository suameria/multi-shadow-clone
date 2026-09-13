"""Small explicit value-type contract, independent of transport and scoring answers."""
import math

TYPES = {'boolean', 'number', 'string', 'string or null'}


def validate_fields(fields):
    if not isinstance(fields, dict) or len(fields) > 100:
        raise ValueError('invalid response fields')
    for name, kind in fields.items():
        if not isinstance(name, str) or not name or len(name) > 100 or not isinstance(kind, str) or kind not in TYPES:
            raise ValueError('unsupported response field')


def response_schema(fields):
    validate_fields(fields)
    if not fields:
        return None
    properties = {name: {'type': ['string', 'null'] if kind == 'string or null' else kind}
                  for name, kind in fields.items()}
    return {'type': 'object', 'properties': {
        'text': {'type': 'string'}, 'source_ids': {'type': 'array', 'items': {'type': 'string'}},
        'limits': {'type': 'array', 'items': {'type': 'string'}},
        'values': {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}},
        'required': ['text', 'source_ids', 'limits', 'values'], 'additionalProperties': False}


def values_match(fields, values):
    if set(fields) != set(values):
        return False
    def matches(kind, value):
        if kind == 'boolean': return type(value) is bool
        if kind == 'number': return type(value) in (int, float) and math.isfinite(value)
        if kind == 'string': return isinstance(value, str)
        return value is None or isinstance(value, str)
    return all(matches(kind, values[name]) for name, kind in fields.items())


def audit_schema():
    """Audit evidence is descriptive text; executable or arbitrary schemas are not accepted."""
    properties = {key: {"type": "string"} for key in
                  ("code", "target_path", "expected", "observed", "evidence")}
    properties["repairable"] = {"type": "boolean"}
    defect = {"type": "object", "properties": properties,
              "required": list(properties), "additionalProperties": False}
    return {"type": "object", "properties": {
        "approved": {"type": "boolean"}, "reason": {"type": "string"},
        "defects": {"type": "array", "items": defect}},
        "required": ["approved", "reason", "defects"], "additionalProperties": False}


def planning_schema(max_nodes=8):
    """Only proposal fields, including an empty capability-gap proposal."""
    if type(max_nodes) is not int or not 0 <= max_nodes <= 8:
        raise ValueError("invalid planning node budget")
    node_properties = {key: {"type": "string"} for key in ("id", "role_id", "instruction")}
    node_properties.update({key: {"type": "array", "items": {"type": "string"}}
                            for key in ("dependencies", "source_ids")})
    node = {"type": "object", "properties": node_properties,
            "required": list(node_properties), "additionalProperties": False}
    return {"type": "object", "properties": {
        "text": {"type": "string"}, "source_ids": {"type": "array", "items": {"type": "string"}},
        "limits": {"type": "array", "items": {"type": "string"}},
        "values": {"type": "object", "properties": {"nodes": {"type": "array", "items": node, "maxItems": max_nodes}},
                   "required": ["nodes"], "additionalProperties": False}},
        "required": ["text", "source_ids", "limits", "values"], "additionalProperties": False}
