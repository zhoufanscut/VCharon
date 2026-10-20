"""The bootstrap line, the loader and the bundle that carry vcharon's code to the server; client."""

from __future__ import annotations

import base64
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


def build(nonce, extra_modules=None):
    """The bundle: an 8-byte big-endian length, then zlib-compressed JSON with the nonce and the
    source of each module SERVER names. extra_modules ({name: source}) adds or replaces
    modules; it's a test seam."""
    modules, packages = _sources()
    if extra_modules:
        modules.update(extra_modules)
    doc = {"nonce": nonce, "packages": packages, "modules": modules}
    blob = zlib.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
                         .encode("utf-8"), 9)
    return struct.pack(">Q", len(blob)) + blob


def summary():
    """{"modules", "has_helper", "bytes"}: the bundle a session sends, built as a session builds
    it and read back as the loader reads it, so doctor shows the helper's source is there (in a
    binary, the .py files the spec collects under sys._MEIPASS). bytes: the bundle's size."""
    blob = build(os.urandom(16).hex())
    size = struct.unpack(">Q", blob[:8])[0]
    modules = json.loads(zlib.decompress(blob[8:8 + size]).decode("utf-8"))["modules"]
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


def _vcharon_main():
    import importlib.util
    import json
    import re
    import struct
    import zlib

    def bad(why):
        sys.stderr.write("vcharon: bad bundle: %s\n" % why)
        sys.stderr.flush()
        sys.exit(91)

    # The bootstrap read its line through this buffer, which may already hold the bundle's
    # first bytes. Read nothing through any other object.
    inp = sys.stdin.buffer
    head = inp.read(8)
    if len(head) != 8:
        bad("short header")
    size = struct.unpack(">Q", head)[0]
    if size > 64 << 20:
        bad("too big")
    blob = inp.read(size)
    if len(blob) != size:
        bad("short body")
    try:
        bundle = json.loads(zlib.decompress(blob).decode("utf-8"))
        nonce, modules = bundle["nonce"], bundle["modules"]
        packages = frozenset(bundle["packages"])
        good = (isinstance(nonce, str) and re.match(r"\A[0-9a-f]{32}\Z", nonce)
                and isinstance(modules, dict) and "vcharon.helper" in modules
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
