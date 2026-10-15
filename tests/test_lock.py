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


# Takes the watcher's lock on the snapshot argv[1] as a watcher does (it writes its PID, with
# argv[2] as its PID namespace, "-" for none), says that PID, then waits to be killed.
TAKE_LOCK = """
import os, sys, time
from vcharon import lock
from vcharon.mailbox import watch
ns = None if sys.argv[2] == "-" else sys.argv[2]
lock.pid_ns = lambda: ns
lk = watch.take_lock(sys.argv[1])
print(os.getpid() if lk is not None else "not held", flush=True)
time.sleep(60)
"""


# Takes the lock argv[1] and writes its PID; leaves the file as it is when it ends (no release),
# as a holder killed before its release does; says its PID, its PID namespace and what
# holder_pid reads there.
WRITE_AND_READ = """
import os, sys
from vcharon import lock
lk = lock.Lock.open(sys.argv[1])
assert lk.try_acquire()
lk.write_pid()
print(os.getpid(), lock.pid_ns(), lock.Lock.holder_pid(sys.argv[1]), flush=True)
os._exit(0)
"""


def take_in_child(snapshot):
    """(a child process holding the watcher's lock on snapshot, its PID) once it has said so.
    Its own os.getpid(), not Popen.pid: on Windows a venv's python.exe runs the real one as a
    child. It writes the PID namespace this process's pid_ns gives (show_pid's, in a test that
    called it), so this process reads its PID back."""
    package = os.path.dirname(os.path.abspath(vcharon.__file__))
    ns = lock.pid_ns()
    child = subprocess.Popen([sys.executable, "-c", TAKE_LOCK, snapshot,
                              "-" if ns is None else ns],
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


# the longest PID and namespace PID_RE takes, from PID_AT on, and that namespace
LONGEST_NS = "pid:[" + "9" * 20 + "]"
LONGEST = b"9" * 10 + b"\n" + LONGEST_NS.encode() + b"\n"

# the PID namespace show_pid gives this process: Linux's initial one, or none off Linux
SHOWN_NS = lock.INIT_PID_NS if sys.platform.startswith("linux") else None


def show_pid(case, shown=True):
    """Makes write_pid in this process write its PID and holder_pid read it (or neither, shown
    False: as with an unreadable /proc/self/ns/pid) for the rest of the test case, in whatever
    PID namespace the suite runs."""
    ns = SHOWN_NS if shown else ""
    patcher = mock.patch.object(lock, "pid_ns", lambda: ns)
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
        # the longest PID and namespace, left by a holder killed before its release: longer
        # than any write here, so the new one is all there is only if the file is cut after it
        stale = b"\x00" + LONGEST
        with open(self.path, "wb") as f:
            f.write(stale)
        with Lock.open(self.path) as lk:
            # not held: nothing written
            lk.write_pid()
            self.assertEqual(os.path.getsize(self.path), len(stale))
            self.assertTrue(lk.try_acquire())
            lk.write_pid()
            self.assertEqual(Lock.holder_pid(self.path), os.getpid())
        # released: cut back to byte 0, so a free lock names no one
        self.assertEqual(os.path.getsize(self.path), 1)
        self.assertIsNone(Lock.holder_pid(self.path))
        self.assertIsNone(Lock.holder_pid(os.path.join(self.tmp, "missing")))

    def test_no_pid_when_hidden(self):
        # a holder that can't read its PID namespace writes none, and clears the PID left by
        # one killed before its release, which would name a dead process
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

    def write_in(self, ns):
        """The bytes write_pid leaves in a fresh lock file from PID_AT on, with pid_ns giving
        ns."""
        with mock.patch.object(lock, "pid_ns", lambda: ns), Lock.open(self.path) as lk:
            self.assertTrue(lk.try_acquire())
            lk.write_pid()
            with open(self.path, "rb") as f:
                data = f.read()
        os.remove(self.path)
        return data[lock.PID_AT:]

    def read_in(self, data, ns):
        """holder_pid on a lock file holding data from PID_AT on, with pid_ns giving ns."""
        with open(self.path, "wb") as f:
            f.write(b"\x00" + data)
        with mock.patch.object(lock, "pid_ns", lambda: ns):
            return Lock.holder_pid(self.path)

    def test_pid_and_namespace(self):
        ns, other = "pid:[4026532561]", "pid:[4026532999]"
        pid = os.getpid()
        # Linux, initial namespace: the bare PID, as before, which an older vcharon reads too
        data = self.write_in(lock.INIT_PID_NS)
        self.assertEqual(data, b"%d\n" % pid)
        self.assertEqual(self.read_in(data, lock.INIT_PID_NS), pid)
        self.assertIsNone(self.read_in(data, ns))
        # Linux, another namespace: the PID, then that namespace; read back only from the same
        # one
        data = self.write_in(ns)
        self.assertEqual(data, b"%d\n%s\n" % (pid, ns.encode()))
        self.assertEqual(self.read_in(data, ns), pid)
        self.assertIsNone(self.read_in(data, other))
        self.assertIsNone(self.read_in(data, lock.INIT_PID_NS))
        # the reader's own link unreadable: no PID
        self.assertIsNone(self.read_in(data, ""))
        # a bare PID from an older vcharon (from 0.6.0rc2 on, written only from the initial
        # namespace)
        self.assertEqual(self.read_in(b"42\n", lock.INIT_PID_NS), 42)
        self.assertIsNone(self.read_in(b"42\n", ns))
        self.assertIsNone(self.read_in(b"42\n", ""))
        # macOS and Windows: a bare PID, read as one; a namespace there came from elsewhere
        data = self.write_in(None)
        self.assertEqual(data, b"%d\n" % pid)
        self.assertEqual(self.read_in(data, None), pid)
        self.assertIsNone(self.read_in(b"42\n%s\n" % ns.encode(), None))
        # the longest form PID_RE takes is read whole; one byte more is no PID
        self.assertEqual(self.read_in(LONGEST, LONGEST_NS), 9999999999)
        self.assertIsNone(self.read_in(LONGEST + b"\n", LONGEST_NS))

    def test_malformed_namespace(self):
        ns = "pid:[4026532561]"
        for data in (b"42\npid:[4026532561]", b"42\npid:[4026532561]\n\n",
                     b"42\npid:4026532561\n", b"42\nnet:[4026532561]\n", b"42\npid:[]\n",
                     b"42\npid:[04026532561]\n", b"42\n\npid:[4026532561]\n",
                     b"42 pid:[4026532561]\n", b"pid:[4026532561]\n42\n",
                     b"42\npid:[" + b"1" * 21 + b"]\n"):
            with self.subTest(data=data):
                self.assertIsNone(self.read_in(data, ns))

    def pid_ns(self, platform, link):
        """pid_ns() worked out afresh on platform (sys.platform), /proc/self/ns/pid reading
        link (an exception: raising it); also, whether it read the link."""
        read = []

        def readlink(path):
            read.append(path)
            if isinstance(link, BaseException):
                raise link
            return link

        lock.pid_ns.cache_clear()
        self.addCleanup(lock.pid_ns.cache_clear)
        with mock.patch.object(lock.sys, "platform", platform), \
                mock.patch.object(lock.os, "readlink", readlink):
            ns = lock.pid_ns()
            # read once: the second call reads nothing
            self.assertEqual(lock.pid_ns(), ns)
        self.assertIn(read, ([], ["/proc/self/ns/pid"]))
        return ns, bool(read)

    def test_pid_ns(self):
        self.assertEqual(self.pid_ns("linux", "pid:[4026532561]"), ("pid:[4026532561]", True))
        self.assertEqual(self.pid_ns("linux", FileNotFoundError()), ("", True))
        self.assertEqual(self.pid_ns("linux", PermissionError()), ("", True))
        # macOS and Windows: none, without a look
        for other in ("darwin", "win32"):
            self.assertEqual(self.pid_ns(other, FileNotFoundError()), (None, False))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PID namespaces")
    def test_pid_ns_here(self):
        # the real link, as this process reads it
        try:
            want = os.readlink("/proc/self/ns/pid")
        except OSError:
            want = ""
        lock.pid_ns.cache_clear()
        self.addCleanup(lock.pid_ns.cache_clear)
        self.assertEqual(lock.pid_ns(), want)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PID namespaces")
    def test_holder_in_a_new_namespace(self):
        # as in a sandbox that runs each command in its own PID namespace: its PID is written
        # with that namespace (not the bare form), so a reader outside it gets none, and one
        # inside it the PID
        unshare = ["unshare", "--user", "--pid", "--fork"]
        try:
            can = subprocess.run([*unshare, "true"], capture_output=True, check=False,
                                 timeout=60).returncode == 0
        except OSError:
            can = False
        if not can:
            self.skipTest("unshare can't make a PID namespace here without root")
        package = os.path.dirname(os.path.abspath(vcharon.__file__))
        out = subprocess.run([*unshare, sys.executable, "-c", WRITE_AND_READ, self.path],
                             capture_output=True, text=True, check=False, timeout=60,
                             env=dict(os.environ, PYTHONPATH=os.path.dirname(package)))
        self.assertEqual(out.returncode, 0, out.stderr)
        pid, ns, read = out.stdout.split()
        self.assertEqual(read, pid)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"\x00%s\n%s\n" % (pid.encode(), ns.encode()))
        lock.pid_ns.cache_clear()
        self.addCleanup(lock.pid_ns.cache_clear)
        self.assertNotEqual(lock.pid_ns(), ns)
        self.assertIsNone(Lock.holder_pid(self.path))

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
