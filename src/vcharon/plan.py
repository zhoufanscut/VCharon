"""Plan types, their JSON form and their structure checks (DESIGN, "Plans"), on both ends."""

from __future__ import annotations

import dataclasses
import math
import re

from .proto import VCharonError, quote


@dataclasses.dataclass
class Entry:
    """One step of a plan. kind, size, mtime and executable are for puts; tree and why for
    deletes. executable is JSON "exec"; None means the source left it out (a source on
    Windows), so the execute bits stay as they are. sha256 is for file puts in a --full run:
    64 lowercase hex digits, or None."""

    op: str
    path: str
    kind: str = ""
    size: int = 0
    mtime: float = 0.0
    executable: bool | None = None
    tree: bool = False
    why: str = ""
    sha256: str | None = None


@dataclasses.dataclass
class Plan:
    entries: list
    identity: dict | None = None
    state: dict | None = None
    notes: list = dataclasses.field(default_factory=list)


def put_file(path, size, mtime, executable=None, sha256=None):
    return Entry("put", path, kind="file", size=size, mtime=mtime, executable=executable,
                 sha256=sha256)


def put_dir(path):
    return Entry("put", path, kind="dir")


def delete(path, tree=False, why=""):
    return Entry("delete", path, tree=tree, why=why)


def to_json(plan):
    entries = []
    for e in plan.entries:
        if e.op == "put" and e.kind == "file":
            obj = {"op": "put", "path": e.path, "kind": "file", "size": e.size,
                   "mtime": e.mtime}
            if e.executable is not None:
                obj["exec"] = e.executable
            if e.sha256 is not None:
                obj["sha256"] = e.sha256
        elif e.op == "put":
            obj = {"op": "put", "path": e.path, "kind": e.kind}
        else:
            obj = {"op": "delete", "path": e.path}
            if e.tree:
                obj["tree"] = True
            if e.why:
                obj["why"] = e.why
        entries.append(obj)
    return {"entries": entries, "identity": plan.identity, "state": plan.state,
            "notes": list(plan.notes)}


def _bad(what):
    return VCharonError("protocol", "a malformed plan: %s" % what)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def good_sha256(value):
    return isinstance(value, str) and _SHA256.match(value) is not None


# Seconds either side of 1970 an mtime may be: about 3,000 years, far inside what
# round(mtime * 1e9) and the OS can take.
MAX_MTIME = 1e11


def good_mtime(value):
    if not (_is_int(value) or isinstance(value, float)):
        return False
    try:
        # an int like 10**400 overflows a float
        return math.isfinite(value) and abs(value) <= MAX_MTIME
    except OverflowError:
        return False


def _keys(obj, i, required, optional=()):
    missing = [k for k in required if k not in obj]
    if missing:
        raise _bad("entry %d has no %s" % (i, ", ".join(missing)))
    extra = sorted(set(obj) - set(required) - set(optional))
    if extra:
        raise _bad("entry %d has unknown keys: %s" % (i, quote(extra)))


def _entry(obj, i):
    if not isinstance(obj, dict):
        raise _bad("entry %d isn't an object: %s" % (i, quote(obj)))
    op = obj.get("op")
    if op == "put":
        kind = obj.get("kind")
        if kind == "file":
            _keys(obj, i, ("op", "path", "kind", "size", "mtime"), ("exec", "sha256"))
        elif kind == "dir":
            _keys(obj, i, ("op", "path", "kind"))
        else:
            raise _bad("entry %d has an unknown kind %s" % (i, quote(kind)))
    elif op == "delete":
        _keys(obj, i, ("op", "path"), ("tree", "why"))
    else:
        raise _bad("entry %d has an unknown op %s" % (i, quote(op)))
    path = obj["path"]
    if not isinstance(path, str):
        raise _bad("entry %d has a path that isn't a string: %s" % (i, quote(path)))
    if op == "delete":
        tree = obj.get("tree", False)
        why = obj.get("why", "")
        if not isinstance(tree, bool):
            raise _bad("entry %d has a tree that isn't true or false" % i)
        if not isinstance(why, str):
            raise _bad("entry %d has a why that isn't a string" % i)
        return Entry("delete", path, tree=tree, why=why)
    if kind == "dir":
        return Entry("put", path, kind="dir")
    size, mtime, executable = obj["size"], obj["mtime"], obj.get("exec")
    sha256 = obj.get("sha256")
    if not _is_int(size) or size < 0:
        raise _bad("entry %d has a bad size %s" % (i, quote(size)))
    if not good_mtime(mtime):
        raise _bad("entry %d has a bad mtime %s" % (i, quote(mtime)))
    if "exec" in obj and not isinstance(executable, bool):
        raise _bad("entry %d has an exec that isn't true or false" % i)
    if "sha256" in obj and not good_sha256(sha256):
        raise _bad("entry %d has a bad sha256 %s" % (i, quote(sha256)))
    return Entry("put", path, kind="file", size=size, mtime=mtime, executable=executable,
                 sha256=sha256)


def from_json(obj):
    """A Plan from its JSON form. Checks the structure only; the path text is pathrules' job."""
    if not isinstance(obj, dict):
        raise _bad("not an object: %s" % quote(obj))
    extra = sorted(set(obj) - {"entries", "identity", "state", "notes"})
    if extra:
        raise _bad("unknown keys: %s" % quote(extra))
    if not isinstance(obj.get("entries"), list):
        raise _bad("entries is missing or isn't a list")
    identity = obj.get("identity")
    state = obj.get("state")
    notes = obj.get("notes", [])
    if not isinstance(identity, (dict, type(None))):
        raise _bad("identity isn't an object or null")
    if not isinstance(state, (dict, type(None))):
        raise _bad("state isn't an object or null")
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise _bad("notes isn't a list of strings")
    entries = [_entry(e, i) for i, e in enumerate(obj["entries"])]
    return Plan(entries, identity=identity, state=state, notes=list(notes))
