"""Bind host-selected test templates to observed source inputs before dispatch."""
from ..domain.admission import Rejected
from ..domain.checks import NodeCheckTemplate


def persisted_node_checks(store, attempt_id, contract_hash, templates, files):
    """Recovery reuses saved hashes; it never re-observes mutable inputs."""
    saved = store.read(attempt_id, contract_hash)
    if saved is not None:
        return saved
    definitions = bind_node_checks(templates, files)
    return store.save(attempt_id, contract_hash, definitions)


def bind_node_checks(templates, files):
    templates = tuple(templates)
    if not templates or len(templates) > 100 or any(type(t) is not NodeCheckTemplate for t in templates):
        raise Rejected('invalid host check templates')
    if len({t.check_id for t in templates}) != len(templates):
        raise Rejected('duplicate template check identity')
    bound = []
    for template in templates:
        template.validate()
        paths = sorted(set(dict(template.fixed_inputs)) | set(template.mutable_paths))
        remaining = template.max_input_bytes
        observations = {}
        for path in paths:
            # inspect_file requires a positive bound, including for an empty file.
            value = files.inspect_file(path, max(1, remaining))
            observations[path] = value
            remaining -= value['bytes']
            if remaining < 0:
                raise Rejected('check input observations exceed bound')
        definition = template.bind(observations)
        # Detect changes between individual observations; execution rechecks again.
        files.read_files([{'path': p, 'expected_hash': h} for p, h in definition.inputs],
                         template.max_input_bytes)
        bound.append(definition)
    return tuple(bound)
