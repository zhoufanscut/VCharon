"""The bootstrap line, the loader and the bundle that carry vcharon's code to the server; client."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import zlib

import vcharon
from vcharon import FLOOR
from vcharon.proto import VCharonError

# The only text that goes through the server's shell. It has no ' so it fits in '...'.
BOOTSTRAP = "import sys,base64;exec(base64.b64decode(sys.stdin.buffer.readline()))"


def remote_command(remote_python):
    return "%s -I -c '%s'" % (remote_python, BOOTSTRAP)


def loader_source(floor=FLOOR):
    return "FLOOR = %r\n" % (tuple(floor),) + _LOADER


def loader_line(floor=FLOOR):
    return base64.b64encode(loader_source(floor).encode()) + b"\n"


def payload(extra_modules=None):
    """(the bundle's JSON, its sha256 in hex): {"packages", "modules": {name: source}} as UTF-8
    bytes, built the same way on every run so that one install always hashes the same and the
    server's cached copy is found. The modules are those SERVER names; extra_modules ({name:
    source}) adds or replaces some, a test seam that changes the hash too."""
    modules, packages = _sources()
    if extra_modules:
        modules.update(extra_modules)
    doc = {"packages": packages, "modules": modules}
    data = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    data = data.encode("utf-8")
    return data, hashlib.sha256(data).hexdigest()


def key_line(nonce, digest):
    """The line after the loader: the session's nonce and the bundle's sha256."""
    return ("%s %s\n" % (nonce, digest)).encode("ascii")


def frame(data):
    """What a session sends when the server asks for the bundle: an 8-byte big-endian length,
    then the JSON zlib-compressed."""
    blob = zlib.compress(data, 9)
    return struct.pack(">Q", len(blob)) + blob


def summary():
    """{"modules", "has_helper", "bytes"}: the bundle a session sends, built as a session builds
    it and read back as the loader reads it, so doctor shows the helper's source is there (in a
    binary, the .py files the spec collects under sys._MEIPASS). bytes: its size when sent,
    compressed and framed."""
    data, digest = payload()
    blob = frame(data)
    size = struct.unpack(">Q", blob[:8])[0]
    back = zlib.decompress(blob[8:8 + size])
    if hashlib.sha256(back).hexdigest() != digest:
        raise ValueError("the bundle doesn't read back as it was built")
    modules = json.loads(back.decode("utf-8"))["modules"]
    return {"modules": len(modules), "has_helper": "vcharon.helper" in modules,
            "bytes": len(blob)}


# Every module the helper can import, and only those: the bundle a session sends. The plugins
# are here because plugin.load() imports vcharon.plugins.<name> by name. tests/test_bundle.py
# walks the helper's imports and fails when this list misses one or names one it can't reach.
SERVER = (
    "vcharon",
    "vcharon.channels",
    "vcharon.charter",
    "vcharon.entries",
    "vcharon.fsops",
    "vcharon.helper",
    "vcharon.lock",
    "vcharon.pathrules",
    "vcharon.plan",
    "vcharon.platform",
    "vcharon.plugin",
    "vcharon.plugins",
    "vcharon.plugins.dir",
    "vcharon.plugins.path",
    "vcharon.proto",
    "vcharon.stage",
)

BROKEN_HINT = "this install is broken: install vcharon again"


def _sources(root=None):
    """{module name: source} for each module SERVER names, and the packages among them (a name
    with a directory and an __init__.py). root: another directory to read as the package; a
    test seam. A module whose file is missing raises here, on the client, so a broken install
    isn't found later as an ImportError on the server."""
    if root is None:
        root = os.path.dirname(os.path.abspath(vcharon.__file__))
    found = {}
    packages = []
    missing = []
    for name in SERVER:
        base = os.path.join(root, *name.split(".")[1:])
        init = os.path.join(base, "__init__.py")
        if os.path.isfile(init):
            path = init
            packages.append(name)
        elif name != "vcharon" and os.path.isfile(base + ".py"):
            path = base + ".py"
        else:
            missing.append(name)
            continue
        with open(path, "rb") as f:
            found[name] = f.read().decode("utf-8")
    if missing:
        raise VCharonError("internal", "this install has no source (.py) of %s, which the "
                           "helper imports (in %s)" % (", ".join(missing), root), BROKEN_HINT)
    return {name: found[name] for name in sorted(found)}, sorted(packages)


