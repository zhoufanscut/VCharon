"""vcharon lead: the leader's offer, a member's takeover by terms, the race at the server, the
gone check, and what every reader makes of a channel whose lead moved (DESIGN, "Leader
handoff")."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import unittest
from unittest import mock

from vcharon import channel_cmd, channels, charter, cli, entries, platform, state
from vcharon.mailbox import watch

from tests.test_channel import ChannelCase, hold, release
from tests.util import write_tree

# a time the gone check takes for "long ago": older than channels.GONE_AFTER
LONG_AGO = 3 * 3600


def charter_text(name, number, term=None, after=None, leader=None):
    """A CHANNEL.md holding one charter of name's."""
    # a taker's is to itself, as vcharon lead writes it; create's to @all
    lines = ["# CHANNEL", "", "## 2026-10-23 10:00:00 — %s#%d — channel" % (name, number),
             "to: %s" % ("@all" if term is None else "@" + name),
             "leader: %s" % (leader or name), "created: 2026-10-23 10:00:00",
             "rules: vcharon guide rules"]
    if term is not None:
        lines.append("term: %d" % term)
    if after is not None:
        lines.append("after: %s" % after)
    lines += ["format: 1", "created by: vcharon 0.8.5", "max mb: 50", "max files: 1000",
              "max entry kb: 1000", ""]
    return "\n".join(lines).encode("utf-8")


def entry(name, number, title, to="@all", header=()):
    return ("\n## 2026-10-23 10:%02d:00 — %s#%d — %s\nto: %s\n%s\nbody\n"
            % (number % 60, name, number, title, to,
               "".join("%s: %s\n" % kv for kv in header))).encode("utf-8")


class ChainTest(unittest.TestCase):
    """charter.Chain: who leads, and whose @all counts, by terms and entry numbers."""

    def chain(self, *charters):
        return charter.Chain([charter.Charter(*c) for c in charters])

    def test_one_term(self):
        c = self.chain(("lead", 2, 1, None))
        self.assertEqual((c.leader, c.term), ("lead", 1))
        # term 1 counts as before any handoff: from #1 on
        self.assertTrue(all(c.counts_all("lead", n) for n in (1, 2, 3, 99)))
        self.assertTrue(c.counts_all("lead", None))
        self.assertFalse(c.counts_all("mac", 5))

    def test_three_terms(self):
        # lead (term 1) to mac after lead#7; mac (charter #4, term 2) to win after mac#9; win's
        # charter #3 (term 4: a marker skipped term 3)
        c = self.chain(("lead", 2, 1, None), ("mac", 4, 2, ("lead", 7)),
                       ("win", 3, 4, ("mac", 9)))
        self.assertEqual((c.leader, c.term), ("win", 4))
        got = {f: [n for n in range(1, 12) if c.counts_all(f, n)]
               for f in ("lead", "mac", "win")}
        self.assertEqual(got, {"lead": [1, 2, 3, 4, 5, 6, 7], "mac": [5, 6, 7, 8, 9],
                               "win": [4, 5, 6, 7, 8, 9, 10, 11]})
        self.assertEqual([f for f in ("lead", "mac", "win") if c.counts_all(f, None)], ["win"])
        self.assertTrue(c.former("lead") and c.former("mac") and not c.former("win"))
        self.assertFalse(c.former("other"))

    def test_a_leader_twice(self):
        # lead hands to mac after lead#5, then takes it back (charter #9, term 3) after mac#6
        c = self.chain(("lead", 2, 1, None), ("mac", 3, 2, ("lead", 5)),
                       ("lead", 9, 3, ("mac", 6)))
        self.assertEqual(c.leader, "lead")
        self.assertEqual([n for n in range(1, 13) if c.counts_all("lead", n)],
                         [1, 2, 3, 4, 5, 10, 11, 12])
        self.assertEqual([n for n in range(1, 13) if c.counts_all("mac", n)], [4, 5, 6])

    def test_a_tie(self):
        c = self.chain(("a", 2, 1, None), ("b", 2, 1, None))
        self.assertIsNone(c.leader)
        self.assertEqual((c.tied, c.term), (["a", "b"], 1))
        self.assertFalse(c.counts_all("a", 3) or c.counts_all("b", 3))

    def test_a_successor_naming_someone_else(self):
        # an after: that names another member gives the earlier term no range: none counts
        c = self.chain(("lead", 2, 1, None), ("mac", 3, 2, ("win", 5)))
        self.assertFalse(any(c.counts_all("lead", n) for n in range(1, 9)))

    def test_fallback(self):
        c = charter.Chain([], "lead")
        self.assertEqual((c.leader, c.term), ("lead", 1))
        self.assertTrue(c.counts_all("lead", 5))
        self.assertFalse(c.counts_all("mac", 5))
        self.assertIsNone(charter.Chain([]).leader)

    def test_from_a_tree(self):
        tree = os.path.join(self._tmp(), "game")
        write_tree(tree, {
            "lead/CHANNEL.md": charter_text("lead", 2),
            "mac/CHANNEL.md": charter_text("mac", 4, 2, "lead#7"),
            # a forged CHANNEL.md entry of someone else's name in mac's file is no charter
            "win/CHANNEL.md": b"# CHANNEL\n",
            "win/MEMBER.md": b"x"})
        c = charter.chain(tree)
        self.assertEqual((c.leader, c.term), ("mac", 2))
        self.assertEqual(c.holders(), ["lead", "mac", "win"])
        # a CHANNEL.md with no entry of its folder's is term 1, as a hand edit
        self.assertEqual([(x.folder, x.number, x.term) for x in c.charters
                          if x.folder == "win"], [("win", 0, 1)])
        # the charter's own entry: the last of the folder's in its CHANNEL.md
        with open(os.path.join(tree, "lead", "CHANNEL.md"), "ab") as f:
            f.write(charter_text("lead", 9, 3, "mac#6").split(b"\n", 2)[2])
        c = charter.chain(tree)
        self.assertEqual((c.leader, c.term), ("lead", 3))
        self.assertEqual(charter.parse(charter.read_text(os.path.join(tree, "lead")),
                                       "lead")["format"], 1)

    def _tmp(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="vcharon-chain-")
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp

    def test_lacks_lead(self):
        for version, lacks in (("0.8.6", False), ("0.8.6rc1", False), ("0.9.0", False),
                               ("0.8.4", True), ("0.1.0", True), (None, True)):
            with self.subTest(version=version):
                self.assertEqual(charter.lacks_lead(version), lacks)
        # this build carries its own number until the release bumps it
        self.assertFalse(charter.lacks_lead(charter.VERSION))


