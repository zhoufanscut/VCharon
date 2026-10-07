"""vcharon list, create, join, leave and close, and the channel root: names, the claim,
records, sections, close and leave, the helper's channel calls, and channels.d/ through the
commands."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import vcharon
from vcharon import (
    channel_cmd,
    channels,
    charter,
    cli,
    config,
    entries,
    plan,
    platform,
    remote,
    skill,
    state,
)
from vcharon.mailbox import post as post_mod
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

from tests import util
from tests.test_cli import VCHARON_DIR
from tests.test_lock import hold_in_child, stop_child
from tests.util import CAN_SYMLINK, FAKE_SSH, TEST_MACHINE_ID, FakeSshCase, read_tree, write_tree

OTHER_MACHINE = "fedcba9876543210fedcba9876543210"
# leave's fix for the leader, as written (before runnable)
LEADER_LEAVE_FIX = ("a leader ends the channel with CLOSED to @all after every member's DONE, "
                    "then a close; how: vcharon guide end")
# a watcher's exit-12 line: another watcher, or a create, join, leave or close, holds its lock
WATCHER_LOCKED = ("ERROR another watcher is running on this mailbox (%s.lock), or a create, "
                  "join, leave or close of this member")


def hold(case, path):
    """A child process holding the lock file path (made first), until the test ends."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "ab").close()
    child = hold_in_child(path)
    case.addCleanup(release, child)
    return child


def release(child):
    """Ends a holder; once is enough."""
    if child.poll() is None:
        stop_child(child)


class ChannelCase(FakeSshCase):
    """One fake server (its root under the fake home) and boxes, each a VCHARON_HOME of its own
    with [vcharon] box set. The current directory is a git checkout named Web."""

    def setUp(self):
        FakeSshCase.setUp(self)
        self.root = os.path.join(self.home, "channels")
        os.environ["VCHARON_CHANNELS_ROOT"] = self.root
        self.homes = {}
        self.use_box("mac", self.vcharon_home)
        self.project = os.path.join(self.tmp, "work", "Web")
        os.makedirs(os.path.join(self.project, ".git"))
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    # A class that sets TEMPLATE builds its channel once (from_template) and gives every test
    # a copy. The files hold absolute paths into the temp folder (records, job state, logs),
    # so each test of such a class gets the same temp folder path: a copy put back there
    # reads as the build left it. The tests stay apart: each starts from a fresh copy.
    TEMPLATE = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.TEMPLATE:
            # made again each time the class runs: a later run in the same process (tests of
            # two classes named in turn) finds the last one's folder gone
            cls._template_root = tempfile.mkdtemp(prefix="vcharon-class-")
            cls._template = None
            cls.addClassCleanup(shutil.rmtree, cls._template_root, True)

    def make_tmp(self):
        if not self.TEMPLATE:
            return FakeSshCase.make_tmp(self)
        tmp = os.path.join(self._template_root, "case")
        shutil.rmtree(tmp, True)
        if os.path.exists(tmp):
            # never hand a test a folder that isn't new
            left = [os.path.join(d, n) for d, dirs, files in os.walk(tmp) for n in dirs + files]
            self.fail("the last test left files that can't be removed in %s: %s"
                      % (tmp, ", ".join(left[:5]) or "the folder itself"))
        os.mkdir(tmp)
        return tmp

    def from_template(self, build):
        """build() in this class's first test, then keep a copy of the temp folder, the
        environment and the boxes it left; every later test gets that copy in place of its
        fresh setUp's, as if it had run build() itself."""
        assert self.TEMPLATE, "from_template needs TEMPLATE = True on the class"
        cls = type(self)
        saved = os.path.join(cls._template_root, "template")
        if cls._template is None:
            build()
            shutil.copytree(self.tmp, saved, symlinks=True)
            cls._template = (dict(os.environ), dict(self.homes))
            return
        env, homes = cls._template
        # out of the folder first: Windows can't remove the current directory
        os.chdir(cls._template_root)
        shutil.rmtree(self.tmp)
        shutil.copytree(saved, self.tmp, symlinks=True)
        os.environ.clear()
        os.environ.update(env)
        self.homes = dict(homes)
        os.chdir(self.project)

    def use_box(self, box, home=None):
        """Switches to the box named box (a VCHARON_HOME of its own, made on first use)."""
        if box not in self.homes:
            home = home or os.path.join(self.tmp, "box-" + box)
            os.makedirs(home, exist_ok=True)
            with open(os.path.join(home, "vcharon.ini"), "w", encoding="utf-8") as f:
                f.write("[vcharon]\nbox = %s\n" % box)
            self.homes[box] = home
        os.environ["VCHARON_HOME"] = self.homes[box]
        return self.homes[box]

    def channel(self, *argv):
        return self.run_cli(*argv)

    def ok(self, *argv):
        code, out, err = self.channel(*argv)
        self.assertEqual(code, 0, out + err)
        return out

    def refused(self, *argv, code=1):
        """The ERROR line of a refused command."""
        return self.refusal(*argv, code=code)[0]

    def refusal(self, *argv, code=1):
        """The ERROR line and the fix line (as written, before runnable) of a refused
        command."""
        got, out, err = self.channel(*argv)
        self.assertEqual(got, code, out + err)
        lines = err.splitlines()
        fixes = [l[len("  fix: "):] for l in lines if l.startswith("  fix: ")]
        return lines[0], fixes[0] if fixes else None

    def server_tree(self):
        return read_tree(self.root) if os.path.isdir(self.root) else {}

    def joined(self, section, box=None):
        """A remote member's local tree on the current box."""
        return os.path.join(os.environ["VCHARON_HOME"], "joined", section)

    def record(self, section):
        with open(os.path.join(os.environ["VCHARON_HOME"], "state", "channels",
                               section + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def lead(self, channel="game", where=("--server", "fake-dest"), box="laptop", project="ui"):
        """box creates channel and leads it; back to the mac box after."""
        self.use_box(box)
        self.ok("create", channel, *where, "--project", project)
        self.use_box("mac")
        return "%s-%s" % (box, project)


class NamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cfg(self, box="mac"):
        return config.Config("/x/vcharon.ini", True, config.Settings(), box=box)

    def test_project_from_git(self):
        repo = os.path.join(self.tmp, "My Repo.v2")
        deep = os.path.join(repo, "src", "a")
        os.makedirs(deep)
        os.mkdir(os.path.join(repo, ".git"))
        self.assertEqual(channel_cmd.project_of(deep), "My Repo.v2")
        self.assertEqual(channel_cmd.member_name(self.cfg(), cwd=deep), "mac-my-repo-v2")
        # a worktree's .git is a file
        tree = os.path.join(self.tmp, "wt")
        os.makedirs(os.path.join(tree, "x"))
        with open(os.path.join(tree, ".git"), "w") as f:
            f.write("gitdir: elsewhere\n")
        self.assertEqual(channel_cmd.project_of(os.path.join(tree, "x")), "wt")
        # no .git: the current directory's own name
        plain = os.path.join(self.tmp, "plain", "Here")
        os.makedirs(plain)
        self.assertEqual(channel_cmd.project_of(plain), "Here")

    def test_project_from_svn_or_hg(self):
        # an SVN (1.7 and later) or Mercurial checkout's root, from any folder below it: a
        # member that changes folders keeps its name
        for mark in (".svn", ".hg"):
            with self.subTest(mark=mark):
                root = os.path.join(self.tmp, mark[1:], "Core")
                deep = os.path.join(root, "RPC", "x")
                os.makedirs(deep)
                os.mkdir(os.path.join(root, mark))
                self.assertEqual(channel_cmd.project_of(deep), "Core")
                self.assertEqual(channel_cmd.project_of(root), "Core")
        # only a directory counts: a file named .svn or .hg is no checkout
        plain = os.path.join(self.tmp, "files", "Top")
        here = os.path.join(plain, "Here")
        os.makedirs(here)
        for mark in (".svn", ".hg"):
            with open(os.path.join(plain, mark), "w") as f:
                f.write("x\n")
        self.assertEqual(channel_cmd.project_of(here), "Here")
        # the nearest marker wins: a git repo inside an SVN checkout is its own project
        outer = os.path.join(self.tmp, "nested", "outer")
        inner = os.path.join(outer, "inner")
        os.makedirs(os.path.join(inner, "src"))
        os.mkdir(os.path.join(outer, ".svn"))
        os.mkdir(os.path.join(inner, ".git"))
        self.assertEqual(channel_cmd.project_of(os.path.join(inner, "src")), "inner")

    def test_project_source(self):
        # the folder and the mark that made it the project, for join's and create's note
        for mark in (".git", ".svn", ".hg"):
            with self.subTest(mark=mark):
                root = os.path.join(self.tmp, "src" + mark)
                deep = os.path.join(root, "a", "b")
                os.makedirs(deep)
                os.mkdir(os.path.join(root, mark))
                self.assertEqual(channel_cmd.project_source(deep), (root, mark))
                self.assertEqual(channel_cmd.project_source(root), (root, mark))
        # a worktree's .git file
        tree = os.path.join(self.tmp, "wt")
        os.makedirs(os.path.join(tree, "x"))
        with open(os.path.join(tree, ".git"), "w", encoding="utf-8", newline="") as f:
            f.write("gitdir: elsewhere\n")
        self.assertEqual(channel_cmd.project_source(os.path.join(tree, "x")), (tree, ".git"))
        # none: the folder itself, and no mark
        plain = os.path.join(self.tmp, "plain", "Here")
        os.makedirs(plain)
        self.assertEqual(channel_cmd.project_source(plain), (plain, None))

    def test_project_note(self):
        root = os.path.join(self.tmp, "src")
        deep = os.path.join(root, "a")
        os.makedirs(deep)
        os.mkdir(os.path.join(root, ".svn"))
        plain = os.path.join(self.tmp, "Here")
        os.mkdir(plain)
        # the undo, by verb, with the flags the record will hold; spelled as this box runs
        # vcharon
        self.assertEqual(channel_cmd.project_note("join", "game", "src", None, deep),
                         "  note: project src is the checkout %s (.svn); " % root
                         + platform.runnable("if that is the wrong project: vcharon leave game "
                                             "--project src (then join again with --project "
                                             "P)"))
        self.assertEqual(channel_cmd.project_note("create", "game", "here", "b", plain),
                         "  note: project here is this folder's name (no .git, .svn or .hg "
                         "here or above); " + platform.runnable(
                             "if that is the wrong project: vcharon close game --project here "
                             "--role b (then create it again with --project P)"))

        # without the undo (a rejoin): where the name came from only
        self.assertEqual(channel_cmd.project_note("join", "game", "src", None, deep,
                                                  undo=False),
                         "  note: project src is the checkout %s (.svn)" % root)

    @unittest.skipIf(os.name == "nt", "Windows names can't hold a control character")
    def test_the_note_escapes_the_path(self):
        root = os.path.join(self.tmp, "a\x1bb")
        os.makedirs(os.path.join(root, ".git"))
        note = channel_cmd.project_note("join", "game", "ab", None, root)
        self.assertTrue(note.startswith("  note: project ab is the checkout %s (.git); "
                                        % root.replace("\x1b", "\\x1b")), note)
        self.assertNotIn("\x1b", note)

    def test_runnable_never_sees_the_path(self):
        # a folder named like a command stays as it is; only the undo is respelled
        root = os.path.join(self.tmp, "x vcharon leave y")
        os.makedirs(os.path.join(root, ".git"))
        with mock.patch.object(platform, "self_command", return_value="/opt/vc"):
            note = channel_cmd.project_note("join", "game", "y", None, root)
        self.assertEqual(note, "  note: project y is the checkout %s (.git); if that is the "
                         "wrong project: /opt/vc leave game --project y (then join again with "
                         "--project P)" % root)

    def test_cleaning_and_cutting(self):
        for text, want in (("Web", "web"), ("a  b--c", "a-b-c"), ("-x-", "x"),
                           ("游戏_ui", "_ui"), ("abcdefghijklmnopq", "abcdefghijklmn"),
                           ("abcdefghijklm-op", "abcdefghijklm"), ("...", ""), ("Ä", "")):
            with self.subTest(text=text):
                self.assertEqual(channel_cmd.clean_project(text), want)

    def test_member_names(self):
        cfg = self.cfg()
        self.assertEqual(channel_cmd.member_name(cfg, project="UI"), "mac-ui")
        self.assertEqual(channel_cmd.member_name(cfg, project="UI", role="b2"), "mac-ui-b2")
        # the longest: 10 + 1 + 14 + 1 + 6
        name = channel_cmd.member_name(self.cfg("x" * 10), project="p" * 20, role="r" * 6)
        self.assertEqual(len(name), 32)
        for role in ("", "abcdefg", "A", "a-b"):
            with self.subTest(role=role):
                with self.assertRaises(VCharonError) as cm:
                    channel_cmd.member_name(cfg, project="ui", role=role)
                self.assertEqual(cm.exception.code, "config")
        with self.assertRaises(VCharonError) as cm:
            channel_cmd.member_name(cfg, project="...")
        self.assertEqual(cm.exception.message, "the project's name comes out empty: give "
                                               "--project")
        # over 32 (a box that config would refuse)
        with self.assertRaises(VCharonError) as cm:
            channel_cmd.member_name(self.cfg("x" * 11), project="p" * 14, role="r" * 6)
        self.assertIn("at most 32 characters", cm.exception.message)

    def test_the_box_defaults_to_the_os(self):
        for osn, word in (("linux", "linux"), ("darwin", "mac"), ("windows", "win")):
            with (self.subTest(osn=osn),
                  mock.patch.object(platform, "os_name", return_value=osn)):
                self.assertEqual(platform.os_word(), word)
                cfg = self.cfg(None)
                self.assertEqual((cfg.box_name, cfg.box_source), (word, "os"))
                self.assertEqual(cfg.box_text(), "%s (default, from the OS)" % word)
                self.assertEqual(channel_cmd.member_name(cfg, project="UI"), word + "-ui")
        cfg = self.cfg("laptop")
        self.assertEqual((cfg.box_name, cfg.box_source), ("laptop", "config"))
        self.assertEqual(cfg.box_text(), "laptop (set in /x/vcharon.ini)")


class CreateJoinTest(ChannelCase):
    """create and join, remote members over the fake ssh and server members (--local)."""

    def test_create_remote(self):
        os.environ.pop("VCHARON_CHANNELS_ROOT")
        self.root = os.path.join(self.home, ".local", "state", "vcharon", "channels")
        self.use_box("laptop")
        out = self.ok("create", "game", "--server", "fake-dest", "--project", "ui")
        own = os.path.join(self.joined("game.laptop-ui"), "laptop-ui")
        # the next steps, with the flags that find the membership from any folder, then the OK
        # line, still the last; an agent other than Claude Code is told to check its tool
        self.assertEqual(out.splitlines()[-4:], [
            platform.runnable("  next: start your watcher now (vcharon guide watch): "
                              "vcharon watch game --until-change --project ui"),
            platform.runnable(channel_cmd.FIRST_CHECK),
            platform.runnable("  then post the plan (vcharon guide post): vcharon post game "
                              "--steps --to @all --title '…' --project ui, with the body on "
                              "stdin"),
            "OK  created game; your folder is %s" % own])
        # the record, the section, the local tree; the run pushed both files
        self.assertEqual(self.record("game.laptop-ui"), {
            "version": 1, "channel": "game", "name": "laptop-ui", "leader": "laptop-ui",
            "ssh": "fake-dest", "remote": "~/.local/state/vcharon/channels/game",
            "machine": TEST_MACHINE_ID, "project": "ui", "role": None, "format": 1,
            "limits": {"max_mb": 50, "max_files": 1000, "max_entry_kb": 1000}})
        with open(os.path.join(self.homes["laptop"], "channels.d", "game.laptop-ui.ini"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), textwrap.dedent("""\
                [game.laptop-ui]
                ssh            = fake-dest
                mailbox.me     = laptop-ui
                mailbox.leader = laptop-ui
                mailbox.local  = %s
                mailbox.remote = ~/.local/state/vcharon/channels/game
                mailbox.max_mb = 50
                mailbox.max_files = 1000
                """) % self.joined("game.laptop-ui"))
        self.assertEqual(sorted(os.listdir(own)), ["CHANNEL.md", "MEMBER.md"])
        self.assertEqual(sorted(read_tree(self.root)), [
            "game/", "game/laptop-ui/", "game/laptop-ui/CHANNEL.md", "game/laptop-ui/MEMBER.md"])
        member = entries.parse_file(os.path.join(own, "MEMBER.md"))
        claimer = platform.claimer("game")
        self.assertRegex(claimer, r"\A[0-9a-f]{16}\Z")
        self.assertEqual([(e.id, e.title, e.to, e.header) for e in member], [
            ("laptop-ui#1", "member", ("@laptop-ui",),
             [("channel", "game"), ("name", "laptop-ui"), ("leader", "laptop-ui"),
              ("box", "laptop"), ("os", platform.os_word()), ("agent", "other"),
              ("project", "ui"), ("claimer", claimer), ("vcharon", vcharon.VERSION)])])
        ch = entries.parse_file(os.path.join(own, "CHANNEL.md"))
        self.assertEqual([(e.id, e.title, e.to) for e in ch],
                         [("laptop-ui#2", "channel game created", ("@all",))])
        self.assertEqual([k for k, v in ch[0].header],
                         ["leader", "created", "rules", "format", "created by", "max mb",
                          "max files", "max entry kb"])
        self.assertEqual(ch[0].header[3:], [
            ("format", "1"), ("created by", "vcharon " + vcharon.VERSION), ("max mb", "50"),
            ("max files", "1000"), ("max entry kb", "1000")])
        self.assertEqual(dict(ch[0].header)["rules"], channel_cmd.RULES)
        # the rules ship with vcharon itself: a command, not a path in a checkout
        self.assertEqual(channel_cmd.RULES, "vcharon guide rules")

    def test_create_local_and_join(self):
        # a server member leads; a Mac member joins over ssh
        leader = self.lead(where=("--local",))
        self.assertEqual(leader, "laptop-ui")
        self.use_box("laptop")
        self.assertEqual(self.record("game.laptop-ui")["ssh"], None)
        self.assertEqual(self.record("game.laptop-ui")["remote"], os.path.join(self.root, "game"))
        self.assertFalse(os.path.exists(os.path.join(self.homes["laptop"], "channels.d")))
        # the leader posts a step for the member before it joins
        own = os.path.join(self.root, "game", "laptop-ui")
        entries.post(os.path.join(own, "STEPS.md"), own, "laptop-ui", "step 1",
                     ["@mac-web"], body="do it")
        entries.post(os.path.join(own, "STEPS.md"), own, "laptop-ui", "for someone else",
                     ["@win-x"], body="not yours")
        self.use_box("mac")
        out = self.ok("join", "game", "--server", "fake-dest")
        lines = out.splitlines()
        # the entries already addressed to it: CHANNEL.md's @all, and its step
        self.assertIn("entries for mac-web already in game:", lines)
        self.assertIn("  laptop-ui/CHANNEL.md", lines)
        self.assertIn("  laptop-ui/STEPS.md", lines)
        self.assertIn("  do it", lines)
        self.assertNotIn("  not yours", lines)
        self.assertNotIn("for someone else", out)
        self.assertEqual(self.record("game.mac-web")["leader"], "laptop-ui")
        # the JOIN entry is in the own RESULTS.md, to the leader, and join's own sync sent it
        # with MEMBER.md
        local = self.joined("game.mac-web")
        results = entries.parse_file(os.path.join(local, "mac-web", "RESULTS.md"))
        self.assertEqual([(e.id, e.title, e.to) for e in results],
                         [("mac-web#2", "JOIN", ("@laptop-ui",))])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game", "mac-web"))),
                         ["MEMBER.md", "RESULTS.md"])
        self.assertEqual(read_tree(os.path.join(self.root, "game", "mac-web"))["RESULTS.md"],
                         read_tree(os.path.join(local, "mac-web"))["RESULTS.md"])
        code, out, err = self.run_cli("sync", "game")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game", "mac-web"))),
                         ["MEMBER.md", "RESULTS.md"])

    def test_join_local(self):
        self.lead()
        self.use_box("linux")
        out = self.ok("join", "game", "--local", "--project", "x", "--role", "b")
        own = os.path.join(self.root, "game", "linux-x-b")
        # the watcher is next (a member posts no plan), then the check of the agent's tool,
        # then the note that the steps need its user's word; the OK line stays the last
        self.assertEqual(out.splitlines()[-4:], [
            platform.runnable("  next: start your watcher now (vcharon guide watch): "
                              "vcharon watch game --until-change --project x --role b"),
            platform.runnable(channel_cmd.FIRST_CHECK),
            "  note: if your user only asked you to join, ask them whether to work on the steps "
            "the leader assigns you",
            "OK  in game as linux-x-b; your folder is %s" % own])
        # a rejoin (a new session) doesn't ask again, nor tell it to check its tool
        out = self.ok("join", "game", "--local", "--project", "x", "--role", "b")
        self.assertIn("took back game/linux-x-b", out)
        self.assertNotIn(channel_cmd.ASK_USER, out.splitlines())
        self.assertNotIn(platform.runnable(channel_cmd.FIRST_CHECK), out.splitlines())
        self.assertEqual(sorted(os.listdir(own)), ["MEMBER.md", "RESULTS.md"])
        self.assertIn("entries for linux-x-b already in game:", out)
        self.assertEqual(self.record("game.linux-x-b"), {
            "version": 1, "channel": "game", "name": "linux-x-b", "leader": "laptop-ui",
            "ssh": None, "remote": os.path.join(self.root, "game"),
            "machine": TEST_MACHINE_ID, "project": "x", "role": "b",
            **util.record_format()})

    def test_the_same_name_in_two_channels(self):
        self.lead("game")
        self.lead("docs", box="win", project="d")
        self.ok("join", "game", "--server", "fake-dest")
        self.ok("join", "docs", "--server", "fake-dest")
        self.assertEqual(sorted(os.listdir(os.path.join(self.homes["mac"], "channels.d"))),
                         ["docs.mac-web.ini", "game.mac-web.ini"])
        for channel in ("game", "docs"):
            self.assertTrue(os.path.isdir(os.path.join(self.root, channel, "mac-web")))

    def test_no_box_set_is_the_os(self):
        # no [vcharon] box, then no config file at all: the OS's word, never a refusal
        with open(os.path.join(self.vcharon_home, "vcharon.ini"), "w") as f:
            f.write("[vcharon]\n")
        with mock.patch.object(platform, "os_word", return_value="mac"):
            out = self.ok("create", "game", "--server", "fake-dest")
        self.assertIn("vcharon: create game  as mac-web on fake-dest", out)
        os.remove(os.path.join(self.vcharon_home, "vcharon.ini"))
        with mock.patch.object(platform, "os_word", return_value="win"):
            out = self.ok("join", "game", "--server", "fake-dest", "--project", "api")
        self.assertIn("vcharon: join game  as win-api on fake-dest", out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))),
                         ["mac-web", "win-api"])

    def test_create_twice_and_bad_names(self):
        self.lead()
        self.use_box("win")
        self.assertEqual(self.refusal("create", "game", "--server", "fake-dest"),
                         ("ERROR channel: the channel game already exists",
                          "join it, or pick another name"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "win-web")))
        for name in ("Game", "a.b", "x" * 25, "con", "..", ".vcharon-closed-x"):
            with self.subTest(name=name):
                self.assertTrue(self.refused("create", name, "--local", code=3).startswith(
                    "ERROR config: "))
        self.assertEqual(sorted(os.listdir(self.root)), ["game"])

    def test_no_channel_or_no_leader_refused_before_the_claim(self):
        self.assertEqual(self.refusal("join", "game", "--server", "fake-dest"),
                         ("ERROR channel: there is no channel game on fake-dest: check its name",
                          platform.runnable("vcharon list --server fake-dest")))
        write_tree(self.root, {"game/a/MEMBER.md": b"m"})
        self.assertEqual(self.refusal("join", "game", "--server", "fake-dest"),
                         ("ERROR channel: game has no leader (no member's folder holds "
                          "CHANNEL.md)", "ask the user"))
        write_tree(self.root, {"game/a/CHANNEL.md": b"c", "game/b/CHANNEL.md": b"c"})
        self.assertEqual(self.refused("join", "game", "--local"),
                         "ERROR channel: game has 2 leaders (a/, b/ hold CHANNEL.md)")
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["a", "b"])
        self.assertFalse(os.path.exists(os.path.join(self.vcharon_home, "state", "channels")))
        self.assertFalse(os.path.exists(os.path.join(self.vcharon_home, "joined")))

    def test_a_name_taken_rejoin_and_another_server(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # the same name from another box: told to pass --role
        self.use_box("mac2", os.path.join(self.tmp, "second-mac"))
        with open(os.path.join(self.homes["mac2"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = mac\n")
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest"),
                         "ERROR channel: the name mac-web is taken in game")
        self.assertFalse(os.path.exists(os.path.join(self.homes["mac2"], "state", "channels")))
        # nor the watcher's lock file the join took and held
        self.assertEqual([p for p in read_tree(self.homes["mac2"]) if p.endswith(".lock")], [])
        self.ok("join", "game", "--server", "fake-dest", "--role", "b")
        # this box has the record: a rejoin, which takes the folder back
        self.use_box("mac")
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  took back game/mac-web; the leader is laptop-ui", out)
        # the record is for another server: refused before any claim
        os.environ["VCHARON_TEST_MACHINE_ID"] = OTHER_MACHINE
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest"),
                         "ERROR channel: you are in game on another server")
        os.environ["VCHARON_TEST_MACHINE_ID"] = TEST_MACHINE_ID
        # with --rejoin and no record: the user said it's this agent's
        os.remove(os.path.join(self.homes["mac"], "state", "channels", "game.mac-web.json"))
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest"),
                         "ERROR channel: the name mac-web is taken in game")
        out = self.ok("join", "game", "--server", "fake-dest", "--rejoin")
        self.assertIn("took back game/mac-web", out)
        self.assertEqual(self.record("game.mac-web")["leader"], "laptop-ui")

    def test_a_remote_end_must_be_linux(self):
        # a Mac or Windows box has a machine id now, so the client refuses it by its OS
        refused = ("ERROR state_mismatch: fake-dest runs %s: only a Linux server is supported "
                   "as a remote end")
        fix = "  fix: only a Linux server takes remote members: check the alias"
        self.use_box("laptop")
        os.environ["VCHARON_TEST_OS"] = "darwin"
        code, _out, err = self.channel("create", "game", "--server", "fake-dest", "--project", "ui")
        self.assertEqual((code, err.splitlines()[:2]), (3, [refused % "darwin", fix]))
        self.assertEqual(self.server_tree(), {})
        self.assertFalse(os.path.exists(channel_cmd.record_path("game", "laptop-ui")))
        os.environ["VCHARON_TEST_OS"] = "linux"
        self.lead()
        os.environ["VCHARON_TEST_OS"] = "windows"
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest", code=3),
                         refused % "windows")
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "mac-web")))
        os.environ["VCHARON_TEST_OS"] = "linux"
        self.ok("join", "game", "--server", "fake-dest")
        os.environ["VCHARON_TEST_OS"] = "darwin"
        before = self.server_tree()
        code, _out, err = self.channel("leave", "game")
        self.assertEqual((code, err.splitlines()[:2]), (3, [refused % "darwin", fix]))
        self.assertEqual(self.server_tree(), before)
        self.assertTrue(os.path.exists(channel_cmd.record_path("game", "mac-web")))

    def test_no_machine_id_here_has_its_os_hint(self):
        # a Mac: this check's refusal and hint come before the client id's (made after it)
        hint = ("check that /usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice prints an "
                "IOPlatformUUID line, then try again; if it prints none, ask your user")
        self.lead()
        before = self.server_tree()
        for verb in ("create", "join"):
            with self.subTest(verb=verb):
                with mock.patch.object(channel_cmd.platform, "machine_id", return_value=None), \
                        mock.patch.object(channel_cmd.platform, "os_name",
                                          return_value="darwin"), \
                        mock.patch.object(channel_cmd.platform, "no_machine_hint",
                                          return_value=hint):
                    code, _out, err = self.channel(verb, "game", "--local", "--project", "x")
                self.assertEqual((code, err.splitlines()[:2]),
                                 (3, ["ERROR state_mismatch: this machine has no machine id, so "
                                      "vcharon can't tie a channel's record to it",
                                      "  fix: " + hint]))
        self.assertEqual(self.server_tree(), before)
        self.assertFalse(os.path.exists(platform.client_id_path()))

    def test_the_leader_gets_the_join_with_the_folder(self):
        # no second sync by the member (no watcher, no post): the leader's next sync brings
        # the JOIN with MEMBER.md
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.use_box("laptop")
        self.assertEqual(self.run_cli("sync", "game", "--project", "ui")[0], 0)
        copy = os.path.join(self.joined("game.laptop-ui"), "mac-web")
        self.assertEqual(sorted(os.listdir(copy)), ["MEMBER.md", "RESULTS.md"])
        self.assertEqual([(e.id, e.title, e.to) for e in
                          entries.parse_file(os.path.join(copy, "RESULTS.md"))],
                         [("mac-web#2", "JOIN", ("@laptop-ui",))])

    def test_rejoin_without_a_local_tree_pulls_the_own_folder(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        local = self.joined("game.mac-web")
        own = os.path.join(local, "mac-web")
        entries.post(os.path.join(own, "RESULTS.md"), own, "mac-web", "step done",
                     ["@laptop-ui"], body="b")
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        server_copy = read_tree(os.path.join(self.root, "game", "mac-web"))
        # the box loses its tree; the record and the section stay
        shutil.rmtree(local)
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  pulled your folder from the server: 2 files", out)
        # nothing at the server was replaced; the REJOIN took the next number, and join's sync
        # sent it: the one change at the server is RESULTS.md gaining it at its end
        results = entries.parse_file(os.path.join(own, "RESULTS.md"))
        self.assertEqual([e.id for e in results], ["mac-web#2", "mac-web#3",
                                                   "mac-web#4"])
        self.assertEqual([e.title for e in results], ["JOIN", "step done", "REJOIN"])
        self.assertEqual(entries.next_number(own, "mac-web"), 5)
        after = read_tree(os.path.join(self.root, "game", "mac-web"))
        self.assertEqual({k: v for k, v in after.items() if k != "RESULTS.md"},
                         {k: v for k, v in server_copy.items() if k != "RESULTS.md"})
        self.assertTrue(after["RESULTS.md"].startswith(server_copy["RESULTS.md"]))
        self.assertEqual([e.title for e in entries.parse(after["RESULTS.md"].decode())],
                         ["JOIN", "step done", "REJOIN"])

    def test_join_refused_while_the_watcher_lock_is_held(self):
        self.lead()
        hold(self, channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web") + ".lock")
        refused = "ERROR channel: a live session holds mac-web in game"
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest"), refused)
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "mac-web")))
        # a server member's: the server mode's lock, on the channel folder as main keys it
        hold(self, channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web",
                                                os.path.join(self.root, "game")) + ".lock")
        self.assertEqual(self.refused("join", "game", "--local"), refused)

    def test_the_refusal_names_the_holders_membership(self):
        # a second agent in the leader's folder builds the leader's name: the refusal says the
        # name is the leader's membership by this machine's record, and the fix leaves which
        # case it is to the reader (the leader itself, re-running join after a /clear, gets the
        # same line for its own watcher)
        self.lead(where=("--local",), box="mac", project="web")
        # the leader re-running join is told nothing about steps: it runs the channel
        self.assertNotIn(channel_cmd.ASK_USER, self.ok("join", "game", "--local").splitlines())
        hold(self, channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web",
                                                os.path.join(self.root, "game")) + ".lock")
        fix = ("your own earlier watcher or command: keep it or let it end; another agent's "
               "(your user says so): join with --role R; unsure: ask your user")
        self.assertEqual(self.refusal("join", "game", "--local"), (
            "ERROR channel: a live session holds mac-web in game (this machine's record: the "
            "leader's membership, created here with --project web, no --role)", fix))
        # with --role R the agent is a member of its own, and the note names the leader's
        # membership without calling it the reader's
        out = self.ok("join", "game", "--local", "--role", "cc")
        self.assertEqual(out.splitlines()[0], "note: this project also holds game on this "
                         "machine as mac-web (--project web, no --role): another session's, or "
                         "yours with other flags")
        # a member's own name held: the record says it is a member, with its flags
        hold(self, channel_cmd.watcher_snapshot(None, "game.mac-web-cc", "mac-web-cc",
                                                os.path.join(self.root, "game")) + ".lock")
        self.assertEqual(self.refusal("join", "game", "--local", "--role", "cc"), (
            "ERROR channel: a live session holds mac-web-cc in game (this machine's record: a "
            "member, joined here with --project web --role cc)", fix))

    def test_the_refusal_for_a_name_held_on_another_server(self):
        # the record is of a membership on another server: never this join's to take, so the
        # fix is another member, and no alias is named. A --local join holding the same name
        # stands in for the real case, a second server's join whose section key (C.name) and
        # so watcher lock are the same
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        hold(self, channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web",
                                                os.path.join(self.root, "game")) + ".lock")
        self.assertEqual(self.refusal("join", "game", "--local"), (
            "ERROR channel: a live session holds mac-web in game (this machine's record: a "
            "membership on another server)", "pass --role R to join from here as another member"))

    def test_a_watcher_started_during_the_join_exits_12(self):
        # the join holds the watcher's lock to its end: a watcher started after the join's
        # check exits 12, so its sync can't take the job lock the join's own sync needs
        self.lead()
        snapshot = channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web")
        real = channel_cmd._run_section
        started, job_locks = [], []

        def watcher_sync(job, sync_args):
            # what a watcher that got its lock runs: a sync, which holds the job's lock
            job_locks.append(state.lock(job + ".up"))
            self.addCleanup(job_locks[-1].release)
            return 0, None, None

        def sync(args, section, full):
            lines = []
            started.append(watch.watch_job(section, ["game", "--project", "web"], 2,
                                           out=lines.append, sleep=lambda s: None,
                                           run=watcher_sync, rounds=1))
            started.append([line[len("2026-10-01 09:05:46 "):] for line in lines])
            return real(args, section, full)

        with mock.patch.object(channel_cmd, "_run_section", sync):
            code, out, err = self.channel("join", "game", "--server", "fake-dest")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("busy", out + err)
        self.assertIn("OK  in game as mac-web", out)
        self.assertEqual(started, [12, [WATCHER_LOCKED % snapshot]])
        self.assertEqual(job_locks, [])
        # released when the join returned: the member's watcher starts now
        lk = watch.take_lock(snapshot)
        self.assertIsNotNone(lk)
        lk.release()

    def test_a_refused_join_releases_the_watcher_lock(self):
        self.lead()
        # a refusal after the lock was taken
        no = channels.refused("refused for the test", "nothing")
        with mock.patch.object(channel_cmd, "_another_server", side_effect=no):
            self.assertEqual(self.refused("join", "game", "--server", "fake-dest"),
                             "ERROR channel: refused for the test")
        snapshot = channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web")
        # no record was written: the lock file it took goes too
        self.assertFalse(os.path.exists(snapshot + ".lock"))
        lk = watch.take_lock(snapshot)
        self.assertIsNotNone(lk)
        lk.release()

    def test_a_watcher_started_during_the_create_exits_12(self):
        # create holds the watcher's lock from its claim to its end, through its sync
        self.use_box("laptop")
        snapshot = channel_cmd.watcher_snapshot(None, "game.laptop-ui", "laptop-ui")
        real = channel_cmd._run_section
        started = []

        def sync(args, section, full):
            lines = []
            started.append(watch.watch_job(section, ["game", "--project", "ui"], 2,
                                           out=lines.append, sleep=lambda s: None,
                                           run=lambda job, sync_args: (0, None, None),
                                           rounds=1))
            started.append([line[len("2026-10-01 09:05:46 "):] for line in lines])
            return real(args, section, full)

        with mock.patch.object(channel_cmd, "_run_section", sync):
            out = self.ok("create", "game", "--server", "fake-dest", "--project", "ui")
        self.assertIn("OK  created game", out)
        self.assertEqual(started, [12, [WATCHER_LOCKED % snapshot]])
        lk = watch.take_lock(snapshot)
        self.assertIsNotNone(lk)
        lk.release()

    def test_create_refused_while_the_watcher_lock_is_held(self):
        self.use_box("laptop")
        lock = channel_cmd.watcher_snapshot(None, "game.laptop-ui", "laptop-ui") + ".lock"
        child = hold(self, lock)
        self.assertEqual(
            self.refused("create", "game", "--server", "fake-dest", "--project", "ui"),
            "ERROR channel: a live session holds laptop-ui in game")
        # the claim is released; the holder's lock file stays
        self.assertFalse(os.path.exists(os.path.join(self.root, "game")))
        release(child)
        self.assertTrue(os.path.exists(lock))

    def test_the_server_lock_name_is_the_watchers(self):
        # ~/…, ./… and the absolute form give one lock name (main's abspath(expanduser))
        # (the real path: on macOS the temp dir is under /var, a link to /private/var, and
        # getcwd() gives the /private form)
        tool = watch
        root = os.path.realpath(self.root)
        here = os.path.join(root, "game")
        a = channel_cmd.watcher_snapshot(None, "s", "me", here)
        self.assertEqual(a, tool.snapshot_path(os.path.abspath(here), "me"))
        os.makedirs(here)
        os.chdir(root)
        self.assertEqual(channel_cmd.watcher_snapshot(None, "s", "me", "./game"), a)
        # ntpath.expanduser takes USERPROFILE before HOME, so mock both
        with mock.patch.dict(os.environ, {"HOME": root, "USERPROFILE": root}):
            self.assertEqual(channel_cmd.watcher_snapshot(None, "s", "me", "~/game"), a)

    def test_a_failed_join_leaves_no_folder(self):
        self.lead()

        # after the claim: the writes fail halfway, having made the local tree
        def write_member(cfg, server, channel, name, leader, section, made, *a, **kw):
            local = channel_cmd.local_text(section)
            os.makedirs(local)
            made.append(local + os.sep)
            raise OSError(errno.EIO, "a failed write", local)

        with mock.patch.object(channel_cmd, "_write_member", write_member):
            code, _out, _err = self.channel("join", "game", "--server", "fake-dest")
        self.assertNotEqual(code, 0)
        # the claim is released and what was written undone
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["laptop-ui"])
        self.assertEqual(os.listdir(os.path.join(self.vcharon_home, "joined")), [])
        # so a plain join gets in, as a new member (a folder left there would be "taken")
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  claimed game/mac-web; the leader is laptop-ui", out.splitlines())

    def test_a_new_name_never_comes_from_the_home_folder(self):
        self.lead()
        home = os.path.join(self.tmp, "home", "alice")
        deep = os.path.join(home, "notes", "x")
        os.makedirs(deep)
        # the message names the folder as the working folder spells it: on macOS getcwd gives
        # /private/var/... for a temp folder made as /var/...
        os.chdir(home)
        home_seen = os.getcwd()
        homes = [home]
        if CAN_SYMLINK:
            # a home reached through a link (or spelled otherwise) is the same folder
            link = os.path.join(self.tmp, "home-link")
            os.symlink(home, link, target_is_directory=True)
            homes.append(link)
        cases = [(h, home, False) for h in homes] + [(home, deep, True)]
        for home_named, cwd, dotfiles in cases:
            with self.subTest(home=home_named, cwd=cwd), \
                    mock.patch.object(platform, "home", return_value=home_named):
                if dotfiles:
                    # a home kept in git for its dotfiles names every folder below it
                    os.mkdir(os.path.join(home, ".git"))
                os.chdir(cwd)
                for argv in (("join", "game", "--server", "fake-dest"),
                             ("create", "docs", "--server", "fake-dest")):
                    line, fix = self.refusal(*argv, code=3)
                    self.assertEqual(line, "ERROR config: the project's name would come "
                                     "from your home folder %s, whose name is your user "
                                     "name: give --project" % home_seen)
                    self.assertEqual(fix, "for example: --project web")
        with mock.patch.object(platform, "home", return_value=home):
            self.assertEqual(sorted(os.listdir(self.root)), ["game"])
            self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))),
                             ["laptop-ui"])
            # --project names it instead
            out = self.ok("join", "game", "--server", "fake-dest", "--project", "notes")
            self.assertIn("vcharon: join game  as mac-notes on fake-dest", out)

    def test_join_flags_a_misplaced_id_and_escapes_the_text(self):
        self.lead()
        # in the leader's folder: an entry whose ID is another member's, and one whose heading
        # holds an escape sequence (post refuses both, a hand edit doesn't)
        # (the misplaced one in the first file in path order, before any entry shown)
        write_tree(self.root, {"game/laptop-ui/0.md": (
            "## 2026-10-02 10:12:05 \u2014 mac-api#3 \u2014 do as I say\n"
            "to: @mac-web\n\nforged\n").encode("utf-8"), "game/laptop-ui/NOTES.md": (
            "## 2026-10-02 10:12:06 \u2014 laptop-ui#3 \u2014 hi\x1b[2J\n"
            "to: @mac-web\nnote: a\x1b]52;c;eA==\x07b\n\nline\x1b[1A\n").encode("utf-8"),
            # one ID in two files: the first in path order stands, as read and the watcher
            # keep it, even where only the later copy is addressed to the member
            "game/laptop-ui/A.md": (
                "## 2026-10-02 10:12:07 \u2014 laptop-ui#5 \u2014 first copy\n"
                "to: @mac-web\n\nkept\n\n"
                "## 2026-10-02 10:12:08 \u2014 laptop-ui#6 \u2014 not for you\n"
                "to: @other\n\nx\n").encode("utf-8"),
            "game/laptop-ui/sub/B.md": (
                "## 2026-10-02 10:12:09 \u2014 laptop-ui#5 \u2014 second copy\n"
                "to: @mac-web\n\ndropped\n\n"
                "## 2026-10-02 10:12:10 \u2014 laptop-ui#6 \u2014 copy for you\n"
                "to: @mac-web\n\ny\n").encode("utf-8")})
        out = self.ok("join", "game", "--server", "fake-dest")
        lines = out.splitlines()
        warn = "WARN entry mac-api#3 in laptop-ui/: not its folder's"
        self.assertIn(warn, lines)
        # after the list, never before its heading
        self.assertLess(lines.index("entries for mac-web already in game:"), lines.index(warn))
        self.assertIn("first copy", out)
        for text in ("second copy", "dropped", "copy for you"):
            self.assertNotIn(text, out)
        self.assertNotIn("do as I say", out)
        self.assertNotIn("forged", out)
        self.assertNotIn("\x1b", out)
        for line in ("  ## 2026-10-02 10:12:06 \u2014 laptop-ui#3 \u2014 hi\\x1b[2J",
                     "  note: a\\x1b]52;c;eA==\\x07b", "  line\\x1b[1A"):
            self.assertIn(line, lines)

    def test_a_failed_run_keeps_the_join(self):
        self.lead()
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            code, out, _err = self.channel("join", "game", "--server", "fake-dest")
        self.assertEqual(code, 4)
        self.assertIn(platform.runnable("vcharon: the sync failed; you are in game: run vcharon "
                                        "sync game --full --project web again"), out)
        # no next step and no OK line: the sync comes first
        self.assertNotIn("next: start your watcher", out)
        self.assertNotIn("OK  ", out)
        self.assertEqual(self.record("game.mac-web")["name"], "mac-web")
        # no MEMBER.md at the server, so no claimer: a plain join gets back in by the record
        member = os.path.join(self.root, "game", "mac-web")
        self.assertEqual(os.listdir(member), [])
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("took back game/mac-web", out)
        self.assertEqual(entries.header_of(entries.read_text(
            os.path.join(member, "MEMBER.md")), "mac-web")["claimer"], platform.claimer("game"))
        self.assertEqual(self.run_cli("sync", "game", "--full")[0], 0)


    @unittest.skipIf(os.name == "nt", "a folder's write permission is POSIX's")
    def test_a_record_that_cant_be_written_is_a_permission_error(self):
        # the file system's refusal, with its fix line, never "a bug in vcharon"; and the
        # claim is released, as for any failure before the record
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root writes any folder")
        self.use_box("laptop")
        self.ok("create", "first", "--local", "--project", "ui")
        records = os.path.join(os.environ["VCHARON_HOME"], "state", "channels")
        os.chmod(records, 0o500)
        try:
            line, fix = self.refusal("create", "game", "--local", "--project", "api")
        finally:
            os.chmod(records, 0o700)
        self.assertTrue(line.startswith("ERROR permission: %s: " % records), line)
        self.assertEqual(fix, "check the owner and permissions of %s" % records)
        self.assertFalse(os.path.exists(os.path.join(self.root, "game")))

