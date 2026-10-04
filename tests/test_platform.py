"""OS names, standard dirs, capabilities and machine facts."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import site
import struct
import subprocess
import sys
import sysconfig
import tempfile
import types
import unittest
from unittest import mock

from vcharon import platform
from vcharon.proto import VCharonError

import tests


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
    def test_vcharon_home(self):
        for osn in ("linux", "darwin"):
            self.patch(on(osn, VCHARON_HOME="/tmp/fh", HOME="/home/me"))
            self.assertEqual(platform.config_path(), "/tmp/fh/vcharon.ini")
            self.assertEqual(platform.state_dir(), "/tmp/fh/state")
            self.assertEqual(platform.log_dir(), "/tmp/fh/logs")
        self.patch(on("windows", VCHARON_HOME="D:\\fh"))
        self.assertEqual(platform.config_path(), "D:\\fh\\vcharon.ini")
        self.assertEqual(platform.log_dir(), "D:\\fh\\logs")

    def test_relative_vcharon_home_is_refused(self):
        # a relative folder would follow the current directory, so each command is refused;
        # "\\vc" has no drive, so it is relative on Windows too
        for value in ("vc", "./vc", "..", "\\vc"):
            with self.subTest(value=value):
                patcher = mock.patch.dict(os.environ, {"VCHARON_HOME": value})
                patcher.start()
                self.addCleanup(patcher.stop)
                for where in (platform.config_path, platform.state_dir, platform.log_dir,
                              platform.joined_dir):
                    with self.assertRaises(VCharonError) as cm:
                        where()
                    self.assertEqual(cm.exception.code, "config")
                    self.assertEqual(cm.exception.message,
                                     "VCHARON_HOME is '%s', not an absolute folder" % value)
                    self.assertIn("set VCHARON_HOME to a full path", cm.exception.hint)
                    # a backslash prints once, as typed
                    self.assertNotIn("\\\\", cm.exception.message)

    def test_vcharon_home_expands_the_tilde(self):
        # the real OS's expanduser, which reads HOME on POSIX and USERPROFILE on Windows
        home = os.path.abspath(os.sep + "home-me")
        patcher = mock.patch.dict(os.environ, {"VCHARON_HOME": "~/vc", "HOME": home,
                                               "USERPROFILE": home})
        patcher.start()
        self.addCleanup(patcher.stop)
        want = os.path.join(home, "vc", "state")
        self.assertEqual(os.path.normpath(platform.state_dir()), want)
        self.assertEqual(os.path.normpath(platform.config_path()),
                         os.path.join(home, "vc", "vcharon.ini"))

    def test_standard_dirs(self):
        # (os, environment) -> (config file, state dir, logs dir)
        linux = ("/home/me/.config/vcharon/vcharon.ini", "/home/me/.local/state/vcharon/state",
                 "/home/me/.local/state/vcharon/logs")
        for osn, env, want in (
                ("darwin", {"HOME": "/Users/me", "XDG_CONFIG_HOME": "/x"},
                 ("/Users/me/.config/vcharon/vcharon.ini",
                  "/Users/me/Library/Application Support/vcharon/state",
                  "/Users/me/Library/Logs/vcharon")),
                ("linux", {"HOME": "/home/me"}, linux),
                ("linux", {"HOME": "/home/me", "XDG_CONFIG_HOME": "/cfg", "XDG_STATE_HOME": "/st"},
                 ("/cfg/vcharon/vcharon.ini", "/st/vcharon/state", "/st/vcharon/logs")),
                # a relative XDG folder is ignored
                ("linux", {"HOME": "/home/me", "XDG_CONFIG_HOME": "cfg", "XDG_STATE_HOME": "st"},
                 linux),
                ("windows", {"APPDATA": "C:\\Users\\me\\AppData\\Roaming",
                             "LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"},
                 ("C:\\Users\\me\\AppData\\Roaming\\vcharon\\vcharon.ini",
                  "C:\\Users\\me\\AppData\\Local\\vcharon\\state",
                  "C:\\Users\\me\\AppData\\Local\\vcharon\\logs"))):
            with self.subTest(os=osn, env=env), contextlib.ExitStack() as stack:
                for patcher in on(osn, **env):
                    stack.enter_context(patcher)
                self.assertEqual((platform.config_path(), platform.state_dir(),
                                  platform.log_dir()), want)


class SshPathTest(PatchedCase):
    def test_posix(self):
        for osn in ("linux", "darwin"):
            self.patch(on(osn))
            self.assertEqual(platform.default_ssh_path(), "/usr/bin/ssh")

    def test_windows(self):
        # (SystemRoot, a 32-bit Python on 64-bit Windows) -> the path
        for root, wow64, want in (
                ("D:\\Win", False, "D:\\Win\\System32\\OpenSSH\\ssh.exe"),
                (None, False, "C:\\Windows\\System32\\OpenSSH\\ssh.exe"),
                ("C:\\Windows", True, "C:\\Windows\\Sysnative\\OpenSSH\\ssh.exe")):
            env = {"SystemRoot": root} if root else {}
            with self.subTest(root=root, wow64=wow64), contextlib.ExitStack() as stack:
                for patcher in on("windows", **env) + [
                        mock.patch.object(platform, "is_wow64", return_value=wow64)]:
                    stack.enter_context(patcher)
                self.assertEqual(platform.default_ssh_path(), want)

    def test_is_wow64(self):
        real = struct.calcsize
        with mock.patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": "AMD64"}, clear=True):
            with mock.patch.object(struct, "calcsize",
                                   lambda fmt: 4 if fmt == "P" else real(fmt)):
                self.assertTrue(platform.is_wow64())
            self.assertEqual(platform.is_wow64(), real("P") == 4)
        with (mock.patch.dict(os.environ, {}, clear=True),
              mock.patch.object(struct, "calcsize",
                                lambda fmt: 4 if fmt == "P" else real(fmt))):
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
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
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
        none = {"ioreg": lambda: None, "machine_guid": lambda: None}
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(platform.machine_id((missing, bad, short, good), **none),
                             "3ac29748d7984ad7ba3568bd550567f5")
            self.assertIsNone(platform.machine_id((missing, bad), **none))
            self.assertIsNone(platform.machine_id((), **none))
        with mock.patch.dict(os.environ, {"VCHARON_TEST_MACHINE_ID": "f" * 32}, clear=True):
            self.assertEqual(platform.machine_id((good,)), "f" * 32)

    def test_machine_id_from_the_os(self):
        # macOS's IOPlatformUUID and Windows' MachineGuid, hashed, where the files are
        # missing
        uuid = "4C4C4544-0042-3510-8051-B4C04F4D4D32"
        hashed = hashlib.sha256(b"vcharon:4c4c4544-0042-3510-8051-b4c04f4d4d32").hexdigest()[:32]
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
                        self.assertEqual(platform.machine_id(
                            missing, **{reader: lambda value=value: value}), want)
                self.assertIsNone(platform.machine_id(missing, **{reader: fails}))
                self.assertEqual(platform.machine_id((good,), **{reader: must_not_run}),
                                 "3ac29748d7984ad7ba3568bd550567f5")
            with mock.patch.object(platform, "os_name", return_value=osn), \
                    mock.patch.dict(os.environ, {"VCHARON_TEST_MACHINE_ID": "f" * 32}, clear=True):
                self.assertEqual(platform.machine_id(missing, **{reader: must_not_run}), "f" * 32)
        # Linux reads only the files
        with mock.patch.object(platform, "os_name", return_value="linux"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(platform.machine_id(missing, ioreg=must_not_run,
                                                  machine_guid=must_not_run))

    def test_the_default_readers(self):
        # machine_id() with its own readers: ioreg's output on a Mac, the registry on Windows
        missing = (os.path.join(self.tmp, "missing"),)
        uuid = "4C4C4544-0042-3510-8051-B4C04F4D4D32"
        hashed = hashlib.sha256(("vcharon:" + uuid.lower()).encode()).hexdigest()[:32]
        out = ('    "IOPlatformUUID" = "%s"\n' % uuid).encode()
        with mock.patch.object(platform, "os_name", return_value="darwin"), \
                mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(platform.fsops, "run",
                                   return_value=platform.fsops.Ran(0, out, b"")):
                self.assertEqual(platform.machine_id(missing), hashed)
            # no ioreg at all
            with mock.patch.object(platform.fsops, "run", side_effect=FileNotFoundError(2, "x")):
                self.assertIsNone(platform.machine_id(missing))
        module = types.ModuleType("winreg")
        module.HKEY_LOCAL_MACHINE, module.KEY_READ, module.KEY_WOW64_64KEY = 7, 1, 256
        module.REG_SZ = 1

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        module.OpenKey = lambda *args: Key()
        module.QueryValueEx = lambda key, name: (uuid, 1)
        with mock.patch.object(platform, "os_name", return_value="windows"), \
                mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(platform, "winreg", module):
                self.assertEqual(platform.machine_id(missing), hashed)

            def no_key(*args):
                raise FileNotFoundError(2, "no such key")

            module.OpenKey = no_key
            with mock.patch.object(platform, "winreg", module):
                self.assertIsNone(platform.machine_id(missing))

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
        with mock.patch.object(platform, "winreg", fake(guid, 1)):
            self.assertEqual(platform._machine_guid(), guid)
        self.assertEqual(opened, [(7, "SOFTWARE\\Microsoft\\Cryptography", 0, 1 | 256)])
        with mock.patch.object(platform, "winreg", fake(b"\x01\x02", 3)):
            self.assertIsNone(platform._machine_guid())
        with mock.patch.object(platform, "winreg", fake(5, 1)):
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
                self.assertTrue(hint.endswith("ask your user"), hint)

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

    def test_os_release(self):
        a = self.file("a", 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nVERSION_ID="13"\n'
                           "ID=debian\nID=other\n")
        none = self.file("none", "NAME=x\n")
        missing = os.path.join(self.tmp, "missing")
        self.assertEqual(platform.os_release((missing, a)),
                         {"PRETTY_NAME": "Debian GNU/Linux 13 (trixie)", "ID": "debian",
                          "VERSION_ID": "13"})
        # the first file that exists counts, even with none of the keys
        self.assertEqual(platform.os_release((none, a)),
                         {"PRETTY_NAME": None, "ID": None, "VERSION_ID": None})
        self.assertIsNone(platform.os_release((missing,)))

    def test_distro_warning(self):
        text = "VCharon is tested on Debian 13 or later; "
        for hello, want in (
                ({"distro_id": "debian", "distro_version": "13"}, None),
                ({"distro_id": "debian", "distro_version": "13.1"}, None),
                ({"distro_id": "debian", "distro_version": "20"}, None),
                ({"distro_id": "debian", "distro_version": "12", "distro": "Debian 12"},
                 text + "it is ID=debian VERSION_ID=12 (Debian 12)"),
                ({"distro_id": "debian"}, text + "it is ID=debian VERSION_ID=?"),
                ({"distro_id": "ubuntu", "distro_version": "24.04"},
                 text + "it is ID=ubuntu VERSION_ID=24.04"),
                ({"distro_id": "debian", "distro_version": "\u0661\u0663"},
                 text + "it is ID=debian VERSION_ID=\u0661\u0663"),
                ({}, text + "its os-release names no distro (no ID=)"),
                ({"distro_id": 13, "distro_version": 13},
                 text + "its os-release names no distro (no ID=)")):
            with self.subTest(hello=hello):
                self.assertEqual(platform.distro_warning(hello), want)

    def test_os_release_to_the_warning(self):
        # a server's os-release file, through the hello's fields (helper.hello), to doctor's
        # warning; --json's "tested" is that there is none. test_doctor's DistroTest runs an old
        # Debian and a missing file end to end.
        text = "VCharon is tested on Debian 13 or later; "
        for data, warning, fields in (
                (b'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\n'
                 b'VERSION_ID="13"\nVERSION="13 (trixie)"\nID=debian\n', None, ("debian", "13")),
                (b'ID=debian\nVERSION_ID="14"\nPRETTY_NAME="Debian GNU/Linux 14 (forky)"\n', None,
                 ("debian", "14")),
                (b'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n',
                 text + "it is ID=debian VERSION_ID=12 (Debian GNU/Linux 12 (bookworm))",
                 ("debian", "12")),
                (b'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'
                 b'PRETTY_NAME="Ubuntu 24.04.1 LTS"\n',
                 text + "it is ID=ubuntu VERSION_ID=24.04 (Ubuntu 24.04.1 LTS)",
                 ("ubuntu", "24.04")),
                (None, text + "its os-release names no distro (no ID=)", (None, None)),
                (b"\x00\xff garbage\nno equals here\n=\n",
                 text + "its os-release names no distro (no ID=)", (None, None)),
                (b"ID=debian\nVERSION_ID=thirteen\n",
                 text + "it is ID=debian VERSION_ID=thirteen", ("debian", "thirteen"))):
            with self.subTest(data=data):
                path = os.path.join(self.tmp, "os-release")
                if data is None:
                    path = os.path.join(self.tmp, "missing")
                else:
                    with open(path, "wb") as f:
                        f.write(data)
                release = platform.os_release((path,)) or {}
                hello = {"distro": release.get("PRETTY_NAME"), "distro_id": release.get("ID"),
                         "distro_version": release.get("VERSION_ID")}
                self.assertEqual((hello["distro_id"], hello["distro_version"]), fields)
                self.assertEqual(platform.distro_warning(hello), warning)

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


# this host's own form, for this host's paths (a Linux form can't take C:\...)
HOST = "windows" if os.name == "nt" else "linux"


class VCharonCommandTest(unittest.TestCase):
    """How a fix line runs vcharon on this box: the pure core for every OS, on any OS."""

    def test_posix_quotes_a_space_and_shell_marks(self):
        self.assertEqual(platform.command_for("/opt/my py/bin/python3", "/home/me/my vcharon",
                                              "linux", no_which),
                         "'/opt/my py/bin/python3' '/home/me/my vcharon'")
        self.assertEqual(platform.command_for("/usr/bin/python3", "/home/me/a$b/vcharon",
                                              "darwin", no_which),
                         "/usr/bin/python3 '/home/me/a$b/vcharon'")
        self.assertEqual(platform.command_for("/usr/bin/python3", "/home/me/vcharon", "linux",
                                              no_which), "/usr/bin/python3 /home/me/vcharon")

    def test_windows_forward_slashes_and_double_quotes(self):
        exe = "C:\\Program Files\\Python39\\python.exe"
        self.assertEqual(platform.command_for(exe, "C:\\Users\\me\\vcharon", "windows",
                                              no_which),
                         '"C:/Program Files/Python39/python.exe" C:/Users/me/vcharon')
        self.assertEqual(platform.command_for("C:\\Python39\\python.exe",
                                              "D:\\my work\\vcharon", "windows", no_which),
                         'C:/Python39/python.exe "D:/my work/vcharon"')
        # PATH's python.exe is that same file: its name
        self.assertEqual(platform.command_for(exe, "C:\\Users\\me\\vcharon", "windows",
                                              lambda name: exe if name == "python.exe" else None),
                         "python.exe C:/Users/me/vcharon")

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
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        real = os.path.join(tmp, "python3.13")
        open(real, "w").close()
        link = os.path.join(tmp, "python3")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        # by realpath: PATH's python3 is a link to this very file. The host's own OS form,
        # since real and link are this host's paths (a Linux form can't split C:\...).
        osn = "windows" if os.name == "nt" else "linux"
        which = lambda name: link if name == "python3.13" else None
        self.assertEqual(platform.command_for(real, "/f", osn, which, venv=False),
                         "python3.13 /f")
        # a venv's python is a link to the base one, and only the link finds the venv's
        # packages: by the path itself
        self.assertEqual(platform.command_for(real, "/f", osn, which, venv=True),
                         platform.command_for(real, "/f", osn, no_which))

    def test_runnable(self):
        cmd = "py -P -m vcharon"
        for text, want in (
                ("vcharon leave mb --project p", "py -P -m vcharon leave mb --project p"),
                ("run: vcharon key dest", "run: py -P -m vcharon key dest"),
                ("(vcharon close mb --project x)", "(py -P -m vcharon close mb --project x)"),
                ("use `vcharon sync mb --full`", "use `py -P -m vcharon sync mb --full`"),
                ("vcharon --help", "py -P -m vcharon --help"),
                ("vcharon sync a --reset up, and vcharon sync a --full",
                 "py -P -m vcharon sync a --reset up, and py -P -m vcharon sync a --full")):
            with self.subTest(text=text):
                self.assertEqual(platform.runnable(text, cmd), want)
        for verb in platform.COMMANDS:
            with self.subTest(verb=verb):
                self.assertEqual(platform.runnable("vcharon %s" % verb, cmd), "%s %s"
                                 % (cmd, verb))
        for text in ("vcharon's rules", "vcharon/README.md", "see /x/vcharon sync",
                     "vcharon sync's fix", "MEMBER.md is vcharon's", "vcharon join's to write",
                     "vcharon syncer", "(vcharon cp: --skip-symlinks)", "a vcharon",
                     "xvcharon sync a", "vcharon  sync", "vcharon run a", "vcharon state reset a",
                     "vcharon channel list", ""):
            with self.subTest(text=text):
                self.assertEqual(platform.runnable(text, cmd), text)

    def test_runnable_twice_is_once(self):
        # the command ends in `-m vcharon`: its own `vcharon <command>` is never taken again;
        # nor is a binary's path, and `vcharon` itself is the same text again
        text = "run vcharon leave mb --project x, then vcharon sync mb --full"
        for cmd in ("python3 -P -m vcharon", '"C:/my py/python.exe" -P -m vcharon',
                    "/opt/bin/vcharon", "'/my bin/vcharon'", '"C:/my bin/vcharon.exe"',
                    "vcharon", None):
            with self.subTest(cmd=cmd):
                once = platform.runnable(text, cmd)
                self.assertEqual(platform.runnable(once, cmd), once)
        self.assertEqual(platform.runnable(text, "python3 -P -m vcharon"),
                         "run python3 -P -m vcharon leave mb --project x, then python3 -P -m "
                         "vcharon sync mb --full")


class SelfTest(unittest.TestCase):
    """self_argv (how vcharon starts itself), child_env, and self_command (how a fix line spells
    the command), each with sys.frozen, sys._MEIPASS, sys.executable and which faked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def frozen(self, executable, meipass=True):
        """Patches this process to look like a PyInstaller binary at executable."""
        patchers = [mock.patch.object(sys, "frozen", True, create=True),
                    mock.patch.object(sys, "executable", executable)]
        if meipass:
            patchers.append(mock.patch.object(sys, "_MEIPASS", os.path.join(self.tmp, "_MEI1"),
                                              create=True))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_self_argv(self):
        self.assertFalse(platform.is_frozen())
        self.assertEqual(platform.self_argv(), [sys.executable, "-P", "-m", "vcharon"])
        binary = os.path.join(self.tmp, "vcharon")
        self.frozen(binary)
        self.assertTrue(platform.is_frozen())
        self.assertEqual(platform.self_argv(), [binary])

    def test_frozen_needs_meipass_too(self):
        # sys.frozen alone (another freezer) isn't PyInstaller's binary
        self.frozen(os.path.join(self.tmp, "vcharon"), meipass=False)
        self.assertFalse(platform.is_frozen())
        self.assertEqual(platform.self_argv(), [sys.executable, "-P", "-m", "vcharon"])

    def test_child_env(self):
        base = {"A": "1", "PYINSTALLER_RESET_ENVIRONMENT": "x"}
        self.assertEqual(platform.child_env(base), base)
        self.assertIsNot(platform.child_env(base), base)
        self.frozen(os.path.join(self.tmp, "vcharon"))
        self.assertEqual(platform.child_env({"A": "1"}),
                         {"A": "1", "PYINSTALLER_RESET_ENVIRONMENT": "1"})

    def test_a_binary(self):
        binary = os.path.join(self.tmp, "my bin", "vcharon")
        os.makedirs(os.path.dirname(binary))
        open(binary, "w").close()
        self.frozen(binary)
        # PATH's vcharon is this binary: its name
        self.assertEqual(platform.self_command(lambda name: binary, HOST), "vcharon")
        # none on PATH, or another one: the whole path, quoted for this host's shell
        quoted = platform._quoted([binary], HOST)
        self.assertNotEqual(quoted, binary)
        self.assertEqual(platform.self_command(no_which, HOST), quoted)
        other = os.path.join(self.tmp, "vcharon")
        open(other, "w").close()
        self.assertEqual(platform.self_command(lambda name: other, HOST), quoted)
        # doctor's PATH check asks the same question of each vcharon on PATH
        self.assertTrue(platform.runs_this(binary))
        self.assertFalse(platform.runs_this(other))

    def test_the_current_folder_doesnt_count_on_windows(self):
        # cmd.exe (and shutil.which) look in the current folder first; Git Bash and PowerShell,
        # where agents type commands, never do: a vcharon.exe started from its download folder
        # isn't what a typed vcharon runs there
        here = os.path.join(self.tmp, "Downloads")
        on_path = os.path.join(self.tmp, "old")
        for folder in (here, on_path):
            os.makedirs(folder)
            open(os.path.join(folder, "vcharon.exe"), "w").close()
        binary = os.path.join(here, "vcharon.exe")
        old = os.path.join(on_path, "vcharon.exe")
        cwd = os.getcwd()
        os.chdir(here)
        self.addCleanup(os.chdir, cwd)
        which = lambda name: binary
        with mock.patch.dict(os.environ, {"PATHEXT": ".EXE"}):
            self.assertEqual(platform.typed_vcharon(which, "windows", on_path), old)
            self.assertIsNone(platform.typed_vcharon(which, "windows", ""))
            # the current folder on PATH itself: it counts
            self.assertEqual(platform.typed_vcharon(
                which, "windows", os.pathsep.join([here, on_path])), binary)
            # elsewhere, which's answer as it is
            self.assertEqual(platform.typed_vcharon(which, "linux", on_path), binary)
            self.assertEqual(platform.typed_vcharon(lambda name: old, "windows", on_path), old)
            self.assertIsNone(platform.typed_vcharon(lambda name: None, "windows", on_path))
            # so a binary run from there is spelled by its path, not as vcharon
            self.frozen(binary)
            with mock.patch.dict(os.environ, {"PATH": on_path}):
                self.assertEqual(platform.self_command(which, "windows"),
                                 platform._quoted([binary], "windows"))
                self.assertFalse(platform.runs_this(old))

    def test_a_posix_binary_path(self):
        # the declared OS's form, on a path of that OS
        self.frozen("/opt/my tools/vcharon")
        self.assertEqual(platform.self_command(no_which, "linux"), "'/opt/my tools/vcharon'")

    def test_a_binary_through_a_link(self):
        binary = os.path.join(self.tmp, "vcharon-0.1.0")
        open(binary, "w").close()
        link = os.path.join(self.tmp, "vcharon")
        try:
            os.symlink(binary, link)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        self.frozen(binary)
        self.assertEqual(platform.self_command(lambda name: link, HOST), "vcharon")

    def test_a_windows_binary(self):
        exe = "C:\\Users\\me\\My Tools\\vcharon.exe"
        self.frozen(exe)
        self.assertEqual(platform.self_command(no_which, "windows"),
                         '"C:/Users/me/My Tools/vcharon.exe"')
        self.frozen("C:\\tools\\vcharon.exe")
        self.assertEqual(platform.self_command(no_which, "windows"), "C:/tools/vcharon.exe")

    def entry_point(self, folder, python, text=None):
        """A vcharon entry-point script in folder, its #! line naming python; its path."""
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "vcharon")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text if text is not None else "#!%s\nimport vcharon\n" % python)
        return path

    def test_an_entry_point_of_this_environment(self):
        # pipx and uv: ~/.local/bin/vcharon links to the venv's bin/vcharon, next to its python
        venv_bin = os.path.join(self.tmp, "venvs", "vcharon", "bin")
        os.makedirs(venv_bin)
        python = os.path.join(venv_bin, "python")
        open(python, "w").close()
        entry = self.entry_point(venv_bin, python)
        local_bin = os.path.join(self.tmp, "local-bin")
        os.makedirs(local_bin)
        link = os.path.join(local_bin, "vcharon")
        try:
            os.symlink(entry, link)
        except (OSError, NotImplementedError):
            link = entry
        with mock.patch.object(sys, "executable", python):
            for found in (entry, link):
                with self.subTest(found=found):
                    self.assertTrue(platform.runs_this_package(found))
                    self.assertEqual(platform.self_command(lambda name, found=found: found, HOST),
                                     "vcharon")
            # another install's vcharon on PATH: this one, by its python
            other = self.entry_point(os.path.join(self.tmp, "other"), python)
            self.assertFalse(platform.runs_this_package(other))
            want = platform.command_for(python, None, HOST, no_which) + " -P -m vcharon"
            self.assertEqual(platform.self_command(lambda name: other, HOST), want)
            self.assertEqual(platform.self_command(no_which, HOST), want)

    @unittest.skipIf(os.name == "nt", "an .exe launcher has no #! line")
    def test_the_scripts_python(self):
        folder = os.path.join(self.tmp, "bin")
        python = os.path.join(folder, "python")
        os.makedirs(folder)
        open(python, "w").close()
        with mock.patch.object(sys, "executable", python):
            # another Python's script in this folder, or none named: not this install
            for text in ("#!/usr/bin/python3.9\nimport vcharon\n", "", "import vcharon\n",
                         "#!\n"):
                with self.subTest(text=text):
                    entry = self.entry_point(folder, python, text)
                    self.assertFalse(platform.runs_this_package(entry, [folder]))
            # pip's /bin/sh form for a long interpreter path, a quoted one, one with an option
            sh = "#!/bin/sh\n'''exec' \"%s\" \"$0\" \"$@\"\n' '''\n" % python
            for text in (sh, '#!"%s"\n' % python, "#!%s -E\n" % python):
                with self.subTest(text=text):
                    entry = self.entry_point(folder, python, text)
                    self.assertTrue(platform.runs_this_package(entry, [folder]))

    def test_scripts_folders(self):
        # pip install --user: the user scheme's scripts folder
        user = os.path.join(self.tmp, "user-bin")
        entry = self.entry_point(user, sys.executable)
        self.assertFalse(platform.runs_this_package(entry, []))
        self.assertTrue(platform.runs_this_package(entry, [os.path.join(self.tmp, "x"), user]))
        self.assertIn(os.path.dirname(sys.executable), platform._scripts_dirs())
        # a found name that isn't a file (gone, or a folder) is no entry point
        self.assertFalse(platform.runs_this_package(os.path.join(user, "gone"), [user]))
        self.assertFalse(platform.runs_this_package(user, [self.tmp]))

    def test_the_user_scheme_only_outside_a_venv(self):
        def get_path(name, scheme=None):
            return "/user-bin" if scheme else "/scripts"

        with mock.patch.object(sysconfig, "get_path", get_path):
            for prefix, base, enabled, user in (("/p", "/p", True, True),
                                                ("/p", "/p", False, False),
                                                ("/venv", "/p", True, False)):
                with self.subTest(prefix=prefix, base=base, enabled=enabled), \
                        mock.patch.object(sys, "prefix", prefix), \
                        mock.patch.object(sys, "base_prefix", base), \
                        mock.patch.object(site, "ENABLE_USER_SITE", enabled):
                    self.assertEqual("/user-bin" in platform._scripts_dirs(), user)
                    self.assertIn("/scripts", platform._scripts_dirs())

    def test_this_vcharon(self):
        # whatever this run's install is, the spelling parses back into this CLI
        command = platform.self_command()
        self.assertTrue(command == "vcharon" or command.endswith(" -P -m vcharon"), command)
        self.assertEqual(platform.runnable("vcharon sync a"), command + " sync a")


