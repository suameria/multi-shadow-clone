"""Read explicit local host configuration; never evaluate model-authored code."""
import json
from pathlib import Path
from ..domain.admission import Rejected, relative_path
from ..domain.checks import NodeCheckTemplate


def load_workspace_config(path):
    path=Path(path)
    if path.stat().st_size>65536:raise Rejected('workspace configuration too large')
    def unique(items):
        value={}
        for key,item in items:
            if key in value:raise Rejected('duplicate configuration field')
            value[key]=item
        return value
    value=json.loads(path.read_text(),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(Rejected('nonfinite configuration')))
    required={'workspace_id','workspace_root','paths'}
    optional={'check_templates','container_profile'}
    if type(value) is not dict or not required<=set(value) or set(value)-required-optional:
        raise Rejected('unsupported workspace configuration fields')
    import re
    if type(value['workspace_id']) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',value['workspace_id']):
        raise Rejected('invalid workspace ID')
    if type(value['workspace_root']) is not str or not Path(value['workspace_root']).is_absolute():raise Rejected('workspace root must be absolute')
    paths=value['paths']
    if type(paths) is not list or not 1<=len(paths)<=256 or any(type(p) is not str for p in paths) or len(set(paths))!=len(paths):
        raise Rejected('invalid workspace path list')
    for item in paths:relative_path(item)
    if 'check_templates' in value:
        raw=value['check_templates']
        if type(raw) is not list or not 1<=len(raw)<=100 or 'container_profile' not in value:raise Rejected('templates require explicit runtime')
        templates=[]
        for item in raw:
            if type(item) is not dict or set(item)!=set(NodeCheckTemplate.__dataclass_fields__):raise Rejected('invalid template fields')
            template=NodeCheckTemplate(**{**item,'fixed_inputs':tuple(tuple(v) for v in item['fixed_inputs']),'mutable_paths':tuple(item['mutable_paths'])})
            template.validate()
            if not (set(dict(template.fixed_inputs))|set(template.mutable_paths))<=set(paths):raise Rejected('template exceeds workspace paths')
            templates.append(template)
        value['check_templates']=templates
    elif 'container_profile' in value:raise Rejected('runtime requires registered templates')
    return value