class LeadCase(ChannelCase):
    """laptop-ui leads game over the fake server; mac-web (this box) and win-web are members."""

    def setUp(self):
        ChannelCase.setUp(self)
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.use_box("win")
        self.ok("join", "game", "--server", "fake-dest")
        self.use_box("mac")

    def as_leader(self, *argv, code=0):
        self.use_box("laptop")
        try:
            got, out, err = self.run_cli(*argv, "--project", "ui")
        finally:
            self.use_box("mac")
        self.assertEqual(got, code, out + err)
        return out, err

    def offer(self, to="@mac-web"):
        return self.as_leader("lead", "game", "--to", to)[0]

    def server_charter(self, name):
        return charter.chain(os.path.join(self.root, "game")), os.path.join(
            self.root, "game", name, "CHANNEL.md")

    def listed(self):
        [ch] = [c for c in channels.list_channels(self.root)["channels"] if c["name"] == "game"]
        return ch


class OfferTest(LeadCase):
    def test_offer_then_take_remote(self):
        out = self.offer()
        self.assertIn("  posted HANDOFF laptop-ui#", out)
        self.assertIn("OK  offered the lead of game to mac-web", out)
        handoff = [e for e in entries.parse_file(os.path.join(self.root, "game", "laptop-ui",
                                                              "RESULTS.md"))
                   if e.title == "HANDOFF"]
        self.assertEqual([(e.to, e.header) for e in handoff],
                         [(("@mac-web",), [("lead", "mac-web")])])
        # the leader's highest number at the takeover, for after:
        high = entries.next_number(os.path.join(self.root, "game", "laptop-ui"), "laptop-ui") - 1
        self.ok("sync", "game")
        out = self.ok("lead", "game")
        self.assertIn("  you lead game: term 2, after laptop-ui#%d" % high, out)
        self.assertIn("  posted PLAN mac-web#", out)
        self.assertIn("  posted LEADER mac-web#", out)
        self.assertEqual(out.splitlines()[-2:], [
            platform.runnable("  next: tell your user you lead game now (term 2); then start "
                              "your watcher again (vcharon guide lead): vcharon watch game "
                              "--until-change --project web"),
            "OK  you lead game (term 2)"])
        chain, path = self.server_charter("mac-web")
        self.assertEqual((chain.leader, chain.term), ("mac-web", 2))
        # the server's charter and this box's copy are the same bytes
        local = os.path.join(self.joined("game.mac-web"), "mac-web", "CHANNEL.md")
        with open(path, "rb") as a, open(local, "rb") as b:
            self.assertEqual(a.read(), b.read())
        head = charter.header_of(entries.read_text(path), "mac-web")
        self.assertEqual((head["leader"], head["term"], head["after"], head["format"],
                          head["max mb"]), ("mac-web", "2", "laptop-ui#%d" % high, "1", "50"))
        # the record, the section and MEMBER.md follow
        self.assertEqual(self.record("game.mac-web")["leader"], "mac-web")
        with open(os.path.join(self.homes["mac"], "channels.d", "game.mac-web.ini"),
                  encoding="utf-8") as f:
            self.assertIn("mailbox.leader = mac-web\n", f.read())
        member = entries.header_of(entries.read_text(os.path.join(
            self.root, "game", "mac-web", "MEMBER.md")), "mac-web", 1)
        self.assertEqual(member["leader"], "mac-web")
        # the plan quotes the old leader's STEPS.md (none here); LEADER to all
        steps = entries.parse_file(os.path.join(self.root, "game", "mac-web", "STEPS.md"))
        self.assertEqual([(e.title, e.to, e.header) for e in steps],
                         [("PLAN", ("@mac-web",), [("term", "2")])])
        self.assertIn("laptop-ui's STEPS.md as it was", steps[0].body)
        results = entries.parse_file(os.path.join(self.root, "game", "mac-web", "RESULTS.md"))
        [leader] = [e for e in results if e.title == "LEADER"]
        self.assertEqual(leader.to, ("@all",))
        # the listing, list --json and whoami
        ch = self.listed()
        self.assertEqual((ch["leader"], ch["term"], ch["leaders"]),
                         ("mac-web", 2, ["laptop-ui", "mac-web"]))
        doc = json.loads(self.ok("list", "--server", "fake-dest", "--json"))
        [one] = [c for c in doc["channels"] if c["name"] == "game"]
        self.assertEqual((one["leader"], one["term"]), ("mac-web", 2))
        self.assertIn("  game  leader mac-web (term 2)  members laptop-ui, mac-web, win-web",
                      self.ok("list", "--server", "fake-dest"))
        doc = json.loads(self.ok("whoami", "game", "--json"))
        self.assertEqual((doc["leader"], doc["term"], doc["leads"]), ("mac-web", 2, True))
        self.assertIn("  channel  game, led by mac-web (you), term 2",
                      self.ok("whoami", "game"))
        # the old leader's copy, once synced, says so too
        out, _ = self.as_leader("sync", "game")
        out, _ = self.as_leader("whoami", "game", "--json")
        doc = json.loads(out)
        self.assertEqual((doc["leader"], doc["term"], doc["leads"]), ("mac-web", 2, False))
        # a re-run changes nothing and says who leads
        before = self.server_tree()
        out = self.ok("lead", "game")
        self.assertIn("  you lead game: term 2, after laptop-ui#%d" % high, out)
        self.assertNotIn("posted", out)
        self.assertEqual(self.server_tree(), before)

    def test_a_running_sync_sends_the_offer(self):
        # the leader's watcher's round holds its up job's lock: a note, never a failed offer
        # (a re-run would post a second HANDOFF)
        self.use_box("laptop")
        held = state.lock("game.laptop-ui.up")
        try:
            got, out, err = self.run_cli("lead", "game", "--to", "@mac-web", "--project", "ui")
        finally:
            held.release()
            self.use_box("mac")
        self.assertEqual(got, 0, out + err)
        self.assertEqual(err, "note: a sync of game is running (your watcher's): it sends the "
                              "entry\n")
        self.assertIn("OK  offered the lead of game to mac-web", out)

    def test_a_failed_send_warns(self):
        # the offer is posted and the lead taken at the server: a failed sync is a WARN
        failed = channels.refused("the server said no", "do what it says")
        with mock.patch.object(cli, "run_jobs", side_effect=failed):
            out, err = self.as_leader("lead", "game", "--to", "@mac-web")
        self.assertIn("OK  offered the lead of game to mac-web", out)
        self.assertEqual(err.splitlines()[:2], [
            "WARN not sent to fake-dest: channel: the server said no",
            "  fix: the entry is saved in your folder, but no sync sends it until: do what it "
            "says"])
        # the leader's next sync sends it
        self.as_leader("sync", "game")
        self.ok("sync", "game")
        with mock.patch.object(cli, "run_jobs", side_effect=failed):
            got, out, err = self.run_cli("lead", "game")
        self.assertEqual(got, 0, out + err)
        self.assertIn("OK  you lead game (term 2)", out)
        self.assertIn("WARN not sent to fake-dest: channel: the server said no", err)
        self.assertEqual(self.listed()["leader"], "mac-web")

    def test_offer_then_take_local(self):
        self.lead("docs", where=("--local",), box="linux", project="d")
        self.use_box("linux")
        self.ok("join", "docs", "--local", "--project", "e")
        out = self.ok("lead", "docs", "--to", "@linux-e", "--project", "d")
        self.assertIn("OK  offered the lead of docs to linux-e", out)
        out = self.ok("lead", "docs", "--project", "e")
        self.assertIn("OK  you lead docs (term 2)", out)
        ch = next(c for c in channels.list_channels(self.root)["channels"] if c["name"] == "docs")
        self.assertEqual((ch["leader"], ch["term"]), ("linux-e", 2))
        self.assertEqual(self.record("docs.linux-e")["leader"], "linux-e")
        # the former leader's watcher start sets its MEMBER.md's leader: (silently), and its
        # check prints the LEADER with the next: line
        member = os.path.join(self.root, "docs", "linux-d", "MEMBER.md")
        self.assertEqual(entries.header_of(entries.read_text(member), "linux-d", 1)["leader"],
                         "linux-d")
        code, out, _err = self.run_cli("watch", "docs", "--once", "--project", "d")
        self.assertEqual(code, 0, out)
        self.assertEqual(entries.header_of(entries.read_text(member), "linux-d", 1)["leader"],
                         "linux-e")
        self.assertIn("  next: linux-e leads docs now: send reports, DONE and requests to "
                      "@linux-e; you are a member now; only linux-e closes", out)

    def test_offer_refusals(self):
        # a member offers nothing
        self.assertEqual(self.refusal("lead", "game", "--to", "@win-web"), (
            "ERROR channel: only the leader offers the lead of game, and that is laptop-ui",
            "ask your user"))
        # usage: --to with --gone, --to the leader itself, a bad name
        self.assertEqual(self.refused("lead", "game", "--to", "@x", "--gone", code=3),
                         "ERROR config: --to and --gone don't go together")
        self.use_box("laptop")
        self.assertEqual(self.refused("lead", "game", "--to", "@laptop-ui", "--project", "ui",
                                      code=3), "ERROR config: --to names you")
        self.assertEqual(self.refused("lead", "game", "--to", "@nobody", "--project", "ui"),
                         "ERROR channel: nobody has no folder in game")
        # a member whose vcharon has no lead
        member = os.path.join(self.root, "game", "win-web", "MEMBER.md")
        text = entries.read_text(member)
        entries.set_header(member, os.path.dirname(member), "win-web", 1, "vcharon", "0.8.4")
        self.assertEqual(self.refusal("lead", "game", "--to", "@win-web", "--project", "ui"), (
            "ERROR channel: win-web runs vcharon 0.8.4, which can't take the lead (it came in "
            "0.8.6)", "ask your user: win-web's user updates its vcharon, then offer it again"))
        with open(member, "w", encoding="utf-8") as f:
            f.write(text)
        # a member that left
        self.use_box("win")
        self.ok("leave", "game")
        self.use_box("laptop")
        self.ok("sync", "game", "--project", "ui")
        self.assertEqual(self.refused("lead", "game", "--to", "@win-web", "--project", "ui"),
                         "ERROR channel: win-web left game")

    def test_a_lobby_has_no_leader(self):
        self.ok("join", "lobby", "--server", "fake-dest")
        self.assertEqual(self.refusal("lead", "lobby", code=3), (
            "ERROR config: a lobby has no leader",
            "a lobby's members are equals: address them by name"))
        self.assertEqual(self.refused("lead", "lobby", "--to", "@x", code=3),
                         "ERROR config: a lobby has no leader")


