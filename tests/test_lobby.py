"""The lobby (DESIGN, "The lobby"): its kind and format, the join that makes it, the claim's
CHANNEL.md and its undo, taking a folder back, day files and their cleanup, silent entries,
the watcher's marks kept short, who is here, and what a work channel keeps."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from vcharon import channel_cmd, channels, charter, config, entries, kind
from vcharon.mailbox import post as post_mod
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

from tests import test_mailbox_watch as watch_tests
from tests import util
from tests.test_channel import ChannelCase
from tests.test_charter import channel_md
from tests.test_mailbox_watch import Rounds, member_md, never
from tests.util import write_tree

LOBBY_LIMITS = {"max_mb": 50, "max_files": 1000, "max_entry_kb": 10000}
# a time on this machine's clock, whatever its zone, and its day file
T0 = time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))
TODAY = "chat-2026-11-20.md"


def day_name(t):
    return "chat-%s.md" % time.strftime("%Y-%m-%d", time.localtime(t))


def lobby_md(name, n=2):
    """A lobby's CHANNEL.md as its claim writes it, founded by name."""
    title, to, header = charter.charter_entry("lobby", name, "9.9", LOBBY_LIMITS, T0,
                                              charter.LOBBY_KIND)
    return ("# CHANNEL\n" + entries.build(entries.stamp(T0), name, n, title, to,
                                          header=header)).encode("utf-8")


class KindTest(unittest.TestCase):
    def test_header_and_check(self):
        made = entries.build("t", "lead", 2, "x", ["@all"],
                             header=charter.header("9.9", LOBBY_LIMITS, charter.LOBBY_KIND))
        self.assertIn("format: 2\nkind: lobby\ncreated by: vcharon 9.9\n", made)
        info = charter.parse(made, "lead")
        self.assertEqual(info, {"format": 2, "kind": "lobby", "limits": LOBBY_LIMITS})
        self.assertEqual(charter.check("lobby", info), LOBBY_LIMITS)
        # a work channel's: format 1, no kind: line at all
        work = entries.build("t", "lead", 2, "x", ["@all"],
                             header=charter.header("9.9", charter.default_limits()))
        self.assertIn("format: 1\ncreated by", work)
        self.assertNotIn("kind", charter.parse(work, "lead"))
        # format 2 without kind: lobby, another kind, or a lobby in format 1: no vcharon
        # writes them
        for bad in ({"format": 2}, {"format": 2, "kind": "chat"}, {"format": 1, "kind": "lobby"},
                    {"format": 1, "kind": "work"}):
            with self.subTest(bad=bad):
                with self.assertRaises(VCharonError) as cm:
                    charter.check("game", dict(bad, limits=LOBBY_LIMITS))
                self.assertEqual(cm.exception.hint, charter.NO_KIND_HINT)
        # a newer format: the update, before any kind
        with self.assertRaises(VCharonError) as cm:
            charter.check("game", {"format": 3, "kind": "lobby", "limits": LOBBY_LIMITS})
        self.assertEqual(cm.exception.hint, charter.UPDATE_HINT)

    def test_the_two_kinds(self):
        self.assertIs(kind.of({}), kind.WORK)
        self.assertIs(kind.of({"kind": "lobby"}), kind.LOBBY)
        e = entries.parse("## t — a#3 — JOIN\nto: @a\n")[0]
        self.assertTrue(kind.LOBBY.silent(e, "a"))
        self.assertFalse(kind.LOBBY.silent(e, "b"))
        self.assertFalse(kind.WORK.silent(e, "a"))
        both = entries.parse("## t — a#3 — x\nto: @a @b\n")[0]
        self.assertFalse(kind.LOBBY.silent(both, "a"))
        self.assertEqual(kind.LOBBY.day_file("2026-11-20 23:59:59"), TODAY)
        self.assertIsNone(kind.WORK.day_file("2026-11-20 23:59:59"))
        self.assertEqual((kind.LOBBY.announce_to("a", "b"), kind.WORK.announce_to("a", "b")),
                         ("@a", "@b"))

    def test_day_files_and_the_cleanup(self):
        own = os.path.join(self.tmp(), "own")
        today = datetime.date(2026, 11, 20)
        keep = "chat-%s.md" % (today - datetime.timedelta(days=29))
        old = "chat-%s.md" % (today - datetime.timedelta(days=30))
        older = "chat-2025-01-01.md"
        write_tree(own, {keep: b"k", old: b"o", older: b"o", "MEMBER.md": b"m",
                         "CHANNEL.md": b"c", "chat-2026-13-40.md": b"bad date",
                         "notes.md": b"n", "sub/" + older: b"below", TODAY: b"t",
                         "Chat-2025-01-02.md": b"case"})
        if util.CAN_SYMLINK:
            os.symlink(os.path.join(own, "notes.md"), os.path.join(own, "chat-2025-01-03.md"))
        self.assertEqual(kind.old_day_files(own, today), [older, old])
        path = os.path.join(own, TODAY)
        # only a post that created today's file deletes them
        self.assertEqual(kind.LOBBY.cleanup_for(own, path, False), [])
        self.assertEqual(kind.WORK.cleanup_for(own, path, True), [])
        self.assertEqual(kind.LOBBY.cleanup_for(own, os.path.join(own, "x.md"), True), [])
        doomed = kind.LOBBY.cleanup_for(own, path, True)
        self.assertEqual(kind.cleanup(doomed), ([older, old], []))
        self.assertEqual(sorted(os.listdir(own)), sorted(
            [keep, "MEMBER.md", "CHANNEL.md", "chat-2026-13-40.md", "notes.md", "sub", TODAY,
             "Chat-2025-01-02.md"] + (["chat-2025-01-03.md"] if util.CAN_SYMLINK else [])))
        self.assertTrue(os.path.exists(os.path.join(own, "sub", older)))

    def test_the_limit_check_counts_the_cleanup_out(self):
        own = os.path.join(self.tmp(), "own")
        write_tree(own, {"MEMBER.md": b"m", "chat-2025-01-01.md": b"x" * 600,
                         "chat-2025-01-02.md": b"x" * 600})
        limits = {"max_mb": 1, "max_files": 10, "max_entry_kb": 1}
        path = os.path.join(own, TODAY)
        with mock.patch.object(charter, "MB", 1500):
            # 1201 bytes there, and 300 more: over 1500 without the cleanup, under with it
            with self.assertRaises(VCharonError) as cm:
                post_mod.limit_check(own, limits)(path, 300)
            self.assertEqual(cm.exception.code, "too_big")
            post_mod.limit_check(own, limits,
                                 lambda p: kind.LOBBY.cleanup_for(own, p, True))(path, 300)
            # an existing file: nothing freed
            write_tree(own, {TODAY: b""})
            with self.assertRaises(VCharonError):
                post_mod.limit_check(own, limits,
                                     lambda p: kind.LOBBY.cleanup_for(own, p, False))(path, 300)

    def tmp(self):
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp


