"""Exclusive file locks (DESIGN §11.3)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from vcharon.lock import Lock

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
    line = child.stdout.readline()
    if line.strip() != b"held":
        child.kill()
        child.communicate()
        raise AssertionError("the child didn't take the lock: %r" % line)
    return child


def stop_child(child):
    child.kill()
    child.communicate()


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
