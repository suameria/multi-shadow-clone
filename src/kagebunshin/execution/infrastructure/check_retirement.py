"""Retire a snapshot whose caller has already proved a completed check receipt.

This is a private filesystem primitive, not deletion authority from model data.
The caller must obtain ownership from the private journal, never a model reply.
"""
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat

from ..domain.admission import Rejected, content_hash


def retire_check_workspace(ownership):
    if (type(ownership) is not dict or set(ownership)!={'path','device','inode','marker_hash'}
        or type(ownership['path']) is not str
        or type(ownership['device']) is not int or type(ownership['inode']) is not int):
        raise Rejected('invalid snapshot ownership')
    content_hash(ownership['marker_hash'])
    root=Path(ownership['path'])
    if (not root.is_absolute() or root.resolve()!=root or not root.name.startswith('kagebunshin-check-')):
        raise Rejected('snapshot path is not the recorded canonical path')
    try:
        parent_fd=os.open(root.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except FileNotFoundError:
        return {'state':'path_absent'}
    root_fd=None
    try:
        try:
            root_fd=os.open(root.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent_fd)
        except FileNotFoundError:
            return {'state':'path_absent'}
        info=os.fstat(root_fd)
        if (info.st_dev,info.st_ino)!=(ownership['device'],ownership['inode']):
            raise Rejected('snapshot directory identity changed')
        try:
            fcntl.flock(root_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Rejected('snapshot still has a live owner') from exc
        marker_fd=os.open('.kagebunshin-owner',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root_fd)
        try:
            marker_info=os.fstat(marker_fd)
            if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_nlink!=1 or marker_info.st_size>4096:
                raise Rejected('snapshot marker is not an owned regular file')
            marker=os.read(marker_fd,4097)
            if sha256(marker).hexdigest()!=ownership['marker_hash']:
                raise Rejected('snapshot ownership marker changed')
        finally:
            os.close(marker_fd)
        current=os.stat(root.name,dir_fd=parent_fd,follow_symlinks=False)
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):
            raise Rejected('snapshot path changed before retirement')
        shutil.rmtree(root.name,dir_fd=parent_fd)
        return {'state':'removed'}
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)