class TakeTest(LeadCase):
    def test_no_offer(self):
        self.assertEqual(self.refusal("lead", "game"), (
            "ERROR channel: laptop-ui hasn't offered you the lead of game",
            platform.runnable("ask laptop-ui to offer it to you: the leader runs vcharon lead "
                              "game --to @mac-web ; if the leader is gone, ask your user")))
        # an offer to another member, then one to this: the latest counts
        self.offer("@win-web")
        self.assertRegex(self.refused("lead", "game"),
                         r"\AERROR channel: laptop-ui's latest offer of the lead of game "
                         r"\(laptop-ui#\d+\) is to win-web, not to you\Z")
        self.offer("@mac-web")
        self.ok("lead", "game")
        # and the leader leads no more: its own leave and close
        self.assertEqual(listed_leader(self), "mac-web")
        # nothing was left of a refusal: no marker but term 2's
        self.assertEqual(os.listdir(os.path.join(self.home, "lead", "game")), ["2"])

    def test_leader_names_the_older_members(self):
        # a member on a vcharon without lead ignores the new leader's @all: LEADER names it
        member = os.path.join(self.root, "game", "win-web", "MEMBER.md")
        entries.set_header(member, os.path.dirname(member), "win-web", 1, "vcharon", "0.8.4")
        self.offer()
        self.ok("sync", "game")
        out = self.ok("lead", "game")
        self.assertIn("into RESULTS.md, to @all @win-web", out)
        results = entries.parse_file(os.path.join(self.root, "game", "mac-web", "RESULTS.md"))
        [leader] = [e for e in results if e.title == "LEADER"]
        self.assertEqual(leader.to, ("@all", "@win-web"))

    def test_the_takers_watcher_running(self):
        self.offer()
        lock = channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web") + ".lock"
        child = hold(self, lock)
        got, _out, err = self.run_cli("lead", "game")
        self.assertEqual(got, 1)
        self.assertIn("ERROR channel: your watcher of mac-web in game is running", err)
        self.assertIn("stop the watcher with %s" % platform.runnable("vcharon watch game --stop "
                                                                    "--project web"), err)
        release(child)
        self.assertEqual(self.listed()["leader"], "laptop-ui")

    def test_crash_after_the_server_wrote_the_charter(self):
        # the run stops after channel.lead (the record write fails): the server has the
        # charter; a re-run finishes the rest, each step once
        self.offer()
        self.ok("sync", "game")
        with mock.patch.object(channel_cmd, "write_record", side_effect=OSError(5, "boom")):
            got, out, _err = self.run_cli("lead", "game")
        self.assertNotEqual(got, 0)
        self.assertEqual(out.splitlines()[-1], platform.runnable(
            "  note: you lead game at the server now (term 2), but the takeover stopped before "
            "its end: once the error below is dealt with, run: vcharon lead game --project web "
            "again"))
        self.assertEqual(self.listed()["leader"], "mac-web")
        self.assertEqual(self.record("game.mac-web")["leader"], "laptop-ui")
        out = self.ok("lead", "game")
        self.assertIn("  you lead game: term 2", out)
        self.assertEqual(self.record("game.mac-web")["leader"], "mac-web")
        own = os.path.join(self.root, "game", "mac-web")
        titles = [e.title for p in entries.md_files(own) for e in entries.parse_file(p)]
        self.assertEqual((titles.count("PLAN"), titles.count("LEADER")), (1, 1))

    def test_crash_between_the_marker_and_the_charter(self):
        # a run that died after its marker: the re-run takes the marker as its own, term 2
        self.offer()
        self.ok("sync", "game")
        write_tree(os.path.join(self.home, "lead", "game", "2"), {"mac-web": b""})
        self.ok("lead", "game")
        self.assertEqual((self.listed()["leader"], self.listed()["term"]), ("mac-web", 2))



