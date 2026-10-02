"""OS names, standard dirs, capabilities and machine facts."""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

from vcharon import platform


def on(osn, **env):
    """Patches the OS name and the home directory, and replaces the environment."""
    # home() through a patch: on Windows, expanduser reads USERPROFILE, not HOME
    home = env.pop("HOME", "/home/me")
    return [mock.patch.object(platform, "os_name", return_value=osn),
            mock.patch.object(platform, "home", return_value=home),
            mock.patch.dict(os.environ, env, clear=True)]


class PatchedCase(unittest.TestCase):
    def patch(self, patchers):
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


class PathsTest(PatchedCase):
    def test_ferry_home(self):
        for osn in ("linux", "darwin"):
            self.patch(on(osn, FERRY_HOME="/tmp/fh", HOME="/home/me"))
            self.assertEqual(platform.config_path(), "/tmp/fh/ferry.ini")
            self.assertEqual(platform.state_dir(), "/tmp/fh/state")
            self.assertEqual(platform.log_dir(), "/tmp/fh/logs")
        self.patch(on("windows", FERRY_HOME="D:\\fh"))
        self.assertEqual(platform.config_path(), "D:\\fh\\ferry.ini")
        self.assertEqual(platform.log_dir(), "D:\\fh\\logs")

    def test_darwin(self):
        self.patch(on("darwin", HOME="/Users/me", XDG_CONFIG_HOME="/x"))
        self.assertEqual(platform.config_path(), "/Users/me/.config/ferry/ferry.ini")
        self.assertEqual(platform.state_dir(),
                         "/Users/me/Library/Application Support/ferry/state")
        self.assertEqual(platform.log_dir(), "/Users/me/Library/Logs/ferry")

    def test_linux_defaults(self):
        self.patch(on("linux", HOME="/home/me"))
        self.assertEqual(platform.config_path(), "/home/me/.config/ferry/ferry.ini")
        self.assertEqual(platform.state_dir(), "/home/me/.local/state/ferry/state")
        self.assertEqual(platform.log_dir(), "/home/me/.local/state/ferry/logs")

    def test_linux_xdg(self):
        self.patch(on("linux", HOME="/home/me", XDG_CONFIG_HOME="/cfg", XDG_STATE_HOME="/st"))
        self.assertEqual(platform.config_path(), "/cfg/ferry/ferry.ini")
        self.assertEqual(platform.state_dir(), "/st/ferry/state")
        self.assertEqual(platform.log_dir(), "/st/ferry/logs")

    def test_linux_relative_xdg_is_ignored(self):
        self.patch(on("linux", HOME="/home/me", XDG_CONFIG_HOME="cfg", XDG_STATE_HOME="st"))
        self.assertEqual(platform.config_path(), "/home/me/.config/ferry/ferry.ini")
        self.assertEqual(platform.log_dir(), "/home/me/.local/state/ferry/logs")

    def test_windows(self):
        self.patch(on("windows", APPDATA="C:\\Users\\me\\AppData\\Roaming",
                      LOCALAPPDATA="C:\\Users\\me\\AppData\\Local"))
        self.assertEqual(platform.config_path(),
                         "C:\\Users\\me\\AppData\\Roaming\\ferry\\ferry.ini")
        self.assertEqual(platform.state_dir(), "C:\\Users\\me\\AppData\\Local\\ferry\\state")
        self.assertEqual(platform.log_dir(), "C:\\Users\\me\\AppData\\Local\\ferry\\logs")


