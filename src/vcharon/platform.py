"""OS name, standard dirs, capabilities and facts about this machine, on both ends."""

from __future__ import annotations

import errno
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
import tempfile
import time

from . import fsops, pathrules
from .proto import VCharonError

if os.name == "nt":
    # at start, not when the machine id is first read: a long-running command imports nothing
    # after it starts (DESIGN, "Running watchers")
    import winreg
else:
    winreg = None

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
COMMANDS = ("setup", "key", "doctor", "ping", "list", "create", "join", "leave", "close", "whoami",
            "post", "read", "watch", "sync", "guide", "skill", "--version", "--help")
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


def os_word():
    """This OS as a member's name and MEMBER.md spell it: mac, win or linux. Never a host
    name: one can name an employer or a network."""
    return {"darwin": "mac", "windows": "win"}.get(os_name(), "linux")


def _path():
    # By os_name() rather than os.path, so each OS's paths can be tested on any OS.
    return ntpath if os_name() == "windows" else posixpath


def home():
    return os.path.expanduser("~")


def _vcharon_home():
    value = os.environ.get("VCHARON_HOME")
    if not value:
        return None
    path = os.path.expanduser(value)
    # A relative folder would follow the current directory: records would land in whatever
    # checkout a command runs from, and a channel section's mailbox.local would be refused.
    # os_name() is this machine's OS outside tests; a test that declares another OS may give
    # either OS's form, so either is taken.
    if not (os.path.isabs(path) or _path().isabs(path)):
        example = "C:\\vc\\home" if os.name == "nt" else "/tmp/vc/home"
        # as typed: %r would double a Windows path's backslashes
        raise VCharonError("config", "VCHARON_HOME is '%s', not an absolute folder"
                           % pathrules.printable(value),
                           "set VCHARON_HOME to a full path, such as %s" % example)
    return path


def _xdg(var, default):
    # The XDG spec says to ignore a relative value.
    value = os.environ.get(var)
    if value and posixpath.isabs(value):
        return value
    return posixpath.join(home(), default)


def _windows_dir(var, default):
    return os.environ.get(var) or ntpath.join(home(), "AppData", default)


def config_path():
    """(DESIGN, "Where files live")."""
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
    """The base of the channels' local trees, as a channel section's
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


def quote_command(parts, osn=None):
    """parts as one command line for this OS's shell (osn: another OS), as _quoted spells
    them."""
    return _quoted(parts, osn or os_name())


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


# what Windows takes for PATHEXT when it isn't set
PATHEXT_DEFAULT = ".COM;.EXE;.BAT;.CMD"


def vcharons_on_path(path=None, osn=None):
    """Every vcharon command on PATH, in PATH's order, each file once (by its real path): on
    Windows a file named vcharon plus one of PATHEXT's extensions (Windows runs by extension),
    elsewhere a file named vcharon that may be run. An empty PATH entry is skipped: it names
    the folder doctor runs in, not the one an agent will later run vcharon in. A literal ~ in
    an entry isn't expanded, nor a quoted Windows entry unquoted. path: PATH's text, osn: the
    OS (tests)."""
    osn = osn or os_name()
    if path is None:
        path = os.environ.get("PATH", "")
    names = ["vcharon"]
    if osn == "windows":
        names = []
        for ext in (os.environ.get("PATHEXT") or PATHEXT_DEFAULT).split(";"):
            name = "vcharon" + ext.strip().lower()
            if ext.strip() and name not in names:
                names.append(name)
    found, seen = [], set()
    for folder in path.split(os.pathsep):
        if not folder:
            continue
        for name in names:
            candidate = os.path.join(folder, name)
            if not os.path.isfile(candidate):
                continue
            if osn != "windows" and not os.access(candidate, os.X_OK):
                continue
            real = os.path.normcase(os.path.realpath(candidate))
            if real not in seen:
                seen.add(real)
                found.append(candidate)
    return found


def typed_vcharon(which=shutil.which, osn=None, path=None):
    """The file a typed vcharon runs: which's answer, but on Windows never one found only
    through the current folder (cmd.exe searches it first; Git Bash and PowerShell, where
    agents run commands, never do): then the first on PATH (vcharons_on_path), or None.
    path: PATH's text (tests)."""
    found = which("vcharon")
    if not found or (osn or os_name()) != "windows":
        return found
    here = os.path.normcase(os.path.realpath(os.getcwd()))
    if os.path.normcase(os.path.realpath(os.path.dirname(os.path.abspath(found)))) != here:
        return found
    if path is None:
        path = os.environ.get("PATH", "")
    if any(entry and os.path.normcase(os.path.realpath(entry)) == here
           for entry in path.split(os.pathsep)):
        return found
    on_path = vcharons_on_path(path, "windows")
    return on_path[0] if on_path else None


