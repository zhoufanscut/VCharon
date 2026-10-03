"""No file of the project names the tool it was extracted from, in its contents or its name,
beyond the exact hits ALLOWED counts."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the old name, in parts: this file must not hold it, or it would allow itself
OLD = "f" + "erry"
WORD = re.compile(re.escape(OLD).encode("ascii"), re.IGNORECASE)
# path (with /, from ROOT) -> how many hits it may have (its name counts as one): exactly that
# many, so a new hit in an allowed file fails too, and so does an entry left over
ALLOWED: dict[str, int] = {}
# never checked: the local plan; never walked (git leaves them out by .gitignore): what git,
# the venv, a build and other tools make
SKIP_FILES = {"PLAN.md"}
SKIP_DIRS = {"build", "dist", "__pycache__"}
KEEP_DOT_DIRS = {".github"}


def _git_files():
    """The files git tracks or would add (ignored ones left out), or None unless ROOT is the
    top of a git checkout: an sdist, or no git."""
    try:
        # empty exactly at the top: nothing to decode or compare
        prefix = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=ROOT,
                                capture_output=True, check=False)
        if prefix.returncode != 0 or prefix.stdout.strip():
            return None
        listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others",
                                 "--exclude-standard"], cwd=ROOT, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in listed.stdout.decode("utf-8").split("\0") if p]


def _skipped_dir(name):
    return (name in SKIP_DIRS or name.endswith(".egg-info")
            or (name.startswith(".") and name not in KEEP_DOT_DIRS))


def _walked_files(root=ROOT):
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _skipped_dir(d))
        for name in sorted(files):
            found.append(os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/"))
    return found


def project_files():
    """Every file to check, as paths with / from ROOT."""
    files = _git_files()
    if files is None:
        files = _walked_files()
    return [p for p in files if p not in SKIP_FILES and os.path.isfile(os.path.join(ROOT, p))]


def hits(path, root=ROOT):
    """The places in path that name the word: its name, then path:line for each line."""
    found = [path + ": in the file name"] if WORD.search(path.encode("utf-8")) else []
    with open(os.path.join(root, path), "rb") as f:
        for number, line in enumerate(f, 1):
            if WORD.search(line):
                found.append("%s:%d" % (path, number))
    return found


def problems(files, allowed, root=ROOT):
    """What is wrong: each hit in a file allowed has none of, each allowed file whose count of
    hits isn't its own, and each allowed file that isn't there."""
    found = []
    for path in files:
        places = hits(path, root)
        if path not in allowed:
            found += places
        elif len(places) != allowed[path]:
            found.append("%s: %d hits, ALLOWED says %d: %s" % (path, len(places), allowed[path],
                                                               ", ".join(places) or "none"))
    found += ["%s: in ALLOWED, but not here" % p for p in sorted(allowed) if p not in files]
    return found


class OldNameTest(unittest.TestCase):

    def test_no_file_names_it(self):
        files = project_files()
        self.assertIn("pyproject.toml", files)
        self.assertIn("src/vcharon/cli.py", files)
        self.assertIn(".github/workflows/ci.yml", files)
        self.assertEqual(problems(files, ALLOWED), [],
                         "rename these, or set the file's count in ALLOWED, with a reason")

    def test_the_check(self):
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        word = OLD.capitalize()
        texts = {"a.txt": "one %s\nnone\nand %s.ini\n" % (word, OLD.upper()), "b.txt": "x\n",
                 "c-%s.txt" % OLD: "x\n", "d.txt": "%s's\n" % OLD}
        for name, text in texts.items():
            with open(os.path.join(tmp, name), "w") as f:
                f.write(text)
        files = sorted(texts)
        self.assertEqual(problems(files, {"a.txt": 2, "c-%s.txt" % OLD: 1}, tmp), ["d.txt:1"])
        self.assertEqual(problems(files, {"a.txt": 1, "d.txt": 1, "c-%s.txt" % OLD: 1}, tmp),
                         ["a.txt: 2 hits, ALLOWED says 1: a.txt:1, a.txt:3"])
        self.assertEqual(problems(files, {"a.txt": 2, "d.txt": 1, "gone.txt": 1}, tmp),
                         ["c-%s.txt: in the file name" % OLD, "gone.txt: in ALLOWED, but not here"])
        # an allowed file with no hits left is an entry left over
        self.assertEqual(problems(files, {"a.txt": 2, "b.txt": 1, "d.txt": 1,
                                          "c-%s.txt" % OLD: 1}, tmp),
                         ["b.txt: 0 hits, ALLOWED says 1: none"])

    def test_walk_skips_tool_folders(self):
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in (".github/w.yml", ".pytest_cache/v", ".venv/x", "src/__pycache__/m.pyc",
                    "src/m.py", "x.egg-info/PKG-INFO", ".gitignore"):
            os.makedirs(os.path.join(tmp, os.path.dirname(rel)), exist_ok=True)
            open(os.path.join(tmp, rel), "w").close()
        self.assertEqual(_walked_files(tmp), [".gitignore", ".github/w.yml", "src/m.py"])

    def test_walk_finds_what_git_finds(self):
        # the walk is the fallback in an sdist: it must not skip a file git has
        if _git_files() is None:
            self.skipTest("the project's folder isn't the top of a git checkout")
        self.assertEqual(set(project_files()) - set(_walked_files()), set())


if __name__ == "__main__":
    unittest.main()
