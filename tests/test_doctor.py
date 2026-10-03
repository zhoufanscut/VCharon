"""vcharon doctor (decisions 16-22 of the M5 plan), through fake ssh and fake ssh-add.

Item 1 of DESIGN §14's M5: doctor reports each failure class of §6.4 (RowTest, one case per
row).
"""

from __future__ import annotations

import builtins
import json
import os
import re
import struct
import sys
import time
import unittest
from unittest import mock

import vcharon
from vcharon import bundle, doctor, keys, platform, plugins, ssh, state

from tests import util
from tests.test_plugin import probe_module
from tests.util import (TEST_MACHINE_ID, FakeSshCase, helper_override, read_tree,
                        start_failure_text, write_tree)

CONFIG = """
[push]
ssh       = fake-dest
from      = local:path
from.path = {src}
to        = remote:dir
to.path   = inbox

[pull]
ssh       = fake-dest
from      = remote:path
from.path = outbox
to        = local:dir
to.path   = {dst}
"""

NO_MACHINE = """
from vcharon import platform as _platform

_platform.machine_id = lambda *args, **kw: None
"""

DENIED = "fake@host: Permission denied (publickey)."
# What doctor tells you to do when no agent answers, for this machine's OS (doctor._agent's
# rule): start the Windows service, or a shell to start an agent in.
NO_AGENT_FIX = (keys.ADMIN_HINT if platform.os_name() == "windows"
                else 'only a key without a passphrase works without an agent; start one: %s'
                     % keys.START_AGENT)
LAST_OK = r"\AOK  nothing failed  \(\d+\.\d s\)\Z"
# a test job's state_mismatch hint (a channel section's names its sync commands)
RESET = "check the target; then reset the job's state, and sync it with --full"


def with_helper(code):
    """A patch under which doctor's session gets a helper_override(code) helper."""
    extra = helper_override(code)
    real = ssh.Session

    class Session(real):
        def __init__(s, *args, **kw):
            kw["extra_modules"] = extra
            real.__init__(s, *args, **kw)

    return mock.patch.object(ssh, "Session", Session)