def listed_leader(case):
    return case.listed()["leader"]


class ServerLeadTest(ChannelCase):
    """channels.lead at the server: the race, the gone check, the 60 s guard."""

    def setUp(self):
        ChannelCase.setUp(self)
        self.path = os.path.join(self.root, "game")
        write_tree(self.path, {
            "lead/CHANNEL.md": charter_text("lead", 2),
            "lead/MEMBER.md": member_md("lead"),
            "lead/RESULTS.md": b"# RESULTS\n" + entry("lead", 3, "steps"),
            "a/MEMBER.md": member_md("a"), "b/MEMBER.md": member_md("b")})
        self.old()

    def old(self):
        """Every folder of lead's, and the stamps, long ago by this machine's clock."""
        then = time.time() - LONG_AGO
        for dirpath, _dirs, _files in os.walk(os.path.join(self.path, "lead")):
            os.utime(dirpath, (then, then))

    def take(self, name, mode="gone", now=None):
        return channels.lead(self.root, "game", name, 2, mode, "2026-10-23 11:00:00", now=now)

    def test_gone(self):
        got = self.take("a")
        self.assertEqual((got["term"], got["after"], got["from"]), (2, "lead#3", "lead"))
        self.assertTrue(got["charter"].startswith("# CHANNEL\n\n## 2026-10-23 11:00:00 — a#2 — "))
        self.assertEqual(charter.chain(self.path).leader, "a")
        # the old leader is no one to take from any more: b's takeover is of a's lead
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, "lead", "game"))), ["2"])

    def test_active_by_its_stamp_or_a_folder(self):
        now = time.time()
        channels.stamp_seen(self.root, "game", "lead", "stream 2", now=now - 600)
        with self.assertRaises(channels.VCharonError) as e:
            self.take("a", now=now)
        self.assertEqual(e.exception.message, "lead was active 10 min ago (its watcher, or a "
                         "change in its folder); a gone leader is one inactive for 30 min")
        os.utime(os.path.join(self.home, "seen", "game", "lead"), (now - LONG_AGO,) * 2)
        # a stage dir of a sync under way counts, by its folder's own mtime
        stage = os.path.join(self.path, "lead", ".vcharon-stage-x")
        os.mkdir(stage)
        os.utime(stage, (now - 120, now - 120))
        os.utime(os.path.join(self.path, "lead"), (now - LONG_AGO,) * 2)
        with self.assertRaises(channels.VCharonError) as e:
            self.take("a", now=now)
        self.assertIn("lead was active 2 min ago", e.exception.message)
        # a file's own mtime (copied from the writer's clock) doesn't count
        os.rmdir(stage)
        os.utime(os.path.join(self.path, "lead"), (now - LONG_AGO,) * 2)
        os.utime(os.path.join(self.path, "lead", "RESULTS.md"), (now, now))
        self.assertEqual(self.take("a", now=now)["term"], 2)

    def race(self):
        """a and b take at once, both past reading the terms before either's mkdir; {name:
        its term, or its refusal's text}."""
        barrier = threading.Barrier(2)
        real = channels._next_term

        def next_term(*a, **kw):
            got = real(*a, **kw)
            # both have read the terms before either makes its marker
            barrier.wait(10)
            return got

        results = {}

        def run(name):
            try:
                results[name] = self.take(name)["term"]
            except channels.VCharonError as e:
                results[name] = e.message

        with mock.patch.object(channels, "_next_term", next_term):
            threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        return results

    def test_the_race_has_one_winner(self):
        results = self.race()
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, "lead", "game"))), ["2"])
        winner = [n for n, r in results.items() if r == 2]
        self.assertEqual(len(winner), 1, results)
        [loser] = [n for n in results if n not in winner]
        self.assertEqual(results[loser], "%s took the lead of game first" % winner[0])
        self.assertEqual(charter.chain(self.path).leader, winner[0])
        self.assertFalse(os.path.exists(os.path.join(self.path, loser, "CHANNEL.md")))

    def test_a_marker_under_way(self):
        marker = os.path.join(self.home, "lead", "game", "2")
        write_tree(marker, {"b": b""})
        with self.assertRaises(channels.VCharonError) as e:
            self.take("a")
        self.assertEqual(e.exception.message, "a takeover or close of game is under way (by b)")
        # past the guard, it is skipped: term 3
        then = time.time() - channels.LEAD_WAIT - 1
        os.utime(marker, (then, then))
        self.assertEqual(self.take("a")["term"], 3)
        self.assertEqual(charter.chain(self.path).term, 3)

    def test_a_stale_own_marker(self):
        # a's run died after its marker, long ago: a and b at once must still have one winner,
        # so a takes no marker as its own once it is past the guard
        marker = os.path.join(self.home, "lead", "game", "2")
        write_tree(marker, {"a": b""})
        then = time.time() - channels.LEAD_WAIT - 1
        os.utime(marker, (then, then))
        results = self.race()
        winner = [n for n, r in results.items() if r == 3]
        self.assertEqual(len(winner), 1, results)
        [loser] = [n for n in results if n not in winner]
        self.assertEqual(results[loser], "%s took the lead of game first" % winner[0])
        self.assertFalse(os.path.exists(os.path.join(self.path, loser, "CHANNEL.md")))
        self.assertEqual(charter.chain(self.path).leader, winner[0])

    def test_a_new_channel_drops_an_old_ones_markers(self):
        # markers of an earlier channel game, left when its sweep failed
        shutil.rmtree(self.path)
        write_tree(os.path.join(self.home, "lead", "game", "7"), {"a": b""})
        channels.claim(self.root, "game", "lead", True)
        self.assertFalse(os.path.exists(os.path.join(self.home, "lead", "game")))

    def test_recovery_reply(self):
        # the creator, asked to take what it has
        with self.assertRaises(channels.VCharonError) as e:
            channels.lead(self.root, "game", "lead", 9, "gone", "2026-10-23 11:00:00")
        self.assertEqual(e.exception.message, "you lead game already")
        self.take("a")
        again = self.take("a")
        self.assertEqual((again["term"], again["after"]), (2, "lead#3"))
        # the old leader, gone or not, takes it back only from a gone a
        with self.assertRaises(channels.VCharonError) as e:
            channels.lead(self.root, "game", "lead", 9, "gone", "2026-10-23 11:00:00")
        self.assertIn("a was active 0 min ago", e.exception.message)

    def test_a_failed_write_drops_the_marker(self):
        with (mock.patch.object(entries, "append", side_effect=OSError(28, "No space left")),
              self.assertRaises(channels.VCharonError) as e):
            self.take("a")
        self.assertEqual(e.exception.code, "no_space")
        self.assertEqual(os.listdir(os.path.join(self.home, "lead", "game")), [])
        self.assertEqual(charter.chain(self.path).leader, "lead")
        self.assertEqual(self.take("a")["term"], 2)

    def test_a_failed_close_drops_its_marker(self):
        # the close's rename fails: its term 2 marker goes, so a takeover right after isn't
        # refused as one under way
        with (mock.patch.object(channels.fsops, "retry_in_use",
                                side_effect=OSError(5, "Input/output error")),
              self.assertRaises(channels.VCharonError)):
            channels.remove(self.root, "game", "lead")
        self.assertEqual(os.listdir(os.path.join(self.home, "lead", "game")), [])
        self.assertEqual(self.take("a")["term"], 2)

    def test_steps_cut_to_the_entry_limit(self):
        write_tree(self.path, {"lead/STEPS.md": b"x" * 10 + b"\n" + b"y" * 999000 + b"\n"})
        self.old()
        got = self.take("a")
        self.assertEqual((got["steps"], got["steps_cut"]), ("x" * 10 + "\n", True))

    def test_sweep(self):
        self.take("a")
        shutil.rmtree(self.path)
        channels.sweep_seen(self.root)
        self.assertFalse(os.path.exists(os.path.join(self.home, "lead", "game")))