class LobbyCase(ChannelCase):
    """A channel root on this machine (--local) or the fake server; the current box mac in
    the checkout Web, so its member is mac-web."""

    def lobby_folder(self, name="mac-web"):
        return os.path.join(self.root, "lobby", name)

    def join(self, box="mac", *where):
        """box joins the lobby (--local unless where), then back to mac: its output."""
        self.use_box(box)
        try:
            return self.ok("join", "lobby", *(where or ("--local",)))
        finally:
            self.use_box("mac")

    def at(self, t, *argv, box="mac"):
        """vcharon argv on box with the clock at t (a number, or a function): (exit code,
        stdout, stderr)."""
        clock = t if callable(t) else (lambda: t)
        self.use_box(box)
        try:
            with mock.patch("time.time", clock):
                return self.channel(*argv)
        finally:
            self.use_box("mac")

    def files(self, name="mac-web"):
        return sorted(os.listdir(self.lobby_folder(name)))


class JoinTest(LobbyCase):
    def test_a_record_of_a_removed_lobby(self):
        # the user removed the lobby by hand: the member's next join is refused with the
        # leave; after it, the join makes the new lobby
        self.ok("join", "lobby", "--local")
        shutil.rmtree(os.path.join(self.root, "lobby"))
        self.assertEqual(self.refusal("join", "lobby", "--local"), (
            "ERROR channel: your join record of lobby as mac-web is of an earlier channel: "
            "lobby is gone from this machine",
            channel_cmd.platform.runnable(
                "run vcharon leave lobby --project web (it posts nothing, and removes this "
                "machine's files of that membership), then join again")))
        self.assertEqual(self.server_tree(), {})
        self.ok("leave", "lobby")
        self.assertIn("made the lobby", self.ok("join", "lobby", "--local"))

    def test_the_first_join_makes_it_and_the_next_joins(self):
        out = self.ok("join", "lobby", "--local")
        self.assertIn("  claimed lobby/mac-web; made the lobby\n", out)
        self.assertIn(channel_cmd.LOBBY_NOTE.replace("vcharon guide",
                                                     "%s guide" % self.command()), out)
        self.assertNotIn(channel_cmd.ASK_USER, out)
        folder = self.lobby_folder()
        self.assertEqual(self.files(), ["CHANNEL.md", "MEMBER.md", day_name(time.time())])
        info = charter.read(folder, "mac-web")
        self.assertEqual(info, {"format": 2, "kind": "lobby", "limits": LOBBY_LIMITS})
        record = self.record("lobby.mac-web")
        self.assertEqual((record["kind"], record["format"], record["leader"]),
                         ("lobby", 2, "mac-web"))
        # MEMBER.md's #1 and the JOIN: to the member itself
        [first] = entries.parse_file(os.path.join(folder, "MEMBER.md"))
        [joined] = entries.parse_file(os.path.join(folder, day_name(time.time())))
        self.assertEqual((first.to, joined.to, joined.title), (("@mac-web",), ("@mac-web",),
                                                               "JOIN"))
        out = self.join("linux")
        self.assertIn("  claimed lobby/linux-web; in the lobby\n", out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "lobby"))),
                         ["linux-web", "mac-web"])
        # create lobby: refused, exit 3, before anything
        before = self.server_tree()
        self.assertEqual(self.refusal("create", "lobby", "--local", "--project", "x", code=3), (
            "ERROR config: the lobby is made by its first join",
            channel_cmd.platform.runnable("join it: vcharon join lobby --local --project x")))
        self.assertEqual(self.server_tree(), before)

    def command(self):
        return channel_cmd.platform.self_command()

    def test_two_first_joins_at_once(self):
        # linux makes it between mac's listing and mac's claim: mac's claim is refused as
        # existing, and mac joins the lobby linux made
        real = channels.list_channels
        calls = []

        def listing(root, *a, **kw):
            calls.append(root)
            if len(calls) == 1:
                # the other join's claim, as the helper makes it
                channels.claim(self.root, "lobby", "linux-web", True, lobby=True)
                return {"channels": [], "others": []}
            return real(root, *a, **kw)

        with mock.patch.object(channel_cmd.channels, "list_channels", listing):
            out = self.ok("join", "lobby", "--local")
        self.assertIn("  claimed lobby/mac-web; in the lobby\n", out)
        self.assertEqual(charter.read(self.lobby_folder("linux-web"), "linux-web")["kind"],
                         "lobby")
        self.assertFalse(os.path.exists(os.path.join(self.lobby_folder(), "CHANNEL.md")))
        self.assertEqual(self.record("lobby.mac-web")["leader"], "linux-web")

    def test_a_lobby_listed_without_channel_md(self):
        self.join("linux")
        charter_md = os.path.join(self.lobby_folder("linux-web"), "CHANNEL.md")
        with open(charter_md, "rb") as f:
            data = f.read()
        os.remove(charter_md)
        real = channels.list_channels
        calls = []

        def listing(root, *a, **kw):
            # the claim writes it a moment after the listing that missed it
            calls.append(root)
            if len(calls) == 2:
                write_tree(self.root, {"lobby/linux-web/CHANNEL.md": data})
            return real(root, *a, **kw)

        with mock.patch.object(channel_cmd, "LOBBY_POLL", 0), \
                mock.patch.object(channel_cmd.channels, "list_channels", listing):
            out = self.ok("join", "lobby", "--local")
        self.assertIn("  claimed lobby/mac-web; in the lobby\n", out)
        self.assertGreaterEqual(len(calls), 2)
        # still none after the wait: refused, nothing claimed; an empty lobby/ too
        for gap in ("founder's folder", "empty"):
            with self.subTest(gap=gap):
                shutil.rmtree(os.path.join(self.root, "lobby"))
                write_tree(self.root, {"lobby/linux-web/MEMBER.md": b"# MEMBER\n"}
                           if gap != "empty" else {"lobby/": None})
                self.use_box("win")
                with mock.patch.object(channel_cmd, "LOBBY_WAIT", 0.05), \
                        mock.patch.object(channel_cmd, "LOBBY_POLL", 0.01):
                    before = self.server_tree()
                    self.assertEqual(self.refusal("join", "lobby", "--local"), (
                        "ERROR channel: the lobby has no CHANNEL.md: it is gone or was never "
                        "finished", "ask your user to remove the whole lobby folder at the "
                        "channel root; the next join makes a new one"))
                    self.assertEqual(self.server_tree(), before)
                self.use_box("mac")

    def test_an_older_vcharons_lobby_is_a_work_channel(self):
        write_tree(self.root, {"lobby/lead/CHANNEL.md": channel_md("lead", "1"),
                               "lobby/lead/MEMBER.md": member_md("lead", "lead", "lobby")})
        out = self.ok("join", "lobby", "--local")
        self.assertIn("  claimed lobby/mac-web; the leader is lead\n", out)
        self.assertIn(channel_cmd.OLD_LOBBY_NOTE + "\n", out)
        self.assertIn(channel_cmd.ASK_USER, out)
        record = self.record("lobby.mac-web")
        self.assertNotIn("kind", record)
        # its JOIN to the leader, in RESULTS.md
        [joined] = entries.parse_file(os.path.join(self.lobby_folder(), "RESULTS.md"))
        self.assertEqual(joined.to, ("@lead",))

    def test_leave_then_join_takes_the_folder_back(self):
        self.join("linux")
        self.ok("join", "lobby", "--local")
        out = self.ok("leave", "lobby")
        self.assertIn("OK  left lobby", out)
        day = day_name(time.time())
        [_, left] = entries.parse_file(os.path.join(self.lobby_folder(), day))
        self.assertIn("  posted LEAVE %s into %s, to @mac-web (wakes no one)\n" % (left.id, day),
                      out)
        out = self.ok("join", "lobby", "--local")
        # one line says it: no note repeats it
        self.assertEqual([l for l in out.splitlines() if "took back" in l],
                         ["  took back lobby/mac-web, this machine's folder; in the lobby"])
        titles = [e.title for e in entries.parse_file(
            os.path.join(self.lobby_folder(), day))]
        self.assertEqual(titles, ["JOIN", "LEAVE", "REJOIN"])
        # another machine's folder (another claimer): refused as in any channel
        self.ok("leave", "lobby")
        entries.set_header(os.path.join(self.lobby_folder(), "MEMBER.md"), self.lobby_folder(),
                           "mac-web", 1, "claimer", "0123456789abcdef")
        line, _ = self.refusal("join", "lobby", "--local")
        self.assertEqual(line, "ERROR channel: another machine holds mac-web in lobby")
        # a folder with no MEMBER.md (no claimer): refused, never taken back
        os.remove(os.path.join(self.lobby_folder(), "MEMBER.md"))
        line, _ = self.refusal("join", "lobby", "--local")
        self.assertEqual(line, "ERROR channel: the name mac-web is taken in lobby")

    def test_the_leaves_cleanup_follows_its_posted_line(self):
        self.join("linux")
        self.ok("join", "lobby", "--local")
        day = day_name(time.time())
        old = day_name(time.time() - 40 * 86400)
        os.rename(os.path.join(self.lobby_folder(), day),
                  os.path.join(self.lobby_folder(), old))
        lines = self.ok("leave", "lobby").splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith("  posted LEAVE "))
        self.assertEqual(lines[i + 1], "  removed %s (older than 30 days)" % old)

    def test_the_founder_leaves_and_close_is_refused(self):
        self.ok("join", "lobby", "--local")
        self.join("linux")
        self.assertEqual(self.refusal("close", "lobby", code=3), (
            "ERROR config: a lobby isn't closed",
            channel_cmd.platform.runnable("vcharon leave lobby --project web")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "lobby")))
        out = self.ok("leave", "lobby")
        self.assertIn("OK  left lobby", out)
        # the founder's folder stays, CHANNEL.md in it: linux's lobby goes on
        self.assertIn("CHANNEL.md", self.files())
        # an older vcharon's lobby is a work channel: its leader closes it
        shutil.rmtree(os.path.join(self.root, "lobby"))
        write_tree(self.root, {"lobby/mac-web/CHANNEL.md": channel_md("mac-web", "1"),
                               "lobby/mac-web/MEMBER.md": member_md("mac-web", "mac-web",
                                                                    "lobby")})
        channel_cmd.write_record({"version": 1, "channel": "lobby", "name": "mac-web",
                                  "leader": "mac-web", "ssh": None,
                                  "remote": os.path.join(self.root, "lobby"),
                                  "machine": util.TEST_MACHINE_ID, "project": "web",
                                  "role": None, **util.record_format()})
        out = self.ok("close", "lobby")
        self.assertIn("OK  closed lobby", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "lobby")))


