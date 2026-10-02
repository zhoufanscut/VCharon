"""tools/mailbox_post.py: a dated entry appended atomically (the M8 plan), in channel mode with
IDs, a header and a lock (DESIGN §14 M10)."""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from vcharon import entries, platform, plugin

from tests import util
from tests.util import write_tree

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
TOOL = os.path.join(TOOLS, "mailbox_post.py")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


post = _load("mailbox_post")
watch = _load("mailbox_watch")

# 2026-10-01 09:05:46 on this machine's clock, whatever its zone
T0 = time.mktime((2026, 10, 1, 9, 5, 46, 0, 0, -1))


def member_md(name, leader, channel="mb"):
    """MEMBER.md as ferry channel writes it."""
    return ("# MEMBER\n\n## 2026-10-01 09:00 — %s#1 — member\nto: @%s\nchannel: %s\nname: %s\n"
            "leader: %s\n" % (name, leader, channel, name, leader)).encode("utf-8")


class PostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ferry-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # the post lock goes to ferry's state dir: one per test
        self.ferry_home = os.path.join(self.tmp, "ferry-home")
        patch = mock.patch.dict(os.environ, {"FERRY_HOME": self.ferry_home})
        patch.start()
        self.addCleanup(patch.stop)
        # a channel's tree: this member windows, and its leader debian
        self.tree = os.path.join(self.tmp, "mb")
        self.folder = os.path.join(self.tree, "windows")
        write_tree(self.tree, {"windows/MEMBER.md": member_md("windows", "debian"),
                               "debian/MEMBER.md": member_md("debian", "debian")})
        self.file = os.path.join(self.folder, "RESULTS.md")

    def main(self, argv, stdin=None, t=T0, me="windows"):
        # --me: the tree here is no remote member's local tree, so a post needs it (M11a)
        if me is not None:
            argv = list(argv) + ["--me", me]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            try:
                code = post.main(argv, stdin=None if stdin is None else io.BytesIO(stdin),
                                 clock=lambda: t)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def content(self):
        with open(self.file, "rb") as f:
            return f.read().decode("utf-8")


TO = ["--to", "@debian"]


