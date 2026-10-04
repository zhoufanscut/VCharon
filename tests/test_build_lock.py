"""build-requirements.txt, the release build's hash lock (DESIGN, "Releases"): every package at
one exact version with its hashes, and PyInstaller's two pins the same as the `build` extra's,
so a hand build from pyproject.toml gets what a release is built with."""

from __future__ import annotations

import os
import re
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# name==version, an optional environment marker, then the --hash options
LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+?)(?: ; ([^\\]+?))?((?: --hash=sha256:"
                  r"[0-9a-f]{64})+)$")


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock():
    """The lock's requirements as {name: (version, marker, hashes)}; fails on a line it can't
    read, so a requirement without a pin or a hash can't slip past."""
    with open(os.path.join(ROOT, "build-requirements.txt"), encoding="utf-8") as f:
        text = f.read()
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    # a requirement runs over continuation lines ending in a backslash
    joined = re.sub(r"\s*\\\n\s*", " ", "\n".join(lines)).strip()
    found = {}
    for req in joined.splitlines():
        m = LINE.match(req.strip())
        if m is None:
            raise AssertionError("not an exact pin with hashes: %r" % req)
        name, version, marker, hashes = m.groups()
        found[_norm(name)] = (version, marker, hashes.split())
    return found


class BuildLockTest(unittest.TestCase):
    def test_every_requirement_is_pinned_with_hashes(self):
        lock = _lock()
        # what the build itself needs: PyInstaller, pip, and the editable install's backend
        for name in ("pyinstaller", "pyinstaller-hooks-contrib", "pip", "hatchling",
                     "editables"):
            self.assertIn(name, lock)
        # PyInstaller's per-OS dependencies, each only where it applies
        self.assertEqual(lock["macholib"][1], 'sys_platform == "darwin"')
        self.assertEqual(lock["pefile"][1], 'sys_platform == "win32"')
        self.assertEqual(lock["pywin32-ctypes"][1], 'sys_platform == "win32"')

    def test_build_extra_matches(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            build = tomllib.load(f)["project"]["optional-dependencies"]["build"]
        lock = _lock()
        self.assertTrue(build)
        for req in build:
            name, _, version = req.partition("==")
            self.assertTrue(version, "the build extra pins exactly: %s" % req)
            self.assertEqual(lock[_norm(name)][0], version, name)


if __name__ == "__main__":
    unittest.main()