def self_command(which=shutil.which, osn=None):
    """How this vcharon is run on this box, as the start of a command line a person or an
    agent types: `vcharon` when that name on PATH runs this very install (the binary itself,
    or an entry point of this environment; typed_vcharon), else the binary's full path, or
    `<python> -P -m vcharon`, quoted for this OS's shell. which: shutil.which (tests fake
    it)."""
    osn = osn or os_name()
    found = typed_vcharon(which, osn)
    if found and runs_this(found):
        return "vcharon"
    if is_frozen():
        return _quoted([sys.executable], osn)
    return command_for(sys.executable, None, osn, which) + " -P -m vcharon"


def runs_this(found):
    """Whether the vcharon command at found runs this very install: the binary itself, or an
    entry point of this environment (runs_this_package)."""
    if is_frozen():
        return _same_file(found, sys.executable)
    return runs_this_package(found)


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
    """Windows' own OpenSSH (DESIGN, "The ssh command"), never Git for Windows' ssh."""
    if os_name() == "windows":
        root = os.environ.get("SystemRoot") or "C:\\Windows"
        # Windows redirects System32 for 32-bit processes; Sysnative is the real one.
        system = "Sysnative" if is_wow64() else "System32"
        return ntpath.join(root, system, "OpenSSH", "ssh.exe")
    return "/usr/bin/ssh"


def caps():
    """Client capabilities for plugins (DESIGN, "Plugin interface")."""
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
    string. Elsewhere winreg is None, and this raises."""
    # the 64-bit view, so a 32-bit Python reads the same value as a 64-bit one
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        value, kind = winreg.QueryValueEx(key, "MachineGuid")
    return value if kind == winreg.REG_SZ and isinstance(value, str) else None


def _hashed(uuid):
    """A device id as vcharon keeps it: a record stores the id and a refusal
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
    except Exception:  # noqa: BLE001
        # no ioreg, no winreg, a timeout, a key that isn't there: no id, as on a Linux box
        # without the files
        return None
    if not isinstance(uuid, str) or not _UUID.match(uuid):
        return None
    return _hashed(uuid)


CLIENT_ID_FILE = "client-id"
# where a client-id file's id came from: its second line
FROM_MACHINE = "from the machine id"
FROM_RANDOM = "random"
# an empty client-id file is another run writing it (or was, before the link): read again
# this many times, this far apart, before refusing it
CLIENT_ID_TRIES = 5
CLIENT_ID_PAUSE = 0.1


def client_id_path():
    return _path().join(state_dir(), CLIENT_ID_FILE)


def _read_client_id(path):
    """(id, origin) of the client-id file, or None when it's missing; an empty file is read
    again a few times (a writer racing us), then refused like any other malformed one."""
    hint = ("if your user confirms it, delete it; this machine then looks new to the "
            "channels it is in")
    for left in range(CLIENT_ID_TRIES - 1, -1, -1):
        try:
            with open(path, "rb") as f:
                text = f.read(256).decode("ascii", "replace")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise fsops.error(e, path)
        if text.strip() or not left:
            break
        time.sleep(CLIENT_ID_PAUSE)
    lines = text.split("\n")
    cid = lines[0].strip()
    if not _MACHINE_ID.match(cid):
        raise VCharonError("config", "%s isn't a client id (32 hex digits)" % path, hint)
    origin = lines[1].strip() if len(lines) > 1 else ""
    return cid, origin if origin in (FROM_MACHINE, FROM_RANDOM) else "unknown"


