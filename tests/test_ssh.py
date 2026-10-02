"""The ssh command line, destination checks, and failures before the helper runs."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from vcharon import config, fsops, platform, ssh
from vcharon.proto import VCharonError

REMOTE = "python3 -I -c 'import sys,base64;exec(base64.b64decode(sys.stdin.buffer.readline()))'"


def settings(**overrides):
    base = dict(ssh_path="/usr/bin/ssh", connect_timeout=10, handshake_timeout=30,
                idle_timeout=300, run_timeout=0, compress=False)
    base.update(overrides)
    return config.Settings(**base)


class CommandTest(unittest.TestCase):
    def argv(self, osn, probe=False, **overrides):
        with mock.patch.object(platform, "os_name", return_value=osn):
            return ssh.ssh_command(settings(**overrides), "devbox", probe=probe)

    def test_darwin(self):
        self.assertEqual(self.argv("darwin"), [
            "/usr/bin/ssh", "-T", "-e", "none", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "devbox", REMOTE])

    def test_control_master_off_on_windows_and_in_probes(self):
        off = ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
        win = self.argv("windows", ssh_path="C:\\Windows\\System32\\OpenSSH\\ssh.exe")
        self.assertEqual(win[0], "C:\\Windows\\System32\\OpenSSH\\ssh.exe")
        self.assertEqual(win[12:16], off)
        self.assertEqual(win[-2:], ["devbox", REMOTE])
        probe = self.argv("linux", probe=True)
        self.assertEqual(probe[12:16], off)
        self.assertNotIn("ControlMaster=no", self.argv("linux"))

    def test_compress_and_timeouts(self):
        argv = self.argv("darwin", compress=True, connect_timeout=3, remote_python="python3.13")
        self.assertIn("ConnectTimeout=3", argv)
        self.assertEqual(argv[-3:], ["-C", "devbox", REMOTE.replace("python3", "python3.13", 1)])

    def test_check_dest(self):
        for dest in ("devbox", "me@host", "ssh://me@host:2222", "[fe80::1]", "a.b-c_d"):
            ssh.check_dest(dest)
        for dest in ("", "-oProxyCommand=x", "-", "a b", "a\tb", "a\nb", "a\x00b", "a\x7fb",
                     "a\u00a0b", "a\x85b"):
            with self.assertRaises(VCharonError) as cm:
                ssh.check_dest(dest)
            self.assertEqual(cm.exception.code, "config")
            self.assertEqual(cm.exception.exit_code, 3)


class ClassifyTest(unittest.TestCase):
    def classify(self, rc, tail=(), killed=None, **overrides):
        err = ssh.classify_exit(rc, list(tail), "devbox", settings(**overrides), killed=killed)
        self.assertEqual(err.code, "connect")
        self.assertEqual(err.exit_code, 4)
        self.assertEqual(err.tail, list(tail))
        return err

    def test_every_row(self):
        rows = [
            (255, ["Host key verification failed."], "host key", "run ssh devbox once"),
            (255, ["me@192.0.2.4: Permission denied (publickey)."], "Permission denied",
             "vcharon key devbox"),
            (255, ["ssh: connect to host 1.2.3.4 port 22: Connection refused"],
             "Connection refused", "check the host, port, VPN"),
            (255, ["ssh: connect to host x port 22: Operation timed out"], "timed out",
             "check the host, port, VPN"),
            (255, ["ssh: Could not resolve hostname nope: nodename nor servname provided"],
             "Could not resolve", "check the host, port, VPN"),
            (255, ["ssh: connect to host x port 22: No route to host"], "No route to host",
             "check the host"),
            (255, ["ssh: connect to host x port 22: Network is unreachable"],
             "Network is unreachable", "check the host"),
            (127, ["bash: python3: command not found"], "python3 wasn't found",
             "install python3 on devbox, or set remote_python"),
            (126, ["bash: /usr/bin/python3: Permission denied"], "isn't runnable",
             "check remote_python"),
            (90, ["vcharon: the server's Python is 3.7; vcharon needs 3.9 or newer"], "too old",
             "install Python 3.9 or newer"),
            (91, ["vcharon: bad bundle: short body"], "couldn't load vcharon's code",
             "bug in vcharon"),
            (1, ["boom"], "ssh exited with code 1 before vcharon started",
             "a shell startup file"),
            (255, ["kex_exchange_identification: read: Connection reset by peer"],
             "ssh exited with code 255 before vcharon started", "see ssh's messages above"),
        ]
        for rc, tail, message, hint in rows:
            err = self.classify(rc, tail)
            self.assertIn(message, err.message, (rc, tail))
            self.assertIn(hint, err.hint, (rc, tail))

    def test_order(self):
        # a host key failure wins over Permission denied, and 126 over its stderr text
        err = self.classify(255, ["Host key verification failed.", "Permission denied"])
        self.assertIn("host key", err.message)
        self.assertIn("isn't runnable", self.classify(126, ["Permission denied"]).message)

    def test_remote_python_in_hints(self):
        err = self.classify(127, remote_python="/opt/py3/bin/python3")
        self.assertIn("/opt/py3/bin/python3", err.message)
        self.assertIn("/opt/py3/bin/python3", err.hint)

    def test_watchdog_kill(self):
        for killed, seconds in (("handshake", 30), ("run", 45)):
            err = self.classify(-9, ["Permission denied"], killed=killed, run_timeout=45)
            self.assertEqual(err.message, "no answer from vcharon on devbox within %d s" % seconds)
            self.assertIn("authentication or a jump host may be stuck", err.hint)
            self.assertIn("run ssh devbox in a terminal", err.hint)

    def test_killed_by_a_signal(self):
        self.assertIn("killed by signal 9", self.classify(-9).message)

    def test_startup_file_hint_only_off_255(self):
        # A startup file that eats stdin makes the bootstrap exit 1; 255 is ssh's own failure.
        self.assertIn("shell startup file", self.classify(1, ["SyntaxError"]).hint)
        self.assertNotIn("shell startup file", self.classify(255, ["mux_client: x"]).hint)


class KillTest(unittest.TestCase):
    def session(self, marker_seen):
        s = ssh.Session(settings(), "devbox", mock.Mock())
        s._proc = mock.Mock()
        s._proc.poll.return_value = None
        s._marker_seen = marker_seen
        return s

    def test_no_handshake_kill_once_the_marker_arrived(self):
        # the watchdog saw no marker, then the marker arrived before it could kill
        s = self.session(marker_seen=True)
        self.assertFalse(s.kill("handshake"))
        s._proc.kill.assert_not_called()
        self.assertIsNone(s._killed)
        self.assertTrue(s.kill("idle"))
        s._proc.kill.assert_called_once_with()
        self.assertEqual(s._killed, "idle")

    def test_handshake_kill(self):
        s = self.session(marker_seen=False)
        self.assertTrue(s.kill("handshake"))
        s._proc.kill.assert_called_once_with()
        self.assertEqual(s._killed, "handshake")


class QueueTest(unittest.TestCase):
    def test_bounded_by_bytes(self):
        q = ssh._FrameQueue(100)
        # one item bigger than the limit still passes into an empty queue
        q.put("big", 500)
        done = threading.Event()

        def put_more():
            q.put("next", 10)
            done.set()

        threading.Thread(target=put_more, daemon=True).start()
        time.sleep(0.2)
        self.assertFalse(done.is_set())
        self.assertEqual(q.get(1), "big")
        self.assertTrue(done.wait(2))
        self.assertEqual(q.get(1), "next")
        self.assertIs(q.get(0.05), ssh._EMPTY)

    def test_close_wakes_a_blocked_put(self):
        q = ssh._FrameQueue(10)
        q.put("a", 10)
        done = threading.Event()
        threading.Thread(target=lambda: (q.put("b", 10), done.set()), daemon=True).start()
        time.sleep(0.2)
        self.assertFalse(done.is_set())
        q.close()
        self.assertTrue(done.wait(2))
        self.assertIs(q.get(0.05), ssh._EMPTY)


# the line measured against the test server (M5 plan §3)
AGENT_LINE = ('debug1: Server accepts key: "me@laptop" RSA '
              'SHA256:abcDEF0123456789abcDEF0123456789abcDEF01234 agent')


class SshAddPrefixTest(unittest.TestCase):
    def prefix(self, osn, path):
        with mock.patch.object(platform, "os_name", return_value=osn):
            return ssh.ssh_add_prefix(settings(ssh_path=path))

    def test_next_to_ssh(self):
        self.assertEqual(self.prefix("linux", "/usr/bin/ssh"), ["/usr/bin/ssh-add"])
        self.assertEqual(self.prefix("darwin", "/opt/homebrew/bin/ssh"),
                         ["/opt/homebrew/bin/ssh-add"])
        self.assertEqual(self.prefix("windows", "C:\\Windows\\System32\\OpenSSH\\ssh.exe"),
                         ["C:\\Windows\\System32\\OpenSSH\\ssh-add.exe"])
        self.assertEqual(self.prefix("windows", "C:\\Tools\\SSH.EXE"),
                         ["C:\\Tools\\ssh-add.exe"])


class ProbeCommandTest(unittest.TestCase):
    def test_v_after_the_prefix(self):
        with mock.patch.object(platform, "os_name", return_value="linux"):
            argv = ssh.probe_command(settings(), "devbox")
            self.assertEqual(argv[:2], ["/usr/bin/ssh", "-v"])
            self.assertEqual(argv[1:], ["-v"] + ssh.ssh_command(settings(), "devbox",
                                                                 probe=True)[1:])
            self.assertIn("ControlMaster=no", argv)
            self.assertIn("ControlPath=none", argv)
            self.assertIn("BatchMode=yes", argv)
            self.assertEqual(argv[-2:], ["devbox", REMOTE])
            with mock.patch.object(ssh, "ssh_prefix", lambda s: ["python3", "fake_ssh.py"]):
                argv = ssh.probe_command(settings(), "devbox")
            self.assertEqual(argv[:3], ["python3", "fake_ssh.py", "-v"])
            self.assertEqual(argv[-1], REMOTE)


class ParseAcceptedTest(unittest.TestCase):
    def test_lines(self):
        fp = "SHA256:abcDEF0123456789"
        cases = [
            ("debug1: Server accepts key: /home/me/.ssh/id_ed25519 ED25519 %s" % fp,
             ("/home/me/.ssh/id_ed25519", "ED25519", False)),
            ("debug1: Server accepts key: /home/me/.ssh/id_rsa RSA %s explicit agent" % fp,
             ("/home/me/.ssh/id_rsa", "RSA", True)),
            ("debug1: Server accepts key: /k ECDSA-SK %s token" % fp, ("/k", "ECDSA-SK", False)),
            ("debug1: Server accepts key: /k ED25519-SK %s explicit authenticator agent" % fp,
             ("/k", "ED25519-SK", True)),
            ("debug1: Server accepts key: /k RSA MD5:aa:bb", ("/k", "RSA", False)),
            (AGENT_LINE, ('"me@laptop"', "RSA", True)),
            # ssh's log doubles a backslash (review B2)
            ("debug1: Server accepts key: C:\\\\Users\\\\A B/.ssh/id_ed25519 ED25519 %s" % fp,
             ("C:\\Users\\A B/.ssh/id_ed25519", "ED25519", False)),
            ("debug1: Server accepts key: just a comment", ("just a comment", "", False)),
            ("debug1: Server accepts key: my key agent", ("my key", "", True)),
        ]
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(tuple(ssh.parse_accepted(line)), want)

    def test_other_lines(self):
        for line in ("debug1: Offering public key: /k RSA SHA256:x", "", "Server accepts key",
                     # the phrase in the middle of a line, as remote stderr could print it
                     # (review W2)
                     "x debug1: Server accepts key: /k RSA SHA256:x",
                     "Server accepts key: /k RSA SHA256:x"):
            self.assertIsNone(ssh.parse_accepted(line), line)

    def test_escapes_are_decoded(self):
        """Review B2: ssh's log escapes the ident as strnvis(VIS_SAFE | VIS_OCTAL) does."""
        cases = [("k\\\\ey\\303\\251", "k\\eyé"),
                 ("C:\\\\Users\\\\A B/.ssh/id_ed25519", "C:\\Users\\A B/.ssh/id_ed25519"),
                 ("\\344\\270\\255/id", "中/id"),
                 # a lone backslash, or fewer than 3 octal digits, stays as it is
                 ("a\\b", "a\\b"), ("a\\", "a\\"), ("a\\12", "a\\12"), ("a\\12x", "a\\12x"),
                 ("a\\8123", "a\\8123"),
                 # bytes that aren't UTF-8 survive as surrogates
                 ("a\\377", "a\udcff")]
        for printed, ident in cases:
            with self.subTest(printed=printed):
                line = "debug1: Server accepts key: %s RSA SHA256:x" % printed
                self.assertEqual(ssh.parse_accepted(line).ident, ident)


class ProbeKeyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.a = os.path.join(tmp, "id_a")
        self.b = os.path.join(tmp, "id b")
        for path in (self.a, self.b):
            with open(path, "w") as f:
                f.write("key")

    def probe(self, rc, *keys):
        return ssh.Probe(rc, [], [ssh.AcceptedKey(*k) for k in keys], None)

    def test_probe_key(self):
        comment = ('"c@h"', "RSA", True)
        a, b = (self.a, "RSA", False), (self.b, "ED25519", True)
        self.assertIsNone(ssh.probe_key(self.probe(0)))
        self.assertIsNone(ssh.probe_key(self.probe(255)))
        # a login: the last accepted key is the one that logged in
        self.assertEqual(ssh.probe_key(self.probe(0, a, comment)).ident, '"c@h"')
        # a failure: the first file that isn't the agent's, else the first key
        self.assertEqual(ssh.probe_key(self.probe(255, comment, b, a)).ident, self.a)
        self.assertEqual(ssh.probe_key(self.probe(255, comment, b)).ident, '"c@h"')
        missing = (self.a + "-gone", "RSA", False)
        self.assertEqual(ssh.probe_key(self.probe(255, missing, comment)).ident, self.a + "-gone")

    def test_escaped_file_is_a_file(self):
        # review B2: a key file with a non-ASCII name, as ssh prints it
        path = os.path.join(os.path.dirname(self.a), "id_é")
        with open(path, "w") as f:
            f.write("key")
        printed = "".join(chr(b) if 32 <= b < 127 and b != 92 else "\\%03o" % b
                          for b in path.encode("utf-8")).replace("\\134", "\\\\")
        self.assertNotEqual(printed, path)
        key = ssh.parse_accepted("debug1: Server accepts key: %s ED25519 SHA256:x" % printed)
        self.assertEqual(key.ident, path)
        self.assertEqual(ssh.key_kind(key), "file")

    def test_relative_ident_isnt_a_file(self):
        # review W2: a name the server made up can't point at a file in the current directory
        old = os.getcwd()
        os.chdir(os.path.dirname(self.a))
        self.addCleanup(os.chdir, old)
        for flag, kind in ((False, "other"), (True, "agent")):
            key = ssh.AcceptedKey("id_a", "RSA", flag)
            self.assertEqual(ssh.key_kind(key), kind)
        # after a failure, the relative name isn't taken for the file key
        accepted = [ssh.AcceptedKey("id_a", "RSA", False), ssh.AcceptedKey(self.a, "RSA", False)]
        self.assertEqual(ssh.probe_key(ssh.Probe(255, [], accepted, None)).ident, self.a)

    def test_key_kind(self):
        kinds = [((self.a, "RSA", False), "file"), ((self.b, "RSA", True), "file+agent"),
                 (('"c@h"', "RSA", True), "agent"), ((self.a + "-gone", "RSA", False), "other")]
        for key, kind in kinds:
            self.assertEqual(ssh.key_kind(ssh.AcceptedKey(*key)), kind, key)


