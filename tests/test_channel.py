"""ferry channel and the channel root (M10): names, the claim, records, sections, close and
leave, the helper's channel calls, and channels.d/ through the commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from vcharon import channel_cmd, channels, config, entries, platform
from vcharon.proto import VCharonError

from tests import util
from tests.test_cli import FERRY_DIR
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
    """One fake server (its root under the fake home) and boxes, each a FERRY_HOME of its own
    with [ferry] box set. The current directory is a git checkout named Web."""

    def setUp(self):
        FakeSshCase.setUp(self)
        self.root = os.path.join(self.home, "channels")
        os.environ["FERRY_CHANNELS_ROOT"] = self.root
        self.homes = {}
        self.use_box("mac", self.ferry_home)
        self.project = os.path.join(self.tmp, "work", "Web")
        os.makedirs(os.path.join(self.project, ".git"))
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    def use_box(self, box, home=None):
        """Switches to the box named box (a FERRY_HOME of its own, made on first use)."""
        if box not in self.homes:
            home = home or os.path.join(self.tmp, "box-" + box)
            os.makedirs(home, exist_ok=True)
            with open(os.path.join(home, "ferry.ini"), "w", encoding="utf-8") as f:
                f.write("[ferry]\nbox = %s\n" % box)
            self.homes[box] = home
        os.environ["FERRY_HOME"] = self.homes[box]
        return self.homes[box]

    def channel(self, *argv):
        return self.run_cli("channel", *argv)

    def ok(self, *argv):
        code, out, err = self.channel(*argv)
        self.assertEqual(code, 0, out + err)
        return out

    def refused(self, *argv, code=1):
        got, out, err = self.channel(*argv)
        self.assertEqual(got, code, out + err)
        return err.splitlines()[0]

    def server_tree(self):
        return read_tree(self.root) if os.path.isdir(self.root) else {}

    def joined(self, section, box=None):
        """A remote member's local tree on the current box."""
        return os.path.join(os.environ["FERRY_HOME"], "joined", section)

    def record(self, section):
        with open(os.path.join(os.environ["FERRY_HOME"], "state", "channels",
                               section + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def lead(self, channel="game", where=("--ssh", "fake-dest"), box="laptop", project="ui"):
        """box creates channel and leads it; back to the mac box after."""
        self.use_box(box)
        self.ok("create", channel, *where, "--project", project)
        self.use_box("mac")
        return "%s-%s" % (box, project)


class NamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ferry-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cfg(self, box="mac"):
        return config.Config("/x/ferry.ini", True, config.Settings(), box=box)

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
        with self.assertRaises(VCharonError) as cm:
            channel_cmd.member_name(self.cfg(None), project="ui")
        self.assertEqual(cm.exception.message, channel_cmd.BOX_HINT)


class CreateJoinTest(ChannelCase):
    """create and join, remote members over the fake ssh and server members (--local)."""

    def test_create_remote(self):
        os.environ.pop("FERRY_CHANNELS_ROOT")
        self.root = os.path.join(self.home, ".local", "state", "ferry", "channels")
        self.use_box("laptop")
        out = self.ok("create", "game", "--ssh", "fake-dest", "--project", "ui")
        self.assertIn("OK  created game", out)
        own = os.path.join(self.joined("game.laptop-ui"), "laptop-ui")
        # the record, the section, the local tree; the run pushed both files
        self.assertEqual(self.record("game.laptop-ui"), {
            "version": 1, "channel": "game", "name": "laptop-ui", "leader": "laptop-ui",
            "ssh": "fake-dest", "remote": "~/.local/state/ferry/channels/game",
            "machine": TEST_MACHINE_ID, "project": "ui", "role": None})
        with open(os.path.join(self.homes["laptop"], "channels.d", "game.laptop-ui.ini"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), textwrap.dedent("""\
                [game.laptop-ui]
                ssh            = fake-dest
                mailbox.me     = laptop-ui
                mailbox.leader = laptop-ui
                mailbox.local  = %s
                mailbox.remote = ~/.local/state/ferry/channels/game
                """) % self.joined("game.laptop-ui"))
        self.assertEqual(sorted(os.listdir(own)), ["CHANNEL.md", "MEMBER.md"])
        self.assertEqual(sorted(read_tree(self.root)), [
            "game/", "game/laptop-ui/", "game/laptop-ui/CHANNEL.md", "game/laptop-ui/MEMBER.md"])
        member = entries.parse_file(os.path.join(own, "MEMBER.md"))
        self.assertEqual([(e.id, e.title, e.to, e.header) for e in member], [
            ("laptop-ui#1", "member", ("@laptop-ui",),
             [("channel", "game"), ("name", "laptop-ui"), ("leader", "laptop-ui")])])
        ch = entries.parse_file(os.path.join(own, "CHANNEL.md"))
        self.assertEqual([(e.id, e.title, e.to) for e in ch],
                         [("laptop-ui#2", "channel game created", ("@all",))])
        self.assertEqual([k for k, v in ch[0].header],
                         ["leader", "server host", "created", "rules"])
        self.assertEqual(dict(ch[0].header)["rules"], channel_cmd.RULES)
        # ferry is self-contained: the rules are found by the ferry folder, not a checkout
        self.assertEqual(channel_cmd.RULES, "MAILBOX.md in the ferry folder (the ferry-mailbox skill)")

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
        out = self.ok("join", "game", "--ssh", "fake-dest")
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
        code, out, err = self.run_cli("run", "game.mac-web")
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
            "machine": TEST_MACHINE_ID, "project": "x", "role": None})

    def test_the_same_name_in_two_channels(self):
        self.lead("game")
        self.lead("docs", box="win", project="d")
        self.ok("join", "game", "--ssh", "fake-dest")
        self.ok("join", "docs", "--ssh", "fake-dest")
        self.assertEqual(sorted(os.listdir(os.path.join(self.homes["mac"], "channels.d"))),
                         ["docs.mac-web.ini", "game.mac-web.ini"])
        for channel in ("game", "docs"):
            self.assertTrue(os.path.isdir(os.path.join(self.root, channel, "mac-web")))

    def test_box_missing(self):
        with open(os.path.join(self.ferry_home, "ferry.ini"), "w") as f:
            f.write("[ferry]\n")
        got, out, err = self.channel("create", "game", "--ssh", "fake-dest")
        self.assertEqual(got, 3)
        self.assertEqual(err.splitlines()[0], "ERROR config: " + channel_cmd.BOX_HINT)
        self.assertFalse(os.path.exists(self.root))

    def test_create_twice_and_bad_names(self):
        self.lead()
        self.use_box("win")
        self.assertEqual(self.refused("create", "game", "--ssh", "fake-dest"),
                         "ERROR channel: the channel game already exists: join it, or pick "
                         "another name")
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "win-web")))
        for name in ("Game", "a.b", "x" * 25, "con", "..", ".ferry-closed-x"):
            with self.subTest(name=name):
                self.assertTrue(self.refused("create", name, "--local", code=3).startswith(
                    "ERROR config: "))
        self.assertEqual(sorted(os.listdir(self.root)), ["game"])

    def test_no_channel_or_no_leader_refused_before_the_claim(self):
        self.assertEqual(self.refused("join", "game", "--ssh", "fake-dest"),
                         "ERROR channel: there is no channel game on fake-dest: check its name "
                         "(ferry channel list)")
        write_tree(self.root, {"game/a/MEMBER.md": b"m"})
        self.assertEqual(self.refused("join", "game", "--ssh", "fake-dest"),
                         "ERROR channel: game has no leader (no member's folder holds "
                         "CHANNEL.md): ask the user")
        write_tree(self.root, {"game/a/CHANNEL.md": b"c", "game/b/CHANNEL.md": b"c"})
        self.assertEqual(self.refused("join", "game", "--local"),
                         "ERROR channel: game has 2 leaders (a/, b/ hold CHANNEL.md): ask the "
                         "user")
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["a", "b"])
        self.assertFalse(os.path.exists(os.path.join(self.ferry_home, "state", "channels")))
        self.assertFalse(os.path.exists(os.path.join(self.ferry_home, "joined")))

    def test_a_name_taken_rejoin_and_another_server(self):
        self.lead()
        self.ok("join", "game", "--ssh", "fake-dest")
        # the same name from another box: told to pass --role
        self.use_box("mac2", os.path.join(self.tmp, "second-mac"))
        with open(os.path.join(self.homes["mac2"], "ferry.ini"), "w") as f:
            f.write("[ferry]\nbox = mac\n")
        self.assertEqual(self.refused("join", "game", "--ssh", "fake-dest"),
                         "ERROR channel: the name mac-web is taken in game: pass --role")
        self.assertFalse(os.path.exists(os.path.join(self.homes["mac2"], "state", "channels")))
        self.ok("join", "game", "--ssh", "fake-dest", "--role", "b")
        # this box has the record: a rejoin, which takes the folder back
        self.use_box("mac")
        out = self.ok("join", "game", "--ssh", "fake-dest")
        self.assertIn("  took back game/mac-web; the leader is laptop-ui", out)
        # the record is for another server: refused before any claim
        os.environ["FERRY_TEST_MACHINE_ID"] = OTHER_MACHINE
        self.assertEqual(self.refused("join", "game", "--ssh", "fake-dest"),
                         "ERROR channel: you are in game on another server: pass --role")
        os.environ["FERRY_TEST_MACHINE_ID"] = TEST_MACHINE_ID
        # with --rejoin and no record: the user said it's this agent's
        os.remove(os.path.join(self.homes["mac"], "state", "channels", "game.mac-web.json"))
        self.assertEqual(self.refused("join", "game", "--ssh", "fake-dest"),
                         "ERROR channel: the name mac-web is taken in game: pass --role")
        self.ok("join", "game", "--ssh", "fake-dest", "--rejoin")
        self.assertEqual(self.record("game.mac-web")["leader"], "laptop-ui")

    def test_a_remote_end_must_be_linux(self):
        # M11a: a Mac or Windows box has a machine id now, so the client refuses it by its OS
        refused = ("ERROR state_mismatch: fake-dest runs %s: only a Linux server is supported "
                   "as a remote end")
        fix = "  fix: only a Linux server takes remote members: check the alias"
        self.use_box("laptop")
        os.environ["FERRY_TEST_OS"] = "darwin"
        code, out, err = self.channel("create", "game", "--ssh", "fake-dest", "--project", "ui")
        self.assertEqual((code, err.splitlines()[:2]), (3, [refused % "darwin", fix]))
        self.assertEqual(self.server_tree(), {})
        self.assertFalse(os.path.exists(channel_cmd.record_path("game", "laptop-ui")))
        os.environ["FERRY_TEST_OS"] = "linux"
        self.lead()
        os.environ["FERRY_TEST_OS"] = "windows"
        self.assertEqual(self.refused("join", "game", "--ssh", "fake-dest", code=3),
                         refused % "windows")
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "mac-web")))
        os.environ["FERRY_TEST_OS"] = "linux"
        self.ok("join", "game", "--ssh", "fake-dest")
        os.environ["FERRY_TEST_OS"] = "darwin"
        before = self.server_tree()
        code, out, err = self.channel("leave", "game")
        self.assertEqual((code, err.splitlines()[:2]), (3, [refused % "darwin", fix]))
        self.assertEqual(self.server_tree(), before)
        self.assertTrue(os.path.exists(channel_cmd.record_path("game", "mac-web")))

    def test_no_machine_id_here_has_its_os_hint(self):
        hint = "ferry couldn't read this Mac's IOPlatformUUID (ioreg): ask the user"
        with mock.patch.object(channel_cmd.platform, "machine_id", return_value=None), \
                mock.patch.object(channel_cmd.platform, "no_machine_hint", return_value=hint):
            code, out, err = self.channel("create", "game", "--local", "--project", "ui")
        self.assertEqual((code, err.splitlines()[:2]),
                         (3, ["ERROR state_mismatch: this machine has no machine id, so ferry "
                              "can't tie a channel's record to it", "  fix: " + hint]))
        self.assertEqual(self.server_tree(), {})

    def test_rejoin_without_a_local_tree_pulls_the_own_folder(self):
        self.lead()
        self.ok("join", "game", "--ssh", "fake-dest")
        local = self.joined("game.mac-web")
        own = os.path.join(local, "mac-web")
        entries.post(os.path.join(own, "RESULTS.md"), own, "mac-web", "step done",
                     ["@laptop-ui"], body="b")
        self.assertEqual(self.run_cli("run", "game.mac-web")[0], 0)
        server_copy = read_tree(os.path.join(self.root, "game", "mac-web"))
        # the box loses its tree; the record and the section stay
        shutil.rmtree(local)
        out = self.ok("join", "game", "--ssh", "fake-dest")
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
        refused = ("ERROR channel: a live session holds mac-web in game: if that watcher is "
                   "yours, keep using it; else pass --role")
        self.assertEqual(self.refused("join", "game", "--ssh", "fake-dest"), refused)
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "mac-web")))
        # a server member's: the server mode's lock, on the channel folder as main keys it
        hold(self, channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web",
                                                os.path.join(self.root, "game")) + ".lock")
        self.assertEqual(self.refused("join", "game", "--local"), refused)

    def test_the_server_lock_name_is_the_watchers(self):
        # ~/…, ./… and the absolute form give one lock name (main's abspath(expanduser))
        # (the real path: on macOS the temp dir is under /var, a link to /private/var, and
        # getcwd() gives the /private form)
        tool = channel_cmd._watch_tool()
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
        write_tree(self.ferry_home, {"state/channels": b"a file in the way"})
        code, out, err = self.channel("join", "game", "--ssh", "fake-dest")
        self.assertNotEqual(code, 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["laptop-ui"])
        self.assertFalse(os.path.exists(os.path.join(self.ferry_home, "joined")))

    def test_a_failed_run_keeps_the_join(self):
        self.lead()
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            code, out, err = self.channel("join", "game", "--ssh", "fake-dest")
        self.assertEqual(code, 4)
        self.assertIn(platform.runnable("ferry: the run failed; you are in game: run ferry run "
                                        "game.mac-web --full again"), out)
        self.assertEqual(self.record("game.mac-web")["name"], "mac-web")
        self.assertEqual(self.run_cli("run", "game.mac-web", "--full")[0], 0)