# os.link's errors on a file system without hard links (FAT, exFAT, some SMB and FUSE mounts)
NO_LINK_ERRNOS = frozenset(getattr(errno, name) for name in (
    "EPERM", "ENOTSUP", "EOPNOTSUPP", "EXDEV", "ENOSYS") if hasattr(errno, name))


def _create_excl(path, data):
    """path made with O_EXCL and data written into it: FileExistsError when it's there. A
    reader that comes between the create and the write sees it empty, and reads again
    (_read_client_id)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def client_id(make=True):
    """(this client's id, where it came from): always the state dir's client-id file, made at
    first use from machine_id() (sha256 of "vcharon:" and it, 32 hex digits) or, on a Linux
    box with no machine id, at random; a Mac or Windows box always has one, so there a missing
    id is a failed read, refused. After that the file alone counts, so a machine id that turns up
    later, or one ioreg or the registry fails to give once, changes nothing; a wiped state
    dir gets the same id back while the machine id is there. Only a hash of it with a
    channel's name ever leaves this machine (claimer). make=False (doctor, which changes
    nothing): (None, where a new one would come from) when the file isn't there yet. A
    malformed file is an error: a new id would make this machine look like another one."""
    path = client_id_path()
    found = _read_client_id(path)
    if found is not None:
        return found
    mid = machine_id()
    if mid:
        cid = hashlib.sha256(("vcharon:" + mid).encode("ascii")).hexdigest()[:32]
        origin = FROM_MACHINE
    elif os_name() in ("darwin", "windows"):
        # these always have one: None is a read that failed this time, and a random id made
        # now would be kept for good
        raise VCharonError("io", "couldn't read this machine's id (%s), so no client id is "
                           "made" % ("ioreg's IOPlatformUUID" if os_name() == "darwin"
                                     else "the registry's MachineGuid"),
                           "run the command again; if it keeps failing, your user can write "
                           "%s by hand: 32 hex digits (0-9, a-f), then a line: %s"
                           % (path, FROM_RANDOM))
    else:
        # a Linux box without /etc/machine-id (some containers)
        cid, origin = os.urandom(16).hex(), FROM_RANDOM
    if not make:
        return None, origin
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=folder, prefix=".client-id-", suffix=".tmp")
    except OSError as e:
        raise fsops.error(e, folder)
    try:
        data = ("%s\n%s\n" % (cid, origin)).encode("ascii")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # the whole file appears at once, and never over another run's: a hard link fails
        # when path exists; Windows' rename does too. By this host's own OS: a POSIX rename
        # would replace the file.
        try:
            if fsops.WINDOWS:
                os.rename(temp, path)
            else:
                try:
                    os.link(temp, path)
                except OSError as e:
                    if e.errno not in NO_LINK_ERRNOS:
                        raise
                    _create_excl(path, data)
        except FileExistsError:
            pass
    except OSError as e:
        raise fsops.error(e, path)
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass
    # ours, or the one another run linked first
    found = _read_client_id(path)
    if found is None:
        raise VCharonError("io", "couldn't make %s" % path, "run the command again")
    return found


def claimer(channel, make=True):
    """The claimer id this machine writes into MEMBER.md in channel: sha256 of "<channel>:"
    and client_id(), 16 hex digits. With the channel's name in the hash, no raw id reaches a
    shared file, and one machine looks different in different channels."""
    cid, _ = client_id(make)
    if cid is None:
        return None
    return hashlib.sha256(("%s:%s" % (channel, cid)).encode("utf-8")).hexdigest()[:16]


# The agent that runs vcharon, from what each one sets in the shells it starts; only what its
# own docs or source say. More than one found (one agent started inside another's shell) gives
# None: the user passes --agent.
AGENTS = ("claude", "codex", "opencode", "other")
AGENT_ENV = (
    # Claude Code: "Set to `1` in subprocesses Claude Code spawns (Bash and PowerShell tools,
    # ...)", https://code.claude.com/docs/en/env-vars (CLAUDECODE)
    ("claude", "CLAUDECODE"),
    # Codex CLI: its shell tool sets CODEX_THREAD_ID (codex-rs/core/src/unified_exec/
    # process_manager.rs, open_session_with_sandbox; codex-rs/protocol/src/
    # shell_environment.rs), https://github.com/openai/codex/blob/
    # 9d2b60303e83198905604e704116daa8998c3c47/codex-rs/protocol/src/shell_environment.rs
    ("codex", "CODEX_THREAD_ID"),
    # OpenCode: `process.env.OPENCODE = "1"` at start (packages/opencode/src/index.ts), and its
    # shell tool passes process.env on (packages/opencode/src/tool/shell.ts, shellEnv),
    # https://github.com/anomalyco/opencode/blob/c42ae0d56b6f86f8df39d451d6d2cfe6414b3928/
    # packages/opencode/src/index.ts
    ("opencode", "OPENCODE"),
)


def detect_agent(env=None):
    """The agent whose variable is set in env (os.environ), or None: none, or several."""
    env = os.environ if env is None else env
    found = [agent for agent, var in AGENT_ENV if env.get(var)]
    return found[0] if len(found) == 1 else None


def no_machine_hint():
    """What to do when machine_id() is None on this box: on a Mac or Windows, the command
    that shows the id vcharon reads; None elsewhere."""
    osn = os_name()
    if osn == "darwin":
        return ("check that /usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice prints an "
                "IOPlatformUUID line, then try again; if it prints none, ask your user")
    if osn == "windows":
        return ("check that reg query HKLM\\SOFTWARE\\Microsoft\\Cryptography /v MachineGuid "
                "/reg:64 prints a MachineGuid, then try again; if it prints none, ask your user")
    return None


OS_RELEASE = ("/etc/os-release", "/usr/lib/os-release")
# the server distro vcharon is tested on: Debian, from this VERSION_ID on (only a warning
# elsewhere)
TESTED_DISTRO = ("debian", 13)


def os_release(paths=OS_RELEASE):
    """{"PRETTY_NAME", "ID", "VERSION_ID"} from os-release, each None when missing or empty;
    None when no file can be read. The first file that exists is the one that counts
    (os-release(5))."""
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read(65536).splitlines()
        except OSError:
            continue
        out = dict.fromkeys(("PRETTY_NAME", "ID", "VERSION_ID"))
        for line in lines:
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and key in out and out[key] is None:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                out[key] = value or None
        return out
    return None


def distro(paths=OS_RELEASE):
    """PRETTY_NAME from os-release, or None."""
    found = os_release(paths)
    return found["PRETTY_NAME"] if found else None


def distro_warning(hello):
    """None for a server whose hello says Debian 13 or later (distro_id, distro_version);
    else the warning's text, with what was found. Never a refusal."""
    distro_id, version = hello.get("distro_id"), hello.get("distro_version")
    major = None
    if isinstance(version, str):
        head = version.split(".")[0]
        major = int(head) if head.isascii() and head.isdigit() and len(head) < 6 else None
    if distro_id == TESTED_DISTRO[0] and major is not None and major >= TESTED_DISTRO[1]:
        return None
    if not isinstance(distro_id, str) and not isinstance(version, str):
        found = "its os-release names no distro (no ID=)"
    else:
        found = "it is ID=%s VERSION_ID=%s" % (distro_id or "?", version or "?")
    pretty = hello.get("distro")
    if isinstance(pretty, str) and pretty:
        found += " (%s)" % pretty
    return "VCharon is tested on Debian 13 or later; %s" % found


def user():
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def python_version():
    return "%d.%d.%d" % sys.version_info[:3]