class WatchTermsTest(ChannelCase):
    """A local member's watcher over a channel whose lead moved twice: lead (term 1) to mac
    after lead#4, mac (term 2) to win after mac#7; win took term 3."""

    def setUp(self):
        ChannelCase.setUp(self)
        self.path = os.path.join(self.root, "game")
        write_tree(self.path, {n + "/MEMBER.md": member_md(n)
                               for n in ("lead", "mac", "win", "me")})
        write_tree(self.path, {"lead/CHANNEL.md": charter_text("lead", 2)})

    def run_watch(self, me, *steps):
        from tests.test_mailbox_watch import Rounds
        lines = []
        watch.watch_dir(self.path, me, 10, out=lines.append, sleep=Rounds(*steps),
                        rounds=len(steps), fresh=True)
        return [line[20:] for line in lines]

    def posted(self):
        write_tree(self.path, {
            "lead/RESULTS.md": b"# RESULTS\n" + entry("lead", 3, "one") + entry("lead", 4, "two")
            + entry("lead", 8, "late") + entry("lead", 9, "CLOSED"),
            "mac/CHANNEL.md": charter_text("mac", 5, 2, "lead#4"),
            "mac/RESULTS.md": b"# RESULTS\n" + entry("mac", 3, "as a member") + entry(
                "mac", 6, "LEADER", header=[("term", "2")]) + entry("mac", 7, "last of mac")
            + entry("mac", 10, "after"),
            "win/CHANNEL.md": charter_text("win", 3, 3, "mac#7"),
            "win/RESULTS.md": b"# RESULTS\n" + entry("win", 4, "LEADER", header=[("term", "3")])
            + entry("win", 11, "go on")})

    def test_a_member(self):
        lines = self.run_watch("me", self.posted)
        # each @all by its poster's term: lead's up to lead#4, mac's from its charter (#5) to
        # mac#7, win's after its charter (#3). The former leaders' later @all, the CLOSED too,
        # and mac's as a member, are ignored; only the leader's LEADER gets the next: line.
        # The takers' charters are to themselves
        # (the other entries line is held, and comes before the next line printed)
        self.assertEqual(lines[1:], [
            "2 other entries (mac, win)",
            "to all: lead#3 — one  (lead/RESULTS.md)",
            "to all: lead#4 — two  (lead/RESULTS.md)",
            "to all: win#4 — LEADER  (win/RESULTS.md)",
            "  next: win leads game now: send reports, DONE and requests to @win",
            "to all: mac#6 — LEADER  (mac/RESULTS.md)",
            "to all: mac#7 — last of mac  (mac/RESULTS.md)",
            "to all: win#11 — go on  (win/RESULTS.md)",
            "note: @all from lead, mac, not the leader: ignored"])

    def test_a_former_leader_and_the_closing_line(self):
        def closed():
            with open(os.path.join(self.path, "win", "RESULTS.md"), "ab") as f:
                f.write(entry("win", 12, "CLOSED"))

        lines = self.run_watch("lead", self.posted, closed)
        flags = "<the --project and --role that make lead>"
        self.assertIn("  next: win leads game now: send reports, DONE and requests to @win; you "
                      "are a member now; only win closes", lines)
        # the new leader's CLOSED tells the former one to leave
        i = lines.index("to all: win#12 — CLOSED  (win/RESULTS.md)")
        self.assertEqual(lines[i + 1], platform.runnable(
            watch.CLOSED_NEXT % ("game", flags, "game", flags)))

    def test_the_offer(self):
        lines = self.run_watch("me", lambda: write_tree(self.path, {
            "lead/RESULTS.md": b"# RESULTS\n" + entry("lead", 3, "HANDOFF", to="@me",
                                                     header=[("lead", "me")])
            + entry("lead", 4, "HANDOFF", to="@me", header=[("lead", "win")]),
            "mac/RESULTS.md": b"# RESULTS\n" + entry("mac", 3, "HANDOFF", to="@me",
                                                    header=[("lead", "me")])}))
        flags = "<the --project and --role that make me>"
        offer = platform.runnable("  next: lead offers you the lead of game: stop your watcher "
                                  "if it runs (vcharon watch game --stop %s), then run: vcharon "
                                  "lead game %s ; then tell your user" % (flags, flags))
        # only the leader's, and only one naming this member
        self.assertEqual([line for line in lines if line.startswith(("to you", "  next"))], [
            "to you: lead#3 — HANDOFF  (lead/RESULTS.md)", offer,
            "to you: mac#3 — HANDOFF  (mac/RESULTS.md)",
            "to you: lead#4 — HANDOFF  (lead/RESULTS.md)"])


