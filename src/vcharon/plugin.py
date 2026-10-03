"""Plugin options, the Ctx, the base classes and the registry (DESIGN §9), on both ends."""

from __future__ import annotations

import contextlib
import importlib
import ntpath
import os
import posixpath
import re
import shutil

from . import fsops, pathrules, platform, plugins, stage
from .proto import VCharonError, quote

ENDS = ("local", "remote")
# the levels of a doctor check (DESIGN §13)
LEVELS = ("ok", "warn", "FAIL")

_BOOLS = {"yes": True, "true": True, "1": True, "no": False, "false": False, "0": False}
_DIGITS = re.compile(r"\A[0-9]+\Z")


class Choice:
    """An option kind: exactly one of a few strings."""

    def __init__(self, values):
        self.values = tuple(values)


def choice(*values):
    return Choice(values)


class Option:
    """One plugin option. kind: str, bool, int, list, or a choice(...)."""

    def __init__(self, kind, required=False, default=None):
        self.kind = kind
        self.required = required
        self.default = default


def _no_tick():
    pass


class Ctx:
    """What a plugin object knows about its end (DESIGN §9.1)."""

    def __init__(self, end, log=None, tick=None, home=None, osn=None, caps=None):
        # end: "local" or "remote"; the rest default to this machine
        self.end = end
        self.os = osn or platform.os_name()
        self.caps = platform.caps() if caps is None else caps
        self.home = home or platform.home()
        self.fs = fsops
        self._log = log
        self._tick = tick or _no_tick

    def log(self, msg):
        """Never raises: logging never fails a run."""
        if self._log is None:
            return
        with contextlib.suppress(Exception):
            self._log(msg)

    def tick(self):
        self._tick()

    def _resep(self, path):
        """path with this end's own separators: on a Windows end "a/b" is "a\\b". On any other
        end it's unchanged, where / is the only separator and \\ is part of a name."""
        return path.replace("/", "\\") if self.os == "windows" else path

    def resolve(self, path, where):
        """path with ~ expanded, as an absolute path, by the rules of the end this plugin runs on
        (DESIGN §9.1): that end's path module, and that end's home. A relative path is relative
        to the home on the remote end; on the local end it's refused, since a hotkey run's
        current directory is unpredictable. The process's current directory is never used."""
        mod = ntpath if self.os == "windows" else posixpath
        seps = "/\\" if self.os == "windows" else "/"
        if path == "~":
            path = self.home
        elif path[:1] == "~" and path[1:2] and path[1:2] in seps:
            # "~/" keeps a trailing separator: to the file sink, it doesn't name a file.
            rest = path[2:].lstrip(seps)
            path = mod.join(self.home, self._resep(rest)) if rest else mod.join(self.home, "")
        elif path.startswith("~") and self.os == platform.os_name():
            # "~user": only this machine's user database can answer for another user, and only
            # with this machine's own paths. For another OS's end the text stays a name relative
            # to that end's home, as it does here for a user that doesn't exist.
            path = os.path.expanduser(self._resep(path))
        if pathrules.is_absolute(path, self.os):
            return path
        if self.end == "remote":
            return mod.join(self.home, self._resep(path))
        raise VCharonError("bad_options", "%s: must be an absolute path or start with ~" % where,
                           "fix %s" % where)


class Plugin:
    """What sources and sinks share: their end's Ctx and their options, already converted."""

    remote = False

    def __init__(self, ctx, options):
        self.ctx = ctx
        self.options = options

    def close(self):
        """Releases what the plugin holds; safe to call twice."""

    def doctor(self):
        """vcharon doctor's checks of this side on its own end: [(level, message, hint)], level
        ok, warn or FAIL, hint a string or None. Only reads: never creates a root, a stage
        dir or any file."""
        return []


