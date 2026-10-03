"""vcharon list, create, join, leave and close, and the channel root: names, the claim,
records, sections, close and leave, the helper's channel calls, and channels.d/ through the
commands."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import vcharon
from vcharon import channel_cmd, channels, config, entries, platform
from vcharon.mailbox import post as post_mod
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

from tests import util
from tests.test_cli import VCHARON_DIR
from tests.test_lock import hold_in_child, stop_child
from tests.util import CAN_SYMLINK, FAKE_SSH, TEST_MACHINE_ID, FakeSshCase, read_tree, write_tree

OTHER_MACHINE = "fedcba9876543210fedcba9876543210"


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
        self.assertIn("OK  created game", out)
        own = os.path.join(self.joined("game.laptop-ui"), "laptop-ui")
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
              ("project", "ui"), ("claimer", claimer)])])
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
        # the JOIN entry is in the own RESULTS.md, to the leader; the next run sends it
        local = self.joined("game.mac-web")
        results = entries.parse_file(os.path.join(local, "mac-web", "RESULTS.md"))
        self.assertEqual([(e.id, e.title, e.to) for e in results],
                         [("mac-web#2", "JOIN", ("@laptop-ui",))])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game", "mac-web"))),
                         ["MEMBER.md"])
        code, out, err = self.run_cli("sync", "game")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game", "mac-web"))),
                         ["MEMBER.md", "RESULTS.md"])

    def test_join_local(self):
        self.lead()
        self.use_box("linux")
        out = self.ok("join", "game", "--local", "--project", "x")
        own = os.path.join(self.root, "game", "linux-x")
        self.assertEqual(sorted(os.listdir(own)), ["MEMBER.md", "RESULTS.md"])
        self.assertIn("entries for linux-x already in game:", out)
        self.assertEqual(self.record("game.linux-x"), {
            "version": 1, "channel": "game", "name": "linux-x", "leader": "laptop-ui",
            "ssh": None, "remote": os.path.join(self.root, "game"),
            "machine": TEST_MACHINE_ID, "project": "x", "role": None,
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
        self.ok("join", "game", "--server", "fake-dest", "--rejoin")
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
        # nothing at the server was replaced; the REJOIN took the next number
        results = entries.parse_file(os.path.join(own, "RESULTS.md"))
        self.assertEqual([e.id for e in results], ["mac-web#2", "mac-web#3",
                                                   "mac-web#4"])
        self.assertEqual([e.title for e in results], ["JOIN", "step done", "REJOIN"])
        self.assertEqual(entries.next_number(own, "mac-web"), 5)
        self.assertEqual(read_tree(os.path.join(self.root, "game", "mac-web")), server_copy)

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
        # the record can't be written: the claim is released
        write_tree(self.vcharon_home, {"state/channels": b"a file in the way"})
        code, _out, _err = self.channel("join", "game", "--server", "fake-dest")
        self.assertNotEqual(code, 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["laptop-ui"])
        self.assertFalse(os.path.exists(os.path.join(self.vcharon_home, "joined")))

    def test_a_failed_run_keeps_the_join(self):
        self.lead()
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            code, out, _err = self.channel("join", "game", "--server", "fake-dest")
        self.assertEqual(code, 4)
        self.assertIn(platform.runnable("vcharon: the sync failed; you are in game: run vcharon "
                                        "sync game --full --project web again"), out)
        self.assertEqual(self.record("game.mac-web")["name"], "mac-web")
        self.assertEqual(self.run_cli("sync", "game", "--full")[0], 0)


def _script(argv):
    return ("import sys\n"
            "sys.path[:0] = [%r]\n"
            "from vcharon import cli, ssh\n"
            "ssh.ssh_prefix = lambda settings: [sys.executable, %r]\n"
            "sys.exit(cli.main(%r))\n" % (VCHARON_DIR, FAKE_SSH, list(argv)))


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
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                channel = "race%d" % attempt
                a, b = self.use_box("a%d" % attempt), self.use_box("b%d" % attempt)
                got = self.both([a, b], ["create", channel, "--server", "fake-dest"])
                codes = sorted(g[0] for g in got)
                self.assertEqual(codes, [0, 1], got)
                loser = next(g for g in got if g[0] == 1)
                self.assertIn("ERROR channel: the channel %s already exists" % channel, loser[2])
                # one leader, no empty channel, no second member
                self.assertEqual(len(os.listdir(os.path.join(self.root, channel))), 1)

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

    def setUp(self):
        ChannelCase.setUp(self)
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

    def test_leave_refusals_change_nothing(self):
        locks = (channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web"),
                 os.path.join(self.homes["mac"], "state", "game.mac-web.down"))
        for lock in locks:
            open(lock + ".lock", "ab").close()
        before = self.everything()
        self.use_box("laptop")
        got, _out, err = self.channel("leave", "game", "--project", "ui")
        self.assertEqual(got, 1)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR channel: you lead game: close it instead",
            "  fix: " + platform.runnable("vcharon close game --project ui")])
        self.use_box("mac")
        for lock in locks:
            with self.subTest(lock=lock):
                child = hold(self, lock + ".lock")
                line = self.refused("leave", "game")
                self.assertTrue(line.startswith("ERROR channel: %s.lock is held" % lock), line)
                self.assertTrue(line.endswith("of mac-web in game)"), line)
                release(child)
        self.assertEqual(self.everything(), before)

    def test_a_failed_run_removes_nothing(self):
        before = self.box_files()
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            code, out, _err = self.channel("leave", "game")
        self.assertEqual(code, 4)
        self.assertIn("nothing was removed", out)
        after = self.box_files()
        # only the LEAVE entry was written
        self.assertEqual(sorted(set(after) ^ set(before)), [])

    def test_leave_after_the_channel_is_gone(self):
        shutil.rmtree(os.path.join(self.root, "game"))
        out = self.ok("leave", "game")
        self.assertIn("  note    game is gone on the server", out)
        self.assertNotIn("channels.d/game.mac-web.ini", self.box_files())
        self.assertFalse(os.path.exists(os.path.join(self.root, "game")))

    def test_local_leave_after_the_channel_is_gone(self):
        # a local member: its server is this machine, not "the server"
        self.use_box("linux")
        self.ok("join", "game", "--local", "--project", "x")
        shutil.rmtree(os.path.join(self.root, "game"))
        out = self.ok("leave", "game", "--project", "x")
        self.assertIn("  note    game is gone on this machine", out.splitlines())
        self.assertNotIn("on the server", out)
        self.assertFalse(os.path.exists(os.path.join(self.homes["linux"], "state", "channels",
                                                     "game.linux-x.json")))

    def test_close_refusals_change_nothing(self):
        self.use_box("laptop")
        lock = channel_cmd.watcher_snapshot(None, "game.laptop-ui", "laptop-ui") + ".lock"
        self.use_box("mac")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        open(lock, "ab").close()
        before = self.everything()
        self.assertEqual(self.refused("close", "game"),
                         "ERROR channel: only the leader closes game, and that is laptop-ui")
        self.use_box("laptop")
        # the watcher lock held: refused before anything on the server changes
        child = hold(self, lock)
        self.assertEqual(self.refusal("close", "game", "--project", "ui")[1],
                         "stop the watcher first")
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

    def test_close(self):
        self.use_box("laptop")
        out = self.ok("close", "game", "--project", "ui")
        self.assertIn("OK  closed game", out)
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
                          platform.runnable("vcharon close docs --project d")))
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

    def test_a_held_post_lock_stays(self):
        # a post running in the own folder: its lock isn't taken from under it (a leave posts
        # no LEAVE once the channel is gone)
        post_lock = entries.lock_path(os.path.join(self.joined("game.mac-web"),
                                                   "mac-web"))
        hold(self, post_lock)
        shutil.rmtree(os.path.join(self.root, "game"))
        out = self.ok("leave", "game")
        self.assertNotIn("  removed %s" % post_lock, out.splitlines())
        self.assertTrue(os.path.isfile(post_lock))
        self.assertFalse(os.path.exists(self.joined("game.mac-web")))

    def test_a_leader_whose_box_lost_its_record(self):
        self.use_box("laptop")
        os.remove(os.path.join(self.homes["laptop"], "state", "channels", "game.laptop-ui.json"))
        self.ok("join", "game", "--server", "fake-dest", "--project", "ui", "--rejoin")
        self.assertEqual(self.record("game.laptop-ui")["leader"], "laptop-ui")
        self.ok("close", "game", "--project", "ui")
        self.assertEqual(os.listdir(self.root), [])


class SkippedTest(ChannelCase):
    """channels.d/ files that are broken, and the retired [mailbox], through the commands."""

    PUSH = textwrap.dedent("""\
        [vcharon]
        box = mac

        [mailbox]
        ssh            = fake-dest
        mailbox.me     = windows
        mailbox.local  = ~/m
        mailbox.remote = m

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
        log = self.vcharon_log()
        self.assertIn("skipped vcharon.ini [mailbox]: [mailbox] is a mailbox section, which "
                      "vcharon.ini doesn't hold: delete it (channel sections live in "
                      "channels.d/ next to it)", log)
        self.assertIn("skipped channels.d/game.mac-x.ini: [game.mac-x]: holds only a channel "
                      "section, with mailbox keys", log)

    def test_naming_one_fails_with_its_error(self):
        for name in ("mailbox", "mailbox.up"):
            with self.subTest(name=name):
                code, _out, err = self.run_jobs(name)
                self.assertEqual(code, 3)
                self.assertEqual(err.splitlines()[0], "ERROR config: [mailbox] is a mailbox "
                                 "section, which vcharon.ini doesn't hold: delete it (channel "
                                 "sections live in channels.d/ next to it)")
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
        self.assertEqual([w.split(":")[0] for w in warns], [
            "warn config skipped vcharon.ini [mailbox]",
            "warn config skipped channels.d/game.mac-x.ini"])


