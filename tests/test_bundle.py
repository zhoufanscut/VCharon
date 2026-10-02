"""The bootstrap line, the loader and the bundle."""

from __future__ import annotations

import ast
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

from vcharon import bundle

from tests.util import PACKAGE_DIR

NONCE = "0123456789abcdef" * 2


def unpack(blob):
    size = struct.unpack(">Q", blob[:8])[0]
    return size, json.loads(zlib.decompress(blob[8:]).decode("utf-8"))


def run_loader(stdin, timeout=30):
    """Runs the bootstrap the way the server does, fed `stdin`."""
    return subprocess.run([sys.executable, "-I", "-c", bundle.BOOTSTRAP], input=stdin,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


class BundleTest(unittest.TestCase):
    def test_holds_every_module(self):
        blob = bundle.build(NONCE)
        size, doc = unpack(blob)
        self.assertEqual(size, len(blob) - 8)
        self.assertEqual(doc["nonce"], NONCE)
        self.assertEqual(doc["packages"], ["vcharon", "vcharon.plugins"])
        expected = {}
        for dirpath, dirnames, filenames in os.walk(PACKAGE_DIR):
            dirnames[:] = [d for d in dirnames if d.isidentifier() and d != "__pycache__"]
            for filename in filenames:
                # editor litter such as .#helper.py isn't a module
                if filename.endswith(".py") and filename[:-3].isidentifier():
                    rel = os.path.relpath(os.path.join(dirpath, filename), PACKAGE_DIR)
                    name = "vcharon." + rel[:-3].replace(os.sep, ".")
                    if name.endswith(".__init__"):
                        name = name[:-len(".__init__")]
                    with open(os.path.join(dirpath, filename), "rb") as f:
                        expected[name] = f.read().decode("utf-8")
        self.assertEqual(doc["modules"], expected)
        self.assertEqual(list(doc["modules"]), sorted(expected))
        for name in ("vcharon", "vcharon.helper", "vcharon.proto", "vcharon.plugins", "vcharon.ssh",
                     "vcharon.plugins.path", "vcharon.plugins.dir"):
            self.assertIn(name, doc["modules"])

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "needs symlinks")
    def test_skips_names_python_cant_import(self):
        root = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, root, True)
        for name, data in (("__init__.py", b"X = 1\n"), ("a.py", b"A = 1\n"),
                           ("._a.py", b"\x00\x05\x16\x07\xff\xfe junk"),
                           ("notes.txt", b"not python")):
            with open(os.path.join(root, name), "wb") as f:
                f.write(data)
        # an Emacs lock file: a dangling symlink
        os.symlink("user@host.1234:1790000000", os.path.join(root, ".#b.py"))
        os.mkdir(os.path.join(root, ".hidden"))
        with open(os.path.join(root, ".hidden", "c.py"), "w") as f:
            f.write("C = 1\n")
        modules, packages = bundle._sources(root)
        self.assertEqual(modules, {"vcharon": "X = 1\n", "vcharon.a": "A = 1\n"})
        self.assertEqual(packages, ["vcharon"])

    def test_extra_modules(self):
        size, doc = unpack(bundle.build(NONCE, {"vcharon.extra": "X = 1\n", "vcharon": "Y = 2\n"}))
        self.assertEqual(doc["modules"]["vcharon.extra"], "X = 1\n")
        self.assertEqual(doc["modules"]["vcharon"], "Y = 2\n")

    def test_remote_command(self):
        self.assertNotIn("'", bundle.BOOTSTRAP)
        self.assertEqual(bundle.remote_command("python3"),
                         "python3 -I -c 'import sys,base64;exec(base64.b64decode("
                         "sys.stdin.buffer.readline()))'")

    def test_loader_parses_on_python_3_7(self):
        try:
            ast.parse("x = 1", feature_version=(3, 7))
        except (ValueError, TypeError):
            self.skipTest("this Python can't parse as 3.7")
        ast.parse(bundle.loader_source(), feature_version=(3, 7))

    def test_loader_line(self):
        line = bundle.loader_line((3, 9))
        self.assertTrue(line.endswith(b"\n"))
        self.assertNotIn(b"\n", line[:-1])
        self.assertTrue(bundle.loader_source((3, 9)).startswith("FLOOR = (3, 9)\n"))


class LoaderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        patcher = mock.patch.dict(os.environ, {"VCHARON_HOME": tmp})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_too_old(self):
        result = run_loader(bundle.loader_line((99, 0)) + bundle.build(NONCE))
        self.assertEqual(result.returncode, 90, result.stderr)
        self.assertIn(b"vcharon needs 99.0 or newer", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_bad_bundles(self):
        good = bundle.build(NONCE)
        size, doc = unpack(good)
        doc["nonce"] = "not hex"
        wrong = zlib.compress(json.dumps(doc).encode())
        for blob in (b"", good[:5], good[:-10], good[:8] + b"x" * (len(good) - 8),
                     struct.pack(">Q", 65 << 20), struct.pack(">Q", len(wrong)) + wrong):
            result = run_loader(bundle.loader_line() + blob)
            self.assertEqual(result.returncode, 91, (blob[:20], result.stderr))
            self.assertIn(b"vcharon: bad bundle: ", result.stderr)

    def test_helper_that_wont_import(self):
        blob = bundle.build(NONCE, {"vcharon.helper": "def main(nonce):\n    return (\n"})
        result = run_loader(bundle.loader_line() + blob)
        self.assertEqual(result.returncode, 91, result.stderr)
        self.assertIn(b"SyntaxError", result.stderr)
        self.assertIn(b"vcharon-bundle/vcharon/helper.py", result.stderr)
        # the loader writes through sys.stderr, whose text layer turns \n into \r\n on Windows
        self.assertTrue(result.stderr.replace(b"\r\n", b"\n")
                        .endswith(b"vcharon: bad bundle: vcharon.helper didn't import\n"))

    def test_runs_the_helper(self):
        # The helper answers with the marker and a hello, then sees end of file and exits.
        result = run_loader(bundle.loader_line() + bundle.build(NONCE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(b"VCHARON-READY " + NONCE.encode() + b"\n"))
        self.assertIn(b'"t":"hello"', result.stdout)


if __name__ == "__main__":
    unittest.main()
