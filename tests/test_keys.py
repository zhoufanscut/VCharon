"""vcharon key (decisions 8-15 of the M5 plan), through fake ssh and fake ssh-add."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from vcharon import keys, platform

from tests.util import FAKE_SSH, FAKE_SSH_ADD, FakeSshCase, start_failure_text

FP = "SHA256:abcDEF0123456789"
DENIED = "fake@host: Permission denied (publickey)."
ADMIN = ("once, in an admin PowerShell: Get-Service ssh-agent | Set-Service -StartupType "
         "Automatic; Start-Service ssh-agent")
EVAL = 'start one in this shell: eval "$(ssh-agent -s)", then run vcharon key again'
LINUX_NOTE = ("  note    an agent forwarded by ssh -A or ForwardAgent lasts only while the ssh "
              "login that brought it is open; for cron, use a key without a passphrase on this "
              "machine (see vcharon/README.md)")


def accepts(ident, *flags):
    return "debug1: Server accepts key: %s RSA %s%s" % (ident, FP, "".join(" " + f for f in flags))


class KeyCase(FakeSshCase):
    def setUp(self):
        FakeSshCase.setUp(self)
        self.ssh_log = os.path.join(self.tmp, "ssh-argv.log")
        self.add_log = os.path.join(self.tmp, "ssh-add-argv.log")
        self.unlock = os.path.join(self.tmp, "unlocked")
        self.key = os.path.join(self.tmp, "id_rsa")
        with open(self.key, "w") as f:
            f.write("not really a key\n")
        os.environ.update(FAKE_SSH_ARGV_LOG=self.ssh_log, FAKE_SSH_ADD_ARGV_LOG=self.add_log)
        for name in ("SSH_AUTH_SOCK", "SSH_CONNECTION"):
            os.environ.pop(name, None)
        self.terminal = self.patch(keys, "terminal", return_value=True)
        self.os_name("linux")

    def patch(self, obj, name, **kw):
        patcher = mock.patch.object(obj, name, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def os_name(self, osn):
        patcher = mock.patch.object(platform, "os_name", return_value=osn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def agent(self, rc, listing=""):
        os.environ["FAKE_SSH_ADD_L_RC"] = str(rc)
        os.environ["FAKE_SSH_ADD_LIST"] = listing

    def locked(self, key=None):
        """ssh -v names a file key the server accepts, then fails: until the add creates the
        unlock file."""
        os.environ.update(FAKE_SSH_STDERR=accepts(key or self.key) + "\n" + DENIED,
                          FAKE_SSH_EXIT="255", FAKE_SSH_UNLOCK_FILE=self.unlock)

    def logs_in(self, line):
        os.environ["FAKE_SSH_STDERR"] = line

    def adds(self):
        """The argv of every ssh-add call but -l."""
        return self.json_lines(self.add_log)

    def ssh_runs(self):
        return self.json_lines(self.ssh_log)

    def json_lines(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def key_cli(self, *argv, code=0):
        got, out, err = self.run_cli("key", *argv)
        self.assertEqual(got, code, out + err)
        return out.splitlines(), err.splitlines()


class KeyTest(KeyCase):
    def test_no_terminal(self):
        self.terminal.return_value = False
        out, err = self.key_cli("fake-dest", code=3)
        self.assertEqual(out, [])
        self.assertEqual(err[:2], ["ERROR config: vcharon key needs a terminal: ssh-add asks for "
                                   "your passphrase there", "  fix: run it in a terminal window"])
        self.assertEqual((self.ssh_runs(), self.adds()), ([], []))

    def test_usage(self):
        _out, err = self.key_cli(code=3)
        self.assertEqual(err[:2], ["ERROR config: give a destination, or --key FILE",
                                   "  fix: " + platform.runnable(keys.KEY_EXAMPLE)])
        missing = os.path.join(self.tmp, "nope")
        _out, err = self.key_cli("--key", missing, code=3)
        self.assertEqual(err[:2], ["ERROR config: %s isn't a file" % missing,
                                   "  fix: check the path"])
        _out, err = self.key_cli("--key", self.tmp, "fake-dest", code=3)
        self.key_cli("--", "-oProxyCommand=x", code=3)
        self.assertEqual((self.ssh_runs(), self.adds()), ([], []))

    def test_locked_macos(self):
        self.os_name("darwin")
        self.agent(1)
        self.locked()
        out, _err = self.key_cli("fake-dest")
        self.assertEqual(self.adds(), [["--apple-use-keychain", self.key]])
        self.assertEqual(out[:4], ["vcharon: key fake-dest", "  agent   holds no keys",
                                   "  key     %s (RSA): the server accepts it, but it's locked"
                                   % self.key, "  run     %s" % self.key_add_shown(
                                       "--apple-use-keychain", self.key)])
        self.assertEqual(out[4:10], ["  config  add these lines at the top of ~/.ssh/config:",
                                     "            IgnoreUnknown UseKeychain",
                                     "            Host fake-dest",
                                     "                IdentityFile %s" % self.key,
                                     "                UseKeychain yes",
                                     "                AddKeysToAgent yes"])
        self.assertEqual(out[10], "  test    ok: vcharon logs in to fake-dest with no prompt")
        self.assertRegex(out[11], r"\AOK  \(\d+\.\d s\)\Z")
        self.assertEqual(len(out), 12)
        # the probe had -v and ControlMaster off; so did the test
        runs = self.ssh_runs()
        self.assertEqual(len(runs), 2)
        self.assertIn("-v", runs[0])
        self.assertNotIn("-v", runs[1])
        for argv in runs:
            self.assertIn("ControlMaster=no", argv)
        with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("  agent   holds no keys", text)
        self.assertIn("  test    ok", text)

    def key_add_shown(self, *rest):
        """The ssh-add command as the `run` line shows it: one shlex quoting of the whole
        argument list, which is what keys.show_argv does for the OS these tests declare."""
        return shlex.join([sys.executable, FAKE_SSH_ADD] + list(rest))

    def test_locked_macos_other_ssh(self):
        self.os_name("darwin")
        # any absolute path but Apple's; a host path, since ssh_path is checked as one
        other = os.path.join(self.tmp, "ssh")
        with open(os.path.join(self.vcharon_home, "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nssh_path = %s\n" % other)
        self.locked()
        out, _err = self.key_cli("fake-dest")
        self.assertIn("  warn    only Apple's ssh (/usr/bin/ssh) reads the Keychain; ssh_path is "
                      + other, out)

    def test_locked_windows(self):
        self.os_name("windows")
        self.locked()
        self.agent(2)
        out, err = self.key_cli("fake-dest", code=3)
        self.assertEqual(err[:2], ["ERROR config: the ssh-agent service isn't running",
                                   "  fix: " + ADMIN])
        self.assertIn("  agent   none: the ssh-agent service isn't running", out)
        self.assertEqual(self.adds(), [])
        self.agent(1)
        out, err = self.key_cli("fake-dest")
        self.assertEqual(self.adds(), [[self.key]])
        self.assertIn("  test    ok: vcharon logs in to fake-dest with no prompt", out)
        self.assertFalse(any(line.startswith("  config") for line in out))

    def test_locked_linux(self):
        self.locked()
        self.agent(2)
        out, err = self.key_cli("fake-dest", code=3)
        self.assertEqual(err[:2], ["ERROR config: there's no ssh agent here: SSH_AUTH_SOCK is "
                                   "unset", "  fix: " + platform.runnable(EVAL)])
        self.assertIn("  agent   none: SSH_AUTH_SOCK is unset", out)
        sock = os.path.join(self.tmp, "agent.sock")
        os.environ["SSH_AUTH_SOCK"] = sock
        out, err = self.key_cli("fake-dest", code=3)
        self.assertEqual(err[:2], ["ERROR config: no agent answers at %s" % sock,
                                   '  fix: a forwarded agent ends with its ssh login; log in '
                                   'again, or start one: eval "$(ssh-agent -s)"'])
        self.assertIn("  agent   none: nothing answers at %s" % sock, out)
        self.assertEqual(self.adds(), [])
        self.agent(0, "3072 SHA256:x other key (RSA)\n")
        out, err = self.key_cli("fake-dest")
        self.assertIn("  agent   holds 1 key", out)
        self.assertEqual(self.adds(), [[self.key]])
        self.assertIn("  test    ok: vcharon logs in to fake-dest with no prompt", out)

    def test_key_path_with_a_space(self):
        self.os_name("darwin")
        key = os.path.join(self.tmp, "my keys", "id rsa")
        os.mkdir(os.path.dirname(key))
        with open(key, "w") as f:
            f.write("k")
        self.locked(key)
        out, _err = self.key_cli("fake-dest")
        self.assertEqual(self.adds(), [["--apple-use-keychain", key]])
        self.assertIn('                IdentityFile "%s"' % key, out)

    def test_key_path_with_a_space_linux_windows(self):
        key = os.path.join(self.tmp, "my keys", "id rsa")
        os.mkdir(os.path.dirname(key))
        with open(key, "w") as f:
            f.write("k")
        os.environ["SSH_AUTH_SOCK"] = os.path.join(self.tmp, "agent.sock")
        for osn, rc in (("linux", 0), ("windows", 1)):
            with self.subTest(osn=osn):
                self.os_name(osn)
                self.agent(rc, "3072 SHA256:x other (RSA)\n" if rc == 0 else "")
                if os.path.exists(self.unlock):
                    os.remove(self.unlock)
                if os.path.exists(self.add_log):
                    os.remove(self.add_log)
                self.locked(key)
                _out, _err = self.key_cli("fake-dest")
                self.assertEqual(self.adds(), [[key]])

    def test_not_runnable_isnt_a_locked_key(self):
        # review B1: bash's own "Permission denied" for a remote_python it can't run
        os.environ["SSH_AUTH_SOCK"] = "/x"
        self.agent(0, "3072 SHA256:x me (RSA)\n")
        os.environ.update(FAKE_SSH_STDERR=accepts(self.key) + "\nbash: line 1: /usr/bin/python3: "
                          "Permission denied", FAKE_SSH_EXIT="126")
        _out, err = self.key_cli("fake-dest", code=4)
        self.assertEqual(err[0], "ERROR connect: python3 isn't runnable on fake-dest")
        self.assertEqual(self.adds(), [])

    def test_ssh_add_cant_start(self):
        # review W3
        missing = os.path.join(self.tmp, "no-such-ssh-add")
        os.environ["SSH_AUTH_SOCK"] = "/x"
        self.locked()
        with mock.patch.object(keys.ssh, "ssh_add_prefix", lambda settings: [missing]):
            out, err = self.key_cli("fake-dest", code=3)
        why = "couldn't start %s: %s" % (missing, start_failure_text(missing))
        self.assertIn("  agent   %s" % why, out)
        self.assertEqual(err[:2], ["ERROR config: " + why, "  fix: install the OpenSSH client, "
                                   "or set ssh_path in vcharon.ini"])
        self.assertFalse(any(line.startswith("  run") for line in out))

    def test_run_terminal_cant_start(self):
        # review W3: an OSError from run_terminal isn't "a bug in vcharon"
        os.environ["SSH_AUTH_SOCK"] = "/x"
        self.agent(1)
        self.locked()
        error = FileNotFoundError(2, "No such file or directory")
        with mock.patch.object(keys.fsops, "run_terminal", side_effect=error):
            _out, err = self.key_cli("fake-dest", code=3)
        self.assertTrue(err[0].startswith("ERROR config: couldn't start "), err)
        self.assertTrue(err[0].endswith(": No such file or directory"), err)
        self.assertEqual(err[1],
                         "  fix: install the OpenSSH client, or set ssh_path in vcharon.ini")

    def test_unlock_needs_a_terminal(self):
        self.terminal.return_value = False
        said = []
        with self.assertRaises(keys.VCharonError) as cm:
            keys.unlock(self.settings(), "fake-dest", None, self.log, said.append)
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ("config", "vcharon key needs a terminal: ssh-add asks for your "
                          "passphrase there"))
        self.assertEqual(cm.exception.hint, "run it in a terminal window")
        self.assertEqual((said, self.ssh_runs(), self.adds()), ([], [], []))

    def test_agent_only(self):
        for osn in ("linux", "darwin", "windows"):
            with self.subTest(osn=osn):
                self.os_name(osn)
                self.agent(0, "3072 SHA256:x c@h (RSA)\n")
                self.logs_in(accepts('"c@h"', "agent"))
                out, _err = self.key_cli("fake-dest")
                lines = ['  key     "c@h" (RSA): only the agent holds it; there\'s no key file '
                         'here to unlock', "  note    runs work while that agent holds the key"]
                if osn == "linux":
                    lines.append(LINUX_NOTE)
                self.assertEqual(out[2:2 + len(lines)], lines)
                self.assertEqual(out[2 + len(lines)],
                                 "  test    ok: vcharon logs in to fake-dest with no prompt")
                self.assertFalse(any(line.startswith("  run") for line in out))
                self.assertEqual(self.adds(), [])

    def test_file_the_agent_holds(self):
        self.logs_in(accepts(self.key, "agent"))
        self.agent(0, "3072 SHA256:x me (RSA)\n")
        out, _err = self.key_cli("fake-dest")
        self.assertIn("  key     %s (RSA): already in the agent, for as long as that agent runs"
                      % self.key, out)
        self.os_name("windows")
        out, _err = self.key_cli("fake-dest")
        self.assertIn("  key     %s (RSA): already in the ssh-agent service, which keeps it "
                      "across reboots" % self.key, out)
        self.assertEqual(self.adds(), [])
        self.os_name("darwin")
        out, _err = self.key_cli("fake-dest")
        self.assertEqual(self.adds(), [["--apple-use-keychain", self.key]])
        self.assertIn("  config  add these lines at the top of ~/.ssh/config:", out)

    def test_file_without_a_passphrase(self):
        self.logs_in(accepts(self.key))
        out, _err = self.key_cli("fake-dest")
        self.assertIn("  key     %s (RSA): it has no passphrase; nothing to unlock" % self.key,
                      out)
        self.assertEqual(self.adds(), [])

    def test_logs_in_without_a_key(self):
        out, _err = self.key_cli("fake-dest")
        self.assertIn("  key     none: fake-dest logs in without a key", out)

    def test_no_key_accepted(self):
        os.environ.update(FAKE_SSH_STDERR=DENIED, FAKE_SSH_EXIT="255")
        _out, err = self.key_cli("fake-dest", code=4)
        self.assertEqual(err[:2], ["ERROR connect: the server fake-dest accepts none of your "
                                   "keys (Permission denied)", "  fix: add your public key to "
                                   "~/.ssh/authorized_keys on fake-dest"])
        sock = os.path.join(self.tmp, "gone.sock")
        os.environ["SSH_AUTH_SOCK"] = sock
        self.agent(2)
        _out, err = self.key_cli("fake-dest", code=4)
        self.assertEqual(err[1], "  fix: no agent answers at %s: a forwarded agent ends with "
                                 "its ssh login; log in again" % sock)
        self.assertEqual(self.adds(), [])

    def test_host_key(self):
        os.environ.update(FAKE_SSH_STDERR="Host key verification failed.", FAKE_SSH_EXIT="255")
        _out, err = self.key_cli("fake-dest", code=4)
        self.assertEqual(err[0], "ERROR connect: ssh couldn't verify the host key of fake-dest")
        self.assertIn("  fix: run ssh fake-dest once in a terminal", err)
        self.assertEqual(self.adds(), [])

    def test_add_fails(self):
        os.environ["SSH_AUTH_SOCK"] = "/x"
        self.agent(1)
        self.locked()
        os.environ["FAKE_SSH_ADD_RC"] = "1"
        out, err = self.key_cli("fake-dest", code=3)
        self.assertEqual(err[:2], ["ERROR config: ssh-add didn't add %s (exit 1)" % self.key,
                                   "  fix: " + platform.runnable("check the passphrase, then "
                                                                 "run vcharon key again")])
        self.assertFalse(any(line.startswith("  test") for line in out))

    def test_still_locked_after_the_add(self):
        os.environ["SSH_AUTH_SOCK"] = "/x"
        self.agent(1)
        self.locked()
        del os.environ["FAKE_SSH_UNLOCK_FILE"]
        out, err = self.key_cli("fake-dest", code=4)
        self.assertEqual(out[-1], "  test    FAIL")
        self.assertEqual(err[0], "ERROR connect: ssh couldn't log in to fake-dest (Permission "
                                 "denied)")
        self.assertEqual(self.adds(), [[self.key]])

    def test_key_file_given(self):
        os.environ["SSH_AUTH_SOCK"] = "/x"
        self.agent(1)
        # ~ is USERPROFILE on Windows and HOME elsewhere: set both, as fake_ssh.py does
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.tmp
        out, _err = self.key_cli("--key", "~/id_rsa", "fake-dest")
        self.assertEqual(self.adds(), [[self.key]])
        self.assertIn("  key     %s: given with --key" % self.key, out)
        # a relative FILE is made absolute, from the current directory as the OS reports it:
        # on macOS the temp dir's /var or /tmp becomes /private/var or /private/tmp
        old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old)
        here = os.path.join(os.getcwd(), "id_rsa")
        out, _err = self.key_cli("--key", "id_rsa")
        self.assertEqual(self.adds(), [[self.key], [here]])
        self.assertEqual(out[0], "vcharon: key %s" % here)
        self.assertFalse(any(line.startswith("  test") for line in out))
        # no probe: no run had -v
        self.assertTrue(self.ssh_runs())
        self.assertFalse(any("-v" in argv for argv in self.ssh_runs()))

    def test_host_of(self):
        for dest, host in (("devbox", "devbox"), ("me@host", "host"), ("ssh://me@host:2222", "host"),
                           ("me@[fe80::1]", "fe80::1"), ("ssh://[fe80::1]:22", "fe80::1"),
                           ("ssh://host", "host"), ("a@b@host", "host")):
            self.assertEqual(keys.host_of(dest), host, dest)


# the folder that holds the vcharon package (src/ in the repo)
VCHARON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

# cli.main in a child whose stdout is a real UTF-8 pipe, with fake ssh and ssh-add and a
# terminal: StringIO would take any str and hide a crash on a lone surrogate
CHILD = """
import sys
sys.path[:0] = [%r]
from vcharon import cli, keys, ssh
ssh.ssh_prefix = lambda settings: [sys.executable, %r]
ssh.ssh_add_prefix = lambda settings: [sys.executable, %r]
keys.terminal = lambda: True
sys.exit(cli.main(sys.argv[1:]))
""" % (VCHARON_DIR, FAKE_SSH, FAKE_SSH_ADD)


class UndecodableKeyNameTest(KeyCase):
    """Review N1: a key file whose name isn't valid UTF-8 (the byte 0xff) is shown as \\xff,
    and still reaches ssh-add unchanged."""

    def setUp(self):
        KeyCase.setUp(self)
        if os.name != "posix":
            self.skipTest("POSIX file names are bytes")
        self.key = os.path.join(self.tmp, os.fsdecode(b"id\xff"))
        try:
            with open(self.key, "w") as f:
                f.write("k")
        except (OSError, UnicodeError):
            self.skipTest("this file system refuses the name")
        printed = os.path.join(self.tmp, "id\\377")
        os.environ.update(FAKE_SSH_STDERR=accepts(printed) + "\n" + DENIED, FAKE_SSH_EXIT="255",
                          FAKE_SSH_UNLOCK_FILE=self.unlock, SSH_AUTH_SOCK="/x",
                          FAKE_SSH_ADD_L_RC="1", PYTHONIOENCODING="utf-8")
        self.shown = os.path.join(self.tmp, "id\\xff")

    def child(self, *argv):
        return subprocess.run([sys.executable, "-c", CHILD] + list(argv), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=120, check=False)

    def test_key(self):
        result = self.child("key", "fake-dest")
        out = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 0, out + result.stderr.decode("utf-8", "replace"))
        self.assertIn("  key     %s (RSA): the server accepts it, but it's locked" % self.shown,
                      out)
        [run] = [line for line in out.splitlines() if line.startswith("  run     ")]
        self.assertIn(self.shown, run)
        self.assertIn("  test    ok: vcharon logs in to fake-dest with no prompt", out)
        # ssh-add got the real name
        self.assertEqual(self.adds(), [[self.key]])

    def test_key_on_macos_config_lines(self):
        self.os_name("darwin")
        with mock.patch.object(platform, "os_name", return_value="darwin"):
            said = []
            keys.unlock(self.settings(), "fake-dest", None, self.log, said.append)
        self.assertIn("                IdentityFile %s" % self.shown, said)
        self.assertEqual(self.adds(), [["--apple-use-keychain", self.key]])
        for line in said:
            line.encode("utf-8")

    def test_doctor(self):
        result = self.child("doctor", "--server", "fake-dest")
        out = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 1, out + result.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"ERROR internal", result.stderr)
        self.assertIn("  FAIL  fake-dest  ssh can't use your key %s: the server accepts it, but "
                      "it's locked by a passphrase" % self.shown, out)
        self.assertEqual(self.adds(), [])


if __name__ == "__main__":
    unittest.main()
