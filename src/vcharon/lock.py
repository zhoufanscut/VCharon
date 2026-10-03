"""Exclusive file locks for jobs and stage dirs (DESIGN, "Lock"), on both ends."""

from __future__ import annotations

import errno
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class Lock:
    """An exclusive lock on one file, taken without waiting; the OS drops it when the process
    dies (DESIGN, "Lock")."""

    def __init__(self, fd):
        self.fd = fd
        self.held = False

    @classmethod
    def open(cls, path, dir_fd=None, create=True, exclusive=False):
        """Opens the lock file. FileNotFoundError when create is False and it's missing,
        FileExistsError when exclusive and it exists."""
        flags = os.O_RDWR
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        if os.name == "nt":
            if dir_fd is not None:
                raise ValueError("Windows has no dir_fd")
            flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
            return cls(os.open(path, flags, 0o600))
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        return cls(os.open(path, flags, 0o600, dir_fd=dir_fd))

    def try_acquire(self):
        """True if this object holds the lock now; False if someone else does."""
        if self.held:
            return True
        if os.name == "nt":
            # msvcrt locks bytes from the current position: always byte 0.
            os.lseek(self.fd, 0, 0)
            try:
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EDEADLK):
                    return False
                raise
        else:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        self.held = True
        return True

    def release(self):
        """Unlocks if held, then closes; safe to call twice."""
        if self.fd is None:
            return
        try:
            if self.held:
                self.held = False
                try:
                    if os.name == "nt":
                        os.lseek(self.fd, 0, 0)
                        msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(self.fd, fcntl.LOCK_UN)
                except OSError:
                    # closing drops the lock anyway
                    pass
        finally:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
