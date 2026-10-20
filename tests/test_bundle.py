"""The bootstrap line, the loader and the bundle."""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tokenize
import unittest
import zlib
from unittest import mock

from vcharon import bundle, plugins
from vcharon.proto import VCharonError

from tests.util import PACKAGE_DIR

NONCE = "0123456789abcdef" * 2


def unpack(blob):
    size = struct.unpack(">Q", blob[:8])[0]
    return size, json.loads(zlib.decompress(blob[8:]).decode("utf-8"))


def run_loader(stdin, timeout=30):
    """Runs the bootstrap the way the server does, fed `stdin`."""
    return subprocess.run([sys.executable, "-I", "-c", bundle.BOOTSTRAP], input=stdin,
                          capture_output=True, timeout=timeout, check=False)


def newer_syntax(source):
    """[(what, (line, col))] for the syntax in source that Python 3.6 can't parse, of the kinds
    the loader could slip into: f-strings, :=, a match statement's keyword at a statement's
    start (a name match = 1 counts too: the loader has no use for it), a positional-only /
    in a def's or a lambda's parameters, except*, a def's type parameters (def f[T]) and a
    parenthesized with of several context managers (with (a as x, b as y))."""
    found = []
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    start = True
    in_def = depth = 0
    lambdas = []
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    level = 0

    def nxt(i):
        return tokens[i + 1].string if i + 1 < len(tokens) else ""

    for i, tok in enumerate(tokens):
        name = tok.type == tokenize.NAME
        op = tok.type == tokenize.OP
        if tok.type == fstring_start or (
                tok.type == tokenize.STRING
                and "f" in re.match(r"[A-Za-z]*", tok.string).group().lower()):
            found.append(("f-string", tok.start))
        if op and tok.string == ":=":
            found.append((":=", tok.start))
        if start and name and tok.string == "match":
            found.append(("match", tok.start))
        if name and tok.string == "except" and nxt(i) == "*":
            found.append(("except*", tok.start))
        if name and tok.string == "def" and i + 2 < len(tokens) and tokens[i + 2].string == "[":
            found.append(("def f[T]", tok.start))
        if name and tok.string == "with" and nxt(i) == "(":
            inner = 0
            for later in tokens[i + 1:]:
                if later.string in "([{" and later.type == tokenize.OP:
                    inner += 1
                elif later.string in ")]}" and later.type == tokenize.OP:
                    inner -= 1
                    if not inner:
                        break
                elif inner == 1 and later.type == tokenize.NAME and later.string == "as":
                    found.append(("with (... as ...)", tok.start))
                    break
        # a lambda's parameters run to its ":" at the bracket level it started at
        if op and tok.string in "([{":
            level += 1
        elif op and tok.string in ")]}":
            level -= 1
        if name and tok.string == "lambda":
            lambdas.append(level)
        elif op and tok.string == ":" and lambdas and lambdas[-1] == level:
            lambdas.pop()
        positional = (op and tok.string == "/" and tokens[i - 1].string == ","
                      and nxt(i) in (",", ")", ":"))
        if positional and lambdas and lambdas[-1] == level:
            found.append(("/ in a lambda", tok.start))
        if name and tok.string == "def":
            in_def, depth = True, 0
        elif in_def and op:
            if tok.string == "(":
                depth += 1
            elif tok.string == ")":
                depth -= 1
                in_def = depth > 0
            elif positional and depth == 1:
                # a parameter of its own, not a division in a default value
                found.append(("/ in a def", tok.start))
        start = tok.type in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
                             tokenize.COMMENT)
    return found


def module_file(name, root=PACKAGE_DIR):
    """The file of vcharon module `name` under the package directory, or None."""
    base = os.path.join(root, *name.split(".")[1:])
    if os.path.isfile(os.path.join(base, "__init__.py")):
        return os.path.join(base, "__init__.py")
    if name != "vcharon" and os.path.isfile(base + ".py"):
        return base + ".py"
    return None