class ClaimTest(LobbyCase):
    def test_a_failed_channel_md_write_removes_both_folders(self):
        root = os.path.join(self.tmp, "r")
        with mock.patch.object(channels.entries, "post", side_effect=OSError(28, "No space")), \
                self.assertRaises(VCharonError):
            channels.claim(root, "lobby", "mac-web", True, lobby=True)
        self.assertEqual(os.listdir(root), [])
        got = channels.claim(root, "lobby", "mac-web", True, lobby=True)
        with open(os.path.join(root, "lobby", "mac-web", "CHANNEL.md"), encoding="utf-8") as f:
            self.assertEqual(got["charter"], f.read())
        self.assertEqual((got["format"], got["kind"]), (2, "lobby"))
        # a work channel's create claim writes none, and its reply has no kind
        got = channels.claim(root, "game", "mac-web", True)
        self.assertEqual(os.listdir(os.path.join(root, "game", "mac-web")), [])
        self.assertNotIn("charter", got)

    def fail_after_the_claim(self, also=None):
        """mac's first join failing at its record, after the claim made the lobby; also(): run
        just before (another member joining meanwhile). (stderr, the lobby's tree)."""
        def write_record(doc):
            if also is not None:
                also()
            raise OSError(28, "No space left on device")

        with mock.patch.object(channel_cmd, "write_record", write_record):
            code, out, err = self.channel("join", "lobby", "--local")
        self.assertEqual(code, 1, out + err)
        return err, self.server_tree()

    def test_a_failed_founders_join_alone_removes_all(self):
        err, tree = self.fail_after_the_claim()
        self.assertEqual(tree, {})
        self.assertNotIn("stays", err)
        self.assertIn("ERROR", err)

    def test_a_failed_founders_join_keeps_channel_md_for_the_others(self):
        err, tree = self.fail_after_the_claim(
            also=lambda: write_tree(self.root, {"lobby/linux-web/MEMBER.md": b"# MEMBER\n"}))
        self.assertEqual(sorted(p for p in tree if not p.endswith("/")),
                         ["lobby/linux-web/MEMBER.md", "lobby/mac-web/CHANNEL.md"])
        self.assertIn(channel_cmd.KEPT_NOTE % "mac-web", err)
        self.assertLess(err.index("stays"), err.index("ERROR"))


