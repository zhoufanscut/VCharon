"""The command line: ping, version, usage errors and exit codes."""

from __future__ import annotations

import errno
import io
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from vcharon import VERSION, cli, doctor, fsops, install, pathrules, platform, ssh, stage, state
from vcharon import run as engine
from vcharon.plan import Plan, delete, put_dir, put_file
from vcharon.proto import VCharonError

from tests import util
from tests.test_lock import hold_in_child, stop_child
from tests.util import (
    CAN_SYMLINK,
    FAKE_SSH,
    TEST_MACHINE_ID,
    FakeSshCase,
    helper_override,
    read_tree,
    write_tree,
)

PULL_HINT = ("the vcharon on the server may be broken, or the server compromised; nothing was "
             "changed")


def json_inner(value):
    """value as vcharon prints it inside the JSON of an identity, a target or a saved state, with
    the quotes left to the template: json.dumps escapes a Windows path's \\, so a message that
    names one reads C:\\\\Users\\\\... here."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


# a helper whose source.plan answers with ENTRIES, whatever it was asked for
LYING = """
import io
from vcharon import plan as _plan, proto as _proto

def source_plan(h, call_id, args):
    h.plan = _plan.from_json({"entries": ENTRIES})
    h.ok(call_id, _plan.to_json(h.plan))

def source_send(h, call_id, args):
    h.ok(call_id, {})
    _proto.send_stream(h.conn, [(i, lambda: io.BytesIO(b"EVIL")) for i in args["indexes"]])

_real.HANDLERS["source.plan"] = source_plan
_real.HANDLERS["source.send"] = source_send
"""

# the folder that holds the vcharon package (src/ in the repo)
VCHARON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

# a helper whose server has no machine id
NO_MACHINE = """
from vcharon import platform as _platform

_platform.machine_id = lambda *args, **kw: None
"""

PUSH = """
[push]
ssh       = fake-dest
from      = local:path
from.path = {src}
to        = remote:dir
to.path   = inbox
"""

PULL = """
[pull]
ssh        = fake-dest
from       = remote:path
from.path  = outbox
from.prune = yes
to         = local:dir
to.path    = {dst}
"""

# the hint of a test job's state_mismatch: a channel section's names its sync commands
RESET_HINT = "  fix: check the target; then reset the job's state, and sync it with --full"
OK_LINE = r"\AOK  %d written, %d deleted  \(\d+\.\d s\)\Z"


class CliTest(FakeSshCase):
    def test_version(self):
        # the bare version, nothing else (an update's smoke check compares it); the protocol
        # and Python are doctor's
        code, out, err = self.run_cli("--version")
        self.assertEqual((code, out, err), (0, VERSION + "\n", ""))

    def test_ping(self):
        code, out, err = self.run_cli("ping", "fake-dest")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "vcharon: ping fake-dest")
        self.assertRegex(lines[1], r"\A  helper   vcharon " + re.escape(VERSION)
                         + r", protocol 3, Python 3\.")
        self.assertRegex(lines[2], r"\A  server   .+, user .*, home " + re.escape(self.home)
                         + r"\Z")
        self.assertEqual(lines[3], "  machine  0123456789abcdef0123456789abcdef")
        self.assertRegex(lines[4], r"\A  connect  \d+\.\d\d s  \(ssh start to hello\)\Z")
        self.assertRegex(lines[5], r"\A  echo     1048832 bytes, byte-identical, \d+\.\d\d s "
                         r"round trip\Z")
        self.assertRegex(lines[6], r"\AOK  \(\d+\.\d s\)\Z")
        self.assertEqual(err, "")
        log = os.path.join(self.vcharon_home, "logs", "vcharon.log")
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("vcharon %s ping fake-dest" % VERSION, text)
        self.assertIn("ping OK", text)

    def test_ping_warnings(self):
        class NoMachineId(ssh.Session):
            def open(self):
                hello = super().open()
                hello["machine"] = None
                return hello

        os.environ["FAKE_SSH_JUNK_B64"] = "aGVsbG8K"
        with mock.patch.object(ssh, "Session", NoMachineId):
            code, out, err = self.run_cli("ping", "fake-dest")
        self.assertEqual(code, 0, err)
        self.assertIn("\n  warn     the server's shell printed 6 bytes before vcharon started; "
                      "see the log\n", out)
        self.assertIn("\n  machine  none (jobs that keep state won't run)\n", out)

    def test_ping_permission_denied(self):
        os.environ["FAKE_SSH_EXIT"] = "255"
        os.environ["FAKE_SSH_STDERR"] = "Permission denied (publickey)."
        code, _out, err = self.run_cli("ping", "fake-dest")
        self.assertEqual(code, 4)
        lines = err.splitlines()
        self.assertEqual(lines[0], "ERROR connect: ssh couldn't log in to fake-dest "
                         "(Permission denied)")
        self.assertIn("  | Permission denied (publickey).", lines)
        # the command as this box runs vcharon
        self.assertIn(platform.runnable("  fix: add your key to the server, or run: vcharon key "
                                        "fake-dest"), lines)
        self.assertIn("  log: %s" % os.path.join(self.vcharon_home, "logs", "vcharon.log"), lines)

    def test_a_bug_is_internal(self):
        with mock.patch.object(ssh.Session, "echo", side_effect=RuntimeError("oops")):
            code, _out, err = self.run_cli("ping", "fake-dest")
        self.assertEqual(code, 1)
        lines = err.splitlines()
        self.assertEqual(lines[0], "ERROR internal: RuntimeError: oops")
        self.assertIn("  fix: this is a bug in vcharon; see the log", lines)
        with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"), encoding="utf-8") as f:
            self.assertIn("Traceback (most recent call last):", f.read())

    def test_ping_echo_differs(self):
        # the only check that the connection is binary-safe: a flipped byte, a short echo
        real = ssh.Session.echo
        for name, alter, at, received in (
                ("flipped", lambda b: b[:300] + bytes([b[300] ^ 1]) + b[301:], 300, 1048832),
                ("short", lambda b: b[:-5], 1048827, 1048827)):
            with self.subTest(name):
                def echo(session, data, alter=alter):
                    return alter(real(session, data))

                with mock.patch.object(ssh.Session, "echo", echo):
                    code, out, err = self.run_cli("ping", "fake-dest")
                self.assertEqual(code, 1, err)
                lines = err.splitlines()
                self.assertEqual(lines[:2], [
                    "ERROR protocol: the echo came back different: 1048832 bytes sent, %d "
                    "received, first difference at byte %d" % (received, at),
                    "  fix: see the log; the connection isn't binary-safe"])
                self.assertNotIn("  echo ", out)
                self.assertNotIn("OK", out)

    def test_usage_errors_exit_3(self):
        for argv in ([], ["nope"], ["ping"], ["ping", "a", "b"], ["ping", "-x", "host"],
                     ["ping", "--", "-oProxyCommand=x"], ["ping", "bad host"]):
            with self.subTest(argv=argv):
                code, _out, err = self.run_cli(*argv)
                self.assertEqual(code, 3)
                self.assertTrue(err.startswith("ERROR config: "), err)
                self.assertIn("  fix: ", err)

    def test_bad_config_exits_3(self):
        with open(os.path.join(self.vcharon_home, "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nidle_timeout = 0\n")
        code, _out, err = self.run_cli("ping", "fake-dest")
        self.assertEqual(code, 3)
        self.assertIn("ERROR config: vcharon.ini [vcharon] idle_timeout: must be a whole number",
                      err)

    def test_removed_commands_and_options(self):
        # the tool works out who you are: no --config, --me, --job or --dir; no channel noun,
        # run, state or version verb
        other = os.path.join(self.tmp, "other.ini")
        for argv in (["run", "a"], ["state", "reset", "a"], ["version"], ["channel", "list"],
                     ["--config", other, "ping", "fake-dest"],
                     ["ping", "fake-dest", "--config", other], ["-v", "ping", "fake-dest"],
                     ["post", "c", "--to", "@a", "--title", "t", "--me", "x"],
                     ["watch", "c", "--job", "c.x"], ["watch", "c", "--dir", "/x"],
                     ["read", "c", "--config", other], ["list", "--ssh", "fake-dest"],
                     ["doctor", "fake-dest"], ["sync", "c.x.up"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (3, ""))
                self.assertTrue(err.startswith("ERROR config: "), err)

    def test_an_unknown_flag_points_to_its_verbs_help(self):
        for argv, word, fix in (
                (["read", "c", "--bogus"], "--bogus", "vcharon read --help"),
                (["post", "c", "--to", "@a", "--title", "t", "--x=1"], "--x=1",
                 "vcharon post --help"),
                (["skill", "install", "--nope"], "--nope", "vcharon skill install --help"),
                (["--bogus"], "--bogus", "vcharon --help"),
                (["--bogus", "read", "c"], "--bogus", "vcharon --help")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(err.splitlines()[:2], [
                    "ERROR config: unrecognized arguments: %s" % word,
                    "  fix: %s" % platform.runnable(fix)])

    def test_no_abbreviations(self):
        # a prefix of a flag is no flag: it would become part of the contract
        for argv in (["sync", "c", "--ful"], ["sync", "c", "--re", "up"],
                     ["post", "c", "--to", "@a", "--tit", "t", "--body", "b"],
                     ["watch", "c", "--until"], ["list", "--serv", "x"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (3, ""))
                self.assertTrue(err.startswith("ERROR config: "), err)

    def test_verbose_prints_the_log(self):
        code, _out, err = self.run_cli("ping", "fake-dest", "-v")
        self.assertEqual(code, 0)
        self.assertIn("  info  starting ssh: ", err)

    @unittest.skipUnless(os.name == "posix", "sends SIGINT")
    def test_ctrl_c_kills_ssh_and_exits_130(self):
        os.environ["FAKE_SSH_STALL"] = "30"
        # A shell may start background jobs with SIGINT ignored; Python then never sees it, so
        # the child puts back Python's own handler before it prints anything.
        script = ("import signal, sys\n"
                  "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
                  "sys.path[:0] = [%r]\n"
                  "from vcharon import cli, ssh\n"
                  "ssh.ssh_prefix = lambda settings: [sys.executable, %r]\n"
                  "sys.exit(cli.main(['ping', 'fake-dest']))\n" % (VCHARON_DIR, FAKE_SSH))
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        self.assertEqual(util.readline(proc.stdout, 20), "vcharon: ping fake-dest\n")
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        _out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 130)
        self.assertEqual(err, "vcharon: interrupted\n")
        with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"), encoding="utf-8") as f:
            self.assertIn("killing ssh: interrupted", f.read())

    def test_help_exits_0(self):
        code, out, _err = self.run_cli("--help")
        self.assertEqual(code, 0)
        verbs = ("key", "doctor", "ping", "list", "create", "join", "leave", "close", "whoami",
                 "post", "read", "watch", "sync")
        for command in verbs:
            self.assertIn("\n    %s " % command, out)
        # the exit codes, and one screen
        self.assertIn(cli.EXIT_CODES, out)
        self.assertLessEqual(len(out.splitlines()), 50)
        # each verb's own --help: test_commands' FixRoundTripTest parses its example


class Utf8ConsoleTest(FakeSshCase):
    """UTF-8 output on every OS, whatever the console or the locale."""

    def test_main_switches_both_streams(self):
        for osn in ("linux", "darwin", "windows"):
            out, err = mock.Mock(), mock.Mock()
            for stream in (out, err):
                # Python 3.14's argparse asks the stream whether to print in colour: a stream
                # with no file behind it, not a tty
                stream.fileno.side_effect = io.UnsupportedOperation
                stream.isatty.return_value = False
            with self.subTest(osn=osn), \
                    mock.patch.object(cli.platform, "os_name", return_value=osn), \
                    mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
                self.assertEqual(cli.main(["--version"]), 0)
            for stream in (out, err):
                stream.reconfigure.assert_called_once_with(encoding="utf-8",
                                                           errors="backslashreplace")

    def test_streams_without_reconfigure(self):
        class Broken:
            def reconfigure(self, **kw):
                raise ValueError("closed")

            def write(self, text):
                pass

        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", Broken()):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(out.getvalue(), VERSION + "\n")

    def test_an_ascii_console_creates_and_says_so(self):
        # a POSIX locale or PYTHONIOENCODING that isn't UTF-8: the create's lines name a path
        # that ASCII can't hold, and once the channel exists a failed print would make a retry
        # say "already exists"
        project = os.path.join(self.tmp, "mañana")
        os.makedirs(os.path.join(project, ".git"))
        env = dict(os.environ, PYTHONIOENCODING="ascii",
                   VCHARON_CHANNELS_ROOT=os.path.join(self.tmp, "chän"))
        ran = subprocess.run([sys.executable, "-P", "-m", "vcharon", "create", "t", "--local"],
                             env=env, cwd=project, capture_output=True, timeout=120,
                             check=False)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        out = ran.stdout.decode("utf-8")
        self.assertIn("chän", out)
        self.assertNotIn("internal", ran.stderr.decode("utf-8"))


def with_helper(code):
    """A patch under which cli.main's session gets a helper_override(code) helper."""
    extra = helper_override(code)
    real = ssh.Session

    class Session(real):
        def __init__(s, *args, **kw):
            kw["extra_modules"] = extra
            real.__init__(s, *args, **kw)

    return mock.patch.object(ssh, "Session", Session)