def imported_by(name):
    """The vcharon modules that module `name`'s import statements name, function-level ones
    too: `from a import b` names a, and a.b when that is a module."""
    path = module_file(name)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    package = name.split(".")
    if not path.endswith("__init__.py"):
        package = package[:-1]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module
            found.append(module)
            found += [module + "." + alias.name for alias in node.names]
    return [n for n in found if n.split(".")[0] == "vcharon" and module_file(n)]


def helper_closure(roots=None):
    """{module: the module that imports it} for every vcharon module the helper can import,
    from vcharon.helper and each plugin (plugin.load() imports vcharon.plugins.<name> by name,
    which no import statement shows), or from roots. Importing a.b runs a too."""
    if roots is None:
        roots = ["vcharon.helper"] + ["vcharon.plugins." + name.replace("-", "_")
                                      for name in plugins.ALL]
    reached = {}
    todo = [(root, "a root") for root in roots]
    while todo:
        name, by = todo.pop()
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            module = ".".join(parts[:i])
            if module not in reached:
                reached[module] = by
                todo += [(n, module) for n in imported_by(module)]
    return reached


class BundleTest(unittest.TestCase):
    def test_holds_the_modules_server_names(self):
        blob = bundle.build(NONCE)
        size, doc = unpack(blob)
        self.assertEqual(size, len(blob) - 8)
        self.assertEqual(doc["nonce"], NONCE)
        self.assertEqual(doc["packages"], ["vcharon", "vcharon.plugins"])
        expected = {}
        for name in bundle.SERVER:
            with open(module_file(name), "rb") as f:
                expected[name] = f.read().decode("utf-8")
        self.assertEqual(doc["modules"], expected)
        self.assertEqual(list(doc["modules"]), sorted(expected))
        for name in ("vcharon", "vcharon.helper", "vcharon.proto", "vcharon.plugins",
                     "vcharon.plugins.path", "vcharon.plugins.dir"):
            self.assertIn(name, doc["modules"])
        # the client's own modules stay home
        for client_only in ("vcharon.cli", "vcharon.ssh", "vcharon.bundle", "vcharon.mailbox",
                            "vcharon.guide", "vcharon.skill"):
            self.assertFalse([m for m in doc["modules"]
                              if m == client_only or m.startswith(client_only + ".")])

    def test_server_is_what_the_helper_can_import(self):
        reached = helper_closure()
        missing = set(reached) - set(bundle.SERVER)
        # Name the new edge, an import from a module in the list, not whatever comes first
        # alphabetically: a module reached only through another missing one follows it in.
        if "vcharon.helper" in missing:
            self.fail("bundle.SERVER must list vcharon.helper, the helper itself")
        plugin_roots = {"vcharon.plugins." + p.replace("-", "_") for p in plugins.ALL}
        for name in sorted(missing):
            by = [m for m in bundle.SERVER if m in reached and name in imported_by(m)]
            if name in plugin_roots:
                by.append("plugin.load(), as a plugin")
            if by:
                others = len(missing) - 1
                self.fail("the helper imports %s (from %s), not in bundle.SERVER%s: add it if "
                          "the server needs it, or move the import to client code"
                          % (name, ", ".join(by),
                             "; %d other modules are missing too" % others if others else ""))
        if missing:
            self.fail("the helper imports %s: add to bundle.SERVER what the server needs"
                      % ", ".join(sorted(missing)))
        for name in bundle.SERVER:
            if module_file(name) is None:
                self.fail("bundle.SERVER names %s, which isn't a module of the package" % name)
            if name not in reached:
                self.fail("bundle.SERVER names %s, which the helper never imports: remove it"
                          % name)
        self.assertEqual(len(bundle.SERVER), len(set(bundle.SERVER)))

    def test_the_walk_sees_each_kind_of_import(self):
        # the closure's own check: what cli.py imports at the top, in a function, relatively
        # and as `from . import x`
        reached = imported_by("vcharon.cli")
        self.assertIn("vcharon.ssh", reached)
        self.assertIn("vcharon.guide", reached)
        # imported only inside functions
        self.assertIn("vcharon.channels", imported_by("vcharon.charter"))
        self.assertIn("vcharon.update", reached)
        self.assertIn("vcharon.helper", helper_closure())
        self.assertIn("vcharon.plugins", helper_closure())

    def copy_server(self, root):
        for name in bundle.SERVER:
            rel = os.path.relpath(module_file(name), PACKAGE_DIR)
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            shutil.copyfile(module_file(name), os.path.join(root, rel))

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "needs symlinks")
    def test_reads_only_what_server_names(self):
        root = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, root, True)
        self.copy_server(root)
        for name, data in (("cli.py", b"C = 1\n"), ("._proto.py", b"\x00\x05\x16\x07\xff\xfe junk"),
                           ("notes.txt", b"not python")):
            with open(os.path.join(root, name), "wb") as f:
                f.write(data)
        # an Emacs lock file: a dangling symlink
        os.symlink("user@host.1234:1790000000", os.path.join(root, ".#helper.py"))
        os.mkdir(os.path.join(root, "mailbox"))
        with open(os.path.join(root, "mailbox", "__init__.py"), "w") as f:
            f.write("M = 1\n")
        modules, packages = bundle._sources(root)
        self.assertEqual(sorted(modules), sorted(bundle.SERVER))
        self.assertEqual(packages, ["vcharon", "vcharon.plugins"])

    def test_a_missing_module_is_an_error_on_the_client(self):
        # a broken install: the bundle isn't sent, rather than failing to import on the server
        root = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, root, True)
        self.copy_server(root)
        os.remove(os.path.join(root, "stage.py"))
        os.remove(os.path.join(root, "plugins", "dir.py"))
        with self.assertRaises(VCharonError) as caught:
            bundle._sources(root)
        self.assertEqual(caught.exception.code, "internal")
        self.assertEqual(caught.exception.message, "this install has no source (.py) of "
                         "vcharon.plugins.dir, vcharon.stage, which the helper imports (in %s)"
                         % root)
        self.assertEqual(caught.exception.hint, bundle.BROKEN_HINT)

    def test_summary_reads_back_what_a_session_sends(self):
        modules, _packages = bundle._sources()
        found = bundle.summary()
        self.assertEqual((found["modules"], found["has_helper"]), (len(modules), True))
        # compressed, so another nonce may change it by a few bytes
        self.assertAlmostEqual(found["bytes"], len(bundle.build(NONCE)), delta=200)
        with mock.patch.object(bundle, "_sources", return_value=({"vcharon": ""}, ["vcharon"])):
            self.assertEqual(bundle.summary()["has_helper"], False)

    def test_a_binary_reads_the_sources_beside_its_bytecode(self):
        # a one-file binary's vcharon.__file__ is <_MEIPASS>/vcharon/__init__.pyc, and the
        # spec puts the .py files beside it: those are what a session sends
        meipass = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, meipass, True)
        package = os.path.join(meipass, "vcharon")
        self.copy_server(package)
        os.makedirs(os.path.join(package, "guide"))
        for rel, data in (("cli.py", "C = 1\n"), ("guide/__init__.py", ""),
                          ("guide/start.md", "# start\n")):
            with open(os.path.join(package, *rel.split("/")), "w", encoding="utf-8") as f:
                f.write(data)
        with open(os.path.join(package, "helper.py"), "w", encoding="utf-8") as f:
            f.write("H = 1\n")
        with mock.patch.object(bundle.vcharon, "__file__",
                               os.path.join(package, "__init__.pyc")):
            modules, packages = bundle._sources()
            found = bundle.summary()
        self.assertEqual(sorted(modules), sorted(bundle.SERVER))
        self.assertEqual(modules["vcharon.helper"], "H = 1\n")
        self.assertEqual(packages, ["vcharon", "vcharon.plugins"])
        self.assertEqual((found["modules"], found["has_helper"]), (len(bundle.SERVER), True))

    def test_extra_modules(self):
        _size, doc = unpack(bundle.build(NONCE, {"vcharon.extra": "X = 1\n", "vcharon": "Y = 2\n"}))
        self.assertEqual(doc["modules"]["vcharon.extra"], "X = 1\n")
        self.assertEqual(doc["modules"]["vcharon"], "Y = 2\n")

    def test_remote_command(self):
        self.assertNotIn("'", bundle.BOOTSTRAP)
        self.assertEqual(bundle.remote_command("python3"),
                         "python3 -I -c 'import sys,base64;exec(base64.b64decode("
                         "sys.stdin.buffer.readline()))'")

    def test_loader_parses_on_an_old_python(self):
        # an old server's Python must reach the floor check, which the loader runs first
        for version in ((3, 6), (3, 7)):
            try:
                ast.parse("x = 1", feature_version=version)
            except (ValueError, TypeError):
                continue
            ast.parse(bundle.loader_source(), feature_version=version)
        self.assertTrue(bundle.loader_source().startswith("FLOOR = (3, 13)\nimport sys\n\n"
                                                          "if sys.version_info[:2] < FLOOR:"))

    def test_loader_has_no_newer_syntax(self):
        # ast's feature_version doesn't refuse an f-string on 3.13: tokens do. No f-string,
        # no :=, no match statement, no positional-only / in a def (all newer than 3.6).
        self.assertEqual(newer_syntax(bundle.loader_source()), [])
        # the check itself sees each of them
        for bad, what in (('x = f"{1}"\n', "f-string"), ("x = rF'a'\n", "f-string"),
                          ("if (y := 1):\n    pass\n", ":="),
                          ("match x:\n    case 1:\n        pass\n", "match"),
                          ("def g(a, /, b):\n    pass\n", "/ in a def"),
                          ("def g(a, /):\n    pass\n", "/ in a def"),
                          ("h = lambda a, /: a\n", "/ in a lambda"),
                          ("try:\n    pass\nexcept* ValueError:\n    pass\n", "except*"),
                          ("def g[T](a):\n    pass\n", "def f[T]"),
                          ("with (open(a) as x, open(b) as y):\n    pass\n",
                           "with (... as ...)")):
            with self.subTest(bad=bad):
                self.assertEqual([w for w, _ in newer_syntax(bad)], [what])
        self.assertEqual(newer_syntax("match = 1 / 2\ndef h(a=1 / 2):\n    pass\n"),
                         [("match", (1, 0))])
        # what 3.6 parses: a division in a lambda, one with in parentheses, except, x[0]
        self.assertEqual(newer_syntax("f = lambda a, b=1 / 2: a / b\nwith (open(a)) as x:\n"
                                      "    pass\ntry:\n    pass\nexcept (A, B):\n    pass\n"
                                      "y = [1][0]\n"), [])

    def test_loader_line(self):
        line = bundle.loader_line((3, 13))
        self.assertTrue(line.endswith(b"\n"))
        self.assertNotIn(b"\n", line[:-1])
        self.assertTrue(bundle.loader_source((3, 13)).startswith("FLOOR = (3, 13)\n"))


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
        self.assertIn(b"vcharon needs 99.0 or later", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_refuses_3_11_and_3_12(self):
        # the real floor, with the server's Python faked older: what a Debian 12 (3.11) or an
        # Ubuntu 24.04 (3.12) server gets
        for minor in (11, 12):
            with self.subTest(minor=minor):
                code = ("import sys\nsys.version_info = (3, %d, 0)\nexec(compile(%r, 'loader', "
                        "'exec'))\n" % (minor, bundle.loader_source()))
                result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                                        timeout=60, check=False)
                self.assertEqual(result.returncode, 90, result.stderr)
                self.assertIn(b"the server's Python is 3.%d; vcharon needs 3.13 or later" % minor,
                              result.stderr)

    def test_bad_bundles(self):
        good = bundle.build(NONCE)
        _size, doc = unpack(good)
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