class RemoteTest(LobbyCase):
    def test_the_founders_copy_is_the_servers_bytes(self):
        # no sync in the founder's join: the claim alone put CHANNEL.md at the server, so a
        # second join right after works
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 0):
            out = self.ok("join", "lobby", "--server", "fake-dest")
        self.assertIn("made the lobby", out)
        local = os.path.join(self.joined("lobby.mac-web"), "mac-web", "CHANNEL.md")
        with open(local, "rb") as f, \
                open(os.path.join(self.lobby_folder(), "CHANNEL.md"), "rb") as g:
            self.assertEqual(f.read(), g.read())
        with open(channel_cmd.section_path(config.load(), "lobby.mac-web"),
                  encoding="utf-8") as f:
            self.assertIn("mailbox.kind   = lobby\n", f.read())
        out = self.join("linux", "--server", "fake-dest")
        self.assertIn("  claimed lobby/linux-web; in the lobby\n", out)
        # and the founder's sync leaves the server's CHANNEL.md as it was
        before = self.server_tree()["lobby/mac-web/CHANNEL.md"]
        self.ok("sync", "lobby")
        self.assertEqual(self.server_tree()["lobby/mac-web/CHANNEL.md"], before)


class DownJobTest(unittest.TestCase):
    def test_a_lobbys_down_has_no_delete_limit(self):
        def jobs(extra):
            parser = config._parse(("[lobby.mac-web]\nssh = devbox\nmailbox.me = mac-web\n"
                                    "mailbox.leader = linux-web\nmailbox.local = ~/x\n"
                                    "mailbox.remote = ~/c/lobby\n" + extra).encode("utf-8"),
                                   "f", "f", "fix")
            return config._read_mailbox(parser, "lobby.mac-web", "f", "fix", config.Settings())

        up, down = jobs("mailbox.kind = lobby\n")
        self.assertEqual(down.sink.options["max_deletes"], "0")
        self.assertEqual(down.mailbox.kind, "lobby")
        self.assertNotIn("max_deletes", up.sink.options)
        # a work channel's section, and one written before the key: the dir sink's 500
        _up, down = jobs("")
        self.assertNotIn("max_deletes", down.sink.options)
        self.assertIsNone(down.mailbox.kind)
        with self.assertRaises(VCharonError):
            jobs("mailbox.kind = work\n")


