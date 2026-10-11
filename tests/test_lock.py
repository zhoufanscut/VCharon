"""Exclusive file locks (DESIGN, "Lock")."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import vcharon
from vcharon import lock
from vcharon.lock import Lock

from tests.util import readline

# Holds an exclusive lock on argv[1], says so, then waits to be killed.
if os.name == "nt":
    HOLDER = """
import msvcrt, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_BINARY)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print("held", flush=True)
time.sleep(60)
"""
else:
    HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
time.sleep(60)
"""


def hold_in_child(path):
    """A child process that holds the lock on path, once it has said so."""
    child = subprocess.Popen([sys.executable, "-c", HOLDER, path], stdout=subprocess.PIPE,
                             stdin=subprocess.DEVNULL)
    try:
        line = readline(child.stdout)
    except AssertionError:
        line = b"(nothing)"
    if line.strip() != b"held":
        child.kill()
        child.communicate(timeout=60)
        raise AssertionError("the child didn't take the lock: %r" % line)
    return child


# Takes the watcher's lock on the snapshot argv[1] as a watcher does (it writes its PID, in
# whatever PID namespace the suite runs), says that PID, then waits to be killed.
TAKE_LOCK = """
import os, sys, time
from vcharon import lock
from vcharon.mailbox import watch
lock._pid_shown = True
lk = watch.take_lock(sys.argv[1])
print(os.getpid() if lk is not None else "not held", flush=True)
time.sleep(60)
"""


def take_in_child(snapshot):
    """(a child process holding the watcher's lock on snapshot, its PID) once it has said so.
    Its own os.getpid(), not Popen.pid: on Windows a venv's python.exe runs the real one as a
    child."""
    package = os.path.dirname(os.path.abspath(vcharon.__file__))
    child = subprocess.Popen([sys.executable, "-c", TAKE_LOCK, snapshot],
                             stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             env=dict(os.environ, PYTHONPATH=os.path.dirname(package)))
    try:
        line = readline(child.stdout)
    except AssertionError:
        line = b"(nothing)"
    if not line.strip().isdigit():
        stop_child(child)
        raise AssertionError("the child didn't take the lock: %r" % line)
    return child, int(line)


def show_pid(case, shown=True):
    """Makes write_pid in this process write its PID (or not, shown False) for the rest of the
    test case, in whatever PID namespace the suite runs."""
    patcher = mock.patch.object(lock, "pid_shown", lambda: shown)
    patcher.start()
    case.addCleanup(patcher.stop)


