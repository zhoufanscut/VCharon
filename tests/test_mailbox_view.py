"""vcharon read: a channel's entries, every member's, in one order."""

from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from vcharon import channel_cmd, cli
from vcharon.mailbox import read as view

from tests import util
from tests.util import CAN_SYMLINK, TEST_MACHINE_ID, write_tree

# a member of the channel, for the record that finds it (DESIGN, "Which membership")
READ = ["read", "mb", "--project", "p"]

# this box's clock for every view here: naive local time, as read._now() gives
NOW = datetime.datetime(2026, 10, 2, 12, 0, 30)  # noqa: DTZ001
MINUTE = "2026-10-02 10:12"
# a heading's time in that minute
WHEN = MINUTE + ":00"

# a channel section, in channels.d/ next to vcharon.ini, as test_mailbox_watch.py's
MAILBOX = """
[mb.windows]
ssh            = devbox
mailbox.me     = windows
mailbox.leader = debian
mailbox.local  = {local}
mailbox.remote = ~/.local/state/vcharon/channels/mb
"""


def entry(eid, title="t", when=WHEN, to="@all", re_=None, extra=(), body=""):
    """One entry's text, as vcharon post writes it; eid None for a heading without one."""
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
        self.home = os.path.join(self.tmp, "home")
        patch = mock.patch.dict(os.environ, {"VCHARON_HOME": self.home})
        patch.start()
        self.addCleanup(patch.stop)
        self.tree = os.path.join(self.tmp, "mb")
        os.mkdir(self.tree)
        # a local member of mb: its tree is the channel's folder
        self.record("zz", None, self.tree)

    def record(self, name, ssh, remote):
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": name, "leader": "aa",
                                  "ssh": ssh, "remote": remote, "machine": TEST_MACHINE_ID,
                                  "project": "p", "role": None, **util.record_format()})

    def main(self, *argv):
        """vcharon read mb ARGV at NOW: (exit code, stdout lines, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                mock.patch.object(view, "_now", lambda: NOW):
            code = cli.main(READ + list(argv))
        return code, out.getvalue().splitlines(), err.getvalue()

    def view(self, *argv):
        """The lines of a view of self.tree that must exit 0."""
        code, lines, err = self.main(*argv)
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
                         % WHEN)
        self.assertEqual(self.notes(lines), [])

    def test_ties_by_name_then_number(self):
        write_tree(self.tree, {"b-x/R.md": md(entry("b-x#2")), "a-y/R.md": md(entry("a-y#5")),
                               # no edges in a wrong folder: the key alone, #9 before #10
                               "c-z/R.md": md(entry("w#10"), entry("w#9"))})
        self.assertEqual(self.ids(self.view()), ["a-y#5", "b-x#2", "w#9", "w#10"])

    def test_across_minutes_time_wins(self):
        write_tree(self.tree, {"aa/R.md": md(entry("aa#2", when="2026-10-02 10:11:00",
                                                   re_="bb#1")),
                               "bb/R.md": md(entry("bb#1", when="2026-10-02 10:12:00"))})
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
            "dd/R.md": md(entry("dd#1", when="2026-10-02 10:13:00", re_="ee#1")),
            "ee/R.md": md(entry("ee#1", when="2026-10-02 10:13:00")),
        })
        lines = self.view()
        # the cycle aa#2 > bb#2 > bb#1 > aa#2: number edges only there
        self.assertEqual(self.ids(lines), ["aa#1", "aa#2", "bb#1", "bb#2", "cc#1", "ee#1",
                                           "dd#1"])
        self.assertEqual(self.notes(lines), ["note: %s: re: lines make a cycle; that minute "
                                             "goes by number only" % MINUTE])

    def test_seconds_order_one_minute(self):
        # arrival order within the minute, whatever the names
        write_tree(self.tree, {
            "aa/R.md": md(entry("aa#2", when=MINUTE + ":50")),
            "bb/R.md": md(entry("bb#1", when=MINUTE + ":05")),
            "cc/R.md": md(entry("cc#1", when=MINUTE + ":05")),
        })
        lines = self.view()
        self.assertEqual(self.ids(lines), ["bb#1", "cc#1", "aa#2"])
        self.assertEqual(lines[1], "%s:05  bb#1  @all  t  (bb/R.md)" % MINUTE)
        self.assertEqual(self.notes(lines), [])

    def test_seconds_keep_numbers_and_re(self):
        # clocks a few seconds apart: the answer is stamped before its question, and one
        # member's #8 before its #7 (a hand edit); the edges still win within the minute
        write_tree(self.tree, {
            "aa/R.md": md(entry("aa#7", when=MINUTE + ":40"), entry("aa#8", when=MINUTE + ":10")),
            "bb/R.md": md(entry("bb#1", when=MINUTE + ":20", re_="aa#7")),
        })
        lines = self.view()
        self.assertEqual(self.ids(lines), ["aa#7", "aa#8", "bb#1"])
        self.assertEqual(self.notes(lines),
                         ["note: bb#1 answers aa#7 but is stamped earlier: clocks differ?"])

    def test_seconds_across_minutes(self):
        write_tree(self.tree, {"aa/R.md": md(entry("aa#1", when="2026-10-02 10:13:01")),
                               "bb/R.md": md(entry("bb#1", when="2026-10-02 10:12:59"))})
        self.assertEqual(self.ids(self.view()), ["bb#1", "aa#1"])

    def test_a_re_not_in_the_tree(self):
        write_tree(self.tree, {"aa/R.md": md(entry("aa#1", re_="zz#4"))})
        self.assertEqual(self.notes(self.view()), ["note: aa#1 answers zz#4, which isn't in "
                                                   "the tree (not synced yet, or a typo)"])

    def test_missing_bad_and_future_times(self):
        write_tree(self.tree, {"aa/R.md": md(
            entry("aa#1", when="2026-10-02 09:00:00"),
            entry("aa#2", when=""),
            entry("aa#3", when="yesterday"),
            # this box's current minute is no note; the next one is
            entry("aa#4", when="2026-10-02 12:00:00"),
            entry("aa#5", when="2026-10-02 12:01:00"),
            # a time without seconds is a bad one
            entry("aa#6", when=MINUTE))})
        lines = self.view()
        self.assertEqual(self.ids(lines), ["aa#2", "aa#3", "aa#6", "aa#1", "aa#4", "aa#5"])
        self.assertEqual(lines[1], "-  aa#2  @all  t  (aa/R.md)")
        self.assertEqual(self.notes(lines), [
            "note: aa#2 has no time: listed first",
            "note: aa#3 has a bad time 'yesterday': listed first",
            "note: aa#6 has a bad time '%s': listed first" % MINUTE,
            "note: aa#5 is stamped after now (2026-10-02 12:01:00)"])


class PlacedTest(ViewCase):
    def test_unplaced_entries(self):
        write_tree(self.tree, {
            "aa/A.md": md(entry("aa#1"), entry(None, "no id here", when="2026-10-02 10:00:00")),
            "aa/B.md": md(entry("aa#1", "copy", when="2026-10-02 10:00:00"), entry("aa#2")),
            "bb/R.md": md(entry("aa#3", "forged", re_="aa#2"), entry("bb#1", re_="aa#3")),
        })
        lines = self.view()
        self.assertEqual(self.ids(lines), ["-", "aa#1", "aa#1", "aa#2", "aa#3", "bb#1"])
        self.assertIn("2026-10-02 10:00:00  aa#1  @all  copy  (aa/B.md)", lines)
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
            entry("aa#1", "one", when="2026-10-02 10:00:00"),
            entry("aa#2", "two", when="2026-10-02 10:01:00", to="@bb @cc", re_="bb#1",
                  extra=["kind: steps"], body="line 1\n\n> # quoted"),
            entry("aa#3", "three", when="2026-10-02 10:02:00")),
            "bb/R.md": md(entry("bb#1", "b", when="2026-10-02 09:00:00"))})

    def test_last(self):
        lines = self.view("--last", "2")
        self.assertEqual(lines[0], "mb: 4 entries from 2 members (%s)" % self.tree)
        self.assertEqual(self.ids(lines), ["aa#2", "aa#3"])

    def test_full(self):
        lines = self.view("--full", "--last", "2")
        self.assertEqual(lines[1:], [
            "2026-10-02 10:01:00  aa#2  @bb @cc  re bb#1  two  (aa/R.md)",
            "    kind: steps",
            "    line 1",
            "",
            "    > # quoted",
            "2026-10-02 10:02:00  aa#3  @all  three  (aa/R.md)"])

    def test_json(self):
        code, lines, err = self.main("--json", "--last", "2")
        self.assertEqual((code, err), (0, ""))
        [line] = lines
        doc = json.loads(line)
        self.assertEqual(doc, {
            "channel": "mb", "folder": self.tree, "synced": False, "members": ["aa", "bb"],
            "member_info": [{"name": n, "box": None, "os": None, "agent": None,
                             "project": None, "vcharon": None} for n in ("aa", "bb")],
            "count": 4, "notes": [],
            "entries": [
                {"time": "2026-10-02 10:01:00", "id": "aa#2", "name": "aa", "number": 2,
                 "to": ["@bb", "@cc"], "re": "bb#1", "title": "two", "file": "aa/R.md",
                 "header": None, "body": None},
                {"time": "2026-10-02 10:02:00", "id": "aa#3", "name": "aa", "number": 3,
                 "to": ["@all"], "re": None, "title": "three", "file": "aa/R.md",
                 "header": None, "body": None}]})
        doc = json.loads(self.main("--json", "--full", "--last", "2")[1][0])
        self.assertEqual((doc["entries"][0]["header"], doc["entries"][0]["body"]),
                         ([["kind", "steps"]], "line 1\n\n> # quoted"))
        self.assertEqual((doc["entries"][1]["header"], doc["entries"][1]["body"]), ([], ""))

    def test_usage_and_a_missing_dir(self):
        for argv in (["--dir", self.tree], ["--job", "mb.windows"], ["--config", "x"],
                     ["--last", "0"], ["--last", "x"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 3)
        missing = os.path.join(self.tmp, "nope")
        self.record("zz", None, missing)
        code, lines, err = self.main()
        self.assertEqual((code, lines), (1, []))
        # the text after the colon is the OS's, localized on Windows
        self.assertTrue(err.startswith("ERROR can't read %s: " % missing), err)
        self.assertEqual(err.count("\n"), 1)


def member(name, version=None, extra=()):
    """A MEMBER.md whose #1 is name's, with a vcharon: line when version is given."""
    lines = ["box: mac", "project: web"] + (["vcharon: " + version] if version else [])
    return ("# MEMBER\n" + entry(name + "#1", "member", to="@aa",
                                 extra=lines + list(extra))).encode("utf-8")