class PostTest(LobbyCase):
    def setUp(self):
        LobbyCase.setUp(self)
        self.join("linux")
        self.ok("join", "lobby", "--local")
        # the join's day file is of the real clock's day: gone, so the posts at T0 make
        # today's file whatever day the suite runs
        for n in self.files():
            if n.startswith("chat-"):
                os.remove(os.path.join(self.lobby_folder(), n))

    def post(self, t, *argv, box="mac"):
        argv = list(argv) + ([] if "--title" in argv else ["--title", "hi"])
        return self.at(t, "post", "lobby", *argv, "--body", "b", box=box)

    def test_the_day_file_and_anyones_all(self):
        code, out, err = self.post(T0, "--to", "@all")
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"\Aposted mac-web#\d+ — hi into mac-web/%s, to @all at 2026-11-20 "
                              r"12:00:00\n\Z" % TODAY)
        [e] = entries.parse_file(os.path.join(self.lobby_folder(), TODAY))
        self.assertEqual(e.to, ("@all",))
        # --file as in a work channel; --steps refused
        code, out, _ = self.post(T0, "--to", "@linux-web", "--file", "notes.md")
        self.assertIn(" into mac-web/notes.md, to @linux-web at ", out)
        # a refused --file points at the day file, not a work channel's RESULTS.md
        day_fix = "leave out --file: a lobby post goes into your day file chat-YYYY-MM-DD.md"
        for name, fix in (("x.txt", day_fix), ("MEMBER.md", day_fix),
                          ("nope/x.md", "make nope/ in your own folder first, or " + day_fix)):
            with self.subTest(name=name):
                code, _, err = self.post(T0, "--to", "@linux-web", "--file", name)
                self.assertEqual((code, err.splitlines()[1]), (1, "  fix: " + fix))
        # a usage error (exit 3), as create lobby and close lobby are
        code, _, err = self.post(T0, "--to", "@all", "--steps")
        self.assertEqual((code, err.splitlines()[:2]),
                         (3, ["ERROR config: a lobby has no plan", "  fix: post without --steps"]))

    def test_midnight(self):
        # the command named the day file at 23:59:59.999 (the path given); the heading's
        # clock reading, taken under the lock, is the next day: the file is the heading's
        folder = self.lobby_folder()
        after = time.mktime((2026, 11, 21, 0, 0, 0, 0, 0, -1))
        done = {}
        id_, when = post_mod.post(os.path.join(folder, TODAY), "mac-web", ["@linux-web"], "t",
                                  body="b", clock=lambda: after, limits=LOBBY_LIMITS,
                                  kind_=kind.LOBBY, day=True, done=done)
        self.assertEqual(when, "2026-11-21 00:00:00")
        self.assertEqual(done["path"], os.path.join(folder, "chat-2026-11-21.md"))
        [e] = entries.parse_file(done["path"])
        self.assertEqual(e.id, id_)
        self.assertNotIn(TODAY, self.files())

    def test_the_cleanup(self):
        folder = self.lobby_folder()
        write_tree(folder, {"chat-2026-10-21.md": entries.build(
            "2026-10-21 10:00:00", "mac-web", 90, "old", ["@all"]).encode("utf-8"),
            "chat-2026-10-22.md": b"# chat\n", "chat-2026-13-40.md": b"x", "notes.md": b"n"})
        # a new file of --file: no cleanup
        code, out, _ = self.post(T0, "--to", "@all", "--file", "new.md")
        self.assertEqual(code, 0)
        self.assertNotIn("removed", out)
        self.assertIn("chat-2026-10-21.md", self.files())
        self.assertIn("mac-web#91", out)
        # today's day file made by this post: the one older than 30 days goes
        code, out, err = self.post(T0, "--to", "@all")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[1:],
                         ["  removed chat-2026-10-21.md (older than 30 days)"])
        files = self.files()
        for kept in ("chat-2026-10-22.md", "chat-2026-13-40.md", "notes.md", "MEMBER.md",
                     TODAY):
            self.assertIn(kept, files)
        self.assertNotIn("chat-2026-10-21.md", files)
        # numbering goes on from today's file, which holds the largest
        self.assertIn("mac-web#92", out)
        # today's file there now: no second cleanup
        write_tree(folder, {"chat-2026-01-01.md": b"# chat\n"})
        code, out, _ = self.post(T0, "--to", "@all")
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("mac-web#93", out)

    def test_the_cleanup_makes_room_at_the_files_limit(self):
        # mac's folder at the lobby's 1000 files, one of them an old day file: the post that
        # makes today's file is counted with that file gone, and goes through
        folder = self.lobby_folder()
        files = {"chat-2026-01-01.md": b"# chat\n"}
        files.update(("f%04d.txt" % i, b"") for i in range(1000 - 2))
        write_tree(folder, files)
        self.assertEqual(len(os.listdir(folder)), 1000)
        code, out, err = self.post(T0, "--to", "@all")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("  removed chat-2026-01-01.md (older than 30 days)\n", out)
        self.assertEqual(len(os.listdir(folder)), 1000)

    def test_a_failed_delete_is_a_note(self):
        write_tree(self.lobby_folder(), {"chat-2026-01-01.md": b"# chat\n"})
        real = os.remove

        def remove(path, *a, **kw):
            if path.endswith("chat-2026-01-01.md"):
                raise PermissionError(13, "Permission denied")
            return real(path, *a, **kw)

        with mock.patch.object(kind.os, "remove", remove):
            code, _out, err = self.post(T0, "--to", "@all")
        self.assertEqual(code, 0)
        self.assertEqual(err, channel_cmd.REMOVE_FAILED % ("chat-2026-01-01.md", 30,
                                                           "Permission denied") + "\n")


class LobbyWatchCase(watch_tests.WatchCase):
    """A lobby's tree, self.tree, watched by debian (a local member, not the founder mac)."""

    def setUp(self):
        watch_tests.WatchCase.setUp(self)
        # the founder mac is the leader its members' MEMBER.md name
        write_tree(self.tree, {"mac/MEMBER.md": member_md("mac", "mac", "lobby"),
                               "mac/CHANNEL.md": lobby_md("mac"),
                               "debian/MEMBER.md": member_md("debian", "mac", "lobby")})

    def run_dir(self, *steps, kind_=kind.LOBBY, me="debian"):
        lines = []
        code = watch.watch_dir(self.tree, me, 10, out=lines.append,
                               sleep=Rounds(*steps) if steps else never, rounds=len(steps),
                               kind_=kind_)
        self.assertEqual(code, 0)
        return self.said(lines)[1:]

    def snapshot(self, me="debian"):
        with open(self.state(me), encoding="utf-8") as f:
            return json.load(f)


