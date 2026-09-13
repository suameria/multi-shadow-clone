"""Host-built Seatbelt policy for a disposable Python check workspace.

This builds a policy only. Process lifetime, output limits and durable execution
receipts belong to the check runner; this is not a standalone execution API.
"""
import json
from pathlib import Path

from ..domain.admission import Rejected


def _canonical(path: Path) -> Path:
    path = Path(path)
    if (not path.is_absolute() or path.resolve(strict=True) != path
        or any(ord(c) < 32 for c in str(path))):
        raise Rejected('sandbox paths must be existing canonical absolute paths')
    return path


def python_check_policy(*, workspace: Path, scratch: Path, runtime: Path, executable: Path) -> str:
    """Accept host-owned paths, never model-supplied permission requests.

    The workspace must be a disposable snapshot without credentials. Checks may
    read it but write only to scratch. Runtime installation is host-trusted.
    """
    workspace, scratch, runtime, executable = map(_canonical, (workspace, scratch, runtime, executable))
    if not all(p.is_dir() for p in (workspace, scratch, runtime)) or not executable.is_file():
        raise Rejected('sandbox directories or executable are unavailable')
    if scratch == workspace or not scratch.is_relative_to(workspace):
        raise Rejected('scratch must be strictly inside the disposable workspace')
    if (not executable.is_relative_to(runtime) or workspace.is_relative_to(runtime)
        or runtime.is_relative_to(workspace)):
        raise Rejected('runtime and disposable workspace must be separate')

    def rule(kind, path):
        return '(' + kind + ' ' + json.dumps(str(path), ensure_ascii=False) + ')'

    ancestors = {str(p) for item in (runtime, workspace) for p in item.parents}
    ancestors.update(('/dev', '/usr', '/System'))
    read = [rule('literal', '/'), rule('subpath', '/System/Library'), rule('subpath', '/usr/lib'),
            rule('subpath', runtime), rule('subpath', workspace),
            rule('literal', '/dev/null'), rule('literal', '/dev/urandom')]
    return '\n'.join((
        '(version 1)', '(deny default)',
        '(allow process-exec ' + rule('literal', executable) + ')',
        '(allow sysctl-read)',
        '(allow file-read-metadata ' + ' '.join(rule('literal', p) for p in sorted(ancestors)) + ')',
        '(allow file-read* ' + ' '.join(read) + ')',
        '(allow file-write* ' + rule('subpath', scratch) + ')',
    ))