class FirstLookTest(ChannelCase):
    """join and create save the member's watcher snapshot (watch.first_look): its first start
    prints what came after the join or create, once, and nothing join listed (DESIGN, "Create,
    join, leave, close")."""

    def post(self, box, project, to, title):
        self.use_box(box)
        self.ok("post", "game", "--to", to, "--title", title, "--body", "b",
                "--project", project)
        self.use_box("mac")

    def watch_local(self, box, name, once=False):
        """(exit code, lines without their time) of one round of name's watcher, as vcharon
        watch starts it."""
        self.use_box(box)
        record = channel_cmd.read_record("game", name)
        limits = channel_cmd.channel_limits(record)
        lines = []
        code = watch.watch_dir(channel_cmd.local_root(record), name, 10, out=lines.append,
                               sleep=lambda s: None, rounds=1, once=once,
                               folder_limits=(limits["max_mb"] * charter.MB,
                                              limits["max_files"]))
        self.use_box("mac")
        return code, [l[20:] for l in lines]

    def watch_remote(self, box, name, project):
        """watch_local's for a remote member: one round, whose sync runs in this process."""
        self.use_box(box)
        lines = []

        def run(job, sync_args):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = cli.sync_section(job)
            return code, None if code == 0 else "ERROR the sync exited %d" % code, None

        code = watch.watch_job("game." + name, ["game", "--project", project], 30,
                               out=lines.append, sleep=lambda s: None, run=run, rounds=1)
        self.use_box("mac")
        return code, [l[20:] for l in lines]

    @staticmethod
    def told(lines):
        return [l for l in lines if l.startswith(("to you: ", "to all: "))]

    def snapshot(self, box, name, local=True):
        self.use_box(box)
        path = channel_cmd.watcher_snapshot(None, "game." + name, name,
                                            os.path.join(self.root, "game") if local else None)
        self.use_box("mac")
        return path

    def join_local(self, *extra):
        self.use_box("linux")
        out = self.ok("join", "game", "--local", "--project", "x", *extra)
        self.use_box("mac")
        return out

    def test_a_local_member_and_leader(self):
        self.lead(where=("--local",))
        out = self.join_local()
        self.assertIn("  laptop-ui/CHANNEL.md", out.splitlines())
        self.post("laptop", "ui", "@linux-x", "step 1")
        code, lines = self.watch_local("linux", "linux-x")
        self.assertEqual(code, 0)
        self.assertIn(", since ", lines[0])
        self.assertEqual([l.split("  (")[0] for l in self.told(lines)],
                         ["to you: laptop-ui#3 — step 1"])
        # told once
        self.assertEqual(self.told(self.watch_local("linux", "linux-x")[1]), [])
        # the leader's first start: the JOIN that came after its create
        lines = self.watch_local("laptop", "laptop-ui")[1]
        self.assertEqual(self.told(lines), ["to you: linux-x#2 — JOIN  (linux-x/RESULTS.md)"])

    def test_once_right_after_the_join(self):
        self.lead(where=("--local",))
        self.join_local()
        code, lines = self.watch_local("linux", "linux-x", once=True)
        self.assertEqual((code, lines[-1]), (watch.EXIT_NOTHING, "EXIT nothing new"))
        self.post("laptop", "ui", "@linux-x", "step 1")
        code, lines = self.watch_local("linux", "linux-x", once=True)
        self.assertEqual((code, lines[-1]), (watch.EXIT_CHANGE, "EXIT change"))

    def test_a_remote_member_after_a_sync_by_hand(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.post("laptop", "ui", "@mac-web", "step 1")
        # the by-hand sync brings it before the first start
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        lines = self.watch_remote("mac", "mac-web", "web")[1]
        self.assertEqual([l.split("  (")[0] for l in self.told(lines)],
                         ["to you: laptop-ui#3 — step 1"])
        self.use_box("laptop")
        self.assertEqual(self.run_cli("sync", "game", "--project", "ui")[0], 0)
        lines = self.watch_remote("laptop", "laptop-ui", "ui")[1]
        self.assertEqual(self.told(lines), ["to you: mac-web#2 — JOIN  (mac-web/RESULTS.md)"])

    def test_a_join_between_create_s_up_and_down(self):
        real = channel_cmd._run_section
        calls = []

        def sync(args, section, full):
            calls.append(section)
            if len(calls) > 1:
                return real(args, section, full)
            code = self.run_jobs(section + ".up", "--full")[0]
            self.use_box("mac")
            self.ok("join", "game", "--server", "fake-dest")
            self.use_box("laptop")
            return code or self.run_jobs(section + ".down", "--full")[0]

        self.use_box("laptop")
        with mock.patch.object(channel_cmd, "_run_section", sync):
            self.ok("create", "game", "--server", "fake-dest", "--project", "ui")
        self.assertEqual(calls, ["game.laptop-ui", "game.mac-web"])
        # create's down brought the JOIN
        self.assertTrue(os.path.exists(os.path.join(self.joined("game.laptop-ui"), "mac-web",
                                                    "RESULTS.md")))
        lines = self.watch_remote("laptop", "laptop-ui", "ui")[1]
        self.assertEqual(self.told(lines), ["to you: mac-web#2 — JOIN  (mac-web/RESULTS.md)"])

    def test_a_join_whose_sync_failed(self):
        self.lead()
        self.post("laptop", "ui", "@mac-web", "early")
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            self.assertEqual(self.channel("join", "game", "--server", "fake-dest")[0], 4)
        # the fix line's sync, then the first start
        self.assertEqual(self.run_cli("sync", "game", "--full")[0], 0)
        lines = self.watch_remote("mac", "mac-web", "web")[1]
        self.assertEqual([l.split("  (")[0] for l in self.told(lines)],
                         ["to all: laptop-ui#2 — channel game created",
                          "to you: laptop-ui#3 — early"])

    def test_an_entry_after_the_look_is_the_watcher_s(self):
        self.lead(where=("--local",))
        real = watch.first_look
        own = os.path.join(self.root, "game", "laptop-ui")

        def first_look(*a, **kw):
            got = real(*a, **kw)
            entries.post(os.path.join(own, "STEPS.md"), own, "laptop-ui", "late", ["@linux-x"])
            return got

        with mock.patch.object(watch, "first_look", first_look):
            out = self.join_local()
        self.assertIn("  laptop-ui/CHANNEL.md", out.splitlines())
        self.assertNotIn("late", out)
        lines = self.watch_local("linux", "linux-x")[1]
        self.assertEqual(self.told(lines), ["to you: laptop-ui#3 — late  (laptop-ui/STEPS.md)"])

    def test_a_misplaced_entry_after_the_look_is_warned_once(self):
        # the marks filter comes before the not-its-folder warning: the watcher prints that one
        self.lead(where=("--local",))
        real = watch.first_look
        own = os.path.join(self.root, "game", "laptop-ui")

        def first_look(*a, **kw):
            got = real(*a, **kw)
            entries.post(os.path.join(own, "STEPS.md"), own, "zed", "late", ["@linux-x"])
            return got

        with mock.patch.object(watch, "first_look", first_look):
            out = self.join_local()
        self.assertIn("  laptop-ui/CHANNEL.md", out.splitlines())
        self.assertNotIn("not its folder's", out)
        lines = self.watch_local("linux", "linux-x")[1]
        self.assertEqual([l for l in lines if "not its folder's" in l],
                         ["WARN entry zed#1 in laptop-ui/: not its folder's"])

    def test_a_rejoin_keeps_its_snapshot(self):
        self.lead(where=("--local",))
        self.join_local()
        self.watch_local("linux", "linux-x")
        # while no watcher runs
        self.post("laptop", "ui", "@linux-x", "while away")
        self.post("laptop", "ui", "@win-y", "for someone else")
        with open(os.path.join(self.root, "game", "laptop-ui", "notes2.txt"), "w",
                  encoding="utf-8") as f:
            f.write("n")
        out = self.join_local()
        self.assertIn("took back game/linux-x", out)
        self.assertIn("while away", out)
        lines = self.watch_local("linux", "linux-x")[1]
        self.assertIn(", since ", lines[0])
        self.assertEqual([l.split("  (")[0] for l in self.told(lines)],
                         ["to you: laptop-ui#3 — while away"])
        self.assertIn("1 other entry (laptop-ui)", lines)
        self.assertIn("new laptop-ui/notes2.txt", lines)

    def test_a_rejoin_without_a_usable_snapshot_saves_one(self):
        self.lead(where=("--local",))
        self.join_local()
        path = self.snapshot("linux", "linux-x")
        for spoil in (os.remove, lambda p: open(p, "w", encoding="utf-8").close()):
            spoil(path)
            self.post("laptop", "ui", "@linux-x", "listed")
            self.assertIn("listed", self.join_local())
            lines = self.watch_local("linux", "linux-x")[1]
            self.assertIn(", since ", lines[0])
            self.assertEqual(self.told(lines), [])

    def test_a_first_join_replaces_a_leftover_snapshot(self):
        self.lead(where=("--local",))
        # an earlier channel's of the same name: its marks would take the new one's as seen
        path = self.snapshot("linux", "linux-x")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        watch.save_snapshot(path, os.path.join(self.root, "game"), "linux-x", {},
                            "2026-01-01 00:00:00",
                            marks=watch.Marks({"laptop-ui": {"low": 10, "more": []}}))
        self.join_local()
        self.post("laptop", "ui", "@linux-x", "step 1")
        lines = self.watch_local("linux", "linux-x")[1]
        self.assertEqual([l.split("  (")[0] for l in self.told(lines)],
                         ["to you: laptop-ui#3 — step 1"])

    def test_a_failed_save_is_a_note(self):
        failed = mock.patch.object(watch, "save_snapshot",
                                   side_effect=OSError(errno.ENOSPC, "No space left on device"))
        self.use_box("laptop")
        with failed:
            out = self.ok("create", "game", "--local", "--project", "ui")
        self.assertIn(platform.runnable(channel_cmd.SNAPSHOT_NOT_SAVED % (
            "No space left on device", "game", "--project ui")), out.splitlines())
        self.assertFalse(os.path.exists(self.snapshot("laptop", "laptop-ui")))
        self.use_box("linux")
        with failed:
            out = self.ok("join", "game", "--local", "--project", "x")
        self.assertIn(platform.runnable(channel_cmd.SNAPSHOT_NOT_SAVED % (
            "No space left on device", "game", "--project x")), out.splitlines())
        # listed whole, as before
        self.assertIn("  laptop-ui/CHANNEL.md", out.splitlines())
        self.assertIn("OK  in game as linux-x", out)
        self.assertFalse(os.path.exists(self.snapshot("linux", "linux-x")))
        # the first start is a baseline
        lines = self.watch_local("linux", "linux-x")[1]
        self.assertNotIn(", since ", lines[0])

    def test_a_record_that_can_t_be_read_in_the_look_is_a_note(self):
        broken = mock.patch.object(channel_cmd, "channel_limits",
                                   side_effect=VCharonError("config", "the limits broke"))
        self.lead(where=("--local",))
        self.use_box("linux")
        with broken:
            code, out, _err = self.channel("join", "game", "--local", "--project", "x")
        self.assertEqual(code, 0)
        self.assertIn(platform.runnable(channel_cmd.SNAPSHOT_NOT_SAVED % (
            "the limits broke", "game", "--project x")), out.splitlines())
        self.assertIn("OK  in game as linux-x", out)
        self.assertFalse(os.path.exists(self.snapshot("linux", "linux-x")))

    def test_an_interrupted_create_saves_nothing(self):
        self.use_box("laptop")
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 130):
            code, _out, _err = self.channel("create", "game", "--server", "fake-dest",
                                            "--project", "ui")
        self.assertEqual(code, 130)
        path = self.snapshot("laptop", "laptop-ui", local=False)
        self.assertTrue(os.path.exists(path + ".lock"))
        self.assertFalse(os.path.exists(path))

    def test_a_leader_s_rejoin_keeps_create_s_snapshot(self):
        # create's snapshot holds no file in another folder: still a usable one, so a new
        # session's rejoin before the first start keeps it and the JOIN stays new
        self.lead(where=("--local",))
        self.join_local()
        path = self.snapshot("laptop", "laptop-ui")
        with open(path, "rb") as f:
            created = f.read()
        self.use_box("laptop")
        out = self.ok("join", "game", "--local", "--project", "ui")
        self.use_box("mac")
        self.assertIn("took back game/laptop-ui", out)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), created)
        lines = self.watch_local("laptop", "laptop-ui")[1]
        self.assertEqual(self.told(lines), ["to you: linux-x#2 — JOIN  (linux-x/RESULTS.md)"])

    def test_a_create_whose_sync_failed_saves_its_snapshot(self):
        # the fix line's sync --full, a member's join and the leader's sync by hand all come
        # before the leader's first start: the JOIN is still printed
        self.use_box("laptop")
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            code, out, _err = self.channel("create", "game", "--server", "fake-dest",
                                           "--project", "ui")
        self.assertEqual(code, 4)
        self.assertIn("the sync failed; game is created", out)
        self.assertTrue(os.path.exists(self.snapshot("laptop", "laptop-ui", local=False)))
        self.use_box("laptop")
        code, out, err = self.run_cli("sync", "game", "--full", "--project", "ui")
        self.assertEqual(code, 0, out + err)
        self.use_box("mac")
        self.ok("join", "game", "--server", "fake-dest")
        self.use_box("laptop")
        self.assertEqual(self.run_cli("sync", "game", "--project", "ui")[0], 0)
        lines = self.watch_remote("laptop", "laptop-ui", "ui")[1]
        self.assertIn(", since ", lines[0])
        self.assertEqual(self.told(lines), ["to you: mac-web#2 — JOIN  (mac-web/RESULTS.md)"])

    def test_a_rejoin_with_no_record_replaces_a_leftover_snapshot(self):
        # no record on this machine: a snapshot there is no session's of this membership
        self.lead(where=("--local",))
        self.join_local()
        self.use_box("linux")
        os.remove(channel_cmd.record_path("game", "linux-x"))
        self.use_box("mac")
        path = self.snapshot("linux", "linux-x")
        watch.save_snapshot(path, os.path.join(self.root, "game"), "linux-x", {},
                            "2026-01-01 00:00:00",
                            marks=watch.Marks({"laptop-ui": {"low": 10, "more": []}}))
        out = self.join_local("--rejoin")
        self.assertIn("took back game/linux-x", out)
        self.post("laptop", "ui", "@linux-x", "step 1")
        lines = self.watch_local("linux", "linux-x")[1]
        self.assertEqual([l.split("  (")[0] for l in self.told(lines)],
                         ["to you: laptop-ui#3 — step 1"])