class WatchTest(LobbyWatchCase):
    def test_what_a_lobby_tells(self):
        def joins():
            self.post("mac", 3, "JOIN", to="@mac", file=TODAY)
            self.post("win", 1, "member", to="@win", file="MEMBER.md")
            self.post("win", 2, "JOIN", to="@win", file=TODAY)

        def says():
            self.post("win", 3, "hello", to="@all", file=TODAY, when="2026-10-01 09:01")
            self.post("mac", 4, "CLOSED", to="@all", file=TODAY, when="2026-10-01 09:02")
            self.post("mac", 5, "for win", to="@win", file=TODAY)

        def gone():
            os.remove(os.path.join(self.tree, "win", TODAY))

        self.assertEqual(self.run_dir(joins, says, gone), [
            "to all: win#3 — hello  (win/%s)" % TODAY,
            "to all: mac#4 — CLOSED  (mac/%s)" % TODAY,
            "1 other entry (mac)"])
        # a work channel's watcher: the same entries are other entries, a member's @all is
        # ignored, and the leader's CLOSED has its next: line
        shutil.rmtree(self.tree)
        self.setUp()
        self.assertEqual(self.run_dir(joins, says, kind_=kind.WORK), [
            "3 other entries (mac, win)",
            "to all: mac#4 — CLOSED  (mac/%s)" % TODAY,
            watch.closed_next("mb", "debian", "mac"),
            "1 other entry (mac)",
            "note: @all from win, not the leader: ignored"])

    def test_the_snapshot_drops_the_heads_of_gone_files(self):
        old = "chat-2026-09-01.md"

        def posts():
            self.post("mac", 3, "one", to="@debian", file=old)
            self.post("mac", 4, "two", to="@debian", file=TODAY)

        def cleanup():
            os.remove(os.path.join(self.tree, "mac", old))

        def quiet():
            pass

        lines = self.run_dir(posts, cleanup, quiet)
        self.assertEqual(lines, ["to you: mac#3 — one  (mac/%s)" % old,
                                 "to you: mac#4 — two  (mac/%s)" % TODAY])
        doc = self.snapshot()
        self.assertEqual(sorted(doc["heads"]), ["mac#1", "mac#2", "mac#4"])
        self.assertEqual(doc["seen"]["mac"], {"low": 4, "more": []})
        # a work channel keeps them
        os.remove(self.state())
        write_tree(self.tree, {"mac/" + old: b""})
        os.remove(os.path.join(self.tree, "mac", old))
        self.run_dir(posts, cleanup, kind_=kind.WORK)
        self.assertIn("mac#3", self.snapshot()["heads"])

    def test_with_no_kind_given_it_is_read_from_channel_md(self):
        # a local member's watch with no record (watch_dir's own path): the kind comes from
        # the leader's CHANNEL.md, so the lobby's rules hold
        def says():
            self.post("win", 2, "JOIN", to="@win", file=TODAY)
            self.post("win", 3, "hello", to="@all", file=TODAY)

        self.assertEqual(self.run_dir(says, kind_=None),
                         ["to all: win#3 — hello  (win/%s)" % TODAY])

    def test_charter_and_old_files_dont_hold_the_mark_down(self):
        # the founder's CHANNEL.md #2, and win's notes.md #2 (an old --file post), stay for
        # good; once the day files' older numbers are gone, the list of numbers seen is empty
        # again a round later all the same
        write_tree(self.tree, {"win/MEMBER.md": member_md("win", "mac", "lobby")})
        self.post("win", 2, "notes", to="@mac", file="notes.md")
        for folder in ("mac", "win"):
            self.post(folder, 3, "old", to="@mac", file="chat-2026-09-01.md")

        def quiet():
            pass

        def away():
            # #4..#9 came and went while no round ran
            for folder in ("mac", "win"):
                os.remove(os.path.join(self.tree, folder, "chat-2026-09-01.md"))
                self.post(folder, 10, "new", to="@all", file=TODAY)

        self.assertEqual(self.run_dir(away, quiet),
                         ["to all: mac#10 — new  (mac/%s)" % TODAY,
                          "to all: win#10 — new  (win/%s)" % TODAY])
        seen = self.snapshot()["seen"]
        self.assertEqual((seen["mac"], seen["win"]),
                         ({"low": 10, "more": []}, {"low": 10, "more": []}))

    def test_an_entry_a_round_late_is_still_printed(self):
        # win's older day files are gone (its highest seen: #49); #51 lands in a new day file
        # one round before #50 lands in report.md (a sync bringing the files over two
        # rounds): the floor stops at #49, so #50 is printed. A lag of two rounds or more
        # would be taken as seen (DESIGN, "The lobby"): not asserted here
        write_tree(self.tree, {"win/MEMBER.md": member_md("win", "mac", "lobby")})
        self.post("win", 49, "older", to="@debian", file="notes.md")

        def day_first():
            self.post("win", 51, "day", to="@debian", file=TODAY)

        def report_late():
            self.post("win", 50, "report", to="@debian", file="report.md")

        self.assertEqual(self.run_dir(day_first, report_late), [
            "to you: win#51 — day  (win/%s)" % TODAY,
            "to you: win#50 — report  (win/report.md)"])

    def test_the_floor_holds_below_the_day_files(self):
        # win's day file holds #2 and #4; #3 lands in notes.md two rounds late. The day
        # files' smallest number (#2) is the floor, so #3 is still printed
        write_tree(self.tree, {"win/MEMBER.md": member_md("win", "mac", "lobby")})
        self.post("win", 2, "JOIN", to="@win", file=TODAY)
        self.post("win", 4, "day", to="@debian", file=TODAY)

        def quiet():
            pass

        def late():
            self.post("win", 3, "notes", to="@debian", file="notes.md")

        self.assertEqual(self.run_dir(quiet, quiet, late),
                         ["to you: win#3 — notes  (win/notes.md)"])

    def test_across_midnight_a_round_late(self):
        # #9 in chat-10-05 is the highest seen; that file goes (a cleanup) as #11 lands in
        # chat-10-07, one round before #10 lands in chat-10-06: #10 is printed
        write_tree(self.tree, {"win/MEMBER.md": member_md("win", "mac", "lobby")})
        self.post("win", 9, "old", to="@debian", file="chat-2026-10-05.md")

        def next_day_first():
            os.remove(os.path.join(self.tree, "win", "chat-2026-10-05.md"))
            self.post("win", 11, "after midnight", to="@debian", file="chat-2026-10-07.md")

        def day_before_late():
            self.post("win", 10, "before midnight", to="@debian", file="chat-2026-10-06.md")

        self.assertEqual(self.run_dir(next_day_first, day_before_late), [
            "to you: win#11 — after midnight  (win/chat-2026-10-07.md)",
            "to you: win#10 — before midnight  (win/chat-2026-10-06.md)"])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "a file this user can't read")
    def test_a_folder_with_an_unread_file_isnt_raised(self):
        # win#5 is in notes.md, which can't be read in the round that sees it, below the
        # floor of win's day files (#9): that folder's mark isn't raised, and #5 is printed
        # once the file can be read
        write_tree(self.tree, {"win/MEMBER.md": member_md("win", "mac", "lobby")})
        self.post("win", 9, "day", to="@mac", file=TODAY)
        notes = os.path.join(self.tree, "win", "notes.md")

        def unreadable():
            self.post("win", 5, "notes", to="@debian", file="notes.md")
            os.chmod(notes, 0)

        def readable():
            os.chmod(notes, 0o644)

        self.addCleanup(os.chmod, notes, 0o644)
        self.assertEqual(self.run_dir(unreadable, readable),
                         ["to you: win#5 — notes  (win/notes.md)"])

    def test_a_later_number_first_doesnt_hide_the_earlier(self):
        # the start sees win#4 in notes.md before win#3 lands in its day file: #3 is printed
        write_tree(self.tree, {"win/MEMBER.md": member_md("win", "mac", "lobby")})
        self.post("win", 2, "JOIN", to="@win", file=TODAY)
        self.post("win", 4, "later", to="@debian", file="notes.md")

        def late():
            self.post("win", 3, "earlier", to="@debian", file=TODAY)

        self.assertEqual(self.run_dir(late),
                         ["to you: win#3 — earlier  (win/%s)" % TODAY])

    def test_a_member_that_joins_after_a_cleanup(self):
        # mac's #2..#499 are gone: the round after the start takes the numbers below the
        # smallest in mac's day files as seen (up to its highest seen at the start), and later
        # rounds keep the list of numbers seen empty
        os.remove(os.path.join(self.tree, "mac", "CHANNEL.md"))
        self.post("mac", 500, "old", to="@all", file=TODAY)

        def more():
            self.post("mac", 502, "later", to="@debian", file=TODAY)

        def next_one():
            self.post("mac", 501, "late", to="@debian", file=TODAY)
            self.post("mac", 503, "last", to="@debian", file=TODAY)

        def quiet():
            pass

        def while_away():
            # #504..#550 came and went (a cleanup) between two rounds: a round later the
            # numbers below the smallest one left are taken as seen
            os.remove(os.path.join(self.tree, "mac", TODAY))
            self.post("mac", 551, "after", to="@debian", file="chat-2026-11-21.md")

        lines = self.run_dir(more, next_one, while_away, quiet)
        self.assertEqual(lines, ["to you: mac#502 — later  (mac/%s)" % TODAY,
                                 "to you: mac#501 — late  (mac/%s)" % TODAY,
                                 "to you: mac#503 — last  (mac/%s)" % TODAY,
                                 "to you: mac#551 — after  (mac/chat-2026-11-21.md)"])
        self.assertEqual(self.snapshot()["seen"]["mac"], {"low": 551, "more": []})


