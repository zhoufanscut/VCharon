"""vcharon post: a dated entry appended atomically, in channel mode with IDs, a
header and a lock. The command line posts into the member's RESULTS.md, or the
leader's STEPS.md; post() itself is tested on other paths too, since it checks whatever path
it's given."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from vcharon import channel_cmd, cli, entries, plugin
from vcharon.mailbox import post as post_mod
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

from tests import util
from tests.util import TEST_MACHINE_ID, write_tree

# 2026-10-01 09:05:46 on this machine's clock, whatever its zone
T0 = time.mktime((2026, 10, 1, 9, 5, 46, 0, 0, -1))
# the post command of this member, windows: its record is for the project p
POST = ["post", "mb", "--project", "p"]
# the leader's, debian: project q
LEADER = ["post", "mb", "--project", "q"]


def member_md(name, leader, channel="mb"):
    """MEMBER.md as vcharon join writes it."""
    return ("# MEMBER\n\n## 2026-10-01 09:00 — %s#1 — member\nto: @%s\nchannel: %s\nname: %s\n"
            "leader: %s\n" % (name, leader, channel, name, leader)).encode("utf-8")


class PostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # the post lock goes to vcharon's state dir: one per test
        self.vcharon_home = os.path.join(self.tmp, "vcharon-home")
        patch = mock.patch.dict(os.environ, {"VCHARON_HOME": self.vcharon_home,
                                             "VCHARON_TEST_MACHINE_ID": TEST_MACHINE_ID})
        patch.start()
        self.addCleanup(patch.stop)
        # a channel's folder in a channel root: this member windows, and its leader debian,
        # both local members of this box, as vcharon join --local leaves them
        self.tree = os.path.join(self.tmp, "mb")
        self.folder = os.path.join(self.tree, "windows")
        write_tree(self.tree, {"windows/MEMBER.md": member_md("windows", "debian"),
                               "debian/MEMBER.md": member_md("debian", "debian")})
        for name, project in (("windows", "p"), ("debian", "q")):
            channel_cmd.write_record({"version": 1, "channel": "mb", "name": name,
                                      "leader": "debian", "ssh": None, "remote": self.tree,
                                      "machine": TEST_MACHINE_ID, "project": project,
                                      "role": None, **util.record_format()})
        self.file = os.path.join(self.folder, "RESULTS.md")

    def main(self, argv, stdin=None, t=T0):
        """vcharon <argv> in this process, at the time t: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                mock.patch.object(cli, "_stdin_bytes", lambda: stdin or b""), \
                mock.patch("time.time", lambda: t):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def post(self, *argv, body="b", stdin=None, t=T0):
        """vcharon post as windows, with --title t and the body, and --to @debian unless argv
        has a --to."""
        argv = list(argv) + ([] if "--to" in argv else TO)
        if "--title" not in argv:
            argv += ["--title", "t"]
        if body is not None:
            argv += ["--body", body]
        return self.main(POST + argv, stdin=stdin, t=t)

    def post_at(self, path, me="windows", to=("@debian",), body="b", t=T0):
        """A post into path as me: (the ID, stderr), or the refusal, a VCharonError. A path in
        windows's own folder goes through the command line, as --file; any other path (another
        member's folder, outside every one) only post() itself can be given."""
        rel = os.path.relpath(path, self.folder)
        if me == "windows" and not rel.startswith(os.pardir) and not os.path.isabs(rel):
            argv = POST + ["--file", rel.replace(os.sep, "/"), "--title", "t", "--body", body,
                           "--to"] + list(to)
            code, out, err = self.main(argv, t=t)
            if code == 0:
                return out.split()[1], err
            lines = err.splitlines()
            m = re.match(r"\AERROR ([a-z_]+): (.*)\Z", lines[0])
            self.assertIsNotNone(m, err)
            fix = [l[len("  fix: "):] for l in lines if l.startswith("  fix: ")]
            e = VCharonError(m.group(1), m.group(2), fix[0] if fix else None)
            self.assertEqual(code, e.exit_code, err)
            return e
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            try:
                id_, _ = post_mod.post(path, me, list(to), "t", body=body, clock=lambda: t)
            except VCharonError as e:
                return e
        return id_, err.getvalue()

    def content(self, path=None):
        with open(path or self.file, "rb") as f:
            return f.read().decode("utf-8")


