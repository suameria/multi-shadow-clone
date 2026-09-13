"""Descriptor-relative, bounded reads from a host-selected directory capability.

No pathname reopening after validation. This is not an isolation boundary for
executing generated code. Writes remain provisional; no check execution.
"""
from contextlib import contextmanager
from hashlib import sha256
import os
import fcntl
import stat
import threading
from ..domain.admission import Rejected, relative_path, content_hash


class OwnedFiles:
    def __init__(self, root, allowed_paths):
        self.lifecycle_lock = threading.Lock()
        self.allowed_paths = frozenset(relative_path(p) for p in allowed_paths)
        root = os.fspath(root)
        if not os.path.isabs(root):
            raise Rejected('host root must be absolute')
        parts = root.split('/')[1:]
        if any(p in {'', '.', '..'} for p in parts):
            raise Rejected('host root must be canonical')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = following
            self.root_fd = fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _workspace_lock(self, exclusive=False):
        if self.root_fd is None:
            raise Rejected("workspace closed")
        try:
            fcntl.flock(self.root_fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Rejected("workspace is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(self.root_fd, fcntl.LOCK_UN)

    def close(self):
        with self.lifecycle_lock:
            if self.root_fd is not None:
                os.close(self.root_fd)
                self.root_fd = None

    def contract(self):
        """Bind the opened capability, not a path that may resolve elsewhere.

        File contents remain per-operation hash expectations. Directory inode
        identity survives reopen but is not proof of exclusive host ownership.
        """
        with self.lifecycle_lock:
            if self.root_fd is None:
                raise Rejected('workspace closed')
            info = os.fstat(self.root_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_nlink == 0:
                raise Rejected('workspace no longer exists')
            return {'kind': 'owned-files-v1', 'device': info.st_dev,
                    'inode': info.st_ino, 'uid': info.st_uid, 'gid': info.st_gid,
                    'paths': sorted(self.allowed_paths)}

    @contextmanager
    def _open_file(self, path):
        path = relative_path(path)
        if path not in self.allowed_paths or self.root_fd is None:
            raise Rejected('file not granted or workspace closed')
        parent = os.dup(self.root_fd)
        fd = None
        try:
            pieces = path.split('/')
            for part in pieces[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = following
            fd = os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            yield fd
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)

    def read_files(self, files, max_bytes):
        with self.lifecycle_lock, self._workspace_lock():
            return self._read_files(files, max_bytes)

    def _read_files(self, files, max_bytes):
        if type(max_bytes) is not int or max_bytes < 1 or type(files) is not list or not files:
            raise Rejected('invalid bounded read')
        seen = set()
        for item in files:
            if type(item) is not dict or set(item) != {'path', 'expected_hash'}:
                raise Rejected('invalid read expectation')
            path = relative_path(item['path'])
            if path not in self.allowed_paths or path in seen:
                raise Rejected('path not granted or duplicated')
            seen.add(path)
            content_hash(item['expected_hash'])
        remaining = max_bytes
        results = []
        for item in files:
            with self._open_file(item['path']) as fd:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > remaining:
                    raise Rejected('non-regular, linked or oversized file')
                chunks = []
                size = 0
                while True:
                    chunk = os.read(fd, min(65536, remaining - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > remaining:
                        raise Rejected('aggregate read budget exceeded')
                    chunks.append(chunk)
                data = b''.join(chunks)
                after = os.fstat(fd)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise Rejected('file changed during read')
                fingerprint = sha256(data).hexdigest()
                if fingerprint != item['expected_hash']:
                    raise Rejected('file content differs from expectation')
                results.append({'path': item['path'], 'sha256': fingerprint, 'content': data.decode('utf-8'), 'bytes': len(data)})
                remaining -= len(data)
        return results

    def apply_change(self, path, before_hash, content, max_bytes):
        """Host-only provisional writer; not yet exposed as a Codex tool.

        Serializes this adapter's operations. External writers do not share this
        lock: a precondition check plus rename is not a filesystem compare/swap.
        """
        import secrets
        path = relative_path(path)
        content_hash(before_hash, missing=True)
        if type(max_bytes) is not int or max_bytes < 1 or (content is not None and type(content) is not str):
            raise Rejected('invalid write request')
        if content is None and before_hash is None:
            raise Rejected('deletion requires an existing hash')
        data = content.encode('utf-8') if content is not None else None
        if data is not None and len(data) > max_bytes:
            raise Rejected('write budget exceeded')
        with self.lifecycle_lock, self._workspace_lock(exclusive=True):
            if self.root_fd is None or path not in self.allowed_paths:
                raise Rejected('file not granted or workspace closed')
            parent = os.dup(self.root_fd)
            temporary = None
            try:
                parts = path.split('/')
                for part in parts[:-1]:
                    following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent)
                    parent = following
                leaf = parts[-1]
                try:
                    original = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    original = None
                if before_hash is None:
                    if original is not None:
                        raise Rejected('new file already exists')
                else:
                    if original is None:
                        raise Rejected('expected existing file is missing')
                    source = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    try:
                        checked = os.fstat(source)
                        if (not stat.S_ISREG(checked.st_mode) or checked.st_nlink != 1
                            or checked.st_size > max_bytes or (checked.st_dev,checked.st_ino) != (original.st_dev,original.st_ino)):
                            raise Rejected('unexpected source file')
                        hasher, size = sha256(), 0
                        while True:
                            block = os.read(source, min(65536,max_bytes-size+1))
                            if not block: break
                            size += len(block)
                            if size > max_bytes: raise Rejected('source exceeds budget')
                            hasher.update(block)
                        after = os.fstat(source)
                        if (checked.st_size,checked.st_mtime_ns,checked.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                            raise Rejected('source changed during verification')
                        if hasher.hexdigest() != before_hash:
                            raise Rejected('file content differs from expectation')
                    finally:
                        os.close(source)
                if data is not None:
                    temporary = '.multi-shadow-clone-' + secrets.token_hex(16)
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    try:
                        offset = 0
                        while offset < len(data):
                            count = os.write(fd, data[offset:])
                            if count <= 0: raise OSError('short write')
                            offset += count
                        if original is not None:
                            os.fchmod(fd, stat.S_IMODE(original.st_mode) & 0o777)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                if original is not None:
                    current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    def identity(st):
                        return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
                    if identity(current) != identity(original):
                        raise Rejected('target changed before replacement')
                    if data is None:
                        os.unlink(leaf, dir_fd=parent)
                    else:
                        os.replace(temporary,leaf,src_dir_fd=parent,dst_dir_fd=parent)
                        temporary = None
                else:
                    # link creation is no-clobber even if another writer arrives.
                    os.link(temporary,leaf,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
                if temporary is not None:
                    os.unlink(temporary, dir_fd=parent)
                    temporary = None
                os.fsync(parent)
                return {'path':path,'before_hash':before_hash,'after_hash':sha256(data).hexdigest() if data is not None else None}
            finally:
                try:
                    if temporary is not None:
                        os.unlink(temporary, dir_fd=parent)
                finally:
                    os.close(parent)

    def inspect_file(self, path, max_bytes):
        """Read-only current-state observation, not proof of who wrote it."""
        with self.lifecycle_lock, self._workspace_lock():
            if type(max_bytes) is not int or max_bytes < 1:
                raise Rejected('invalid inspection budget')
            try:
                with self._open_file(path) as fd:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
                        raise Rejected('unexpected inspection target')
                    hasher, size = sha256(), 0
                    while True:
                        block = os.read(fd,min(65536,max_bytes-size+1))
                        if not block: break
                        size += len(block)
                        if size > max_bytes: raise Rejected('inspection budget exceeded')
                        hasher.update(block)
                    after = os.fstat(fd)
                    if (before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                        raise Rejected('inspection target changed')
                    return {'exists':True,'sha256':hasher.hexdigest(),'bytes':size}
            except FileNotFoundError:
                return {'exists':False,'sha256':None,'bytes':0}