class LobbyStreamTest(watch_tests.WatchCase):
    """A streaming watcher (StreamTest's fake child) in a lobby: a JOIN to its own poster
    wakes nothing, an @all from any member is to all."""

    setUp = watch_tests.StreamTest.setUp
    spawn = watch_tests.StreamTest.spawn
    reap = staticmethod(watch_tests.StreamTest.reap)
    watch = watch_tests.StreamTest.watch
    entry = watch_tests.StreamTest.entry

    def test_silent_entries(self):
        code, lines = self.watch([["out", "ROUND 0"], self.entry("mac", 2, "JOIN", to="@mac"),
                                  ["out", "ROUND 0"], self.entry("mac", 3, "hi", to="@all"),
                                  ["out", "ROUND 0"]], rounds=3, kind_=kind.LOBBY)
        self.assertEqual(code, 0)
        self.assertEqual(lines[1:], ["to all: mac#3 — hi  (mac/RESULTS.md)"])


class WhoamiTest(LobbyCase):
    def setUp(self):
        LobbyCase.setUp(self)
        self.ok("join", "lobby", "--local")
        for box in ("linux", "win", "laptop"):
            self.join(box)
        now = time.time()
        seen = channels.seen_root(self.root)
        # linux watches now; win watched an hour ago; laptop left a minute ago, its watcher
        # stamp fresh; mac (you) has no stamp and its files are new
        for name, age in (("linux-web", 5), ("win-web", 3600), ("laptop-web", 30)):
            channels.stamp_seen(self.root, "lobby", name, "local 10", now=now - age)
        self.assertTrue(os.path.isdir(os.path.join(seen, "lobby")))
        self.use_box("laptop")
        self.ok("leave", "lobby")
        self.use_box("mac")
        # a member not seen for two days
        write_tree(self.root, {"lobby/old-web/MEMBER.md": member_md("old-web", "mac-web",
                                                                     "lobby")})
        two_days = now - 2 * 86400
        os.utime(os.path.join(self.root, "lobby", "old-web", "MEMBER.md"),
                 (two_days, two_days))

    def test_presence(self):
        out = self.ok("whoami", "lobby")
        lines = out.splitlines()
        self.assertIn("  channel  lobby (a lobby, founded by mac-web)", lines)
        members = [l.split()[:2] + [l.split()[2]] if "(you)" in l else l.split()[:2]
                   for l in lines if l.startswith("    ")]
        self.assertEqual(members, [["linux-web", "here"], ["laptop-web", "left"],
                                   ["win-web", "away"], ["mac-web", "(you)", "away"]])
        self.assertNotIn("(leader)", out)
        self.assertEqual(lines[-1], "  +1 not seen in 24 h (--all)")
        out = self.ok("whoami", "lobby", "--all")
        self.assertIn("    old-web  gone  ", out)
        self.assertNotIn("not seen", out)
        doc = json.loads(self.ok("whoami", "lobby", "--json"))
        self.assertEqual((doc["kind"], doc["leader"], doc["founder"], doc["leads"]),
                         ("lobby", None, "mac-web", False))
        self.assertEqual({m["name"]: (m["presence"], m["leader"]) for m in doc["members"]}, {
            "linux-web": ("here", False), "laptop-web": ("left", False),
            "win-web": ("away", False), "mac-web": ("away", False), "old-web": ("gone", False)})
        self.assertIn("  channel  lobby (a lobby, founded by mac-web)\n",
                      self.ok("whoami", "lobby"))
        # list too: no leader, the founder by its own key
        [ch] = [c for c in json.loads(self.ok("list", "--local", "--json"))["channels"]
                if c["name"] == "lobby"]
        self.assertEqual((ch["kind"], ch["leader"], ch["leaders"], ch["founder"]),
                         ("lobby", None, [], "mac-web"))

    def test_all_is_a_lobbys(self):
        self.assertEqual(self.refused("whoami", "--all", code=3),
                         "ERROR config: --all goes with a lobby's name")
        self.lead()
        self.ok("join", "game", "--local")
        self.assertEqual(self.refused("whoami", "game", "--all", code=3),
                         "ERROR config: --all is for a lobby; game is a work channel")

    def lead(self):
        return LobbyCase.lead(self, where=("--local",))


