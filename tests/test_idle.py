"""Old channels and cleanup: a member's idle time by the server's clock, channel.prune, vcharon
list --remove-idle and list's idle column (DESIGN, "Removing idle members")."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from vcharon import channel_cmd, channels, cli, fsops, platform
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

from tests.test_channel import ChannelCase
from tests.test_lead import charter_text
from tests.test_mailbox_watch import member_md
from tests.util import CAN_SYMLINK, read_tree, write_tree

DAY = 86400
# the server's clock in the CLI tests: every folder there 20 days idle
SHIFT = 20 * DAY


def by_mtime(st):
    """channels._changed with mtimes, which a test can set (a change time can't be set back)."""
    return st.st_mtime


class IdleTest(unittest.TestCase):
    """channels.member_idle on a channel root of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "channels")
        write_tree(self.root, {"game/mac/MEMBER.md": member_md("mac", "lead", "game"),
                               "game/mac/RESULTS.md": b"r", "game/mac/sub/a.patch": b"p"})
        self.folder = os.path.join(self.root, "game", "mac")

    def idle(self, now):
        return channels.member_idle(self.root, "game", "mac", now)

    def set_all(self, t):
        """Every entry's mtime in the folder, the folder too, to t (folders last)."""
        for dirpath, dirnames, filenames in os.walk(self.folder, topdown=False):
            for n in filenames + dirnames:
                os.utime(os.path.join(dirpath, n), (t, t), follow_symlinks=False)
            os.utime(dirpath, (t, t))

    def test_a_writers_mtimes_dont_count(self):
        # a sync copies each file's mtime from the writer's machine: years back or ahead,
        # the folder's idle time is the server's change times'
        for t in (1e9, time.time() + 1e8):
            with self.subTest(t=t):
                self.set_all(t)
                now = time.time() + 3600
                idle, items = self.idle(now)
                self.assertTrue(3590 <= idle <= 3601, idle)
                # the folder, sub/, and three files
                self.assertEqual(items, 5)

    def test_the_stamp_counts_when_it_holds_a_pace(self):
        t = time.time()
        self.assertTrue(channels.stamp_seen(self.root, "game", "mac", "local 10", now=t + 1800))
        idle, _ = self.idle(t + 3600)
        self.assertTrue(1795 <= idle <= 1800, idle)
        # a stamp that isn't one: not counted
        stamp = os.path.join(channels.seen_root(self.root), "game", "mac")
        with open(stamp, "w", encoding="ascii") as f:
            f.write("junk\n")
        os.utime(stamp, (t + 1800, t + 1800))
        idle, _ = self.idle(t + 3600)
        self.assertTrue(3590 <= idle <= 3601, idle)

    def test_stage_dirs_count_and_links_are_never_followed(self):
        old = time.time() - 10 * DAY
        write_tree(self.folder, {".vcharon-stage-1/half": b"h"})
        self.set_all(old)
        stage = os.path.join(self.folder, ".vcharon-stage-1", "half")
        os.utime(stage, (old + 9 * DAY,) * 2)
        now = old + 10 * DAY
        with mock.patch.object(channels, "_changed", by_mtime):
            # a sync's half-written file is activity
            self.assertEqual(self.idle(now)[0], DAY)
            shutil.rmtree(os.path.dirname(stage))
            os.utime(self.folder, (old, old))
            self.assertEqual(self.idle(now)[0], 10 * DAY)
            if not CAN_SYMLINK:
                return
            # a link to a fresh folder elsewhere: its own time counts, never its target's
            fresh = os.path.join(self.tmp, "fresh")
            write_tree(fresh, {"new.md": b"n"})
            link = os.path.join(self.folder, "link")
            os.symlink(fresh, link)
            os.utime(link, (old, old), follow_symlinks=False)
            os.utime(self.folder, (old, old))
            self.assertEqual(self.idle(now), (10 * DAY, 6))

    def test_a_folder_that_isnt_one(self):
        self.assertEqual(channels.member_idle(self.root, "game", "nobody", time.time()),
                         (None, 0))
        write_tree(self.root, {"game/file": b"f"})
        self.assertEqual(channels.member_idle(self.root, "game", "file", time.time()), (None, 0))

    def test_windows_counts_mtimes(self):
        # there st_ctime is the creation time; a Windows root holds local members only
        st = mock.Mock(st_ctime=1.0, st_mtime=2.0)
        with mock.patch.object(fsops, "WINDOWS", True):
            self.assertEqual(channels._changed(st), 2.0)
        with mock.patch.object(fsops, "WINDOWS", False):
            self.assertEqual(channels._changed(st), 1.0)

    def test_no_stamp_for_a_folder_gone(self):
        # a watcher still running once its folder was removed stamps nothing
        shutil.rmtree(self.folder)
        self.assertFalse(channels.stamp_seen(self.root, "game", "mac", "local 10"))
        self.assertFalse(os.path.lexists(os.path.join(channels.seen_root(self.root), "game",
                                                      "mac")))


class PruneTest(unittest.TestCase):
    """channels.prune: checked again at the call, then renamed, then deleted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "channels")
        write_tree(self.root, {"game/lead/CHANNEL.md": charter_text("lead", 2),
                               "game/lead/MEMBER.md": member_md("lead", "lead", "game"),
                               "game/mac/MEMBER.md": member_md("mac", "lead", "game"),
                               "game/mac/RESULTS.md": b"r"})
        self.later = time.time() + SHIFT

    def prune(self, name="mac", days=14, now=None, channel="game", budget=450):
        return channels.prune(self.root, channel, name, days, budget,
                              now=self.later if now is None else now)

    def test_rename_then_delete_and_the_stamp_goes(self):
        channels.stamp_seen(self.root, "game", "mac", "local 10", now=time.time() - 1)
        seen = os.path.join(channels.seen_root(self.root), "game", "mac")
        self.assertTrue(os.path.isfile(seen))
        moved = []
        real = fsops.remove_tree

        def remove_tree(top, name, tick):
            # renamed at the root's top first: the folder is gone from the channel already
            moved.append((name, os.path.isdir(os.path.join(self.root, name)),
                          os.path.lexists(os.path.join(self.root, "game", "mac"))))
            return real(top, name, tick)

        with mock.patch.object(fsops, "remove_tree", remove_tree):
            got = self.prune()
        self.assertTrue(got["removed"].startswith(".vcharon-removed-game-mac-"), got)
        self.assertEqual((got["kept"], got["deleted"]), (None, 3))
        self.assertTrue(SHIFT - 5 <= got["idle"] <= SHIFT, got)
        self.assertEqual(moved, [(got["removed"], True, False)])
        self.assertEqual(sorted(os.listdir(self.root)), ["game"])
        self.assertEqual(os.listdir(os.path.join(self.root, "game")), ["lead"])
        self.assertFalse(os.path.lexists(seen))

    def test_a_failed_delete_leaves_the_renamed_folder_which_list_notes(self):
        with mock.patch.object(fsops, "remove_tree",
                               side_effect=OSError(13, "Permission denied")), \
                self.assertRaises(VCharonError) as cm:
            self.prune()
        [left] = [n for n in os.listdir(self.root) if n != "game"]
        self.assertTrue(left.startswith(channels.REMOVED_PREFIX), left)
        self.assertIn("game/mac is removed, but deleting %s failed: " % left,
                      cm.exception.message)
        self.assertEqual(cm.exception.hint, "delete it by hand; vcharon list's output notes it")
        listing = channels.list_channels(self.root)
        self.assertEqual(listing["others"], [{"name": left, "why": "a member's folder removed "
                                              "as idle that wasn't deleted"}])
        self.assertEqual([c["members"] for c in listing["channels"]], [["lead"]])

    def test_never_a_charter_holder_a_former_leader_too(self):
        # mac took the lead (term 2) after lead#3: both folders hold a CHANNEL.md, and the
        # terms' chain reads both
        write_tree(self.root, {"game/mac/CHANNEL.md": charter_text("mac", 4, term=2,
                                                                   after="lead#3")})
        [ch] = channels.list_channels(self.root)["channels"]
        self.assertEqual((ch["leader"], ch["term"]), ("mac", 2))
        for name in ("lead", "mac"):
            with self.subTest(name=name):
                self.assertEqual(self.prune(name)["kept"], channels.KEPT_CHARTER)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))), ["lead", "mac"])

    def test_a_member_back_since_the_listing_is_kept(self):
        got = self.prune(now=time.time())
        self.assertEqual((got["kept"], got["removed"]), (channels.KEPT_ACTIVE, None))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "game", "mac")))

    def test_idle_over_the_days_not_at_them(self):
        for idle, kept in ((14 * DAY, channels.KEPT_ACTIVE), (14 * DAY + 1, None)):
            with self.subTest(idle=idle), \
                    mock.patch.object(channels, "member_idle", return_value=(idle, 3)):
                self.assertEqual(self.prune()["kept"], kept)

    def test_over_450_kept_in_a_work_channel_not_in_a_lobby(self):
        # the folder, MEMBER.md and RESULTS.md, and n patches
        def fill(folder, n):
            write_tree(folder, {"p%03d" % i: b"x" for i in range(n)})

        mac = os.path.join(self.root, "game", "mac")
        fill(mac, 448)
        self.assertEqual(channels.member_idle(self.root, "game", "mac", self.later)[1], 451)
        self.assertEqual(self.prune()["kept"], channels.KEPT_ITEMS)
        os.remove(os.path.join(mac, "p000"))
        self.assertIsNone(self.prune()["kept"])
        # a lobby's down has no max_deletes: the same folder goes
        channels.claim(self.root, "lobby", "founder", True, lobby=True)
        write_tree(self.root, {"lobby/mac/MEMBER.md": member_md("mac", "founder", "lobby")})
        fill(os.path.join(self.root, "lobby", "mac"), 600)
        got = self.prune(channel="lobby")
        self.assertEqual((got["kept"], got["deleted"]), (None, 602))
        self.assertEqual(self.prune("founder", channel="lobby")["kept"], channels.KEPT_CHARTER)

    def test_one_runs_removals_hold_450_together(self):
        # each remote member's next down deletes them all in one plan
        write_tree(self.root, {"game/win/MEMBER.md": member_md("win", "lead", "game")})
        for name in ("mac", "win"):
            write_tree(os.path.join(self.root, "game", name),
                       {"p%03d" % i: b"x" for i in range(300)})
        got = self.prune("mac", budget=450)
        self.assertEqual((got["kept"], got["items"]), (None, 303))
        got = self.prune("win", budget=450 - 303)
        self.assertEqual((got["kept"], got["items"], got["removed"]),
                         (channels.KEPT_BUDGET, 302, None))
        self.assertEqual(self.prune("win", budget=302)["kept"], None)
        for budget in (-1, 451, None):
            with self.subTest(budget=budget), self.assertRaises(VCharonError) as cm:
                self.prune("lead", budget=budget)
            self.assertEqual(cm.exception.code, "protocol")

    def test_what_isnt_a_members_folder(self):
        self.assertEqual(self.prune("nobody")["kept"], channels.KEPT_GONE)
        write_tree(self.root, {"game/afile": b"f"})
        self.assertEqual(self.prune("afile")["kept"], channels.KEPT_NOT_MEMBER)
        if CAN_SYMLINK:
            elsewhere = os.path.join(self.tmp, "elsewhere")
            write_tree(elsewhere, {"MEMBER.md": b"m"})
            os.symlink(elsewhere, os.path.join(self.root, "game", "link"))
            self.assertEqual(self.prune("link")["kept"], channels.KEPT_NOT_MEMBER)
            self.assertEqual(os.listdir(elsewhere), ["MEMBER.md"])
        with self.assertRaises(VCharonError) as cm:
            self.prune(channel="nope")
        self.assertEqual(cm.exception.code, "not_found")
        for days in (0, 3651, True, "14"):
            with self.subTest(days=days), self.assertRaises(VCharonError) as cm:
                self.prune(days=days)
            self.assertEqual(cm.exception.code, "protocol")
        for channel, name in (("..", "a"), ("game", "..")):
            with self.assertRaises(VCharonError) as cm:
                self.prune(name, channel=channel)
            self.assertEqual(cm.exception.code, "channel")


class RemoveIdleTest(ChannelCase):
    """vcharon list --remove-idle: laptop-ui leads game on this machine's root, mac-web (this
    box's own membership) and linux-web are members; the server's clock is SHIFT ahead, and
    the leader's stamp keeps it active."""

    def setUp(self):
        ChannelCase.setUp(self)
        self.lead("game", where=("--local",))
        self.ok("join", "game", "--local")
        self.use_box("linux")
        self.ok("join", "game", "--local")
        self.use_box("mac")
        os.environ["VCHARON_TEST_CLOCK_SHIFT"] = str(SHIFT)
        self.fresh("laptop-ui")

    def fresh(self, name, channel="game"):
        """name's last-watched stamp at the server's shifted now: not idle."""
        channels.stamp_seen(self.root, channel, name, "local 10", now=time.time() + SHIFT)

    def lines(self, *argv, code=0):
        got, out, err = self.channel("list", "--remove-idle", *argv)
        self.assertEqual(got, code, out + err)
        return out.splitlines()

    def own(self, channel="game"):
        return platform.runnable(channel_cmd.OWN_TEXT % (channel, "--project web"))

    def test_without_yes_nothing_changes(self):
        before = read_tree(self.root)
        lines = self.lines("--local")
        self.assertEqual(lines, [
            "vcharon: list --remove-idle  (this machine)  members idle over 14 days",
            "  game",
            "    remove  linux-web  idle 20 d",
            "    keep  mac-web  idle 20 d: " + self.own(),
            platform.runnable("  to remove them, your user runs: vcharon list --remove-idle "
                              "--yes --local")])
        self.assertEqual(read_tree(self.root), before)

    def test_yes_removes_and_list_shows_idle(self):
        lines = self.lines("--local", "--yes")
        self.assertRegex(lines[2], r"\A    removed  linux-web  idle 20 d: \d+ files and folders "
                                   r"deleted\Z")
        self.assertEqual(lines[3], "    kept  mac-web  idle 20 d: " + self.own())
        self.assertEqual(len(lines), 4)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))),
                         ["laptop-ui", "mac-web"])
        self.assertEqual(os.listdir(self.root), ["game"])
        # list's member lines end with the idle time; the leader's stamp is fresh
        out = self.ok("list", "--local").splitlines()
        self.assertTrue(out[2].startswith("    laptop-ui  ") and out[2].endswith("  idle 0 min"),
                        out)
        self.assertTrue(out[3].endswith("  idle 20 d"), out)
        doc = json.loads(self.ok("list", "--local", "--json"))
        self.assertEqual([(m["name"], m["idle_seconds"] // DAY)
                          for m in doc["channels"][0]["member_info"]],
                         [("laptop-ui", 0), ("mac-web", 20)])
        # nothing left to remove
        self.assertEqual(self.lines("--local", "--yes")[1:],
                         ["  game", "    kept  mac-web  idle 20 d: " + self.own()])

    def test_a_member_back_at_the_removal_is_kept(self):
        # listed 20 days idle; by prune's own clock it was here a moment ago
        clocks = iter([time.time() + SHIFT, time.time()])
        with mock.patch.object(channels, "server_now", lambda: next(clocks)):
            lines = self.lines("--local", "--yes")
        self.assertEqual(lines[2], "    kept  linux-web  idle 0 min: active again")
        self.assertIn("linux-web", os.listdir(os.path.join(self.root, "game")))

    def test_json(self):
        doc = json.loads("\n".join(self.lines("--local", "--json", "--days", "19")))
        self.assertEqual(sorted(doc), ["days", "idle_channels", "members", "server", "yes"])
        self.assertEqual((doc["server"], doc["days"], doc["yes"], doc["idle_channels"]),
                         (None, 19, False, []))
        for one in doc["members"]:
            self.assertTrue(SHIFT - 60 <= one.pop("idle_seconds") <= SHIFT, one)
        self.assertEqual(doc["members"], [
            {"channel": "game", "name": "linux-web", "action": "remove", "why": None,
             "deleted": None},
            {"channel": "game", "name": "mac-web", "action": "keep", "why": "own",
             "deleted": None}])
        doc = json.loads("\n".join(self.lines("--local", "--json", "--yes")))
        self.assertEqual([(m["name"], m["action"], m["why"]) for m in doc["members"]],
                         [("linux-web", "removed", None), ("mac-web", "kept", "own")])
        self.assertIsInstance(doc["members"][0]["deleted"], int)

    def test_days(self):
        # more days than the members are idle: none
        self.assertEqual(self.lines("--local", "--days", "21"), [
            "vcharon: list --remove-idle  (this machine)  members idle over 21 days", "  none"])
        self.assertEqual(self.lines("--local", "--days", "3650")[1], "  none")
        for days in ("0", "3651", "x", "-1"):
            with self.subTest(days=days):
                line = self.refused("list", "--remove-idle", "--local", "--days", days, code=3)
                self.assertIn("--days: must be a whole number of days, 1 to 3650", line)
        # their flags without --remove-idle
        for extra in (("--days", "3"), ("--yes",), ("game",)):
            with self.subTest(extra=extra):
                self.assertEqual(self.refusal("list", "--local", *extra, code=3), (
                    "ERROR config: --days, --yes and channel names go with --remove-idle",
                    platform.runnable("vcharon list --help")))

    def test_named_channels_only_and_one_unknown(self):
        self.lead("docs", where=("--local",), box="win", project="d")
        self.use_box("linux")
        self.ok("join", "docs", "--local")
        self.use_box("mac")
        self.fresh("win-d", "docs")
        both = self.lines("--local")
        self.assertIn("  docs", both)
        self.assertIn("  game", both)
        only = self.lines("--local", "docs")
        self.assertEqual(only[1:3], ["  docs", "    remove  linux-web  idle 20 d"])
        self.assertNotIn("  game", only)
        self.assertTrue(only[-1].endswith("--remove-idle --yes --local docs"), only)
        before = read_tree(self.root)
        self.assertEqual(self.refusal("list", "--remove-idle", "--local", "--yes", "docs",
                                      "nope"),
                         ("ERROR not_found: there is no channel nope",
                          "check the name against vcharon list's output"))
        self.assertEqual(read_tree(self.root), before)

    def test_a_charter_holder_and_a_whole_idle_channel(self):
        # the leader idle, a member active: the leader's folder stays
        os.remove(os.path.join(channels.seen_root(self.root), "game", "laptop-ui"))
        self.fresh("linux-web")
        lines = self.lines("--local")
        self.assertEqual(lines[2], "    keep  laptop-ui  idle 20 d: it holds CHANNEL.md (the "
                                   "leader's, a former leader's, or the lobby founder's)")
        # every member idle, the leader too: one note, no member
        os.remove(os.path.join(channels.seen_root(self.root), "game", "linux-web"))
        lines = self.lines("--local", "--yes")
        self.assertEqual(lines[1:], [channel_cmd.IDLE_CHANNEL_NOTE % ("game", 14)])
        self.assertEqual(len(os.listdir(os.path.join(self.root, "game"))), 3)
        doc = json.loads(self.ok("list", "--remove-idle", "--local", "--json"))
        self.assertEqual((doc["members"], doc["idle_channels"]), ([], ["game"]))

    def test_over_450_files_kept_in_a_work_channel(self):
        write_tree(os.path.join(self.root, "game", "linux-web"),
                   {"p%03d" % i: b"x" for i in range(500)})
        why = channel_cmd.KEPT_TEXT[channels.KEPT_ITEMS]
        # the dry run says so as well, from the listing
        self.assertEqual(self.lines("--local")[2], "    keep  linux-web  idle 20 d: " + why)
        self.assertEqual(self.lines("--local", "--yes")[2], "    kept  linux-web  idle 20 d: "
                         + why)
        self.assertIn("linux-web", os.listdir(os.path.join(self.root, "game")))

    def test_an_unreadable_record_of_the_name_keeps_it(self):
        # it may be this machine's own membership: never guessed
        path = channel_cmd.record_path("game", "linux-web")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.lines("--local")[2],
                         "    keep  linux-web  idle 20 d: " + platform.runnable(
                             channel_cmd.OWN_TEXT % ("game", "<the --project and --role that "
                                                     "make linux-web>")))

    def two_of_303(self):
        """win-web joins too; linux-web and win-web hold 303 files and folders each."""
        self.use_box("win")
        self.ok("join", "game", "--local")
        self.use_box("mac")
        for name in ("linux-web", "win-web"):
            folder = os.path.join(self.root, "game", name)
            write_tree(folder, {"p%03d" % i: b"x"
                                for i in range(303 - len(os.listdir(folder)) - 1)})

    def test_a_failed_removal_spends_its_share(self):
        # it may be renamed already: its copies go with the members' next downs all the same
        self.two_of_303()
        real = fsops.remove_tree
        calls = []

        def remove_tree(*a):
            calls.append(a[1])
            if len(calls) == 1:
                raise OSError(13, "Permission denied")
            return real(*a)

        with mock.patch.object(fsops, "remove_tree", remove_tree):
            got, out, err = self.channel("list", "--remove-idle", "--local", "--yes")
        self.assertEqual(got, 1, out + err)
        lines = out.splitlines()
        self.assertTrue(lines[2].startswith("    failed  linux-web  idle 20 d: "), lines)
        self.assertEqual(lines[4], "    kept  win-web  idle 20 d: "
                         + channel_cmd.KEPT_TEXT[channels.KEPT_BUDGET])
        self.assertEqual(len(calls), 1)

    def test_a_member_kept_for_the_budget_is_offered_again(self):
        # listed, both idle: win-web past the budget; at the call linux-web is back, so its
        # share goes to win-web
        self.two_of_303()
        lines = self.lines("--local")
        self.assertEqual(lines[4], "    keep  win-web  idle 20 d: "
                         + channel_cmd.KEPT_TEXT[channels.KEPT_BUDGET])
        clocks = iter([time.time() + SHIFT, time.time(), time.time() + SHIFT])
        with mock.patch.object(channels, "server_now", lambda: next(clocks)):
            lines = self.lines("--local", "--yes")
        self.assertEqual(lines[2], "    kept  linux-web  idle 0 min: active again")
        self.assertEqual(lines[4], "    removed  win-web  idle 20 d: 303 files and folders "
                                   "deleted")

    def test_a_failed_removal_is_the_error_at_the_end(self):
        with mock.patch.object(fsops, "remove_tree",
                               side_effect=OSError(13, "Permission denied")):
            got, out, err = self.channel("list", "--remove-idle", "--local", "--yes")
        self.assertEqual(got, 1, out + err)
        self.assertRegex(out.splitlines()[2], r"\A    failed  linux-web  idle 20 d: permission: "
                                              r"game/linux-web is removed, but deleting ")
        self.assertIn("ERROR permission: game/linux-web is removed, but deleting ", err)
        notes = [l for l in self.ok("list", "--local").splitlines() if "removed as idle" in l]
        self.assertEqual(len(notes), 1, notes)


