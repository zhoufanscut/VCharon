"""OS name, standard dirs, capabilities and facts about this machine, on both ends."""

from __future__ import annotations

import getpass
import hashlib
import ntpath
import os
import posixpath
import re
import shlex
import shutil
import site
import struct
import sys
import sysconfig

from . import fsops

_MACHINE_ID = re.compile(r"\A[0-9a-f]{32}\Z")
# macOS's IOPlatformUUID and Windows' MachineGuid: a UUID, in either case
_UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}\Z")
_IOREG_UUID = re.compile(r'"IOPlatformUUID" = "([^"]*)"')
# what needs quoting in a command line: shlex.quote's own test, on both kinds of shell
_UNSAFE = re.compile(r"[^\w@%+=:,./-]", re.ASCII)
# vcharon's commands, as runnable() takes them: `vcharon` at the text's start or after a space, (
# or `, then one of these and a space, the end, or a mark that ends a clause; never `vcharon's`,
# `vcharon/`, `vcharon sync's`, a path, or the `-m vcharon` of a command runnable() wrote
COMMANDS = ("key", "doctor", "ping", "list", "create", "join", "leave", "close", "whoami",
            "post", "read", "watch", "sync", "--version", "--help")
_VCHARON_WORD = re.compile(r"(?:\A|(?<=[ (`]))(?<!-m )vcharon (%s)(?=\Z|[\s),.;`])"
                           % "|".join(re.escape(c) for c in COMMANDS))
IOREG = "/usr/sbin/ioreg"
IOREG_TIMEOUT = 5


def os_name():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _path():
    # By os_name() rather than os.path, so each OS's paths can be tested on any OS.
    return ntpath if os_name() == "windows" else posixpath


def home():
    return os.path.expanduser("~")


def _vcharon_home():
    return os.environ.get("VCHARON_HOME") or None


def _xdg(var, default):
    # The XDG spec says to ignore a relative value.
    value = os.environ.get(var)
    if value and posixpath.isabs(value):
        return value
    return posixpath.join(home(), default)


def _windows_dir(var, default):
    return os.environ.get(var) or ntpath.join(home(), "AppData", default)


def config_path():
    """DESIGN §11.1."""
    base = _vcharon_home()
    if base:
        return _path().join(base, "vcharon.ini")
    osn = os_name()
    if osn == "windows":
        return ntpath.join(_windows_dir("APPDATA", "Roaming"), "vcharon", "vcharon.ini")
    if osn == "darwin":
        return posixpath.join(home(), ".config", "vcharon", "vcharon.ini")
    return posixpath.join(_xdg("XDG_CONFIG_HOME", ".config"), "vcharon", "vcharon.ini")


def state_dir():
    base = _vcharon_home()
    if base:
        return _path().join(base, "state")
    osn = os_name()
    if osn == "windows":
        return ntpath.join(_windows_dir("LOCALAPPDATA", "Local"), "vcharon", "state")
    if osn == "darwin":
        return posixpath.join(home(), "Library", "Application Support", "vcharon", "state")
    return posixpath.join(_xdg("XDG_STATE_HOME", ".local/state"), "vcharon", "state")


def log_dir():
    base = _vcharon_home()
    if base:
        return _path().join(base, "logs")
    osn = os_name()
    if osn == "windows":
        return ntpath.join(_windows_dir("LOCALAPPDATA", "Local"), "vcharon", "logs")
    if osn == "darwin":
        return posixpath.join(home(), "Library", "Logs", "vcharon")
    return posixpath.join(_xdg("XDG_STATE_HOME", ".local/state"), "vcharon", "logs")


def joined_dir():
    """The base of the channels' local trees (DESIGN §14 M10), as a channel section's
    mailbox.local spells it. Set per OS, not taken from the state dir: macOS's has a space."""
    base = _vcharon_home()
    if base:
        return _path().join(base, "joined")
    if os_name() == "windows":
        return ntpath.join(_windows_dir("LOCALAPPDATA", "Local"), "vcharon", "joined")
    return "~/.local/state/vcharon/joined"