class JobTest(FakeSshCase):
    """The sync's runner (cli.run_jobs), jobs and state through fake ssh; the remote end is
    under the fake server's home."""

    def setUp(self):
        FakeSshCase.setUp(self)
        util.use_test_jobs(self)
        self.local = os.path.join(self.tmp, "local")
        self.src = os.path.join(self.local, "src")
        write_tree(self.src, {"a.txt": b"a" * 1000, "d/b.txt": b"b" * 200, "e/": None,
                              "中 文.txt": b"zh"})
        self.inbox = os.path.join(self.home, "inbox")
        os.mkdir(self.inbox)
        self.argv_file = os.path.join(self.tmp, "argv.json")
        os.environ["FAKE_SSH_ARGV_FILE"] = self.argv_file
        self.config = self.write_config(PUSH.format(src=self.src))

    def job_log(self, name="push"):
        try:
            with open(os.path.join(self.vcharon_home, "logs", name + ".log"),
                      encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def saved(self, name="push"):
        with open(state.path(name), encoding="utf-8") as f:
            return json.load(f)

    def state_bytes(self, name="push"):
        """The state file's bytes, or None: a refused run must leave them as they were."""
        try:
            with open(state.path(name), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def lock_is_free(self, name="push"):
        state.lock(name).release()

    def ok(self, *argv):
        code, out, err = self.run_jobs(*argv)
        self.assertEqual(code, 0, err)
        return out.splitlines()

    def failed(self, code, *argv):
        got, out, err = self.run_jobs(*argv)
        self.assertEqual(got, code, err)
        return out.splitlines(), err.splitlines()

    def no_ssh(self):
        """Forgets that ssh ran, for a later check that it didn't."""
        if os.path.exists(self.argv_file):
            os.remove(self.argv_file)

    def pull_job(self, dst):
        write_tree(self.home, {"outbox/x.log": b"x", "outbox/sub/y.log": b"yy"})
        os.makedirs(dst, exist_ok=True)
        self.write_config(PULL.format(dst=dst))

    # a run's lines

    def test_push(self):
        lines = self.ok("push")
        self.assertEqual(lines[:2], ["vcharon: push  %s -> fake-dest:inbox" % self.src,
                                     "  put     3 files, 2 dirs (1.2 kB)"])
        self.assertRegex(lines[2], OK_LINE % (3, 0))
        self.assertEqual(len(lines), 3)
        self.assertEqual(read_tree(self.inbox), read_tree(self.src))
        doc = self.saved()
        self.assertEqual(doc["sink"], {"end": "remote", "machine": TEST_MACHINE_ID,
                                       "root": os.path.realpath(self.inbox)})
        self.assertEqual(doc["identity"], {"end": "local", "path": self.src, "kind": "dir"})
        self.assertEqual(doc["fingerprint"], state.fingerprint(
            cli.config.load().jobs["push"]))
        self.assertEqual(sorted(doc["source"]["sent"]), ["a.txt", "d", "d/b.txt", "e",
                                                        "中 文.txt"])
        log = self.job_log()
        self.assertIn("vcharon %s sync push; Python " % VERSION, log)
        self.assertIn("  info    put     3 files, 2 dirs (1.2 kB)\n", log)
        self.assertIn("transfer: 3 files, 1202 bytes", log)
        # the second run: nothing to do, and nothing moves
        lines = self.ok("push")
        self.assertEqual(lines[:2], ["vcharon: push  %s -> fake-dest:inbox" % self.src,
                                     "  nothing to do"])
        self.assertRegex(lines[2], OK_LINE % (0, 0))
        self.assertEqual(len(lines), 3)
        second = self.job_log()[len(log):]
        self.assertIn("  info    nothing to do\n", second)
        self.assertNotIn("transfer:", second)
        self.assertNotIn("commit:", second)
        # and a change is all the third one sends
        write_tree(self.src, {"d/b.txt": b"B" * 201})
        lines = self.ok("push")
        self.assertEqual(lines[1], "  put     1 file, 0 dirs (201 B)")
        self.assertRegex(lines[2], OK_LINE % (1, 0))
        self.assertEqual(read_tree(self.inbox), read_tree(self.src))

    def test_dry_run(self):
        lines = self.ok("push", "--dry-run")
        self.assertEqual(lines[0], "vcharon: push  %s -> fake-dest:inbox  (dry run)" % self.src)
        self.assertEqual(lines[1:7], ["  put     3 files, 2 dirs (1.2 kB)", "          a.txt",
                                      "          d/", "          d/b.txt", "          e/",
                                      "          中 文.txt"])
        self.assertRegex(lines[7], r"\AOK  dry run, nothing changed  \(\d+\.\d s\)\Z")
        self.assertFalse(os.path.exists(state.path("push")))
        self.assertEqual(read_tree(self.inbox), {})
        self.ok("push")
        with open(state.path("push"), "rb") as f:
            before = f.read()
        write_tree(self.src, {"new.txt": b"n"})
        lines = self.ok("push", "--dry-run")
        self.assertEqual(lines[1:3], ["  put     1 file, 0 dirs (1 B)", "          new.txt"])
        with open(state.path("push"), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertNotIn("new.txt", read_tree(self.inbox))

    def test_dry_run_lists_at_most_fifty(self):
        sixty = os.path.join(self.local, "sixty")
        write_tree(sixty, {"f%02d" % i: b"x" for i in range(60)})
        self.write_config(PUSH.format(src=sixty))
        lines = self.ok("push", "--dry-run")
        self.assertEqual(lines[1], "  put     60 files, 0 dirs (60 B)")
        self.assertEqual(cli.LIST_MAX, 50)
        self.assertEqual(lines[2:52], ["          f%02d" % i for i in range(50)])
        self.assertEqual(lines[52], "          … and 10 more")
        self.assertRegex(lines[53], r"\AOK  dry run, nothing changed  \(\d+\.\d s\)\Z")
        self.assertEqual(len(lines), 54)
        self.assertEqual(read_tree(self.inbox), {})

    def test_full(self):
        # the target holds the tree already, with other mtimes
        write_tree(self.inbox, read_tree(self.src))
        write_tree(self.inbox, {"a.txt": b"A" * 1000})
        lines = self.ok("push", "--full")
        self.assertEqual(lines[:2], ["vcharon: push  %s -> fake-dest:inbox  (full)" % self.src,
                                     "  put     3 files, 2 dirs (1.2 kB), 2 already there"])
        # have files count as written
        self.assertRegex(lines[2], OK_LINE % (3, 0))
        self.assertIn("transfer: 1 files, 1000 bytes", self.job_log())
        self.assertEqual(read_tree(self.inbox), read_tree(self.src))
        lines = self.ok("push", "--full", "--dry-run")
        self.assertEqual(lines[:2], ["vcharon: push  %s -> fake-dest:inbox  (full, dry run)"
                                     % self.src,
                                     "  put     3 files, 2 dirs (1.2 kB), 3 already there"])
        self.assertEqual(self.ok("push")[1], "  nothing to do")

    # a fingerprint, identity or sink mismatch is refused

    def test_config_changed(self):
        self.ok("push")
        before = self.state_bytes()
        self.no_ssh()
        self.write_config(PUSH.format(src=self.src).replace("inbox", "inbox2"))
        out, err = self.failed(3, "push")
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(err[0], "ERROR state_mismatch: the state of push was saved for another "
                                 "config: its ssh, from, to, from.path or to.path changed")
        self.assertIn(RESET_HINT, err)
        self.assertEqual(err[-1], "  log: %s" % os.path.join(self.vcharon_home, "logs",
                                                            "push.log"))
        # before ssh starts, and before the header
        self.assertFalse(os.path.exists(self.argv_file))
        self.assertEqual(out, [])
        # the other options can change
        self.write_config(PUSH.format(src=self.src) + "from.allow_empty = yes\n"
                          "to.create = yes\nidle_timeout = 60\n")
        self.assertEqual(self.ok("push")[1], "  nothing to do")

    def test_pull_from_another_server(self):
        back = os.path.join(self.local, "back")
        self.pull_job(back)
        self.ok("pull")
        before = read_tree(back)
        self.assertEqual(before, {"x.log": b"x", "sub/": None, "sub/y.log": b"yy"})
        # the alias now points at another server, where outbox has less: prune mustn't run
        os.remove(os.path.join(self.home, "outbox", "x.log"))
        os.environ["VCHARON_TEST_MACHINE_ID"] = "f" * 32
        saved = self.state_bytes("pull")
        out, err = self.failed(3, "pull")
        self.assertEqual(self.state_bytes("pull"), saved)
        path = os.path.join(self.home, "outbox")
        self.assertEqual(err[0], 'ERROR state_mismatch: the state of pull was saved for another '
                                 'source: {"end": "remote", "kind": "dir", "machine": "%s", '
                                 '"path": "%s"}, now {"end": "remote", "kind": "dir", '
                                 '"machine": "%s", "path": "%s"}'
                         % (TEST_MACHINE_ID, json_inner(path), "f" * 32, json_inner(path)))
        self.assertIn(RESET_HINT, err)
        self.assertEqual(read_tree(back), before)
        self.assertEqual(out, ["vcharon: pull  fake-dest:outbox -> %s" % back])

    def test_push_to_another_server(self):
        self.ok("push")
        os.environ["VCHARON_TEST_MACHINE_ID"] = "f" * 32
        write_tree(self.src, {"new.txt": b"n"})
        before = self.state_bytes()
        _out, err = self.failed(3, "push")
        self.assertEqual(self.state_bytes(), before)
        root = os.path.realpath(self.inbox)
        self.assertEqual(err[0], 'ERROR state_mismatch: the state of push was saved for another '
                                 'target: {"end": "remote", "machine": "%s", "root": "%s"}, now '
                                 '{"end": "remote", "machine": "%s", "root": "%s"}'
                         % (TEST_MACHINE_ID, json_inner(root), "f" * 32, json_inner(root)))
        self.assertNotIn("new.txt", read_tree(self.inbox))

    @unittest.skipUnless(os.name == "posix", "needs symlinks")
    def test_local_root_moved(self):
        first, second = os.path.join(self.local, "r1"), os.path.join(self.local, "r2")
        os.makedirs(second)
        link = os.path.join(self.local, "link")
        self.pull_job(first)
        os.symlink(first, link)
        self.write_config(PULL.format(dst=link))
        self.ok("pull")
        os.remove(link)
        os.symlink(second, link)
        before = self.state_bytes("pull")
        _out, err = self.failed(3, "pull")
        self.assertEqual(self.state_bytes("pull"), before)
        # the binding holds the resolved root (on macOS the temp dir is under /private)
        self.assertEqual(err[0], 'ERROR state_mismatch: the state of pull was saved for another '
                                 'target: {"end": "local", "root": "%s"}, now {"end": "local", '
                                 '"root": "%s"}'
                         % (os.path.realpath(first), os.path.realpath(second)))
        self.assertEqual(read_tree(second), {})

    def test_malformed_state_file(self):
        self.ok("push")
        with open(state.path("push"), "wb") as f:
            f.write(b"{broken")
        self.no_ssh()
        out, err = self.failed(3, "push")
        self.assertEqual(self.state_bytes(), b"{broken")
        self.assertTrue(err[0].startswith("ERROR state_mismatch: the state file %s can't be "
                                          "read: it isn't valid JSON: " % state.path("push")),
                        err[0])
        self.assertIn(RESET_HINT, err)
        self.assertFalse(os.path.exists(self.argv_file))
        # a state the source can't read: the source's own refusal gets the job's hint too
        doc = {"schema": 1, "job": "push", "fingerprint": state.fingerprint(
                   cli.config.load().jobs["push"]),
               "identity": {"end": "local", "path": self.src, "kind": "dir"},
               "sink": {"end": "remote", "machine": TEST_MACHINE_ID,
                        "root": os.path.realpath(self.inbox)},
               "source": {"sent": {"a.txt": "file"}}, "saved": "2026-09-28T08:00:00Z"}
        with open(state.path("push"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        before = self.state_bytes()
        out, err = self.failed(3, "push")
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue(err[0].startswith("ERROR state_mismatch: the saved state is malformed: "),
                        err[0])
        self.assertIn(RESET_HINT, err)
        # after a reset the run works, and sends everything again
        code, out, err = self.reset_job("push")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-1], "removed %s" % state.path("push"))
        self.assertEqual(self.ok("push")[1], "  put     3 files, 2 dirs (1.2 kB)")

    # a second concurrent run exits with 2

    def test_busy_in_another_process(self):
        # the lock is the OS's: a second vcharon process sees it (DESIGN, "Lock")
        self.lock_is_free()
        child = hold_in_child(os.path.join(self.vcharon_home, "state", "push.lock"))
        try:
            out, err = self.failed(2, "push")
        finally:
            stop_child(child)
        self.assertEqual(err[0], "ERROR busy: another run of push is in progress")
        self.assertFalse(os.path.exists(self.argv_file))
        self.assertEqual(out, [])
        self.ok("push")

    # a directory kept because the target added a file to it is noted once, not by both the
    # check and the commit (seen on Windows with the [ca] job, 2026-09-30)
    def test_prune_kept_dir_noted_once(self):
        back = os.path.join(self.local, "back")
        os.makedirs(back)
        outbox = os.path.join(self.home, "outbox")
        write_tree(outbox, {"sub/y.txt": b"y"})
        self.write_config(PULL.format(dst=back))
        self.ok("pull")
        write_tree(back, {"sub/mine.txt": b"m"})
        os.remove(os.path.join(outbox, "sub", "y.txt"))
        os.rmdir(os.path.join(outbox, "sub"))
        self.write_config(PULL.format(dst=back) + "from.allow_empty = yes\n")
        lines = self.ok("pull")
        self.assertEqual([l for l in lines if l.startswith("  note")],
                         ["  note    kept sub: it isn't empty"])
        self.assertRegex(lines[-1], OK_LINE % (0, 1))
        self.assertEqual(read_tree(back), {"sub/": None, "sub/mine.txt": b"m"})

    # prune refuses an emptied source (test_path pins this in the plugin: test_prune,
    # test_empty_source); here, through the CLI

    def test_prune_pull(self):
        back = os.path.join(self.local, "back")
        os.makedirs(back)
        outbox = os.path.join(self.home, "outbox")
        write_tree(outbox, {"keep.txt": b"k", "sub/y.txt": b"y"})
        self.write_config(PULL.format(dst=back))
        self.ok("pull")
        everything = {"keep.txt": b"k", "sub/": None, "sub/y.txt": b"y"}
        self.assertEqual(read_tree(back), everything)
        # the server's outbox emptied, as an unmounted disk looks: refused
        shutil.rmtree(outbox)
        os.mkdir(outbox)
        before = self.state_bytes("pull")
        _out, err = self.failed(1, "pull")
        self.assertEqual(err[:2], ["ERROR empty_source: %s is empty, but earlier runs sent 3 paths "
                                   "from it" % outbox,
                                   "  fix: check that its disk is mounted; if it's really empty, "
                                   "set from.allow_empty = yes"])
        self.assertEqual(read_tree(back), everything)
        self.assertEqual(self.state_bytes("pull"), before)
        # with allow_empty the sent files go
        self.write_config(PULL.format(dst=back) + "from.allow_empty = yes\n")
        lines = self.ok("pull")
        self.assertEqual(lines[1:3], ["  put     0 files, 0 dirs (0 B)", "  delete  3"])
        self.assertRegex(lines[3], OK_LINE % (0, 3))
        self.assertEqual(read_tree(back), {})
        self.assertEqual(self.saved("pull")["source"], {"sent": {}})

    # a run's ways out: the lock is released, and the state file never says more than is true

    def test_save_fails_after_a_good_commit(self):
        full = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        for how, patch in (("state.save", mock.patch.object(state, "save", side_effect=full)),
                           ("its fsync", mock.patch.object(os, "fsync", side_effect=full))):
            with self.subTest(how=how):
                with patch:
                    out, err = self.failed(1, "push")
                self.assertEqual(err[:2], ["ERROR no_space: couldn't save the state of push: %s: "
                                           "the disk is full" % state.path("push"),
                                           "  fix: the files were written; fix that, then run "
                                           "again"])
                self.assertEqual(out[1], "  put     3 files, 2 dirs (1.2 kB)")
                self.assertNotIn("OK", "\n".join(out))
                self.assertEqual(read_tree(self.inbox), read_tree(self.src))
                # no state file, no temp file, and the lock is free
                self.assertEqual(os.listdir(os.path.join(self.vcharon_home, "state")),
                                 ["push.lock"])
                self.lock_is_free()

    @unittest.skipUnless(os.name == "posix", "needs POSIX modes")
    def test_save_fails_after_a_failed_commit(self):
        if os.geteuid() == 0:
            self.skipTest("root can write in a read-only directory")
        self.ok("push")
        before = self.state_bytes()
        write_tree(self.src, {"a2/1": b"1", "e/1": b"e1"})
        e = os.path.join(self.inbox, "e")
        os.chmod(e, 0o555)
        self.addCleanup(os.chmod, e, 0o755)
        full = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        with mock.patch.object(state, "save", side_effect=full):
            _out, err = self.failed(1, "push")
        # the run's own error; the failed save is only logged
        self.assertEqual(err[:3], ["ERROR permission: e/1: Permission denied",
                                   "  done    1 written, 0 deleted before the failure",
                                   "  fix: check the owner and permissions of e/1"])
        self.assertIn("couldn't save the state of push after the failure: ", self.job_log())
        self.assertEqual(self.state_bytes(), before)
        self.lock_is_free()

    def test_a_server_that_isnt_linux(self):
        # a Mac or Windows box has a machine id now; the client refuses it by its OS
        for osn in ("darwin", "windows"):
            os.environ["VCHARON_TEST_OS"] = osn
            _out, err = self.failed(3, "push")
            self.assertEqual(err[:2], ["ERROR state_mismatch: fake-dest runs %s: only a Linux "
                                       "server is supported as a remote end" % osn,
                                       "  fix: point fake-dest at a Linux server"])
            self.assertFalse(os.path.exists(state.path("push")))
            self.assertEqual(read_tree(self.inbox), {})

    # a commit that fails partway saves what it wrote; the next run finishes

    @unittest.skipUnless(os.name == "posix", "needs POSIX modes")
    def test_a_failed_commit_saves_what_it_wrote_push(self):
        if os.geteuid() == 0:
            self.skipTest("root can write in a read-only directory")
        self.ok("push")
        old = self.saved()["source"]["sent"]
        write_tree(self.src, {"a2/1": b"1", "a2/2": b"2", "e/1": b"e1", "e/2": b"e2"})
        e = os.path.join(self.inbox, "e")
        os.chmod(e, 0o555)
        self.addCleanup(os.chmod, e, 0o755)
        out, err = self.failed(1, "push")
        self.assertEqual(err[:3], ["ERROR permission: e/1: Permission denied",
                                   "  done    2 written, 0 deleted before the failure",
                                   "  fix: check the owner and permissions of e/1"])
        self.assertNotIn("OK", "\n".join(out))
        sent = self.saved()["source"]["sent"]
        self.assertEqual(sorted(sent), sorted(list(old) + ["a2", "a2/1", "a2/2"]))
        self.assertEqual({k: sent[k] for k in old}, old)
        self.assertIn("saved the state of push: what the failed commit wrote", self.job_log())
        os.chmod(e, 0o755)
        lines = self.ok("push")
        self.assertEqual(lines[1], "  put     2 files, 0 dirs (4 B)")
        self.assertEqual(read_tree(self.inbox), read_tree(self.src))

    # the reset (vcharon sync C --reset up|down, for a channel section's job)

    def test_reset(self):
        def reset(name="push"):
            code, out, err = self.reset_job(name)
            self.assertEqual((code, err), (0, ""))
            return out.splitlines()

        self.assertEqual(reset(), ["vcharon: no state for push"])
        self.ok("push")
        root = os.path.realpath(self.inbox)
        lines = reset()
        # a summary of what's forgotten, then the file goes
        self.assertEqual(lines[0], "vcharon: state of push  (%s)" % state.path("push"))
        self.assertRegex(lines[1], r"\A  saved     \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertEqual(lines[2:], [
            '  source    {"end": "local", "kind": "dir", "path": "%s"}' % json_inner(self.src),
            '  target    {"end": "remote", "machine": "%s", "root": "%s"}'
            % (TEST_MACHINE_ID, json_inner(root)),
            "  sent      3 files, 2 dirs",
            "removed %s" % state.path("push")])
        self.assertFalse(os.path.exists(state.path("push")))
        self.assertIn("state reset: removed %s" % state.path("push"), self.job_log())
        self.assertEqual(reset(), ["vcharon: no state for push"])
        # a malformed file: the reset removes it
        with open(state.path("push"), "wb") as f:
            f.write(b"[]")
        self.assertEqual(reset(), ["vcharon: the state of push can't be read (it isn't a JSON "
                                   "object)", "removed %s" % state.path("push")])

    def test_source_that_saves_no_state(self):
        # a saved null: the next run gets {}
        self.ok("push")
        doc = self.saved()
        doc["source"] = None
        with open(state.path("push"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.assertEqual(self.ok("push")[1], "  put     3 files, 2 dirs (1.2 kB)")

    # usage and config errors

    def test_unknown_job_and_missing_config(self):
        _out, err = self.failed(3, "nope")
        self.assertEqual(err[:2], ["ERROR config: no channel section nope in %s"
                                   % os.path.join(self.vcharon_home, "channels.d"),
                                   "  fix: " + cli.NO_SECTION_HINT])
        self.write_config(PUSH.format(src=self.src) + "from.pth = x\n")
        _out, err = self.failed(3, "push")
        self.assertEqual(err[0], "ERROR bad_options: from.pth: unknown option")
        os.remove(self.config)
        _out, err = self.failed(3, "push")
        # no config file: as no section (a join writes the section, and needs no config file)
        self.assertEqual(err[:2], ["ERROR config: no channel section push in %s"
                                   % os.path.join(self.vcharon_home, "channels.d"),
                                   "  fix: " + cli.NO_SECTION_HINT])
        self.assertFalse(os.path.exists(self.argv_file))
        self.assertFalse(os.path.exists(state.path("push")))

    def test_verbose(self):
        code, _out, err = self.run_jobs("push", "-v")
        self.assertEqual(code, 0, err)
        self.assertIn("  info  vcharon %s sync push; Python " % VERSION, err)
        self.assertIn("  info  starting ssh: ", err)
        code, _out, err = self.reset_job("push", verbose=True)
        self.assertIn("  info  state reset: removed ", err)


# three push jobs: a and b share a destination, c has another
MULTI = """
[a]
ssh       = fake-dest
from      = local:path
from.path = {local}/a
to        = remote:dir
to.path   = inbox_a

[b]
ssh       = fake-dest
from      = local:path
from.path = {local}/b
to        = remote:dir
to.path   = inbox_b
"""

# a pull on a's destination; {path} is under the server's home
PULLED = """
[p]
ssh       = fake-dest
from      = remote:path
from.path = {path}
to        = local:dir
to.path   = {local}/p
"""

# a helper that dies in the sink.check of inbox_a (lost), or refuses it as a protocol error
BREAKS = """
import os
from vcharon.proto import VCharonError

_check = _real.HANDLERS["sink.check"]

def sink_check(h, call_id, args):
    if args["options"].get("path") == "inbox_a":
        if HOW == "lost":
            os._exit(1)
        raise VCharonError("protocol", "broken on purpose")
    return _check(h, call_id, args)

_real.HANDLERS["sink.check"] = sink_check
"""

SUMMARY_OK = r"\AOK  %d jobs  \(\d+\.\d s\)\Z"
SUMMARY_FAILED = r"\AFAILED  %s  \(\d+\.\d s\)\Z"


class MultiJobTest(FakeSshCase):
    """The sync's runner with several jobs: one connection, per-job blocks, logs and
    states, the summary line and the exit code."""

    def setUp(self):
        FakeSshCase.setUp(self)
        util.use_test_jobs(self)
        self.local = os.path.join(self.tmp, "local")
        for name in "ab":
            write_tree(os.path.join(self.local, name), {name + ".txt": name.encode()})
            os.mkdir(os.path.join(self.home, "inbox_" + name))
        self.ssh_log = os.path.join(self.tmp, "ssh.log")
        os.environ["FAKE_SSH_ARGV_LOG"] = self.ssh_log
        self.config(MULTI)

    def config(self, *parts):
        self.write_config("".join(p.format(local=self.local, path="{path}") for p in parts))

    def dests(self):
        """The destination of every ssh started so far, in order."""
        try:
            with open(self.ssh_log, encoding="utf-8") as f:
                return [json.loads(line)[-2] for line in f]
        except FileNotFoundError:
            return []

    def job_log(self, name):
        try:
            with open(os.path.join(self.vcharon_home, "logs", name + ".log"),
                      encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def run_id(self, name):
        return re.search(r"  (\d{8}-\d{6}-[0-9a-f]{6})  ", self.job_log(name)).group(1)

    def inbox(self, name):
        return read_tree(os.path.join(self.home, "inbox_" + name))

    def assert_locks_free(self, *names):
        for name in names:
            state.lock(name).release()

    def test_two_jobs_one_ssh(self):
        code, out, err = self.run_jobs("a", "b")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "vcharon: a  %s/a -> fake-dest:inbox_a" % self.local)
        self.assertEqual(lines[1], "  put     1 file, 0 dirs (1 B)")
        self.assertRegex(lines[2], OK_LINE % (1, 0))
        self.assertEqual(lines[3], "vcharon: b  %s/b -> fake-dest:inbox_b" % self.local)
        self.assertEqual(lines[4], "  put     1 file, 0 dirs (1 B)")
        self.assertRegex(lines[5], OK_LINE % (1, 0))
        self.assertRegex(lines[6], SUMMARY_OK % 2)
        self.assertEqual(len(lines), 7)
        self.assertEqual(self.dests(), ["fake-dest"])
        self.assertEqual(self.inbox("a"), {"a.txt": b"a"})
        self.assertEqual(self.inbox("b"), {"b.txt": b"b"})
        for name in "ab":
            with open(state.path(name), encoding="utf-8") as f:
                self.assertEqual(list(json.load(f)["source"]["sent"]), [name + ".txt"])
        # the session's own lines are a's; b says whose connection it used
        a, b = self.job_log("a"), self.job_log("b")
        self.assertIn("vcharon %s sync a b; Python " % VERSION, a)
        self.assertIn("vcharon %s sync a b; Python " % VERSION, b)
        self.assertIn("starting ssh", a)
        self.assertIn("ssh exited with code 0", a)
        self.assertNotIn("starting ssh", b)
        self.assertNotIn("ssh exited", b)
        self.assertIn("  info  shares the connection of a (run %s)\n" % self.run_id("a"), b)
        self.assertNotEqual(self.run_id("a"), self.run_id("b"))
        self.assert_locks_free("a", "b")
        # a second run: nothing to do for either, still one connection
        lines = self.run_jobs("a", "b")[1].splitlines()
        self.assertEqual([lines[1], lines[4]], ["  nothing to do"] * 2)
        self.assertEqual(self.dests(), ["fake-dest"] * 2)

    def test_dry_run_and_full_apply_to_every_job(self):
        lines = self.run_jobs("a", "b", "--dry-run")[1].splitlines()
        self.assertEqual([l for l in lines if l.startswith("vcharon: ")],
                         ["vcharon: a  %s/a -> fake-dest:inbox_a  (dry run)" % self.local,
                          "vcharon: b  %s/b -> fake-dest:inbox_b  (dry run)" % self.local])
        self.assertFalse(os.path.exists(state.path("a")))
        self.assertFalse(os.path.exists(state.path("b")))
        lines = self.run_jobs("--full", "a", "b")[1].splitlines()
        self.assertEqual([l for l in lines if l.startswith("vcharon: ")],
                         ["vcharon: a  %s/a -> fake-dest:inbox_a  (full)" % self.local,
                          "vcharon: b  %s/b -> fake-dest:inbox_b  (full)" % self.local])

    def test_busy_second_job(self):
        held = state.lock("b")
        self.addCleanup(held.release)
        code, out, err = self.run_jobs("a", "b")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err.splitlines()[:2], ["ERROR busy: another run of b is in progress",
                                                "  fix: wait for it to finish"])
        self.assertEqual(self.dests(), [])
        self.assertFalse(os.path.exists(state.path("a")))
        self.assertIn("not run: another run of b is in progress", self.job_log("a"))
        self.assert_locks_free("a")
        held.release()
        self.assertEqual(self.run_jobs("a", "b")[0], 0)

    def test_bad_option_in_the_second_job(self):
        self.config(MULTI.replace("to.path   = inbox_b\n", "to.path   = inbox_b\nto.bogus  = 1\n"))
        code, out, err = self.run_jobs("a", "b")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        # the option's text doesn't name the job; with several jobs the line does
        self.assertTrue(err.startswith("ERROR b: bad_options: "), err)
        self.assertIn("bogus", err.splitlines()[0])
        self.assertEqual(self.dests(), [])
        self.assertEqual(self.job_log("a"), "")
        self.assertFalse(os.path.exists(state.path("a")))

    def test_a_failed_job_doesnt_stop_the_next(self):
        # p's source is missing on the server: the helper refuses its plan, and stays in step;
        # q then plans on the same connection, which only a job.reset allows
        write_tree(self.home, {"outbox/x.log": b"x"})
        self.config(MULTI, PULLED.format(local=self.local, path="missing"),
                    PULLED.replace("[p]", "[q]").replace("{local}/p", "{local}/q")
                    .format(local=self.local, path="outbox"))
        os.makedirs(os.path.join(self.local, "p"))
        os.makedirs(os.path.join(self.local, "q"))
        code, out, err = self.run_jobs("p", "q", "a")
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual(lines[0], "vcharon: p  fake-dest:missing -> %s/p" % self.local)
        self.assertEqual(lines[1], "vcharon: q  fake-dest:outbox -> %s/q" % self.local)
        self.assertEqual(lines[2], "  put     1 file, 0 dirs (1 B)")
        self.assertRegex(lines[3], OK_LINE % (1, 0))
        self.assertEqual(lines[4], "vcharon: a  %s/a -> fake-dest:inbox_a" % self.local)
        self.assertRegex(lines[-1], SUMMARY_FAILED % "1 of 3 jobs failed")
        self.assertTrue(err.startswith("ERROR p: not_found: "), err)
        self.assertIn("  log: %s" % os.path.join(self.vcharon_home, "logs", "p.log"), err)
        self.assertEqual(self.dests(), ["fake-dest"])
        self.assertEqual(read_tree(os.path.join(self.local, "q")), {"x.log": b"x"})
        self.assertFalse(os.path.exists(state.path("p")))
        self.assertTrue(os.path.exists(state.path("q")))
        self.assertIn("error  not_found: ", self.job_log("p"))
        self.assert_locks_free("p", "q", "a")

    def test_exit_code_is_the_first_failure(self):
        # b's state is for another config (exit 3); a's inbox is gone (exit 1)
        os.makedirs(os.path.dirname(state.path("b")), exist_ok=True)
        with open(state.path("b"), "w", encoding="utf-8") as f:
            f.write("{}")
        shutil.rmtree(os.path.join(self.home, "inbox_a"))
        for argv, code, errors in ((("b", "a"), 3, [["ERROR b", "state_mismatch"],
                                                    ["ERROR a", "not_found"]]),
                                   (("a", "b"), 1, [["ERROR a", "not_found"],
                                                    ["ERROR b", "state_mismatch"]])):
            with self.subTest(argv=argv):
                got, out, err = self.run_jobs(*argv)
                self.assertEqual(got, code, err)
                self.assertRegex(out.splitlines()[-1], SUMMARY_FAILED % "2 of 2 jobs failed")
                self.assertEqual([l.split(": ")[:2] for l in err.splitlines()
                                  if l.startswith("ERROR")], errors)

    def test_the_error_line_names_the_failing_job(self):
        # with several jobs the console's ERROR line says which failed; one job's line,
        # and the job's log in both runs, are as before
        os.makedirs(os.path.dirname(state.path("b")), exist_ok=True)
        with open(state.path("b"), "w", encoding="utf-8") as f:
            f.write("{}")
        message = ("state_mismatch: the state file %s can't be read: it has no schema, job, "
                   "fingerprint, identity, sink, source, saved" % state.path("b"))
        code, _out, err = self.run_jobs("a", "b")
        self.assertEqual(code, 3)
        self.assertEqual(err.splitlines()[0], "ERROR b: " + message)
        self.assertEqual([l for l in err.splitlines() if l.startswith("ERROR")],
                         ["ERROR b: " + message])
        self.assertEqual(self.inbox("a"), {"a.txt": b"a"})
        code, _out, err = self.run_jobs("b")
        self.assertEqual(code, 3)
        self.assertEqual(err.splitlines()[0], "ERROR " + message)
        logged = [line.split("  error  ", 1)[1] for line in self.job_log("b").splitlines()
                  if "  error  " in line]
        self.assertEqual([line for line in logged if "can't be read" in line], [message] * 2)
        self.assertNotIn("b: state_mismatch", self.job_log("b"))

    def test_a_broken_connection_skips_the_rest(self):
        for how, first_error in (("lost", "ERROR a: lost: "), ("protocol", "ERROR a: protocol: ")):
            with self.subTest(how=how):
                os.environ["FAKE_SSH_ARGV_LOG"] = self.ssh_log = os.path.join(
                    self.tmp, "ssh-%s.log" % how)
                with with_helper("HOW = %r\n" % how + BREAKS):
                    code, out, err = self.run_jobs("a", "b")
                self.assertEqual(code, 1)
                lines = out.splitlines()
                self.assertEqual(lines[:2], ["vcharon: a  %s/a -> fake-dest:inbox_a" % self.local,
                                             "vcharon: b  skipped: the connection to fake-dest "
                                             "broke"])
                self.assertRegex(lines[-1], SUMMARY_FAILED % "1 of 2 jobs failed, 1 skipped")
                self.assertTrue(err.startswith(first_error), err)
                self.assertEqual(self.dests(), ["fake-dest"])
                self.assertEqual(self.inbox("b"), {})
                self.assertFalse(os.path.exists(state.path("b")))
                self.assertIn("vcharon: b  skipped: the connection to fake-dest broke",
                              self.job_log("b"))
                self.assert_locks_free("a", "b")

    def test_a_connect_failure_skips_the_rest(self):
        os.environ["FAKE_SSH_EXIT"] = "255"
        code, out, _err = self.run_jobs("a", "b")
        self.assertEqual(code, 4)
        self.assertEqual(out.splitlines()[1],
                         "vcharon: b  skipped: the connection to fake-dest broke")
        self.assertRegex(out.splitlines()[-1], SUMMARY_FAILED % "1 of 2 jobs failed, 1 skipped")
        self.assertEqual(self.dests(), ["fake-dest"])

    def test_ctrl_c_stops_everything(self):
        calls = []

        def interrupted(eng, *args, **kw):
            calls.append(eng.sink_side.options["path"])
            raise KeyboardInterrupt

        with mock.patch.object(engine.Engine, "run", interrupted):
            code, out, err = self.run_jobs("a", "b")
        self.assertEqual((code, err), (130, "vcharon: interrupted\n"))
        self.assertEqual(calls, ["inbox_a"])
        self.assertEqual(out.splitlines(), ["vcharon: a  %s/a -> fake-dest:inbox_a" % self.local])
        self.assertIn("  error  interrupted", self.job_log("a"))
        self.assertEqual(self.dests(), ["fake-dest"])
        self.assert_locks_free("a", "b")
        # an interrupted run saves no state
        self.assertFalse(os.path.exists(state.path("a")))
        self.assertFalse(os.path.exists(state.path("b")))

    def test_no_machine_id_fails_every_job_on_it(self):
        with with_helper(NO_MACHINE):
            code, out, err = self.run_jobs("a", "b")
        self.assertEqual(code, 3)
        self.assertRegex(out.splitlines()[-1], SUMMARY_FAILED % "2 of 2 jobs failed")
        self.assertEqual([l for l in err.splitlines() if l.startswith("ERROR")],
                         ["ERROR %s: state_mismatch: the server fake-dest has no machine id, so "
                          "vcharon can't tie the state of %s to it" % (name, name)
                          for name in "ab"])
        self.assertIn("  fix: " + platform.runnable(state.NO_MACHINE_HINT), err.splitlines())
        self.assertFalse(os.path.exists(state.path("a")))
        self.assertFalse(os.path.exists(state.path("b")))
        self.assertEqual((self.inbox("a"), self.inbox("b")), ({}, {}))
        self.assertEqual(self.dests(), ["fake-dest"])

    @unittest.skipUnless(os.name == "posix", "needs POSIX modes")
    def test_a_commit_that_fails_partway(self):
        # the failed job saves what its commit wrote, bound to its own target; the other job
        # is untouched by it, and so is the other job's error block
        if os.geteuid() == 0:
            self.skipTest("root can write in a read-only directory")
        for name in "ab":
            write_tree(os.path.join(self.local, name), {"ro/x": b"x"})
            os.mkdir(os.path.join(self.home, "inbox_" + name, "ro"))
        for failing, other in (("a", "b"), ("b", "a"), ("a", None)):
            with self.subTest(failing=failing, other=other):
                for name in "ab":
                    if os.path.exists(state.path(name)):
                        os.remove(state.path(name))
                    ro = os.path.join(self.home, "inbox_" + name, "ro")
                    os.chmod(ro, 0o555 if name == failing else 0o755)
                    self.addCleanup(os.chmod, ro, 0o755)
                if other is None:
                    # b then fails before its engine: a state for another config
                    with open(state.path("b"), "w", encoding="utf-8") as f:
                        f.write("{}")
                logs = {n: len(self.job_log(n)) for n in "ab"}
                code, out, err = self.run_jobs("a", "b")
                # a's partial commit (1), whatever b did
                self.assertEqual(code, 1)
                blocks = re.split(r"\n(?=ERROR )", err.strip())
                self.assertEqual(len(blocks), 2 if other is None else 1, err)
                self.assertIn("  done    1 written, 0 deleted before the failure", blocks[0])
                if other is None:
                    self.assertTrue(blocks[1].startswith("ERROR b: state_mismatch: "),
                                    blocks[1])
                    self.assertNotIn("  done ", blocks[1])
                    self.assertRegex(out.splitlines()[-1], SUMMARY_FAILED % "2 of 2 jobs failed")
                else:
                    self.assertRegex(out.splitlines()[-1], SUMMARY_FAILED % "1 of 2 jobs failed")
                for name in "ab":
                    new_log = self.job_log(name)[logs[name]:]
                    line = "saved the state of %s: what the failed commit wrote" % name
                    if name == failing:
                        self.assertIn(line, new_log)
                    else:
                        self.assertNotIn("what the failed commit wrote", new_log)
                    if name == "b" and other is None:
                        continue
                    with open(state.path(name), encoding="utf-8") as f:
                        doc = json.load(f)
                    self.assertEqual(doc["sink"], {
                        "end": "remote", "machine": TEST_MACHINE_ID,
                        "root": os.path.realpath(os.path.join(self.home, "inbox_" + name))})
                    self.assertEqual(doc["identity"], {"end": "local", "kind": "dir",
                                                       "path": os.path.join(self.local, name)})
                    sent = sorted(doc["source"]["sent"])
                    if name == failing:
                        self.assertNotIn("ro/x", sent)
                        self.assertIn(name + ".txt", sent)
                    else:
                        self.assertEqual(sent, sorted([name + ".txt", "ro", "ro/x"]))

    def test_a_protocol_error_here_skips_the_rest(self):
        # the helper's job.reset answers something malformed: the controller refuses it, and
        # the helper may be out of step, so d is skipped although the session looks usable
        self.config(MULTI, "\n[d]\nssh = fake-dest\nfrom = local:path\nfrom.path = {local}/d\n"
                    "to = remote:dir\nto.path = inbox_d\n")
        os.mkdir(os.path.join(self.home, "inbox_d"))
        write_tree(os.path.join(self.local, "d"), {"d.txt": b"d"})
        bad = ("def job_reset(h, call_id, args):\n    h.ok(call_id, {'x': 1})\n\n"
               "_real.HANDLERS['job.reset'] = job_reset\n")
        with with_helper(bad):
            code, out, err = self.run_jobs("a", "b", "d")
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertIn("vcharon: d  skipped: the connection to fake-dest broke", lines)
        self.assertRegex(lines[-1], SUMMARY_FAILED % "1 of 3 jobs failed, 1 skipped")
        self.assertTrue(err.startswith("ERROR b: protocol: a malformed job.reset result"), err)
        self.assertEqual(self.dests(), ["fake-dest"])
        self.assertEqual(self.inbox("a"), {"a.txt": b"a"})
        self.assertEqual(self.inbox("d"), {})

    def test_proxy_warnings_go_to_the_jobs_own_log(self):
        # b's sink.commit fails without saying what it did: the warning is b's, though a
        # opened the connection
        bad = ("from vcharon import proto as _proto\n"
               "def sink_commit(h, call_id, args):\n"
               "    h.conn.send_json(_proto.err_msg(call_id, VCharonError('io', 'no')))\n"
               "_real.HANDLERS['sink.commit'] = sink_commit\n"
               "from vcharon.proto import VCharonError\n")
        with with_helper(bad):
            code, _out, _err = self.run_jobs("a", "b")
        self.assertEqual(code, 1)
        warning = "sink.commit failed without saying what it did"
        self.assertEqual(self.job_log("a").count(warning), 1)
        self.assertEqual(self.job_log("b").count(warning), 1)
        self.assertIn("starting ssh", self.job_log("a"))

    def test_one_job_as_before(self):
        # one job prints no summary line, and no skip or share line
        # anywhere; its error goes to stderr alone
        lines = self.run_jobs("a")[1].splitlines()
        self.assertEqual(lines[:2], ["vcharon: a  %s/a -> fake-dest:inbox_a" % self.local,
                                     "  put     1 file, 0 dirs (1 B)"])
        self.assertRegex(lines[2], OK_LINE % (1, 0))
        self.assertEqual(len(lines), 3)
        lines = self.run_jobs("a")[1].splitlines()
        self.assertEqual(lines[1], "  nothing to do")
        self.assertRegex(lines[2], OK_LINE % (0, 0))
        self.assertEqual(len(lines), 3)
        shutil.rmtree(os.path.join(self.home, "inbox_b"))
        code, out, err = self.run_jobs("b")
        self.assertEqual(code, 1)
        self.assertEqual(out, "vcharon: b  %s/b -> fake-dest:inbox_b\n" % self.local)
        err = err.splitlines()
        self.assertTrue(err[0].startswith("ERROR not_found: "), err)
        self.assertEqual(err[-1], "  log: %s" % os.path.join(self.vcharon_home, "logs", "b.log"))
        self.assertEqual(len(err), 3)
        self.assertNotIn("shares the connection", self.job_log("a") + self.job_log("b"))
        self.assertIn("vcharon %s sync b; Python " % VERSION, self.job_log("b"))


class Between:
    """cli._wait_or_end for --repeat tests: each wait between rounds runs the next action;
    with none left, stdin has ended."""

    def __init__(self, *actions):
        self.actions = list(actions)
        self.waits = []

    def __call__(self, ended, seconds):
        self.waits.append(seconds)
        if not self.actions:
            return True
        self.actions.pop(0)()
        return False


class RepeatTest(FakeSshCase):
    """The sync's --repeat: rounds of the jobs on one session, per-round locks,
    the round's lines on stdout, its exit, and a quiet disk while there's nothing to do. On
    MultiJobTest's jobs and helpers, without its tests."""

    maxDiff = None

    config = MultiJobTest.config
    dests = MultiJobTest.dests
    job_log = MultiJobTest.job_log
    inbox = MultiJobTest.inbox
    assert_locks_free = MultiJobTest.assert_locks_free

    def setUp(self):
        MultiJobTest.setUp(self)
        # never the test runner's own stdin; one test reads a real one, in a child
        patcher = mock.patch.object(cli, "_watch_stdin", lambda ended: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def repeat(self, *argv, between=()):
        """The runner on ARGV --repeat 1, with the actions between its rounds: (exit code, stdout
        lines, stderr, the Between)."""
        waits = Between(*between)
        with mock.patch.object(cli, "_wait_or_end", waits):
            code, out, err = self.run_jobs(*argv + ("--repeat", "1"))
        return code, out.splitlines(), err, waits

    def job_log_path(self, name):
        return os.path.join(self.vcharon_home, "logs", name + ".log")

    def test_refusals(self):
        for extra, what in ((["--full"], "--full"), (["--dry-run"], "--dry-run")):
            with self.subTest(flag=what):
                code, out, err = self.run_jobs("a", "--repeat", "2", *extra)
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(err.splitlines()[:2],
                                 ["ERROR config: --repeat doesn't go with %s" % what,
                                  "  fix: leave one of them out"])
        for value in ("0", "301", "x", "1.5", "²", ""):
            with self.subTest(value=value):
                code, out, err = self.run_cli("sync", "mb", "--repeat", value)
                self.assertEqual(code, 3)
                self.assertTrue(err.startswith("ERROR config: argument --repeat: must be a "
                                               "whole number of seconds, 1 to 300"), err)
        self.assertEqual(self.dests(), [])
        self.assert_locks_free("a", "b")

    def test_rounds_on_one_session(self):
        resets = []
        real = cli.remote.reset

        def reset(session):
            resets.append(session)
            return real(session)

        with mock.patch.object(cli.remote, "reset", reset):
            code, lines, err, waits = self.repeat("a", "b", between=[lambda: None] * 2)
        self.assertEqual((code, err), (0, ""))
        # a good job prints nothing: three rounds, three ROUND lines
        self.assertEqual(lines, ["ROUND 0"] * 3)
        self.assertEqual(waits.waits, [1] * 3)
        self.assertEqual(self.dests(), ["fake-dest"])
        # job.reset before every job but the session's first: b in round 1, both after
        self.assertEqual(len(resets), 5)
        self.assertEqual(len(set(map(id, resets))), 1)
        a = self.job_log("a")
        self.assertEqual(a.count("hello from fake-dest"), 1)
        self.assertIn("vcharon %s sync a b --repeat 1; Python " % VERSION, a)
        self.assertIn("repeat: stdin ended; stopping after 3 rounds since ", a)
        self.assertIn("ssh exited with code 0", a)
        self.assertEqual(self.inbox("a"), {"a.txt": b"a"})
        self.assertEqual(self.inbox("b"), {"b.txt": b"b"})
        self.assert_locks_free("a", "b")

    def test_a_closed_stdout_ends_the_command(self):
        # stdout's reader gone mid-job is the whole command's end, not job a's internal error:
        # no ERROR line, no ROUND line, b never runs, a's lock is freed
        ran = []

        def gone(args, jr, conn, stack):
            ran.append(jr.name)
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

        with mock.patch.object(cli, "_run_one", gone):
            code, lines, err, _waits = self.repeat("a", "b", between=[lambda: None])
        self.assertEqual((code, lines, err, ran), (1, [], "", ["a"]))
        self.assertIn("stdout closed: [Errno 32] Broken pipe", self.job_log("a"))
        self.assert_locks_free("a", "b")

    def code_file(self):
        """A stand-in for the binary the watchdog compares, and the swap an update makes."""
        code = os.path.join(self.tmp, "vcharon-binary")
        with open(code, "wb") as f:
            f.write(b"old")
        patcher = mock.patch.object(install, "code_path", return_value=code)
        patcher.start()
        self.addCleanup(patcher.stop)

        def swap():
            with open(code + ".new", "wb") as f:
                f.write(b"new!")
            os.replace(code + ".new", code)

        return swap

    def test_updated_at_the_top_of_a_round(self):
        # on its own, never relying on its watcher: before any other work of the round
        swap = self.code_file()
        code, lines, err, waits = self.repeat("a", "b", between=[lambda: None, swap])
        self.assertEqual((code, err), (install.EXIT_UPDATED, ""))
        self.assertEqual(lines, ["ROUND 0", "ROUND 0", "EXIT updated"])
        self.assertEqual(len(waits.waits), 2)
        self.assertIn("repeat: vcharon changed under it; exiting with 14", self.job_log("a"))
        self.assertIn("ssh exited with code 0", self.job_log("a"))
        self.assert_locks_free("a", "b")

    def test_an_orphan_at_the_top_of_a_round(self):
        # a binary whose bootloader parent was killed: exits on its own, locks freed. The
        # parent is faked (OrphanTest has the real checks); the Watchdog builds it here
        # (run_jobs doesn't go through main). handle None: had exit_with_parent built it, no
        # thread would follow it
        gone = []

        class Parent:
            pid = 4242
            handle = None
            followed = False

            def gone(self):
                return bool(gone)

        def kill_parent():
            gone.append(True)

        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.object(install, "Parent", Parent):
            code, lines, err, _ = self.repeat("a", "b", between=[kill_parent])
        self.assertEqual((code, err), (install.EXIT_ORPHANED, ""))
        self.assertEqual(lines, ["ROUND 0", "EXIT orphaned"])
        self.assertIn("repeat: the process that started this one (pid 4242) is gone; exiting "
                      "with 15", self.job_log("a"))
        self.assert_locks_free("a", "b")

    def crash_in_round_two(self, swap):
        """_run_one, raising in the second round, after the swap if there is one."""
        real = cli._run_one
        calls = []

        def run_one(*args):
            calls.append(1)
            if len(calls) == 3:
                if swap is not None:
                    swap()
                raise ValueError("bad marshal data (unknown type code)")
            return real(*args)

        return mock.patch.object(cli, "_run_one", run_one)

    def test_an_unexpected_error_in_a_binary_after_a_swap(self):
        swap = self.code_file()
        with self.crash_in_round_two(swap), \
                mock.patch.object(platform, "is_frozen", return_value=True):
            code, lines, err, _ = self.repeat("a", "b", between=[lambda: None] * 3)
        self.assertEqual((code, err), (install.EXIT_UPDATED, ""))
        # not a round's ERROR line: the watcher would take it for one
        self.assertEqual(lines, ["ROUND 0", "EXIT updated"])
        self.assertIn("vcharon changed under this process (ValueError: bad marshal data (unknown "
                      "type code)); exiting with 14", self.job_log("a"))
        self.assert_locks_free("a", "b")

    def test_an_unexpected_error_without_a_swap_is_a_rounds_error(self):
        self.code_file()
        with self.crash_in_round_two(None), \
                mock.patch.object(platform, "is_frozen", return_value=True):
            code, lines, err, _ = self.repeat("a", "b", between=[lambda: None] * 2)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines[0], "ROUND 0")
        self.assertEqual(lines[1], "ERROR a: internal: ValueError: bad marshal data (unknown "
                                   "type code)")
        self.assertNotIn("EXIT updated", lines)

    def test_new_files_in_a_later_round(self):
        def more():
            write_tree(os.path.join(self.local, "b"), {"new.txt": b"new"})

        code, lines, err, _ = self.repeat("a", "b", between=[lambda: None, more])
        self.assertEqual((code, lines, err), (0, ["ROUND 0"] * 3, ""))
        self.assertEqual(self.inbox("b"), {"b.txt": b"b", "new.txt": b"new"})
        with open(state.path("b"), encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)["source"]["sent"]), ["b.txt", "new.txt"])
        # b's log has its round-1 lines and its round-3 lines; round 2 had nothing to do
        b = self.job_log("b")
        self.assertEqual(b.count("  info  vcharon: b  "), 2)
        self.assertEqual(b.count("saved the state of b"), 2)
        self.assertEqual(len(re.findall(r"  info  OK  1 written, 0 deleted", b)), 2)

    def test_a_busy_job_sits_out_one_round(self):
        held = []

        def hold():
            held.append(state.lock("b"))
            write_tree(os.path.join(self.local, "b"), {"late.txt": b"late"})

        def release():
            held.pop().release()

        code, lines, err, _ = self.repeat("a", "b", between=[hold, release])
        self.assertEqual((code, err), (0, ""))
        # busy prints no block: the round's code says it
        self.assertEqual(lines, ["ROUND 0", "ROUND 2", "ROUND 0"])
        self.assertEqual(self.inbox("b"), {"b.txt": b"b", "late.txt": b"late"})
        self.assertIn("not run this round: another run of b is in progress", self.job_log("b"))
        self.assertEqual(self.dests(), ["fake-dest"])
        self.assert_locks_free("a", "b")

    def test_a_failed_job_doesnt_stop_the_rounds(self):
        shutil.rmtree(os.path.join(self.local, "a"))
        code, lines, err, _ = self.repeat("a", "b", between=[lambda: None])
        self.assertEqual((code, err), (0, ""))
        block = lines[:lines.index("ROUND 1")]
        self.assertTrue(block[0].startswith("ERROR a: not_found: "), block)
        self.assertEqual(block[-1], "  log: %s" % self.job_log_path("a"))
        # the same block again in round 2, and ROUND 1 both times
        self.assertEqual(lines, block + ["ROUND 1"] + block + ["ROUND 1"])
        self.assertEqual(self.inbox("b"), {"b.txt": b"b"})
        self.assertEqual(self.dests(), ["fake-dest"])
        # a failed round logs its lines
        self.assertEqual(self.job_log("a").count("  error  not_found: "), 2)

    def test_a_broken_connection_ends_it(self):
        for how in ("lost", "protocol"):
            with self.subTest(how=how):
                os.environ["FAKE_SSH_ARGV_LOG"] = self.ssh_log = os.path.join(
                    self.tmp, "ssh-%s.log" % how)
                with with_helper("HOW = %r\n" % how + BREAKS):
                    code, lines, err, waits = self.repeat("a", "b", between=[lambda: None])
                self.assertEqual((code, err), (1, ""))
                self.assertTrue(lines[0].startswith("ERROR a: %s: " % how), lines)
                self.assertIn("  log: %s" % self.job_log_path("a"), lines)
                self.assertEqual(lines[-2:], ["vcharon: b  skipped: the connection to fake-dest "
                                              "broke", "ROUND 1"])
                # no second round, no second connection
                self.assertEqual(waits.waits, [])
                self.assertEqual(self.dests(), ["fake-dest"])
                self.assertIn("repeat: the connection broke; exiting with 1", self.job_log("a"))
                self.assert_locks_free("a", "b")

    def test_a_connect_failure_ends_it(self):
        os.environ["FAKE_SSH_EXIT"] = "255"
        code, lines, err, waits = self.repeat("a", "b", between=[lambda: None])
        self.assertEqual((code, err), (4, ""))
        self.assertTrue(lines[0].startswith("ERROR a: connect: "), lines)
        self.assertEqual(lines[-2:], ["vcharon: b  skipped: the connection to fake-dest broke",
                                      "ROUND 4"])
        self.assertEqual(waits.waits, [])

    def test_nothing_to_do_writes_nothing(self):
        stats = {}

        def look():
            for name in "ab":
                st = os.stat(state.path(name))
                stats[name] = (st.st_ino, st.st_mtime_ns, st.st_size)
                stats[name + ".log"] = os.path.getsize(self.job_log_path(name))
            # a coarse clock can't hide a rewrite: the files look a minute old now
            for name in "ab":
                past = time.time() - 60
                os.utime(state.path(name), (past, past))
                st = os.stat(state.path(name))
                stats[name] = (st.st_ino, st.st_mtime_ns, st.st_size)

        code, lines, err, _ = self.repeat("a", "b", between=[look, lambda: None, lambda: None])
        self.assertEqual((code, lines, err), (0, ["ROUND 0"] * 4, ""))
        for name in "ab":
            st = os.stat(state.path(name))
            self.assertEqual((st.st_ino, st.st_mtime_ns, st.st_size), stats[name])
        # b logs nothing after round 1; a only its line for the end
        self.assertEqual(os.path.getsize(self.job_log_path("b")), stats["b.log"])
        with open(self.job_log_path("a"), encoding="utf-8") as f:
            f.seek(stats["a.log"])
            rest = f.read().splitlines()
        self.assertEqual(len(rest), 2, rest)
        self.assertIn("  info  repeat: stdin ended; stopping after 4 rounds since ", rest[0])
        self.assertIn("  info  ssh exited with code 0", rest[1])

    def test_the_summary_line(self):
        with mock.patch.object(cli, "REPEAT_SUMMARY", 0):
            code, _lines, err, _ = self.repeat("a", "b", between=[lambda: None])
        self.assertEqual((code, err), (0, ""))
        summaries = re.findall(r"  info  (repeat: \d+ rounds since [-0-9: ]+, nothing to do in "
                               r"\d+)\n", self.job_log("a"))
        self.assertEqual([re.sub(r"since [-0-9: ]+,", "since T,", s) for s in summaries],
                         ["repeat: 1 rounds since T, nothing to do in 0",
                          "repeat: 1 rounds since T, nothing to do in 1"])
        self.assertNotIn("repeat: ", self.job_log("b"))

    def test_run_timeout_counts_within_a_round(self):
        # every wait falls after end_round and before the next start_round, so run_timeout
        # never counts it; the first round's session opens in it and counts from there. That
        # the timer stops between the two is test_session's.
        events = []
        start_round, end_round = ssh.Session.start_round, ssh.Session.end_round

        def start(session):
            events.append("start")
            start_round(session)

        def end(session):
            events.append("end")
            end_round(session)
        with mock.patch.object(ssh.Session, "start_round", start), \
                mock.patch.object(ssh.Session, "end_round", end):
            code, lines, err, _ = self.repeat("a", "b",
                                              between=[lambda: events.append("wait")] * 2)
        self.assertEqual((code, lines, err), (0, ["ROUND 0"] * 3, ""))
        self.assertEqual(events, ["end", "wait", "start", "end", "wait", "start", "end"])
        self.assertNotIn("killing ssh", self.job_log("a"))

    def channel(self):
        """MailboxTest's channel section and its record: the argv of its sync."""
        local = os.path.join(self.tmp, "local", "box")
        # the test jobs go: only a child with the seam could read them
        self.write_config("[vcharon]\n")
        write_channel_section(self, MAILBOX.format(local=local))
        write_tree(os.path.join(self.home, "vcharon_mailbox"), {"debian/STEPS.md": b"steps",
                                                                "windows/": None})
        write_tree(os.path.join(local, "windows"), {"MEMBER.md": MEMBER})
        self.write_record("mb", "windows", "debian")
        return ["sync", "mb", "--project", "p"]

    def child(self, *argv, before=""):
        """vcharon ARGV in a child with the real stdin thread, through the fake ssh; before:
        code it runs first."""
        code = ("import sys\n"
                "sys.path.insert(0, %r)\n"
                "from vcharon import cli, ssh\n"
                "ssh.ssh_prefix = lambda settings: [sys.executable, %r]\n"
                "%s"
                "sys.exit(cli.main(sys.argv[1:]))\n" % (VCHARON_DIR, FAKE_SSH, before))
        # -S: no site, so no .pth file is read, as in a binary. site.py
        # decodes .pth files with utf-8-sig, which would hide a lazy import of that codec from
        # the import checks; the path insert above stands in for the editable install's .pth.
        # the environment's scripts folder first on PATH: an entry point named vcharon there
        # makes a fix line's spelling read sysconfig's data, as on CI
        path = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")
        return subprocess.Popen([sys.executable, "-S", "-c", code] + list(argv),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=dict(os.environ, PATH=path))

    def test_a_broken_connection_with_stdin_open_in_a_child(self):
        # a stdin thread blocked in sys.stdin.buffer would abort the exit at interpreter
        # shutdown ("Fatal Python error"), and the watcher would take that for an error
        os.environ["FAKE_SSH_EXIT"] = "255"
        argv = self.channel() + ["--repeat", "1"]
        for _ in range(3):
            child = self.child(*argv)
            try:
                # stdin stays open until the child has exited
                out = util.read_all(child.stdout)
                err = util.read_all(child.stderr)
                child.wait(60)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()
            self.assertEqual((child.returncode, err), (4, b""))
            self.assertEqual(out.replace(b"\r", b"").splitlines()[-1], b"ROUND 4")

    def test_updated_in_a_child_with_no_imports_after_its_start(self):
        # sync --repeat as a watcher starts it: rounds, then the binary is swapped; it exits
        # 14 on its own, and its rounds needed no module that wasn't there at the start
        swap = self.code_file()
        code_path = install.code_path()
        imported = os.path.join(self.tmp, "imported.json")
        before = ("import atexit, json, os\n"
                  "from vcharon import install\n"
                  "install.code_path = lambda: %r\n"
                  "start = []\n"
                  "init = install.Watchdog.__init__\n"
                  "def watched(self, *args, **kwargs):\n"
                  "    init(self, *args, **kwargs)\n"
                  "    start.append(set(sys.modules))\n"
                  "install.Watchdog.__init__ = watched\n"
                  "def dump():\n"
                  "    with open(%r, 'w') as f:\n"
                  "        json.dump(sorted(set(sys.modules) - start[0]), f)\n"
                  "atexit.register(dump)\n" % (code_path, imported))
        child = self.child(*self.channel() + ["--repeat", "1"], before=before)
        try:
            # two rounds: the first sends and saves, the second has nothing to do
            lines = [util.readline(child.stdout) for _ in range(2)]
            swap()
            # stdin stays open: its end would stop the child too. Its output is a few lines,
            # so the pipes can't fill while it is waited for.
            child.wait(60)
            out = util.read_all(child.stdout)
            err = util.read_all(child.stderr)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
        self.assertEqual((child.returncode, err), (14, b""))
        lines = [line.rstrip(b"\r\n") for line in lines] + out.replace(b"\r", b"").splitlines()
        self.assertEqual(lines[:2], [b"ROUND 0"] * 2)
        self.assertEqual(lines[-1], b"EXIT updated")
        with open(imported, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])
        self.assertIn("repeat: vcharon changed under it; exiting with 14",
                      self.job_log("mb.windows.up"))
        self.assert_locks_free("mb.windows.up", "mb.windows.down")

    def test_end_of_stdin_in_a_child(self):
        # the real stdin thread: the child syncs rounds until its stdin ends, then says bye
        child = self.child(*self.channel() + ["--repeat", "1"])
        try:
            lines = [util.readline(child.stdout)]
            child.stdin.close()
            # communicate() would flush the closed pipe
            child.stdin = None
            out, err = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual([l.rstrip(b"\r\n") for l in lines], [b"ROUND 0"])
        self.assertEqual(out.replace(b"\r", b""), b"ROUND 0\n" * (len(out.splitlines())))
        self.assertEqual(err, b"")
        up = self.job_log("mb.windows.up")
        self.assertIn("repeat: stdin ended; stopping after ", up)
        self.assertIn("ssh exited with code 0", up)
        self.assert_locks_free("mb.windows.up", "mb.windows.down")