class Source(Plugin):
    def plan(self, state, full=False):
        """The plan.Plan of what to send. state: what the last successful run saved, {} if
        nothing, or None when the caller keeps no state (a one-off pull). full: plan every path,
        each file with its sha256 (DESIGN §10.5)."""
        raise NotImplementedError

    def open(self, index):
        """A binary reader, with read(n) and close(), for the file put at index."""
        raise NotImplementedError

    def state_after(self, written, deleted):
        """The state to save after a commit that failed having written these puts' paths and
        got through these deletes' paths; None saves nothing. Sources that keep state override
        it."""
        return


class Sink(Plugin):
    def __init__(self, ctx, options):
        Plugin.__init__(self, ctx, options)
        self._done = stage.Done([], 0, [])

    @property
    def done(self):
        """A stage.Done of what the commit did so far: read it after a failed commit."""
        return self._done

    def check(self, plan):
        """Refuses the plan with a VCharonError, or returns a stage.Checked. Only reads."""
        raise NotImplementedError

    def stage(self, index, reader):
        """Stores one file put's bytes; the root isn't touched yet."""
        raise NotImplementedError

    def commit(self):
        """Applies the plan; returns a stage.Done."""
        raise NotImplementedError

    def abort(self):
        """Drops what was staged."""
        raise NotImplementedError


class StagerSink(Sink):
    """A sink that applies its plan through one stage.Stager: a subclass's check() calls
    make_stager() and returns self.stager.check(); stage, commit, abort, close and done go to
    it. Before check(), abort() and close() do nothing (the engine may abort a sink whose
    source failed to plan)."""

    def __init__(self, ctx, options):
        Sink.__init__(self, ctx, options)
        self.stager = None

    def make_stager(self, root, create=False, max_deletes=0):
        if self.stager is not None:
            raise VCharonError("internal", "check() runs once per sink")
        self.stager = stage.Stager(root, create=create, max_deletes=max_deletes,
                                   tick=self.ctx.tick, log=self.ctx.log)
        return self.stager

    @property
    def done(self):
        if self.stager is None:
            return self._done
        return self.stager.done

    def _need_stager(self, what):
        if self.stager is None:
            raise VCharonError("internal", "%s() came before check()" % what)

    def stage(self, index, reader):
        self._need_stager("stage")
        self.stager.stage(index, reader)

    def commit(self):
        self._need_stager("commit")
        return self.stager.commit()

    def abort(self):
        if self.stager is not None:
            self.stager.abort()

    def close(self):
        if self.stager is not None:
            self.stager.close()


def load(name):
    """The plugin module called name; only the ones plugins.ALL lists."""
    if name not in plugins.ALL:
        raise VCharonError("config", "unknown plugin %s" % pathrules.show(name),
                           "the plugins are: %s" % ", ".join(plugins.ALL))
    # A module name can't hold "-": a name like a-b would live in a_b.py.
    module = importlib.import_module("vcharon.plugins." + name.replace("-", "_"))
    if getattr(module, "NAME", None) != name:
        raise VCharonError("internal", "the plugin module %s calls itself %s"
                           % (name, quote(getattr(module, "NAME", None))))
    return module


def _convert(option, value, where):
    hint = "fix %s" % where
    kind = option.kind
    if kind is str:
        if option.required and not value:
            raise VCharonError("bad_options", "%s: can't be empty" % where, hint)
        return value
    if kind is bool:
        if value.lower() not in _BOOLS:
            raise VCharonError("bad_options", "%s: must be yes or no" % where, hint)
        return _BOOLS[value.lower()]
    if kind is int:
        try:
            if _DIGITS.match(value):
                return int(value)
        except ValueError:
            # more digits than int() takes (Python 3.11 and later)
            pass
        raise VCharonError("bad_options", "%s: must be a whole number, 0 or more" % where, hint)
    if kind is list:
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(kind, Choice):
        if value not in kind.values:
            raise VCharonError("bad_options", "%s: must be one of %s"
                               % (where, ", ".join(kind.values)), hint)
        return value
    raise VCharonError("internal", "%s has an unknown kind %r" % (where, kind))