class DoctorCase(FakeSshCase):
    def setUp(self):
        FakeSshCase.setUp(self)
        util.use_test_jobs(self)
        self.src = os.path.join(self.tmp, "local", "src")
        self.dst = os.path.join(self.tmp, "local", "dst")
        write_tree(self.src, {"a.txt": b"a", "d/b.txt": b"bb"})
        os.makedirs(self.dst)
        write_tree(self.home, {"inbox/": None, "outbox/x.log": b"x"})
        self.config = self.write_config(CONFIG.format(src=self.src, dst=self.dst))
        for name in ("SSH_AUTH_SOCK", "SSH_CONNECTION"):
            os.environ.pop(name, None)
        os.environ.update(FAKE_SSH_ADD_L_RC="0", FAKE_SSH_ADD_LIST="256 SHA256:x me (ED25519)",
                          FAKE_SSH_ADD_ARGV_LOG=os.path.join(self.tmp, "adds.log"))
        self.terminal = self.patch(keys, "terminal", return_value=False)
        self.input = self.patch(builtins, "input", side_effect=AssertionError("no prompt"))
        # This machine's own OS: with Ctx.resolve following the end's rules (DESIGN §9.1), a
        # client that pretended to be Linux while its jobs use C:\... paths can't be resolved.
        self.os_name(platform.os_name())

    def patch(self, obj, name, **kw):
        patcher = mock.patch.object(obj, name, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def os_name(self, osn):
        self.patch(platform, "os_name", return_value=osn)

    def doctor(self, *argv, code=None):
        got, out, err = self.run_cli("doctor", *argv)
        if code is not None:
            self.assertEqual(got, code, out + err)
        self.err = err
        return out.splitlines()

    def checks(self, lines):
        """[(level, subject, text)] of the check lines."""
        found = []
        for line in lines:
            m = re.match(r"\A  (ok  |warn|FAIL)  (\S+) +(.*)\Z", line)
            if m and not line.startswith("  " + " " * 4):
                found.append((m.group(1).strip(), m.group(2), m.group(3)))
        return found

    def subjects(self, lines):
        seen = []
        for level, subject, text in self.checks(lines):
            if subject not in seen:
                seen.append(subject)
        return seen

    def of(self, lines, subject):
        return [(level, text) for level, s, text in self.checks(lines) if s == subject]

    def adds(self):
        try:
            with open(os.environ["FAKE_SSH_ADD_ARGV_LOG"], encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


USED = "member folders are claimed with the client-id file %s (%s)"


class DoctorTest(DoctorCase):
    def test_everything_ok(self):
        lines = self.doctor(code=0)
        home = self.home
        self.assertEqual(lines[0], "vcharon: doctor")
        self.assertEqual(self.subjects(lines), ["vcharon", "python", "config", "box", "ssh",
                                                "agent", "dirs", "machine", "fake-dest", "push",
                                                "pull"])
        self.assertTrue(all(level == "ok" for level, s, t in self.checks(lines)), lines)
        # the version and how this box runs vcharon (the fix lines' spelling) come first
        self.assertEqual(lines.pop(1), "  ok    vcharon    0.1.0, protocol 3, reads channel "
                         "formats up to 1; runs as %s" % platform.self_command())
        # the subjects are padded to the longest one, fake-dest
        self.assertEqual(lines[1], "  ok    python     %s (%s) on %s"
                         % (platform.python_version(), sys.executable, platform.os_name()))
        self.assertEqual(lines[2], "  ok    config     %s: 2 jobs" % self.config)
        self.assertEqual(lines.pop(3), "  ok    box        %s (default, from the OS)"
                         % platform.os_word())
        self.assertEqual(lines[3], "  ok    ssh        OpenSSH_fake 1.0, for vcharon's tests "
                                   "(%s)" % platform.default_ssh_path())
        self.assertEqual(lines[4], "  ok    agent      holds 1 key")
        self.assertEqual(lines[5], "  ok    dirs       state %s, logs %s"
                         % (os.path.join(self.vcharon_home, "state"),
                            os.path.join(self.vcharon_home, "logs")))
        self.assertEqual(lines[6], "  ok    machine    %s" % TEST_MACHINE_ID)
        self.assertEqual(lines.pop(7), "  ok    machine    " + USED % (
            os.path.join(self.vcharon_home, "state", "client-id"),
            "made at the first join, from the machine id"))
        self.assertEqual(lines[7], "  ok    fake-dest  logs in")
        self.assertRegex(lines[8], r"\A  ok    fake-dest  Python \d+\.\d+\.\d+ on .+, user .*; "
                         r"handshake \d+\.\d\d s\Z")
        # M12b: the fake server is this machine, so its clock and zone are ours
        self.assertRegex(lines[9], r"\A  ok    fake-dest  clock [+-]\d\.\d s from this "
                         r"machine\Z")
        self.assertRegex(lines[10], r"\A  ok    fake-dest  echo 4 MiB byte-identical in "
                         r"\d+\.\d\d s\Z")
        # from.path is shown made absolute, not resolved; the dir sink's to.path as its check
        # resolved it (on macOS the temp dir is under /private)
        self.assertEqual(lines[11:17], [
            "  ok    push       state: none yet; the first run sends everything",
            "  ok    push       from.path %s: a directory" % self.src,
            "  ok    push       to.path %s: a directory you can write"
            % os.path.realpath(os.path.join(home, "inbox")),
            "  ok    pull       state: none yet; the first run sends everything",
            "  ok    pull       from.path %s: a directory" % os.path.join(home, "outbox"),
            "  ok    pull       to.path %s: a directory you can write"
            % os.path.realpath(self.dst)])
        self.assertRegex(lines[17], LAST_OK)
        self.assertEqual(len(lines), 18)

    def test_json(self):
        prose = self.doctor(code=0)
        got, out, err = self.run_cli("doctor", "--json")
        self.assertEqual((got, err), (0, ""))
        [line] = out.splitlines()
        doc = json.loads(line)
        self.assertEqual(sorted(doc), ["box", "box_source", "checks", "claimer_source",
                                       "command", "executable", "failed", "format", "ok", "os",
                                       "protocol", "python", "servers", "version",
                                       "warnings"])
        self.assertEqual(doc["format"], 1)
        self.assertEqual(doc["servers"], [{
            "server": "fake-dest", "python": platform.python_version(), "os": "linux",
            "distro": "Debian GNU/Linux 13 (trixie)", "distro_id": "debian",
            "distro_version": "13", "tested": True}])
        self.assertEqual((doc["box"], doc["box_source"], doc["claimer_source"]),
                         (platform.os_word(), "os", "client-id file (from the machine id)"))
        self.assertEqual((doc["version"], doc["protocol"], doc["ok"], doc["failed"],
                          doc["warnings"]), (vcharon.VERSION, vcharon.PROTOCOL, True, 0, 0))
        self.assertEqual(doc["command"], platform.self_command())
        self.assertEqual(doc["python"], platform.python_version())
        self.assertEqual(doc["executable"], sys.executable)
        self.assertEqual(doc["os"], platform.os_name())
        for check in doc["checks"]:
            self.assertEqual(sorted(check), ["fix", "level", "note", "subject", "text"])
        # the same checks as the lines, less what the time and the echo change
        self.assertEqual([(c["level"], c["subject"]) for c in doc["checks"]],
                         [(level, subject) for level, subject, text in self.checks(prose)])
        # a FAIL's fix, as this box runs vcharon
        self.write_config(CONFIG.format(src=self.src + "-gone", dst=self.dst))
        got, out, err = self.run_cli("doctor", "--json")
        doc = json.loads(out)
        self.assertEqual((got, doc["ok"], doc["failed"]), (1, False, 1))
        [fail] = [c for c in doc["checks"] if c["level"] == "FAIL"]
        self.assertEqual((fail["subject"], fail["fix"], fail["note"]),
                         ("push", "check the path", None))
        with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"), encoding="utf-8") as f:
            text = f.read()
        # every line is logged too
        self.assertIn("  ok    fake-dest  logs in", text)
        self.assertIn("OK  nothing failed", text)

    def test_read_only(self):
        # a saved state, so its file is there to compare
        self.assertEqual(self.run_jobs("push")[0], 0)
        trees = {p: read_tree(p) for p in (self.home, self.src, self.dst)}
        with open(state.path("push"), "rb") as f:
            saved = f.read()
        self.doctor(code=0)
        for p, tree in trees.items():
            self.assertEqual(read_tree(p), tree, p)
        with open(state.path("push"), "rb") as f:
            self.assertEqual(f.read(), saved)
        self.assertFalse([p for p in read_tree(self.tmp) if ".vcharon-stage-" in p])
        self.assertFalse([p for p in read_tree(self.tmp) if ".vcharon-doctor-" in p])

    def test_fix_and_note_lines_start_under_the_text(self):
        self.write_config("")
        os.environ["FAKE_SSH_ADD_L_RC"] = "2"
        lines = self.doctor(code=0)
        at = lines.index("  warn  agent    none: %s" % keys.no_agent_why())
        self.assertEqual(lines[at + 1], "                 fix: " + NO_AGENT_FIX)
        self.assertEqual(lines[3], "  ok    config   %s: 0 jobs" % self.config)
        self.assertEqual(lines[4], "                 note: no channels joined over ssh; to check "
                                   "a server: vcharon doctor --server ALIAS")
        self.assertRegex(lines[-1], r"\AOK  nothing failed, 1 warning  \(\d+\.\d s\)\Z")

    # the client

    def test_agent(self):
        self.write_config("")
        os.environ["FAKE_SSH_ADD_LIST"] = "256 SHA256:x a (ED25519)\n3072 SHA256:y b (RSA)\n"
        self.assertEqual(self.of(self.doctor(code=0), "agent"), [("ok", "holds 2 keys")])
        os.environ["FAKE_SSH_ADD_L_RC"] = "1"
        lines = self.doctor("--server", "fake-dest", code=0)
        self.assertEqual(self.of(lines, "agent"), [("warn", "holds no keys")])
        self.assertIn("fix: " + platform.runnable("a key with a passphrase works only once "
                                                  "it's in the agent: run vcharon key fake-dest"),
                      lines[lines.index("  warn  agent      holds no keys") + 1])
        os.environ["FAKE_SSH_ADD_L_RC"] = "2"
        lines = self.doctor(code=0)
        self.assertEqual(self.of(lines, "agent"),
                         [("warn", "none: %s" % keys.no_agent_why())])
        sock = os.path.join(self.tmp, "gone.sock")
        os.environ["SSH_AUTH_SOCK"] = sock
        lines = self.doctor(code=0)
        # on Windows the socket isn't what answers: the agent is a service (keys.no_agent_why)
        self.assertEqual(self.of(lines, "agent"),
                         [("warn", "none: %s" % keys.no_agent_why())])
        if platform.os_name() != "windows":
            self.assertIn("fix: a forwarded agent ends with its ssh login; log in again",
                          lines[lines.index("  warn  agent    none: nothing answers at %s" % sock)
                                + 1])
        self.os_name("windows")
        lines = self.doctor(code=0)
        self.assertEqual(self.of(lines, "agent"),
                         [("warn", "none: the ssh-agent service isn't running")])
        self.assertIn("Start-Service ssh-agent", "\n".join(lines))

    def test_ssh_add_cant_start(self):
        # review W3: not "none", which would send you after an agent
        self.write_config("")
        missing = os.path.join(self.tmp, "no-such-ssh-add")
        self.patch(ssh, "ssh_add_prefix", side_effect=lambda settings: [missing])
        lines = self.doctor(code=0)
        why = "couldn't start %s: %s" % (missing, start_failure_text(missing))
        self.assertEqual(self.of(lines, "agent"), [("warn", why)])
        self.assertEqual(lines[lines.index("  warn  agent    " + why) + 1],
                         "                 fix: install the OpenSSH client, or set ssh_path in "
                         "vcharon.ini")

    def test_python_on_windows(self):
        self.write_config("")
        self.os_name("windows")
        self.patch(platform, "is_wow64", return_value=True)
        lines = self.doctor(code=0)
        # the floor (3.11) covers every OS: no warning of its own on Windows
        self.assertEqual([level for level, text in self.of(lines, "python")], ["ok", "warn"])
        self.assertIn("  warn  python   32-bit Python on 64-bit Windows", lines)

    def test_dirs_not_writable(self):
        self.write_config("")
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as f:
            f.write("a file where a directory should be")
        os.environ["VCHARON_HOME"] = blocker
        got, out, err = self.run_cli("doctor")
        self.assertEqual(got, 1)
        dirs = [line for line in out.splitlines() if line.startswith("  FAIL  dirs")]
        self.assertEqual(len(dirs), 2, out)
        self.assertTrue(dirs[0].startswith("  FAIL  dirs     can't write in %s: "
                                           % os.path.join(blocker, "state")), dirs)

    def test_machine(self):
        # M11d: this machine's id, with or without a destination in scope
        for argv in ((), ("--server", "fake-dest")):
            with self.subTest(argv=argv):
                lines = self.doctor(*argv, code=0)
                self.assertEqual(self.of(lines, "machine"), [
                    ("ok", TEST_MACHINE_ID),
                    ("ok", USED % (platform.client_id_path(),
                                   "made at the first join, from the machine id"))])
        os.environ["VCHARON_TEST_MACHINE_ID"] = "f" * 32
        self.assertEqual(self.of(self.doctor(code=0), "machine")[0], ("ok", "f" * 32))

    def test_no_machine_id(self):
        # only a warn on Linux: a client of remote jobs needs no id of its own, and claims with
        # a random client id; a Mac or Windows box always has one, so there no client id can
        # be made either (a FAIL)
        self.write_config("")
        self.patch(platform, "machine_id", return_value=None)
        text = ("no machine id: this machine can't hold a channel (--local), nor keep jobs' "
                "state as a server")
        for osn, fix in (("linux", "as root, run systemd-machine-id-setup: it writes "
                                   "/etc/machine-id"),
                         ("darwin", "check that /usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice "
                                    "prints an IOPlatformUUID line, then try again; if it "
                                    "prints none, ask your user"),
                         ("windows", "check that reg query HKLM\\SOFTWARE\\Microsoft\\"
                                     "Cryptography /v MachineGuid /reg:64 prints a MachineGuid, "
                                     "then try again; if it prints none, ask your user")):
            for argv in ((), ("--server", "fake-dest")):
                with self.subTest(osn=osn, argv=argv):
                    self.os_name(osn)
                    linux = osn == "linux"
                    lines = self.doctor(*argv, code=0 if linux else 1)
                    path = platform.client_id_path()
                    if linux:
                        claim = ("ok", USED % (path, "made at the first join, random"))
                    else:
                        claim = ("FAIL", "couldn't read this machine's id (%s), so no client id "
                                 "is made" % ("ioreg's IOPlatformUUID" if osn == "darwin"
                                              else "the registry's MachineGuid"))
                    self.assertEqual(self.of(lines, "machine"), [("warn", text), claim])
                    at = [i for i, line in enumerate(lines) if line.startswith("  warn  machine")]
                    self.assertEqual(len(at), 1, lines)
                    self.assertEqual(lines[at[0] + 1].strip(), "fix: " + fix)
                    if linux:
                        self.assertRegex(lines[-1], r"\AOK  nothing failed, \d warnings?  ")
                    else:
                        doc = json.loads(self.run_cli("doctor", "--json", *argv)[1])
                        self.assertIsNone(doc["claimer_source"])

    def test_box_and_the_client_id_file(self):
        # the box's source, and a client-id file: made by the first join, never by doctor
        self.write_config("[vcharon]\nbox = laptop\n")
        # a Linux box without a machine id: a random client id
        self.os_name("linux")
        self.patch(platform, "machine_id", return_value=None)
        doc = json.loads(self.run_cli("doctor", "--json")[1])
        self.assertEqual((doc["box"], doc["box_source"], doc["claimer_source"]),
                         ("laptop", "config", "client-id file (random)"))
        lines = self.doctor(code=0)
        # the path as the declared OS joins it: on a Windows host, Linux's / before the name
        self.assertEqual(self.of(lines, "box"),
                         [("ok", "laptop (set in %s)" % platform.config_path())])
        path = platform.client_id_path()
        self.assertFalse(os.path.exists(path))
        cid, origin = platform.client_id()
        self.assertEqual(origin, "random")
        self.assertEqual(self.of(self.doctor(code=0), "machine")[1],
                         ("ok", USED % (path, "random")))
        with open(path, "w") as f:
            f.write("not an id\n")
        self.assertEqual(self.of(self.doctor(code=1), "machine")[1],
                         ("FAIL", "%s isn't a client id (32 hex digits)" % path))
        doc = json.loads(self.run_cli("doctor", "--json")[1])
        self.assertIsNone(doc["claimer_source"])

    # a destination

    def test_agent_only_key(self):
        os.environ["FAKE_SSH_STDERR"] = ('debug1: Server accepts key: "c@h" RSA SHA256:x agent')
        lines = self.doctor("--server", "fake-dest", code=0)
        at = lines.index('  ok    fake-dest  logs in with "c@h" (RSA), from the agent')
        self.assertEqual(lines[at + 1], "                   note: only the agent holds this "
                                        "key: runs work while it does")
        os.environ["SSH_CONNECTION"] = "192.0.2.1 5000 192.0.2.2 22"
        lines = self.doctor("--server", "fake-dest", code=0)
        note = "                   note: only the agent holds this key: runs work while it does"
        if platform.os_name() == "linux":
            # doctor says this for a Linux client only: elsewhere there is no forwarded agent
            note += "; a forwarded agent, only while this ssh login is open"
        self.assertEqual(lines[at + 1], note)
        self.os_name("darwin")
        lines = self.doctor("--server", "fake-dest", code=0)
        self.assertNotIn("forwarded", lines[at + 1])

    def test_junk_and_no_machine_id(self):
        os.environ["FAKE_SSH_JUNK_B64"] = "aGVsbG8K"
        with with_helper(NO_MACHINE):
            lines = self.doctor(code=1)
        self.assertIn(("warn", "the server's shell printed 6 bytes before vcharon started"),
                      self.of(lines, "fake-dest"))
        self.assertIn(("warn", "no machine id: jobs can't keep state there"),
                      self.of(lines, "fake-dest"))
        for job in ("push", "pull"):
            self.assertIn(("FAIL", "the server has no machine id, so %s can't keep state there"
                           % job), self.of(lines, job))
        self.assertRegex(lines[-1], r"\AFAIL  2 failed, 2 warnings  \(\d+\.\d s\)\Z")

    def test_a_server_that_isnt_linux(self):
        # M11a: a FAIL for the destination and for each job on it, not the no-machine warning
        os.environ["VCHARON_TEST_OS"] = "darwin"
        lines = self.doctor(code=1)
        refused = "fake-dest runs darwin: only a Linux server is supported as a remote end"
        self.assertIn(("FAIL", refused), self.of(lines, "fake-dest"))
        self.assertNotIn("no machine id", "\n".join(lines))
        for job in ("push", "pull"):
            self.assertIn(("FAIL", refused), self.of(lines, job))
        self.assertIn("fix: point fake-dest at a Linux server", [l.strip() for l in lines])

    # a job

    def test_missing_local_source(self):
        self.write_config(CONFIG.format(src=self.src + "-gone", dst=self.dst))
        lines = self.doctor(code=1)
        self.assertIn(("FAIL", "from.path %s-gone doesn't exist" % self.src),
                      self.of(lines, "push"))
        self.assertIn("fix: check the path", "\n".join(lines))

    def test_remote_sink_root(self):
        text = CONFIG.format(src=self.src, dst=self.dst).replace("to.path   = inbox",
                                                                  "to.path   = new/inbox")
        self.write_config(text)
        lines = self.doctor(code=1)
        missing = os.path.join(self.home, "new", "inbox")
        self.assertIn(("FAIL", "the root %s doesn't exist" % missing), self.of(lines, "push"))
        self.write_config(text.replace("to.path   = new/inbox",
                                       "to.path   = new/inbox\nto.create = yes"))
        lines = self.doctor(code=0)
        # the FAIL above is the check's error, which names the root as given; this line shows
        # the root the check resolved (on macOS the temp dir is under /private)
        missing = os.path.join(os.path.realpath(self.home), "new", "inbox")
        self.assertIn(("ok", "to.path %s doesn't exist yet; the first run creates it" % missing),
                      self.of(lines, "push"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "new")))

    def test_bad_option(self):
        text = CONFIG.format(src=self.src, dst=self.dst).replace("to.path   = inbox",
                                                                  "to.path   = inbox\nto.nope = 1")
        self.write_config(text)
        lines = self.doctor(code=1)
        push = self.of(lines, "push")
        self.assertIn(("FAIL", "to.nope: unknown option"), push)
        # that side's doctor is skipped; the other side's runs
        self.assertFalse(any(t.startswith("to.path") for level, t in push), push)
        self.assertTrue(any(t.startswith("from.path") for level, t in push), push)

    def test_missing_capability(self):
        module = probe_module("desk", caps=("desktop",))
        for patcher in (mock.patch.object(plugins, "ALL", plugins.ALL + ("desk",)),
                        mock.patch.dict(sys.modules, {module.__name__: module})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.patch(platform, "caps", return_value={"desktop": False})
        self.write_config(CONFIG.format(src=self.src, dst=self.dst).replace(
            "from      = local:path", "from      = local:desk", 1))
        lines = self.doctor(code=1)
        self.assertIn(("FAIL", "desk needs desktop, which this client lacks"),
                      self.of(lines, "push"))

    def test_state(self):
        os.makedirs(os.path.dirname(state.path("push")))
        with open(state.path("push"), "w") as f:
            f.write("{nope")
        lines = self.doctor(code=1)
        [(level, text)] = [c for c in self.of(lines, "push") if "state" in c[1]]
        self.assertEqual(level, "FAIL")
        self.assertTrue(text.startswith("the state file %s can't be read: " % state.path("push")))
        self.assertIn("fix: " + RESET, "\n".join(lines))
        os.remove(state.path("push"))
        self.assertEqual(self.run_jobs("push")[0], 0)
        lines = self.doctor(code=0)
        saved = state.load("push").saved
        self.assertIn(("ok", "state saved %s, 2 files, 1 dir sent" % saved),
                      self.of(lines, "push"))
        self.write_config(CONFIG.format(src=self.src, dst=self.dst).replace(
            "to.path   = inbox", "to.path   = inbox2"))
        os.mkdir(os.path.join(self.home, "inbox2"))
        lines = self.doctor(code=1)
        self.assertIn(("FAIL", "the state of push was saved for another config: its ssh, from, "
                       "to, from.path or to.path changed"), self.of(lines, "push"))

    # scope

    def test_one_destination(self):
        lines = self.doctor("--server", "fake-dest", code=0)
        self.assertEqual(self.subjects(lines), ["vcharon", "python", "config", "box", "ssh",
                                                "agent", "dirs", "machine", "fake-dest"])

    def test_bad_destination(self):
        got, out, err = self.run_cli("doctor", "--server=-x")
        self.assertEqual(got, 3)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("ERROR config: the destination '-x' starts with '-'"))

    def test_no_jobs(self):
        self.write_config("[vcharon]\n")
        lines = self.doctor(code=0)
        self.assertEqual(self.subjects(lines), ["vcharon", "python", "config", "box", "ssh",
                                                "agent", "dirs", "machine"])

    def test_broken_config(self):
        self.write_config("[vcharon]\ncompress = maybe\n")
        lines = self.doctor(code=1)
        self.assertEqual(self.of(lines, "config"),
                         [("FAIL", "vcharon.ini [vcharon] compress: must be yes or no")])
        self.assertEqual(self.subjects(lines), ["vcharon", "python", "config", "ssh",
                                                "agent", "dirs", "machine"])
        lines = self.doctor("--server", "fake-dest", code=1)
        self.assertEqual([level for level, text in self.of(lines, "fake-dest")],
                         ["ok", "ok", "ok", "ok"])

    # a locked key: the fix line says to unlock it; the doctor never prompts (it offered to,
    # in a terminal, before the command line said never prompt)

    def test_a_locked_key_is_a_fix_line_not_a_prompt(self):
        key = os.path.join(self.tmp, "id_rsa")
        with open(key, "w") as f:
            f.write("k")
        os.environ.update(FAKE_SSH_STDERR="debug1: Server accepts key: %s RSA SHA256:x\n%s"
                          % (key, DENIED), FAKE_SSH_EXIT="255",
                          FAKE_SSH_UNLOCK_FILE=os.path.join(self.tmp, "unlocked"),
                          SSH_AUTH_SOCK=os.path.join(self.tmp, "agent.sock"))
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                self.terminal.return_value = terminal
                lines = self.doctor("--server", "fake-dest", code=1)
                self.input.assert_not_called()
                self.assertEqual(self.adds(), [])
                self.assertIn("fix: " + platform.runnable("vcharon key fake-dest"),
                              [l.strip() for l in lines])


class ClockTest(DoctorCase):
    """M12b: the server's clock and time zone against this machine's, after the handshake
    line; the fake server's hello moved by VCHARON_TEST_CLOCK_SHIFT and VCHARON_TEST_UTC_OFFSET."""

    def clock(self, shift, code):
        os.environ["VCHARON_TEST_CLOCK_SHIFT"] = str(shift)
        lines = self.doctor("--server", "fake-dest", code=code)
        dest = self.of(lines, "fake-dest")
        self.assertTrue(dest[1][0] in ("ok", "warn") and "handshake" in dest[1][1], lines)
        level, text = dest[2]
        m = re.match(r"\Aclock ([+-]\d+\.\d) s from this machine\Z", text)
        self.assertIsNotNone(m, lines)
        # the hello's one-way delay comes off the gap; a busy machine may make it large
        self.assertLess(abs(float(m.group(1)) - shift), 5, lines)
        return lines, level

    def test_ok_under_30_s(self):
        lines, level = self.clock(5, 0)
        self.assertEqual(level, "ok")
        self.assertNotIn("NTP", "\n".join(lines))
        self.assertRegex(lines[-1], LAST_OK)

    def test_warn_ahead_and_behind(self):
        for shift in (45, -45):
            with self.subTest(shift=shift):
                lines, level = self.clock(shift, 0)
                self.assertEqual(level, "warn")
                at = [i for i, line in enumerate(lines) if "  clock " in line][0]
                self.assertEqual(lines[at + 1].strip(), "fix: " + doctor.CLOCK_HINT)
                sign = "+" if shift > 0 else "-"
                self.assertIn("clock " + sign, lines[at])
                self.assertRegex(lines[-1], r"\AOK  nothing failed, 1 warning  ")
                # the gap is in the session's log line too
                with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"),
                          encoding="utf-8") as f:
                    self.assertRegex(f.read(), r"hello from fake-dest, clock \%s\d+\.\d s:"
                                     % sign)

    def test_other_time_zone(self):
        local = time.localtime().tm_gmtoff
        os.environ["VCHARON_TEST_UTC_OFFSET"] = str(local + 3600)
        lines = self.doctor("--server", "fake-dest", code=0)
        text = "time zone %s, this machine %s" % (doctor.utc_text(local + 3600),
                                                  doctor.utc_text(local))
        self.assertIn(("warn", text), self.of(lines, "fake-dest"))
        at = lines.index("  warn  fake-dest  " + text)
        self.assertEqual(lines[at + 1].strip(), "fix: " + doctor.ZONE_HINT)

    def test_no_clock_line_without_a_destination(self):
        self.write_config("")
        lines = self.doctor(code=0)
        self.assertNotIn("clock", "\n".join(lines))

    def test_formatter(self):
        hello = {"time": 1000.0, "utc_offset": 28800}
        self.assertEqual(doctor.clock_checks(hello, 995.0, 28800),
                         [("ok", "clock +5.0 s from this machine", None)])
        self.assertEqual(doctor.clock_checks(hello, 1029.94, 28800),
                         [("ok", "clock -29.9 s from this machine", None)])
        self.assertEqual(doctor.clock_checks(hello, 1029.96, 28800),
                         [("warn", "clock -30.0 s from this machine", doctor.CLOCK_HINT)])
        self.assertEqual(doctor.clock_checks(hello, 1030.0, 28800),
                         [("warn", "clock -30.0 s from this machine", doctor.CLOCK_HINT)])
        self.assertEqual(doctor.clock_checks(hello, 955.0, 0),
                         [("warn", "clock +45.0 s from this machine", doctor.CLOCK_HINT),
                          ("warn", "time zone UTC+08:00, this machine UTC+00:00",
                           doctor.ZONE_HINT)])
        self.assertEqual(doctor.clock_checks({"utc_offset": -12600}, 0.0, 19800),
                         [("warn", "time zone UTC-03:30, this machine UTC+05:30",
                           doctor.ZONE_HINT)])
        # an older hello, or a field of the wrong type: no line
        self.assertEqual(doctor.clock_checks({}, 0.0, 0), [])
        self.assertEqual(doctor.clock_checks({"time": True, "utc_offset": "8"}, 0.0, 0), [])