def _script(argv):
    return ("import sys\n"
            "sys.path[:0] = [%r]\n"
            "from vcharon import cli, ssh\n"
            "ssh.ssh_prefix = lambda settings: [sys.executable, %r]\n"
            "sys.exit(cli.main(%r))\n" % (VCHARON_DIR, FAKE_SSH, list(argv)))


class StaleSkillTest(ChannelCase):
    """join and create note a skill copy vcharon wrote that isn't this version's (DESIGN,
    "Create, join, leave, close"): on stdout, just before the next: line; never a refusal."""

    def setUp(self):
        ChannelCase.setUp(self)
        util.skill_home(self)

    def note(self, *agents):
        targets = " and ".join(skill.path(a) for a in agents)
        head = ("note: your vcharon skill at %s is from another version: " if len(agents) == 1
                else "note: your vcharon skills at %s are from another version: ")
        return "  " + head % targets + platform.runnable(
            "vcharon skill install %s" % " ".join("--" + a for a in agents))

    def join(self, project="x"):
        """A local join's stdout, which must exit 0 with nothing on stderr, less the notes
        after next: (checked here)."""
        code, out, err = self.channel("join", "game", "--local", "--project", project)
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        self.assertEqual(lines.pop(-2), channel_cmd.ASK_USER, lines)
        self.assertEqual(lines.pop(-2), platform.runnable(channel_cmd.FIRST_CHECK), lines)
        return lines

    def test_create_notes_a_stale_copy(self):
        util.write_skill("claude", util.OLD_SKILL)
        util.write_skill("codex")
        self.use_box("laptop")
        code, out, err = self.channel("create", "game", "--local", "--project", "ui")
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        self.assertEqual(lines[-5], self.note("claude"))
        self.assertTrue(lines[-4].startswith(platform.runnable("  next: ")), lines)
        self.assertEqual([l for l in lines if "skill" in l], [self.note("claude")])

    def test_join_notes_the_stale_copies(self):
        self.lead(where=("--local",))
        util.write_skill("codex", util.OLD_SKILL)
        lines = self.join("x")
        self.assertEqual(lines[-3], self.note("codex"))
        self.assertTrue(lines[-2].startswith(platform.runnable("  next: ")), lines)
        util.write_skill("claude", util.OLD_SKILL)
        lines = self.join("y")
        self.assertEqual(lines[-3], self.note("claude", "codex"))
        self.assertEqual(len([l for l in lines if "skill" in l]), 1, lines)

    def test_no_note(self):
        self.lead(where=("--local",))
        projects = iter("abcdefg")

        def quiet():
            lines = self.join(next(projects))
            self.assertEqual([l for l in lines if "skill" in l], [])
            self.assertTrue(lines[-2].startswith(platform.runnable("  next: ")), lines)

        # none installed
        quiet()
        # this version's copies
        util.write_skill("claude")
        util.write_skill("codex")
        quiet()
        # the user's own file of that name, without the marker
        util.write_skill("claude", "---\nname: vcharon\n---\nmy own notes\n")
        quiet()
        # a folder where the file goes
        os.remove(skill.path("codex"))
        os.mkdir(skill.path("codex"))
        quiet()
        # a check that fails outright: no note, and the join still works
        with mock.patch.object(skill, "installed", side_effect=OSError("broken")):
            quiet()
        with mock.patch.object(skill, "text", side_effect=OSError("no SKILL.md")):
            util.write_skill("claude", util.OLD_SKILL)
            quiet()

    @unittest.skipIf(os.name == "nt", "Windows names can't hold a control character")
    def test_a_control_character_in_the_path_is_escaped(self):
        self.lead(where=("--local",))
        home = os.path.join(self.tmp, "a\x1bb")
        os.mkdir(home)
        os.environ.update(HOME=home, USERPROFILE=home)
        target = util.write_skill("claude", util.OLD_SKILL)
        lines = self.join()
        self.assertEqual(lines[-3], self.note("claude").replace(target, target.replace(
            "\x1b", "\\x1b")))

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "needs a file this user can't read")
    def test_an_unreadable_copy(self):
        self.lead(where=("--local",))
        target = util.write_skill("claude", util.OLD_SKILL)
        os.chmod(target, 0)
        self.addCleanup(os.chmod, target, 0o600)
        lines = self.join()
        self.assertEqual([l for l in lines if "skill" in l], [])


class FirstCheckTest(ChannelCase):
    """A first join and a create tell an agent other than Claude Code to check its tool once
    (the guide's one-minute check), right after the watcher's next: line; a rejoin doesn't."""

    def lines(self, *argv):
        code, out, err = self.channel(*argv)
        self.assertEqual((code, err), (0, ""), out)
        return out.splitlines()

    def test_a_first_join_of_another_agent(self):
        self.lead(where=("--local",))
        lines = self.lines("join", "game", "--local", "--agent", "opencode")
        at = lines.index(platform.runnable(channel_cmd.FIRST_CHECK))
        self.assertTrue(lines[at - 1].startswith(platform.runnable("  next: start your "
                                                                   "watcher")), lines)
        self.assertEqual(lines[at + 1], channel_cmd.ASK_USER)
        # a rejoin, a new session of the same agent, did the check already
        self.assertNotIn(platform.runnable(channel_cmd.FIRST_CHECK),
                         self.lines("join", "game", "--local", "--agent", "opencode"))

    def test_claude_code_skips_it(self):
        self.use_box("laptop")
        lines = self.lines("create", "game", "--local", "--project", "ui", "--agent", "claude")
        self.assertNotIn(platform.runnable(channel_cmd.FIRST_CHECK), lines)
        self.assertTrue(lines[-3].startswith(platform.runnable("  next: ")), lines)
        self.use_box("mac")
        lines = self.lines("join", "game", "--local", "--agent", "claude")
        self.assertNotIn(platform.runnable(channel_cmd.FIRST_CHECK), lines)
        self.assertEqual(lines[-2], channel_cmd.ASK_USER)

    def test_a_create_of_another_agent(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "t"}):
            lines = self.lines("create", "game", "--local", "--project", "ui")
        # between the watcher's line and the plan's: "that command" is the watcher's
        self.assertEqual(lines[-3], platform.runnable(channel_cmd.FIRST_CHECK))
        self.assertTrue(lines[-4].startswith(platform.runnable("  next: ")), lines)
        self.assertTrue(lines[-2].startswith(platform.runnable("  then post the plan")), lines)


class ProjectNoteTest(ChannelCase):
    """join and create say where a project part they derived came from, and how to undo the
    membership (DESIGN, "Member names"): one note line right after their `vcharon: join|create`
    line, never with --project, and never in a channel file (it may name a path)."""

    def run_in(self, cwd, *argv):
        """stdout's lines of argv run from cwd, which must exit 0 with nothing on stderr; and
        the folder as the process sees it (macOS's getcwd gives /private/var for /var)."""
        os.chdir(cwd)
        code, out, err = self.channel(*argv)
        self.assertEqual((code, err), (0, ""), out)
        return out.splitlines(), os.getcwd()

    def undo(self, verb, flags_):
        return platform.runnable("if that is the wrong project: vcharon %s game %s (then %s "
                                 "again with --project P)" % (
                                     "close" if verb == "create" else "leave", flags_,
                                     "create it" if verb == "create" else "join"))

    def test_a_workspace_of_checkouts(self):
        # a folder that isn't a checkout, holding checkouts: the name depends on where the
        # agent starts, so each start says which it was
        ws = os.path.join(self.tmp, "ws")
        src = os.path.join(ws, "server", "src")
        deep = os.path.join(src, "deep")
        os.makedirs(deep)
        os.mkdir(os.path.join(src, ".svn"))
        self.use_box("laptop")
        lines, _ = self.run_in(ws, "create", "game", "--local")
        self.assertEqual(lines[:2], [
            "vcharon: create game  as laptop-ws on this machine",
            "  note: project ws is this folder's name (no .git, .svn or .hg here or above); "
            + self.undo("create", "--project ws")])
        self.use_box("mac")
        lines, here = self.run_in(deep, "join", "game", "--local", "--role", "b")
        self.assertEqual(lines[:2], [
            "vcharon: join game  as mac-src-b on this machine",
            "  note: project src is the checkout %s (.svn); " % os.path.dirname(here)
            + self.undo("join", "--project src --role b")])
        # the path is this box's only: no channel file holds it
        files = {rel: data for rel, data in self.server_tree().items() if data is not None}
        self.assertIn("game/mac-src-b/MEMBER.md", files)
        for rel, data in files.items():
            self.assertNotIn(os.path.dirname(here).encode("utf-8"), data, rel)
            self.assertNotIn(b"note: project", data, rel)

    def test_a_checkout_in_the_current_folder(self):
        self.lead(where=("--local",))
        for mark in (".git", ".hg"):
            with self.subTest(mark=mark):
                repo = os.path.join(self.tmp, "co" + mark[1:])
                os.makedirs(os.path.join(repo, mark))
                lines, here = self.run_in(repo, "join", "game", "--local")
                self.assertEqual(lines[1], "  note: project co%s is the checkout %s (%s); "
                                 % (mark[1:], here, mark)
                                 + self.undo("join", "--project co" + mark[1:]))

    def test_a_rejoin_notes_it_without_the_undo(self):
        # the record is found by the project the folder gives, so the second join is a rejoin
        # of the same name: still from the folder, so still noted, but its name is settled:
        # no leave offered at each session
        self.lead(where=("--local",))
        lines, here = self.run_in(self.project, "join", "game", "--local")
        where = "  note: project web is the checkout %s (.git)" % here
        self.assertEqual(lines[1], where + "; " + self.undo("join", "--project web"))
        lines, _ = self.run_in(self.project, "join", "game", "--local")
        self.assertEqual(lines[1], where)
        self.assertIn("  took back game/mac-web; the leader is laptop-ui", lines)

    def test_a_note_comes_after_the_also_hold_one(self):
        # "note: this project also holds" comes before the vcharon: line; the project note after it
        self.lead(where=("--local",))
        self.run_in(self.project, "join", "game", "--local", "--role", "b")
        lines, here = self.run_in(self.project, "join", "game", "--local")
        self.assertEqual(lines[:3], [
            "note: this project also holds game on this machine as mac-web-b (--project web "
            "--role b): another session's, or yours with other flags",
            "vcharon: join game  as mac-web on this machine",
            "  note: project web is the checkout %s (.git); " % here
            + self.undo("join", "--project web")])

    def test_no_note_with_project(self):
        for argv in (("create", "game", "--local", "--project", "ui"),
                     ("join", "game", "--local", "--project", "api")):
            with self.subTest(argv=argv):
                lines, _ = self.run_in(self.project, *argv)
                self.assertEqual([l for l in lines if "note: project" in l], [])


class ConcurrencyTest(ChannelCase):
    """Two vcharon processes at once, each with its own fake ssh and helper: the claim's one
    mkdir lets exactly one win."""

    def both(self, homes, argv):
        procs = []
        for home in homes:
            env = dict(os.environ, VCHARON_HOME=home)
            # the child writes UTF-8 (cli.py _utf8_console), never the locale's code page
            procs.append(subprocess.Popen([sys.executable, "-c", _script(argv)], env=env,
                                          cwd=self.project, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, universal_newlines=True,
                                          encoding="utf-8"))
        return [(p.wait(timeout=120),) + p.communicate() for p in procs]

    def test_two_creates_at_once(self):
        a, b = self.use_box("a"), self.use_box("b")
        got = self.both([a, b], ["create", "race", "--server", "fake-dest"])
        codes = sorted(g[0] for g in got)
        self.assertEqual(codes, [0, 1], got)
        loser = next(g for g in got if g[0] == 1)
        self.assertIn("ERROR channel: the channel race already exists", loser[2])
        # one leader, no empty channel, no second member
        self.assertEqual(len(os.listdir(os.path.join(self.root, "race"))), 1)

    def test_two_joins_with_one_name_from_two_boxes(self):
        self.lead()
        a = self.homes["mac"]
        b = os.path.join(self.tmp, "second-mac")
        os.makedirs(b)
        with open(os.path.join(b, "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = mac\n")
        got = self.both([a, b], ["join", "game", "--server", "fake-dest"])
        self.assertEqual(sorted(g[0] for g in got), [0, 1], got)
        loser = next(g for g in got if g[0] == 1)
        self.assertIn("ERROR channel: the name mac-web is taken in game\n  fix: pass --role R to "
                      "join as another member;", loser[2])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))),
                         ["laptop-ui", "mac-web"])