class VersionTest(ViewCase):
    """The members' vcharon versions, from their MEMBER.md: a note when two differ, and each
    member's in --json."""

    def note(self, lines):
        found = [n for n in self.notes(lines) if "vcharon versions" in n]
        return found[0] if found else None

    def test_two_versions_differ(self):
        write_tree(self.tree, {"aa/MEMBER.md": member("aa", "0.1.0"),
                               "aa/R.md": md(entry("aa#2", re_="zz#9")),
                               "bb/MEMBER.md": member("bb", "0.2.0rc1"),
                               "cc/MEMBER.md": member("cc"), "dd/": None})
        lines = self.view()
        # after every other note: one per line, the last line
        self.assertEqual(lines[-2:], [
            "note: aa#2 answers zz#9, which isn't in the tree (not synced yet, or a typo)",
            "note: members' vcharon versions differ (from their MEMBER.md): aa 0.1.0, "
            "bb 0.2.0rc1, cc unknown, dd unknown; their guides may differ"])
        doc = json.loads(self.main("--json")[1][0])
        self.assertEqual([(m["name"], m["vcharon"]) for m in doc["member_info"]],
                         [("aa", "0.1.0"), ("bb", "0.2.0rc1"), ("cc", None), ("dd", None)])
        self.assertEqual(doc["member_info"][0], {"name": "aa", "box": "mac", "os": None,
                                                 "agent": None, "project": "web",
                                                 "vcharon": "0.1.0"})
        self.assertEqual(doc["notes"][-1], lines[-1].removeprefix("note: "))

    def test_quiet_unless_two_known_versions_differ(self):
        for tree in ({"aa/MEMBER.md": member("aa", "0.1.0"), "bb/MEMBER.md": member("bb", "0.1.0"),
                      "cc/MEMBER.md": member("cc")},
                     {"aa/MEMBER.md": member("aa", "0.1.0"), "bb/MEMBER.md": member("bb")},
                     {"aa/MEMBER.md": member("aa"), "bb/": None},
                     # a value in another shape counts as none
                     {"aa/MEMBER.md": member("aa", "0.1.0"),
                      "bb/MEMBER.md": member("bb", "0.2 beta")}):
            with self.subTest(tree=sorted(tree)):
                shutil.rmtree(self.tree)
                write_tree(self.tree, tree)
                self.assertIsNone(self.note(self.view()))

    def test_another_members_first_entry_and_unknown_lines(self):
        # only the folder's own #1 counts; a header line this vcharon doesn't know is ignored
        # (a newer vcharon may write more)
        write_tree(self.tree, {
            "aa/MEMBER.md": member("aa", "0.1.0", extra=["future: x", "no colon here"]),
            "bb/MEMBER.md": member("aa", "0.2.0") + member("bb", "0.1.0")[len("# MEMBER\n"):]})
        self.assertIsNone(self.note(self.view()))
        doc = json.loads(self.main("--json")[1][0])
        self.assertEqual([m["vcharon"] for m in doc["member_info"]], ["0.1.0", "0.1.0"])