class SshPathTest(PatchedCase):
    def test_posix(self):
        for osn in ("linux", "darwin"):
            self.patch(on(osn))
            self.assertEqual(platform.default_ssh_path(), "/usr/bin/ssh")

    def test_windows(self):
        self.patch(on("windows", SystemRoot="D:\\Win"))
        self.patch([mock.patch.object(platform, "is_wow64", return_value=False)])
        self.assertEqual(platform.default_ssh_path(), "D:\\Win\\System32\\OpenSSH\\ssh.exe")

    def test_windows_without_systemroot(self):
        self.patch(on("windows"))
        self.patch([mock.patch.object(platform, "is_wow64", return_value=False)])
        self.assertEqual(platform.default_ssh_path(), "C:\\Windows\\System32\\OpenSSH\\ssh.exe")

    def test_wow64_uses_sysnative(self):
        self.patch(on("windows", SystemRoot="C:\\Windows"))
        self.patch([mock.patch.object(platform, "is_wow64", return_value=True)])
        self.assertEqual(platform.default_ssh_path(), "C:\\Windows\\Sysnative\\OpenSSH\\ssh.exe")

    def test_is_wow64(self):
        real = struct.calcsize
        with mock.patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": "AMD64"}, clear=True):
            with mock.patch.object(struct, "calcsize",
                                   lambda fmt: 4 if fmt == "P" else real(fmt)):
                self.assertTrue(platform.is_wow64())
            self.assertEqual(platform.is_wow64(), real("P") == 4)
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(struct, "calcsize",
                                   lambda fmt: 4 if fmt == "P" else real(fmt)):
                self.assertFalse(platform.is_wow64())