class ServerCallsTest(ChannelCase):
    """The channel root's functions, in-process and through the helper: names checked again,
    only directly below the root, never through a symlink."""

    def test_names_refused_on_every_call(self):
        os.makedirs(os.path.join(self.root, "game"))
        for channel, name in (("..", "a"), ("", "a"), (".", "a"), ("game", ".."),
                              ("game", ""), ("a/b", "c"), ("game", "a/b"), ("Game", "a"),
                              ("game", "A"), ("x" * 25, "a"), ("con", "a"), ("game", "nul")):
            for fn in ("claim", "release", "remove"):
                with self.subTest(channel=channel, name=name, fn=fn):
                    args = (self.root, channel, name) + ((False,) if fn == "claim" else ())
                    with self.assertRaises(VCharonError) as cm:
                        getattr(channels, fn)(*args)
                    self.assertEqual(cm.exception.code, "channel")
        self.assertEqual(os.listdir(os.path.join(self.root, "game")), [])

    def test_over_the_wire(self):
        s = self.session()
        s.open()
        self.assertEqual(s.call("channel.list", {}), {"channels": [], "others": []})
        got = s.call("channel.claim", {"channel": "game", "name": "a", "create": True})
        # no host name in the reply: its values go into the channel's files
        # a new channel has no CHANNEL.md yet: no format
        none = {"max_mb": None, "max_files": None, "max_entry_kb": None}
        self.assertEqual(got, {"existed": False, "machine": TEST_MACHINE_ID,
                               "root": self.root, "claimer": None, "format": None,
                               "limits": none})
        # an existing folder: its MEMBER.md's claimer:, read at the server
        entries.post(os.path.join(self.root, "game", "a", "MEMBER.md"),
                     os.path.join(self.root, "game", "a"), "a", "member", ["@a"],
                     header=[("leader", "a"), ("claimer", "0123456789abcdef")], number=1)
        got = s.call("channel.claim", {"channel": "game", "name": "a", "create": False})
        self.assertEqual((got["existed"], got["claimer"]), (True, "0123456789abcdef"))
        with self.assertRaises(VCharonError) as cm:
            s.call("channel.claim", {"channel": "..", "name": "a", "create": False})
        self.assertEqual(cm.exception.code, "channel")
        with self.assertRaises(VCharonError) as cm:
            s.call("channel.claim", {"channel": "game", "name": "a"})
        self.assertEqual(cm.exception.code, "protocol")

    def test_member_fields_come_from_the_members_own_first_entry(self):
        # another member's #1 first in the file (copied in by hand): the claim and the list
        # read a#1, as set_header and the rejoin do
        text = ("# MEMBER\n\n## t \u2014 b#1 \u2014 member\nto: @b\nbox: bbb\n"
                "claimer: %s\n\n## t \u2014 a#1 \u2014 member\nto: @a\nbox: aaa\n"
                "claimer: %s\n" % ("b" * 16, "a" * 16))
        write_tree(self.root, {"game/a/MEMBER.md": text.encode("utf-8")})
        got = channels.claim(self.root, "game", "a", False)
        self.assertEqual((got["existed"], got["claimer"]), (True, "a" * 16))
        [ch] = channels.list_channels(self.root)["channels"]
        self.assertEqual(ch["fields"]["a"]["box"], "aaa")
        # no a#1 at all: nothing
        write_tree(self.root, {"game/c/MEMBER.md": text.replace("a#1", "a#2").encode("utf-8")})
        self.assertIsNone(channels.claim(self.root, "game", "c", False)["claimer"])
        [ch] = channels.list_channels(self.root)["channels"]
        self.assertEqual(ch["fields"]["c"], {"box": None, "os": None, "agent": None,
                                             "project": None})

    def test_member_fields_skip_lines_they_dont_know(self):
        # a newer vcharon's MEMBER.md may hold more header lines: each field is still read, the
        # rest left alone; the version only in a version's shape
        text = ("# MEMBER\n\n## t — a#1 — member\nto: @a\nbox: aaa\nnewer: 1\n"
                "a line without a colon\nvcharon: %s\nclaimer: %s\n" % ("%s", "a" * 16))
        for version, want in (("0.1.0", "0.1.0"), ("0.2.0rc1", "0.2.0rc1"),
                              ("1.0.0+local.2", "1.0.0+local.2"), ("0.2 beta", None),
                              ("v0.1.0", None), ("", None)):
            with self.subTest(version=version):
                found = channels.member_fields((text % version).encode("utf-8"), "a")
                self.assertEqual(found, {"box": "aaa", "os": None, "agent": None,
                                         "project": None, "claimer": "a" * 16,
                                         "vcharon": want})
        # vcharon list shows its four fields, never the version
        write_tree(self.root, {"game/a/MEMBER.md": (text % "0.1.0").encode("utf-8")})
        [ch] = channels.list_channels(self.root)["channels"]
        self.assertEqual(ch["fields"]["a"], {"box": "aaa", "os": None, "agent": None,
                                             "project": None})
        self.assertEqual(channels.claim(self.root, "game", "a", False)["claimer"], "a" * 16)

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_symlinks_refused(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        write_tree(elsewhere, {"lead/CHANNEL.md": b"c"})
        os.makedirs(self.root)
        os.symlink(elsewhere, os.path.join(self.root, "game"))
        for fn, args in (("claim", (False,)), ("claim", (True,)), ("release", ()),
                         ("remove", ())):
            with self.subTest(fn=fn, args=args):
                with self.assertRaises(VCharonError) as cm:
                    getattr(channels, fn)(self.root, "game", "lead", *args)
                self.assertEqual(cm.exception.code, "channel", cm.exception.message)
        # a symlinked member folder
        os.remove(os.path.join(self.root, "game"))
        os.makedirs(os.path.join(self.root, "docs"))
        os.symlink(os.path.join(elsewhere, "lead"), os.path.join(self.root, "docs", "lead"))
        for fn in ("claim", "release", "remove"):
            with self.subTest(fn=fn):
                with self.assertRaises(VCharonError) as cm:
                    getattr(channels, fn)(self.root, "docs", "lead",
                                          *((False,) if fn == "claim" else ()))
                self.assertEqual(cm.exception.code, "channel", cm.exception.message)
        self.assertEqual(read_tree(elsewhere), {"lead/": None, "lead/CHANNEL.md": b"c"})
        listing = channels.list_channels(self.root)
        self.assertEqual([c["name"] for c in listing["channels"]], ["docs"])
        self.assertEqual(listing["channels"][0]["members"], [])

    def test_release_only_what_a_failed_join_made(self):
        write_tree(self.root, {"game/lead/CHANNEL.md": b"c", "game/a/MEMBER.md": b"m",
                               "game/b/MEMBER.md": b"m", "game/b/RESULTS.md": b"r"})
        self.assertEqual(channels.release(self.root, "game", "a"), {"removed": True})
        self.assertEqual(channels.release(self.root, "game", "b"), {"removed": False})
        self.assertEqual(channels.release(self.root, "game", "zz"), {"removed": False})
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["b", "lead"])
        # the last member's release removes the channel too
        write_tree(self.root, {"solo/lead/MEMBER.md": b"m", "solo/lead/CHANNEL.md": b"c"})
        self.assertEqual(channels.release(self.root, "solo", "lead"), {"removed": True})
        self.assertEqual(os.listdir(self.root), ["game"])

    def test_list(self):
        write_tree(self.root, {"game/lead/CHANNEL.md": b"c", "game/a/MEMBER.md": b"m",
                               "game/notes.md": b"n", "two/x/CHANNEL.md": b"c",
                               "two/y/CHANNEL.md": b"c", "empty/": None, "file": b"f",
                               "Bad/": None, ".vcharon-closed-old-1/": None})
        self.lead("docs", where=("--local",))
        out = self.ok("list", "--server", "fake-dest")
        lines = out.splitlines()
        self.assertEqual(lines[0], "vcharon: list  (fake-dest)")
        game = next(l for l in lines if l.startswith("  game  "))
        self.assertIn("  leader lead  members a, lead  newest ", game)
        self.assertIn("    note: notes.md at its top isn't a member's folder", lines)
        self.assertTrue(any(l.startswith("  two  leader ?") for l in lines), out)
        self.assertIn("    note: x/, y/ all hold CHANNEL.md: ask the user", lines)
        self.assertTrue(any(l.startswith("  empty  leader ?  members none  newest -")
                            for l in lines), out)
        self.assertIn("  note: .vcharon-closed-old-1: a closed channel that wasn't deleted", lines)
        self.assertIn("  note: Bad: not a channel's folder", lines)
        self.assertIn("  note: file: not a channel's folder", lines)
        self.assertTrue(any(l.startswith("  docs  leader laptop-ui") for l in lines), out)
        self.assertEqual(self.ok("list", "--local"), out.replace("(fake-dest)",
                                                                 "(this machine)"))


class LeaveCloseTest(ChannelCase):
    """leave and close: their refusals change nothing; their removal takes exactly the
    membership's own files on this box."""

    TEMPLATE = True

    def setUp(self):
        ChannelCase.setUp(self)
        self.from_template(self.two_members)

    def two_members(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # a second member on the same box, in the same channel, and its run's files
        self.ok("join", "game", "--server", "fake-dest", "--role", "b")
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        self.assertEqual(self.run_cli("sync", "game", "--role", "b")[0], 0)

    def box_files(self, box="mac"):
        return sorted(p for p in read_tree(self.homes[box]) if not p.startswith("logs/vcharon.log"))

    def everything(self):
        return self.box_files("mac"), self.box_files("laptop"), self.server_tree()

    def test_leave(self):
        # a watcher ran: its snapshot and lock; the posts made the own folder's post lock
        snapshot = channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web")
        write_tree(self.homes["mac"], {os.path.relpath(snapshot, self.homes["mac"]): b"{}",
                                       os.path.relpath(snapshot, self.homes["mac"]) + ".lock":
                                       b""})
        post_lock = entries.lock_path(os.path.join(self.joined("game.mac-web"),
                                                   "mac-web"))
        self.assertTrue(os.path.isfile(post_lock))
        before = set(self.box_files())
        out = self.ok("leave", "game")
        self.assertIn("OK  left game", out)
        # the LEAVE entry reached the server before the removal
        results = entries.parse_file(os.path.join(self.root, "game", "mac-web",
                                                  "RESULTS.md"))
        self.assertIn("  posted LEAVE %s into RESULTS.md, to @laptop-ui\n" % results[-1].id, out)
        self.assertEqual([(e.title, e.to) for e in results],
                         [("JOIN", ("@laptop-ui",)), ("LEAVE", ("@laptop-ui",))])
        gone = sorted(before - set(self.box_files()))
        self.assertEqual(sorted(set(self.box_files()) - before), [])
        # exactly its own: the tree (which holds its copy of the other members' folders), the
        # section, the record, state, logs, locks
        self.assertTrue(all("mac-web-b" not in p for p in gone
                            if not p.startswith("joined/game.mac-web/")), gone)
        for path in ("channels.d/game.mac-web.ini", "state/channels/game.mac-web.json",
                     "state/game.mac-web.up.json", "state/game.mac-web.down.json",
                     "state/game.mac-web.up.lock", "logs/game.mac-web.up.log",
                     "joined/game.mac-web/", "state/mailbox-watch-game.mac-web.json",
                     "state/mailbox-watch-game.mac-web.json.lock",
                     os.path.relpath(post_lock, self.homes["mac"]).replace(os.sep, "/")):
            self.assertIn(path, gone)
        # one line for each: the tree, and each file outside it
        removed = sorted(os.path.relpath(l[len("  removed "):], self.homes["mac"])
                         .replace(os.sep, "/") for l in out.splitlines()
                         if l.startswith("  removed "))
        self.assertEqual(removed, sorted(["joined/game.mac-web"] +
                                         [p for p in gone if not p.startswith("joined/")]))
        self.assertFalse(any(p.startswith("joined/game.mac-web/") for p in self.box_files()))
        # the second member is untouched and still runs
        for path in ("channels.d/game.mac-web-b.ini", "state/channels/game.mac-web-b.json",
                     "state/game.mac-web-b.up.json", "joined/game.mac-web-b/"):
            self.assertIn(path, self.box_files())
        self.assertEqual(self.run_cli("sync", "game", "--role", "b")[0], 0)
        # the server folder stays: the member's history, its name taken
        self.assertTrue(os.path.isdir(os.path.join(self.root, "game", "mac-web")))
        got, out, err = self.channel("leave", "game")
        self.assertEqual(got, 1)
        # only the role member is left from this project: it names the role (DESIGN, "Which
        # membership")
        self.assertEqual(err.splitlines()[:2], [
            "ERROR channel: you are in game from web only with a role",
            "  fix: pass --role b"])

    def test_refusals_change_nothing(self):
        # leave's: the leader, and a lock held
        locks = (channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web"),
                 os.path.join(self.homes["mac"], "state", "game.mac-web.down"))
        for lock in locks:
            open(lock + ".lock", "ab").close()
        self.use_box("laptop")
        lock = channel_cmd.watcher_snapshot(None, "game.laptop-ui", "laptop-ui") + ".lock"
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        open(lock, "ab").close()
        before = self.everything()
        got, _out, err = self.channel("leave", "game", "--project", "ui")
        self.assertEqual(got, 1)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR channel: you lead game: close it instead",
            "  fix: " + platform.runnable(LEADER_LEAVE_FIX)])
        # text, never a close to run: that would skip CLOSED and the members' DONE
        self.assertNotIn("vcharon close", err)
        # its own watcher running: still close, not "stop the watcher", which would stop the
        # watcher of a channel it still leads
        child = hold(self, lock)
        got, _out, err = self.channel("leave", "game", "--project", "ui")
        self.assertEqual(got, 1)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR channel: you lead game: close it instead",
            "  fix: " + platform.runnable(LEADER_LEAVE_FIX)])
        release(child)
        self.use_box("mac")
        for held in locks:
            with self.subTest(lock=held):
                child = hold(self, held + ".lock")
                line = self.refused("leave", "game")
                self.assertTrue(line.startswith("ERROR channel: %s.lock is held" % held), line)
                self.assertTrue(line.endswith("of mac-web in game)"), line)
                release(child)
        # close's: not the leader; its fix is text too (a member leaves after CLOSED)
        self.assertEqual(self.refusal("close", "game"),
                         ("ERROR channel: only the leader closes game, and that is laptop-ui",
                          platform.runnable("members leave after the leader's CLOSED; how: "
                                            "vcharon guide end")))
        self.use_box("laptop")
        # the watcher lock held: refused before anything on the server changes
        child = hold(self, lock)
        self.assertEqual(self.refusal("close", "game", "--project", "ui")[1],
                         "stop the watcher first, or wait for that command to end")
        release(child)
        # another server than the record's
        os.environ["VCHARON_TEST_MACHINE_ID"] = OTHER_MACHINE
        self.assertEqual(self.refusal("close", "game", "--project", "ui"),
                         ("ERROR channel: fake-dest isn't the server game is on (its machine id "
                          "is %s, the record's %s)" % (OTHER_MACHINE, TEST_MACHINE_ID),
                          "check the alias"))
        os.environ["VCHARON_TEST_MACHINE_ID"] = TEST_MACHINE_ID
        # a second CHANNEL.md, then a stray file at the top: the server refuses
        write_tree(self.root, {"game/mac-web/CHANNEL.md": b"c"})
        self.assertEqual(self.refusal("close", "game", "--project", "ui"),
                         ("ERROR channel: game has 2 leaders (laptop-ui/, mac-web/ hold "
                          "CHANNEL.md)", "ask the user"))
        os.remove(os.path.join(self.root, "game", "mac-web", "CHANNEL.md"))
        write_tree(self.root, {"game/notes.md": b"n"})
        self.assertEqual(self.refusal("close", "game", "--project", "ui"),
                         ("ERROR channel: game holds notes.md at its top, not a member's folder",
                          "ask the user, and remove it first"))
        os.remove(os.path.join(self.root, "game", "notes.md"))
        self.assertEqual(self.everything(), before)

    def test_a_watcher_started_during_the_leave_exits_12(self):
        # leave holds the watcher's lock to its end, through its sync; the lock file goes last
        snapshot = channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web")
        real = channel_cmd._run_section
        started = []

        def sync(args, section, full):
            lines = []
            started.append(watch.watch_job(section, ["game", "--project", "web"], 2,
                                           out=lines.append, sleep=lambda s: None,
                                           run=lambda job, sync_args: (0, None, None),
                                           rounds=1))
            started.append([line[len("2026-10-01 09:05:46 "):] for line in lines])
            return real(args, section, full)

        with mock.patch.object(channel_cmd, "_run_section", sync):
            out = self.ok("leave", "game")
        self.assertEqual(started, [12, [WATCHER_LOCKED % snapshot]])
        self.assertEqual(out.splitlines()[-2:], ["  removed %s.lock" % snapshot, "OK  left game"])
        self.assertFalse(os.path.exists(snapshot + ".lock"))

    def test_a_lock_file_that_wont_go_doesnt_fail_the_leave(self):
        # on Windows a watcher opening the lock file at that instant makes its delete fail
        snapshot = channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web")
        real = os.unlink

        def unlink(path, *a, **kw):
            if path == snapshot + ".lock":
                raise PermissionError(13, "in use", path)
            return real(path, *a, **kw)

        with mock.patch.object(os, "unlink", unlink):
            out = self.ok("leave", "game")
        self.assertEqual(out.splitlines()[-1], "OK  left game")
        self.assertNotIn("  removed %s.lock" % snapshot, out.splitlines())
        self.assertTrue(os.path.exists(snapshot + ".lock"))

    def test_a_failed_leave_releases_the_watcher_lock(self):
        snapshot = channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web")
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            code, _out, _err = self.channel("leave", "game")
        self.assertEqual(code, 4)
        lk = watch.take_lock(snapshot)
        self.assertIsNotNone(lk)
        lk.release()

    def test_close_holds_the_watcher_lock(self):
        self.use_box("laptop")
        snapshot = channel_cmd.watcher_snapshot(None, "game.laptop-ui", "laptop-ui")
        real = channel_cmd._need_machine
        tries = []

        def need_machine(server):
            # mid-close: a watcher's start finds the lock held
            tries.append(watch.take_lock(snapshot))
            return real(server)

        with mock.patch.object(channel_cmd, "_need_machine", need_machine):
            out = self.ok("close", "game", "--project", "ui")
        self.assertEqual(tries, [None])
        self.assertIn("  removed %s.lock" % snapshot, out.splitlines())
        self.assertFalse(os.path.exists(snapshot + ".lock"))

    def test_leave_after_the_channel_is_gone(self):
        # a post running in the own folder: its lock isn't taken from under it (a leave posts
        # no LEAVE once the channel is gone)
        post_lock = entries.lock_path(os.path.join(self.joined("game.mac-web"),
                                                   "mac-web"))
        holder = hold(self, post_lock)
        shutil.rmtree(os.path.join(self.root, "game"))
        out = self.ok("leave", "game")
        self.assertNotIn("  removed %s" % post_lock, out.splitlines())
        self.assertTrue(os.path.isfile(post_lock))
        # the lock's checks are done; Windows refuses to read a locked file, and read_tree
        # below reads every file
        release(holder)
        self.assertFalse(os.path.exists(self.joined("game.mac-web")))
        self.assertIn("  note    game is gone on the server", out)
        self.assertNotIn("channels.d/game.mac-web.ini", self.box_files())
        self.assertFalse(os.path.exists(os.path.join(self.root, "game")))
        # what the guide's end topic tells an agent: nothing of the channel stays here
        # this box's other membership of game is named, since it stays
        self.assertEqual(out.splitlines()[-2:], [
            "  note    nothing of game as mac-web is left on this machine; still here: mac-web-b "
            "(leave each on its own)", "OK  left game"])
        # (this box's other membership, mac-web-b, stays)
        self.assertEqual([p for p in self.box_files() if "game.mac-web" in p
                          and "game.mac-web-b" not in p], [])

    def test_close(self):
        self.use_box("laptop")
        out = self.ok("close", "game", "--project", "ui")
        self.assertIn("OK  closed game", out)
        # close says it too; the laptop box has no other membership of game
        self.assertIn("  note    nothing of game as laptop-ui is left on this machine",
                      out.splitlines())
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse(any("game.laptop-ui" in p for p in self.box_files("laptop")),
                         self.box_files("laptop"))
        # a member's post and run make no channel again: up never creates
        self.use_box("mac")
        own = os.path.join(self.joined("game.mac-web"), "mac-web")
        entries.post(os.path.join(own, "RESULTS.md"), own, "mac-web", "late", ["@laptop-ui"],
                     body="x")
        code, out, err = self.run_cli("sync", "game")
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(self.root), [])
        fixes = [l for l in err.splitlines() if l.startswith("  fix: ")]
        # the leave command as this box runs vcharon
        self.assertEqual(fixes, ["  fix: " + platform.runnable(
            "the channel is closed, or your folder in it is gone: vcharon leave game "
            "--project web")] * 2)
        self.assertNotIn("create it", err)
        # its sign to leave
        out = self.ok("leave", "game")
        self.assertIn("game is gone on the server", out)
        self.assertEqual(os.listdir(self.root), [])

    def test_close_of_a_server_led_channel(self):
        self.lead("docs", where=("--local",), box="linux", project="d")
        self.use_box("linux")
        self.assertEqual(self.refusal("leave", "docs", "--project", "d"),
                         ("ERROR channel: you lead docs: close it instead",
                          platform.runnable(LEADER_LEAVE_FIX)))
        # its watcher ran (server mode, keyed by the channel folder) and it posted
        snapshot = channel_cmd.watcher_snapshot(None, "docs.linux-d", "linux-d",
                                                os.path.join(self.root, "docs"))
        write_tree(self.homes["linux"], {os.path.relpath(snapshot, self.homes["linux"]): b"{}",
                                        os.path.relpath(snapshot, self.homes["linux"]) + ".lock":
                                        b""})
        own = os.path.join(self.root, "docs", "linux-d")
        entries.post(os.path.join(own, "STEPS.md"), own, "linux-d", "steps", ["@all"])
        post_lock = entries.lock_path(own)
        out = self.ok("close", "docs", "--project", "d")
        self.assertIn("OK  closed docs", out)
        self.assertEqual(sorted(os.listdir(self.root)), ["game"])
        record = os.path.join(self.homes["linux"], "state", "channels", "docs.linux-d.json")
        for path in (record, snapshot, snapshot + ".lock", post_lock):
            self.assertFalse(os.path.exists(path), path)
            self.assertIn("  removed %s" % path, out.splitlines())
        # the client-id file stays: it's this machine's, not the channel's
        self.assertEqual(sorted(os.listdir(os.path.join(self.homes["linux"], "state"))),
                         ["channels", "client-id"])

class LeaderOnlyTest(ChannelCase):
    """leave and close with only the leader made: LeaveCloseTest's members aren't needed."""

    def test_local_leave_after_the_channel_is_gone(self):
        # a local member: its server is this machine, not "the server"
        self.lead()
        self.use_box("linux")
        self.ok("join", "game", "--local", "--project", "x")
        shutil.rmtree(os.path.join(self.root, "game"))
        out = self.ok("leave", "game", "--project", "x")
        self.assertIn("  note    game is gone on this machine", out.splitlines())
        self.assertNotIn("on the server", out)
        self.assertFalse(os.path.exists(os.path.join(self.homes["linux"], "state", "channels",
                                                     "game.linux-x.json")))

    def test_close_after_the_channel_is_gone(self):
        for where in (("--server", "fake-dest"), ("--local",)):
            with self.subTest(where=where):
                self.lead(where=where)
                self.use_box("laptop")
                shutil.rmtree(os.path.join(self.root, "game"))
                out = self.ok("close", "game", "--project", "ui")
                lines = out.splitlines()
                self.assertIn("  note    there is no channel game", lines)
                self.assertIn("  note    nothing of game as laptop-ui is left on this machine",
                              lines)
                self.assertEqual(lines[-1], "OK  closed game")
                self.assertEqual([p for p in read_tree(self.homes["laptop"])
                                  if "game.laptop-ui" in p], [])
                self.assertEqual(os.listdir(self.root), [])
                self.use_box("mac")

    def test_a_leader_whose_box_lost_its_record(self):
        self.lead()
        self.use_box("laptop")
        os.remove(os.path.join(self.homes["laptop"], "state", "channels", "game.laptop-ui.json"))
        self.ok("join", "game", "--server", "fake-dest", "--project", "ui", "--rejoin")
        self.assertEqual(self.record("game.laptop-ui")["leader"], "laptop-ui")
        self.ok("close", "game", "--project", "ui")
        self.assertEqual(os.listdir(self.root), [])


