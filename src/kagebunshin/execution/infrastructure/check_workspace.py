"""Build a disposable check snapshot from already bounded file observations."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import fcntl
import os
from pathlib import Path
import shutil
import secrets
import tempfile

from ..domain.admission import Rejected, relative_path
from ..domain.checks import CheckOutcomeUnknown


@dataclass(frozen=True)
class CheckWorkspace:
    root: Path
    scratch: Path
    inputs: tuple[tuple[str, str, int], ...]
    input_hash: str
    ownership: dict


@contextmanager
def check_workspace(observations, *, max_bytes, parent=None):
    """Materialize text bytes, never copy links or walk the source directory.

    The caller owns process lifetime and must finish/reap checks before leaving
    this context. The resulting directory contains no host execution database.
    """
    if (type(observations) is not list or not 0 < len(observations) <= 256
        or type(max_bytes) is not int or not 0 < max_bytes <= 16_777_216):
        raise Rejected('invalid check snapshot bounds')
    prepared = []
    paths = set()
    total = 0
    for item in observations:
        if type(item) is not dict or set(item) != {'path','sha256','content','bytes'}:
            raise Rejected('invalid check input observation')
        path = relative_path(item['path'])
        if (len(path) > 1024 or len(path.split('/')) > 32
            or path.split('/')[0].casefold() in {'.kagebunshin-scratch','.kagebunshin-owner'}
            or path in paths or type(item['content']) is not str):
            raise Rejected('invalid or conflicting check input path')
        data = item['content'].encode('utf-8')
        digest = sha256(data).hexdigest()
        total += len(data)
        if (type(item['bytes']) is not int or item['bytes'] != len(data)
            or item['sha256'] != digest or total > max_bytes):
            raise Rejected('check input hash or byte bound differs')
        paths.add(path)
        prepared.append((path, data, digest))
    for path in paths:
        parts = path.split('/')
        if any('/'.join(parts[:i]) in paths for i in range(1, len(parts))):
            raise Rejected('check input file conflicts with a directory')
    prepared.sort(key=lambda item: item[0])
    inputs = tuple((path, digest, len(data)) for path, data, digest in prepared)
    input_hash = sha256(json.dumps(inputs, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    root = Path(tempfile.mkdtemp(prefix='kagebunshin-check-', dir=parent)).resolve()
    retained = False
    root_fd = None
    try:
        root_fd = os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        fcntl.flock(root_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        info = os.fstat(root_fd)
        marker = json.dumps({'nonce':secrets.token_hex(32),'input_hash':input_hash},sort_keys=True).encode()
        marker_fd = os.open('.kagebunshin-owner',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=root_fd)
        with os.fdopen(marker_fd,'wb') as output:
            output.write(marker)
            output.flush()
            os.fsync(output.fileno())
        ownership={'path':str(root),'device':info.st_dev,'inode':info.st_ino,'marker_hash':sha256(marker).hexdigest()}
        scratch = root/'.kagebunshin-scratch'
        scratch.mkdir(mode=0o700)
        for path, data, _ in prepared:
            target = root/path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
        yield CheckWorkspace(root, scratch, inputs, input_hash, ownership)
    except CheckOutcomeUnknown as exc:
        retained = True
        exc.workspace_path = str(root)
        raise
    finally:
        try:
            if not retained:
                shutil.rmtree(root)
        finally:
            if root_fd is not None:
                os.close(root_fd)