def _script(argv):
    return ("import sys\n"
            "sys.path[:0] = [%r]\n"
            "from vcharon import cli, ssh\n"
            "ssh.ssh_prefix = lambda settings: [sys.executable, %r]\n"
            "sys.exit(cli.main(%r))\n" % (FERRY_DIR, FAKE_SSH, list(argv)))


class ConcurrencyTest(ChannelCase):
    """Two ferry processes at once, each with its own fake ssh and helper: the claim's one
    mkdir lets exactly one win."""

    def both(self, homes, argv):
        procs = []
        for home in homes:
            env = dict(os.environ, FERRY_HOME=home)
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
                got = self.both([a, b], ["channel", "create", channel, "--ssh", "fake-dest"])
                codes = sorted(g[0] for g in got)
                self.assertEqual(codes, [0, 1], got)
                loser = [g for g in got if g[0] == 1][0]
                self.assertIn("ERROR channel: the channel %s already exists" % channel, loser[2])
                # one leader, no empty channel, no second member
                self.assertEqual(len(os.listdir(os.path.join(self.root, channel))), 1)

    def test_two_joins_with_one_name_from_two_boxes(self):
        self.lead()
        a = self.homes["mac"]
        b = os.path.join(self.tmp, "second-mac")
        os.makedirs(b)
        with open(os.path.join(b, "ferry.ini"), "w") as f:
            f.write("[ferry]\nbox = mac\n")
        got = self.both([a, b], ["channel", "join", "game", "--ssh", "fake-dest"])
        self.assertEqual(sorted(g[0] for g in got), [0, 1], got)
        loser = [g for g in got if g[0] == 1][0]
        self.assertIn("ERROR channel: the name mac-web is taken in game: pass --role",
                      loser[2])
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
        self.assertEqual(got["existed"], False)
        self.assertEqual(got["machine"], TEST_MACHINE_ID)
        self.assertTrue(got["host"])
        # the helper's own root text, FERRY_CHANNELS_ROOT here
        self.assertEqual(got["root"], self.root)
        with self.assertRaises(VCharonError) as cm:
            s.call("channel.claim", {"channel": "..", "name": "a", "create": False})
        self.assertEqual(cm.exception.code, "channel")
        with self.assertRaises(VCharonError) as cm:
            s.call("channel.claim", {"channel": "game", "name": "a"})
        self.assertEqual(cm.exception.code, "protocol")

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
                               "Bad/": None, ".ferry-closed-old-1/": None})
        self.lead("docs", where=("--local",))
        out = self.ok("list", "--ssh", "fake-dest")
        lines = out.splitlines()
        self.assertEqual(lines[0], "ferry: channel list  (fake-dest)")
        game = [l for l in lines if l.startswith("  game  ")][0]
        self.assertIn("  leader lead  members a, lead  newest ", game)
        self.assertIn("    note: notes.md at its top isn't a member's folder", lines)
        self.assertTrue(any(l.startswith("  two  leader ?") for l in lines), out)
        self.assertIn("    note: x/, y/ all hold CHANNEL.md: ask the user", lines)
        self.assertTrue(any(l.startswith("  empty  leader ?  members none  newest -")
                            for l in lines), out)
        self.assertIn("  note: .ferry-closed-old-1: a closed channel that wasn't deleted", lines)
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
        self.ok("join", "game", "--ssh", "fake-dest")
        # a second member on the same box, in the same channel, and its run's files
        self.ok("join", "game", "--ssh", "fake-dest", "--role", "b")
        self.assertEqual(self.run_cli("run", "game.mac-web")[0], 0)
        self.assertEqual(self.run_cli("run", "game.mac-web-b")[0], 0)

    def box_files(self, box="mac"):
        return sorted(p for p in read_tree(self.homes[box]) if not p.startswith("logs/ferry.log"))

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
        self.assertEqual(self.run_cli("run", "game.mac-web-b")[0], 0)
        # the server folder stays: the member's history, its name taken
        self.assertTrue(os.path.isdir(os.path.join(self.root, "game", "mac-web")))
        self.assertEqual(self.refused("leave", "game"),
                         "ERROR channel: you aren't in game as mac-web (no record at %s)"
                         % os.path.join(self.homes["mac"], "state", "channels",
                                        "game.mac-web.json"))

    def test_leave_refusals_change_nothing(self):
        locks = (channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web"),
                 os.path.join(self.homes["mac"], "state", "game.mac-web.down"))
        for lock in locks:
            open(lock + ".lock", "ab").close()
        before = self.everything()
        self.use_box("laptop")
        self.assertEqual(self.refused("leave", "game", "--project", "ui"),
                         "ERROR channel: you lead game: close it instead (ferry channel close "
                         "game --project ui)")
        self.use_box("mac")
        for lock in locks:
            with self.subTest(lock=lock):
                child = hold(self, lock + ".lock")
                line = self.refused("leave", "game")
                self.assertTrue(line.startswith("ERROR channel: %s.lock is held" % lock), line)
                self.assertTrue(line.endswith("stop the watcher first"), line)
                release(child)
        self.assertEqual(self.everything(), before)

    def test_a_failed_run_removes_nothing(self):
        before = self.box_files()
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            code, out, err = self.channel("leave", "game")
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
        # a local member: its server is this machine, not "the server" (M11d)
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
        self.assertTrue(self.refused("close", "game", "--project", "ui").endswith(
            "stop the watcher first"))
        release(child)
        # another server than the record's
        os.environ["FERRY_TEST_MACHINE_ID"] = OTHER_MACHINE
        self.assertEqual(self.refused("close", "game", "--project", "ui"),
                         "ERROR channel: fake-dest isn't the server game is on (its machine id "
                         "is %s, the record's %s): check the alias" % (OTHER_MACHINE,
                                                                        TEST_MACHINE_ID))
        os.environ["FERRY_TEST_MACHINE_ID"] = TEST_MACHINE_ID
        # a second CHANNEL.md, then a stray file at the top: the server refuses
        write_tree(self.root, {"game/mac-web/CHANNEL.md": b"c"})
        self.assertEqual(self.refused("close", "game", "--project", "ui"),
                         "ERROR channel: game has 2 leaders (laptop-ui/, mac-web/ hold "
                         "CHANNEL.md): ask the user")
        os.remove(os.path.join(self.root, "game", "mac-web", "CHANNEL.md"))
        write_tree(self.root, {"game/notes.md": b"n"})
        self.assertEqual(self.refused("close", "game", "--project", "ui"),
                         "ERROR channel: game holds notes.md at its top, not a member's folder: "
                         "ask the user, and remove it first")
        os.remove(os.path.join(self.root, "game", "notes.md"))
        self.assertEqual(self.everything(), before)

    def test_close(self):
        self.use_box("laptop")
        out = self.ok("close", "game", "--project", "ui")
        self.assertIn("OK  closed game", out)
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse(any("game.laptop-ui" in p for p in self.box_files("laptop")),
                         self.box_files("laptop"))
        # a member's post and run make no channel again: up never creates (M10)
        self.use_box("mac")
        own = os.path.join(self.joined("game.mac-web"), "mac-web")
        entries.post(os.path.join(own, "RESULTS.md"), own, "mac-web", "late", ["@laptop-ui"],
                     body="x")
        code, out, err = self.run_cli("run", "game.mac-web")
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(self.root), [])
        fixes = [l for l in err.splitlines() if l.startswith("  fix: ")]
        # the leave command as this box runs ferry (M14a)
        self.assertEqual(fixes, ["  fix: " + platform.runnable(
            "the channel is closed, or your folder in it is gone: ferry channel leave game "
            "--project web")] * 2)
        self.assertNotIn("create it", err)
        # its sign to leave
        out = self.ok("leave", "game")
        self.assertIn("game is gone on the server", out)
        self.assertEqual(os.listdir(self.root), [])

    def test_close_of_a_server_led_channel(self):
        self.lead("docs", where=("--local",), box="linux", project="d")
        self.use_box("linux")
        self.assertEqual(self.refused("leave", "docs", "--project", "d"),
                         "ERROR channel: you lead docs: close it instead (ferry channel close "
                         "docs --project d)")
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
        self.assertEqual(os.listdir(os.path.join(self.homes["linux"], "state")), ["channels"])

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
        self.ok("join", "game", "--ssh", "fake-dest", "--project", "ui", "--rejoin")
        self.assertEqual(self.record("game.laptop-ui")["leader"], "laptop-ui")
        self.ok("close", "game", "--project", "ui")
        self.assertEqual(os.listdir(self.root), [])