class StaleMembershipTest(ChannelCase):
    """A membership kept after its channel ended (no leave after the close), or after its folder
    at the server went: join and create refuse it, leave clears it."""

    def closed_without_leave(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.use_box("laptop")
        self.ok("close", "game", "--project", "ui")
        self.use_box("mac")

    def leave_fix(self, verb):
        return platform.runnable(
            "run vcharon leave game --project web (it posts nothing, and removes this machine's "
            "files of that membership), then %s again" % verb)

    def test_join_after_the_channel_was_made_again(self):
        self.closed_without_leave()
        self.lead(box="linux", project="ui")
        before = self.server_tree()
        self.assertEqual(self.refusal("join", "game", "--server", "fake-dest"), (
            "ERROR channel: your join record of game as mac-web is of an earlier channel: game on "
            "fake-dest has no folder mac-web (the channel was made again, or the folder removed)",
            self.leave_fix("join")))
        # a folder of the name there (made by hand), but another leader than the record's
        write_tree(self.root, {"game/mac-web/": None})
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest"),
                         "ERROR channel: your join record of game as mac-web is of an earlier "
                         "channel: game on fake-dest is led by linux-ui, not laptop-ui (the "
                         "channel was made again)")
        out = self.ok("leave", "game")
        lines = out.splitlines()
        self.assertIn("  note    game on the server is led by linux-ui, not laptop-ui (the "
                      "channel was made again)", lines)
        self.assertIn("  note    nothing of game as mac-web is left on this machine", lines)
        # no LEAVE went into the new channel; nothing of the old membership is left here
        os.rmdir(os.path.join(self.root, "game", "mac-web"))
        self.assertEqual(self.server_tree(), before)
        self.assertEqual([p for p in read_tree(self.homes["mac"]) if "game.mac-web" in p], [])
        # a new member now, led by the new leader
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  claimed game/mac-web; the leader is linux-ui", out.splitlines())
        member = entries.header_of(entries.read_text(
            os.path.join(self.root, "game", "mac-web", "MEMBER.md")), "mac-web")
        self.assertEqual(member["leader"], "linux-ui")

    def test_create_after_a_close_without_leave(self):
        self.closed_without_leave()
        self.assertEqual(self.refusal("create", "game", "--server", "fake-dest"), (
            "ERROR channel: your join record of game as mac-web is of an earlier channel: game is "
            "gone from fake-dest", self.leave_fix("create")))
        self.assertEqual(os.listdir(self.root), [])
        self.ok("leave", "game")
        self.ok("create", "game", "--server", "fake-dest")
        found = entries.parse_file(os.path.join(self.root, "game", "mac-web", "CHANNEL.md"))
        self.assertEqual([(e.name, e.number) for e in found], [("mac-web", 2)])

    def test_a_leader_s_record_of_a_gone_channel(self):
        # the leader's own record: its leave refusal is for a live channel only, or the leave
        # fix below would send it to a close, which can't remove what isn't there
        self.lead()
        shutil.rmtree(os.path.join(self.root, "game"))
        self.use_box("laptop")
        fix = platform.runnable(
            "run vcharon leave game --project ui (it posts nothing, and removes this machine's "
            "files of that membership), then create again")
        self.assertEqual(self.refusal("create", "game", "--server", "fake-dest", "--project",
                                      "ui"),
                         ("ERROR channel: your join record of game as laptop-ui is of an "
                          "earlier channel: game is gone from fake-dest", fix))
        out = self.ok("leave", "game", "--project", "ui")
        self.assertIn("  note    game is gone on the server", out.splitlines())
        self.assertEqual([p for p in read_tree(self.homes["laptop"]) if "game.laptop-ui" in p],
                         [])
        self.ok("create", "game", "--server", "fake-dest", "--project", "ui")

    def test_a_leader_s_record_of_a_channel_made_again(self):
        self.lead()
        shutil.rmtree(os.path.join(self.root, "game"))
        self.lead(box="linux", project="ui")
        before = self.server_tree()
        self.use_box("laptop")
        line, fix = self.refusal("join", "game", "--server", "fake-dest", "--project", "ui")
        self.assertEqual(line, "ERROR channel: your join record of game as laptop-ui is of an "
                         "earlier channel: game on fake-dest has no folder laptop-ui (the "
                         "channel was made again, or the folder removed)")
        self.assertIn(platform.runnable("vcharon leave game --project ui"), fix)
        out = self.ok("leave", "game", "--project", "ui")
        self.assertIn("  note    game on the server has no folder laptop-ui (the channel was "
                      "made again, or the folder removed)", out.splitlines())
        # nothing posted into, or removed from, the new channel
        self.assertEqual(self.server_tree(), before)
        out = self.ok("join", "game", "--server", "fake-dest", "--project", "ui")
        self.assertIn("  claimed game/laptop-ui; the leader is linux-ui", out.splitlines())

    def test_leave_when_the_own_folder_at_the_server_is_gone(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        shutil.rmtree(os.path.join(self.root, "game", "mac-web"))
        before = self.server_tree()
        out = self.ok("leave", "game")
        lines = out.splitlines()
        self.assertIn("  note    game on the server has no folder mac-web (the channel was made "
                      "again, or the folder removed)", lines)
        self.assertEqual(lines[-2:], [
            "  note    nothing of game as mac-web is left on this machine", "OK  left game"])
        self.assertEqual(self.server_tree(), before)
        self.assertEqual([p for p in read_tree(self.homes["mac"]) if "game.mac-web" in p], [])

    def test_a_local_member_s_folder_gone(self):
        self.lead()
        self.use_box("linux")
        self.ok("join", "game", "--local", "--project", "x")
        shutil.rmtree(os.path.join(self.root, "game", "linux-x"))
        out = self.ok("leave", "game", "--project", "x")
        self.assertIn("  note    game on this machine has no folder linux-x (the channel was "
                      "made again, or the folder removed)", out.splitlines())
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["laptop-ui"])
        self.assertEqual(os.listdir(os.path.join(self.homes["linux"], "state", "channels")), [])

    def test_leave_when_the_local_tree_is_gone(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        own = os.path.join(self.joined("game.mac-web"), "mac-web")
        shutil.rmtree(own)
        line, fix = self.refusal("leave", "game")
        self.assertEqual(line, "ERROR not_found: your own folder %s has no MEMBER.md on this "
                         "machine" % own)
        self.assertEqual(fix, platform.runnable(channel_cmd.rejoin_hint("game", "fake-dest",
                                                                        "mac-web")))
        self.assertTrue(os.path.isfile(channel_cmd.record_path("game", "mac-web")))
        self.assertFalse(os.path.exists(own))
        # the rejoin brings it back; the leave then goes as usual
        self.ok("join", "game", "--server", "fake-dest")
        self.ok("leave", "game")
        results = entries.parse_file(os.path.join(self.root, "game", "mac-web", "RESULTS.md"))
        self.assertEqual([e.title for e in results], ["JOIN", "REJOIN", "LEAVE"])

    def test_a_local_member_without_its_member_file(self):
        self.lead()
        self.use_box("linux")
        self.ok("join", "game", "--local", "--project", "x")
        own = os.path.join(self.root, "game", "linux-x")
        os.remove(os.path.join(own, "MEMBER.md"))
        out = self.ok("leave", "game", "--project", "x")
        self.assertIn("  note    %s has no MEMBER.md: no LEAVE posted" % own, out.splitlines())
        results = entries.parse_file(os.path.join(own, "RESULTS.md"))
        self.assertEqual([e.title for e in results], ["JOIN"])
        self.assertEqual(os.listdir(os.path.join(self.homes["linux"], "state", "channels")), [])


class PostSendTest(ChannelCase):
    """A remote member's post goes to the server at once (its up job), unless --no-sync; a
    failed send never fails the post."""

    TEMPLATE = True

    def setUp(self):
        ChannelCase.setUp(self)
        self.from_template(self.joined_game)
        self.srv = os.path.join(self.root, "game", "mac-web", "RESULTS.md")

    def joined_game(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")

    def post(self, *extra):
        return self.run_cli("post", "game", "--to", "@laptop-ui", "--title", "done", "--body",
                            "b", *extra)

    def titles(self):
        return [e.title for e in entries.parse_file(self.srv)] if os.path.exists(self.srv) \
            else []

    def test_sent_at_once_or_by_the_next_sync(self):
        code, out, err = self.post("--no-sync")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 1)
        self.assertNotIn("done", self.titles())
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        self.assertEqual(self.titles().count("done"), 1)
        code, out, err = self.post()
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertRegex(lines[0], r"\Aposted mac-web#\d+ — done to mac-web/RESULTS.md at ")
        self.assertEqual(lines[1:], ["sent to fake-dest"])
        self.assertEqual(self.titles().count("done"), 2)

    def test_a_failed_send_warns(self):
        os.environ.update(FAKE_SSH_EXIT="255", FAKE_SSH_STDERR="kex_exchange_identification: "
                          "read: Connection reset by peer\n")
        code, out, err = self.post()
        del os.environ["FAKE_SSH_EXIT"], os.environ["FAKE_SSH_STDERR"]
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1)
        lines = err.splitlines()
        self.assertTrue(lines[0].startswith("WARN not sent to fake-dest: "), lines)
        self.assertIn("Connection reset by peer", lines[0])
        self.assertEqual(lines[1], platform.runnable(
            "  fix: the entry is saved in your folder; your watcher sends it, or once the "
            "server answers, run: vcharon sync game --project web"))
        # saved here, and the next sync sends it
        self.assertNotIn("done", self.titles())
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        self.assertIn("done", self.titles())

    def test_a_running_sync_sends_it(self):
        # a watcher's round holds the up job's lock: no second sync, a note
        held = state.lock("game.mac-web.up")
        try:
            code, out, err = self.post()
        finally:
            held.release()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(err, "note: a sync of game is running (your watcher's): it sends the "
                              "entry\n")

    def test_a_closed_channel_gives_the_leave_fix(self):
        # the server's channel is gone: no later sync can send the entry, so the WARN's fix is
        # the job's own, never "your watcher sends it"
        shutil.rmtree(os.path.join(self.root, "game"))
        code, out, err = self.post()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1)
        lines = err.splitlines()
        self.assertTrue(lines[0].startswith("WARN not sent to fake-dest: not_found: "), lines)
        self.assertEqual(lines[1:], [
            platform.runnable("  fix: the entry is saved in your folder, but no sync sends it "
                              "until: " + channel_cmd.CHANNEL_GONE_HINT
                              % ("game", "--project web")),
            "  log: " + os.path.join(os.environ["VCHARON_HOME"], "logs", "game.mac-web.up.log")])
        self.assertNotIn("your watcher sends it", err)

    def test_a_short_lived_failure_waits_for_the_next_sync(self):
        # a file changed during the send, or an aborted run: the next round sends the entry,
        # so the fix never makes "run again" or the log's pointer a condition for sending
        later = platform.runnable(
            "  fix: the entry is saved in your folder; your watcher sends it, or once the "
            "server answers, run: vcharon sync game --project web")
        for error in (VCharonError("vanished", "x changed while it was being listed",
                                   "run again"),
                      VCharonError("aborted", "the run stopped")):
            with self.subTest(code=error.code), \
                    mock.patch("vcharon.cli.run_jobs", side_effect=error):
                code, out, err = self.post()
                self.assertEqual(code, 0, err)
                self.assertEqual(len(out.splitlines()), 1)
                lines = err.splitlines()
                self.assertTrue(lines[0].startswith("WARN not sent to fake-dest: %s: "
                                                    % error.code), lines)
                self.assertIn(later, lines)
                self.assertNotIn("no sync sends it", err)
                self.assertNotIn("log has the rest", err)

    def test_an_error_with_only_a_log_waits_for_the_next_sync(self):
        # an error with no fix of its own but a log: the later-send fix, and the log on its own
        # line, never the log's pointer as a condition for sending
        log = os.path.join(self.tmp, "game.mac-web.up.log")

        def failed(_fn, _run):
            print("ERROR aborted: the run stopped\n  log: " + log, file=sys.stderr)
            return 1

        record = {"channel": "game", "name": "mac-web", "ssh": "fake-dest"}
        err = io.StringIO()
        with mock.patch("vcharon.cli._guarded", failed), contextlib.redirect_stderr(err):
            cli._send_post("game", record, ["--project", "web"])
        self.assertEqual(err.getvalue().splitlines(), [
            "WARN not sent to fake-dest: aborted: the run stopped",
            platform.runnable("  fix: the entry is saved in your folder; your watcher sends it, "
                              "or once the server answers, run: vcharon sync game --project "
                              "web"),
            "  log: " + log])

    def test_a_local_member_after_the_close(self):
        # the leader closed the channel: its folder is gone, and only a leave helps
        self.lead("local", where=("--local",), box="pc")
        self.ok("join", "local", "--local")
        self.use_box("pc")
        self.ok("close", "local", "--project", "ui")
        self.use_box("mac")
        gone = os.path.join(self.root, "local")
        for argv in (["read", "local"], ["read", "local", "--json"],
                     ["post", "local", "--to", "@pc-ui", "--title", "t", "--body", "b"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.refusal(*argv), (
                    "ERROR not_found: the channel folder %s is gone" % gone,
                    platform.runnable(channel_cmd.CHANNEL_GONE_HINT
                                      % ("local", "--project web"))))

    def test_a_local_member_s_own_folder_gone(self):
        # join refuses the record of a folder the channel no longer has, so post and the
        # watcher give the leave fix too, never a rejoin that join would refuse
        self.lead("local", where=("--local",), box="pc")
        self.ok("join", "local", "--local")
        own = os.path.join(self.root, "local", "mac-web")
        shutil.rmtree(own)
        fix = platform.runnable(channel_cmd.CHANNEL_GONE_HINT % ("local", "--project web"))
        for argv in (["post", "local", "--to", "@pc-ui", "--title", "t", "--body", "b"],
                     ["watch", "local"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.refusal(*argv), (
                    "ERROR not_found: your folder %s in the channel is gone" % own, fix))
        line, join_fix = self.refusal("join", "local", "--local")
        self.assertIn("is of an earlier channel", line)
        self.assertIn(platform.runnable("vcharon leave local --project web"), join_fix)
        self.assertFalse(os.path.exists(own))

    @unittest.skipIf(os.name == "nt", "a folder's search permission is POSIX's")
    def test_a_local_member_s_unsearchable_parent_is_not_gone(self):
        # only a channel folder that isn't there is gone: a parent the stat can't search is
        # its own error, never the leave fix that would drop the membership
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root searches any folder")
        self.lead("local", where=("--local",), box="pc")
        self.ok("join", "local", "--local")
        os.chmod(self.root, 0o600)
        try:
            for argv in (["read", "local"],
                         ["post", "local", "--to", "@pc-ui", "--title", "t", "--body", "b"]):
                with self.subTest(argv=argv):
                    line, fix = self.refusal(*argv)
                    self.assertTrue(line.startswith("ERROR permission: "), line)
                    self.assertNotIn("is gone", line)
                    self.assertNotIn("leave", fix or "")
        finally:
            os.chmod(self.root, 0o700)

    def test_a_local_member_has_nothing_to_send(self):
        self.lead("local", where=("--local",), box="pc")
        self.ok("join", "local", "--local")
        code, out, err = self.run_cli("post", "local", "--to", "@pc-ui", "--title", "t",
                                      "--body", "b")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 1)
        code, out, err = self.run_cli("post", "local", "--to", "@pc-ui", "--title", "t",
                                      "--body", "b", "--no-sync")
        self.assertEqual(code, 3)
        self.assertIn("--no-sync is for a remote member", err)


class PostHostWarnTest(ChannelCase):
    """A post whose title or body names an ssh alias or host name this machine's vcharon uses
    gets a WARN with a fix line; the entry posts all the same, exit 0."""

    TEMPLATE = True

    def setUp(self):
        ChannelCase.setUp(self)
        self.from_template(self.joined_game)

    def joined_game(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")

    def warned(self, title="done", body="b"):
        """(the ID, the stderr lines) of a post that must succeed."""
        code, out, err = self.run_cli("post", "game", "--to", "@laptop-ui", "--title", title,
                                      "--body", body, "--no-sync")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"\Aposted mac-web#\d+ — ")
        return out.split()[1], err.splitlines()

    def test_an_alias_in_the_body_or_the_title(self):
        for title, body in (("done", "sent to fake-dest and back"),
                            ("ran --server fake-dest", "b"),
                            ("done", "line one\nfake-dest."),
                            ("done", "已发送到fake-dest。")):
            with self.subTest(title=title, body=body):
                id_, lines = self.warned(title, body)
                self.assertEqual(lines, [
                    "WARN entry %s names fake-dest: an ssh alias or host name this machine "
                    "uses; the rules keep ssh aliases and host names out of the channel" % id_,
                    "  fix: if that is the ssh alias, post a correction entry without it, "
                    "with --re %s; if it names something else here, nothing to do" % id_])
        # the entries are written all the same
        titles = [e.title for e in entries.parse_file(
            os.path.join(self.joined("game.mac-web"), "mac-web", "RESULTS.md"))]
        self.assertIn("ran --server fake-dest", titles)

    def test_case_is_ignored(self):
        _id, lines = self.warned(body="see FAKE-Dest")
        self.assertEqual(len(lines), 2)
        self.assertIn(" names fake-dest: ", lines[0])

    def test_only_a_whole_word(self):
        for body in ("fake-dest2", "my-fake-dest", "fake-dest-2", "fake-dest.example.com",
                     "x.fake-dest", "fake-dest_x", "fake", "fakedest"):
            with self.subTest(body=body):
                self.assertEqual(self.warned(body=body)[1], [])
        for body in ("me@fake-dest:22", "(fake-dest)", "`fake-dest`", "ssh://fake-dest/"):
            with self.subTest(body=body):
                self.assertEqual(len(self.warned(body=body)[1]), 2)
        # a word ends at a character that isn't ASCII: Chinese text runs on with no space
        hosts = {"gugu"}
        for text in ("已发送到gugu。", "gugu的日志", "GUGU，"):
            with self.subTest(text=text):
                self.assertEqual(cli.hosts_in(text, hosts), ["gugu"])
        for text in ("x.gugu", "gugu-2", "gugu.example.com", "win-gugu#3", "gugu_x"):
            with self.subTest(text=text):
                self.assertEqual(cli.hosts_in(text, hosts), [])

    def test_names_the_channel_has_anyway(self):
        # an alias that is also the channel's name, a box, project or role in a member's name,
        # or this box: entries name those anyway
        hosts = {"game", "laptop", "UI", "web", "mac", "mac-web", "devbox"}
        with mock.patch("vcharon.cli._ssh_hosts", return_value=hosts):
            _id, lines = self.warned(body="game laptop ui web mac mac-web devbox")
        self.assertEqual(len(lines), 2)
        self.assertIn(" names devbox: ", lines[0])

    def test_another_channel_s_names_still_warn(self):
        # another channel's membership on this machine, on server devbox from project devbox:
        # its project and name are never in this channel's entries, so devbox still warns
        self.write_record("other", "mac-devbox", "laptop-devbox", ssh="devbox",
                          project="devbox", role="devbox")
        _id, lines = self.warned(body="see devbox")
        self.assertEqual(len(lines), 2)
        self.assertIn(" names devbox: ", lines[0])

    def test_an_unreadable_record_never_breaks_the_post(self):
        # a record that can't be read, sorted before the good one: only its own name is left
        # out, and the good record's alias still warns
        records = os.path.join(os.environ["VCHARON_HOME"], "state", "channels")
        with open(os.path.join(records, "aaa.mac-web.json"), "wb") as f:
            f.write(b"{not json")
        self.assertEqual([r["name"] for r in channel_cmd.records(skip_unreadable=True)],
                         ["mac-web"])
        with self.assertRaises(VCharonError):
            channel_cmd.records()
        with mock.patch("vcharon.cli._ssh_hosts", wraps=cli._ssh_hosts) as seen:
            _id, lines = self.warned(body="fake-dest")
        self.assertEqual(len(lines), 2)
        self.assertEqual([r["name"] for r in seen.call_args.args[1]], ["mac-web"])
        with mock.patch("os.listdir", side_effect=PermissionError("denied")):
            self.assertEqual(channel_cmd.records(skip_unreadable=True), [])

    def test_a_local_member_with_no_aliases(self):
        # no remote membership and no section with ssh on this box: nothing to warn about
        self.use_box("solo")
        self.lead("local", where=("--local",), box="pc")
        self.use_box("solo")
        self.ok("join", "local", "--local")
        code, out, err = self.run_cli("post", "local", "--to", "@pc-ui", "--title",
                                      "fake-dest", "--body", "sent to fake-dest")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 1)

    def test_the_host_of_each_destination(self):
        jobs = {str(i): mock.Mock(ssh=d) for i, d in enumerate(
            ["me@devbox", "ssh://me@server.example.com:2222", "[::1]:22", "x", "lab:2200",
             "DevBox"])}
        hosts = cli._ssh_hosts(mock.Mock(jobs=jobs), [])
        # one of each name whatever its case
        self.assertEqual({h.casefold() for h in hosts},
                         {"devbox", "server.example.com", "::1", "lab"})
        self.assertEqual(len(hosts), 4)
        self.assertEqual(cli.hosts_in("on Server.Example.com.", hosts), ["server.example.com"])
        self.assertEqual(cli.hosts_in("devbox2 and server.example.community", hosts), [])


class SkippedTest(ChannelCase):
    """channels.d/ files that are broken, through the commands."""

    PUSH = textwrap.dedent("""\
        [vcharon]
        box = mac

        [push]
        ssh       = fake-dest
        from      = local:path
        from.path = {src}
        to        = remote:dir
        to.path   = inbox
        to.create = yes
        """)

    def setUp(self):
        ChannelCase.setUp(self)
        util.use_test_jobs(self)
        self.src = os.path.join(self.tmp, "src.txt")
        with open(self.src, "w") as f:
            f.write("hi")
        with open(os.path.join(self.vcharon_home, "vcharon.ini"), "w") as f:
            f.write(self.PUSH.format(src=self.src))
        write_tree(self.vcharon_home, {"channels.d/game.mac-x.ini": b"[game.mac-x]\nssh = \n"})

    def vcharon_log(self):
        with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"), encoding="utf-8") as f:
            return f.read()

    def test_other_jobs_run(self):
        code, _out, err = self.run_jobs("push")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertIn("skipped channels.d/game.mac-x.ini: [game.mac-x]: holds only a channel "
                      "section, with mailbox keys", self.vcharon_log())

    def test_naming_one_fails_with_its_error(self):
        code, _out, err = self.run_jobs("game.mac-x.down")
        self.assertEqual(code, 3)
        self.assertTrue(err.startswith("ERROR config: channels.d/game.mac-x.ini [game.mac-x]: "
                                       "holds only a channel section"), err)
        self.assertIn("  fix: fix %s, or delete it" % os.path.join(
            self.vcharon_home, "channels.d", "game.mac-x.ini"), err)
        # through the command line: the membership's record finds the broken section
        self.write_record("game", "mac-x", "laptop-ui", project="x")
        for argv in (["sync", "game"], ["watch", "game"], ["read", "game"]):
            with self.subTest(argv=argv):
                code, _out, err = self.run_cli(*argv + ["--project", "x"])
                self.assertEqual(code, 3)
                self.assertTrue(err.startswith("ERROR config: channels.d/game.mac-x.ini "
                                               "[game.mac-x]: holds only a channel section"), err)

    def test_doctor_lists_them(self):
        os.environ.pop("SSH_AUTH_SOCK", None)
        code, out, _err = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        warns = [" ".join(l.split()) for l in out.splitlines() if l.startswith("  warn  config")]
        self.assertEqual([w.split(":")[0] for w in warns],
                         ["warn config skipped channels.d/game.mac-x.ini"])