class PostTest(PostCase):
    def test_creates_then_appends(self):
        code, out, err = self.main([self.file, "--title", "step 7 done", "--body", "one line"]
                                   + TO)
        self.assertEqual((code, out, err),
                         (0, "posted windows#2 — step 7 done to %s at 2026-10-01 09:05\n"
                          % self.file, ""))
        first = ("# RESULTS\n\n## 2026-10-01 09:05 — windows#2 — step 7 done\nto: @debian\n\n"
                 "one line\n")
        self.assertEqual(self.content(), first)
        # stdin, with the trailing newline a heredoc leaves; --re and two names
        code, out, err = self.main([self.file, "--title", "to debian", "--re", "debian#3",
                                    "--to", "@debian @mac"], stdin=b"a\n\nb\n", t=T0 + 125)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.content(), first + "\n## 2026-10-01 09:07 — windows#3 — to debian\n"
                                                 "to: @debian @mac\nre: debian#3\n\na\n\nb\n")

    def test_a_file_without_a_last_newline(self):
        with open(self.file, "wb") as f:
            f.write(b"# RESULTS\n\nhand-made")
        self.main([self.file, "--title", "t", "--body", "b"] + TO)
        self.assertEqual(self.content(), "# RESULTS\n\nhand-made\n\n## 2026-10-01 09:05 — "
                                         "windows#2 — t\nto: @debian\n\nb\n")

    def test_the_body_is_never_interpreted(self):
        body = "run `rm -rf ~` and $(whoami) and ${HOME} \\n 'q' \"qq\"\n"
        code, out, err = self.main([self.file, "--title", "lit"] + TO,
                                   stdin=body.encode("utf-8"))
        self.assertEqual(code, 0, err)
        self.assertTrue(self.content().endswith("\n\n" + body), self.content())

    def test_refusals(self):
        for argv, stdin in (([self.file, "--title", "t", "--body", ""] + TO, None),
                            ([self.file, "--title", "t", "--body", " \n"] + TO, None),
                            ([self.file, "--title", "t"] + TO, b""),
                            ([self.file, "--title", "t"] + TO, b"\xff\xfe"),
                            ([self.file, "--title", "", "--body", "b"] + TO, None),
                            ([self.file, "--title", "a\nb", "--body", "b"] + TO, None),
                            # any line break splitlines() knows (the M10 review)
                            ([self.file, "--title", "a\u2028## t — windows#9 — forged",
                              "--body", "b"] + TO, None),
                            ([self.file, "--title", "a\x85b", "--body", "b"] + TO, None),
                            ([self.file, "--title", "a\rb", "--body", "b"] + TO, None),
                            ([self.file, "--body", "b"] + TO, None),
                            ([os.path.join(self.tmp, "nope", "R.md"), "--title", "t", "--body",
                              "b"] + TO, None),
                            # M10: --to is required here, under a MEMBER.md
                            ([self.file, "--title", "t", "--body", "b"], None),
                            ([self.file, "--title", "t", "--body", "b", "--to"], None),
                            ([self.file, "--title", "t", "--body", "b", "--to", "debian"], None),
                            ([self.file, "--title", "t", "--body", "b", "--to", "@Debian"],
                             None),
                            ([self.file, "--title", "t", "--body", "b", "--to", "@a @"], None),
                            ([self.file, "--title", "t", "--body", "b", "--re", "debian"] + TO,
                             None),
                            ([self.file, "--title", "t", "--body", "b", "--re", "debian#0"]
                             + TO, None)):
            with self.subTest(argv=argv, stdin=stdin):
                code, out, err = self.main(argv, stdin=stdin)
                self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.file))
        code, out, err = self.main([self.file, "--title", "t", "--body", "b"])
        self.assertIn("--to is required: @<name> ..., or @all", err)
        code, out, err = self.main([self.file, "--title", "t", "--body", "b"] + TO)
        self.assertEqual(code, 0)
        code, out, err = self.main([self.file, "--title", "t"] + TO, stdin=b"")
        self.assertIn("the body is empty", err)

    def test_twins_are_refused(self):
        # M9: a name that is another one next to it on macOS or Windows would make the tree
        # refused there; nothing is written
        answers = os.path.join(self.folder, "answers.md")
        with open(answers, "wb") as f:
            f.write(b"# answers\n")
        before = sorted(os.listdir(self.folder))
        code, out, err = self.main([os.path.join(self.folder, "ANSWERS.md"), "--title", "t",
                                    "--body", "b"] + TO)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "mailbox_post: ANSWERS.md and answers.md are one name on macOS "
                              "and Windows: post to answers.md\n")
        self.assertEqual(sorted(os.listdir(self.folder)), before)
        with open(answers, "rb") as f:
            self.assertEqual(f.read(), b"# answers\n")
        # the exact name appends
        code, out, err = self.main([answers, "--title", "t", "--body", "b"] + TO)
        self.assertEqual(code, 0, err)
        # composed and decomposed: one name on macOS only
        write_tree(self.folder, {"é.md": b"x"})
        code, out, err = self.main([os.path.join(self.folder, "é.md"), "--title", "t",
                                    "--body", "b"] + TO)
        self.assertEqual(code, 1)
        self.assertEqual(err, "mailbox_post: é.md and é.md are one name on macOS: "
                              "post to é.md\n")
        # the folder's name: Mac/ next to mac/, where mac/ is the member's folder; the twin is
        # told before "not in a member's folder"
        write_tree(self.tree, {"mac/MEMBER.md": member_md("mac", "debian"), "Mac/": None})
        target = os.path.join(self.tree, "Mac", "x.md")
        code, out, err = self.main([target, "--title", "t", "--body", "b"] + TO)
        self.assertEqual(code, 1)
        self.assertEqual(err, "mailbox_post: Mac/ and mac/ are one folder on macOS and Windows: "
                              "post into mac/\n")
        # nothing written: on a disk that folds case, Mac/ is mac/ and holds its MEMBER.md
        self.assertEqual(os.listdir(os.path.join(self.tree, "Mac")),
                         ["MEMBER.md"] if util.folds_case() else [])
        # both twins there already: never point at one of them
        if util.folds_case():
            self.skipTest(util.FOLDS_CASE)
        notes = os.path.join(self.folder, "notes.md")
        write_tree(self.folder, {"Notes.md": b"N", "notes.md": b"n"})
        for target in ("notes.md", "Notes.md", "NOTES.md"):
            with self.subTest(target=target):
                code, out, err = self.main([os.path.join(self.folder, target), "--title", "t",
                                            "--body", "b"] + TO)
                self.assertEqual(code, 1)
                self.assertTrue(err.endswith("are one name on macOS and Windows: remove or "
                                             "rename one first\n"), err)
        self.assertEqual(err, "mailbox_post: NOTES.md, Notes.md and notes.md are one name on "
                              "macOS and Windows: remove or rename one first\n")
        with open(notes, "rb") as f:
            self.assertEqual(f.read(), b"n")
        # a member's folder next to a stray that isn't a writer's name: a note, and the post
        # goes on (clients leave the stray out); never "post into Debian/"
        write_tree(self.tree, {"Debian/": None})
        code, out, err = self.main([os.path.join(self.tree, "debian", "STEPS.md"), "--title",
                                    "t", "--body", "b"] + TO, me="debian")
        self.assertEqual(code, 0, err)
        note = ("mailbox_post: note: %s are one folder on macOS and Windows: remove or rename "
                "%s (at the top of the tree clients leave it out; below it, macOS and Windows "
                "clients get nothing until one is removed)\n")
        self.assertEqual(err, note % ("Debian/ and debian/", "Debian/"))
        self.assertTrue(os.path.exists(os.path.join(self.tree, "debian", "STEPS.md")))
        # the same below the top, where the twin blocks clients: the note says so too, and
        # the post (which makes no new twin) goes on
        write_tree(self.folder, {"notes/": None, "Notes/": None})
        code, out, err = self.main([os.path.join(self.folder, "notes", "x.md"), "--title", "t",
                                    "--body", "b"] + TO)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, note % ("Notes/ and notes/", "Notes/"))
        # a folder with a valid twin and another stray: no single folder to point at
        write_tree(self.tree, {"MAC/": None})
        code, out, err = self.main([os.path.join(self.tree, "Mac", "x.md"), "--title", "t",
                                    "--body", "b"] + TO)
        self.assertEqual((code, err), (1, "mailbox_post: MAC/, Mac/ and mac/ are one folder on "
                                          "macOS and Windows: remove or rename one first\n"))
        # two folders, neither a writer's name: remove or rename one
        write_tree(self.folder, {"Sub/": None, "SUB/": None})
        code, out, err = self.main([os.path.join(self.folder, "Sub", "x.md"), "--title", "t",
                                    "--body", "b"] + TO)
        self.assertEqual((code, err), (1, "mailbox_post: SUB/ and Sub/ are one folder on macOS "
                                          "and Windows: remove or rename one first\n"))
        # a folder with no twin is fine
        code, out, err = self.main([os.path.join(self.folder, "NEW.md"), "--title", "t",
                                    "--body", "b"] + TO)
        self.assertEqual(code, 0, err)

    def test_atomic_and_skipped_while_staged(self):
        self.main([self.file, "--title", "first", "--body", "b"] + TO)
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
            code, out, err = self.main([self.file, "--title", "second", "--body", "c"] + TO)
        self.assertEqual(code, 0, err)
        name, during, scanned, planned = seen
        self.assertTrue(name.startswith(".ferry-stage-"), name)
        self.assertEqual(during, before)
        self.assertEqual(scanned, ["windows/MEMBER.md", "windows/RESULTS.md"])
        self.assertEqual(planned, ["debian", "debian/MEMBER.md", "windows", "windows/MEMBER.md",
                                   "windows/RESULTS.md"])
        self.assertEqual(sorted(os.listdir(self.folder)), ["MEMBER.md", "RESULTS.md"])

    def test_headings_in_the_body_are_quoted(self):
        body = ("# fake\n## 2026-10-01 09:00 — windows#9 — to windows\n   ### three spaces\n#\n"
                "#nope\n####### seven\n    # code\nx ## y\n")
        self.main([self.file, "--title", "t"] + TO, stdin=body.encode("utf-8"))
        self.assertTrue(self.content().endswith(
            "— t\nto: @debian\n\n> # fake\n> ## 2026-10-01 09:00 — windows#9 — to windows\n"
            ">    ### three spaces\n> #\n#nope\n####### seven\n    # code\nx ## y\n"),
            self.content())
        # the only heading after the file's own is the entry's
        headings = [l for l in self.content().splitlines() if l.startswith("#")]
        self.assertEqual(headings[:2], ["# RESULTS", "## 2026-10-01 09:05 — windows#2 — t"])
        self.assertEqual(headings[2:], ["#nope", "####### seven"])
        # and the #9 in the body isn't a number taken
        code, out, err = self.main([self.file, "--title", "u", "--body", "b"] + TO)
        self.assertTrue(out.startswith("posted windows#3 — u "), out)

    def test_the_swap_is_tried_again(self):
        # Windows: the swap fails while a ferry run has the file open
        self.main([self.file, "--title", "first", "--body", "b"] + TO)
        real = os.replace
        calls = []

        def replace(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(13, "denied")
            return real(src, dst)

        with mock.patch.object(entries, "REPLACE_PAUSE", 0), \
                mock.patch.object(entries.os, "replace", replace):
            code, out, err = self.main([self.file, "--title", "second", "--body", "c"] + TO)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 3)
        self.assertTrue(self.content().endswith("— second\nto: @debian\n\nc\n"))
        self.assertEqual(sorted(os.listdir(self.folder)), ["MEMBER.md", "RESULTS.md"])

    def test_a_failed_write_leaves_the_file(self):
        self.main([self.file, "--title", "first", "--body", "b"] + TO)
        before = self.content()
        denied = mock.Mock(side_effect=PermissionError(13, "denied"))
        with mock.patch.object(entries, "REPLACE_PAUSE", 0), \
                mock.patch.object(entries.os, "replace", denied):
            code, out, err = self.main([self.file, "--title", "second", "--body", "c"] + TO)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(denied.call_count, entries.REPLACE_TRIES)
        self.assertEqual(err, "mailbox_post: can't write %s: denied\n" % self.file)
        self.assertEqual(self.content(), before)
        self.assertEqual(sorted(os.listdir(self.folder)), ["MEMBER.md", "RESULTS.md"])
        # the number wasn't taken
        code, out, err = self.main([self.file, "--title", "third", "--body", "d"] + TO)
        self.assertTrue(out.startswith("posted windows#3 — third "), out)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_keeps_the_mode(self):
        write_tree(self.folder, {"RESULTS.md": b"# RESULTS\n"})
        os.chmod(self.file, 0o640)
        self.main([self.file, "--title", "t", "--body", "b"] + TO)
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o640)