class RemoveIdleRemoteTest(ChannelCase):
    """Over the fake ssh: the removal by the server's helper, and what the members see."""

    def watch_remote(self, box, name, project="web"):
        """One --once round of name's watcher on box: (exit code, lines without their time).
        Its sync runs in this process, its failure read as a watcher reads the child's
        stderr (watch.parse_failure)."""
        self.use_box(box)
        lines = []

        def run(job, sync_args, **kw):
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = cli.sync_section(job)
            return watch.parse_failure(code, err.getvalue().splitlines(), job)

        try:
            code = watch.watch_job("game." + name, ["game", "--project", project], 30,
                                   out=lines.append, sleep=lambda s: None, run=run, rounds=1,
                                   once=True)
        finally:
            self.use_box("mac")
        return code, [line[20:] for line in lines]

    def test_removal_sync_watch_leave_and_rejoin(self):
        leader = self.lead()
        self.use_box("linux")
        self.ok("join", "game", "--server", "fake-dest")
        # the leader's watcher takes linux-web's entries as seen
        code, lines = self.watch_remote("laptop", leader, "ui")
        self.assertEqual(code, watch.EXIT_CHANGE, lines)
        self.assertIn("to you: linux-web#2 — JOIN  (linux-web/RESULTS.md)", lines)
        os.environ["VCHARON_TEST_CLOCK_SHIFT"] = str(SHIFT)
        channels.stamp_seen(self.root, "game", leader, "run 30", now=time.time() + SHIFT)
        out = self.ok("list", "--remove-idle", "--server", "fake-dest", "--yes")
        self.assertRegex(out, r"\n    removed  linux-web  idle 20 d: \d+ files and folders "
                              r"deleted\n")
        self.assertEqual(os.listdir(os.path.join(self.root, "game")), [leader])
        del os.environ["VCHARON_TEST_CLOCK_SHIFT"]
        # the member: its sync gives the channel-gone fix, its watcher ends with EXIT closed
        gone = platform.runnable("the channel is closed, or your folder in it is gone: vcharon "
                                 "leave game --project web")
        self.use_box("linux")
        self.assertEqual(self.refusal("sync", "game")[1], gone)
        code, lines = self.watch_remote("linux", "linux-web")
        self.assertEqual(code, watch.EXIT_CLOSED, lines)
        self.assertIn("  fix: " + gone, lines)
        self.assertEqual(lines[-1], "EXIT closed (exit 13)")
        self.use_box("linux")
        out = self.ok("leave", "game")
        self.assertIn("  note    nothing of game as linux-web is left on this machine", out)
        # the leader's next round: the folder is gone from its copy, and with it its marks
        code, lines = self.watch_remote("laptop", leader, "ui")
        self.assertEqual(code, watch.EXIT_NOTHING, lines)
        # a rejoin under the same name: a fresh folder, whose JOIN the leader's watcher prints
        self.use_box("linux")
        self.assertIn("claimed game/linux-web", self.ok("join", "game", "--server", "fake-dest"))
        code, lines = self.watch_remote("laptop", leader, "ui")
        self.assertEqual(code, watch.EXIT_CHANGE, lines)
        self.assertIn("to you: linux-web#2 — JOIN  (linux-web/RESULTS.md)", lines)
        self.assertFalse([line for line in lines if "edited" in line], lines)

    def remote_members(self, *boxes):
        """laptop-ui leads game on the fake server; each box joins it over ssh."""
        leader = self.lead()
        for box in boxes:
            self.use_box(box)
            self.ok("join", "game", "--server", "fake-dest")
        self.use_box("mac")
        return leader

    def test_the_removals_of_one_run_fit_a_members_sync(self):
        # two idle folders of 303 files and folders each: together past the run's 450, and a
        # down's 500 deletes
        leader = self.remote_members("linux", "win")
        for name in ("linux-web", "win-web"):
            folder = os.path.join(self.root, "game", name)
            write_tree(folder, {"p%03d" % i: b"x" for i in range(303 - len(os.listdir(folder))
                                                               - 1)})
        os.environ["VCHARON_TEST_CLOCK_SHIFT"] = str(SHIFT)
        channels.stamp_seen(self.root, "game", leader, "run 30", now=time.time() + SHIFT)
        budget = channel_cmd.KEPT_TEXT[channels.KEPT_BUDGET]
        lines = self.ok("list", "--remove-idle", "--server", "fake-dest").splitlines()
        self.assertEqual(lines[2:4], ["    remove  linux-web  idle 20 d",
                                      "    keep  win-web  idle 20 d: " + budget])
        lines = self.ok("list", "--remove-idle", "--server", "fake-dest", "--yes").splitlines()
        self.assertEqual(lines[2:4], ["    removed  linux-web  idle 20 d: 303 files and folders "
                                      "deleted", "    kept  win-web  idle 20 d: " + budget])
        del os.environ["VCHARON_TEST_CLOCK_SHIFT"]
        # the leader's down deletes its copy in one plan, under max_deletes
        self.use_box("laptop")
        code, out, err = self.channel("sync", "game", "--project", "ui")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("linux-web", os.listdir(self.joined("game.laptop-ui")))
        # the next run takes the other
        self.use_box("mac")
        os.environ["VCHARON_TEST_CLOCK_SHIFT"] = str(SHIFT)
        lines = self.ok("list", "--remove-idle", "--server", "fake-dest", "--yes").splitlines()
        self.assertRegex(lines[2], r"\A    removed  win-web  idle 20 d: ")

    def test_the_server_checks_the_days_again(self):
        # listed 40 days idle (an old listing), 20 days by the server's clock at the call: with
        # --days 30 the server keeps it
        self.remote_members("linux")
        os.environ["VCHARON_TEST_CLOCK_SHIFT"] = str(SHIFT)
        real = channel_cmd._Remote.list

        def old_listing(remote):
            listing = real(remote)
            for ch in listing["channels"]:
                ch["idle"] = {n: 40 * DAY for n in ch["idle"]}
                # the leader active
                ch["idle"]["laptop-ui"] = 0
            return listing

        with mock.patch.object(channel_cmd._Remote, "list", old_listing):
            lines = self.ok("list", "--remove-idle", "--server", "fake-dest", "--days", "30",
                            "--yes").splitlines()
        self.assertEqual(lines[2], "    kept  linux-web  idle 20 d: active again")
        self.assertIn("linux-web", os.listdir(os.path.join(self.root, "game")))


if __name__ == "__main__":
    unittest.main()
