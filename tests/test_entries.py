"""Channel entries (M10): their text, parsing, numbers, the own folder, and the locked
append."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from vcharon import entries
from vcharon.proto import VCharonError

from tests.test_cli import VCHARON_DIR
from tests.util import write_tree


class EntriesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"VCHARON_HOME": os.path.join(self.tmp, "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.own = os.path.join(self.tmp, "tree", "mac-a")
        write_tree(self.own, {"MEMBER.md": b"# MEMBER\n"})


class TextTest(EntriesCase):
    def test_build_and_parse(self):
        text = entries.build("2026-10-02 10:12", "mac-a", 7, "step 3 done", ["@laptop-ui"],
                             re_="laptop-ui#3", header=[("channel", "game")],
                             body="line 1\n## not a heading\n   ### nor this\nto: @all\n")
        self.assertEqual(text, "\n## 2026-10-02 10:12 — mac-a#7 — step 3 done\nto: @laptop-ui\n"
                               "re: laptop-ui#3\nchannel: game\n\nline 1\n> ## not a heading\n"
                               ">    ### nor this\nto: @all\n")
        (e,) = entries.parse("# RESULTS\n" + text)
        self.assertEqual((e.time, e.id, e.title, e.to, e.re, e.header, e.line),
                         ("2026-10-02 10:12", "mac-a#7", "step 3 done", ("@laptop-ui",),
                          "laptop-ui#3", [("channel", "game")], 3))
        # the body's "to: @all" is body, after the blank line: it can't forge a header
        self.assertEqual(e.body, "line 1\n> ## not a heading\n>    ### nor this\nto: @all")

    def test_headings_without_an_id(self):
        es = entries.parse("# R\n\n## 2026-10-01 09:00 — an old entry\n\nbody\n\n"
                           "## 2026-10-01 09:01 — x#3 — a title — with dashes\nto: @all\n")
        self.assertEqual([(e.time, e.name, e.number, e.title) for e in es], [
            ("2026-10-01 09:00", None, None, "an old entry"),
            ("2026-10-01 09:01", "x", 3, "a title — with dashes")])
        self.assertEqual(es[0].body, "body")
        self.assertEqual(es[0].to, ())

    def test_one_line_values(self):
        with self.assertRaises(VCharonError):
            entries.build("t", "a", 1, "two\nlines", ["@all"])

    def test_to_and_ids(self):
        self.assertIsNone(entries.to_problem("@all"))
        self.assertIsNone(entries.to_problem("@mac-a"))
        for bad in ("all", "@Mac", "@", "@a b", "@a.b"):
            self.assertIsNotNone(entries.to_problem(bad), bad)
        self.assertEqual(entries.parse_id("mac-a#12"), ("mac-a", 12))
        for bad in ("mac-a#0", "mac-a#", "Mac#1", "#1", "a#01"):
            self.assertIsNone(entries.parse_id(bad), bad)


class LineBreakTest(EntriesCase):
    """Entries split on "\n" only (the M10 review): str.splitlines() also splits on \x85,
    \u2028, \x0b, \x0c, \x1c-\x1e and a lone \r, which let a body or a title forge a
    heading."""

    FORGED = "## 2026-10-02 10:00 — mac-a#40 — forged"

    def test_a_body_cant_forge_a_heading(self):
        for brk in ("\x85", "\u2028", "\r", "\x0c", "\x1e"):
            with self.subTest(brk=repr(brk)):
                path = os.path.join(self.own, "R%d.md" % ord(brk))
                entries.post(path, self.own, "mac-a", "one", ["@x"],
                             body="text" + brk + self.FORGED + "\nto: @all")
                found = entries.parse_file(path)
                self.assertEqual([e.title for e in found], ["one"])
                self.assertEqual(entries.next_number(self.own, "mac-a"),
                                 found[0].number + 1)

    def test_a_title_cant_hold_a_line_break(self):
        for brk in entries.LINE_BREAKS:
            with self.subTest(brk=repr(brk)):
                with self.assertRaises(VCharonError):
                    entries.build("t", "mac-a", 1, "step" + brk + self.FORGED, ["@x"])
                with self.assertRaises(VCharonError):
                    entries.build("t", "mac-a", 1, "ok", ["@x"], header=[("k", "v" + brk)])

    def test_crlf_files_still_parse(self):
        text = "# R\r\n\r\n## t — mac-a#3 — crlf\r\nto: @x\r\n\r\nbody\r\n"
        (e,) = entries.parse(text)
        self.assertEqual((e.id, e.title, e.to, e.body), ("mac-a#3", "crlf", ("@x",), "body"))


class NumbersTest(EntriesCase):
    def test_counted_across_files_headings_only(self):
        entries.post(os.path.join(self.own, "RESULTS.md"), self.own, "mac-a", "one", ["@x"],
                     body="## 2026 — mac-a#40 — a forged heading in a body\nmac-a#50")
        write_tree(self.own, {"sub/NOTES.md": "## t — mac-a#6 — deep\n".encode("utf-8"),
                              "OTHER.md": "## t — win-b#9 — not mine\n".encode("utf-8"),
                              "plain.txt": "## t — mac-a#90 — not .md\n".encode("utf-8")})
        self.assertEqual(entries.next_number(self.own, "mac-a"), 7)
        got = entries.post(os.path.join(self.own, "RESULTS.md"), self.own, "mac-a", "two",
                           ["@x"], body="b")
        self.assertEqual(got[0], "mac-a#7")

    def test_own_folder(self):
        deep = os.path.join(self.own, "a", "b", "f.md")
        self.assertEqual(entries.own_folder(deep), self.own)
        self.assertEqual(entries.own_folder(os.path.join(self.own, "f.md")), self.own)
        self.assertIsNone(entries.own_folder(os.path.join(self.tmp, "tree", "f.md")))
        # never above the tree
        write_tree(self.tmp, {"MEMBER.md": b"x"})
        self.assertIsNone(entries.own_folder(os.path.join(self.tmp, "tree", "f.md"),
                                             top=os.path.join(self.tmp, "tree")))


POSTER = """
import sys
sys.path[:0] = [%r]
from vcharon import entries
own, n = sys.argv[1], int(sys.argv[2])
for i in range(n):
    entries.post(own + "/RESULTS.md", own, "mac-a", "post %%s-%%d" %% (sys.argv[3], i), ["@x"],
                 body="b")