class VerboseProbeTest(unittest.TestCase):
    """verbose_probe with fsops.run replaced: what it parses and classifies."""

    def probe(self, rc, err):
        calls = []

        def run(argv, timeout, new_session=False):
            calls.append((argv, timeout, new_session))
            return fsops.Ran(rc, b"", err.encode("utf-8"))

        with mock.patch.object(fsops, "run", run), \
                mock.patch.object(platform, "os_name", return_value="linux"):
            probe = ssh.verbose_probe(settings(handshake_timeout=17), "devbox", mock.Mock())
        self.calls = calls
        return probe

    def test_runs_in_a_new_session_on_posix(self):
        self.probe(0, "")
        [(argv, timeout, new_session)] = self.calls
        self.assertEqual(argv[1], "-v")
        self.assertEqual(timeout, 17)
        self.assertEqual(new_session, os.name == "posix")

    def test_classifies_only_ssh_own_lines(self):
        probe = self.probe(255, "debug1: Host key verification failed. (a debug line)\n"
                                "ssh: connect to host x port 22: Connection refused\n")
        self.assertEqual(probe.error.message, "ssh couldn't reach devbox (Connection refused)")
        self.assertEqual(probe.error.tail, ["ssh: connect to host x port 22: Connection refused"])

    def test_denied_ignores_debug_lines(self):
        probe = self.probe(255, "debug1: Permission denied, says a debug line\nboom\n")
        self.assertFalse(ssh.denied(probe))
        self.assertTrue(ssh.denied(self.probe(255, "x@h: Permission denied (publickey).\n")))

    def test_denied_needs_ssh_own_exit(self):
        # review B1: bash's "Permission denied" for a remote_python it can't run exits 126
        probe = self.probe(126, "debug1: Server accepts key: /k RSA SHA256:x\n"
                                "bash: line 1: /usr/bin/python3: Permission denied\n")
        self.assertFalse(ssh.denied(probe))
        self.assertIn("isn't runnable", probe.error.message)

    def test_stops_at_authenticated(self):
        # review W2: after the login, stderr is the server's, which could forge a line
        probe = self.probe(0, "debug1: Server accepts key: /real RSA SHA256:x\n"
                              "Authenticated to devbox ([192.0.2.1]:22) using \"publickey\".\n"
                              "debug1: Server accepts key: /forged RSA SHA256:y agent\n")
        self.assertEqual([k.ident for k in probe.accepted], ["/real"])
        probe = self.probe(0, "Authenticated to devbox ([192.0.2.1]:22) using \"publickey\".\n"
                              "debug1: Server accepts key: /forged RSA SHA256:y\n")
        self.assertEqual(probe.accepted, [])


if __name__ == "__main__":
    unittest.main()