class JoinPrintTest(LobbyCase):
    def test_only_whats_to_me_from_the_last_day(self):
        self.join("linux")
        now = time.time()
        old = entries.stamp(now - 2 * 86400)
        recent = entries.stamp(now - 3600)
        write_tree(self.root, {"lobby/linux-web/chat-old.md": (
            "# chat\n" + entries.build(old, "linux-web", 10, "old to you", ["@mac-web"])
            + entries.build(old, "linux-web", 11, "old to all", ["@all"])
            + entries.build(recent, "linux-web", 12, "to all", ["@all"])
            + entries.build(recent, "linux-web", 13, "to you", ["@mac-web"])).encode("utf-8")})
        out = self.ok("join", "lobby", "--local")
        self.assertIn("  ## %s — linux-web#13 — to you" % recent, out)
        for left in ("#10", "#11", "#12", "member"):
            self.assertNotIn("linux-web%s —" % left if left != "member" else "— member", out)
        line = [l for l in out.splitlines() if l.startswith("  not shown: ")]
        # left out: #12 to all, #10 to you; the founder's CHANNEL.md #2 is to itself, so
        # neither counted nor listed. read --to-me lists from #10, the oldest of them, on:
        # #10, #11 (an @all from before the 24 h, neither shown nor counted), #12, #13
        self.assertEqual(line, [channel_cmd.platform.runnable(
            "  not shown: 1 to all in the last 24 h, 1 to you older than 24 h; to see them: "
            "vcharon read lobby --to-me --last 4 --project web")])
        listed = self.ok("read", "lobby", "--to-me")
        for n in ("#10", "#11", "#12", "#13"):
            self.assertIn("linux-web%s" % n, listed)
        self.assertNotIn("linux-web#2", listed)

    def test_the_founders_channel_md_is_silent(self):
        self.join("linux")
        [e] = entries.parse_file(os.path.join(self.lobby_folder("linux-web"), "CHANNEL.md"))
        self.assertEqual((e.id, e.to), ("linux-web#2", ("@linux-web",)))
        out = self.ok("join", "lobby", "--local")
        self.assertIn("no entries for mac-web in lobby yet\n", out)
        self.assertNotIn("not shown", out)
        self.assertNotIn("linux-web#", self.ok("read", "lobby", "--to-me"))

    def test_none_shown_but_some_left_out(self):
        self.join("linux")
        recent = entries.stamp(time.time() - 60)
        write_tree(self.root, {"lobby/linux-web/notes.md": (
            "# notes\n" + entries.build(recent, "linux-web", 9, "hi all", ["@all"]))
            .encode("utf-8")})
        lines = self.ok("join", "lobby", "--local").splitlines()
        i = lines.index("no entries for mac-web in the last 24 h")
        # a count of 0 isn't printed
        self.assertTrue(lines[i + 1].startswith("  not shown: 1 to all in the last 24 h; to "
                                                "see them: "), lines[i + 1])

    def test_a_join_after_a_leave_counts_what_came_before_it(self):
        self.join("linux")
        # two leaves: the later one decides
        for _ in range(2):
            self.ok("join", "lobby", "--local")
            self.ok("leave", "lobby")
        now = time.time()
        own = self.lobby_folder()
        [day] = [f for f in os.listdir(own) if f.startswith("chat-")]
        path = os.path.join(own, day)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        [first, second] = [e for e in entries.parse_file(path) if e.title == "LEAVE"]
        self.assertLess(first.number, second.number)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("## %s — %s — LEAVE" % (first.time, first.id),
                                 "## %s — %s — LEAVE" % (entries.stamp(now - 7200), first.id)))
        write_tree(self.root, {"lobby/linux-web/notes.md": (
            "# notes\n"
            + entries.build(entries.stamp(now - 25 * 3600), "linux-web", 8, "old", ["@mac-web"])
            + entries.build(entries.stamp(now - 3600), "linux-web", 9, "missed", ["@mac-web"])
            + entries.build(entries.stamp(now - 1800), "linux-web", 10, "to all", ["@all"])
            + entries.build(entries.stamp(now + 60), "linux-web", 11, "new", ["@mac-web"]))
            .encode("utf-8")})
        out = self.ok("join", "lobby", "--local")
        self.assertIn("— linux-web#11 — new", out)
        # #9, to mac-web before its last LEAVE, may have come while no watcher ran: counted,
        # and the read reaches it; #10 is any @all of the last 24 h; #8, older than 24 h, is
        # counted as that, before the LEAVE or not
        self.assertNotIn("linux-web#9 ", out)
        self.assertNotIn("linux-web#10 ", out)
        line = [l for l in out.splitlines() if l.startswith("  not shown: ")]
        self.assertEqual(line, [channel_cmd.platform.runnable(
            "  not shown: 1 to you before your leave, 1 to all in the last 24 h, 1 to you older "
            "than 24 h; to see them: vcharon read lobby --to-me --last 4 --project web")])
        self.assertIn("linux-web#9  @mac-web  missed",
                      self.ok("read", "lobby", "--to-me", "--last", "4"))

    def test_none_shown_since_the_leave(self):
        self.join("linux")
        self.ok("join", "lobby", "--local")
        self.ok("leave", "lobby")
        write_tree(self.root, {"lobby/linux-web/notes.md": (
            "# notes\n" + entries.build(entries.stamp(time.time() - 3600), "linux-web", 9,
                                        "missed", ["@mac-web"])).encode("utf-8")})
        lines = self.ok("join", "lobby", "--local").splitlines()
        i = lines.index("no entries for mac-web since your leave")
        self.assertTrue(lines[i + 1].startswith("  not shown: 1 to you before your leave; "),
                        lines[i + 1])

if __name__ == "__main__":
    unittest.main()