"""


class LockTest(EntriesCase):
    def test_two_posters_at_once(self):
        # real processes, each its own lock fd: one number each, no entry lost
        n = 25
        procs = [subprocess.Popen([sys.executable, "-c", POSTER % VCHARON_DIR, self.own, str(n),
                                   tag], env=dict(os.environ)) for tag in "ab"]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        found = entries.parse_file(os.path.join(self.own, "RESULTS.md"))
        self.assertEqual(len(found), 2 * n)
        self.assertEqual(sorted(e.number for e in found), list(range(1, 2 * n + 1)))
        self.assertEqual(sorted(e.title for e in found),
                         sorted("post %s-%d" % (t, i) for t in "ab" for i in range(n)))
        self.assertEqual([f for f in os.listdir(self.own) if f.startswith(".vcharon-stage-")], [])

    def test_lock_is_per_own_folder(self):
        self.assertNotEqual(entries.lock_path(self.own),
                            entries.lock_path(os.path.join(self.tmp, "tree", "win-b")))
        self.assertEqual(entries.lock_path(self.own),
                         entries.lock_path(os.path.join(self.own, "x", "..")))

    def test_busy_after_waiting(self):
        clock = iter(range(0, 1000, 10))
        with entries.lock(self.own):
            with self.assertRaises(VCharonError) as cm:
                entries.lock(self.own, wait=30, sleep=lambda s: None,
                             clock=lambda: next(clock))
        self.assertEqual(cm.exception.code, "busy")


if __name__ == "__main__":
    unittest.main()