class AfterHandoffTest(LeadCase):
    """What the others make of a channel whose lead moved: read, join, post, leave, close."""

    def setUp(self):
        LeadCase.setUp(self)
        # the old leader's @all from before the takeover: to all wherever it arrives
        self.as_leader("post", "game", "--to", "@all", "--title", "early", "--body", "b")
        self.offer()
        self.ok("sync", "game")
        self.ok("lead", "game")
        self.as_leader("sync", "game")

    def test_read_to_me_and_join(self):
        # the former leader's @all after the takeover, written by hand: no one's to all
        late = os.path.join(self.root, "game", "laptop-ui", "RESULTS.md")
        with open(late, "ab") as f:
            f.write(entry("laptop-ui", 50, "late"))
        self.use_box("win")
        self.ok("sync", "game")
        titles = [e["title"] for e in json.loads(self.ok("read", "game", "--to-me",
                                                         "--json"))["entries"]]
        self.assertIn("LEADER", titles)
        self.assertIn("early", titles)
        self.assertNotIn("late", titles)
        doc = json.loads(self.ok("read", "game", "--json"))
        self.assertEqual((doc["leader"], doc["term"]), ("mac-web", 2))
        # a rejoin takes the new leader into the record
        self.assertEqual(self.record("game.win-web")["leader"], "laptop-ui")
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  took back game/win-web; the leader is mac-web", out)
        self.assertEqual(self.record("game.win-web")["leader"], "mac-web")
        # a new member: its JOIN to the new leader, and the entries to all it is shown
        self.use_box("lin")
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  claimed game/lin-web; the leader is mac-web", out)
        self.assertIn("  ## ", out)
        self.assertIn("— LEADER", out)
        self.assertIn("— early", out)
        self.assertNotIn("— late", out)
        results = entries.parse_file(os.path.join(self.root, "game", "lin-web", "RESULTS.md"))
        self.assertEqual([(e.title, e.to) for e in results], [("JOIN", ("@mac-web",))])

    def test_an_old_offer_counts_no_more(self):
        # laptop-ui to mac-web, then back: laptop-ui's first HANDOFF, to mac-web, is from
        # before its term 3 charter, so mac-web needs a new offer
        self.ok("lead", "game", "--to", "@laptop-ui")
        self.as_leader("sync", "game")
        out, _ = self.as_leader("lead", "game")
        self.assertIn("OK  you lead game (term 3)", out)
        self.ok("sync", "game")
        self.assertEqual(self.refused("lead", "game"),
                         "ERROR channel: laptop-ui hasn't offered you the lead of game")
        self.assertEqual((self.listed()["leader"], self.listed()["term"]), ("laptop-ui", 3))

    def test_post(self):
        self.use_box("laptop")
        self.assertEqual(self.refused("post", "game", "--to", "@all", "--title", "t", "--body",
                                      "b", "--project", "ui"),
                         "ERROR channel: @all is the leader's (mac-web since term 2)")
        self.assertEqual(self.refused("post", "game", "--steps", "--to", "@all", "--title", "t",
                                      "--body", "b", "--project", "ui"),
                         "ERROR channel: STEPS.md is the leader's (mac-web): its plan")
        self.use_box("mac")
        self.ok("post", "game", "--steps", "--to", "@all", "--title", "steps", "--body", "b")
        self.ok("post", "game", "--to", "@all", "--title", "all", "--body", "b")
        # a second charter of term 2, written by hand: no one leads, and the refusal says why
        write_tree(os.path.join(self.root, "game", "win-web"),
                   {"CHANNEL.md": charter_text("win-web", 2, term=2, after="laptop-ui#9")})
        self.ok("sync", "game")
        self.assertEqual(self.refused("post", "game", "--to", "@all", "--title", "t", "--body",
                                      "b"),
                         "ERROR channel: @all is the leader's (game has 2 leaders at term 2 "
                         "(mac-web/, win-web/ hold its charter))")

    def test_leave(self):
        self.assertEqual(self.refused("leave", "game"),
                         "ERROR channel: you lead game: hand the lead over, or close it")
        # the former leader leaves as a member, its LEAVE to the new leader
        out, _ = self.as_leader("leave", "game")
        self.assertIn("  posted LEAVE laptop-ui#", out)
        self.assertIn("into RESULTS.md, to @mac-web", out)

    def test_close(self):
        # its watcher running: the listing's refusal, not a stop of the watcher first
        self.use_box("laptop")
        lock = channel_cmd.watcher_snapshot(None, "game.laptop-ui", "laptop-ui") + ".lock"
        self.use_box("mac")
        child = hold(self, lock)
        _out, err = self.as_leader("close", "game", code=1)
        release(child)
        self.assertEqual(err.splitlines()[0],
                         "ERROR channel: only the leader closes game, and that is mac-web")
        # a takeover under way: the close waits
        marker = os.path.join(self.home, "lead", "game", "3")
        write_tree(marker, {"win-web": b""})
        self.assertEqual(self.refused("close", "game"),
                         "ERROR channel: a takeover or close of game is under way (by win-web)")
        shutil.rmtree(marker)
        out = self.ok("close", "game")
        self.assertIn("  closed game:", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, "lead", "game")))

    def test_the_server_refuses_a_former_leaders_close(self):
        with self.assertRaises(channels.VCharonError) as e:
            channels.remove(self.root, "game", "laptop-ui")
        self.assertEqual(e.exception.message, "mac-web leads game since term 2; laptop-ui led "
                         "it before: only the leader closes it")
        # a takeover that took the next term's marker first: the close is refused, and drops
        # nothing of it
        write_tree(os.path.join(self.home, "lead", "game", "3"), {"win-web": b""})
        with self.assertRaises(channels.VCharonError) as e:
            channels.remove(self.root, "game", "mac-web")
        self.assertIn("under way", e.exception.message)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "game")))
        # the close's own marker excludes a takeover: term 3's was the close's
        shutil.rmtree(os.path.join(self.home, "lead", "game", "3"))
        real = channels._take_marker
        taken = []

        def take_marker(*a, **kw):
            path = real(*a, **kw)
            taken.append(sorted(os.listdir(path)))
            return path

        with mock.patch.object(channels, "_take_marker", take_marker):
            channels.remove(self.root, "game", "mac-web")
        self.assertEqual(taken, [["mac-web"]])


def member_md(name):
    return ("# MEMBER\n\n## 2026-10-23 10:00:00 — %s#1 — member\nto: @lead\nleader: lead\n"
            "vcharon: 0.8.6\n" % name).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
