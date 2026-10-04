"""vcharon's tests: python -m unittest discover -s tests -t . -v

A sandbox for the whole suite, set when this package is imported, before any test: HOME and
the OS's config and state folders point into a temp folder, removed at exit, so a test that
loses its own environment (a patcher stopped too early) still can't reach the user's real
folders. It's set in os.environ itself, not through a patcher, so no test
can undo it, and every child a test starts inherits it. The shell's VCHARON_HOME and
VCHARON_CHANNELS_ROOT are dropped, so their defaults fall in the sandbox too: a developer's
hand-run scratch folder is no test's to write. A test sets them for itself. The sandbox is on
for every run, the real-ssh tests included: OpenSSH reads ~/.ssh from the account's home, not
$HOME.

Then a guard: the functions that name vcharon's folders raise an AssertionError for a path
under the real home that isn't under the temp folder (on Windows the temp folder is under the
home)."""

from __future__ import annotations

import atexit
import faulthandler
import os
import shutil
import sys
import tempfile

# On CI, a hung suite prints every thread's stack and fails, inside the job's time limit, so
# the log shows where it hung instead of only that the job was cancelled.
CI_HANG_SECONDS = 15 * 60
if os.environ.get("CI"):
    faulthandler.dump_traceback_later(CI_HANG_SECONDS, exit=True, file=sys.stderr)

REAL_HOME = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
TEMP = os.path.normcase(os.path.realpath(tempfile.gettempdir()))

_sandbox = tempfile.mkdtemp(prefix="vcharon-suite-")
atexit.register(shutil.rmtree, _sandbox, True)
_home = os.path.join(_sandbox, "home")
os.makedirs(_home)
os.environ.update(HOME=_home, USERPROFILE=_home,
                  XDG_CONFIG_HOME=os.path.join(_home, ".config"),
                  XDG_STATE_HOME=os.path.join(_home, ".local", "state"),
                  APPDATA=os.path.join(_home, "AppData", "Roaming"),
                  LOCALAPPDATA=os.path.join(_home, "AppData", "Local"))
for _name in ("VCHARON_HOME", "VCHARON_CHANNELS_ROOT"):
    os.environ.pop(_name, None)

from vcharon import channels, platform, skill


def _under(path, top):
    return path == top or path.startswith(top.rstrip(os.sep) + os.sep)


def _guarded(fn):
    """fn, raising an AssertionError when what it returns is a path in the real home."""
    def check(*args, **kwargs):
        out = fn(*args, **kwargs)
        if isinstance(out, str):
            path = os.path.expanduser(out)
            if os.path.isabs(path):
                real = os.path.normcase(os.path.realpath(path))
                if _under(real, REAL_HOME) and not _under(real, TEMP):
                    raise AssertionError("a test reached the real home: %s() = %s"
                                         % (fn.__name__, out))
        return out
    check.__name__ = fn.__name__
    check.__wrapped__ = fn
    return check


for _module, _names in ((platform, ("config_path", "state_dir", "log_dir", "joined_dir")),
                        (channels, ("root_path",)), (skill, ("path",))):
    for _name in _names:
        setattr(_module, _name, _guarded(getattr(_module, _name)))
