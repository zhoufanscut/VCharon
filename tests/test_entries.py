"""Channel entries: their text, parsing, numbers, the own folder, and the locked
append."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from vcharon import entries
from vcharon.proto import VCharonError

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
                             body="line 1\n## not a heading\n   ### nor this\nto: @all\nre: x#1\n")
        self.assertEqual(text, "\n## 2026-10-02 10:12 — mac-a#7 — step 3 done\nto: @laptop-ui\n"
                               "re: laptop-ui#3\nchannel: game\n\nline 1\n> ## not a heading\n"
                               ">    ### nor this\nto: @all\nre: x#1\n")
        (e,) = entries.parse("# RESULTS\n" + text)
        self.assertEqual((e.time, e.id, e.title, e.to, e.re, e.header, e.line),
                         ("2026-10-02 10:12", "mac-a#7", "step 3 done", ("@laptop-ui",),
                          "laptop-ui#3", [("channel", "game")], 3))
        # the body's "to:" and "re:" are body, after the blank line: they can't forge a header
        self.assertEqual(e.body,
                         "line 1\n> ## not a heading\n>    ### nor this\nto: @all\nre: x#1")

    def test_headings_without_an_id(self):
        es = entries.parse("# R\n\n## 2026-10-01 09:00 — an old entry\n\nbody\n\n"
                           "## 2026-10-01 09:01 — x#3 — a title — with dashes\nto: @all\n")
        self.assertEqual([(e.time, e.name, e.number, e.title) for e in es], [
            ("2026-10-01 09:00", None, None, "an old entry"),
            ("2026-10-01 09:01", "x", 3, "a title — with dashes")])
        self.assertEqual(es[0].body, "body")
        self.assertEqual(es[0].to, ())

    def test_to_and_ids(self):
        self.assertIsNone(entries.to_problem("@all"))
        self.assertIsNone(entries.to_problem("@mac-a"))
        for bad in ("all", "@Mac", "@", "@a b", "@a.b"):
            self.assertIsNotNone(entries.to_problem(bad), bad)
        self.assertEqual(entries.parse_id("mac-a#12"), ("mac-a", 12))
        for bad in ("mac-a#0", "mac-a#", "Mac#1", "#1", "a#01"):
            self.assertIsNone(entries.parse_id(bad), bad)


class LineBreakTest(EntriesCase):
    """Entries split on "\n" only: str.splitlines() also splits on \x85,
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
        write_tree(self.own, {"sub/NOTES.md": "## t — mac-a#6 — deep\n".encode(),
                              "OTHER.md": "## t — win-b#9 — not mine\n".encode(),
                              "plain.txt": "## t — mac-a#90 — not .md\n".encode()})
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


class LockTest(EntriesCase):
    def test_lock_is_per_own_folder(self):
        self.assertNotEqual(entries.lock_path(self.own),
                            entries.lock_path(os.path.join(self.tmp, "tree", "win-b")))
        self.assertEqual(entries.lock_path(self.own),
                         entries.lock_path(os.path.join(self.own, "x", "..")))

    def test_busy_after_waiting(self):
        clock = iter(range(0, 1000, 10))
        with entries.lock(self.own), self.assertRaises(VCharonError) as cm:
            entries.lock(self.own, wait=30, sleep=lambda s: None,
                         clock=lambda: next(clock))
        self.assertEqual(cm.exception.code, "busy")


if __name__ == "__main__":
    unittest.main()
