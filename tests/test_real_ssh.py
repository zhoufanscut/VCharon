"""Against a real server: set VCHARON_TEST_SSH=<dest>. Its key must work under BatchMode."""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from vcharon import cli, doctor, platform

import tests
from tests.util import read_tree, use_test_jobs, write_tree

DEST = os.environ.get("VCHARON_TEST_SSH")
# Windows has no execute bit: os.chmod(0o755) there leaves st_mode at 0o666, and the path source
# leaves exec out (DESIGN, "The path source", "What is copied"). The execute-bit assertions below
# need POSIX.
POSIX = os.name == "posix"


@unittest.skipUnless(DEST, "set VCHARON_TEST_SSH=<dest> to run against a real server")
class RealSshTest(unittest.TestCase):
    def setUp(self):
        tmp = self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # the real home's ~/.ssh holds the key and known_hosts, so HOME leaves the suite's
        # sandbox for this test; vcharon's own folders and the local channel root, which
        # doctor lists, stay in the temp folder
        patcher = mock.patch.dict(os.environ, {
            "HOME": tests.REAL_HOME, "USERPROFILE": tests.REAL_HOME,
            "VCHARON_HOME": tmp, "VCHARON_CHANNELS_ROOT": os.path.join(tmp, "channels")})
        patcher.start()
        self.addCleanup(patcher.stop)
        # the box's PATH may hold other vcharon installs: doctor's exit code here is about ssh
        patcher = mock.patch.object(doctor, "path_check", lambda *a, **kw: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ssh(self, command):
        """Runs one shell command on the server; its stdout."""
        argv = [platform.default_ssh_path(), "-o", "BatchMode=yes", "-o", "ControlMaster=no",
                "-o", "ControlPath=none", DEST, command]
        # the server's names are UTF-8 bytes, and under LC_ALL=C find prints them raw: decoding
        # with this machine's locale (GBK here) would fail
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, check=True).stdout

    def remote_tmp(self):
        """A new directory under the server's home, removed after the test; its path."""
        path = self.ssh("mktemp -d ~/vcharon-test-XXXXXX").strip()
        # never rm -rf anything but what mktemp just made
        self.assertRegex(path, r"\A/[^\s']+/vcharon-test-[A-Za-z0-9]{6}\Z")
        self.addCleanup(self.ssh, "rm -rf -- %s" % shlex.quote(path))
        return path

    def test_ping(self):
        self.assertEqual(cli.main(["ping", DEST]), 0)

    def run_job(self, name, full=False):
        """The sync's runner on the test job name (cli.run_jobs), which must succeed; its
        lines."""
        out = io.StringIO()
        run = cli._Run()
        with contextlib.redirect_stdout(out):
            code = cli._guarded(lambda: cli.run_jobs([name], full=full, run=run), run)
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue().splitlines()

    def test_job(self):
        use_test_jobs(self)
        remote = self.remote_tmp()
        src = os.path.join(self.tmp, "源 src")
        spec = {"文档/报告.txt": "报告\n".encode() * 100, "with space/a b.txt": b"sp",
                "run.sh": b"#!/bin/sh\necho hi\n", "empty dir/": None,
                "big.bin": os.urandom(1 << 20)}
        write_tree(src, spec)
        os.chmod(os.path.join(src, "run.sh"), 0o755)
        with open(os.path.join(self.tmp, "vcharon.ini"), "w", encoding="utf-8") as f:
            f.write("[push]\nssh = %s\nfrom = local:path\nfrom.path = %s\nto = remote:dir\n"
                    "to.path = %s\n" % (DEST, src, remote))
        def size():
            return cli.size_text(sum(len(data) for data in read_tree(src).values() if data))

        self.assertEqual(self.run_job("push")[1], "  put     4 files, 3 dirs (%s)" % size())
        self.assertEqual(self.run_job("push")[1], "  nothing to do")
        write_tree(src, {"with space/a b.txt": b"changed"})
        self.assertEqual(self.run_job("push")[1], "  put     1 file, 0 dirs (7 B)")
        # one byte more at the server: vcharon trusts the target until a --full run
        self.ssh("printf x >> %s" % shlex.quote(remote + "/run.sh"))
        self.assertEqual(self.run_job("push")[1], "  nothing to do")
        self.assertEqual(self.run_job("push", full=True)[1],
                         "  put     4 files, 3 dirs (%s), 3 already there" % size())
        # a pull job with prune from the same server directory
        pulled = os.path.join(self.tmp, "pulled")
        os.mkdir(pulled)
        with open(os.path.join(self.tmp, "vcharon.ini"), "a", encoding="utf-8") as f:
            f.write("[pull]\nssh = %s\nfrom = remote:path\nfrom.path = %s\nfrom.prune = yes\n"
                    "to = local:dir\nto.path = %s\n" % (DEST, remote, pulled))
        self.assertEqual(self.run_job("pull")[1], "  put     4 files, 3 dirs (%s)" % size())
        self.assertEqual(read_tree(pulled), read_tree(src))
        if POSIX:
            self.assertTrue(os.stat(os.path.join(pulled, "run.sh")).st_mode & stat.S_IXUSR)
        self.ssh("rm -- %s" % shlex.quote(remote + "/with space/a b.txt"))
        self.assertEqual(self.run_job("pull")[1:3], ["  put     0 files, 0 dirs (0 B)",
                                                     "  delete  1"])
        tree = read_tree(src)
        del tree["with space/a b.txt"]
        self.assertEqual(read_tree(pulled), tree)
        self.assertEqual(self.run_job("pull")[1], "  nothing to do")

    def doctor(self, code):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            got = cli.main(["doctor", "--server", DEST])
        self.assertEqual(got, code, out.getvalue() + err.getvalue())
        return [line for line in out.getvalue().splitlines()
                if line.startswith(("  ok    %s " % DEST, "  FAIL  %s " % DEST))]

    def test_doctor(self):
        lines = self.doctor(0)
        self.assertRegex(lines[0], r"\A  ok    %s +logs in" % re.escape(DEST))

    def test_doctor_without_the_agent(self):
        keys = [name for name in ("id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519",
                                  "id_ed25519_sk", "id_dsa", "id_xmss")
                if os.path.exists(os.path.join(os.path.expanduser("~"), ".ssh", name))]
        if keys:
            self.skipTest("a key file here could log in without the agent: %s" % keys)
        with mock.patch.dict(os.environ):
            os.environ.pop("SSH_AUTH_SOCK", None)
            lines = self.doctor(1)
        self.assertRegex(lines[0], r"\A  FAIL  %s +" % re.escape(DEST))
        self.assertIn("accepts none of your keys", lines[0])


if __name__ == "__main__":
    unittest.main()
