"""Exclusive file locks for jobs and stage dirs (DESIGN, "Lock"), on both ends."""

from __future__ import annotations

import errno
import os
import re

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# where write_pid puts the holder's PID: after byte 0, the byte msvcrt locks, which no other
# process can read on Windows while it is held
PID_AT = 1
# a decimal PID and its newline, as write_pid writes it; anything else in the file is no PID
PID_RE = re.compile(rb"([1-9][0-9]{0,9})\n")


class Lock:
    """An exclusive lock on one file, taken without waiting; the OS drops it when the process
    dies (DESIGN, "Lock")."""

    def __init__(self, fd):
        self.fd = fd
        self.held = False
        self.pid_written = False

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

    def write_pid(self):
        """Writes this process's PID into the held lock file, from PID_AT on, for a taker it
        refuses to name (holder_pid); release clears it. Best effort: the lock holds either
        way."""
        if not self.held:
            return
        self.pid_written = True
        data = b"%d\n" % os.getpid()
        try:
            os.lseek(self.fd, PID_AT, 0)
            os.write(self.fd, data)
            os.ftruncate(self.fd, PID_AT + len(data))
        except OSError:
            pass

    @staticmethod
    def holder_pid(path):
        """The PID a holder's write_pid left in the lock file path, or None: none written (an
        older vcharon holds it, or the holder hasn't written it yet), the file gone or
        unreadable, or anything but a decimal PID there. Reads from PID_AT on, never byte 0.
        It can be stale: a holder killed before its release leaves its PID, which an older
        vcharon's later hold doesn't replace and a new holder replaces only a moment after it
        takes the lock; and that process may be another one by then."""
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(path, flags)
        except OSError:
            return None
        try:
            os.lseek(fd, PID_AT, 0)
            data = os.read(fd, 12)
        except OSError:
            return None
        finally:
            os.close(fd)
        m = PID_RE.fullmatch(data)
        return int(m.group(1)) if m else None

    def release(self):
        """Unlocks if held (clearing write_pid's PID first), then closes; safe to call twice."""
        if self.fd is None:
            return
        try:
            if self.held:
                self.held = False
                try:
                    if self.pid_written:
                        self.pid_written = False
                        # before the unlock, so the file names no one once the lock is free
                        os.ftruncate(self.fd, PID_AT)
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