class DistroTest(DoctorCase):
    """The server's distro from its hello: Debian 13 or later is ok, anything else a warning,
    never a failure; the fake server's /etc/os-release is VCHARON_TEST_OS_RELEASE's file."""

    def release(self, data):
        if data is None:
            os.environ["VCHARON_TEST_OS_RELEASE"] = os.path.join(self.tmp, "missing")
            return
        with open(self.os_release, "wb") as f:
            f.write(data)

    def test_each_case(self):
        cases = [
            (util.DEBIAN_13, None, ("debian", "13")),
            (b'ID=debian\nVERSION_ID="14"\nPRETTY_NAME="Debian GNU/Linux 14 (forky)"\n', None,
             ("debian", "14")),
            (b'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n',
             "VCharon is tested on Debian 13 or later; it is ID=debian VERSION_ID=12 (Debian "
             "GNU/Linux 12 (bookworm))", ("debian", "12")),
            (b'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'
             b'PRETTY_NAME="Ubuntu 24.04.1 LTS"\n',
             "VCharon is tested on Debian 13 or later; it is ID=ubuntu VERSION_ID=24.04 "
             "(Ubuntu 24.04.1 LTS)", ("ubuntu", "24.04")),
            (None, "VCharon is tested on Debian 13 or later; its os-release names no distro "
                   "(no ID=)", (None, None)),
            (b"\x00\xff garbage\nno equals here\n=\n", "VCharon is tested on Debian 13 or "
             "later; its os-release names no distro (no ID=)", (None, None)),
            (b"ID=debian\nVERSION_ID=thirteen\n", "VCharon is tested on Debian 13 or later; it "
             "is ID=debian VERSION_ID=thirteen", ("debian", "thirteen"))]
        for data, warning, fields in cases:
            with self.subTest(data=data):
                self.release(data)
                lines = self.doctor("--server", "fake-dest", code=0)
                dest = self.of(lines, "fake-dest")
                warns = [text for level, text in dest if level == "warn"]
                self.assertEqual(warns, [warning] if warning else [], lines)
                self.assertRegex(lines[-1], r"\AOK  nothing failed" + (", 1 warning  "
                                                                       if warning else "  "))
                doc = json.loads(self.run_cli("doctor", "--server", "fake-dest", "--json")[1])
                [server] = doc["servers"]
                self.assertEqual((server["distro_id"], server["distro_version"],
                                  server["tested"]), fields + (warning is None,))
                # ping says it too, as a warning line
                code, out, err = self.run_cli("ping", "fake-dest")
                self.assertEqual(code, 0, err)
                self.assertEqual([l for l in out.splitlines() if l.startswith("  warn ")],
                                 ["  warn     " + warning] if warning else [])
                if data is None:
                    os.environ["VCHARON_TEST_OS_RELEASE"] = self.os_release