class SandboxTest(unittest.TestCase):
    """tests/__init__.py's sandbox (DESIGN, "Tests")."""

    def test_the_guard_refuses_the_real_home(self):
        real = {"VCHARON_HOME": os.path.join(tests.REAL_HOME, "vc")}
        with (mock.patch.dict(os.environ, real),
              self.assertRaisesRegex(AssertionError, "reached the real home")):
            platform.config_path()

    def test_the_shell_cant_turn_it_off_or_aim_it(self):
        # a developer's exported hand-run folders, and a real-ssh destination, in the
        # environment the suite starts from
        scratch = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, scratch, True)
        env = dict(os.environ, HOME=scratch, USERPROFILE=scratch,
                   VCHARON_HOME=os.path.join(scratch, "vh"),
                   VCHARON_CHANNELS_ROOT=os.path.join(scratch, "channels"),
                   VCHARON_TEST_SSH="devbox")
        code = ("import os, tests\n"
                "for name in ('VCHARON_HOME', 'VCHARON_CHANNELS_ROOT', 'HOME', 'USERPROFILE'):\n"
                "    print(os.environ.get(name))\n")
        top = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-c", code], cwd=top, env=env, timeout=60,
                             capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(out[:2], ["None", "None"])
        for home in out[2:]:
            self.assertTrue(os.path.basename(os.path.dirname(home)).startswith("vcharon-suite-"),
                            out)


if __name__ == "__main__":
    unittest.main()
