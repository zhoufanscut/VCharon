"""tools/mailbox_view.py: a channel's entries, every member's, in one order (DESIGN §14 M12a)."""

from __future__ import annotations

import datetime
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.util import CAN_SYMLINK, write_tree

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


view = _load("mailbox_view")

# this box's clock for every view here
NOW = datetime.datetime(2026, 10, 2, 12, 0, 30)
MINUTE = "2026-10-02 10:12"

# M10: a channel section, in channels.d/ next to vcharon.ini, as test_mailbox_watch.py's
MAILBOX = """
[mb.windows]
ssh            = devbox
mailbox.me     = windows
mailbox.leader = debian
mailbox.local  = {local}
mailbox.remote = ~/.local/state/vcharon/channels/mb
"""


def entry(eid, title="t", when=MINUTE, to="@all", re_=None, extra=(), body=""):
    """One entry's text, as mailbox_post.py writes it; eid None for a heading without one."""
    head = "## %s — %s — %s" % (when, eid, title) if eid else "## %s — %s" % (when, title)
    lines = ["", head, "to: " + to] + (["re: " + re_] if re_ else []) + list(extra)
    text = "\n".join(lines) + "\n"
    return text + ("\n" + body + "\n" if body else "")


def md(*entries):
    return ("# FILE\n" + "".join(entries)).encode("utf-8")


class ViewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {"VCHARON_HOME": os.path.join(self.tmp, "home")})
        patch.start()
        self.addCleanup(patch.stop)
        self.tree = os.path.join(self.tmp, "mb")
        os.mkdir(self.tree)

    def main(self, *argv):
        """(exit code, stdout lines, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            try:
                code = view.main(list(argv), now=NOW)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue().splitlines(), err.getvalue()

    def view(self, *argv):
        """The lines of a view of self.tree that must exit 0."""
        code, lines, err = self.main("--dir", self.tree, *argv)
        self.assertEqual((code, err), (0, ""), lines)
        return lines

    def ids(self, lines):
        """The IDs of the entry lines, in order."""
        return [line.split("  ")[1] for line in lines[1:] if not line.startswith(("note:", " "))]

    def notes(self, lines):
        return [line for line in lines if line.startswith("note:")]


class OrderTest(ViewCase):
    def test_one_minute_by_number_and_re(self):
        # path order is win-b's first, and puts mac-a#10 before #9; the time is one minute
        write_tree(self.tree, {
            "mac-a/A.md": md(entry("mac-a#10", "ten"), entry("mac-a#11", "eleven")),
            "mac-a/B.md": md(entry("mac-a#9", "nine")),
            "a-win/R.md": md(entry("a-win#3", "answer", re_="mac-a#10"), entry("a-win#4")),
        })
        lines = self.view()
        self.assertEqual(lines[0], "mb: 5 entries from 2 members (%s)" % self.tree)
        # a-win#3 is the smallest name, but waits for the mac-a#10 it answers; #10 after #9
        self.assertEqual(self.ids(lines), ["mac-a#9", "mac-a#10", "a-win#3", "a-win#4",
                                           "mac-a#11"])
        self.assertEqual(lines[3], "%s  a-win#3  @all  re mac-a#10  answer  (a-win/R.md)"
                         % MINUTE)
        self.assertEqual(self.notes(lines), [])

    def test_ties_by_name_then_number(self):
        write_tree(self.tree, {"b-x/R.md": md(entry("b-x#2")), "a-y/R.md": md(entry("a-y#5")),
                               # no edges in a wrong folder: the key alone, #9 before #10
                               "c-z/R.md": md(entry("w#10"), entry("w#9"))})
        self.assertEqual(self.ids(self.view()), ["a-y#5", "b-x#2", "w#9", "w#10"])

    def test_across_minutes_time_wins(self):
        write_tree(self.tree, {"aa/R.md": md(entry("aa#2", when="2026-10-02 10:11",
                                                   re_="bb#1")),
                               "bb/R.md": md(entry("bb#1", when="2026-10-02 10:12"))})
        lines = self.view()
        self.assertEqual(self.ids(lines), ["aa#2", "bb#1"])
        self.assertEqual(self.notes(lines),
                         ["note: aa#2 answers bb#1 but is stamped earlier: clocks differ?"])

    def test_a_cycle_and_a_self_re(self):
        write_tree(self.tree, {
            "bb/R.md": md(entry("bb#1", re_="aa#2"), entry("bb#2")),
            "aa/R.md": md(entry("aa#1"), entry("aa#2", re_="bb#2")),
            "cc/R.md": md(entry("cc#1", re_="cc#1")),
            # another minute keeps its re: edges
            "dd/R.md": md(entry("dd#1", when="2026-10-02 10:13", re_="ee#1")),
            "ee/R.md": md(entry("ee#1", when="2026-10-02 10:13")),
        })
        lines = self.view()
        # the cycle aa#2 > bb#2 > bb#1 > aa#2: number edges only there
        self.assertEqual(self.ids(lines), ["aa#1", "aa#2", "bb#1", "bb#2", "cc#1", "ee#1",
                                           "dd#1"])
        self.assertEqual(self.notes(lines), ["note: %s: re: lines make a cycle; that minute "
                                             "goes by number only" % MINUTE])

    def test_a_re_not_in_the_tree(self):
        write_tree(self.tree, {"aa/R.md": md(entry("aa#1", re_="zz#4"))})
        self.assertEqual(self.notes(self.view()), ["note: aa#1 answers zz#4, which isn't in "
                                                   "the tree (not synced yet, or a typo)"])

    def test_missing_bad_and_future_times(self):
        write_tree(self.tree, {"aa/R.md": md(
            entry("aa#1", when="2026-10-02 09:00"),
            entry("aa#2", when=""),
            entry("aa#3", when="yesterday"),
            # this box's current minute is no note; the next one is
            entry("aa#4", when="2026-10-02 12:00"),
            entry("aa#5", when="2026-10-02 12:01"))})
        lines = self.view()
        self.assertEqual(self.ids(lines), ["aa#2", "aa#3", "aa#1", "aa#4", "aa#5"])
        self.assertEqual(lines[1], "-  aa#2  @all  t  (aa/R.md)")
        self.assertEqual(self.notes(lines), [
            "note: aa#2 has no time: listed first",
            "note: aa#3 has a bad time 'yesterday': listed first",
            "note: aa#5 is stamped after now (2026-10-02 12:01)"])


class PlacedTest(ViewCase):
    def test_unplaced_entries(self):
        write_tree(self.tree, {
            "aa/A.md": md(entry("aa#1"), entry(None, "no id here", when="2026-10-02 10:00")),
            "aa/B.md": md(entry("aa#1", "copy", when="2026-10-02 10:00"), entry("aa#2")),
            "bb/R.md": md(entry("aa#3", "forged", re_="aa#2"), entry("bb#1", re_="aa#3")),
        })
        lines = self.view()
        self.assertEqual(self.ids(lines), ["-", "aa#1", "aa#1", "aa#2", "aa#3", "bb#1"])
        self.assertIn("2026-10-02 10:00  aa#1  @all  copy  (aa/B.md)", lines)
        self.assertEqual(self.notes(lines), [
            "note: aa/A.md line 6: no ID",
            "note: aa#1 again in aa/B.md: the one in aa/A.md is ordered",
            "note: aa#3 in bb/: not its folder's"])

    def test_what_is_left_out(self):
        tree = {"aa/MEMBER.md": md(entry("aa#1", "member")),
                "aa/sub/MEMBER.md": md(entry("aa#2", "a MEMBER.md below the top")),
                "aa/.vcharon-stage-x/R.md": md(entry("aa#3")),
                "aa/.vcharon-stage-y.md": md(entry("aa#4")),
                "aa/notes.txt": md(entry("aa#5")),
                ".vcharon-stage-z/R.md": md(entry("zz#1")),
                "Stray/R.md": md(entry("zz#2")),
                "top.md": md(entry("zz#3")),
                "empty/": None}
        write_tree(self.tree, tree)
        if CAN_SYMLINK:
            os.symlink(os.path.join(self.tree, "aa"), os.path.join(self.tree, "linked"))
            os.symlink(os.path.join(self.tree, "Stray"), os.path.join(self.tree, "aa", "in"))
            os.symlink(os.path.join(self.tree, "aa", "sub", "MEMBER.md"),
                       os.path.join(self.tree, "aa", "link.md"))
        lines = self.view()
        self.assertEqual(lines[0], "mb: 1 entry from 2 members (%s)" % self.tree)
        self.assertEqual(self.ids(lines), ["aa#2"])

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX modes, not root")
    def test_unreadable_folder_and_file(self):
        write_tree(self.tree, {"aa/R.md": md(entry("aa#1")), "aa/locked/R.md": md(entry("aa#2")),
                               "bb/R.md": md(entry("bb#1")), "cc/R.md": md(entry("cc#1"))})
        for path, mode in (("aa/locked", 0o755), ("bb/R.md", 0o644), ("cc", 0o755)):
            full = os.path.join(self.tree, path)
            os.chmod(full, 0)
            self.addCleanup(os.chmod, full, mode)
        lines = self.view()
        self.assertEqual(self.ids(lines), ["aa#1"])
        self.assertEqual(self.notes(lines), ["note: can't read aa/locked/: Permission denied",
                                             "note: can't read cc/: Permission denied",
                                             "note: can't read bb/R.md: Permission denied"])


class OutputTest(ViewCase):
    def setUp(self):
        ViewCase.setUp(self)
        write_tree(self.tree, {"aa/R.md": md(
            entry("aa#1", "one", when="2026-10-02 10:00"),
            entry("aa#2", "two", when="2026-10-02 10:01", to="@bb @cc", re_="bb#1",
                  extra=["kind: steps"], body="line 1\n\n> # quoted"),
            entry("aa#3", "three", when="2026-10-02 10:02")),
            "bb/R.md": md(entry("bb#1", "b", when="2026-10-02 09:00"))})

    def test_last(self):
        lines = self.view("--last", "2")
        self.assertEqual(lines[0], "mb: 4 entries from 2 members (%s)" % self.tree)
        self.assertEqual(self.ids(lines), ["aa#2", "aa#3"])

    def test_full(self):
        lines = self.view("--full", "--last", "2")
        self.assertEqual(lines[1:], [
            "2026-10-02 10:01  aa#2  @bb @cc  re bb#1  two  (aa/R.md)",
            "    kind: steps",
            "    line 1",
            "",
            "    > # quoted",
            "2026-10-02 10:02  aa#3  @all  three  (aa/R.md)"])

    def test_usage_and_a_missing_dir(self):
        for argv in ([], ["--dir", self.tree, "--job", "mb.windows"],
                     ["--dir", self.tree, "--config", "x"], ["--dir", self.tree, "--last", "0"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 2)
        missing = os.path.join(self.tmp, "nope")
        code, lines, err = self.main("--dir", missing)
        self.assertEqual((code, lines), (1, []))
        # the text after the colon is the OS's, localized on Windows (MAILBOX §5)
        self.assertTrue(err.startswith("ERROR can't read %s: " % missing), err)
        self.assertEqual(err.count("\n"), 1)


class JobTest(ViewCase):
    def setUp(self):
        ViewCase.setUp(self)
        self.config = os.path.join(self.tmp, "vcharon.ini")
        with open(self.config, "w", encoding="utf-8") as f:
            f.write("[vcharon]\n")
        write_tree(self.tmp, {"channels.d/mb.windows.ini":
                              MAILBOX.format(local=self.tree).encode("utf-8")})
        write_tree(self.tree, {"debian/R.md": md(entry("debian#2", "steps")),
                               "windows/R.md": md(entry("windows#2", "mine"))})

    def test_a_channel_section(self):
        code, lines, err = self.main("--job", "mb.windows", "--config", self.config)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines[0], "mb: 2 entries from 2 members (%s), as of this box's last "
                         "sync" % self.tree)
        self.assertEqual(self.ids(lines), ["debian#2", "windows#2"])

    def test_a_bad_section(self):
        code, lines, err = self.main("--job", "nope", "--config", self.config)
        self.assertEqual((code, lines), (1, []))
        self.assertEqual(err, "mailbox_view: %s has no channel section [nope]\n" % self.config)


if __name__ == "__main__":
    unittest.main()