TO = ["--to", "@debian"]


class PostTest(PostCase):
    def test_creates_then_appends(self):
        code, out, err = self.main(POST + ["--title", "step 7 done", "--body", "one line"] + TO)
        self.assertEqual((code, out, err),
                         (0, "posted windows#2 — step 7 done into windows/RESULTS.md, "
                          "to @debian at 2026-10-01 09:05:46\n", ""))
        first = ("# RESULTS\n\n## 2026-10-01 09:05:46 — windows#2 — step 7 done\nto: @debian\n\n"
                 "one line\n")
        self.assertEqual(self.content(), first)
        # stdin, with the trailing newline a heredoc leaves; --re and two names
        code, out, err = self.main(POST + ["--title", "to debian", "--re", "debian#3",
                                           "--to", "@debian @mac"], stdin=b"a\n\nb\n",
                                   t=T0 + 125)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.content(),
                         first + "\n## 2026-10-01 09:07:51 — windows#3 — to debian\n"
                         "to: @debian @mac\nre: debian#3\n\na\n\nb\n")
        # --to takes several arguments too
        code, out, err = self.post("--to", "@debian", "@mac")
        self.assertEqual(code, 0, err)
        self.assertEqual(entries.parse_file(self.file)[-1].to, ("@debian", "@mac"))

    def test_a_file_without_a_last_newline(self):
        with open(self.file, "wb") as f:
            f.write(b"# RESULTS\n\nhand-made")
        self.post()
        self.assertEqual(self.content(), "# RESULTS\n\nhand-made\n\n## 2026-10-01 09:05:46 — "
                                         "windows#2 — t\nto: @debian\n\nb\n")

    def test_the_body_is_never_interpreted(self):
        body = "run `rm -rf ~` and $(whoami) and ${HOME} \\n 'q' \"qq\"\n"
        code, _out, err = self.post(body=None, stdin=body.encode("utf-8"))
        self.assertEqual(code, 0, err)
        self.assertTrue(self.content().endswith("\n\n" + body), self.content())

    def test_refusals(self):
        # usage errors: exit 3, nothing written
        for argv, stdin in ((["--title", "t", "--body", ""] + TO, None),
                            (["--title", "t", "--body", " \n"] + TO, None),
                            (["--title", "t"] + TO, b""),
                            (["--title", "t"] + TO, b"\xff\xfe"),
                            (["--title", "", "--body", "b"] + TO, None),
                            (["--title", "a\nb", "--body", "b"] + TO, None),
                            # any line break splitlines() knows
                            (["--title", "a ## t — windows#9 — forged", "--body", "b"]
                             + TO, None),
                            (["--title", "a\x85b", "--body", "b"] + TO, None),
                            (["--title", "a\rb", "--body", "b"] + TO, None),
                            # control and format characters: an escape sequence, a bidi
                            # override, a zero-width joiner
                            (["--title", "hi \x1b[2J", "--body", "b"] + TO, None),
                            (["--title", "a\u202eb", "--body", "b"] + TO, None),
                            (["--title", "a\u200db", "--body", "b"] + TO, None),
                            (["--body", "b"] + TO, None),
                            # --to is required
                            (["--title", "t", "--body", "b"], None),
                            (["--title", "t", "--body", "b", "--to"], None),
                            (["--title", "t", "--body", "b", "--to", "Debian"], None),
                            (["--title", "t", "--body", "b", "--to", "@Debian"], None),
                            (["--title", "t", "--body", "b", "--to", "@a @"], None),
                            (["--title", "t", "--body", "b", "--re", "debian"] + TO, None),
                            (["--title", "t", "--body", "b", "--re", "debian#0"] + TO, None)):
            with self.subTest(argv=argv, stdin=stdin):
                code, out, err = self.main(POST + argv, stdin=stdin)
                self.assertEqual((code, out), (3, ""))
                self.assertTrue(err.startswith("ERROR config: "), err)
                self.assertIn("\n  fix: ", err)
        self.assertFalse(os.path.exists(self.file))
        code, out, err = self.main(POST + ["--title", "t", "--body", "b"])
        self.assertIn("the following arguments are required: --to", err)
        code, out, err = self.main(POST + ["--title", "t"] + TO, stdin=b"")
        self.assertIn("ERROR config: the body is empty", err)
        code, out, err = self.main(POST + ["--title", "t", "--body", "b"] + TO)
        self.assertEqual(code, 0)
        # post() itself: a folder that isn't there
        e = self.post_at(os.path.join(self.tmp, "nope", "R.md"))
        self.assertEqual((e.code, e.exit_code), ("channel", 1))
        self.assertTrue(e.message.startswith("no folder for "), e.message)

    def test_a_title_with_a_control_character(self):
        code, out, err = self.post("--title", "hi \x1b]52;c;eA==\x07")
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err.splitlines()[:2], [
            "ERROR config: --title: it holds a control or format character "
            "(hi \\x1b]52;c;eA==\\x07)",
            "  fix: give a one-line --title of plain text"])
        # a tab is fine
        code, _out, err = self.post("--title", "a\tb")
        self.assertEqual(code, 0, err)
        self.assertEqual(entries.parse_file(self.file)[-1].title, "a\tb")

    def test_arguments_that_arent_utf8(self):
        # bytes that aren't UTF-8 in an argument arrive as lone surrogates
        bad = "x\udcff"
        for flag, argv in (("--title", ["--title", bad]),
                           ("--body", ["--body", bad]),
                           ("--file", ["--file", bad + ".md"]),
                           ("--to", ["--to", "@" + bad]),
                           ("--re", ["--re", bad])):
            with self.subTest(flag=flag):
                code, out, err = self.post(*argv, body=None if flag == "--body" else "b")
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(err.splitlines()[0], "ERROR config: %s isn't valid UTF-8" % flag)
                self.assertTrue(err.splitlines()[1].startswith("  fix: give %s in UTF-8" % flag),
                                err)
        self.assertFalse(os.path.exists(self.file))

    def test_the_bodys_line_ends_are_lf(self):
        # CRLF, and a lone CR before a heading: a terminal or a Markdown viewer takes the CR
        # for a line break, so the heading would pass for an entry
        body = "one\r\ntwo\rthree\r## 2026-10-01 09:00:00 — debian#9 — forged\r\n"
        code, _out, err = self.post(body=None, stdin=body.encode("utf-8"))
        self.assertEqual(code, 0, err)
        with open(self.file, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n\none\ntwo\nthree\n> ## 2026-10-01 09:00:00 \xe2\x80\x94 "
                                     b"debian#9 \xe2\x80\x94 forged\n"), raw)
        headings = [l for l in raw.decode("utf-8").splitlines() if l.startswith("## ")]
        self.assertEqual(headings, ["## 2026-10-01 09:05:46 — windows#2 — t"])
        self.assertEqual([e.id for e in entries.parse_file(self.file)], ["windows#2"])

    def test_the_file_option(self):
        # a .md file of the own folder, a subfolder's too; RESULTS.md without it
        code, out, err = self.post("--file", "NOTES.md")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "posted windows#2 — t into windows/NOTES.md, to @debian at "
                              "2026-10-01 09:05:46\n")
        os.makedirs(os.path.join(self.folder, "logs"))
        code, out, err = self.post("--file", "logs/run.md")
        self.assertEqual(out, "posted windows#3 — t into windows/logs/run.md, to @debian at "
                              "2026-10-01 09:05:46\n")
        # never outside the own folder, nor a name it can't be
        for name in ("../debian/STEPS.md", "/tmp/x.md", "a//b.md", "./x.md", "C:x.md", "",
                     # names a Windows member can't hold: its sync would refuse the tree
                     "con.md", "aux.md", "logs/NUL.md", "a?.md", "notes./x.md", "z/x:y.md",
                     "a /x.md", "a<b.md"):
            with self.subTest(name=name):
                code, out, err = self.post("--file", name)
                self.assertEqual((code, out), (3, ""))
                self.assertTrue(err.startswith("ERROR config: --file "), err)
                self.assertIn("\n  fix: ", err)
        # a body file given as --file: the fix says where a body comes from
        self.assertIn("the body comes from --body or stdin, never from --file",
                      self.post("--file", "/tmp/x.md")[2])
        self.assertEqual(sorted(os.listdir(self.folder)), ["MEMBER.md", "NOTES.md", "logs"])
        # a subfolder that isn't there: make it first (never a rejoin, which wakes the leader)
        code, out, err = self.post("--file", "nope/x.md")
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR channel: nope/ isn't in your own folder %s" % self.folder,
            "  fix: make nope/ in your own folder first, or post into RESULTS.md"])
        code, out, err = self.post("--file", "logs/deep/x.md")
        self.assertTrue(err.startswith("ERROR channel: logs/deep/ isn't in your own folder "),
                        err)
        # a file or folder of the wrong kind on the way
        write_tree(self.folder, {"plain": b"p", "dir.md/": None})
        for name in ("plain/x.md", "dir.md"):
            with self.subTest(name=name):
                code, out, err = self.post("--file", name)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("ERROR unsafe_path: "), err)
        # --steps is the leader's --file STEPS.md: not both
        code, out, err = self.main(LEADER + ["--steps", "--file", "STEPS.md", "--to", "@all",
                                             "--title", "t", "--body", "b"])
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err.splitlines()[:2], [
            "ERROR config: --steps is --file STEPS.md: give one of them",
            "  fix: leave out --file, or --steps"])

    @unittest.skipUnless(util.CAN_SYMLINK, "no symlinks here")
    def test_never_through_a_link(self):
        # another local member can plant a link in this folder: a post never writes through
        # one, nor numbers past what it can't read
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.folder, "linkdir"))
        os.symlink(os.path.join(outside, "f.md"), os.path.join(self.folder, "linked.md"))
        for name, path in (("linkdir/x.md", "linkdir"), ("linked.md", "linked.md")):
            with self.subTest(name=name):
                code, out, err = self.post("--file", name)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err.splitlines()[:2], [
                    "ERROR unsafe_path: %s is a symlink: a post never writes through one"
                    % os.path.join(self.folder, path),
                    "  fix: remove the link from your own folder, then post again"])
        self.assertEqual(os.listdir(outside), [])

    def test_steps_md_is_the_leaders_by_any_name(self):
        # --file STEPS.md from a member is --steps: refused alike, in any case at the top
        for name in ("STEPS.md", "steps.md"):
            with self.subTest(name=name):
                code, out, err = self.post("--file", name)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err.splitlines()[:2], [
                    "ERROR channel: STEPS.md is the leader's (debian): its plan",
                    "  fix: leave out --file %s: your entries go into RESULTS.md" % name])
        self.assertEqual(os.listdir(self.folder), ["MEMBER.md"])
        # below the top it's any other file
        os.makedirs(os.path.join(self.folder, "old"))
        self.assertEqual(self.post("--file", "old/STEPS.md")[0], 0)
        # the leader's own --file STEPS.md is its plan
        code, out, err = self.main(LEADER + ["--file", "STEPS.md", "--to", "@all", "--title",
                                             "plan", "--body", "b"])
        self.assertEqual((code, err), (0, ""))

    def test_no_body_on_a_terminal(self):
        # never a wait for a body typed on the terminal
        with mock.patch.object(cli, "_stdin_is_terminal", return_value=True):
            code, out, err = self.post(body=None)
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err.splitlines()[:2], [
            "ERROR config: no --body, and stdin is a terminal",
            "  fix: give --body TEXT, or the body on stdin: a file, or a quoted heredoc "
            "(<<'EOF')"])
        self.assertFalse(os.path.exists(self.file))
        # --body is fine there
        with mock.patch.object(cli, "_stdin_is_terminal", return_value=True):
            self.assertEqual(self.post()[0], 0)

    def test_twins_are_refused(self):
        # a name that is another one next to it on macOS or Windows would make the tree
        # refused there; nothing is written
        answers = os.path.join(self.folder, "answers.md")
        with open(answers, "wb") as f:
            f.write(b"# answers\n")
        before = sorted(os.listdir(self.folder))
        e = self.post_at(os.path.join(self.folder, "ANSWERS.md"))
        self.assertEqual(e.message, "ANSWERS.md and answers.md are one name on macOS and "
                                    "Windows: post to answers.md")
        self.assertEqual(e.hint, "remove or rename one of those names in your own folder, then "
                                 "post again")
        self.assertEqual(sorted(os.listdir(self.folder)), before)
        with open(answers, "rb") as f:
            self.assertEqual(f.read(), b"# answers\n")
        # the exact name appends
        self.assertEqual(self.post_at(answers), ("windows#2", ""))
        # composed and decomposed: one name on macOS only
        write_tree(self.folder, {"\u00e9.md": b"x"})
        e = self.post_at(os.path.join(self.folder, "e\u0301.md"))
        self.assertEqual(e.message, "e\u0301.md and \u00e9.md are one name on macOS: post to "
                                    "\u00e9.md")
        # the folder's name: Mac/ next to mac/, where mac/ is the member's folder; the twin is
        # told before "not in a member's folder"
        write_tree(self.tree, {"mac/MEMBER.md": member_md("mac", "debian"), "Mac/": None})
        e = self.post_at(os.path.join(self.tree, "Mac", "x.md"))
        self.assertEqual(e.message, "Mac/ and mac/ are one folder on macOS and Windows: post "
                                    "into mac/")
        # nothing written: on a disk that folds case, Mac/ is mac/ and holds its MEMBER.md
        self.assertEqual(os.listdir(os.path.join(self.tree, "Mac")),
                         ["MEMBER.md"] if util.folds_case() else [])
        # through the command line: RESULTS.md with a twin, refused with its fix line
        write_tree(self.folder, {"Results.md": b"R"})
        if not util.folds_case():
            code, out, err = self.post()
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err.splitlines()[:2], [
                "ERROR channel: RESULTS.md and Results.md are one name on macOS and Windows: "
                "post to Results.md",
                "  fix: remove or rename one of those names in your own folder, then post "
                "again"])
        os.remove(os.path.join(self.folder, "Results.md"))
        # both twins there already: never point at one of them
        if util.folds_case():
            self.skipTest(util.FOLDS_CASE)
        notes = os.path.join(self.folder, "notes.md")
        write_tree(self.folder, {"Notes.md": b"N", "notes.md": b"n"})
        for target in ("notes.md", "Notes.md", "NOTES.md"):
            with self.subTest(target=target):
                e = self.post_at(os.path.join(self.folder, target))
                self.assertTrue(e.message.endswith("are one name on macOS and Windows: remove "
                                                   "or rename one first"), e.message)
        self.assertEqual(e.message, "NOTES.md, Notes.md and notes.md are one name on macOS and "
                                    "Windows: remove or rename one first")
        with open(notes, "rb") as f:
            self.assertEqual(f.read(), b"n")
        # a member's folder next to a stray that isn't a writer's name: a note, and the post
        # goes on (clients leave the stray out); never "post into Debian/"
        write_tree(self.tree, {"Debian/": None})
        code, out, err = self.main(LEADER + ["--steps", "--title", "t", "--body", "b"] + TO)
        self.assertEqual(code, 0, err)
        note = ("note: %s are one folder on macOS and Windows: remove or rename %s (at the top "
                "of the tree clients leave it out; below it, macOS and Windows clients get "
                "nothing until one is removed)\n")
        self.assertEqual(err, note % ("Debian/ and debian/", "Debian/"))
        self.assertTrue(os.path.exists(os.path.join(self.tree, "debian", "STEPS.md")))
        # the same below the top, where the twin blocks clients: the note says so too, and
        # the post (which makes no new twin) goes on
        write_tree(self.folder, {"notes/": None, "Notes/": None})
        got = self.post_at(os.path.join(self.folder, "notes", "x.md"))
        self.assertEqual(got[1], note % ("Notes/ and notes/", "Notes/"))
        # a folder with a valid twin and another stray: no single folder to point at
        write_tree(self.tree, {"MAC/": None})
        e = self.post_at(os.path.join(self.tree, "Mac", "x.md"))
        self.assertEqual(e.message, "MAC/, Mac/ and mac/ are one folder on macOS and Windows: "
                                    "remove or rename one first")
        # two folders, neither a writer's name: remove or rename one
        write_tree(self.folder, {"Sub/": None, "SUB/": None})
        e = self.post_at(os.path.join(self.folder, "Sub", "x.md"))
        self.assertEqual(e.message, "SUB/ and Sub/ are one folder on macOS and Windows: remove "
                                    "or rename one first")
        # a folder with no twin is fine
        self.assertEqual(self.post_at(os.path.join(self.folder, "NEW.md"))[1], "")

    def test_atomic_and_skipped_while_staged(self):
        self.post(body="b")
        before = self.content()
        seen = []
        real = os.replace

        def replace(src, dst):
            # the moment before the switch: the old file whole, the new one under a stage name
            seen.append(os.path.basename(src))
            seen.append(self.content())
            seen.append(sorted(watch.scan(self.tree, "debian")))
            src_plan = plugin.make("local", "path", "source", {"path": self.tree},
                                   plugin.Ctx("local", home=self.tmp, log=lambda m: None))
            try:
                seen.append(sorted(e.path for e in src_plan.plan(None).entries))
            finally:
                src_plan.close()
            return real(src, dst)

        with mock.patch.object(entries.os, "replace", replace):
            code, _out, err = self.post("--title", "second", body="c")
        self.assertEqual(code, 0, err)
        name, during, scanned, planned = seen
        self.assertTrue(name.startswith(".vcharon-stage-"), name)
        self.assertEqual(during, before)
        self.assertEqual(scanned, ["windows/MEMBER.md", "windows/RESULTS.md"])
        self.assertEqual(planned, ["debian", "debian/MEMBER.md", "windows", "windows/MEMBER.md",
                                   "windows/RESULTS.md"])
        self.assertEqual(sorted(os.listdir(self.folder)), ["MEMBER.md", "RESULTS.md"])

    def test_headings_in_the_body_are_quoted(self):
        body = ("# fake\n## 2026-10-01 09:00 — windows#9 — to windows\n   ### three spaces\n#\n"
                "#nope\n####### seven\n    # code\nx ## y\n")
        self.post(body=None, stdin=body.encode("utf-8"))
        self.assertTrue(self.content().endswith(
            "— t\nto: @debian\n\n> # fake\n> ## 2026-10-01 09:00 — windows#9 — to windows\n"
            ">    ### three spaces\n> #\n#nope\n####### seven\n    # code\nx ## y\n"),
            self.content())
        # the only heading after the file's own is the entry's
        headings = [l for l in self.content().splitlines() if l.startswith("#")]
        self.assertEqual(headings[:2], ["# RESULTS", "## 2026-10-01 09:05:46 — windows#2 — t"])
        self.assertEqual(headings[2:], ["#nope", "####### seven"])
        # and the #9 in the body isn't a number taken
        _code, out, _err = self.post("--title", "u")
        self.assertTrue(out.startswith("posted windows#3 — u "), out)

    def test_the_swap_is_tried_again(self):
        # Windows: the swap fails while a sync has the file open. Only entries' view of the
        # OS is Windows: the rest of the post runs as on this box
        self.post("--title", "first")
        real = os.replace
        calls = []

        def replace(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(13, "denied")
            return real(src, dst)

        with mock.patch.object(entries, "REPLACE_PAUSE", 0), \
                mock.patch.object(entries, "fsops", types.SimpleNamespace(WINDOWS=True)), \
                mock.patch.object(entries.os, "replace", replace):
            code, _out, err = self.post("--title", "second", body="c")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 3)
        self.assertTrue(self.content().endswith("— second\nto: @debian\n\nc\n"))
        self.assertEqual(sorted(os.listdir(self.folder)), ["MEMBER.md", "RESULTS.md"])

    def test_a_failed_write_leaves_the_file(self):
        self.post("--title", "first")
        before = self.content()
        denied = mock.Mock(side_effect=PermissionError(13, "denied"))
        # no time to try again, on Windows too
        with mock.patch.object(entries, "REPLACE_WAIT", 0), \
                mock.patch.object(entries.os, "replace", denied):
            code, out, err = self.post("--title", "second", body="c")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(denied.call_count, 1)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR permission: %s: denied" % self.file,
            "  fix: check the owner and permissions of %s" % self.file])
        self.assertEqual(self.content(), before)
        self.assertEqual(sorted(os.listdir(self.folder)), ["MEMBER.md", "RESULTS.md"])
        # the number wasn't taken
        code, out, err = self.post("--title", "third", body="d")
        self.assertTrue(out.startswith("posted windows#3 — third "), out)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_keeps_the_mode(self):
        write_tree(self.folder, {"RESULTS.md": b"# RESULTS\n"})
        os.chmod(self.file, 0o640)
        self.post()
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o640)


