"""The bootstrap line, the loader and the bundle that carry vcharon's code to the server; client."""

from __future__ import annotations

import base64
import json
import os
import struct
import zlib

import vcharon
from vcharon import FLOOR

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
    source of every module of the package. extra_modules ({name: source}) adds or replaces
    modules; it's a test seam."""
    modules, packages = _sources()
    if extra_modules:
        modules.update(extra_modules)
    doc = {"nonce": nonce, "packages": packages, "modules": modules}
    blob = zlib.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
                         .encode("utf-8"), 9)
    return struct.pack(">Q", len(blob)) + blob


def _sources(root=None):
    """{module name: source} for every module under the package directory, and the packages.
    root: another directory to read as the package; a test seam."""
    if root is None:
        root = os.path.dirname(os.path.abspath(vcharon.__file__))
    found = {}
    packages = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Only names Python can import. That leaves out dot-directories, and editor and Finder
        # litter such as .#helper.py (an Emacs lock, often a dangling symlink) and ._proto.py
        # (a macOS AppleDouble file, binary).
        dirnames[:] = sorted(d for d in dirnames if d.isidentifier() and d != "__pycache__")
        rel = os.path.relpath(dirpath, root)
        parts = ["vcharon"] + ([] if rel == os.curdir else rel.split(os.sep))
        if "__init__.py" in filenames:
            packages.append(".".join(parts))
        for filename in filenames:
            stem = filename[:-3]
            if not filename.endswith(".py") or not stem.isidentifier():
                continue
            name = ".".join(parts if stem == "__init__" else parts + [stem])
            with open(os.path.join(dirpath, filename), "rb") as f:
                found[name] = f.read().decode("utf-8")
    return {name: found[name] for name in sorted(found)}, sorted(packages)


# The loader runs on the server's Python, which may be older than the floor. It must parse on
# Python 3.7 and 3.8 so that an old server reaches the version check: no f-strings, no
# annotations, no walrus, and no % templating here (loader_source prepends FLOOR).
_LOADER = r'''import sys

if sys.version_info[:2] < FLOOR:
    sys.stderr.write("vcharon: the server's Python is %d.%d; vcharon needs %d.%d or newer\n"
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