class CapsTest(PatchedCase):
    def test_rules(self):
        # (os, environment, desktop): a Mac reached over ssh has no desktop
        cases = [("windows", {}, True), ("windows", {"SSH_CONNECTION": "x"}, True),
                 ("darwin", {}, True), ("darwin", {"SSH_CONNECTION": "1 2 3 4"}, False),
                 ("linux", {}, False), ("linux", {"DISPLAY": ":0"}, True),
                 ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True)]
        for osn, env, desktop in cases:
            with mock.patch.object(platform, "os_name", return_value=osn), \
                    mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(platform.caps(), {"desktop": desktop}, (osn, env))


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ferry-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def file(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_machine_id(self):
        good = self.file("good", "3ac29748d7984ad7ba3568bd550567f5\n")
        bad = self.file("bad", "3AC29748D7984AD7BA3568BD550567F5\n")
        short = self.file("short", "3ac29748d7984ad7\n")
        missing = os.path.join(self.tmp, "missing")
        # no OS reader, so the None cases hold on a Mac or Windows too
        none = dict(ioreg=lambda: None, machine_guid=lambda: None)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(platform.machine_id((missing, bad, short, good), **none),
                             "3ac29748d7984ad7ba3568bd550567f5")
            self.assertIsNone(platform.machine_id((missing, bad), **none))
            self.assertIsNone(platform.machine_id((), **none))
        with mock.patch.dict(os.environ, {"FERRY_TEST_MACHINE_ID": "f" * 32}, clear=True):
            self.assertEqual(platform.machine_id((good,)), "f" * 32)

    def test_machine_id_from_the_os(self):
        # M11a: macOS's IOPlatformUUID and Windows' MachineGuid, hashed, where the files are
        # missing
        uuid = "4C4C4544-0042-3510-8051-B4C04F4D4D32"
        hashed = hashlib.sha256(b"ferry:4c4c4544-0042-3510-8051-b4c04f4d4d32").hexdigest()[:32]
        missing = (os.path.join(self.tmp, "missing"),)
        good = self.file("good", "3ac29748d7984ad7ba3568bd550567f5\n")

        def fails():
            raise OSError("no such key")

        def must_not_run():
            raise AssertionError("read only where the files are missing")

        for osn, reader in (("darwin", "ioreg"), ("windows", "machine_guid")):
            with mock.patch.object(platform, "os_name", return_value=osn), \
                    mock.patch.dict(os.environ, {}, clear=True):
                for value, want in ((uuid, hashed), (uuid.lower(), hashed),
                                    ("not-a-uuid", None), ("", None), (None, None),
                                    (uuid + "\n", None), (b"x", None)):
                    with self.subTest(osn=osn, value=value):
                        self.assertEqual(platform.machine_id(missing, **{reader: lambda: value}),
                                         want)
                self.assertIsNone(platform.machine_id(missing, **{reader: fails}))
                self.assertEqual(platform.machine_id((good,), **{reader: must_not_run}),
                                 "3ac29748d7984ad7ba3568bd550567f5")
            with mock.patch.object(platform, "os_name", return_value=osn), \
                    mock.patch.dict(os.environ, {"FERRY_TEST_MACHINE_ID": "f" * 32}, clear=True):
                self.assertEqual(platform.machine_id(missing, **{reader: must_not_run}), "f" * 32)
        # Linux reads only the files
        with mock.patch.object(platform, "os_name", return_value="linux"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(platform.machine_id(missing, ioreg=must_not_run,
                                                  machine_guid=must_not_run))

    def test_ioreg_output(self):
        out = (b'+-o J314sAP  <class IOPlatformExpertDevice, id 0x100000202>\n  {\n'
               b'    "IOPlatformSerialNumber" = "XYZ"\n'
               b'    "IOPlatformUUID" = "4C4C4544-0042-3510-8051-B4C04F4D4D32"\n  }\n')
        with mock.patch.object(platform.fsops, "run",
                               return_value=platform.fsops.Ran(0, out, b"")) as run:
            self.assertEqual(platform._ioreg_uuid(), "4C4C4544-0042-3510-8051-B4C04F4D4D32")
        self.assertEqual(run.call_args[0], (["/usr/sbin/ioreg", "-rd1", "-c",
                                             "IOPlatformExpertDevice"], 5))
        for ran in (platform.fsops.Ran(1, out, b""), platform.fsops.Ran(None, out, b""),
                    platform.fsops.Ran(0, b"no uuid here", b"")):
            with mock.patch.object(platform.fsops, "run", return_value=ran):
                self.assertIsNone(platform._ioreg_uuid())

    def test_machine_guid(self):
        # a stand-in winreg: the key is opened in the 64-bit view, and only a string counts
        opened = []

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(value, kind):
            module = types.ModuleType("winreg")
            module.HKEY_LOCAL_MACHINE, module.KEY_READ, module.KEY_WOW64_64KEY = 7, 1, 256
            module.REG_SZ, module.REG_BINARY = 1, 3
            module.OpenKey = lambda *args: opened.append(args) or Key()
            module.QueryValueEx = lambda key, name: (value if name == "MachineGuid"
                                                     else None, kind)
            return module

        guid = "4c4c4544-0042-3510-8051-b4c04f4d4d32"
        with mock.patch.dict(sys.modules, {"winreg": fake(guid, 1)}):
            self.assertEqual(platform._machine_guid(), guid)
        self.assertEqual(opened, [(7, "SOFTWARE\\Microsoft\\Cryptography", 0, 1 | 256)])
        with mock.patch.dict(sys.modules, {"winreg": fake(b"\x01\x02", 3)}):
            self.assertIsNone(platform._machine_guid())
        with mock.patch.dict(sys.modules, {"winreg": fake(5, 1)}):
            self.assertIsNone(platform._machine_guid())

    def test_no_machine_hint(self):
        for osn, want in (("linux", None), ("darwin", "IOPlatformUUID"),
                          ("windows", "MachineGuid")):
            with mock.patch.object(platform, "os_name", return_value=osn):
                hint = platform.no_machine_hint()
            if want is None:
                self.assertIsNone(hint)
            else:
                self.assertIn(want, hint)
                self.assertTrue(hint.endswith("ask the user"), hint)

    def test_distro(self):
        a = self.file("a", 'NAME="Debian GNU/Linux"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n'
                           "ID=debian\n")
        b = self.file("b", "PRETTY_NAME='Other OS'\n")
        c = self.file("c", "PRETTY_NAME=Plain\n")
        none = self.file("none", "NAME=x\n")
        missing = os.path.join(self.tmp, "missing")
        self.assertEqual(platform.distro((missing, a, b)), "Debian GNU/Linux 13 (trixie)")
        self.assertEqual(platform.distro((b,)), "Other OS")
        self.assertEqual(platform.distro((c,)), "Plain")
        self.assertIsNone(platform.distro((none, a)))
        self.assertIsNone(platform.distro((missing,)))

    def test_small_facts(self):
        self.assertIn(platform.os_name(), ("linux", "darwin", "windows"))
        self.assertRegex(platform.python_version(), r"\A3\.\d+\.\d+\Z")
        self.assertIsInstance(platform.user(), str)
        with mock.patch("getpass.getuser", side_effect=OSError("no user")):
            self.assertEqual(platform.user(), "")
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "USERPROFILE": self.tmp}):
            self.assertEqual(platform.home(), self.tmp)



def no_which(name):
    return None