def is_frozen():
    """True in a PyInstaller binary, which sets both sys.frozen and sys._MEIPASS."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def self_argv():
    """The argv that starts this vcharon again as a child: the binary itself, or this Python
    with -m vcharon. -P keeps a vcharon/ folder in the current directory from shadowing the
    package."""
    if is_frozen():
        return [sys.executable]
    return [sys.executable, "-P", "-m", "vcharon"]


def child_env(env=None):
    """The environment for a child started with self_argv(): a frozen binary's child unpacks
    its own copy, never reusing the parent's folder (PyInstaller 6.9+), so it outlives the
    parent safely."""
    env = dict(os.environ if env is None else env)
    if is_frozen():
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def command_for(executable, folder, osn, which, venv=None):
    """How to run the vcharon in folder (None: no folder part) with executable, as a command line
    for a shell on osn: the executable's base name when which(name) is that same file (python3
    on a Linux box), else its whole path. On Windows both with /, which Git Bash, cmd and py all
    take (Git Bash reads \\ as an escape). A part that needs it is quoted: shlex.quote off
    Windows, "..." on Windows. Not covered, being rare in a Python or vcharon path: "..." doesn't
    stop Git Bash's $ and `, nor cmd's %VAR%, and PowerShell runs a quoted program only after
    &. venv: whether executable is a venv's python (None: this one's), which only that very
    path runs with the venv's packages."""
    pm = ntpath if osn == "windows" else posixpath
    python = executable
    name = pm.basename(executable)
    found = which(name) if name else None
    if venv is None:
        venv = sys.prefix != sys.base_prefix
    if found and (_same_path(found, executable) if venv else _same_file(found, executable)):
        python = name
    parts = [python] + ([folder] if folder else [])
    return _quoted(parts, osn)


def _quoted(parts, osn):
    if osn == "windows":
        parts = [part.replace("\\", "/") for part in parts]
        return " ".join('"%s"' % part if _UNSAFE.search(part) else part for part in parts)
    return " ".join(shlex.quote(part) for part in parts)


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _scripts_dirs():
    """The folders an entry point of this environment can be in: this Python's own folder (a
    venv's bin/ or Scripts\\, pipx's and uv's too), its default scheme's scripts folder, and,
    outside a venv with user site-packages on, the user scheme's (pip install --user). Inside a
    venv the user's ~/.local/bin isn't this environment's: a vcharon there is another
    install."""
    dirs = [os.path.dirname(sys.executable), sysconfig.get_path("scripts")]
    if site.ENABLE_USER_SITE and sys.prefix == sys.base_prefix:
        # 3.10+; only the client asks, and the server's copy of this module never does
        preferred = getattr(sysconfig, "get_preferred_scheme", None)
        try:
            user = preferred("user") if preferred else "%s_user" % os.name
            dirs.append(sysconfig.get_path("scripts", user))
        except (KeyError, ValueError):
            pass
    return [d for d in dirs if d]


# pip's form for an interpreter path too long for a #! line: a /bin/sh script that execs it
_SH_EXEC = re.compile(r"""\A'''exec' ("[^"]+"|\S+) """)