class ReviewTest(ChannelCase):
    """Probes of the channel commands as tests, and tests that kill mutants of them that
    other tests let survive."""

    def member(self):
        """The leader laptop-ui (remote), and this box's member mac-web with three entries
        and a patch, all sent."""
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.local = self.joined("game.mac-web")
        self.own = os.path.join(self.local, "mac-web")
        for i in range(3):
            entries.post(os.path.join(self.own, "RESULTS.md"), self.own, "mac-web",
                         "step %d" % i, ["@laptop-ui"], body="b")
        with open(os.path.join(self.own, "work.patch"), "w") as f:
            f.write("patch")
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        self.srv_own = os.path.join(self.root, "game", "mac-web")
        self.srv_before = read_tree(self.srv_own)
        self.assertEqual(sorted(self.srv_before), ["MEMBER.md", "RESULTS.md", "work.patch"])

    # --- blockers 1 and 2: the rejoin pull ---

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
        # nothing at the server was pruned; the stray stays here (and is sent up)
        for name, data in self.srv_before.items():
            self.assertEqual(read_tree(self.srv_own)[name], data)
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
        # an old record without the parts: a placeholder, never flags for another name
        path = channel_cmd.record_path("game", "mac-web-b")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        del doc["project"], doc["role"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.assertEqual(channel_cmd.read_record("game", "mac-web-b")["name"], "mac-web-b")
        self.assertEqual(channel_cmd.name_flags("game", "mac-web-b"),
                         "<the --project and --role that make mac-web-b>")

    # --- warnings 3, 4, 7, 8 ---

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
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            for attempt in range(2):
                self.assertEqual(self.channel("leave", "game")[0], 4)
        titles = [e.title for e in entries.parse_file(os.path.join(self.own, "RESULTS.md"))]
        self.assertEqual(titles.count("LEAVE"), 1)
        self.ok("leave", "game")
        titles = [e.title for e in entries.parse_file(os.path.join(self.srv_own, "RESULTS.md"))]
        self.assertEqual(titles.count("LEAVE"), 1)

    # --- warning 6, note 9 ---

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
        self.member()
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.local, alias)
        self.assertEqual(entries.lock_path(self.own),
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

    # --- cases that kill mutants other tests let survive ---

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

    def test_releasing_the_last_member_removes_the_channel(self):
        write_tree(self.root, {"game/lead/MEMBER.md": b"m", "game/lead/CHANNEL.md": b"c"})
        self.assertEqual(channels.release(self.root, "game", "lead"), {"removed": True})
        self.assertEqual(os.listdir(self.root), [])

    def test_a_failed_rejoin_keeps_the_folder(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # the server folder holds only MEMBER.md: what release would take
        srv = os.path.join(self.root, "game", "mac-web")
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

    def test_same_or_no_claimer_is_the_record_or_rejoin(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # the same machine, another state folder: the same claimer, but no record
        self.use_box("mac2")
        with open(os.path.join(self.homes["mac2"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = mac\n")
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest"),
                         "ERROR channel: the name mac-web is taken in game")
        out = self.ok("join", "game", "--server", "fake-dest", "--rejoin")
        self.assertIn("took back game/mac-web", out)
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

    def test_a_failed_first_push_gets_back_in_through_the_record(self):
        self.lead()
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            self.assertEqual(self.channel("join", "game", "--server", "fake-dest")[0], 4)
        # no MEMBER.md at the server: no claimer
        self.assertEqual(os.listdir(os.path.join(self.root, "game", "mac-web")), [])
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("took back game/mac-web", out)
        self.assertEqual(self.server_member()["claimer"], platform.claimer("game"))

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
        for env, want in (({"CLAUDECODE": "1"}, "claude"), ({"CODEX_THREAD_ID": "t"}, "codex"),
                          ({"OPENCODE": "1"}, "opencode"),
                          ({"CLAUDECODE": "1", "OPENCODE": "1"}, "other"), ({}, "other")):
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
                                          "os", "project"])
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
