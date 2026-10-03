"""vcharon's tests: python -m unittest discover -s tests -t . -v

A sandbox for the whole suite, set when this package is imported, before any test: HOME and
the OS's config and state folders point into a temp folder, removed at exit, so a test that
loses its own environment (a patcher stopped too early) still can't reach the user's real
folders. It's set in os.environ itself, not through a patcher, so no test
can undo it, and every child a test starts inherits it. VCHARON_HOME and VCHARON_CHANNELS_ROOT
are each test's own to set. Not with VCHARON_TEST_SSH: the real-ssh tests need the real home
(its ~/.ssh).

Then a guard: the functions that name vcharon's folders raise an AssertionError for a path
under the real home that isn't under the temp folder (on Windows the temp folder is under the
home)."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

REAL_HOME = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
TEMP = os.path.normcase(os.path.realpath(tempfile.gettempdir()))

if not os.environ.get("VCHARON_TEST_SSH"):
    _sandbox = tempfile.mkdtemp(prefix="vcharon-suite-")
    atexit.register(shutil.rmtree, _sandbox, True)
    _home = os.path.join(_sandbox, "home")
    os.makedirs(_home)
    os.environ.update(HOME=_home, USERPROFILE=_home,
                      XDG_CONFIG_HOME=os.path.join(_home, ".config"),
                      XDG_STATE_HOME=os.path.join(_home, ".local", "state"),
                      APPDATA=os.path.join(_home, "AppData", "Roaming"),
                      LOCALAPPDATA=os.path.join(_home, "AppData", "Local"))

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