def shebang_python(path):
    """The interpreter an entry-point script at path names on its #! line (or in pip's
    /bin/sh exec form); None when it names none."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    lines = head.splitlines()
    if not lines or not lines[0].startswith("#!"):
        return None
    line = lines[0][2:].strip()
    if line == "/bin/sh" and len(lines) > 1:
        m = _SH_EXEC.match(lines[1])
        return m.group(1).strip('"') if m else None
    if line.startswith('"'):
        return line[1:].partition('"')[0] or None
    return line.split()[0] if line else None


def runs_this_package(found, scripts_dirs=None):
    """Whether the vcharon command at found is an entry point of this environment: a file whose
    real path is in one of this environment's scripts folders, and (off Windows, where it's an
    .exe launcher) whose #! line names this Python, by real path. pipx and uv link
    ~/.local/bin/vcharon to their own venv's bin/vcharon, whose folder holds the venv's python,
    sys.executable."""
    if not os.path.isfile(found):
        return False
    real = os.path.normcase(os.path.dirname(os.path.realpath(found)))
    folders = scripts_dirs if scripts_dirs is not None else _scripts_dirs()
    if not any(real == os.path.normcase(os.path.realpath(folder)) for folder in folders):
        return False
    if os.name == "nt":
        return True
    python = shebang_python(found)
    return python is not None and _same_file(python, sys.executable)


def self_command(which=shutil.which, osn=None):
    """How this vcharon is run on this box, as the start of a command line a person or an
    agent types: `vcharon` when that name on PATH runs this very install (the binary itself,
    or an entry point of this environment), else the binary's full path, or `<python> -P -m
    vcharon`, quoted for this OS's shell. which: shutil.which (tests fake it)."""
    osn = osn or os_name()
    found = which("vcharon")
    if is_frozen():
        if found and _same_file(found, sys.executable):
            return "vcharon"
        return _quoted([sys.executable], osn)
    if found and runs_this_package(found):
        return "vcharon"
    return command_for(sys.executable, None, osn, which) + " -P -m vcharon"


def runnable(text, command=None):
    """text with each `vcharon <command>` as this box runs it (self_command()), for a fix line
    someone runs. command: another spelling (tests). Applying it twice changes nothing more:
    the `-m vcharon` it writes is never matched again."""
    if command is None:
        command = self_command()
    if not command or not text:
        return text
    return _VCHARON_WORD.sub(lambda m: "%s %s" % (command, m.group(1)), text)


def is_wow64():
    """A 32-bit Python on 64-bit Windows."""
    return struct.calcsize("P") == 4 and bool(os.environ.get("PROCESSOR_ARCHITEW6432"))


def default_ssh_path():
    """DESIGN §6.1: Windows' own OpenSSH, never Git for Windows' ssh."""
    if os_name() == "windows":
        root = os.environ.get("SystemRoot") or "C:\\Windows"
        # Windows redirects System32 for 32-bit processes; Sysnative is the real one.
        system = "Sysnative" if is_wow64() else "System32"
        return ntpath.join(root, system, "OpenSSH", "ssh.exe")
    return "/usr/bin/ssh"


def caps():
    """Client capabilities for plugins (DESIGN §13)."""
    osn = os_name()
    if osn == "windows":
        desktop = True
    elif osn == "darwin":
        desktop = not os.environ.get("SSH_CONNECTION")
    else:
        desktop = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return {"desktop": desktop}


def _ioreg_uuid():
    """macOS: IOPlatformUUID, as ioreg prints it; None if it prints none."""
    ran = fsops.run([IOREG, "-rd1", "-c", "IOPlatformExpertDevice"], IOREG_TIMEOUT)
    if ran.rc != 0:
        return None
    found = _IOREG_UUID.search(ran.out.decode("ascii", "replace"))
    return found.group(1) if found else None


def _machine_guid():
    """Windows: MachineGuid under HKLM\\SOFTWARE\\Microsoft\\Cryptography; None if it isn't a
    string."""
    import winreg
    # the 64-bit view, so a 32-bit Python reads the same value as a 64-bit one
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        value, kind = winreg.QueryValueEx(key, "MachineGuid")
    return value if kind == winreg.REG_SZ and isinstance(value, str) else None


def _hashed(uuid):
    """A device id as vcharon keeps it (DESIGN §14 M11a): a record stores the id and a refusal
    prints it, so never the OS's own id; 32 hex digits, as /etc/machine-id has."""
    return hashlib.sha256(("vcharon:" + uuid.lower()).encode("ascii")).hexdigest()[:32]


def machine_id(paths=("/etc/machine-id", "/var/lib/dbus/machine-id"), ioreg=_ioreg_uuid,
               machine_guid=_machine_guid):
    """This machine's id, which ties state to the actual server; None if it has none. Where
    the files are missing, macOS's IOPlatformUUID or Windows' MachineGuid, hashed. ioreg and
    machine_guid: the readers of those (test seams)."""
    test_id = os.environ.get("VCHARON_TEST_MACHINE_ID")
    if test_id:
        return test_id
    for path in paths:
        try:
            with open(path, "rb") as f:
                text = f.read(4096).decode("ascii", "replace").strip()
        except OSError:
            continue
        if _MACHINE_ID.match(text):
            return text
    reader = {"darwin": ioreg, "windows": machine_guid}.get(os_name())
    if reader is None:
        return None
    try:
        uuid = reader()
    except Exception:
        # no ioreg, no winreg, a timeout, a key that isn't there: no id, as on a Linux box
        # without the files
        return None
    if not isinstance(uuid, str) or not _UUID.match(uuid):
        return None
    return _hashed(uuid)


def no_machine_hint():
    """What to do when machine_id() is None on this box."""
    osn = os_name()
    if osn == "darwin":
        return "vcharon couldn't read this Mac's IOPlatformUUID (ioreg): ask the user"
    if osn == "windows":
        return "vcharon couldn't read this box's MachineGuid (the registry): ask the user"
    return None


def distro(paths=("/etc/os-release", "/usr/lib/os-release")):
    """PRETTY_NAME from os-release, or None."""
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read(65536).splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and key.strip() == "PRETTY_NAME":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value or None
        # The first file that exists is the one that counts (os-release(5)).
        return None
    return None


def user():
    try:
        return getpass.getuser()
    except Exception:
        return ""


def python_version():
    return "%d.%d.%d" % sys.version_info[:3]
