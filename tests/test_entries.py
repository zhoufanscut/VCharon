"""Channel entries: their text, parsing, numbers, the own folder, and the locked
append."""

from __future__ import annotations

import os
import shutil
import tempfile
import types
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

    def test_a_title_cant_hold_a_control_character(self):
        for c in ("\x1b", "\x00", "\x7f", "\x9b", "\u202e", "\u200b", "\ufeff"):
            with self.subTest(c=repr(c)):
                self.assertIn("control or format character",
                              entries.one_line_problem("a" + c + "b"))
                with self.assertRaises(VCharonError):
                    entries.build("t", "mac-a", 1, "ok", ["@x"], header=[("k", "v" + c)])
        self.assertIsNone(entries.one_line_problem("a\tb mañana 東京"))

    def test_the_body_is_lf_only(self):
        # a lone CR is a line break to a terminal and to str.splitlines(): a heading after
        # one must be quoted as after "\n"
        text = entries.build("t", "mac-a", 1, "one", ["@x"],
                             body="a\r\nb\r" + self.FORGED + "\r\n\r\n")
        self.assertNotIn("\r", text)
        self.assertTrue(text.endswith("\n\na\nb\n> " + self.FORGED + "\n"), text)
        self.assertEqual([l for l in text.splitlines() if l.startswith("## ")],
                         ["## t — mac-a#1 — one"])
        # the other breaks stay as they are: entries split on "\n" only
        self.assertIn("a\x85b", entries.quote_body("a\x85b"))

    def test_crlf_files_still_parse(self):
        text = "# R\r\n\r\n## t — mac-a#3 — crlf\r\nto: @x\r\n\r\nbody\r\n"
        (e,) = entries.parse(text)
        self.assertEqual((e.id, e.title, e.to, e.body), ("mac-a#3", "crlf", ("@x",), "body"))


class ReplaceTest(EntriesCase):
    """The swap of a post: tried again on Windows while another program has the file open,
    for REPLACE_WAIT seconds; elsewhere a PermissionError is the file's own."""

    def replace(self, fails, windows, wait=entries.REPLACE_WAIT):
        """_replace with os.replace failing fails times: (raised, tries, pauses)."""
        tries, pauses = [], []
        now = [0.0]

        def fake(src, dst):
            tries.append(src)
            if len(tries) <= fails:
                raise PermissionError(13, "denied")

        def sleep(s):
            pauses.append(s)
            now[0] += s

        with mock.patch.object(entries, "fsops", types.SimpleNamespace(WINDOWS=windows)), \
                mock.patch.object(entries, "REPLACE_WAIT", wait), \
                mock.patch.object(entries, "REPLACE_PAUSE", 0.5), \
                mock.patch.object(entries.os, "replace", fake):
            try:
                entries._replace("a", "b", sleep=sleep, clock=lambda: now[0])
            except PermissionError:
                return True, len(tries), len(pauses)
        return False, len(tries), len(pauses)

    def test_windows_tries_again(self):
        # far more than the five tries a sync's upload could outlast
        self.assertEqual(self.replace(20, True), (False, 21, 20))

    def test_windows_gives_up_after_the_wait(self):
        raised, tries, pauses = self.replace(10 ** 6, True, wait=3)
        self.assertTrue(raised)
        # pauses of 0.5 s, exact in binary
        self.assertEqual(pauses, 6)
        self.assertEqual(tries, pauses + 1)
        # the wait leaves a post waiting on the lock its turn
        self.assertLess(entries.REPLACE_WAIT, entries.LOCK_WAIT)

    def test_elsewhere_raised_at_once(self):
        self.assertEqual(self.replace(1, False), (True, 1, 0))


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


class LeftTest(EntriesCase):
    """has_left: a LEAVE of the member numbered after its last JOIN or REJOIN, in any .md file
    of its folder but MEMBER.md; headings only."""

    def heads(self, *titles, name="mac-a"):
        return "".join("## t \u2014 %s#%d \u2014 %s\nto: @x\n\nb\n" % (name, n, t)
                       for n, t in enumerate(titles, 2)).encode()

    def left(self):
        return entries.has_left(self.own, "mac-a")

    def test_left_and_back(self):
        cases = [((), False), (("JOIN",), False), (("JOIN", "step", "LEAVE"), True),
                 (("JOIN", "LEAVE", "REJOIN"), False), (("JOIN", "LEAVE", "REJOIN", "LEAVE"),
                                                        True)]
        for titles, left in cases:
            with self.subTest(titles=titles):
                write_tree(self.own, {"RESULTS.md": self.heads(*titles)})
                self.assertEqual(self.left(), left)

    def test_by_number_across_files(self):
        # no RESULTS.md: a LEAVE in another file, then a REJOIN numbered later in a deeper one
        write_tree(self.own, {"NOTES.md": "## t \u2014 mac-a#3 \u2014 LEAVE\n".encode()})
        self.assertTrue(self.left())
        write_tree(self.own, {"sub/A.md": "## t \u2014 mac-a#4 \u2014 REJOIN\r\n".encode()})
        self.assertFalse(self.left())

    def test_number_not_file_order(self):
        # A.md is read before RESULTS.md, but the higher number decides
        for first, second, left in (("REJOIN", "LEAVE", False), ("LEAVE", "REJOIN", True)):
            with self.subTest(first=first):
                write_tree(self.own, {
                    "A.md": ("## t \u2014 mac-a#4 \u2014 %s\n" % first).encode(),
                    "RESULTS.md": ("## t \u2014 mac-a#3 \u2014 %s\n" % second).encode()})
                self.assertEqual(self.left(), left)

    def test_what_doesnt_count(self):
        write_tree(self.own, {
            # another member's LEAVE, one in a body, one in MEMBER.md, one in a .txt
            "RESULTS.md": self.heads("JOIN") + self.heads("LEAVE", name="win-b")
            + "\n> ## t \u2014 mac-a#9 \u2014 LEAVE\n".encode(),
            "MEMBER.md": "## t \u2014 mac-a#8 \u2014 LEAVE\n".encode(),
            "x.txt": "## t \u2014 mac-a#9 \u2014 LEAVE\n".encode()})
        self.assertFalse(self.left())
        # a missing folder has left nothing
        self.assertFalse(entries.has_left(os.path.join(self.tmp, "none"), "mac-a"))

    def test_another_members_leave(self):
        # numbered after this member's JOIN, in its folder: still not this member's leave
        write_tree(self.own, {"RESULTS.md": self.heads("JOIN")
                              + "## t \u2014 win-b#7 \u2014 LEAVE\n".encode()})
        self.assertFalse(self.left())
        self.assertTrue(entries.has_left(self.own, "win-b"))


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