class ChannelTest(PostCase):
    """Posting in a channel (DESIGN §14 M10's "Done when": posting)."""

    def post(self, path, *argv, body="b", me="windows"):
        return self.main([path, "--title", "t", "--body", body] + list(argv or TO), me=me)

    def numbers(self):
        """The IDs in every entry heading of the own folder, in file order."""
        found = []
        for path in entries.md_files(self.folder):
            found += [e.id for e in entries.parse_file(path)]
        return found

    def test_ids_counted_across_files(self):
        self.assertEqual(self.post(self.file)[:2], (0, "posted windows#2 — t to %s at "
                                                       "2026-10-01 09:05\n" % self.file))
        self.assertEqual(self.post(os.path.join(self.folder, "NOTES.md"))[0], 0)
        os.makedirs(os.path.join(self.folder, "logs"))
        self.assertEqual(self.post(os.path.join(self.folder, "logs", "run.md"))[0], 0)
        self.assertEqual(self.post(self.file)[0], 0)
        self.assertEqual(sorted(self.numbers(), key=lambda i: int(i.split("#")[1])),
                         ["windows#%d" % n for n in range(1, 6)])
        # another member's entries in this folder don't move the count
        with open(self.file, "ab") as f:
            f.write("\n## 2026-10-01 09:00 — debian#40 — copied\nto: @windows\n".encode())
        self.assertTrue(self.post(self.file)[1].startswith("posted windows#6 "))

    def test_a_subfolder_posts_as_the_own_folder(self):
        os.makedirs(os.path.join(self.folder, "patches", "deep"))
        path = os.path.join(self.folder, "patches", "deep", "p.md")
        code, out, err = self.post(path)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "posted windows#2 — t to %s at 2026-10-01 09:05\n" % path)

    def test_a_quoted_tilde_is_expanded(self):
        # the docs write FILE as "~/…", quoted, so the shell leaves the ~ to the tool
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "USERPROFILE": self.tmp}):
            code, out, err = self.post("~/mb/windows/RESULTS.md")
            # expanduser keeps the typed "/" separators; os.path.join would use the disk's
            want = os.path.expanduser("~/mb/windows/RESULTS.md")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "posted windows#2 — t to %s at 2026-10-01 09:05\n" % want)
        self.assertEqual([e.id for e in entries.parse_file(self.file)], ["windows#2"])

    def test_outside_a_members_folder(self):
        # no MEMBER.md up the tree: refused, --to or not (the old form is gone)
        os.makedirs(os.path.join(self.tree, "mac"))
        path = os.path.join(self.tree, "mac", "RESULTS.md")
        for argv in ([], TO):
            with self.subTest(argv=argv):
                code, out, err = self.main([path, "--title", "t", "--body", "b"] + argv)
                self.assertEqual((code, out), (1, ""))
                # the command as this box runs ferry (M14a)
                self.assertEqual(err, platform.runnable(
                    "mailbox_post: %s: not in a channel member's folder (no MEMBER.md in its "
                    "folder or above; ferry channel join writes it)\n" % path))
                self.assertNotIn(" ferry channel join", err)
        self.assertEqual(os.listdir(os.path.join(self.tree, "mac")), [])
        # a MEMBER.md that is a folder isn't one
        write_tree(self.tree, {"mac/MEMBER.md/": None})
        self.assertEqual(self.post(path)[0], 1)

    def test_member_and_channel_md_are_refused(self):
        before = util.read_tree(self.tree)
        for name in ("MEMBER.md", "CHANNEL.md", "member.md", "Channel.md"):
            with self.subTest(name=name):
                code, out, err = self.post(os.path.join(self.folder, name))
                # "ferry channel's" isn't a command: as written (M14a)
                self.assertEqual((code, out, err), (1, "", "mailbox_post: %s is ferry channel's "
                                                    "to write: post into another file "
                                                    "(RESULTS.md, say)\n" % name))
        # nor a file that isn't .md: the numbers and the watcher read only those
        code, out, err = self.post(os.path.join(self.folder, "RESULTS.txt"))
        self.assertEqual((code, err), (1, "mailbox_post: RESULTS.txt: entries go in .md files "
                                          "(the watcher and the numbering read only those)\n"))
        self.assertEqual(util.read_tree(self.tree), before)

    def test_all_is_the_leaders(self):
        code, out, err = self.post(self.file, "--to", "@all")
        self.assertEqual((code, out, err), (1, "", "mailbox_post: @all is the leader's "
                                                   "(debian): address members by name\n"))
        self.assertFalse(os.path.exists(self.file))
        steps = os.path.join(self.tree, "debian", "STEPS.md")
        code, out, err = self.post(steps, "--to", "@all", me="debian")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(entries.parse_file(steps)[0].to, ("@all",))
        # a MEMBER.md without a leader: nobody's @all
        write_tree(self.folder, {"MEMBER.md": b"# MEMBER\n\n## x \xe2\x80\x94 windows#1 "
                                              b"\xe2\x80\x94 member\nto: @debian\n"})
        code, out, err = self.post(self.file, "--to", "@all")
        self.assertEqual(err, "mailbox_post: @all is the leader's (MEMBER.md names none): "
                              "address members by name\n")

    def test_me_in_a_channel_root(self):
        # M11a: a channel root lets any member write any folder, so --me says whose a post is
        before = util.read_tree(self.tree)
        code, out, err = self.post(self.file, me=None)
        self.assertEqual((code, out, err), (1, "", "mailbox_post: %s: in a channel root, pass "
                                                   "--me <your name>: every member's folder "
                                                   "is writable there\n" % self.file))
        steps = os.path.join(self.tree, "debian", "STEPS.md")
        code, out, err = self.post(steps, "--to", "@all", me="windows")
        self.assertEqual((code, out, err), (1, "", "mailbox_post: %s is debian's folder, not "
                                                   "windows's: post only in your own\n" % steps))
        # a subfolder belongs to the own folder too
        os.makedirs(os.path.join(self.tree, "debian", "sub"))
        deep = os.path.join(self.tree, "debian", "sub", "p.md")
        self.assertEqual(self.post(deep, me="windows")[0], 1)
        self.assertEqual(util.read_tree(self.tree), dict(before, **{"debian/sub/": None}))
        code, out, err = self.post(self.file, me="windows")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "posted windows#2 — t to %s at 2026-10-01 09:05\n" % self.file)

    def test_an_unknown_name_is_a_note(self):
        code, out, err = self.post(self.file, "--to", "@debian", "@mac-x")
        self.assertEqual(code, 0)
        self.assertEqual(err, "mailbox_post: note: @mac-x has no folder in %s yet: posted "
                              "anyway (it may not have synced)\n" % self.tree)
        self.assertEqual(entries.parse_file(self.file)[0].to, ("@debian", "@mac-x"))

    def test_a_body_cant_forge_a_header(self):
        body = ("to: @all\nre: debian#1\n## 2026-10-01 09:00 — windows#7 — forged\nto: @all\n\n"
                "windows#9 and debian#3\n")
        code, out, err = self.post(self.file, body=body)
        self.assertEqual(code, 0, err)
        found = entries.parse_file(self.file)
        self.assertEqual([(e.id, e.to, e.re, e.header) for e in found],
                         [("windows#2", ("@debian",), None, [])])
        self.assertTrue(found[0].body.startswith("to: @all\nre: debian#1\n> ## "), found[0].body)
        # no number in a body is taken
        self.assertTrue(self.post(self.file)[1].startswith("posted windows#3 "))

    def test_two_posts_at_once(self):
        # real processes, each posting in a loop: one number each, no entry lost
        script = ("import sys\n"
                  "sys.argv[0] = %r\n"
                  "import importlib.util\n"
                  "spec = importlib.util.spec_from_file_location('p', %r)\n"
                  "p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)\n"
                  "for i in range(8):\n"
                  "    assert p.main([sys.argv[1], '--me', 'windows', '--to', '@debian', "
                  "'--title', sys.argv[2] + str(i), '--body', 'b']) == 0\n" % (TOOL, TOOL))
        files = [self.file, os.path.join(self.folder, "NOTES.md")]
        children = [subprocess.Popen([sys.executable, "-c", script, path, "p%d-" % k],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for k, path in enumerate(files)]
        for child in children:
            out, err = child.communicate(timeout=120)
            self.assertEqual(child.returncode, 0, err)
        ids = self.numbers()
        self.assertEqual(sorted(ids, key=lambda i: int(i.split("#")[1])),
                         ["windows#%d" % n for n in range(1, 18)])
        titles = sorted(e.title for f in files for e in entries.parse_file(f))
        self.assertEqual(titles, sorted("p%d-%d" % (k, i) for k in range(2) for i in range(8)))


class RealRunTest(PostCase):
    def run_tool(self, argv, stdin=b"", env=None):
        return subprocess.run([sys.executable, TOOL] + argv, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                              env=dict(os.environ, **(env or {})))

    def test_the_real_time(self):
        before = time.time()
        ran = self.run_tool([self.file, "--title", "now", "--me", "windows"] + TO,
                            stdin=b"body\n")
        after = time.time()
        self.assertEqual(ran.returncode, 0, ran.stderr)
        stamps = {time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) for t in (before, after)}
        heading = self.content().splitlines()[2]
        self.assertIn(heading, {"## %s — windows#2 — now" % s for s in stamps})

    def test_utf8_whatever_the_console(self):
        # a console whose code page can't hold ñ (PYTHONIOENCODING stands in for Windows' 936)
        ran = self.run_tool([self.file, "--title", "mañana", "--me", "windows"] + TO,
                            stdin="señal 完成\n".encode("utf-8"), env={"PYTHONIOENCODING": "gbk"})
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertTrue(ran.stdout.decode("utf-8").startswith("posted windows#2 — mañana to "),
                        ran.stdout)
        self.assertTrue(self.content().endswith("— mañana\nto: @debian\n\nseñal 完成\n"),
                        self.content())