class ChannelTest(PostCase):
    """Posting in a channel."""

    def numbers(self):
        """The IDs in every entry heading of the own folder, in file order."""
        found = []
        for path in entries.md_files(self.folder):
            found += [e.id for e in entries.parse_file(path)]
        return found

    def test_outside_a_members_folder(self):
        # no MEMBER.md up the tree: refused
        os.makedirs(os.path.join(self.tree, "mac"))
        path = os.path.join(self.tree, "mac", "RESULTS.md")
        e = self.post_at(path)
        self.assertEqual(e.message, "%s: not in a channel member's folder (no MEMBER.md in its "
                                    "folder or above)" % path)
        self.assertEqual(e.hint, "join the channel again, with the --project and --role you "
                                 "joined with: a rejoin writes MEMBER.md")
        self.assertEqual(os.listdir(os.path.join(self.tree, "mac")), [])
        # a MEMBER.md that is a folder isn't one
        write_tree(self.tree, {"mac/MEMBER.md/": None})
        self.assertIsInstance(self.post_at(path), VCharonError)
        # the command line, whose own folder lost its MEMBER.md
        os.remove(os.path.join(self.folder, "MEMBER.md"))
        code, _out, err = self.post()
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("ERROR channel: %s: not in a channel member's folder"
                                       % self.file), err)

    def test_member_and_channel_md_are_refused(self):
        before = util.read_tree(self.tree)
        for name in ("MEMBER.md", "CHANNEL.md", "member.md", "Channel.md"):
            with self.subTest(name=name):
                e = self.post_at(os.path.join(self.folder, name))
                self.assertEqual((e.message, e.hint), ("%s is vcharon's to write" % name,
                                                       "post into RESULTS.md"))
        # nor a file that isn't .md: the numbers and the watcher read only those
        e = self.post_at(os.path.join(self.folder, "RESULTS.txt"))
        self.assertEqual(e.message, "RESULTS.txt: entries go in .md files (the watcher and the "
                                    "numbering read only those)")
        self.assertEqual(util.read_tree(self.tree), before)

    def test_all_is_the_leaders(self):
        code, out, err = self.post("--to", "@all")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines()[:2], ["ERROR channel: @all is the leader's (debian)",
                                                "  fix: address members by name: --to @<name> "
                                                "..."])
        self.assertFalse(os.path.exists(self.file))
        # the leader's plan: --steps, into STEPS.md
        steps = os.path.join(self.tree, "debian", "STEPS.md")
        code, out, err = self.main(LEADER + ["--steps", "--to", "@all", "--title", "plan",
                                             "--body", "1. do"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "posted debian#2 — plan into debian/STEPS.md, to @all at "
                              "2026-10-01 09:05:46\n")
        self.assertEqual(entries.parse_file(steps)[0].to, ("@all",))
        # and its answers into its own RESULTS.md
        code, out, err = self.main(LEADER + ["--to", "@windows", "--title", "a", "--body", "b"])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.tree, "debian", "RESULTS.md")))
        # a MEMBER.md without a leader: nobody's @all
        write_tree(self.folder, {"MEMBER.md": b"# MEMBER\n\n## x \xe2\x80\x94 windows#1 "
                                              b"\xe2\x80\x94 member\nto: @debian\n"})
        e = self.post_at(self.file, to=["@all"])
        self.assertEqual(e.message, "@all is the leader's (MEMBER.md names none)")

    def test_only_your_own_folder(self):
        # a channel root lets any member write any folder; post() checks the name it's
        # given against the folder's (the command line gives the membership's own)
        before = util.read_tree(self.tree)
        steps = os.path.join(self.tree, "debian", "STEPS.md")
        e = self.post_at(steps, me="windows", to=["@all"])
        self.assertEqual((e.message, e.hint), ("%s is debian's folder, not windows's" % steps,
                                               "post only in your own folder"))
        # a subfolder belongs to the own folder too
        os.makedirs(os.path.join(self.tree, "debian", "sub"))
        deep = os.path.join(self.tree, "debian", "sub", "p.md")
        self.assertIsInstance(self.post_at(deep, me="windows"), VCharonError)
        self.assertEqual(util.read_tree(self.tree), dict(before, **{"debian/sub/": None}))
        code, out, err = self.post()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "posted windows#2 — t into windows/RESULTS.md, to @debian at "
                              "2026-10-01 09:05:46\n")

    def test_an_unknown_name_is_a_note(self):
        code, _out, err = self.post("--to", "@debian", "@mac-x")
        self.assertEqual(code, 0)
        self.assertEqual(err, "note: @mac-x has no folder in %s yet: posted anyway (it may not "
                              "have synced)\n" % self.tree)
        self.assertEqual(entries.parse_file(self.file)[0].to, ("@debian", "@mac-x"))

    def test_a_bare_members_name(self):
        # debian, as @debian; --re's @ taken off
        code, _out, err = self.post("--to", "debian", "--re", "@debian#3")
        self.assertEqual((code, err), (0, ""))
        entry = entries.parse_file(self.file)[0]
        self.assertEqual((entry.to, entry.re), (("@debian",), "debian#3"))

    def test_a_bare_name_that_isnt_a_member(self):
        code, out, err = self.post("--to", "@debian", "mac-x")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines()[:2], [
            "ERROR channel: --to mac-x: not a member of mb (members: debian, windows)",
            "  fix: address one of the members above, by name or as @<name>; @all is the "
            "leader's"])
        self.assertFalse(os.path.exists(self.file))
        # all without its @ is no one's name here either
        code, out, err = self.post("--to", "all")
        self.assertEqual(code, 1)
        self.assertIn("--to all: not a member of mb", err)

    def test_two_posts_at_once(self):
        # real processes, each posting in a loop: one number each, no entry lost
        script = ("import sys\n"
                  "from vcharon.mailbox import post as p\n"
                  "for i in range(8):\n"
                  "    p.post(sys.argv[1], 'windows', ['@debian'], sys.argv[2] + str(i), "
                  "body='b')\n")
        files = [self.file, os.path.join(self.folder, "NOTES.md")]
        children = [subprocess.Popen([sys.executable, "-c", script, path, "p%d-" % k],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for k, path in enumerate(files)]
        for child in children:
            _out, err = child.communicate(timeout=120)
            self.assertEqual(child.returncode, 0, err)
        ids = self.numbers()
        self.assertEqual(sorted(ids, key=lambda i: int(i.split("#")[1])),
                         ["windows#%d" % n for n in range(1, 18)])
        titles = sorted(e.title for f in files for e in entries.parse_file(f))
        self.assertEqual(titles, sorted("p%d-%d" % (k, i) for k in range(2) for i in range(8)))
        self.assertEqual([f for f in os.listdir(self.folder) if f.startswith(".vcharon-stage-")],
                         [])


class RealRunTest(PostCase):
    """vcharon post as its own process, as an agent runs it."""

    def run_post(self, argv, stdin=b"", env=None):
        return subprocess.run([sys.executable, "-P", "-m", "vcharon"] + POST + argv,
                              input=stdin, capture_output=True, timeout=60,
                              env=dict(os.environ, **(env or {})), check=False)

    def test_the_real_time_and_utf8_whatever_the_console(self):
        # a console whose code page can't hold ñ (PYTHONIOENCODING stands in for Windows' 936)
        before = time.time()
        ran = self.run_post(["--title", "mañana"] + TO, stdin="señal 完成\n".encode(),
                            env={"PYTHONIOENCODING": "gbk"})
        after = time.time()
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertTrue(ran.stdout.decode("utf-8").startswith("posted windows#2 — mañana into "),
                        ran.stdout)
        self.assertTrue(self.content().endswith("— mañana\nto: @debian\n\nseñal 完成\n"),
                        self.content())
        heading = self.content().splitlines()[2]
        # to the second, between the two readings of the clock
        when = time.mktime(time.strptime(heading[3:22], "%Y-%m-%d %H:%M:%S"))
        self.assertTrue(int(before) <= when <= after, (before, heading, after))
        self.assertEqual(heading[22:], " — windows#2 — mañana")


if __name__ == "__main__":
    unittest.main()
