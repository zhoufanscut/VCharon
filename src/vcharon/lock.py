"""Exclusive file locks for jobs and stage dirs (DESIGN, "Lock"), on both ends."""

from __future__ import annotations

import errno
import functools
import os
import re
import sys

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# where write_pid puts the holder's PID: after byte 0, the byte msvcrt locks, which no other
# process can read on Windows while it is held
PID_AT = 1
# what write_pid writes: a decimal PID and its newline, then on Linux outside the initial PID
# namespace the holder's namespace as /proc/self/ns/pid reads and a newline; anything else in
# the file is no PID
PID_RE = re.compile(rb"([1-9][0-9]{0,9})\n(?:(pid:\[[1-9][0-9]{0,19}\])\n)?")
# the longest file PID_RE matches, from PID_AT on, and one byte more, so a longer one fails it
PID_READ = 11 + 27 + 1
# /proc/self/ns/pid in Linux's initial PID namespace (PROC_PID_INIT_INO). A bare PID read on
# Linux counts as one from it: write_pid writes one only from there, and so did vcharon from
# 0.6.0rc2 on (0.6.0rc1 wrote one from any namespace)
INIT_PID_NS = "pid:[4026531836]"


@functools.cache
def pid_ns():
    """This process's PID namespace, as /proc/self/ns/pid reads, on Linux ("" when unreadable
    there); None on macOS and Windows, which have none. Read once per process. A PID names the
    same process only to a process in the same namespace (DESIGN, "Lock")."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


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
        refuses to name (holder_pid); release clears it. On Linux outside the initial PID
        namespace, followed by that namespace (pid_ns), which holder_pid compares with the
        reader's own; none when it can't be read. From the initial namespace the bare PID, as
        before, which an older vcharon reads too. Best effort: the lock holds either way."""
        if not self.held:
            return
        ns = pid_ns()
        if ns == "":
            # a PID left by a holder killed before its release would name a dead process, or
            # another one by now, for as long as this hold lasts
            try:
                os.ftruncate(self.fd, PID_AT)
            except OSError:
                pass
            return
        self.pid_written = True
        data = b"%d\n" % os.getpid()
        if ns is not None and ns != INIT_PID_NS:
            data += ns.encode("ascii", "replace") + b"\n"
        try:
            os.lseek(self.fd, PID_AT, 0)
            os.write(self.fd, data)
            os.ftruncate(self.fd, PID_AT + len(data))
        except OSError:
            pass

    @staticmethod
    def holder_pid(path):
        """The PID a holder's write_pid left in the lock file path, or None: none written (an
        older vcharon in a PID namespace of its own holds it, or the holder hasn't written it
        yet), the holder in another PID namespace than this process (there its PID can name
        another process), the file gone or unreadable, or anything but PID_RE's form there.
        A bare PID counts as one from the initial namespace on Linux (INIT_PID_NS; wrong only
        for a 0.6.0rc1 holder in another one). Reads from PID_AT on, never byte 0. It can be
        stale: a holder killed before its release leaves its PID, which an older vcharon's later
        hold doesn't replace and a new holder replaces only a moment after it takes the lock;
        and that process, or even that namespace, may be another one by then."""
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(path, flags)
        except OSError:
            return None
        try:
            os.lseek(fd, PID_AT, 0)
            data = os.read(fd, PID_READ)
        except OSError:
            return None
        finally:
            os.close(fd)
        m = PID_RE.fullmatch(data)
        if not m:
            return None
        ns = pid_ns()
        if ns is None:
            # no namespaces here: a namespace in the file came from elsewhere
            return int(m.group(1)) if m.group(2) is None else None
        held_in = m.group(2).decode("ascii") if m.group(2) else INIT_PID_NS
        return int(m.group(1)) if ns and ns == held_in else None

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