class RowTest(DoctorCase):
    """Item 1: one case per row of DESIGN §6.4, each a FAIL on the fake-dest line with its
    message and fix, exit 1, and the destination's later checks skipped."""

    def failed(self, message, fix, *argv):
        lines = self.doctor(*argv, code=1)
        dest = self.of(lines, "fake-dest")
        self.assertEqual(dest[-1], ("FAIL", message), lines)
        at = lines.index("  FAIL  fake-dest  " + message)
        # a command in it as this box runs vcharon (M14a)
        self.assertEqual(lines[at + 1], "                   fix: " + platform.runnable(fix))
        # no session, no echo, no remote side of the jobs
        self.assertFalse(any("echo" in text for level, text in dest), lines)
        self.assertNotIn(("ok", "to.path %s: a directory you can write"
                          % os.path.join(self.home, "inbox")), self.of(lines, "push"))
        self.assertNotIn(("ok", "from.path %s: a directory" % os.path.join(self.home, "outbox")),
                         self.of(lines, "pull"))
        # the local sides are still checked
        self.assertIn(("ok", "from.path %s: a directory" % self.src), self.of(lines, "push"))
        return lines

    def exit(self, rc, stderr=None):
        os.environ["FAKE_SSH_EXIT"] = str(rc)
        if stderr is not None:
            os.environ["FAKE_SSH_STDERR"] = stderr

    def test_host_key(self):
        self.exit(255, "Host key verification failed.")
        self.failed("ssh couldn't verify the host key of fake-dest",
                    "run ssh fake-dest once in a terminal")

    def test_no_usable_key(self):
        self.exit(255, DENIED)
        self.failed("the server fake-dest accepts none of your keys (Permission denied)",
                    "add your public key to ~/.ssh/authorized_keys on fake-dest")

    def test_locked_key(self):
        key = os.path.join(self.tmp, "id_ed25519")
        with open(key, "w") as f:
            f.write("k")
        self.exit(255, "debug1: Server accepts key: %s ED25519 SHA256:x\n%s" % (key, DENIED))
        self.failed("ssh can't use your key %s: the server accepts it, but it's locked by a "
                    "passphrase" % key, "vcharon key fake-dest")

    def test_network(self):
        self.exit(255, "ssh: connect to host h port 22: Connection refused")
        self.failed("ssh couldn't reach fake-dest (Connection refused)",
                    "check the host, port, VPN")

    def test_no_python(self):
        self.exit(127)
        self.failed("python3 wasn't found on fake-dest",
                    "install python3 on fake-dest, or set remote_python in vcharon.ini")

    def test_not_runnable(self):
        self.exit(126)
        self.failed("python3 isn't runnable on fake-dest", "check remote_python in vcharon.ini")

    def test_not_runnable_with_an_accepted_key(self):
        # review B1: bash exits 126 with its own "Permission denied"; that's no locked key
        key = os.path.join(self.tmp, "id_ed25519")
        with open(key, "w") as f:
            f.write("k")
        self.terminal.return_value = True
        self.exit(126, "debug1: Server accepts key: %s ED25519 SHA256:x\n"
                       "bash: line 1: /usr/bin/python3: Permission denied" % key)
        self.failed("python3 isn't runnable on fake-dest", "check remote_python in vcharon.ini")
        self.input.assert_not_called()

    def test_python_too_old(self):
        real = bundle.loader_line
        self.patch(bundle, "loader_line", side_effect=lambda floor=vcharon.FLOOR: real((99, 0)))
        major, minor = sys.version_info[:2]
        self.failed("the server's python3 is %d.%d; vcharon needs 3.11 or later" % (major, minor),
                    "install python3 3.11 or later on fake-dest (Debian 13's is 3.13), or point "
                    "remote_python in vcharon.ini at one")

    def test_code_didnt_load(self):
        self.patch(bundle, "build", return_value=struct.pack(">Q", 3) + b"abc")
        self.failed("the server couldn't load vcharon's code",
                    "this is a bug in vcharon; see the log")

    def test_other_exit(self):
        self.exit(1)
        self.failed("ssh exited with code 1 before vcharon started on the server",
                    "a shell startup file on the server may have read stdin; see the log")

    def test_stdin_eaten(self):
        os.environ["FAKE_SSH_EAT_STDIN"] = "100"
        lines = self.failed("ssh exited with code 1 before vcharon started on the server",
                            "a shell startup file on the server may have read stdin; see "
                            "the log")
        # the probe sends nothing on stdin, so it logged in; the session failed
        self.assertEqual(self.of(lines, "fake-dest")[0], ("ok", "logs in"))

    def test_no_marker_in_time(self):
        os.environ["FAKE_SSH_STALL"] = "5"
        with open(self.config, "a") as f:
            f.write("\n[vcharon]\nhandshake_timeout = 1\n")
        started = time.monotonic()
        self.failed("no answer from vcharon on fake-dest within 1 s", "authentication or a jump "
                    "host may be stuck; run ssh fake-dest in a terminal to see")
        self.assertLess(time.monotonic() - started, 4)

    def test_ssh_cant_start(self):
        missing = os.path.join(self.tmp, "no-such-ssh")
        self.patch(ssh, "ssh_prefix", side_effect=lambda settings: [missing])
        why = "couldn't start %s: %s" % (missing, start_failure_text(missing))
        lines = self.failed(why, "install the OpenSSH client, or set ssh_path in vcharon.ini")
        self.assertEqual(self.of(lines, "ssh"), [("FAIL", why)])


if __name__ == "__main__":
    unittest.main()