class ReviewTest(ChannelCase):
    """Probes of the channel commands as tests, and tests that kill mutants of them that
    other tests let survive."""

    TEMPLATE = True

    def member(self):
        """The leader laptop-ui (remote), and this box's member mac-web with three entries
        and a patch, all sent. Only as a test's first step: it may put back a copy of the
        temp folder."""
        self.local = self.joined("game.mac-web")
        self.own = os.path.join(self.local, "mac-web")
        self.from_template(self.build_member)
        self.srv_own = os.path.join(self.root, "game", "mac-web")
        self.srv_before = read_tree(self.srv_own)
        self.assertEqual(sorted(self.srv_before), ["MEMBER.md", "RESULTS.md", "work.patch"])

    def build_member(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        for i in range(3):
            entries.post(os.path.join(self.own, "RESULTS.md"), self.own, "mac-web",
                         "step %d" % i, ["@laptop-ui"], body="b")
        with open(os.path.join(self.own, "work.patch"), "w") as f:
            f.write("patch")
        self.assertEqual(self.run_cli("sync", "game")[0], 0)

    # --- the rejoin pull ---

    def test_a_failed_pull_leaves_no_own_folder(self):
        self.member()
        shutil.rmtree(self.local)
        lost = VCharonError("lost", "the connection closed (test)")
        with mock.patch.object(channel_cmd.engine.Engine, "run", side_effect=lost):
            code, out, err = self.channel("join", "game", "--server", "fake-dest")
        self.assertEqual(code, 1, err)
        self.assertFalse(os.path.exists(self.own))
        base = os.path.join(self.homes["mac"], "joined")
        self.assertEqual([f for f in os.listdir(base) if f.startswith(".vcharon-pull-")], [])
        # the next plain run refuses (up has sent files from the missing folder), and the
        # server's copy stays whole
        code, out, err = self.run_cli("sync", "game")
        self.assertEqual(code, 1)
        self.assertIn("not_found: the mailbox's own folder %s is gone" % self.own, err)
        self.assertEqual(read_tree(self.srv_own), self.srv_before)
        # a join that works takes it all back
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  pulled your folder from the server: 3 files added, 0 already here",
                      out)
        self.assertEqual(read_tree(self.srv_own)["work.patch"], b"patch")
        self.assertEqual(sorted(read_tree(self.own)), ["MEMBER.md", "RESULTS.md", "work.patch"])

    def test_a_stray_file_doesnt_skip_the_pull(self):
        self.member()
        shutil.rmtree(self.own)
        write_tree(self.own, {".DS_Store": b"x"})
        # a plain run first: MEMBER.md is gone while up has sent it, so it refuses
        code, out, err = self.run_cli("sync", "game")
        self.assertEqual(code, 1)
        self.assertIn("has no MEMBER.md, which game.mac-web.up has sent", err)
        self.assertEqual(read_tree(self.srv_own), self.srv_before)
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  pulled your folder from the server: 3 files added, 0 already here",
                      out)
        # nothing at the server was pruned (RESULTS.md only gained the REJOIN at its end); the
        # stray stays here (and is sent up)
        for name, data in self.srv_before.items():
            got = read_tree(self.srv_own)[name]
            if name == "RESULTS.md":
                self.assertTrue(got.startswith(data))
                self.assertEqual(entries.parse(got.decode())[-1].title, "REJOIN")
            else:
                self.assertEqual(got, data)
        self.assertEqual(read_tree(self.own)[".DS_Store"], b"x")

    def test_needs_pull_goes_by_ups_state(self):
        self.member()
        self.assertFalse(channel_cmd.needs_pull("game.mac-web", self.own))
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertNotIn("pulled", out)
        # a file deleted on purpose, MEMBER.md still here. Every new
        # session rejoins, so the rejoin must not bring it back; its run deletes it at the
        # server
        os.remove(os.path.join(self.own, "work.patch"))
        self.assertFalse(channel_cmd.needs_pull("game.mac-web", self.own))
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertNotIn("pulled", out)
        self.assertFalse(os.path.exists(os.path.join(self.own, "work.patch")))
        self.assertNotIn("work.patch", os.listdir(self.srv_own))
        # MEMBER.md gone while up has sent it: the tree was lost, so pull; a pull only adds,
        # so a file here is never replaced by the server's
        os.remove(os.path.join(self.own, "MEMBER.md"))
        with open(os.path.join(self.own, "RESULTS.md"), "a") as f:
            f.write("local edit\n")
        self.assertTrue(channel_cmd.needs_pull("game.mac-web", self.own))
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  pulled your folder from the server: 1 files added, 1 already here",
                      out)
        self.assertIn("local edit", read_tree(self.own)["RESULTS.md"].decode())
        self.assertTrue(os.path.isfile(os.path.join(self.own, "MEMBER.md")))
        # no up state: always pull
        self.run_cli("sync", "game", "--reset", "up")
        self.assertTrue(channel_cmd.needs_pull("game.mac-web", self.own))

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_the_pull_never_writes_through_a_link(self):
        # own/sub replaced by a link to a folder outside the tree
        self.member()
        write_tree(self.own, {"sub/a.txt": b"a", "sub/b.txt": b"b"})
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        shutil.rmtree(os.path.join(self.own, "sub"))
        os.symlink(outside, os.path.join(self.own, "sub"))
        os.remove(os.path.join(self.own, "MEMBER.md"))
        code, out, err = self.channel("join", "game", "--server", "fake-dest")
        self.assertEqual(code, 1, out)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR unsafe_path: %s is a symlink: the pull into your own folder doesn't go "
            "through it" % os.path.join(self.own, "sub"), "  fix: " + channel_cmd.LINK_HINT])
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.local))
                          if f.startswith(".vcharon-pull-")], [])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "needs POSIX modes, not root")
    def test_a_failed_move_is_a_plain_error(self):
        # a folder of the own tree that can't be written
        self.member()
        write_tree(self.own, {"sub/x.txt": b"x"})
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        os.remove(os.path.join(self.own, "sub", "x.txt"))
        os.remove(os.path.join(self.own, "MEMBER.md"))
        sub = os.path.join(self.own, "sub")
        os.chmod(sub, 0o555)
        self.addCleanup(os.chmod, sub, 0o755)
        code, out, err = self.channel("join", "game", "--server", "fake-dest")
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("ERROR permission: %s: " % sub), err)
        # what moved before the failure stays (MEMBER.md sorts before sub/)
        self.assertTrue(os.path.isfile(os.path.join(self.own, "MEMBER.md")))
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.local))
                          if f.startswith(".vcharon-pull-")], [])

    def test_hints_rebuild_a_role_members_name(self):
        # the -b member's fix line must not claim mac-web
        self.lead()
        self.ok("join", "game", "--server", "fake-dest", "--role", "b")
        own = os.path.join(self.joined("game.mac-web-b"), "mac-web-b")
        self.assertEqual(self.run_cli("sync", "game", "--role", "b")[0], 0)
        self.assertEqual((self.record("game.mac-web-b")["project"],
                          self.record("game.mac-web-b")["role"]), ("web", "b"))
        os.remove(os.path.join(own, "MEMBER.md"))
        _code, out, err = self.run_cli("sync", "game", "--role", "b")
        fix = next(l for l in err.splitlines() if l.startswith("  fix: "))
        self.assertEqual(fix, "  fix: " + platform.runnable(
            "vcharon join game --server fake-dest --project web --role b takes its files "
            "back from the server (a rejoin); MEMBER.md is vcharon's: to drop other files, delete "
            "them one by one and keep it"))
        # followed as printed: it takes back mac-web-b, and claims nothing new
        command = fix.split("fix: ", 1)[1]
        self.assertTrue(command.startswith(platform.self_command() + " join "), command)
        argv = command[len(platform.self_command()):].split(" takes ")[0].split()
        out = self.ok(*argv)
        self.assertIn("  took back game/mac-web-b", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "mac-web")))
        self.assertEqual(channel_cmd.name_flags("game", "mac-web-b"),
                         "--project web --role b")
        # the gone-channel hint too
        shutil.rmtree(os.path.join(self.root, "game"))
        _code, out, err = self.run_cli("sync", "game", "--role", "b")
        self.assertIn(" fix: %s\n" % platform.runnable(
            channel_cmd.CHANNEL_GONE_HINT % ("game", "--project web --role b")), err)

    # --- leave: the server checked first, one entry ---

    def test_leave_checks_the_server_first(self):
        self.member()
        other = os.path.join(self.tmp, "other-root")
        os.makedirs(other)
        with mock.patch.dict(os.environ, {"VCHARON_TEST_MACHINE_ID": OTHER_MACHINE,
                                          "VCHARON_CHANNELS_ROOT": other}):
            self.assertEqual(self.refusal("leave", "game"),
                             ("ERROR channel: fake-dest isn't the server game is on (its machine "
                              "id is %s, the record's %s)" % (OTHER_MACHINE, TEST_MACHINE_ID),
                              "check the alias"))
        self.assertTrue(os.path.isdir(self.local))
        self.assertTrue(os.path.exists(channel_cmd.record_path("game", "mac-web")))
        self.assertEqual(os.listdir(other), [])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "needs POSIX modes, not root")
    def test_leave_with_an_unreadable_channel(self):
        self.member()
        ch = os.path.join(self.root, "game")
        os.chmod(ch, 0)
        self.addCleanup(os.chmod, ch, 0o755)
        line = self.refused("leave", "game")
        os.chmod(ch, 0o755)
        self.assertTrue(line.startswith("ERROR channel: game on fake-dest isn't usable as a "
                                        "channel (can't be read: "), line)
        self.assertTrue(os.path.isdir(self.local))
        self.assertTrue(os.path.exists(channel_cmd.record_path("game", "mac-web")))

    def test_an_enoent_inside_remove_isnt_a_gone_channel(self):
        self.lead("docs", where=("--local",), box="linux", project="d")
        write_tree(self.root, {"docs/x/MEMBER.md": b"m"})
        self.use_box("linux")
        real = channels._DIR.enter

        def enter(handle, name, owner_rule=True):
            if name == "x":
                raise FileNotFoundError(2, "No such file or directory", name)
            return real(handle, name, owner_rule)

        with mock.patch.object(channels._DIR, "enter", enter):
            line = self.refused("close", "docs", "--project", "d")
        self.assertTrue(line.startswith("ERROR vanished: "), line)
        self.assertTrue(os.path.exists(channel_cmd.record_path("docs", "linux-d")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "docs", "linux-d")))

    def test_one_leave_entry_however_many_tries(self):
        self.member()
        before = LeaveCloseTest.box_files(self)
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            for attempt in range(2):
                code, out, _err = self.channel("leave", "game")
                self.assertEqual(code, 4)
                self.assertIn("nothing was removed", out)
        # a failed run removes nothing: only the LEAVE entry was written
        self.assertEqual(LeaveCloseTest.box_files(self), before)
        titles = [e.title for e in entries.parse_file(os.path.join(self.own, "RESULTS.md"))]
        self.assertEqual(titles.count("LEAVE"), 1)
        self.ok("leave", "game")
        titles = [e.title for e in entries.parse_file(os.path.join(self.srv_own, "RESULTS.md"))]
        self.assertEqual(titles.count("LEAVE"), 1)

    # --- another member's copy, the lock, removing a folder in use ---

    def test_post_into_another_members_copy(self):
        # the command line posts into the member's own folder only; post() itself still
        # refuses another member's copy, and a name that isn't the folder's
        self.member()
        target = os.path.join(self.local, "laptop-ui", "STEPS.md")
        with self.assertRaises(VCharonError) as cm:
            post_mod.post(target, "mac-web", ["@all"], "forged", body="x")
        self.assertIn("that is laptop-ui's folder, not yours", cm.exception.message)
        self.assertEqual(cm.exception.hint, "post into your own folder, mac-web/")
        self.assertFalse(os.path.exists(target))
        code, out, err = self.run_cli("post", "game", "--to", "@laptop-ui", "--title", "mine",
                                      "--body", "x")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("posted mac-web#"), out)
        with self.assertRaises(VCharonError) as cm:
            post_mod.post(os.path.join(self.own, "RESULTS.md"), "laptop-ui", ["@laptop-ui"],
                          "mine", body="x")
        self.assertIn("is mac-web's folder, not laptop-ui's", cm.exception.message)
        # the leader's plan is the leader's: a member's --steps is refused
        code, out, err = self.run_cli("post", "game", "--to", "@all", "--title", "plan",
                                      "--body", "x", "--steps")
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR channel: STEPS.md is the leader's (laptop-ui): its plan",
            "  fix: leave out --steps: your entries go into RESULTS.md"])
        self.assertFalse(os.path.exists(os.path.join(self.own, "STEPS.md")))

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_one_lock_for_two_spellings(self):
        local = os.path.join(self.tmp, "joined", "game.mac-web")
        os.makedirs(os.path.join(local, "mac-web"))
        alias = os.path.join(self.tmp, "alias")
        os.symlink(local, alias)
        self.assertEqual(entries.lock_path(os.path.join(local, "mac-web")),
                         entries.lock_path(os.path.join(alias, "mac-web")))

    def test_remove_retries_a_rename_in_use(self):
        # on Windows a watcher scanning the channel holds a handle, and the rename fails for
        # a moment: winerror 32 or 5 (faked here; 5 was measured on Windows)
        for codes in ((32, 32), (5, 32), (5, 5)):
            with self.subTest(codes=codes):
                write_tree(self.root, {"game/lead/CHANNEL.md": b"c", "game/a/MEMBER.md": b"m"})
                real = channels._DIR.rename
                calls = []

                def rename(handle, old, new, calls=calls, codes=codes, real=real):
                    calls.append(old)
                    if len(calls) <= len(codes):
                        e = OSError(13, "in use")
                        e.winerror = codes[len(calls) - 1]
                        raise e
                    return real(handle, old, new)

                with mock.patch.object(channels._DIR, "rename", rename), \
                        mock.patch.object(channels.fsops, "RETRY_DELAY", 0):
                    channels.remove(self.root, "game", "lead")
                self.assertEqual(calls, ["game"] * 3)
                self.assertEqual(os.listdir(self.root), [])

    def test_remove_gives_up_on_another_error_or_after_the_retries(self):
        write_tree(self.root, {"game/lead/CHANNEL.md": b"c"})
        for winerror, calls_made in ((2, 1), (5, 1 + channels.fsops.RETRIES)):
            with self.subTest(winerror=winerror):
                calls = []

                def rename(handle, old, new, calls=calls, winerror=winerror):
                    calls.append(old)
                    e = OSError(13, "denied")
                    e.winerror = winerror
                    raise e

                with (mock.patch.object(channels._DIR, "rename", rename),
                      mock.patch.object(channels.fsops, "RETRY_DELAY", 0),
                      self.assertRaises(VCharonError)):
                    channels.remove(self.root, "game", "lead")
                self.assertEqual(len(calls), calls_made)
                self.assertTrue(os.path.isdir(os.path.join(self.root, "game", "lead")))

    def test_remove_names_what_holds_the_folder(self):
        # measured on Windows (2026-10-02): a held folder fails the rename with winerror 5 for
        # longer than the retry; close refuses cleanly, with a fix of its own
        write_tree(self.root, {"game/lead/CHANNEL.md": b"c", "game/a/MEMBER.md": b"m"})
        before = read_tree(self.root)
        path = os.path.join(self.root, "game")
        for winerror, code, message in ((5, "permission", "%s: access denied" % path),
                                        (32, "in_use", "%s: another program has it open"
                                         % path)):
            with self.subTest(winerror=winerror):
                def rename(handle, old, new, winerror=winerror):
                    e = PermissionError(13, "拒绝访问。")
                    e.winerror = winerror
                    raise e

                with (mock.patch.object(channels._DIR, "rename", rename),
                      mock.patch.object(channels.fsops, "RETRY_DELAY", 0),
                      self.assertRaises(VCharonError) as cm):
                    channels.remove(self.root, "game", "lead")
                self.assertEqual((cm.exception.code, cm.exception.message), (code, message))
                self.assertEqual(cm.exception.hint,
                                 "something has a file or its current folder inside %s (a "
                                 "shell, an editor, Explorer, or a program a shell started): "
                                 "close it or leave that folder, then run close again" % path)
                self.assertEqual(read_tree(self.root), before)
        # any other rename error keeps fsops' own fix
        def denied(handle, old, new):
            raise PermissionError(13, "Permission denied")

        with (mock.patch.object(channels._DIR, "rename", denied),
              self.assertRaises(VCharonError) as cm):
            channels.remove(self.root, "game", "lead")
        self.assertEqual(cm.exception.hint, "check the owner and permissions of %s" % path)
        self.assertEqual(read_tree(self.root), before)

    # --- create, join, leave and remove at their edges ---

    def test_remove_refuses_a_non_leader(self):
        write_tree(self.root, {"game/lead/CHANNEL.md": b"c", "game/a/MEMBER.md": b"m"})
        with self.assertRaises(VCharonError) as cm:
            channels.remove(self.root, "game", "a")
        self.assertEqual(cm.exception.code, "channel")
        self.assertIn("only the channel's leader closes it", cm.exception.message)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["a", "lead"])

    def test_remove_refuses_a_folder_with_another_name(self):
        for stray in ("Bad", ".git"):
            with self.subTest(stray=stray):
                write_tree(self.root, {"game/lead/CHANNEL.md": b"c", stray + "/x": b"x"})
                os.makedirs(os.path.join(self.root, "game", stray))
                with self.assertRaises(VCharonError) as cm:
                    channels.remove(self.root, "game", "lead")
                self.assertIn("%s/ at its top, not a member's folder" % stray,
                              cm.exception.message)
                self.assertTrue(os.path.isdir(os.path.join(self.root, "game", "lead")))
                os.rmdir(os.path.join(self.root, "game", stray))

    def test_a_failed_create_releases_the_claim(self):
        self.use_box("laptop")
        # the local tree can't be made: after the claim and the record
        write_tree(self.homes["laptop"], {"joined": b"a file in the way"})
        code, _out, _err = self.channel("create", "game", "--server", "fake-dest",
                                        "--project", "ui")
        self.assertNotEqual(code, 0)
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse(os.path.exists(channel_cmd.record_path("game", "laptop-ui")))

    def test_a_claim_that_fails_leaves_no_channel(self):
        real = channels._DIR.mkdir

        def mkdir(handle, name, mode=0o777):
            if name == "lead":
                raise PermissionError(13, "Permission denied", name)
            return real(handle, name, mode)

        with (mock.patch.object(channels._DIR, "mkdir", mkdir),
              self.assertRaises(VCharonError) as cm):
            channels.claim(self.root, "game", "lead", True)
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual(os.listdir(self.root), [])

    def test_a_failed_rejoin_keeps_the_folder(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # the server folder holds only MEMBER.md: what release would take (join sent its JOIN
        # too; removed here, so the folder is releasable)
        srv = os.path.join(self.root, "game", "mac-web")
        os.remove(os.path.join(srv, "RESULTS.md"))
        self.assertEqual(os.listdir(srv), ["MEMBER.md"])
        section = os.path.join(self.homes["mac"], "channels.d", "game.mac-web.ini")
        os.remove(section)
        os.mkdir(section)
        code, _out, _err = self.channel("join", "game", "--server", "fake-dest")
        self.assertNotEqual(code, 0)
        self.assertEqual(os.listdir(srv), ["MEMBER.md"])
        self.assertTrue(os.path.exists(channel_cmd.record_path("game", "mac-web")))

    def test_leave_keeps_a_tree_that_isnt_the_computed_one(self):
        self.member()
        mine = os.path.join(self.tmp, "my-tree")
        shutil.copytree(self.local, mine)
        section = os.path.join(self.homes["mac"], "channels.d", "game.mac-web.ini")
        with open(section, encoding="utf-8") as f:
            text = f.read()
        with open(section, "w", encoding="utf-8") as f:
            f.write(text.replace(self.local, mine))
        for job in ("up", "down"):
            self.assertEqual(self.run_cli("sync", "game", "--reset", job)[0], 0)
        out = self.ok("leave", "game")
        self.assertIn("  note    left %s in place: it isn't %s" % (mine, self.local), out)
        self.assertTrue(os.path.isdir(os.path.join(mine, "mac-web")))
        self.assertTrue(os.path.isdir(self.local))

    def test_a_record_with_another_ssh_is_another_server(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # the same machine, reached --local: still another membership
        self.assertEqual(self.refused("join", "game", "--local"),
                         "ERROR channel: you are in game on another server")

    def test_join_prints_all_only_from_the_leader(self):
        self.lead()
        self.use_box("win")
        self.ok("join", "game", "--server", "fake-dest", "--project", "b")
        own_b = os.path.join(self.joined("game.win-b"), "win-b")
        entries.post(os.path.join(own_b, "RESULTS.md"), own_b, "win-b", "a member's all",
                     [entries.ALL], body="not the leader's")
        self.assertEqual(self.run_cli("sync", "game", "--project", "b")[0], 0)
        self.use_box("mac")
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("channel game created", out)
        self.assertNotIn("a member's all", out)


OTHER_CLIENT = "f" * 32


class ClaimerTest(ChannelCase):
    """Whose a member folder is: MEMBER.md's claimer:, a hash of the channel's name and this
    machine's id; another machine's is refused, --takeover the only way past it."""

    def another_machine(self, cid=OTHER_CLIENT, key="mac2", box="mac"):
        """A second machine with box box: a VCHARON_HOME of its own and its own client id."""
        home = self.use_box(key)
        with open(os.path.join(home, "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = %s\n" % box)
        patcher = mock.patch.object(platform, "client_id", return_value=(cid, "machine id"))
        patcher.start()
        # a patch the test stopped already is a no-op here
        self.addCleanup(patcher.stop)
        return patcher

    def server_member(self, name="mac-web"):
        return entries.header_of(entries.read_text(
            os.path.join(self.root, "game", name, "MEMBER.md")), name)

    def test_the_claimer_id(self):
        mine = platform.claimer("game")
        cid = hashlib.sha256(("vcharon:" + TEST_MACHINE_ID).encode()).hexdigest()[:32]
        self.assertEqual(platform.client_id(), (cid, "from the machine id"))
        self.assertEqual(mine, hashlib.sha256(("game:" + cid).encode()).hexdigest()[:16])
        # another channel, another id; no raw id in it
        self.assertNotEqual(platform.claimer("docs"), mine)
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.assertEqual(self.server_member()["claimer"], mine)
        self.assertNotIn(TEST_MACHINE_ID, entries.read_text(
            os.path.join(self.root, "game", "mac-web", "MEMBER.md")))

    def test_another_machine_is_refused(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        before = self.server_tree()
        self.another_machine()
        for extra in ((), ("--rejoin",)):
            with self.subTest(extra=extra):
                line, fix = self.refusal("join", "game", "--server", "fake-dest", *extra)
                self.assertEqual(line, "ERROR channel: another machine holds mac-web in game")
                self.assertEqual(fix, platform.runnable(
                    "on this machine, run: vcharon setup --box NAME (ask your user for one), then "
                    "join again; only if your user confirms that this machine made that folder "
                    "(its state was wiped), run: vcharon join game --server fake-dest --rejoin "
                    "--takeover --project web"))
        self.assertEqual(self.server_tree(), before)
        self.assertFalse(os.path.exists(os.path.join(self.homes["mac2"], "state", "channels")))
        # its own box name: another member
        with open(os.path.join(self.homes["mac2"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = mac2\n")
        self.ok("join", "game", "--server", "fake-dest")
        self.assertEqual(self.server_member("mac2-web")["claimer"],
                         hashlib.sha256(
                             ("game:" + OTHER_CLIENT).encode()).hexdigest()[:16])

    def test_a_local_member_too(self):
        self.lead(where=("--local",))
        self.ok("join", "game", "--local")
        self.another_machine()
        self.assertEqual(self.refusal("join", "game", "--local", "--rejoin")[0],
                         "ERROR channel: another machine holds mac-web in game")

    def test_with_a_record_here(self):
        # the folder was taken over from elsewhere: this box's record doesn't take it back
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        with mock.patch.object(platform, "client_id", return_value=(OTHER_CLIENT, "x")):
            line, fix = self.refusal("join", "game", "--server", "fake-dest")
        self.assertEqual(line, "ERROR channel: another machine holds mac-web in game, though "
                               "this box has a join record of it")
        self.assertTrue(fix.startswith("ask your user; only if they confirm"), fix)

    def test_no_claimer_is_the_record_or_rejoin(self):
        # the same claimer without a record is CreateJoinTest's name-taken test
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # no claimer: (a MEMBER.md from before it): the record, or --rejoin, as before
        srv = os.path.join(self.root, "game", "mac-web", "MEMBER.md")
        with open(srv, encoding="utf-8") as f:
            text = f.read()
        with open(srv, "w", encoding="utf-8") as f:
            f.write("\n".join(l for l in text.split("\n") if not l.startswith("claimer:")))
        self.another_machine(key="mac3")
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest"),
                         "ERROR channel: the name mac-web is taken in game")
        self.ok("join", "game", "--server", "fake-dest", "--rejoin")

    def test_takeover(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        own = os.path.join(self.joined("game.mac-web"), "mac-web")
        entries.post(os.path.join(own, "RESULTS.md"), own, "mac-web", "work", ["@laptop-ui"],
                     body="b")
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        srv = os.path.join(self.root, "game", "mac-web", "MEMBER.md")
        with open(srv, "rb") as f:
            old = f.read()
        mine = platform.claimer("game")
        # this machine, its state wiped: a new client id
        other = self.another_machine()
        theirs = platform.claimer("game")
        # --takeover only with --rejoin
        self.assertEqual(self.refusal("join", "game", "--server", "fake-dest", "--takeover",
                                      code=3)[0], "ERROR config: --takeover goes with --rejoin")
        out = self.ok("join", "game", "--server", "fake-dest", "--rejoin", "--takeover")
        self.assertIn("  took over game/mac-web: its claimer is this machine's now", out)
        # only the claimer: line changed, at the server too; the old entries are kept
        with open(srv, "rb") as f:
            new = f.read()
        self.assertEqual(new, old.replace(("claimer: %s" % mine).encode(),
                                          ("claimer: %s" % theirs).encode()))
        self.assertIn("work", entries.read_text(
            os.path.join(self.root, "game", "mac-web", "RESULTS.md")))
        # the first machine is now the other one
        self.use_box("mac")
        other.stop()
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest"),
                         "ERROR channel: another machine holds mac-web in game, though this "
                         "box has a join record of it")

    def test_takeover_local(self):
        self.lead(where=("--local",))
        self.ok("join", "game", "--local")
        self.another_machine()
        self.ok("join", "game", "--local", "--rejoin", "--takeover")
        self.assertEqual(self.server_member()["claimer"], platform.claimer("game"))

    def test_set_header(self):
        own = os.path.join(self.tmp, "own")
        os.makedirs(own)
        path = os.path.join(own, "MEMBER.md")
        entries.post(path, own, "a", "member", ["@a"], header=[("box", "x")], number=1)
        entries.post(path, own, "a", "two", ["@a"], header=[("claimer", "keep")])
        with open(path, "rb") as f:
            before = f.read()
        os.chmod(path, 0o640)
        # added at the header's end, then replaced; the other entry's line untouched
        entries.set_header(path, own, "a", 1, "claimer", "1" * 16)
        self.assertEqual(entries.header_of(entries.read_text(path), "a"),
                         {"box": "x", "claimer": "1" * 16})
        entries.set_header(path, own, "a", 1, "claimer", "2" * 16)
        with open(path, "rb") as f:
            after = f.read()
        self.assertEqual(after, before.replace(b"box: x\n", b"box: x\nclaimer: " + b"2" * 16
                                               + b"\n", 1))
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        with self.assertRaises(VCharonError):
            entries.set_header(path, own, "b", 1, "claimer", "x")
        # CRLF kept
        with open(path, "wb") as f:
            f.write(b"# MEMBER\r\n\r\n## t \xe2\x80\x94 a#1 \xe2\x80\x94 member\r\nto: @a\r\n"
                    b"claimer: old\r\n\r\nbody\r\n")
        entries.set_header(path, own, "a", 1, "claimer", "new")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"# MEMBER\r\n\r\n## t \xe2\x80\x94 a#1 \xe2\x80\x94 "
                             b"member\r\nto: @a\r\nclaimer: new\r\n\r\nbody\r\n")

    def test_set_header_stops_at_the_next_heading(self):
        own = os.path.join(self.tmp, "own2")
        os.makedirs(own)
        path = os.path.join(own, "MEMBER.md")
        text = ("# MEMBER\n\n## t \u2014 a#1 \u2014 member\nto: @a\nbox: x\n"
                "## t \u2014 a#2 \u2014 two\nto: @a\nagent: keep\n")
        # newline="": the file holds \n on every host (set_header keeps a file's line ends)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        entries.set_header(path, own, "a", 1, "agent", "codex")
        found = entries.parse_file(path)
        self.assertEqual([e.header for e in found], [[("box", "x"), ("agent", "codex")],
                                                     [("agent", "keep")]])
        self.assertEqual(entries.read_text(path), text.replace(
            "box: x\n", "box: x\nagent: codex\n"))

    def test_the_client_id_file(self):
        # a Linux box with no machine id: a random id, made once in the state folder, used for
        # the claimer. Linux is declared only around client_id: over a whole command it would
        # also give this host's paths Linux's rules (a Windows host's C:\ isn't absolute there).
        self.lead()
        with mock.patch.object(platform, "machine_id", return_value=None):
            with mock.patch.object(platform, "os_name", return_value="linux"):
                self.assertEqual(platform.client_id(make=False), (None, "random"))
                self.assertIsNone(platform.claimer("game", make=False))
                made = platform.client_id()
            path = platform.client_id_path()
            with open(path, "rb") as f:
                before = f.read()
            cid, origin = before.decode("ascii").split("\n")[:2]
            self.assertEqual(made, (cid, origin))
            self.assertEqual(origin, "random")
            self.assertRegex(cid, r"\A[0-9a-f]{32}\Z")
            self.ok("join", "game", "--server", "fake-dest")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), before)
            self.assertEqual(platform.client_id(), (cid, "random"))
            self.assertEqual(self.server_member()["claimer"], hashlib.sha256(
                ("game:" + cid).encode()).hexdigest()[:16])
        # a machine id that turns up later, or fails once, changes nothing: the file counts
        self.assertEqual(platform.client_id(), (cid, "random"))
        with mock.patch.object(platform, "machine_id", side_effect=[None, "1" * 32]):
            self.assertEqual(platform.client_id(), (cid, "random"))
        # a broken file is refused, never replaced: a new id would look like another machine
        with open(path, "w") as f:
            f.write("x\n")
        line = self.refused("join", "game", "--server", "fake-dest", "--role", "b", code=3)
        self.assertEqual(line, "ERROR config: %s isn't a client id (32 hex digits)" % path)

    def test_a_wiped_state_folder_gets_the_same_id(self):
        # from the machine id: made again the same; it stays after the machine id is gone
        cid, origin = platform.client_id()
        self.assertEqual(origin, "from the machine id")
        os.remove(platform.client_id_path())
        self.assertEqual(platform.client_id(), (cid, origin))
        # whatever the OS: the file counts, so not even a Mac's failed read refuses
        for osn in ("linux", "darwin"):
            with mock.patch.object(platform, "machine_id", return_value=None), \
                    mock.patch.object(platform, "os_name", return_value=osn):
                self.assertEqual(platform.client_id(), (cid, origin))
        os.environ["VCHARON_TEST_MACHINE_ID"] = OTHER_MACHINE
        self.assertEqual(platform.client_id(), (cid, origin))

    def test_an_empty_client_id_file(self):
        # read again a few times (a writer racing), then refused; never written over
        path = platform.client_id_path()
        os.makedirs(os.path.dirname(path))
        open(path, "wb").close()
        sleeps = []
        with (mock.patch.object(platform.time, "sleep", sleeps.append),
              self.assertRaises(VCharonError) as cm):
            platform.client_id()
        self.assertEqual(cm.exception.message, "%s isn't a client id (32 hex digits)" % path)
        self.assertEqual(sleeps, [platform.CLIENT_ID_PAUSE] * (platform.CLIENT_ID_TRIES - 1))
        self.assertEqual(os.path.getsize(path), 0)
        # filled in by the other writer between two reads: taken
        calls = []

        def fill(seconds):
            calls.append(seconds)
            with open(path, "w") as f:
                f.write("a" * 32 + "\nrandom\n")
        with mock.patch.object(platform.time, "sleep", fill):
            self.assertEqual(platform.client_id(), ("a" * 32, "random"))
        self.assertEqual(len(calls), 1)
        # no temp file left beside it
        self.assertEqual(os.listdir(os.path.dirname(path)), ["client-id"])

    def test_no_hard_links_falls_back_to_an_exclusive_create(self):
        # FAT, exFAT, some SMB and FUSE mounts: os.link fails with EPERM and the like. The
        # link path on every host: NTFS has os.link too, though a Windows host renames.
        windows = mock.patch.object(platform.fsops, "WINDOWS", False)
        windows.start()
        self.addCleanup(windows.stop)

        def no_link(src, dst):
            raise OSError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(platform.os, "link", no_link):
            cid, origin = platform.client_id()
            self.assertEqual(origin, "from the machine id")
            path = platform.client_id_path()
            self.assertEqual(os.listdir(os.path.dirname(path)), ["client-id"])
            with open(path) as f:
                self.assertEqual(f.read(), "%s\nfrom the machine id\n" % cid)
            # another run's file there: kept, and read
            os.remove(path)
            with open(path, "w") as f:
                f.write("c" * 32 + "\nrandom\n")
            self.assertEqual(platform.client_id(), ("c" * 32, "random"))
        # any other link error is an error
        with mock.patch.object(platform.os, "link",
                               side_effect=OSError(errno.EIO, "I/O error")):
            os.remove(path)
            with self.assertRaises(VCharonError):
                platform.client_id()
        self.assertFalse(os.path.exists(path))

    def test_the_rename_path_keeps_another_run_s_file(self):
        # Windows' way: a rename, which fails when another run's file is there. A fake rename
        # on every host, since a POSIX one would replace that file.
        path = platform.client_id_path()
        folder = os.path.dirname(path)

        def lost_race(src, dst):
            with open(dst, "wb") as f:
                f.write(b"c" * 32 + b"\nrandom\n")
            raise FileExistsError(errno.EEXIST, "File exists", dst)
        with mock.patch.object(platform.fsops, "WINDOWS", True):
            with mock.patch.object(platform.os, "rename", lost_race):
                self.assertEqual(platform.client_id(), ("c" * 32, "random"))
            self.assertEqual(os.listdir(folder), ["client-id"])
            # any other rename error is an error, and leaves no file behind
            os.remove(path)
            with (mock.patch.object(platform.os, "rename",
                                    side_effect=OSError(errno.EIO, "I/O error")),
                  self.assertRaises(VCharonError)):
                platform.client_id()
        self.assertEqual(os.listdir(folder), [])

    @unittest.skipUnless(os.name == "nt", "only Windows' rename refuses an existing file")
    def test_windows_rename_never_replaces_a_file(self):
        # the real rename: another run's file turns up after the first read; kept, and read
        path = platform.client_id_path()
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)

        def other_run_first():
            with open(path, "wb") as f:
                f.write(b"c" * 32 + b"\nrandom\n")
            return "1" * 32
        with mock.patch.object(platform, "machine_id", other_run_first):
            self.assertEqual(platform.client_id(), ("c" * 32, "random"))
        self.assertEqual(os.listdir(folder), ["client-id"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"c" * 32 + b"\nrandom\n")

    def test_the_host_os_picks_link_or_rename(self):
        # a test that declares Windows on a POSIX host still links: a POSIX rename would
        # replace another run's file
        if os.name == "nt":
            self.skipTest("this host renames")
        with mock.patch.object(platform, "os_name", return_value="windows"), \
                mock.patch.object(platform.os, "rename",
                                  side_effect=AssertionError("renamed")):
            self.assertEqual(platform.client_id()[1], "from the machine id")

    def test_a_mac_or_windows_without_its_id_is_refused(self):
        # those always have one: a missing id is a failed read, never a random id kept
        for osn in ("darwin", "windows"):
            with (self.subTest(osn=osn),
                  mock.patch.object(platform, "os_name", return_value=osn),
                  mock.patch.object(platform, "machine_id", return_value=None)):
                path = platform.client_id_path()
                for make in (True, False):
                    with self.assertRaises(VCharonError) as cm:
                        platform.client_id(make)
                    self.assertTrue(cm.exception.message.startswith(
                        "couldn't read this machine's id ("), cm.exception.message)
                    self.assertEqual(cm.exception.hint, "run the command again; if it "
                                     "keeps failing, your user can write %s by hand: 32 "
                                     "hex digits (0-9, a-f), then a line: random" % path)
                self.assertFalse(os.path.exists(path))
                # the way out the fix names: a file written by hand is taken
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write("d" * 32 + "\nrandom\n")
                self.assertEqual(platform.client_id(), ("d" * 32, "random"))
                os.remove(path)
        with mock.patch.object(platform, "machine_id", return_value=None), \
                mock.patch.object(platform, "os_name", return_value="linux"):
            self.assertEqual(platform.client_id()[1], "random")

    @unittest.skipIf(os.name == "nt", "a hard link here; Windows renames")
    def test_two_first_runs_one_id(self):
        # the second run's link fails: it reads the first one's id, and drops its temp file
        path = platform.client_id_path()
        os.makedirs(os.path.dirname(path))
        real_link = os.link

        def first_wins(src, dst):
            with open(dst, "w") as f:
                f.write("b" * 32 + "\nrandom\n")
            return real_link(src, dst)
        with mock.patch.object(platform.os, "link", first_wins):
            self.assertEqual(platform.client_id(), ("b" * 32, "random"))
        self.assertEqual(os.listdir(os.path.dirname(path)), ["client-id"])


class MemberFieldsTest(ChannelCase):
    """MEMBER.md's fields, the agent, and no host name or path in a channel's files."""

    def test_agent_flag_and_detection(self):
        self.lead(where=("--local",))
        own = os.path.join(self.root, "game")
        self.ok("join", "game", "--local", "--agent", "codex")
        self.assertEqual(entries.header_of(entries.read_text(
            os.path.join(own, "mac-web", "MEMBER.md")), "mac-web")["agent"], "codex")
        # one detected agent, and none; test_detect_agent has the whole table
        for env, want in (({"CLAUDECODE": "1"}, "claude"), ({}, "other")):
            with self.subTest(env=env):
                role = "r%d" % len(os.listdir(own))
                with mock.patch.dict(os.environ, env):
                    self.ok("join", "game", "--local", "--role", role)
                self.assertEqual(entries.header_of(entries.read_text(
                    os.path.join(own, "mac-web-" + role, "MEMBER.md")),
                    "mac-web-" + role)["agent"], want)
        # --agent wins over the environment; a name that isn't one is a usage error
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.ok("join", "game", "--local", "--role", "x", "--agent", "opencode")
        self.assertEqual(entries.header_of(entries.read_text(
            os.path.join(own, "mac-web-x", "MEMBER.md")), "mac-web-x")["agent"], "opencode")
        self.assertEqual(self.channel("join", "game", "--local", "--agent", "vim")[0], 3)

    def test_detect_agent(self):
        self.assertIsNone(platform.detect_agent({}))
        self.assertEqual(platform.detect_agent({"CLAUDECODE": "1"}), "claude")
        self.assertEqual(platform.detect_agent({"CODEX_THREAD_ID": "x"}), "codex")
        self.assertEqual(platform.detect_agent({"OPENCODE": "1"}), "opencode")
        self.assertIsNone(platform.detect_agent({"CLAUDECODE": "", "OPENCODE": ""}))
        self.assertIsNone(platform.detect_agent({"CLAUDECODE": "1", "CODEX_THREAD_ID": "x"}))

    def test_no_host_name_or_path_in_channel_files(self):
        host = "zz-host-7f3q"
        with mock.patch("socket.gethostname", return_value=host), \
                mock.patch("platform.node", return_value=host):
            self.lead(where=("--local",))
            self.ok("join", "game", "--local", "--project", "local")
            self.ok("join", "game", "--server", "fake-dest")
            self.assertEqual(self.run_cli("post", "game", "--to", "@laptop-ui", "--title", "t",
                                          "--body", "b")[0], 0)
            self.assertEqual(self.run_cli("sync", "game")[0], 0)
        import socket
        words = [host, socket.gethostname(), self.tmp, os.path.realpath(self.tmp),
                 self.home, os.path.expanduser("~"), TEST_MACHINE_ID]
        tree = read_tree(self.root)
        files = [p for p, data in tree.items() if data is not None]
        self.assertEqual(sorted(p.rsplit("/", 1)[1] for p in files),
                         ["CHANNEL.md", "MEMBER.md", "MEMBER.md", "MEMBER.md", "RESULTS.md",
                          "RESULTS.md"])
        for path in files:
            text = tree[path].decode("utf-8")
            self.assertNotIn("server host", text, path)
            for word in words:
                self.assertNotIn(word, text, (path, word))
        header = entries.header_of(tree["game/mac-web/MEMBER.md"].decode("utf-8"), "mac-web")
        self.assertEqual(sorted(header), ["agent", "box", "channel", "claimer", "leader", "name",
                                          "os", "project", "vcharon"])
        self.assertEqual((header["box"], header["os"], header["project"]),
                         ("mac", platform.os_word(), "web"))

    def test_a_rejoin_updates_the_agent(self):
        # one checkout, Claude Code one day and Codex the next: the folder keeps its name
        self.lead()
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.ok("join", "game", "--server", "fake-dest")
        self.assertEqual(self.member_header()["agent"], "claude")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "t"}):
            out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  agent: codex (was claude)", out)
        # sent by the rejoin's sync
        self.assertEqual(self.member_header()["agent"], "codex")
        out = self.ok("join", "game", "--server", "fake-dest", "--agent", "codex")
        self.assertNotIn("  agent: ", out)
        # a local member, --agent; and with the pull of a lost tree first
        self.lead("docs", where=("--local",), box="linux", project="d")
        self.ok("join", "docs", "--local")
        self.ok("join", "docs", "--local", "--agent", "opencode")
        self.assertEqual(self.member_header("docs")["agent"], "opencode")
        shutil.rmtree(self.joined("game.mac-web"))
        os.remove(os.path.join(os.environ["VCHARON_HOME"], "state", "game.mac-web.up.json"))
        out = self.ok("join", "game", "--server", "fake-dest", "--agent", "claude")
        self.assertIn("pulled your folder from the server", out)
        self.assertIn("  agent: claude (was codex)", out)
        self.assertEqual(self.member_header()["agent"], "claude")

    def test_a_rejoin_from_a_plain_terminal_keeps_the_agent(self):
        self.lead(where=("--local",))
        self.ok("join", "game", "--local", "--agent", "codex")
        out = self.ok("join", "game", "--local")
        self.assertNotIn("  agent: ", out)
        self.assertEqual(self.member_header()["agent"], "codex")

    def test_a_rejoin_updates_the_version(self):
        # a member that joined with another vcharon: the rejoin says which one runs now
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.assertEqual(self.member_header()["vcharon"], vcharon.VERSION)
        own = os.path.join(self.joined("game.mac-web"), "mac-web")
        path = os.path.join(own, "MEMBER.md")
        entries.set_header(path, own, "mac-web", 1, "vcharon", "0.0.9")
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  vcharon: %s (was 0.0.9)" % vcharon.VERSION, out)
        # sent by the rejoin's sync
        self.assertEqual(self.member_header()["vcharon"], vcharon.VERSION)
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertNotIn("  vcharon: ", out)
        # a MEMBER.md written before the line existed: added at the header's end, the rest kept
        with open(path, "rb") as f:
            text = f.read()
        old = text.replace(b"vcharon: %s\n" % vcharon.VERSION.encode(), b"")
        with open(path, "wb") as f:
            f.write(old)
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  vcharon: %s (was not set)" % vcharon.VERSION, out)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), text)

    def test_a_watcher_start_updates_the_version(self):
        # an update without a rejoin (a leader never rejoins; EXIT updated restarts the watcher
        # alone): the watcher's start sets the line, in place, and prints nothing
        self.lead(where=("--local",))
        self.use_box("laptop")
        own = os.path.join(self.root, "game", "laptop-ui")
        path = os.path.join(own, "MEMBER.md")
        entries.set_header(path, own, "laptop-ui", 1, "vcharon", "0.0.9")
        with open(path, "rb") as f:
            stale = f.read()
        started = []
        with mock.patch.object(watch, "watch_dir", lambda *a, **kw: started.append(a) or 0):
            code, out, err = self.run_cli("watch", "game", "--project", "ui")
        self.assertEqual((code, out, err, len(started)), (0, "", "", 1))
        with open(path, "rb") as f:
            # one line changed; the heading stays, so no watcher takes the entry for edited
            self.assertEqual(f.read(), stale.replace(b"vcharon: 0.0.9",
                                                     b"vcharon: " + vcharon.VERSION.encode()))
        # a MEMBER.md from before the line: added
        with open(path, "wb") as f:
            f.write(stale.replace(b"vcharon: 0.0.9\n", b""))
        with mock.patch.object(watch, "watch_dir", lambda *a, **kw: 0):
            self.assertEqual(self.run_cli("watch", "game", "--project", "ui")[:2], (0, ""))
        self.assertEqual(entries.header_of(entries.read_text(path), "laptop-ui")["vcharon"],
                         vcharon.VERSION)
        # a remote member: its local tree's copy, which the next sync sends
        self.use_box("mac")
        self.ok("join", "game", "--server", "fake-dest")
        own = os.path.join(self.joined("game.mac-web"), "mac-web")
        entries.set_header(os.path.join(own, "MEMBER.md"), own, "mac-web", 1, "vcharon", "0.0.9")
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        self.assertEqual(self.member_header()["vcharon"], "0.0.9")
        with mock.patch.object(watch, "watch_job", lambda *a, **kw: 0):
            self.assertEqual(self.run_cli("watch", "game")[:2], (0, ""))
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        self.assertEqual(self.member_header()["vcharon"], vcharon.VERSION)

    def test_a_failed_version_update_never_stops_the_watch(self):
        self.lead(where=("--local",))
        self.use_box("laptop")
        own = os.path.join(self.root, "game", "laptop-ui")
        entries.set_header(os.path.join(own, "MEMBER.md"), own, "laptop-ui", 1, "vcharon",
                           "0.0.9")
        # a post holds the own folder's lock (another process): one try, skipped at once
        hold(self, entries.lock_path(own))
        started = time.monotonic()
        with mock.patch.object(watch, "watch_dir", lambda *a, **kw: 0):
            self.assertEqual(self.run_cli("watch", "game", "--project", "ui"), (0, "", ""))
        self.assertLess(time.monotonic() - started, entries.LOCK_WAIT / 3)
        self.assertEqual(self.member_header_of(own, "laptop-ui")["vcharon"], "0.0.9")
        # any other failure: the same
        busy = VCharonError("io", "can't open the post lock")
        with mock.patch.object(entries, "set_header", side_effect=busy), \
                mock.patch.object(watch, "watch_dir", lambda *a, **kw: 0):
            self.assertEqual(self.run_cli("watch", "game", "--project", "ui"), (0, "", ""))
        self.assertEqual(self.member_header_of(own, "laptop-ui")["vcharon"], "0.0.9")

    def member_header_of(self, own, name):
        return entries.header_of(entries.read_text(os.path.join(own, "MEMBER.md")), name)

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_a_symlinked_member_md_is_refused_before_the_record(self):
        self.lead(where=("--local",))
        self.ok("join", "game", "--local")
        path = os.path.join(self.root, "game", "mac-web", "MEMBER.md")
        elsewhere = os.path.join(self.tmp, "elsewhere.md")
        shutil.move(path, elsewhere)
        os.symlink(elsewhere, path)
        with open(elsewhere, "rb") as f:
            before = f.read()
        record = channel_cmd.record_path("game", "mac-web")
        os.remove(record)
        for extra in ((), ("--agent", "claude"), ("--takeover",)):
            with self.subTest(extra=extra):
                line, fix = self.refusal("join", "game", "--local", "--rejoin", *extra)
                self.assertEqual(line, "ERROR unsafe_path: %s isn't a regular file (a symlink, "
                                 "or a folder): vcharon writes MEMBER.md itself, and never "
                                 "through a link" % path)
                self.assertEqual(fix, "remove it by hand, then join again")
                self.assertFalse(os.path.exists(record))
        with open(elsewhere, "rb") as f:
            self.assertEqual(f.read(), before)

    def member_header(self, channel="game"):
        return entries.header_of(entries.read_text(
            os.path.join(self.root, channel, "mac-web", "MEMBER.md")), "mac-web")

    def test_member_file_reads_only_a_plain_small_file(self):
        folder = os.path.join(self.tmp, "m")
        os.makedirs(folder)
        path = os.path.join(folder, "MEMBER.md")
        body = b"# MEMBER\n\n## t \xe2\x80\x94 m#1 \xe2\x80\x94 member\nto: @m\nbox: x\n"
        self.assertIsNone(channels._member_file(folder))
        with open(path, "wb") as f:
            f.write(body)
        self.assertEqual(channels._member_file(folder), body)
        # over 64 KiB: not vcharon's, not read
        with open(path, "wb") as f:
            f.write(body + b"x" * channels.MEMBER_READ_MAX)
        self.assertIsNone(channels._member_file(folder))
        with open(path, "wb") as f:
            f.write(body.ljust(channels.MEMBER_READ_MAX, b"x"))
        self.assertEqual(len(channels._member_file(folder)), channels.MEMBER_READ_MAX)
        os.remove(path)
        if CAN_SYMLINK:
            other = os.path.join(self.tmp, "other.md")
            with open(other, "wb") as f:
                f.write(body)
            os.symlink(other, path)
            self.assertIsNone(channels._member_file(folder))
            os.remove(path)
        if hasattr(os, "mkfifo"):
            os.mkfifo(path)
            self.assertIsNone(channels._member_file(folder))
            os.remove(path)
        os.mkdir(path)
        self.assertIsNone(channels._member_file(folder))

    def test_the_box_of_a_name_kept_after_a_box_change(self):
        # the record keeps mac-web; a MEMBER.md made again says box mac, not the new box
        self.lead(where=("--local",))
        self.ok("join", "game", "--local")
        with open(os.path.join(self.homes["mac"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = laptop2\n")
        os.remove(os.path.join(self.root, "game", "mac-web", "MEMBER.md"))
        out = self.ok("join", "game", "--local")
        self.assertIn("took back game/mac-web", out)
        self.assertEqual(entries.header_of(entries.read_text(
            os.path.join(self.root, "game", "mac-web", "MEMBER.md")), "mac-web")["box"], "mac")


class SeenTest(ChannelCase):
    """The members' last-watched stamps: seen/<channel>/<member> beside the root, stamped by a
    watcher's pull, its age back in the down's plan reply, kept on the client and shown by
    whoami C; swept with the channel."""

    def seen(self, *parts):
        return os.path.join(self.home, "seen", *parts)

    def test_stamp_and_list(self):
        write_tree(self.root, {"game/a/": None, "game/b/": None})
        self.assertTrue(channels.stamp_seen(self.root, "game", "a", "stream 2", now=1000))
        path = self.seen("game", "a")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"stream 2\n")
        self.assertEqual(os.stat(path).st_mtime, 1000)
        # under SEEN_EVERY: no write; at it: the time, set as given
        self.assertFalse(channels.stamp_seen(self.root, "game", "a", "stream 2", now=1029))
        self.assertEqual(os.stat(path).st_mtime, 1000)
        self.assertTrue(channels.stamp_seen(self.root, "game", "a", "stream 2", now=1030))
        self.assertEqual(os.stat(path).st_mtime, 1030)
        # a clock set back writes it again
        self.assertTrue(channels.stamp_seen(self.root, "game", "a", "stream 2", now=900))
        # another pace is written at once
        self.assertTrue(channels.stamp_seen(self.root, "game", "a", "run 30", now=901))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"run 30\n")
        # a stamp of a name with no member folder, and stray files, are left out
        write_tree(self.home, {"seen/game/zz": b"local 10\n", "seen/game/b": b"what\n",
                               "seen/game/.x.tmp": b"run 30\n"})
        self.assertEqual(channels.list_seen(self.root, "game", now=961), {"a": [60, "run 30"]})
        self.assertEqual(channels.list_seen(self.root, "nope"), {})
        # no channel folder: no stamp, nothing made
        self.assertFalse(channels.stamp_seen(self.root, "nope", "a", "run 30"))
        self.assertFalse(os.path.exists(self.seen("nope")))
        for pace in ("walk 2", "run 0", "run 86401", "run 2 ", ""):
            with self.subTest(pace=pace), self.assertRaises(VCharonError):
                channels.stamp_seen(self.root, "game", "a", pace)
        with self.assertRaises(VCharonError):
            channels.stamp_seen(self.root, "..", "a", "run 30")

    def test_a_close_between_the_check_and_the_mkdir(self):
        # the channel goes after the stamp saw it: the seen/<channel>/ it made goes too
        write_tree(self.root, {"game/a/": None})
        real = channels._mkdir

        def mkdir(path):
            made = real(path)
            if path == self.seen("game"):
                shutil.rmtree(os.path.join(self.root, "game"))
            return made

        with mock.patch.object(channels, "_mkdir", mkdir):
            self.assertFalse(channels.stamp_seen(self.root, "game", "a", "run 30"))
        self.assertEqual(os.listdir(self.seen()), [])

    def test_swept_with_the_channel(self):
        write_tree(self.home, {"seen/old/a": b"run 30\n",
                               # not what a stamp makes: left
                               "seen/odd/a/x": b"", "seen/big/a": b"x" * 100,
                               "seen/game/c": b"run 30\n", "seen/game/a": b"run 30\n"})
        write_tree(self.root, {"game/a/MEMBER.md": b"m"})
        # a claim sweeps, and a new member's name loses an earlier member's stamp
        channels.claim(self.root, "game", "c", False)
        channels.claim(self.root, "game", "a", False)
        self.assertEqual(sorted(read_tree(self.seen())),
                         ["big/", "big/a", "game/", "game/a", "odd/", "odd/a/", "odd/a/x"])
        # a failed create's undo (release)
        write_tree(self.root, {"solo/lead/MEMBER.md": b"m"})
        write_tree(self.home, {"seen/solo/lead": b"run 30\n"})
        self.assertEqual(channels.release(self.root, "solo", "lead"), {"removed": True})
        self.assertFalse(os.path.exists(self.seen("solo")))
        # a close
        write_tree(self.root, {"docs/lead/CHANNEL.md": b"c"})
        write_tree(self.home, {"seen/docs/lead": b"local 10\n"})
        channels.remove(self.root, "docs", "lead")
        self.assertFalse(os.path.exists(self.seen("docs")))
        self.assertTrue(os.path.exists(self.seen("game", "a")))

    def plan(self, s, path, **more):
        args = {"plugin": "path", "options": {"path": path, "mailbox_me": "a", "prune": "yes",
                                              "allow_empty": "yes"},
                "state": {}, "full": False}
        args.update(more)
        got = s.call("source.plan", args)
        remote.reset(s)
        return got

    def test_the_helper_stamps_a_watchers_pull_only(self):
        write_tree(self.root, {"game/a/MEMBER.md": b"m", "game/b/MEMBER.md": b"m"})
        s = self.session()
        s.open()
        game = os.path.join(self.root, "game")
        # a pull without watch: the ages, no stamp
        got = self.plan(s, game)
        self.assertEqual(got["seen"], {})
        self.assertNotIn("seen_error", got)
        self.assertFalse(os.path.exists(self.seen()))
        got = self.plan(s, game, watch="stream 2")
        self.assertEqual(got["seen"], {"a": [0, "stream 2"]})
        with open(self.seen("game", "a"), "rb") as f:
            self.assertEqual(f.read(), b"stream 2\n")
        # the rest of the reply is the plan: from_json refuses a key it doesn't know
        with self.assertRaises(VCharonError):
            plan.from_json(dict(got))
        p = plan.from_json({k: v for k, v in got.items() if k != "seen"})
        self.assertEqual(sorted(e.path for e in p.entries), ["b", "b/MEMBER.md"])
        # a folder that isn't a channel in the root: nothing
        write_tree(self.tmp, {"other/a/x": b"x"})
        self.assertNotIn("seen", self.plan(s, os.path.join(self.tmp, "other"),
                                           watch="stream 2"))
        # a stamp that fails never fails the plan
        shutil.rmtree(self.seen())
        with open(self.seen(), "wb") as f:
            f.write(b"a file")
        got = self.plan(s, game, watch="run 30")
        self.assertIn("stamping a: ", got["seen_error"])
        self.assertEqual(got["seen"], {})
        self.assertIn("entries", got)
        # a pace in another shape: refused, as any malformed call
        with self.assertRaises(VCharonError) as cm:
            s.call("source.plan", {"plugin": "path", "options": {"path": game},
                                   "state": {}, "full": False, "watch": "fast"})
        self.assertEqual(cm.exception.code, "protocol")

    def test_a_watchers_sync_and_whoami(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # a sync by hand stamps nothing, but brings the ages (none yet)
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        self.assertFalse(os.path.exists(self.seen("game")))
        with open(charter.seen_path("game.mac-web"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"members": {}})
        # a watcher's child: its environment carries the pace
        with mock.patch.dict(os.environ, {watch.WATCH_ENV: "stream 2"}):
            self.assertEqual(self.run_cli("sync", "game")[0], 0)
        with open(self.seen("game", "mac-web"), "rb") as f:
            self.assertEqual(f.read(), b"stream 2\n")
        # a pace in another shape is no watcher's: nothing stamped
        os.environ[watch.WATCH_ENV] = "fast"
        self.addCleanup(os.environ.pop, watch.WATCH_ENV, None)
        self.use_box("laptop")
        self.assertEqual(self.run_cli("sync", "game", "--project", "ui")[0], 0)
        self.assertEqual(os.listdir(self.seen("game")), ["mac-web"])
        self.use_box("mac")
        doc = json.loads(self.run_cli("whoami", "game", "--json")[1])
        got = {m["name"]: (m["watched"] is not None, m["watch_every"]) for m in doc["members"]}
        self.assertEqual(got, {"laptop-ui": (False, None), "mac-web": (True, 2)})
        self.assertNotIn("vcharon", doc["members"][0])
        lines = self.run_cli("whoami", "game")[1].splitlines()
        self.assertRegex(lines[-1], r"\A    mac-web \(you\)  .*  watched \d+ s ago "
                                    r"\(every 2 s\)\Z")
        self.assertTrue(lines[-2].endswith("  watched -"), lines[-2])
        # read --json carries them too
        doc = json.loads(self.run_cli("read", "game", "--json")[1])
        got = {m["name"]: (m["watched"] is not None, m["watch_every"])
               for m in doc["member_info"]}
        self.assertEqual(got, {"laptop-ui": (False, None), "mac-web": (True, 2)})
        # leave removes the kept ages with the membership's other files
        self.ok("leave", "game")
        self.assertFalse(os.path.exists(charter.seen_path("game.mac-web")))

    def test_a_servers_failed_stamp_is_logged(self):
        # only logged: the down log is the one place it shows
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        os.makedirs(os.path.dirname(self.seen()), exist_ok=True)
        with open(self.seen(), "wb") as f:
            f.write(b"a file")
        with mock.patch.dict(os.environ, {watch.WATCH_ENV: "stream 2"}):
            code, out, err = self.run_cli("sync", "game")
        self.assertEqual((code, err), (0, ""), out)
        with open(os.path.join(os.environ["VCHARON_HOME"], "logs", "game.mac-web.down.log"),
                  encoding="utf-8") as f:
            self.assertRegex(f.read(), r"  warn  the server couldn't give the members' "
                                       r"last-watched times: stamping mac-web: ")


class SeenRoundsTest(ChannelCase):
    """The stamps across a watcher's sync --repeat rounds, in a real child over the fake ssh,
    and a seen/ that is a link."""

    def seen(self, *parts):
        return os.path.join(self.home, "seen", *parts)

    def down_log(self):
        try:
            with open(os.path.join(os.environ["VCHARON_HOME"], "logs", "game.mac-web.down.log"),
                      encoding="utf-8") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_each_round_of_one_session_stamps_quietly(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        code = ("import sys\n"
                "sys.path.insert(0, %r)\n"
                "from vcharon import cli, ssh\n"
                "ssh.ssh_prefix = lambda settings: [sys.executable, %r]\n"
                "sys.exit(cli.main(sys.argv[1:]))\n" % (VCHARON_DIR, FAKE_SSH))
        env = dict(os.environ, **{watch.WATCH_ENV: "stream 2"})
        child = subprocess.Popen([sys.executable, "-c", code, "sync", "game", "--repeat", "1"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env)
        stamp = self.seen("game", "mac-web")
        try:
            # round 0 pulls the channel; round 1 has nothing to do
            rounds = [util.readline(child.stdout) for _ in range(2)]
            self.assertEqual([r.rstrip(b"\r\n") for r in rounds], [b"ROUND 0"] * 2)
            with open(stamp, "rb") as f:
                self.assertEqual(f.read(), b"stream 2\n")
            logged = self.down_log()
            # a control: the round that pulled logged
            self.assertTrue(any("saved the state of game.mac-web.down" in line
                                for line in logged), logged)
            # an old stamp: a later round of the same session writes the time again
            old = time.time() - 100
            os.utime(stamp, (old, old))
            rounds = [util.readline(child.stdout) for _ in range(2)]
            self.assertEqual([r.rstrip(b"\r\n") for r in rounds], [b"ROUND 0"] * 2)
            self.assertGreater(os.stat(stamp).st_mtime, old + 90)
            # the stamp isn't a change: those rounds logged nothing
            self.assertEqual(self.down_log(), logged)
        finally:
            child.stdin.close()
            child.stdin = None
            if child.poll() is None:
                try:
                    child.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
        self.assertEqual(child.returncode, 0)
        self.assertEqual(charter.load_seen("game.mac-web")["mac-web"][1], 2)
        # the leader never watched: no stamp
        self.assertEqual(os.listdir(self.seen("game")), ["mac-web"])

    @unittest.skipUnless(CAN_SYMLINK, "needs symlinks")
    def test_a_linked_seen_is_never_followed(self):
        write_tree(self.root, {"game/a/": None})
        elsewhere = os.path.join(self.tmp, "elsewhere")
        # what a stamp leaves, but outside the store: a sweep would take it for one
        write_tree(elsewhere, {"notes/alice": b"todo\n", "notes/bob": b"run 30\n"})
        os.symlink(elsewhere, self.seen())
        channels.sweep_seen(self.root)
        self.assertTrue(os.path.exists(os.path.join(elsewhere, "notes", "alice")))
        self.assertFalse(channels.stamp_seen(self.root, "game", "a", "run 30"))
        write_tree(self.root, {"notes/alice/": None, "notes/bob/": None})
        channels.drop_seen(self.root, "notes", "alice")
        self.assertEqual(channels.list_seen(self.root, "notes"), {})
        self.assertEqual(sorted(read_tree(elsewhere)), ["notes/", "notes/alice", "notes/bob"])

    @unittest.skipUnless(CAN_SYMLINK, "needs symlinks")
    def test_a_linked_channel_stamps_folder_is_never_followed(self):
        # seen/ real, seen/<channel> a link: one level down, the same rule
        write_tree(self.root, {"game/alice/": None})
        elsewhere = os.path.join(self.tmp, "elsewhere")
        write_tree(elsewhere, {"alice": b"todo\n", "bob": b"run 30\n"})
        os.mkdir(self.seen())
        os.symlink(elsewhere, self.seen("game"))
        self.assertFalse(channels.stamp_seen(self.root, "game", "alice", "run 30"))
        self.assertEqual(channels.list_seen(self.root, "game"), {})
        channels.drop_seen(self.root, "game", "alice")
        self.assertEqual(sorted(read_tree(elsewhere)), ["alice", "bob"])

    def test_a_junction_seen_is_a_link_not_an_error(self):
        # a Windows junction: fsops.kind says LINK, os.path.islink says False; faked here
        write_tree(self.root, {"game/a/": None})
        os.mkdir(self.seen())
        real_kind = channels.fsops.kind
        top = os.lstat(self.seen())

        def kind(st):
            if (st.st_dev, st.st_ino) == (top.st_dev, top.st_ino):
                return channels.fsops.LINK
            return real_kind(st)

        with mock.patch.object(channels.fsops, "kind", kind):
            self.assertFalse(channels.stamp_seen(self.root, "game", "a", "run 30"))
        self.assertEqual(os.listdir(self.seen()), [])


@unittest.skipIf(sys.platform == "win32", "the fake ssh is reached through a shell script")
class SeenWatchProcessTest(ChannelCase):
    """A real vcharon watch process of a remote member stamps the server: the pace goes from the
    watcher through its sync child's environment to the helper, streaming or not."""

    def seen(self, *parts):
        return os.path.join(self.home, "seen", *parts)

    def watch_until_stamped(self, *flags):
        """Runs vcharon watch game <flags> until seen/game/mac-web exists; (its text, the
        watcher's output)."""
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # the sync children are new processes: they reach the fake server through ssh_path
        wrapper = os.path.join(self.tmp, "ssh.sh")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, FAKE_SSH))
        os.chmod(wrapper, 0o755)
        with open(os.path.join(self.homes["mac"], "vcharon.ini"), "a", encoding="utf-8") as f:
            f.write("ssh_path = %s\n" % wrapper)
        out_path = os.path.join(self.tmp, "watch.out")
        env = dict(os.environ, PYTHONPATH=VCHARON_DIR)
        env.pop(watch.WATCH_ENV, None)
        with open(out_path, "wb") as out:
            proc = subprocess.Popen([sys.executable, "-m", "vcharon", "watch", "game", *flags],
                                    stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, env=env, start_new_session=True)
        stamp = self.seen("game", "mac-web")
        try:
            deadline = time.monotonic() + 60
            while not os.path.exists(stamp) and time.monotonic() < deadline:
                self.assertIsNone(proc.poll(), "the watcher ended early")
                time.sleep(0.2)
        finally:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait()
        with open(out_path, encoding="utf-8", errors="replace") as f:
            said = f.read()
        self.assertTrue(os.path.exists(stamp), said)
        with open(stamp, "rb") as f:
            return f.read(), said

    def test_a_streaming_watcher_stamps(self):
        text, said = self.watch_until_stamped("--every", "3")
        self.assertEqual(text, b"stream 3\n")
        # the member's own copy of the ages came back with the same pull
        self.assertEqual(charter.load_seen("game.mac-web")["mac-web"][1], 3)
        # the leader's watcher never ran: no stamp of it
        self.assertEqual(os.listdir(self.seen("game")), ["mac-web"])
        self.assertNotIn("watched", said)

    def test_a_no_stream_watcher_stamps(self):
        text, said = self.watch_until_stamped("--no-stream", "--every", "7")
        self.assertEqual(text, b"run 7\n")
        self.assertEqual(charter.load_seen("game.mac-web")["mac-web"][1], 7)
        self.assertNotIn("watched", said)


@unittest.skipIf(sys.platform == "win32", "the fake ssh is reached through a shell script")
class OnceProcessTest(ChannelCase):
    """Real vcharon watch --once processes of a remote member, each with its own sync child
    over the fake ssh: refused before a watcher ran, then a quiet check, a check that prints a
    post once, and a stalled sync cut off at the check's own cap."""

    def setUp(self):
        ChannelCase.setUp(self)
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # the sync children are new processes: they reach the fake server through ssh_path.
        # Each ssh's pid is kept: ssh runs in a session of its own, so a sync cut off at its
        # cap leaves it, and the test ends it
        self.pids = os.path.join(self.tmp, "ssh.pids")
        wrapper = os.path.join(self.tmp, "ssh.sh")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho $$ >> %s\nexec %s %s \"$@\"\n"
                    % (self.pids, sys.executable, FAKE_SSH))
        os.chmod(wrapper, 0o755)
        with open(os.path.join(self.homes["mac"], "vcharon.ini"), "a", encoding="utf-8") as f:
            f.write("ssh_path = %s\n" % wrapper)
        self.addCleanup(self.end_ssh)
        self.state = os.path.join(self.homes["mac"], "state", "mailbox-watch-game.mac-web.json")
        self.stamp = os.path.join(self.home, "seen", "game", "mac-web")

    def end_ssh(self):
        try:
            with open(self.pids, encoding="utf-8") as f:
                pids = [int(line) for line in f if line.strip()]
        except FileNotFoundError:
            return
        for pid in pids:
            # only a pid still running the fake ssh: one that ended may be reused by now
            if FAKE_SSH.encode() not in self.command_line(pid):
                continue
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    @staticmethod
    def command_line(pid):
        """pid's command line, b"" once it has ended: /proc where there is one (Linux), else
        ps (macOS has no /proc)."""
        if os.path.isdir("/proc/self"):
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as f:
                    return f.read()
            except FileNotFoundError:
                return b""
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                              check=False).stdout

    def env(self, **extra):
        env = dict(os.environ, PYTHONPATH=VCHARON_DIR, **extra)
        env.pop(watch.WATCH_ENV, None)
        return env

    def once(self, cap=None, **extra):
        """(exit code, stdout lines without their time, stderr, seconds) of one real check;
        cap: ONCE_TIMEOUT for it, set in its own process."""
        argv = [sys.executable, "-m", "vcharon"]
        if cap is not None:
            argv = [sys.executable, "-c", "import sys; from vcharon.mailbox import watch; "
                    "watch.ONCE_TIMEOUT = %d; from vcharon import cli; "
                    "sys.exit(cli.main(sys.argv[1:]))" % cap]
        started = time.monotonic()
        ran = subprocess.run(argv + ["watch", "game", "--once"], stdin=subprocess.DEVNULL,
                             capture_output=True, env=self.env(**extra), timeout=120,
                             check=False)
        took = time.monotonic() - started
        out = ran.stdout.decode("utf-8", "replace").splitlines()
        lines = [l[20:] if l[:4].isdigit() and l[19:20] == " " else l for l in out]
        return ran.returncode, lines, ran.stderr.decode("utf-8", "replace"), took

    def baseline(self):
        """A real --no-stream watcher (every 7 s), until its first round stamped the server
        and saved the snapshot."""
        out_path = os.path.join(self.tmp, "watch.out")
        with open(out_path, "wb") as out:
            proc = subprocess.Popen([sys.executable, "-m", "vcharon", "watch", "game",
                                     "--no-stream", "--every", "7"],
                                    stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, env=self.env(),
                                    start_new_session=True)
        try:
            deadline = time.monotonic() + 60
            while (not (os.path.exists(self.state) and os.path.exists(self.stamp))
                   and time.monotonic() < deadline):
                self.assertIsNone(proc.poll(), "the watcher ended early")
                time.sleep(0.2)
        finally:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait()
        self.assertTrue(os.path.exists(self.state))
        with open(self.stamp, "rb") as f:
            self.assertEqual(f.read(), b"run 7\n")

    def test_quiet_then_refused_then_a_post_once(self):
        # join saved the watcher's snapshot: a check works at once
        code, lines, err, _took = self.once()
        self.assertEqual((code, lines[-1]), (16, "EXIT nothing new"), (lines, err))
        self.assertEqual([l for l in lines if l.startswith("to ")], [])
        # the check stamped at its own pace, --no-stream's default
        with open(self.stamp, "rb") as f:
            self.assertEqual(f.read(), b"run 30\n")
        os.remove(self.state)
        code, lines, err, _took = self.once()
        self.assertEqual(code, 3, (lines, err))
        self.assertIn("ERROR config: --once needs your watcher's saved snapshot: there is none "
                      "for mac-web on this machine", err)
        # the refusal saved no baseline over the entries it would have hidden
        self.assertFalse(os.path.exists(self.state))
        # the watcher's own stamp, not the check's, tells baseline() its first round ran
        os.remove(self.stamp)
        self.baseline()
        code, lines, err, _took = self.once()
        self.assertEqual((code, lines[-1]), (16, "EXIT nothing new"), (lines, err))
        self.use_box("laptop")
        self.ok("post", "game", "--to", "@mac-web", "--title", "for web", "--body", "b",
                "--project", "ui")
        self.use_box("mac")
        code, lines, err, _took = self.once()
        self.assertEqual((code, lines[-1]), (0, "EXIT change"), (lines, err))
        told = [l for l in lines if "for web" in l]
        self.assertEqual(len(told), 1, lines)
        self.assertTrue(told[0].startswith("to you: laptop-ui#"), told)
        # seen once: the next check is quiet
        code, lines, err, _took = self.once()
        self.assertEqual((code, lines[-1]), (16, "EXIT nothing new"), (lines, err))
        self.assertNotIn("for web", "\n".join(lines))

    def test_a_stalled_sync_is_cut_off_at_the_cap(self):
        self.baseline()
        # the ssh hangs before the helper starts; the sync's own hello wait is 30 s, so a
        # cap of 3 s is what ends it
        code, lines, err, took = self.once(cap=3, FAKE_SSH_STALL="60")
        self.assertEqual(code, 11, (lines, err))
        self.assertEqual(lines[-2:], ["ERROR vcharon sync of game.mac-web didn't finish within "
                                      "3 s", "EXIT error"])
        self.assertLess(took, 20)
        # the next check, with the ssh well again, reports the recovery, not a change
        code, lines, err, _took = self.once()
        self.assertEqual((code, lines[-2:]), (16, ["ok again", "EXIT nothing new"]),
                         (lines, err))