def parse_options(module, role, raw):
    """The options of a plugin in a role, converted from the raw strings the config and the
    callers give; they may have come over the wire, so nothing about them is trusted."""
    if (not isinstance(raw, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items())):
        raise VCharonError("protocol", "the %s plugin's options aren't strings by name: %s"
                           % (module.NAME, quote(raw)))
    prefix = "from." if role == "source" else "to."
    for key in raw:
        if key not in module.OPTIONS:
            raise VCharonError("bad_options", "%s: unknown option" % pathrules.show(prefix + key),
                               "the %s plugin's options are: %s"
                               % (module.NAME, ", ".join(
                                   k for k in module.OPTIONS
                                   if k not in getattr(module, "HIDDEN", ()))))
    options = {}
    for key, option in module.OPTIONS.items():
        where = prefix + key
        if key in raw:
            options[key] = _convert(option, raw[key], where)
        elif option.required:
            raise VCharonError("bad_options", "%s: required" % where, "fix %s" % where)
        elif isinstance(option.default, list):
            # a copy: a default list is never shared between two parses
            options[key] = list(option.default)
        else:
            options[key] = option.default
    check = getattr(module, "check_options", None)
    if check is not None:
        check(options)
    return options


def _checked(end, name, role, raw):
    module = load(name)
    if role not in module.KINDS:
        raise VCharonError("config", "%s can't be a %s" % (name, role),
                           "%s can be: %s" % (name, ", ".join(module.KINDS)))
    if end not in ENDS or end not in module.ENDS:
        raise VCharonError("config", "%s can't run on the %s end" % (name, end),
                           "%s runs on: %s" % (name, ", ".join(module.ENDS)))
    options = parse_options(module, role, raw)
    if end == "local":
        have = platform.caps()
        for cap in module.CAPS:
            if not have.get(cap):
                raise VCharonError("missing_capability", "%s needs %s, which this client lacks"
                                   % (name, cap), "run it on a client that has %s" % cap)
    return module, options


def check_side(end, name, role, raw):
    """Every check a side can have before anything runs; the controller calls it before it
    connects. Returns the converted options."""
    return _checked(end, name, role, raw)[1]


def make(end, name, role, raw, ctx):
    """A Source or Sink object, after check_side()'s checks: the helper runs them too, since
    neither end trusts the other."""
    if ctx.end != end:
        raise VCharonError("internal", "a %s plugin with a %s Ctx" % (end, ctx.end))
    module, options = _checked(end, name, role, raw)
    cls = module.Source if role == "source" else module.Sink
    return cls(ctx, options)


def doctor(end, name, role, raw, ctx):
    """vcharon doctor's checks of one side on its own end (DESIGN §9.1, §13): make()'s checks,
    which raise, then the NEEDS commands, then the plugin's own doctor()."""
    if ctx.end != end:
        raise VCharonError("internal", "a %s plugin with a %s Ctx" % (end, ctx.end))
    module, options = _checked(end, name, role, raw)
    checks = []
    for cmd in module.NEEDS:
        found = shutil.which(cmd)
        if found:
            checks.append(("ok", "%s: %s" % (cmd, found), None))
        else:
            checks.append(("FAIL", "%s isn't on this end's PATH" % cmd,
                           "install %s on the %s end" % (cmd, end)))
    cls = module.Source if role == "source" else module.Sink
    obj = cls(ctx, options)
    try:
        checks.extend(tuple(check) for check in obj.doctor())
    finally:
        obj.close()
    return checks


def checks_to_json(checks):
    """plugin.doctor's result on the wire."""
    return {"checks": [[level, message, hint] for level, message, hint in checks]}


def checks_from_json(obj):
    """The checks of a plugin.doctor result; anything but exactly its shape is protocol."""
    checks = obj.get("checks") if isinstance(obj, dict) and set(obj) == {"checks"} else None
    good = isinstance(checks, list) and all(
        isinstance(c, list) and len(c) == 3 and isinstance(c[0], str) and c[0] in LEVELS
        and isinstance(c[1], str) and (c[2] is None or isinstance(c[2], str))
        for c in checks)
    if not good:
        raise VCharonError("protocol", "a malformed plugin.doctor result: %s" % quote(obj))
    return [(c[0], c[1], c[2]) for c in checks]
