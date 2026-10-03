"""How this vcharon was installed, and whether its code changed under a long-running command
(DESIGN, "Self-update" and "Running watchers"); client.

Kept apart from update.py, which only vcharon --update imports: this module is light, and
every command imports it (doctor's install line, the watchers' check, the start-up sweep of
a Windows binary's old copies)."""

from __future__ import annotations

import dataclasses
import os
import sys

from . import platform

# Distribution is GitHub only. The same repo is named in install.sh and install.ps1; the
# three change together.
REPO = "zhoufanscut/VCharon"
RELEASES_URL = "https://github.com/%s/releases" % REPO
GIT_SPEC = "git+https://github.com/%s" % REPO
INSTALL_SH = "curl -fsSL https://raw.githubusercontent.com/%s/main/install.sh | sh" % REPO
INSTALL_PS1 = "irm https://raw.githubusercontent.com/%s/main/install.ps1 | iex" % REPO
# What a Windows update renames the running binary to: <name>.old-<unix time>. A running .exe
# can't be replaced or deleted, but it can be renamed; the copy is deleted at a later start.
OLD_MARK = ".old-"
# A long-running command's exit code, and the watcher's line, when vcharon changed under it.
EXIT_UPDATED = 14


@dataclasses.dataclass(frozen=True)
class Install:
    """Where this vcharon lives and who owns it. kind: "binary" (a PyInstaller one-file
    binary, the one kind vcharon --update replaces), "pipx", "uv", "pip" (any other Python
    environment) or "source" (a checkout, installed editable). path: the file --update would
    replace (binary) or the checkout's root (source); None for the others."""

    kind: str
    path: str | None = None

    @property
    def self_updatable(self):
        return self.kind == "binary"

    def command_for(self, tag):
        """The command that updates this install to tag. Pinned to the tag: pipx and pip
        given a git spec without one resolve the default branch, which may not be the release
        just reported, and pip may call an unchanged version satisfied."""
        spec = '"%s@%s"' % (GIT_SPEC, tag)
        if self.kind == "pipx":
            return "pipx install --force %s" % spec
        if self.kind == "uv":
            return "uv tool install --force %s" % spec
        if self.kind == "source":
            # quoted: a checkout path with a space would become two arguments
            root = platform.quote_command([self.path or "."])
            return "git -C %s fetch --tags && git -C %s checkout %s" % (root, root, tag)
        if self.kind == "binary":
            return "vcharon --update"
        return "%s -m pip install --upgrade %s" % (platform.quote_command([sys.executable]),
                                                   spec)


def detect():
    """The running vcharon's Install. Never raises: a layout it can't name is "pip", whose
    command (this Python's pip) is right for any environment."""
    # PyInstaller sets both sys.frozen and sys._MEIPASS; sys.executable is then the binary
    # itself. frozen alone is set by other freezers and embedders too, and what --update does
    # with a "binary" is replace sys.executable: taking a real interpreter for it would
    # overwrite that. realpath: a symlinked ~/.local/bin/vcharon updates the real file.
    if platform.is_frozen():
        return Install("binary", os.path.realpath(sys.executable))
    # a checkout first: one installed editable into any environment (a pipx or uv one too) is
    # updated with git, not by reinstalling the environment
    root = source_checkout()
    if root is not None:
        return Install("source", root)
    prefix_parts = os.path.realpath(sys.prefix).split(os.sep)
    # pipx's venvs are $PIPX_HOME/venvs/<name>, under ~/.local/pipx or ~/.local/share/pipx
    if "pipx" in prefix_parts:
        return Install("pipx")
    # uv tool install's …/uv/tools/<name>, and a uvx run's …/uv/archive-v0/<hash>; uv's
    # environments often have no pip, so "pip" would print a command that can't run
    if "uv" in prefix_parts:
        return Install("uv")
    return Install("pip")


def source_checkout():
    """The repo's root when this vcharon runs from a checkout (pip install -e .): the package
    in <root>/src/vcharon, with pyproject.toml and .git beside src; else None."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    if os.path.basename(os.path.dirname(here)) != "src":
        return None
    if (os.path.isfile(os.path.join(root, "pyproject.toml"))
            and os.path.exists(os.path.join(root, ".git"))):
        return root
    return None


def code_path():
    """The file whose change means vcharon was replaced under a running process: a binary's
    own file, or the package's __init__.py (pipx install --force and pip rewrite it)."""
    if platform.is_frozen():
        return sys.executable
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")


def identity(path):
    """(size, modification time in ns, file id) of path, following links; None when it can't
    be read (a swap under way, a folder removed). os.stat's st_ino is the file id on Windows
    too."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, st.st_ino


class Watchdog:
    """A long-running command's check that its code is the one it started with (DESIGN,
    "Running watchers"): its identity at start, compared again at the top of each round, and
    after an unexpected exception in a binary."""

    def __init__(self, path=None):
        self.path = code_path() if path is None else path
        self.start = identity(self.path)

    def changed(self):
        """Whether the code's file is another one now, or gone."""
        return identity(self.path) != self.start

    def after_crash(self):
        """Whether an unexpected exception may come from an update: a one-file binary reads
        its code from its own file, by path, at each first-time import, so after a swap that
        read gets the new file at the old offsets (an ImportError, a zlib error, bad marshal
        data). Only a binary: an installed package's files are whole files either way."""
        return platform.is_frozen() and self.changed()


def sweep_old(executable=None):
    """Deletes each <binary>.old-* next to the running binary that it can, as every start of
    a Windows binary does: those are what earlier updates renamed the running binary to. One
    still running can't be deleted, and is left for a later start."""
    path = os.path.realpath(executable or sys.executable)
    folder, name = os.path.split(path)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for one in names:
        if one.startswith(name + OLD_MARK):
            try:
                os.remove(os.path.join(folder, one))
            except OSError:
                pass