def stop_child(child):
    child.kill()
    child.communicate(timeout=60)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "lock")

    def test_in_process(self):
        with Lock.open(self.path) as a:
            self.assertTrue(a.try_acquire())
            self.assertTrue(a.held)
            # again: already held by this object
            self.assertTrue(a.try_acquire())
            b = Lock.open(self.path)
            self.addCleanup(b.release)
            self.assertFalse(b.try_acquire())
            self.assertFalse(b.held)
            a.release()
            self.assertFalse(a.held)
            self.assertTrue(b.try_acquire())
        b.release()
        b.release()

    def test_child_process(self):
        open(self.path, "wb").close()
        child = hold_in_child(self.path)
        try:
            with Lock.open(self.path, create=False) as lk:
                self.assertFalse(lk.try_acquire())
        finally:
            stop_child(child)
        with Lock.open(self.path, create=False) as lk:
            self.assertTrue(lk.try_acquire())

    def test_pid(self):
        show_pid(self)
        # a longer PID left by a holder killed before its release: the new one is all there is
        with open(self.path, "wb") as f:
            f.write(b"\x00" + b"9" * 10 + b"\n")
        self.assertEqual(Lock.holder_pid(self.path), 9999999999)
        with Lock.open(self.path) as lk:
            # not held: nothing written
            lk.write_pid()
            self.assertEqual(os.path.getsize(self.path), 12)
            self.assertTrue(lk.try_acquire())
            lk.write_pid()
            self.assertEqual(Lock.holder_pid(self.path), os.getpid())
        # released: cut back to byte 0, so a free lock names no one
        self.assertEqual(os.path.getsize(self.path), 1)
        self.assertIsNone(Lock.holder_pid(self.path))
        self.assertIsNone(Lock.holder_pid(os.path.join(self.tmp, "missing")))

    def test_no_pid_when_hidden(self):
        # a holder whose PID would name another process writes none, and clears the PID left
        # by one killed before its release, which would name a dead process
        show_pid(self, False)
        with open(self.path, "wb") as f:
            f.write(b"\x00" + b"12345\n")
        with Lock.open(self.path) as lk:
            self.assertTrue(lk.try_acquire())
            lk.write_pid()
            self.assertIsNone(Lock.holder_pid(self.path))
            self.assertFalse(lk.pid_written)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"\x00")

    def pid_shown(self, platform, link):
        """pid_shown() worked out afresh on platform (sys.platform), /proc/self/ns/pid reading
        link (an exception: raising it); also, whether it read the link."""
        read = []

        def readlink(path):
            read.append(path)
            if isinstance(link, BaseException):
                raise link
            return link

        with mock.patch.object(lock, "_pid_shown", None), \
                mock.patch.object(lock.sys, "platform", platform), \
                mock.patch.object(lock.os, "readlink", readlink):
            shown = lock.pid_shown()
            # checked once: the second call reads nothing
            self.assertEqual(lock.pid_shown(), shown)
        self.assertIn(read, ([], ["/proc/self/ns/pid"]))
        return shown, bool(read)

    def test_pid_shown(self):
        # Linux: only the initial PID namespace, as a sandbox's or container's own names
        # another process to the rest of the machine
        self.assertEqual(self.pid_shown("linux", "pid:[4026531836]"), (True, True))
        self.assertEqual(self.pid_shown("linux", "pid:[4026532561]"), (False, True))
        self.assertEqual(self.pid_shown("linux", FileNotFoundError()), (False, True))
        self.assertEqual(self.pid_shown("linux", PermissionError()), (False, True))
        # macOS and Windows: always, without a look
        for other in ("darwin", "win32"):
            self.assertEqual(self.pid_shown(other, FileNotFoundError()), (True, False))

    def real_pid_shown(self, *prefix):
        """pid_shown() in a fresh process, run under the command prefix."""
        package = os.path.dirname(os.path.abspath(vcharon.__file__))
        out = subprocess.run([*prefix, sys.executable, "-c", "from vcharon import lock; "
                              "print(lock.pid_shown())"], capture_output=True, text=True,
                             check=False, env=dict(os.environ, PYTHONPATH=os.path.dirname(package)),
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PID namespaces")
    def test_pid_shown_here(self):
        # the real check: it agrees with the link it reads
        try:
            want = os.readlink("/proc/self/ns/pid") == "pid:[4026531836]"
        except OSError:
            want = False
        self.assertEqual(self.real_pid_shown(), "%s\n" % want)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PID namespaces")
    def test_pid_hidden_in_a_new_namespace(self):
        # as in a sandbox that runs each command in its own PID namespace
        unshare = ["unshare", "--user", "--pid", "--fork"]
        try:
            can = subprocess.run([*unshare, "true"], capture_output=True, check=False,
                                 timeout=60).returncode == 0
        except OSError:
            can = False
        if not can:
            self.skipTest("unshare can't make a PID namespace here without root")
        self.assertEqual(self.real_pid_shown(*unshare), "False\n")

    def test_open_flags(self):
        with self.assertRaises(FileNotFoundError):
            Lock.open(self.path, create=False)
        Lock.open(self.path, exclusive=True).release()
        with self.assertRaises(FileExistsError):
            Lock.open(self.path, exclusive=True)
        self.assertEqual(os.listdir(self.tmp), ["lock"])

    @unittest.skipIf(os.name == "nt", "POSIX only")
    def test_dir_fd_and_no_follow(self):
        fd = os.open(self.tmp, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        with Lock.open("lock", dir_fd=fd) as lk:
            self.assertTrue(lk.try_acquire())
        self.assertTrue(os.path.isfile(self.path))
        os.symlink(self.path, os.path.join(self.tmp, "link"))
        with self.assertRaises(OSError):
            Lock.open("link", dir_fd=fd)


if __name__ == "__main__":
    unittest.main()