# A channel section, in channels.d/: a mailbox section with a leader, a
# <channel>.<me> name, a pre-made server folder and MEMBER.md, as vcharon channel join leaves them.
MAILBOX = """
[mb.windows]
ssh            = fake-dest
mailbox.me     = windows
mailbox.leader = debian
mailbox.local  = {local}
mailbox.remote = vcharon_mailbox
"""
# the command that syncs it: its record (MailboxTest.setUp) is for the project p
SYNC = ("sync", "mb", "--project", "p")
# the fix for a member's own folder that lost its files on this box
REJOIN = ("vcharon join mb --server fake-dest --project p takes its files back from the server "
          "(a rejoin)")
MEMBER = (b"# MEMBER\n\n## 2026-10-01 10:00 \xe2\x80\x94 windows#1 \xe2\x80\x94 member\n"
          b"to: @debian\nchannel: mb\nname: windows\nleader: debian\n")


REAL_CHECK_PLAN = pathrules.check_plan


def write_channel_section(case, text, section="mb.windows"):
    """text, dedented, as $VCHARON_HOME/channels.d/<section>.ini."""
    folder = os.path.join(case.vcharon_home, "channels.d")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, section + ".ini"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(text))


class MailboxTest(FakeSshCase):
    """A channel section end to end: the fake server's home
    holds the channel vcharon_mailbox/, led by "debian", and this client is the member
    "windows", joined: its server folder is made, its local own folder holds MEMBER.md."""

    def setUp(self):
        FakeSshCase.setUp(self)
        util.use_test_jobs(self)
        self.local = os.path.join(self.tmp, "local", "box")
        self.own = os.path.join(self.local, "windows")
        self.server = os.path.join(self.home, "vcharon_mailbox")
        self.ssh_log = os.path.join(self.tmp, "ssh.log")
        os.environ["FAKE_SSH_ARGV_LOG"] = self.ssh_log
        write_channel_section(self, MAILBOX.format(local=self.local))
        # the leader made the channel first; the claim made this member's folder
        write_tree(self.server, {"debian/STEPS.md": b"steps", "windows/": None})
        write_tree(self.own, {"MEMBER.md": MEMBER})
        # vcharon sync finds the membership by its record (DESIGN, "Which membership")
        self.write_record("mb", "windows", "debian")

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return out.splitlines()

    def jobs_ok(self, *argv):
        """The sync's runner on the jobs named: a section's one job alone."""
        code, out, err = self.run_jobs(*argv)
        self.assertEqual(code, 0, err)
        return out.splitlines()

    def ssh_count(self):
        with open(self.ssh_log, encoding="utf-8") as f:
            return len(f.readlines())

    def test_round_trip(self):
        lines = self.ok(*SYNC)
        # up sends MEMBER.md, which join wrote
        self.assertEqual(lines[:2], ["vcharon: mb.windows.up  %s -> "
                                     "fake-dest:vcharon_mailbox/windows" % self.own,
                                     "  put     1 file, 0 dirs (%d B)" % len(MEMBER)])
        self.assertRegex(lines[2], OK_LINE % (1, 0))
        self.assertEqual(lines[3], "vcharon: mb.windows.down  fake-dest:vcharon_mailbox -> %s"
                         % self.local)
        self.assertRegex(lines[-1], r"\AOK  2 jobs  \(\d+\.\d s\)\Z")
        self.assertEqual(self.ssh_count(), 1)
        self.assertEqual(read_tree(self.local), {"debian/": None, "debian/STEPS.md": b"steps",
                                                 "windows/": None, "windows/MEMBER.md": MEMBER})
        self.assertEqual(read_tree(os.path.join(self.server, "windows")), {"MEMBER.md": MEMBER})
        # up sends a new file of this writer's; down brings another writer's, deep ones too
        write_tree(self.own, {"RESULTS.md": b"results"})
        write_tree(self.server, {"mac/RESULTS.md": b"mac", "debian/windows/x": b"x"})
        self.ok(*SYNC)
        self.assertEqual(read_tree(os.path.join(self.server, "windows")),
                         {"MEMBER.md": MEMBER, "RESULTS.md": b"results"})
        self.assertEqual(read_tree(self.local), {
            "debian/": None, "debian/STEPS.md": b"steps", "debian/windows/": None,
            "debian/windows/x": b"x", "mac/": None, "mac/RESULTS.md": b"mac",
            "windows/": None, "windows/MEMBER.md": MEMBER, "windows/RESULTS.md": b"results"})
        # down never touches this writer's folder, whatever the server's copy holds
        write_tree(self.server, {"windows/RESULTS.md": b"edited at the server",
                                 "windows/extra.md": b"extra"})
        self.ok(*SYNC)
        self.assertEqual(read_tree(self.own), {"MEMBER.md": MEMBER, "RESULTS.md": b"results"})
        # a file deleted in the own folder goes at the server; one Debian deletes goes here
        os.remove(os.path.join(self.own, "RESULTS.md"))
        os.remove(os.path.join(self.server, "debian", "STEPS.md"))
        lines = self.ok(*SYNC)
        self.assertEqual(lines[1:3], ["  put     0 files, 0 dirs (0 B)", "  delete  1"])
        self.assertEqual(read_tree(os.path.join(self.server, "windows")),
                         {"MEMBER.md": MEMBER, "extra.md": b"extra"})
        self.assertNotIn("debian/STEPS.md", read_tree(self.local))
        # MEMBER.md gone while up has sent it is an emptied or replaced folder: neither job
        # runs, and the server's copy stays
        os.remove(os.path.join(self.own, "MEMBER.md"))
        code, _out, err = self.run_cli(*SYNC)
        self.assertEqual(code, 1)
        self.assertEqual([l for l in err.splitlines() if l.startswith(("ERROR", "  fix"))][:2], [
            "ERROR mb.windows.up: not_found: the own folder %s has no MEMBER.md, which "
            "mb.windows.up has sent: it was emptied or replaced" % self.own,
            "  fix: " + platform.runnable(
                "vcharon join mb --server fake-dest --project p takes its files back from the "
                "server (a rejoin); MEMBER.md is vcharon's: to drop other files, delete them one "
                "by one and keep it")])
        self.assertEqual(read_tree(os.path.join(self.server, "windows")),
                         {"MEMBER.md": MEMBER, "extra.md": b"extra"})
        # vcharon sync --reset up refuses while MEMBER.md is gone: the rejoin is the fix
        code, _out, err = self.run_cli(*SYNC + ("--reset", "up"))
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR channel: your own folder %s has no MEMBER.md: forgetting what up has sent "
            "would leave it so" % self.own,
            "  fix: " + platform.runnable(REJOIN)])
        self.assertTrue(os.path.exists(state.path("mb.windows.up")))
        # the reset itself: after it, an empty own folder is fine (allow_empty) and up, with
        # no state, deletes nothing
        self.assertEqual(self.reset_job("mb.windows.up")[0], 0)
        self.ok(*SYNC)
        self.assertEqual(read_tree(self.own), {})
        self.assertEqual(state.load("mb.windows.up").source, {"sent": {}})
        self.assertEqual(read_tree(os.path.join(self.server, "windows")),
                         {"MEMBER.md": MEMBER, "extra.md": b"extra"})

    def test_the_server_removes_this_writers_folder(self):
        # the server's windows/ goes: down never planned it, so it has nothing to delete,
        # and the client's own files stay
        write_tree(self.own, {"RESULTS.md": b"results"})
        self.ok(*SYNC)
        self.assertNotIn("windows", state.load("mb.windows.down").source["sent"])
        shutil.rmtree(os.path.join(self.server, "windows"))
        lines = self.jobs_ok("mb.windows.down")
        self.assertEqual(lines[1], "  nothing to do")
        self.assertEqual(read_tree(self.own), {"MEMBER.md": MEMBER, "RESULTS.md": b"results"})
        # up never makes it again (create = no); its fix line says to leave
        code, _out, err = self.run_jobs("mb.windows.up")
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR not_found: the root %s doesn't exist" % os.path.join(self.server, "windows"),
            "  fix: " + platform.runnable(cli.CHANNEL_GONE_HINT % ("mb", "--project p"))])
        # the job's log keeps the plain text: read later, maybe on another box
        with open(os.path.join(self.vcharon_home, "logs", "mb.windows.up.log"),
                  encoding="utf-8") as f:
            self.assertIn("fix: " + cli.CHANNEL_GONE_HINT % ("mb", "--project p"), f.read())
        self.assertFalse(os.path.exists(os.path.join(self.server, "windows")))

    # --- case twins can't stall the mailbox ---

    def folding(self, osn="darwin"):
        """The client's sink checks plans as a client on osn does."""
        # the module's own, not one an earlier subtest patched in
        real = REAL_CHECK_PLAN
        patcher = mock.patch.object(pathrules, "check_plan",
                                    lambda plan, _osn: real(plan, osn))
        patcher.start()
        self.addCleanup(patcher.stop)

    def down_log(self):
        with open(os.path.join(self.vcharon_home, "logs", "mb.windows.down.log"),
                  encoding="utf-8") as f:
            return f.read()

    def left_out_lines(self):
        return [line.split("  info  ", 1)[1] for line in self.down_log().splitlines()
                if "left out at the top" in line]

    def edit_down_sent(self, change):
        with open(state.path("mb.windows.down"), encoding="utf-8") as f:
            raw = json.load(f)
        change(raw["source"]["sent"])
        with open(state.path("mb.windows.down"), "w", encoding="utf-8") as f:
            json.dump(raw, f)

    def test_a_writer_folder_turned_into_a_link_or_a_file(self):
        # the server replaces another writer's folder by a symlink or a file: the walk leaves
        # it out, and left out means never deleted, whatever sent says
        for kind in ("symlink", "file"):
            with self.subTest(kind=kind):
                if kind == "symlink" and not CAN_SYMLINK:
                    # no privilege for os.symlink here: the "file" row still covers "not a folder"
                    continue
                self.setUp()
                write_tree(self.server, {"mac/x.md": b"x", "other/": None})
                self.ok(*SYNC)
                self.assertIn("mac/x.md", read_tree(self.local))
                shutil.rmtree(os.path.join(self.server, "mac"))
                if kind == "symlink":
                    os.symlink("other", os.path.join(self.server, "mac"))
                else:
                    write_tree(self.server, {"mac": b"now a file"})
                lines = self.ok(*SYNC)
                self.assertFalse(any(line.startswith("  delete") for line in lines), lines)
                self.assertEqual(read_tree(os.path.join(self.local, "mac")), {"x.md": b"x"})
                self.assertNotIn("mac", state.load("mb.windows.down").source["sent"])
                self.assertIn("mac", self.left_out_lines()[-1])

    def test_a_gone_writer_folder_without_its_own_entry_in_sent(self):
        # sent holds mac/x.md but not mac: a valid writer's name gone from the server is
        # that writer's folder going, and the client's copy follows
        self.ok(*SYNC)
        write_tree(self.local, {"mac/x.md": b"x"})
        st = os.stat(os.path.join(self.local, "mac", "x.md"))
        self.edit_down_sent(lambda sent: sent.update({"mac/x.md": [st.st_size, st.st_mtime,
                                                                   False]}))
        self.ok(*SYNC)
        self.assertNotIn("mac/x.md", read_tree(self.local))

    def files_here(self):
        """The files of the local tree, less the own MEMBER.md that every test has."""
        return sorted(p for p in read_tree(self.local)
                      if not p.endswith("/") and p != "windows/MEMBER.md")

    def test_strays_at_the_top_dont_block(self):
        # each case below, on the client OS it names: none may be an "ERROR collision" or a
        # pulled stray
        rows = (({"windows/RESULTS.md": b"r", "Windows/CASE.md": b"c"}, "darwin",
                 "Windows/"),
                ({"Debian/CASE.md": b"c"}, "darwin", "Debian/"),
                ({"Debian/CASE.md": b"c"}, "windows", "Debian/"),
                ({"Windows/CASE.md": b"c"}, "darwin", "Windows/"))
        for spec, osn, stray in rows:
            with self.subTest(spec=spec, osn=osn):
                # a stray next to its lowercase writer's folder is a twin on this disk, the
                # own folder windows/ (setUp makes it on the server) too
                tops = {p.split("/")[0] for p in spec} | {"debian", "mac", "windows"}
                if util.folds_case() and stray.rstrip("/").lower() in tops:
                    self.skipTest(util.FOLDS_CASE)
                self.setUp()
                self.folding(osn)
                write_tree(self.server, dict(spec, **{"mac/hello.md": b"hi"}))
                lines = self.ok(*SYNC)
                self.assertRegex(lines[-1], r"\AOK  2 jobs")
                # the other writers' files arrive; nothing from the strays or the server's
                # copy of the own folder
                self.assertEqual(self.files_here(), ["debian/STEPS.md", "mac/hello.md"])
                # exactly the strays: never the own folder windows/
                self.assertEqual(self.left_out_lines(), [
                    "helper: left out at the top, not another writer's folder: %s" % stray])
                # the console shows what the run did, as before
                self.assertFalse(any("left out" in line for line in lines), lines)

    # --- fix lines a mailbox writer can follow ---

    DOWN_FIX = ("  fix: the writer of each folder named above %s; your up still runs; more: "
                "vcharon guide errors")
    # with the writer's folder: MailboxTest's writer is windows
    UP_FIX = "  fix: %s in your own folder (windows/)"

    def refused(self, job, error, fix):
        """A sync: a job fails with error and fix, on the console and in its log;
        the other job still runs. The console's ERROR line names the job, as the watcher
        shows it; the job's own log has the line as before."""
        code, out, err = self.run_cli(*SYNC)
        self.assertEqual(code, 1, err)
        lines = err.splitlines()
        shown = "ERROR mb.windows.%s: %s" % (job, error[len("ERROR "):])
        self.assertIn(shown, lines)
        # the console spells a command as this install runs vcharon; the log keeps it plain
        self.assertEqual(lines[lines.index(shown) + 1], platform.runnable(fix))
        # the watcher's line: vcharon's first stderr line
        self.assertEqual(lines[0], shown)
        self.assertEqual(sum(1 for line in lines if "fix:" in line), 1, err)
        with open(os.path.join(self.vcharon_home, "logs", "mb.windows.%s.log" % job),
                  encoding="utf-8") as f:
            logged = [line.split("  error  ", 1)[1] for line in f.read().splitlines()
                      if "  error  " in line]
        at = logged.index(error[len("ERROR "):])
        self.assertEqual(logged[at + 1], fix.strip())
        self.assertFalse(any(line.startswith("mailbox.") for line in logged), logged)
        self.assertRegex(out.splitlines()[-1], r"\AFAILED  1 of 2 jobs failed")
        return out

    @unittest.skipIf(util.folds_case(), util.FOLDS_CASE)
    def test_a_blocked_down_says_who_fixes_it(self):
        # a twin below the top would make one file of two on the client; the run is
        # refused there (data safety), and the server's watcher warns
        self.folding()
        write_tree(self.server, {"debian/Notes.md": b"N", "debian/notes.md": b"n"})
        self.refused("down", "ERROR collision: debian/Notes.md and debian/notes.md are the "
                     "same path on macOS", self.DOWN_FIX % "removes or renames one of them")
        self.assertEqual(self.files_here(), [])

    def test_a_reserved_name_blocks_a_windows_down(self):
        self.folding("windows")
        write_tree(self.server, {"debian/CON.md": b"c"})
        self.refused("down", "ERROR unsafe_path: debian/CON.md: CON.md is a reserved name on "
                     "Windows", self.DOWN_FIX % "removes or renames it")

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_a_symlink_in_another_writers_folder(self):
        # refused by the helper, on the server: its hint comes over the wire
        os.symlink("STEPS.md", os.path.join(self.server, "debian", "lnk"))
        self.refused("down", "ERROR unsafe_path: debian/lnk: a symlink",
                     self.DOWN_FIX % "removes or renames it")

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_a_symlink_in_the_own_folder(self):
        write_tree(self.own, {"RESULTS.md": b"r"})
        os.symlink("RESULTS.md", os.path.join(self.own, "lnk"))
        self.refused("up", "ERROR unsafe_path: lnk: a symlink", self.UP_FIX % "remove or rename it")

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs names that aren't UTF-8")
    def test_names_that_arent_utf8(self):
        write_tree(self.own, {"RESULTS.md": b"r"})
        with open(os.path.join(os.fsencode(self.own), b"bad\xff.md"), "wb"):
            pass
        self.refused("up", "ERROR unsafe_path: bad\\xff.md: the name isn't valid "
                     "UTF-8", self.UP_FIX % "remove or rename it")
        os.remove(os.path.join(os.fsencode(self.own), b"bad\xff.md"))
        with open(os.path.join(os.fsencode(self.server), b"debian", b"bad\xff.md"), "wb"):
            pass
        self.refused("down", "ERROR unsafe_path: debian/bad\\xff.md: the name isn't valid "
                     "UTF-8", self.DOWN_FIX % "removes or renames it")

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "needs POSIX modes, not root")
    def test_a_folder_that_cant_be_listed(self):
        write_tree(self.own, {"RESULTS.md": b"r", "sub/x": b"x"})
        sub = os.path.join(self.own, "sub")
        os.chmod(sub, 0)
        self.addCleanup(os.chmod, sub, 0o755)
        self.refused("up", "ERROR permission: can't list %s: Permission denied" % sub,
                     self.UP_FIX % "fix its permissions")
        os.chmod(sub, 0o755)
        write_tree(self.server, {"debian/sub/x": b"x"})
        sub = os.path.join(self.server, "debian", "sub")
        os.chmod(sub, 0)
        self.addCleanup(os.chmod, sub, 0o755)
        self.refused("down", "ERROR permission: can't list %s: Permission denied" % sub,
                     self.DOWN_FIX % "fixes its permissions")

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "needs POSIX modes, not root")
    def test_a_permission_error_on_the_own_tree_keeps_its_hint(self):
        # the client's copy can't be written: no writer's business
        self.ok(*SYNC)
        debian = os.path.join(self.local, "debian")
        os.chmod(debian, 0o555)
        self.addCleanup(os.chmod, debian, 0o755)
        write_tree(self.server, {"debian/new.md": b"n"})
        code, _out, err = self.run_cli(*SYNC)
        self.assertEqual(code, 1)
        self.assertIn("ERROR mb.windows.down: permission: ", err)
        self.assertNotIn("writer", err)
        self.assertNotIn("own folder", err)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "needs POSIX modes, not root")
    def test_a_root_that_cant_be_listed_keeps_its_hint(self):
        # the tree itself, on either side: no writer's folder to point at
        for (job, root), mode in itertools.product((("down", self.server), ("up", self.own)),
                                                   (0o300, 0)):
            with self.subTest(job=job, mode=mode):
                os.makedirs(root, exist_ok=True)
                os.chmod(root, mode)
                try:
                    code, _out, err = self.run_cli(*SYNC)
                finally:
                    os.chmod(root, 0o755)
                self.assertEqual(code, 1)
                self.assertIn("ERROR mb.windows.%s: permission: can't list %s: Permission denied\n"
                              "  fix: fix its permissions\n" % (job, root), err)

    def test_a_name_too_long_for_the_target_keeps_its_hint(self):
        # fsops' hint: a root path can be too long too, which no writer fixes
        self.assertNotIn(fsops.TOO_LONG_HINT, cli.SOURCE_HINTS)

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_a_target_side_error_keeps_its_hint(self):
        # an unsafe_path about the client's own tree: its hint was right already
        self.ok(*SYNC)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(elsewhere)
        shutil.rmtree(os.path.join(self.local, "debian"))
        os.symlink(elsewhere, os.path.join(self.local, "debian"))
        write_tree(self.server, {"debian/new.md": b"n"})
        code, _out, err = self.run_cli(*SYNC)
        self.assertEqual(code, 1)
        self.assertIn("ERROR mb.windows.down: unsafe_path: ", err)
        self.assertIn("\n  fix: %s\n" % fsops.LINK_HINT, err)
        self.assertEqual(os.listdir(elsewhere), [])

    @unittest.skipIf(util.folds_case(), util.FOLDS_CASE)
    def test_other_jobs_keep_the_general_hints(self):
        # a plain pull from the same tree: the general hints, exactly as before
        dst = os.path.join(self.tmp, "pulled")
        self.write_config(PULL.format(dst=dst).replace("outbox", "vcharon_mailbox"))
        self.folding()
        write_tree(self.server, {"debian/Notes.md": b"N", "debian/notes.md": b"n"})
        code, _out, err = self.run_jobs("pull")
        self.assertEqual(code, 1)
        self.assertIn("ERROR collision: debian/Notes.md and debian/notes.md are the same path "
                      "on macOS\n  fix: rename one of them at the source\n", err)
        os.remove(os.path.join(self.server, "debian", "notes.md"))
        write_tree(self.server, {"debian/CON.md": b"c"})
        self.folding("windows")
        code, _out, err = self.run_jobs("pull")
        self.assertIn("ERROR unsafe_path: debian/CON.md: CON.md is a reserved name on Windows"
                      "\n  fix: rename these paths at the source\n", err)
        os.remove(os.path.join(self.server, "debian", "CON.md"))
        if CAN_SYMLINK:
            os.symlink("STEPS.md", os.path.join(self.server, "debian", "lnk"))
            code, _out, err = self.run_jobs("pull")
            self.assertIn("ERROR unsafe_path: debian/lnk: a symlink\n  fix: "
                          "remove them at the source\n", err)

    def test_top_level_files_and_reserved_names_are_left_out(self):
        write_tree(self.server, {"notes.md": b"n", "README": b"r", "con/x.md": b"x",
                                 "LPT1/y": b"y", "-a/z": b"z", ".hidden/z": b"z",
                                 "mac/hello.md": b"hi", "debian/mac/x": b"x",
                                 "debian/Windows/y": b"y"})
        # a symlink at the top is left out too, not a failed plan
        left_out = "-a/, .hidden/, LPT1/, README, con/, notes.md"
        if CAN_SYMLINK:
            os.symlink("mac", os.path.join(self.server, "link"))
            left_out = "-a/, .hidden/, LPT1/, README, con/, link, notes.md"
        self.ok(*SYNC)
        # a folder named like a writer below the top is just a folder
        self.assertEqual(self.files_here(), ["debian/STEPS.md", "debian/Windows/y",
                                             "debian/mac/x", "mac/hello.md"])
        self.assertIn("helper: left out at the top, not another writer's folder: %s" % left_out,
                      self.down_log())
        sent = state.load("mb.windows.down").source["sent"]
        self.assertEqual(sorted(p.split("/")[0] for p in sent), [
            "debian"] * 6 + ["mac"] * 2)

    @unittest.skipIf(util.folds_case(), util.FOLDS_CASE)
    def test_a_down_state_holding_names_down_leaves_out(self):
        # A saved down state whose sent holds names down never plans (the own folder, Windows/,
        # top-level files) runs without a reset: those paths leave sent, and nothing is
        # deleted here (DESIGN, "The path source").
        write_tree(self.own, {"RESULTS.md": b"results"})
        self.ok(*SYNC)
        write_tree(self.local, {"Windows/": None, "notes.md": b"n", "README": b"r"})
        doc = state.load("mb.windows.down")
        sent = dict(doc.source["sent"])
        for path in ("windows", "Windows"):
            sent[path] = "d"
        for path in ("notes.md", "README"):
            st = os.stat(os.path.join(self.local, path))
            sent[path] = [st.st_size, st.st_mtime, False]
        with open(state.path("mb.windows.down"), encoding="utf-8") as f:
            raw = json.load(f)
        raw["source"]["sent"] = sent
        with open(state.path("mb.windows.down"), "w", encoding="utf-8") as f:
            json.dump(raw, f)
        # the server still has some of them, and lost the others
        write_tree(self.server, {"notes.md": b"n", "Windows/": None, "mac/hello.md": b"hi"})
        lines = self.ok(*SYNC)
        self.assertNotIn("  delete", "\n".join(lines))
        got = read_tree(self.local)
        for path in ("Windows/", "notes.md", "README", "windows/RESULTS.md"):
            self.assertIn(path, got)
        self.assertIn("mac/hello.md", got)
        self.assertEqual(sorted(state.load("mb.windows.down").source["sent"]),
                         ["debian", "debian/STEPS.md", "mac", "mac/hello.md"])
        # a later run with the strays gone at the server deletes nothing either
        os.remove(os.path.join(self.server, "notes.md"))
        lines = self.ok(*SYNC)
        self.assertIn("notes.md", read_tree(self.local))

    def test_a_closed_channel(self):
        # The channel's folder gone (closed) fails both jobs with not_found,
        # whose fix line says to leave, never "create it"; nothing is made at the server, even
        # with something to send.
        shutil.rmtree(self.server)
        write_tree(self.own, {"hello.md": b"hi"})
        for attempt in range(2):
            code, _out, err = self.run_cli(*SYNC)
            self.assertEqual(code, 1)
            errors = [l for l in err.splitlines() if l.startswith(("ERROR", "  fix"))]
            self.assertEqual(errors, [
                "ERROR mb.windows.up: not_found: the root %s doesn't exist"
                % os.path.join(self.server, "windows"),
                "  fix: " + platform.runnable(cli.CHANNEL_GONE_HINT % ("mb", "--project p")),
                "ERROR mb.windows.down: not_found: %s doesn't exist" % self.server,
                "  fix: " + platform.runnable(cli.CHANNEL_GONE_HINT % ("mb", "--project p"))])
            self.assertNotIn(stage.ROOT_HINT, err)
            self.assertFalse(os.path.exists(self.server))

    def test_one_job_alone(self):
        shutil.rmtree(self.local)
        lines = self.jobs_ok("mb.windows.down")
        self.assertEqual(lines[0], "vcharon: mb.windows.down  fake-dest:vcharon_mailbox -> %s"
                         % self.local)
        self.assertEqual(len(lines), 3)
        self.assertTrue(os.path.isdir(self.own))

    def test_doctor_scope(self):
        # doctor checks a section's two jobs as one destination
        dests = doctor._scope(None, cli.config.load())
        self.assertEqual([[j.name for j in d.jobs] for d in dests],
                         [["mb.windows.up", "mb.windows.down"]])

    def test_the_own_folder_in_another_case_on_the_server(self):
        # a server's Windows/ is this writer's folder on a Windows or macOS client; down
        # leaves it out on the Linux server too
        write_tree(self.server, {"Windows/RESULTS.md": b"theirs", "WINDOWS/x": b"x"})
        self.ok(*SYNC)
        self.assertEqual(self.files_here(), ["debian/STEPS.md"])

    def test_a_vanished_own_folder(self):
        # after up has sent files, a missing own folder is an error: made again empty, up's
        # prune would empty the server's copy
        write_tree(self.own, {"RESULTS.md": b"results"})
        self.ok(*SYNC)
        os.remove(os.path.join(self.own, "MEMBER.md"))
        shutil.rmtree(self.local)
        for attempt in range(2):
            code, _out, err = self.run_cli(*SYNC)
            self.assertEqual(code, 1)
            errors = [l for l in err.splitlines() if l.startswith(("ERROR", "  fix"))]
            self.assertEqual(errors, [
                line for job in ("up", "down") for line in (
                    "ERROR mb.windows.%s: not_found: the mailbox's own folder %s is gone, but "
                    "mb.windows.up has sent files from it" % (job, self.own),
                    "  fix: " + platform.runnable(REJOIN))])
            self.assertFalse(os.path.exists(self.local))
            self.assertEqual(read_tree(os.path.join(self.server, "windows")),
                             {"MEMBER.md": MEMBER, "RESULTS.md": b"results"})
        # its reset is refused (the rejoin is the fix); the reset itself makes the folder
        # again at the next sync, and nothing is deleted
        self.assertEqual(self.run_cli(*SYNC + ("--reset", "up"))[0], 1)
        self.assertEqual(self.reset_job("mb.windows.up")[0], 0)
        self.ok(*SYNC)
        self.assertEqual(read_tree(self.own), {})
        self.assertEqual(read_tree(os.path.join(self.server, "windows")),
                         {"MEMBER.md": MEMBER, "RESULTS.md": b"results"})

    def test_dry_run_makes_nothing(self):
        # down's sink creates its root when it commits; a dry run makes no folder at all
        shutil.rmtree(self.local)
        lines = self.jobs_ok("mb.windows.down", "--dry-run")
        self.assertEqual(lines[1:3], ["  put     1 file, 1 dir (5 B)", "          debian/"])
        self.assertFalse(os.path.exists(self.local))
        _code, _out, _err = self.run_cli(*SYNC, "--dry-run")
        self.assertFalse(os.path.exists(self.local))

    def test_down_allows_an_empty_tree(self):
        self.ok(*SYNC)
        shutil.rmtree(os.path.join(self.server, "debian"))
        lines = self.ok(*SYNC)
        self.assertIn("  delete  2", lines)
        self.assertEqual(read_tree(self.local), {"windows/": None, "windows/MEMBER.md": MEMBER})

    def test_reset_wants_a_job_and_goes_alone(self):
        for argv in (["--reset"], ["--reset", "both"], ["--reset", "mb.windows.up"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*SYNC + tuple(argv))
                self.assertEqual((code, out), (3, ""))
                self.assertTrue(err.startswith("ERROR config: argument --reset: "), err)
        for flag in (["--full"], ["--dry-run"], ["--repeat", "2"]):
            with self.subTest(flag=flag):
                code, out, err = self.run_cli(*SYNC + ("--reset", "up") + tuple(flag))
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(err.splitlines()[:2], [
                    "ERROR config: --reset doesn't go with %s" % flag[0],
                    "  fix: run the reset on its own, then sync"])

    def test_doctor_before_the_first_run_through_doctor(self):
        shutil.rmtree(self.local)
        os.environ.pop("SSH_AUTH_SOCK", None)
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0, out + err)
        lines = [l.strip() for l in out.splitlines() if "from.path" in l]
        self.assertEqual(len(lines), 2, out)
        self.assertTrue(lines[0].endswith("%s doesn't exist yet; vcharon sync mb --project p "
                                          "makes it" % self.own), lines[0])
        self.assertTrue(lines[1].endswith("%s: a directory" % self.server), lines[1])
        # the server's tree missing is a closed channel, a FAIL that says to leave; so is up's
        # folder there
        shutil.rmtree(self.server)
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 1, out)
        fails = [l.strip() for l in out.splitlines() if l.startswith("  FAIL")]
        self.assertEqual(len(fails), 2, out)
        self.assertEqual(out.count("fix: " + platform.runnable(
            cli.CHANNEL_GONE_HINT % ("mb", "--project p"))), 2, out)
        self.assertNotIn(stage.ROOT_HINT, out)
        # once up has sent files, a missing own folder is a FAIL
        write_tree(self.server, {"debian/": None, "windows/": None})
        write_tree(self.own, {"a": b"a"})
        self.ok(*SYNC)
        shutil.rmtree(self.local)
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("has sent files from it", out)

    def test_doctor_keeps_other_failures(self):
        # only the folders a run makes stop being a FAIL before the first run (the test above)
        job = cli.config.load().jobs["mb.windows.up"]
        failing = ("FAIL", "can't list %s: Permission denied" % self.own, "check it")
        self.assertEqual(doctor._made_by_the_run(job, *failing), failing)

    def test_changing_the_tree_is_a_state_mismatch_for_both(self):
        # a section is named <channel>.<me>, so its writer can't change under one name;
        # the fingerprint (with the writer's name) still binds both jobs to the section's keys
        self.ok(*SYNC)
        write_tree(self.home, {"elsewhere/windows/": None})
        write_channel_section(self, MAILBOX.format(local=self.local).replace(
            "= vcharon_mailbox", "= elsewhere"))
        code, _out, err = self.run_cli(*SYNC)
        self.assertEqual(code, 3)
        self.assertEqual([l for l in err.splitlines() if l.startswith("ERROR")],
                         ["ERROR mb.windows.%s: state_mismatch: the state of mb.windows.%s was "
                          "saved for another config: its ssh, mailbox.me, mailbox.local or "
                          "mailbox.remote changed" % (name, name)
                          for name in ("up", "down")])

    def test_same_second_edit(self):
        # a file rewritten with the same size right after the
        # run that sent it. sent keeps st_mtime, a float, so the edit shows wherever the file
        # system's mtimes are finer than the time between the two writes.
        path = os.path.join(self.own, "LOG.md")
        write_tree(self.own, {"LOG.md": b"first"})
        self.ok(*SYNC)
        before = os.stat(path).st_mtime_ns
        write_tree(self.own, {"LOG.md": b"again"})
        after = os.stat(path).st_mtime_ns
        lines = self.ok(*SYNC)
        if after == before:
            self.skipTest("this file system gave both writes one mtime")
        self.assertEqual(lines[1], "  put     1 file, 0 dirs (5 B)")
        self.assertEqual(read_tree(os.path.join(self.server, "windows"))["LOG.md"], b"again")