class SkippedTest(ChannelCase):
    """channels.d/ files that are broken, and the retired [mailbox], through the commands."""

    PUSH = textwrap.dedent("""\
        [ferry]
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
        with open(os.path.join(self.ferry_home, "ferry.ini"), "w") as f:
            f.write(self.PUSH.format(src=self.src))
        write_tree(self.ferry_home, {"channels.d/game.mac-x.ini": b"[game.mac-x]\nssh = \n"})

    def ferry_log(self):
        with open(os.path.join(self.ferry_home, "logs", "ferry.log"), encoding="utf-8") as f:
            return f.read()

    def test_other_jobs_run(self):
        code, out, err = self.run_cli("run", "push")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        log = self.ferry_log()
        self.assertIn("skipped ferry.ini [mailbox]: the fixed mailbox is retired (M10): delete "
                      "[mailbox] from ferry.ini (MAILBOX.md in the ferry folder, \"Retired\")", log)
        self.assertIn("skipped channels.d/game.mac-x.ini: [game.mac-x]: holds only a channel "
                      "section, with mailbox keys", log)

    def test_naming_one_fails_with_its_error(self):
        for name in ("mailbox", "mailbox.up"):
            with self.subTest(name=name):
                code, out, err = self.run_cli("run", name)
                self.assertEqual(code, 3)
                self.assertEqual(err.splitlines()[0], "ERROR config: the fixed mailbox is "
                                 "retired (M10): delete [mailbox] from ferry.ini "
                                 "(MAILBOX.md in the ferry folder, \"Retired\")")
        code, out, err = self.run_cli("run", "game.mac-x.down")
        self.assertEqual(code, 3)
        self.assertTrue(err.startswith("ERROR config: channels.d/game.mac-x.ini [game.mac-x]: "
                                       "holds only a channel section"), err)
        self.assertIn("  fix: fix %s, or delete it" % os.path.join(
            self.ferry_home, "channels.d", "game.mac-x.ini"), err)

    def test_doctor_lists_them(self):
        os.environ.pop("SSH_AUTH_SOCK", None)
        code, out, err = self.run_cli("doctor", "push")
        self.assertEqual(code, 0, out)
        warns = [" ".join(l.split()) for l in out.splitlines() if l.startswith("  warn  config")]
        self.assertEqual([w.split(":")[0] for w in warns], [
            "warn config skipped ferry.ini [mailbox]",
            "warn config skipped channels.d/game.mac-x.ini"])
        code, out, err = self.run_cli("doctor", "game.mac-x")
        self.assertEqual(code, 1, out)
        fails = [" ".join(l.split()) for l in out.splitlines() if l.startswith("  FAIL")]
        self.assertEqual(fails, ["FAIL config channels.d/game.mac-x.ini [game.mac-x]: holds "
                                 "only a channel section, with mailbox keys"])


POST_TOOL = os.path.join(os.path.dirname(FERRY_DIR), "tools", "mailbox_post.py")


class ReviewTest(ChannelCase):
    """The M10 review's findings: its probes as tests, and tests that kill its surviving
    mutants."""

    def member(self):
        """The leader laptop-ui (remote), and this box's member mac-web with three entries
        and a patch, all sent."""
        self.lead()
        self.ok("join", "game", "--ssh", "fake-dest")
        self.local = self.joined("game.mac-web")
        self.own = os.path.join(self.local, "mac-web")
        for i in range(3):
            entries.post(os.path.join(self.own, "RESULTS.md"), self.own, "mac-web",
                         "step %d" % i, ["@laptop-ui"], body="b")
        with open(os.path.join(self.own, "work.patch"), "w") as f:
            f.write("patch")
        self.assertEqual(self.run_cli("run", "game.mac-web")[0], 0)
        self.srv_own = os.path.join(self.root, "game", "mac-web")
        self.srv_before = read_tree(self.srv_own)
        self.assertEqual(sorted(self.srv_before), ["MEMBER.md", "RESULTS.md", "work.patch"])

    # --- blockers 1 and 2: the rejoin pull ---

    def test_a_failed_pull_leaves_no_own_folder(self):
        self.member()
        shutil.rmtree(self.local)
        lost = VCharonError("lost", "the connection closed (test)")
        with mock.patch.object(channel_cmd.engine.Engine, "run", side_effect=lost):
            code, out, err = self.channel("join", "game", "--ssh", "fake-dest")
        self.assertEqual(code, 1, err)
        self.assertFalse(os.path.exists(self.own))
        base = os.path.join(self.homes["mac"], "joined")
        self.assertEqual([f for f in os.listdir(base) if f.startswith(".ferry-pull-")], [])
        # the next plain run refuses (up has sent files from the missing folder), and the
        # server's copy stays whole
        code, out, err = self.run_cli("run", "game.mac-web")
        self.assertEqual(code, 1)
        self.assertIn("not_found: the mailbox's own folder %s is gone" % self.own, err)
        self.assertEqual(read_tree(self.srv_own), self.srv_before)
        # a join that works takes it all back
        out = self.ok("join", "game", "--ssh", "fake-dest")
        self.assertIn("  pulled your folder from the server: 3 files added, 0 already here",
                      out)
        self.assertEqual(read_tree(self.srv_own)["work.patch"], b"patch")
        self.assertEqual(sorted(read_tree(self.own)), ["MEMBER.md", "RESULTS.md", "work.patch"])

    def test_a_stray_file_doesnt_skip_the_pull(self):
        self.member()
        shutil.rmtree(self.own)
        write_tree(self.own, {".DS_Store": b"x"})
        # a plain run first: MEMBER.md is gone while up has sent it, so it refuses
        code, out, err = self.run_cli("run", "game.mac-web")
        self.assertEqual(code, 1)
        self.assertIn("has no MEMBER.md, which game.mac-web.up has sent", err)
        self.assertEqual(read_tree(self.srv_own), self.srv_before)
        out = self.ok("join", "game", "--ssh", "fake-dest")
        self.assertIn("  pulled your folder from the server: 3 files added, 0 already here",
                      out)
        # nothing at the server was pruned; the stray stays here (and is sent up)
        for name, data in self.srv_before.items():
            self.assertEqual(read_tree(self.srv_own)[name], data)
        self.assertEqual(read_tree(self.own)[".DS_Store"], b"x")

    def test_needs_pull_goes_by_ups_state(self):
        self.member()
        self.assertFalse(channel_cmd.needs_pull("game.mac-web", self.own))
        out = self.ok("join", "game", "--ssh", "fake-dest")
        self.assertNotIn("pulled", out)
        # the re-review's Q7: a file deleted on purpose, MEMBER.md still here. Every new
        # session rejoins, so the rejoin must not bring it back; its run deletes it at the
        # server
        os.remove(os.path.join(self.own, "work.patch"))
        self.assertFalse(channel_cmd.needs_pull("game.mac-web", self.own))
        out = self.ok("join", "game", "--ssh", "fake-dest")
        self.assertNotIn("pulled", out)
        self.assertFalse(os.path.exists(os.path.join(self.own, "work.patch")))
        self.assertNotIn("work.patch", os.listdir(self.srv_own))
        # MEMBER.md gone while up has sent it: the tree was lost, so pull; a pull only adds,
        # so a file here is never replaced by the server's
        os.remove(os.path.join(self.own, "MEMBER.md"))
        with open(os.path.join(self.own, "RESULTS.md"), "a") as f:
            f.write("local edit\n")
        self.assertTrue(channel_cmd.needs_pull("game.mac-web", self.own))
        out = self.ok("join", "game", "--ssh", "fake-dest")
        self.assertIn("  pulled your folder from the server: 1 files added, 1 already here",
                      out)
        self.assertIn("local edit", read_tree(self.own)["RESULTS.md"].decode())
        self.assertTrue(os.path.isfile(os.path.join(self.own, "MEMBER.md")))
        # no up state: always pull
        self.run_cli("state", "reset", "game.mac-web.up")
        self.assertTrue(channel_cmd.needs_pull("game.mac-web", self.own))

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_the_pull_never_writes_through_a_link(self):
        # the re-review's Q2: own/sub replaced by a link to a folder outside the tree
        self.member()
        write_tree(self.own, {"sub/a.txt": b"a", "sub/b.txt": b"b"})
        self.assertEqual(self.run_cli("run", "game.mac-web")[0], 0)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        shutil.rmtree(os.path.join(self.own, "sub"))
        os.symlink(outside, os.path.join(self.own, "sub"))
        os.remove(os.path.join(self.own, "MEMBER.md"))
        code, out, err = self.channel("join", "game", "--ssh", "fake-dest")
        self.assertEqual(code, 1, out)
        self.assertEqual(err.splitlines()[:2], [
            "ERROR unsafe_path: %s is a symlink: the pull into your own folder doesn't go "
            "through it" % os.path.join(self.own, "sub"), "  fix: " + channel_cmd.LINK_HINT])
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.local))
                          if f.startswith(".ferry-pull-")], [])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "needs POSIX modes, not root")
    def test_a_failed_move_is_a_plain_error(self):
        # the re-review's Q3: a folder of the own tree that can't be written
        self.member()
        write_tree(self.own, {"sub/x.txt": b"x"})
        self.assertEqual(self.run_cli("run", "game.mac-web")[0], 0)
        os.remove(os.path.join(self.own, "sub", "x.txt"))
        os.remove(os.path.join(self.own, "MEMBER.md"))
        sub = os.path.join(self.own, "sub")
        os.chmod(sub, 0o555)
        self.addCleanup(os.chmod, sub, 0o755)
        code, out, err = self.channel("join", "game", "--ssh", "fake-dest")
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("ERROR permission: %s: " % sub), err)
        # what moved before the failure stays (MEMBER.md sorts before sub/)
        self.assertTrue(os.path.isfile(os.path.join(self.own, "MEMBER.md")))
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.local))
                          if f.startswith(".ferry-pull-")], [])

    def test_hints_rebuild_a_role_members_name(self):
        # the re-review's Q6: the -b member's fix line must not claim mac-web
        self.lead()
        self.ok("join", "game", "--ssh", "fake-dest", "--role", "b")
        own = os.path.join(self.joined("game.mac-web-b"), "mac-web-b")
        self.assertEqual(self.run_cli("run", "game.mac-web-b")[0], 0)
        self.assertEqual((self.record("game.mac-web-b")["project"],
                          self.record("game.mac-web-b")["role"]), ("web", "b"))
        os.remove(os.path.join(own, "MEMBER.md"))
        code, out, err = self.run_cli("run", "game.mac-web-b")
        fix = [l for l in err.splitlines() if l.startswith("  fix: ")][0]
        self.assertEqual(fix, "  fix: " + platform.runnable(
            "ferry channel join game --ssh fake-dest --project web --role b takes its files "
            "back from the server (a rejoin); MEMBER.md is ferry's: to drop other files, delete "
            "them one by one and keep it"))
        # followed as printed: it takes back mac-web-b, and claims nothing new
        command = fix.split("fix: ", 1)[1]
        self.assertTrue(command.startswith(platform.ferry_command() + " channel "), command)
        argv = command[len(platform.ferry_command()):].split(" takes ")[0].split()[1:]
        out = self.ok(*argv)
        self.assertIn("  took back game/mac-web-b", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "mac-web")))
        self.assertEqual(channel_cmd.name_flags("game", "mac-web-b"),
                         "--project web --role b")
        # the gone-channel hint too
        shutil.rmtree(os.path.join(self.root, "game"))
        code, out, err = self.run_cli("run", "game.mac-web-b")
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
        with mock.patch.dict(os.environ, {"FERRY_TEST_MACHINE_ID": OTHER_MACHINE,
                                          "FERRY_CHANNELS_ROOT": other}):
            self.assertEqual(self.refused("leave", "game"),
                             "ERROR channel: fake-dest isn't the server game is on (its machine "
                             "id is %s, the record's %s): check the alias"
                             % (OTHER_MACHINE, TEST_MACHINE_ID))
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
        self.member()
        target = os.path.join(self.local, "laptop-ui", "STEPS.md")
        r = subprocess.run([sys.executable, POST_TOOL, target, "--to", "@all", "--title",
                            "forged", "--body", "x"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True,
                           encoding="utf-8")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("that is laptop-ui's folder, not yours: post into mac-web/", r.stderr)
        self.assertFalse(os.path.exists(target))
        r = subprocess.run([sys.executable, POST_TOOL, os.path.join(self.own, "RESULTS.md"),
                            "--to", "@laptop-ui", "--title", "mine", "--body", "x"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        # under joined/ --me is optional, and checked when given (M11a)
        ran = {}
        for me in ("laptop-ui", "mac-web"):
            ran[me] = subprocess.run([sys.executable, POST_TOOL,
                                      os.path.join(self.own, "RESULTS.md"), "--me", me, "--to",
                                      "@laptop-ui", "--title", "mine", "--body", "x"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     universal_newlines=True, encoding="utf-8")
        self.assertEqual(ran["laptop-ui"].returncode, 1, ran["laptop-ui"].stdout)
        self.assertIn("is mac-web's folder, not laptop-ui's: post only in your own",
                      ran["laptop-ui"].stderr)
        self.assertEqual(ran["mac-web"].returncode, 0, ran["mac-web"].stderr)

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_one_lock_for_two_spellings(self):
        self.member()
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.local, alias)
        self.assertEqual(entries.lock_path(self.own),
                         entries.lock_path(os.path.join(alias, "mac-web")))

    def test_remove_retries_a_rename_in_use(self):
        # M11a: on Windows a watcher scanning the channel holds a handle, and the rename fails
        # for a moment: winerror 32 or 5 (faked here; 5 measured on Windows, M11c run 2)
        for codes in ((32, 32), (5, 32), (5, 5)):
            with self.subTest(codes=codes):
                write_tree(self.root, {"game/lead/CHANNEL.md": b"c", "game/a/MEMBER.md": b"m"})
                real = channels._DIR.rename
                calls = []

                def rename(handle, old, new):
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

                def rename(handle, old, new):
                    calls.append(old)
                    e = OSError(13, "denied")
                    e.winerror = winerror
                    raise e

                with mock.patch.object(channels._DIR, "rename", rename), \
                        mock.patch.object(channels.fsops, "RETRY_DELAY", 0):
                    with self.assertRaises(VCharonError):
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
                def rename(handle, old, new):
                    e = PermissionError(13, "拒绝访问。")
                    e.winerror = winerror
                    raise e

                with mock.patch.object(channels._DIR, "rename", rename), \
                        mock.patch.object(channels.fsops, "RETRY_DELAY", 0):
                    with self.assertRaises(VCharonError) as cm:
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

        with mock.patch.object(channels._DIR, "rename", denied):
            with self.assertRaises(VCharonError) as cm:
                channels.remove(self.root, "game", "lead")
        self.assertEqual(cm.exception.hint, "check the owner and permissions of %s" % path)
        self.assertEqual(read_tree(self.root), before)

    # --- the review's surviving mutants ---

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
        code, out, err = self.channel("create", "game", "--ssh", "fake-dest", "--project", "ui")
        self.assertNotEqual(code, 0)
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse(os.path.exists(channel_cmd.record_path("game", "laptop-ui")))

    def test_a_claim_that_fails_leaves_no_channel(self):
        real = channels._DIR.mkdir

        def mkdir(handle, name, mode=0o777):
            if name == "lead":
                raise PermissionError(13, "Permission denied", name)
            return real(handle, name, mode)

        with mock.patch.object(channels._DIR, "mkdir", mkdir):
            with self.assertRaises(VCharonError) as cm:
                channels.claim(self.root, "game", "lead", True)
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual(os.listdir(self.root), [])

    def test_releasing_the_last_member_removes_the_channel(self):
        write_tree(self.root, {"game/lead/MEMBER.md": b"m", "game/lead/CHANNEL.md": b"c"})
        self.assertEqual(channels.release(self.root, "game", "lead"), {"removed": True})
        self.assertEqual(os.listdir(self.root), [])

    def test_a_failed_rejoin_keeps_the_folder(self):
        self.lead()
        self.ok("join", "game", "--ssh", "fake-dest")
        # the server folder holds only MEMBER.md: what release would take
        srv = os.path.join(self.root, "game", "mac-web")
        self.assertEqual(os.listdir(srv), ["MEMBER.md"])
        section = os.path.join(self.homes["mac"], "channels.d", "game.mac-web.ini")
        os.remove(section)
        os.mkdir(section)
        code, out, err = self.channel("join", "game", "--ssh", "fake-dest")
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
            self.run_cli("state", "reset", "game.mac-web." + job)
        out = self.ok("leave", "game")
        self.assertIn("  note    left %s in place: it isn't %s" % (mine, self.local), out)
        self.assertTrue(os.path.isdir(os.path.join(mine, "mac-web")))
        self.assertTrue(os.path.isdir(self.local))

    def test_a_record_with_another_ssh_is_another_server(self):
        self.lead()
        self.ok("join", "game", "--ssh", "fake-dest")
        # the same machine, reached --local: still another membership
        self.assertEqual(self.refused("join", "game", "--local"),
                         "ERROR channel: you are in game on another server: pass --role")

    def test_join_prints_all_only_from_the_leader(self):
        self.lead()
        self.use_box("win")
        self.ok("join", "game", "--ssh", "fake-dest", "--project", "b")
        own_b = os.path.join(self.joined("game.win-b"), "win-b")
        entries.post(os.path.join(own_b, "RESULTS.md"), own_b, "win-b", "a member's all",
                     [entries.ALL], body="not the leader's")
        self.assertEqual(self.run_cli("run", "game.win-b")[0], 0)
        self.use_box("mac")
        out = self.ok("join", "game", "--ssh", "fake-dest")
        self.assertIn("channel game created", out)
        self.assertNotIn("a member's all", out)