# The loader runs on the server's Python, which may be older than the floor. It must parse on
# any Python 3 an old server may still have (3.6 and later) so that it reaches the version
# check, which runs before any of vcharon's own modules, written for the floor, is compiled:
# no f-strings, no annotations, no walrus, and no % templating here (loader_source prepends
# FLOOR).
_LOADER = r'''import sys

if sys.version_info[:2] < FLOOR:
    sys.stderr.write("vcharon: the server's Python is %d.%d; vcharon needs %d.%d or later\n"
                     % (sys.version_info[0], sys.version_info[1], FLOOR[0], FLOOR[1]))
    sys.stderr.flush()
    sys.exit(90)


def _cache_dir():
    # cache/ beside the channel root, as channels.py finds the root (DEFAULT_ROOT, ROOT_ENV and
    # root_path); tests/test_bundle.py checks that the two agree
    import os
    root = os.environ.get("VCHARON_CHANNELS_ROOT") or "~/.local/state/vcharon/channels"
    root = os.path.expanduser(root)
    if not os.path.isabs(root):
        root = os.path.join(os.path.expanduser("~"), root)
    return os.path.join(os.path.dirname(os.path.normpath(root)), "cache")


def _vcharon_main():
    import hashlib
    import importlib.util
    import json
    import os
    import re
    import stat
    import struct
    import time
    import zlib

    cap = 64 << 20
    # the newest copies kept; about one per client version and OS that connects here (a
    # checkout's line endings, and so the hash, may differ by OS)
    keep = 8

    def bad(why):
        sys.stderr.write("vcharon: bad bundle: %s\n" % why)
        sys.stderr.flush()
        sys.exit(91)

    def note(why):
        sys.stderr.write("vcharon: %s\n" % why)
        sys.stderr.flush()

    def store(path, data):
        # Best effort: the session goes on from memory when the copy can't be written.
        folder = os.path.dirname(path)
        tmp = os.path.join(folder, ".tmp-%s-%d" % (nonce, os.getpid()))
        try:
            os.makedirs(folder, 0o700, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                         0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError as e:
            note("couldn't keep the bundle in %s for the next connection: %s" % (folder, e))
            try:
                os.remove(tmp)
            except OSError:
                pass
            return
        prune(folder, os.path.basename(path))

    def prune(folder, mine):
        # Only after a write, so a connection that finds its copy reads one file.
        try:
            names = os.listdir(folder)
        except OSError:
            return
        found = []
        drop = []
        for name in names:
            path = os.path.join(folder, name)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if name.startswith("helper-") and name != mine:
                found.append((mtime, path))
            elif name.startswith(".tmp-") and mtime < time.time() - 3600:
                # left by a loader killed while writing
                drop.append(path)
        found.sort(reverse=True)
        for path in [path for _, path in found[keep - 1:]] + drop:
            try:
                os.remove(path)
            except OSError:
                pass

    # The bootstrap read its line through this buffer, which may already hold the next bytes.
    # Read nothing through any other object.
    inp = sys.stdin.buffer
    m = re.match(br"\A([0-9a-f]{32}) ([0-9a-f]{64})\n\Z", inp.readline(200))
    if not m:
        bad("no nonce and hash line")
    nonce, digest = m.group(1).decode("ascii"), m.group(2).decode("ascii")
    path = os.path.join(_cache_dir(), "helper-" + digest)
    data = None
    try:
        # Non-blocking, and only a regular file: a FIFO there would block open() forever.
        # O_NONBLOCK changes nothing for a regular file's reads.
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as f:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                data = f.read(cap + 1)
    except OSError:
        pass
    # Hashed on every use: nothing on the server is trusted, only reused.
    if data is not None and hashlib.sha256(data).hexdigest() == digest:
        try:
            os.utime(path, None)
        except OSError:
            pass
    else:
        out = sys.stdout.buffer
        out.write(b"VCHARON-NEED " + nonce.encode("ascii") + b"\n")
        out.flush()
        head = inp.read(8)
        if len(head) != 8:
            bad("short header")
        size = struct.unpack(">Q", head)[0]
        if size > cap:
            bad("too big")
        blob = inp.read(size)
        if len(blob) != size:
            bad("short body")
        try:
            data = zlib.decompressobj().decompress(blob, cap + 1)
        except Exception as e:
            bad(repr(e))
        if len(data) > cap:
            bad("too big")
        if hashlib.sha256(data).hexdigest() != digest:
            bad("its sha256 isn't the one sent before it")
        store(path, data)
    try:
        bundle = json.loads(data.decode("utf-8"))
        modules = bundle["modules"]
        packages = frozenset(bundle["packages"])
        good = (isinstance(modules, dict) and "vcharon.helper" in modules
                and all(isinstance(k, str) and isinstance(v, str) for k, v in modules.items())
                and packages <= set(modules))
    except Exception as e:
        bad(repr(e))
    if not good:
        bad("wrong fields")

    class Finder(object):
        # Serves vcharon's modules from memory. It goes first in sys.meta_path, so no file on the
        # server can stand in for them.
        def find_spec(self, name, path=None, target=None):
            if name not in modules:
                return None
            return importlib.util.spec_from_loader(name, self, is_package=name in packages)

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            name = module.__spec__.name
            code = compile(modules[name], self.filename(name), "exec", dont_inherit=True)
            exec(code, module.__dict__)

        def get_source(self, name):
            # lets tracebacks from the helper show source lines
            return modules.get(name)

        def filename(self, name):
            tail = "/__init__.py" if name in packages else ".py"
            return "vcharon-bundle/" + name.replace(".", "/") + tail

    sys.meta_path.insert(0, Finder())
    try:
        import vcharon.helper
    except Exception:
        # Bundled code that won't compile or import is a vcharon bug, not a startup-file problem.
        import traceback
        traceback.print_exc()
        bad("vcharon.helper didn't import")
    return vcharon.helper.main(nonce)


sys.exit(_vcharon_main())
'''