class FerryCommandTest(unittest.TestCase):
    """How a fix line runs ferry on this box (M14a): the pure core for every OS, on any OS."""

    def test_posix_quotes_a_space_and_shell_marks(self):
        self.assertEqual(platform.command_for("/opt/my py/bin/python3", "/home/me/my ferry",
                                              "linux", no_which),
                         "'/opt/my py/bin/python3' '/home/me/my ferry'")
        self.assertEqual(platform.command_for("/usr/bin/python3", "/home/me/a$b/ferry",
                                              "darwin", no_which),
                         "/usr/bin/python3 '/home/me/a$b/ferry'")
        self.assertEqual(platform.command_for("/usr/bin/python3", "/home/me/ferry", "linux",
                                              no_which), "/usr/bin/python3 /home/me/ferry")

    def test_windows_forward_slashes_and_double_quotes(self):
        exe = "C:\\Program Files\\Python39\\python.exe"
        self.assertEqual(platform.command_for(exe, "C:\\Users\\me\\ferry", "windows",
                                              no_which),
                         '"C:/Program Files/Python39/python.exe" C:/Users/me/ferry')
        self.assertEqual(platform.command_for("C:\\Python39\\python.exe",
                                              "D:\\my work\\ferry", "windows", no_which),
                         'C:/Python39/python.exe "D:/my work/ferry"')
        # PATH's python.exe is that same file: its name
        self.assertEqual(platform.command_for(exe, "C:\\Users\\me\\ferry", "windows",
                                              lambda name: exe if name == "python.exe" else None),
                         "python.exe C:/Users/me/ferry")

    def test_which_match_or_not(self):
        def which(found):
            return lambda name: found if name == "python3" else None

        self.assertEqual(platform.command_for("/usr/bin/python3", "/f", "linux",
                                              which("/usr/bin/python3")), "python3 /f")
        # PATH's python3 is another one: the whole path
        self.assertEqual(platform.command_for("/usr/bin/python3", "/f", "linux",
                                              which("/usr/local/bin/python3")),
                         "/usr/bin/python3 /f")
        self.assertEqual(platform.command_for("/usr/bin/python3", "/f", "linux", no_which),
                         "/usr/bin/python3 /f")

    def test_which_through_a_link(self):
        tmp = tempfile.mkdtemp(prefix="ferry-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        real = os.path.join(tmp, "python3.11")
        open(real, "w").close()
        link = os.path.join(tmp, "python3")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        # by realpath: PATH's python3 is a link to this very file
        self.assertEqual(platform.command_for(real, "/f", "linux",
                                              lambda name: link if name == "python3.11" else None),
                         "python3.11 /f")

    def test_runnable(self):
        cmd = "py /f"
        for text, want in (
                ("ferry channel leave mb --project p", "py /f channel leave mb --project p"),
                ("run: ferry key dest", "run: py /f key dest"),
                ("(ferry channel close mb x)", "(py /f channel close mb x)"),
                ("use `ferry run a.up`", "use `py /f run a.up`"),
                ("ferry --help", "py /f --help"),
                ("ferry state reset a, and ferry run a --full",
                 "py /f state reset a, and py /f run a --full")):
            with self.subTest(text=text):
                self.assertEqual(platform.runnable(text, cmd), want)
        for text in ("ferry's rules", "ferry/README.md", "see /x/ferry run", "ferry run's fix",
                     "MEMBER.md is ferry's", "ferry channel's to write", "ferry runner",
                     "(ferry cp: --skip-symlinks)", "a ferry", "xferry run a", "ferry  run",
                     ""):
            with self.subTest(text=text):
                self.assertEqual(platform.runnable(text, cmd), text)

    def test_this_ferry(self):
        folder = platform.ferry_dir()
        self.assertTrue(os.path.isfile(os.path.join(folder, "vcharon", "__main__.py")), folder)
        self.assertTrue(os.path.isabs(folder))
        self.assertEqual(platform.ferry_command(), platform.command_for(
            sys.executable, None, platform.os_name(), shutil.which) + " -m vcharon")
        self.assertEqual(platform.runnable("ferry run a"),
                         platform.ferry_command() + " run a")
        # no folder (the server's bundled copy): as written
        with mock.patch.object(platform, "ferry_dir", return_value=None):
            self.assertIsNone(platform.ferry_command())
            self.assertEqual(platform.runnable("ferry run a"), "ferry run a")

if __name__ == "__main__":
    unittest.main()