class PullGuardTest(unittest.TestCase):
    def test_pull_name(self):
        cases = {"outbox/report.pdf": "report.pdf", "~/.config": ".config",
                 "/var/log/myapp/": "myapp", "~bob/x": "x", "~//x": "x", "x/../y": "y",
                 "../x": "x", "~/~": "~", "a/./b/.": "b",
                 "": None, "~": None, "~bob": None, "~/": None, ".": None, "..": None,
                 "/": None, "a/..": None, "//": None}
        for path, name in cases.items():
            self.assertEqual(cli.pull_name(path), name, path)

    def refused(self, path, entries):
        with self.assertRaises(VCharonError) as cm:
            cli.pull_guard(path)(Plan(entries))
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(cm.exception.hint, PULL_HINT)
        return cm.exception.message

    def test_passes(self):
        f = put_file("x", 1, 1790000000.0)
        for path, entries in [
                ("outbox/report.pdf", []),
                ("outbox/report.pdf", [put_file("report.pdf", 1, 1790000000.0)]),
                ("outbox/logs", [put_dir("logs"), put_dir("logs/d"), put_file("logs/d/a", 1, 0.0)]),
                ("outbox/logs", [put_dir("logs")]),
                ("~/.config", [put_dir(".config"), put_file(".config/x", 1, 0.0)]),
                ("", [put_dir("me"), put_file("me/.bashrc", 1, 0.0)]),
                ("~", [put_dir("me")]), (".", [put_dir("me")]), ("~bob", [put_dir("bob")]),
                ("x/..", [f])]:
            with self.subTest(path=path, entries=entries):
                cli.pull_guard(path)(Plan(entries))

    def test_refuses(self):
        f = lambda p: put_file(p, 1, 1790000000.0)
        cases = [
            ("logs", [put_dir("logs"), delete("logs/old")],
             "the server's plan deletes logs/old, and a pull never deletes"),
            ("logs", [delete("x", tree=True), put_dir("logs")],
             "the server's plan deletes x, and a pull never deletes"),
            ("logs", [f("logs/a")], 'the server\'s plan starts with "logs/a", not with one name'),
            ("logs", [f("")], 'the server\'s plan starts with "", not with one name'),
            ("logs", [f("logs"), f("logs/a")],
             "the server's plan puts logs as a file, and more after it"),
            ("logs", [put_dir("logs"), f("logsx/a")],
             "the server's plan puts logsx/a, outside logs"),
            ("logs", [put_dir("logs"), put_dir("logs")],
             "the server's plan puts logs, outside logs"),
            ("logs", [put_dir("other")],
             "the server's plan is for other, but the pull asked for logs"),
            ("r.pdf", [f("R.pdf")],
             "the server's plan is for R.pdf, but the pull asked for r.pdf"),
            ("", [put_dir(".ssh")],
             "the server's plan is for .ssh, a dot name that the pull's path doesn't spell out"),
            (".", [put_dir("..")],
             "the server's plan is for .., a dot name that the pull's path doesn't spell out"),
        ]
        for path, entries, message in cases:
            with self.subTest(path=path, entries=entries):
                self.assertEqual(self.refused(path, entries), message)


class EntryPointTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        patcher = mock.patch.dict(os.environ, {"VCHARON_HOME": tmp})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_vcharon(self, *args, cwd=None):
        return subprocess.run([sys.executable] + list(args), cwd=cwd, capture_output=True,
                              text=True, timeout=60, encoding="utf-8", check=False)

    def test_dash_m(self):
        # from a folder that isn't the repo, so the installed package is the one that runs; -P
        # as self_argv starts it
        for argv in (["-m", "vcharon"], ["-P", "-m", "vcharon"]):
            with self.subTest(argv=argv):
                result = self.run_vcharon(*argv + ["--version"], cwd=os.environ["VCHARON_HOME"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, VERSION + "\n")

    def test_a_usage_error_exits_3_not_2(self):
        # argparse's own code, 2, means busy here
        result = self.run_vcharon("-m", "vcharon", "nope", cwd=os.environ["VCHARON_HOME"])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertTrue(result.stderr.startswith("ERROR config: argument <command>: invalid "
                                                 "choice: "), result.stderr)


class BrokenPipeTest(FakeSshCase):
    """stdout's reader gone (vcharon doctor | head -3): exit 1, no ERROR line, and nothing
    from Python's own flush at exit."""

    def test_a_stdout_that_raises(self):
        class Gone(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(errno.EPIPE, "Broken pipe")

        err = io.StringIO()
        with mock.patch("sys.stdout", Gone()), mock.patch("sys.stderr", err):
            code = cli.main(["setup"])
        self.assertEqual((code, err.getvalue()), (1, ""))

    @unittest.skipIf(sys.platform == "win32", "a closed pipe's write isn't EPIPE on Windows")
    def test_a_closed_pipe(self):
        # the reader is gone before the first line: its write fails at once
        r, w = os.pipe()
        os.close(r)
        try:
            ran = subprocess.run([sys.executable, "-m", "vcharon", "doctor"], stdout=w,
                                 stderr=subprocess.PIPE, timeout=60, check=False)
        finally:
            os.close(w)
        self.assertEqual((ran.returncode, ran.stderr), (1, b""))

if __name__ == "__main__":
    unittest.main()