class JobTest(ViewCase):
    def setUp(self):
        ViewCase.setUp(self)
        write_tree(self.home, {"vcharon.ini": b"[vcharon]\n", "channels.d/mb.windows.ini":
                               MAILBOX.format(local=self.tree).encode("utf-8")})
        # a remote member, windows: its local tree, as of the last sync
        os.remove(channel_cmd.record_path("mb", "zz"))
        self.record("windows", "devbox", "~/.local/state/vcharon/channels/mb")
        write_tree(self.tree, {"debian/R.md": md(entry("debian#2", "steps")),
                               "windows/R.md": md(entry("windows#2", "mine"))})

    def test_a_channel_section(self):
        code, lines, err = self.main()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines[0], "mb: 2 entries from 2 members (%s), as of this box's last "
                         "sync" % self.tree)
        self.assertEqual(self.ids(lines), ["debian#2", "windows#2"])

    def test_a_missing_section(self):
        os.remove(os.path.join(self.home, "channels.d", "mb.windows.ini"))
        code, lines, err = self.main()
        self.assertEqual((code, lines), (3, []))
        self.assertEqual(err.splitlines()[:2], [
            "ERROR config: %s has no channel section [mb.windows]"
            % os.path.join(self.home, "channels.d"),
            "  fix: join the channel again, with the --project and --role you joined with: it "
            "writes the section"])


class ConsoleTest(ViewCase):
    def test_utf8_whatever_the_console(self):
        # a console whose code page can't hold ñ (PYTHONIOENCODING stands in for Windows' 936):
        # vcharon read as its own process, as an agent runs it
        write_tree(self.tree, {"aa/R.md": md(entry("aa#1", "mañana"))})
        ran = subprocess.run([sys.executable, "-P", "-m", "vcharon"] + READ,
                             env=dict(os.environ, PYTHONIOENCODING="gbk"),
                             capture_output=True, timeout=60, check=False)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout.decode("utf-8").splitlines()[1:],
                         ["%s  aa#1  @all  mañana  (aa/R.md)" % WHEN])


if __name__ == "__main__":
    unittest.main()
