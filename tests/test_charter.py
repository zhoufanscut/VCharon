"""A channel's format and its folder and entry limits: CHANNEL.md's header, the checks at
create, join, list and every command that uses a membership, and the limits on the writer's
and the reader's side."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from vcharon import channel_cmd, channels, charter, cli, entries, platform, state
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

from tests import util
from tests.test_channel import ChannelCase
from tests.test_mailbox_watch import member_md
from tests.util import read_tree, write_tree

LIMITS = {"max_mb": 50, "max_files": 1000, "max_entry_kb": 1000}


def channel_md(leader, fmt="1", limits=LIMITS, extra=()):
    """CHANNEL.md's text as create writes it; fmt None leaves the format: line out."""
    header = [("leader", leader), ("created", "2026-10-01 09:00")]
    if fmt is not None:
        header.append(("format", fmt))
    header += [(charter.HEADER_KEYS[k], str(limits[k])) for k in charter.LIMIT_KEYS
               if k in limits] + list(extra)
    return ("# CHANNEL\n" + entries.build("2026-10-01 09:00", leader, 2, "channel game created",
                                          ["@all"], header=header)).encode("utf-8")


class CharterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_header_and_parse(self):
        text = channel_md("lead").decode("utf-8")
        self.assertEqual(charter.parse(text, "lead"), {"format": 1, "limits": LIMITS})
        # another member's #2, or none at all: nothing
        self.assertEqual(charter.parse(text, "other")["format"], None)
        self.assertEqual(charter.parse("", "lead"),
                         {"format": None, "limits": dict.fromkeys(LIMITS)})
        # what create writes reads back
        made = entries.build("t", "lead", 2, "x", ["@all"],
                             header=charter.header("9.9", {"max_mb": 7, "max_files": 70,
                                                           "max_entry_kb": 3}))
        self.assertIn("created by: vcharon 9.9\n", made)
        self.assertEqual(charter.parse(made, "lead"),
                         {"format": 1, "limits": {"max_mb": 7, "max_files": 70,
                                                  "max_entry_kb": 3}})
        # not a whole number: None
        for bad in ("x", "-1", "1.5", "", "١", "1" * 10):
            with self.subTest(bad=bad):
                text = channel_md("lead", fmt=bad).decode("utf-8")
                self.assertIsNone(charter.parse(text, "lead")["format"])

    def test_read_a_file(self):
        folder = os.path.join(self.tmp, "lead")
        write_tree(self.tmp, {"lead/CHANNEL.md": channel_md("lead", "2")})
        self.assertEqual(charter.read(folder, "lead")["format"], 2)
        self.assertIsNone(charter.read(os.path.join(self.tmp, "none"), "lead")["format"])
        if util.CAN_SYMLINK:
            # never through a link
            os.mkdir(os.path.join(self.tmp, "linked"))
            os.symlink(os.path.join(folder, "CHANNEL.md"),
                       os.path.join(self.tmp, "linked", "CHANNEL.md"))
            self.assertIsNone(charter.read(os.path.join(self.tmp, "linked"), "lead")["format"])

    def test_check_same_older_newer_none(self):
        # the same format, and an older one (this vcharon reading format 2 at most)
        self.assertEqual(charter.check("game", {"format": 1, "limits": LIMITS}), LIMITS)
        with mock.patch.object(charter, "FORMAT", 2):
            self.assertEqual(charter.check("game", {"format": 1, "limits": LIMITS}), LIMITS)
        # newer
        with self.assertRaises(VCharonError) as cm:
            charter.check("game", {"format": 2, "limits": LIMITS})
        self.assertEqual((cm.exception.code, cm.exception.message, cm.exception.hint),
                         ("channel", "game uses format 2; this vcharon reads up to 1",
                          "ask your user to run: vcharon --update"))
        # none: not a channel vcharon made
        for info in ({"format": None, "limits": LIMITS}, {}, None, {"format": True},
                     {"format": 0, "limits": LIMITS}):
            with self.subTest(info=info):
                with self.assertRaises(VCharonError) as cm:
                    charter.check("game", info)
                self.assertEqual(cm.exception.message, "game has no format: line in its "
                                 "CHANNEL.md, so it isn't a channel this vcharon made")
                self.assertEqual(cm.exception.hint, charter.NO_FORMAT_HINT)
        # limits a format-1 channel must have
        for limits in ({}, dict(LIMITS, max_mb=None), dict(LIMITS, max_files=1),
                       dict(LIMITS, max_entry_kb=0), dict(LIMITS, max_mb=1, max_entry_kb=1001),
                       dict(LIMITS, max_mb=True)):
            with self.subTest(limits=limits):
                with self.assertRaises(VCharonError) as cm:
                    charter.check("game", {"format": 1, "limits": limits})
                self.assertTrue(cm.exception.message.startswith("game's CHANNEL.md: "),
                                cm.exception.message)

    def test_over_and_totals(self):
        self.assertIsNone(charter.over(100, 10, 100, 10))
        self.assertEqual(charter.over(1500000, 3, 1000000, 10),
                         "1.5 MB in 3 files, 500.0 kB over the limit of 1.0 MB and 10 files")
        self.assertEqual(charter.over(10, 12, 1000, 10),
                         "10 B in 12 files, 2 files over the limit of 1.0 kB and 10 files")
        self.assertEqual(charter.over(2000, 11, 1000, 10),
                         "2.0 kB in 11 files, 1.0 kB and 1 file over the limit of 1.0 kB and "
                         "10 files")
        write_tree(self.tmp, {"a/x": b"12345", "a/sub/y": b"123",
                              "a/.vcharon-stage-1/z": b"zzzz",
                              "a/.vcharon-stage-post-1.tmp": b"tttt"})
        self.assertEqual(charter.folder_total(os.path.join(self.tmp, "a")), (8, 2))
        self.assertEqual(charter.folder_total(os.path.join(self.tmp, "none")), (0, 0))
        if util.CAN_SYMLINK:
            os.symlink(os.path.join(self.tmp, "a", "x"), os.path.join(self.tmp, "a", "l"))
            self.assertEqual(charter.folder_total(os.path.join(self.tmp, "a")), (8, 2))

    def test_size_text(self):
        # the sync's own (cli.size_text is this one)
        self.assertIs(cli.size_text, charter.size_text)
        self.assertEqual(charter.size_text(50 * charter.MB), "50.0 MB")


class FormatTest(ChannelCase):
    """The format: same, older, newer and none, at join and list from the reply, and at post,
    read, watch and sync from the join record."""

    def server_channel(self, fmt, limits=LIMITS):
        """A channel game on the server, its leader lead's CHANNEL.md with format fmt."""
        write_tree(self.root, {"game/lead/CHANNEL.md": channel_md("lead", fmt, limits),
                               "game/lead/MEMBER.md": b"# MEMBER\n"})

    def test_newer_format_refused_at_join(self):
        self.server_channel("2")
        before = self.server_tree()
        for where in (("--server", "fake-dest"), ("--local",)):
            with self.subTest(where=where):
                self.assertEqual(self.refusal("join", "game", *where), (
                    "ERROR channel: game uses format 2; this vcharon reads up to 1",
                    "ask your user to run: vcharon --update"))
        # refused before any claim
        self.assertEqual(self.server_tree(), before)
        self.assertEqual(channel_cmd.records(), [])

    def test_no_format_refused_at_join(self):
        self.server_channel(None)
        before = self.server_tree()
        self.assertEqual(self.refusal("join", "game", "--server", "fake-dest"), (
            "ERROR channel: game has no format: line in its CHANNEL.md, so it isn't a channel "
            "this vcharon made", charter.NO_FORMAT_HINT))
        self.assertEqual(self.server_tree(), before)

    def test_same_and_older_format_join(self):
        # the same format; then this vcharon reading up to 2, a format-1 channel
        self.server_channel("1", dict(LIMITS, max_mb=7))
        self.ok("join", "game", "--server", "fake-dest")
        record = self.record("game.mac-web")
        self.assertEqual((record["format"], record["limits"]), (1, dict(LIMITS, max_mb=7)))
        self.use_box("linux")
        with mock.patch.object(charter, "FORMAT", 2):
            self.ok("join", "game", "--server", "fake-dest")
        self.assertEqual(self.record("game.linux-web")["format"], 1)

    def test_the_claim_reply_is_checked_too(self):
        # the claim reads CHANNEL.md again: a newer format there, or one that differs from
        # the list's, stops the join, and the claim is released
        self.server_channel("1")
        real = channels.claim
        for reply, want in (
                ({"format": 2}, ("ERROR channel: game uses format 2; this vcharon reads up to "
                                 "1", "ask your user to run: vcharon --update")),
                ({"limits": dict(LIMITS, max_mb=7)},
                 ("ERROR channel: game's format or limits changed during the join (format 1, "
                  "then 1)", "run the join again"))):
            with self.subTest(reply=reply):
                def claim(*args, reply=reply, **kw):
                    return dict(real(*args, **kw), **reply)

                before = self.server_tree()
                with mock.patch.object(channel_cmd.channels, "claim", claim):
                    self.assertEqual(self.refusal("join", "game", "--local"), want)
                self.assertEqual(self.server_tree(), before)
                self.assertEqual(channel_cmd.records(), [])

    def test_a_record_with_damaged_limits(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        path = channel_cmd.record_path("game", "mac-web")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        doc["limits"]["max_files"] = 3
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.assertEqual(self.refusal("read", "game"), (
            "ERROR channel: the limits in your join record of game: max files is 3, not 10 to "
            "100000",
            platform.runnable("vcharon join game --server fake-dest --project web (a rejoin) "
                              "writes it")))

    def test_list_shows_the_format(self):
        self.lead()
        write_tree(self.root, {"new/lead/CHANNEL.md": channel_md("lead", "3"),
                               "old/lead/CHANNEL.md": channel_md("lead", None)})
        out = self.ok("list", "--server", "fake-dest")
        lines = out.splitlines()
        self.assertRegex(lines[1], r"\A  game  leader laptop-ui  members laptop-ui  newest "
                                   r"\S+ \S+  format 1\Z")
        [new] = [l for l in lines if l.startswith("  new  ")]
        self.assertTrue(new.endswith("  format 3"), new)
        self.assertIn("    note: new uses format 3; this vcharon reads up to 1; ask your user "
                      "to run: vcharon --update", lines)
        [old] = [l for l in lines if l.startswith("  old  ")]
        self.assertTrue(old.endswith("  format -"), old)
        self.assertIn("    note: old has no format: line in its CHANNEL.md, so it isn't a "
                      "channel this vcharon made; " + charter.NO_FORMAT_HINT, lines)
        # the channels this vcharon reads get no note
        self.assertEqual(len([l for l in lines if "format" in l and "note" in l]), 2)
        doc = json.loads(self.ok("list", "--server", "fake-dest", "--json"))
        got = {ch["name"]: (ch["format"], ch["limits"]) for ch in doc["channels"]}
        self.assertEqual(got, {"game": (1, LIMITS), "new": (3, LIMITS),
                               "old": (None, LIMITS)})

    def test_a_newer_format_in_the_record(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        path = channel_cmd.record_path("game", "mac-web")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        doc["format"] = 2
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        for argv in (("post", "game", "--to", "@laptop-ui", "--title", "t", "--body", "b"),
                     ("read", "game"), ("watch", "game", "--until-change"), ("sync", "game")):
            with self.subTest(argv=argv[0]):
                self.assertEqual(self.refusal(*argv), (
                    "ERROR channel: game uses format 2; this vcharon reads up to 1",
                    "ask your user to run: vcharon --update"))


class LimitsTest(ChannelCase):
    """The leader's limits at create, read by members; the writer's and the reader's side."""

    def test_create_flags(self):
        self.use_box("laptop")
        out = self.ok("create", "game", "--local", "--project", "ui", "--max-mb", "7",
                      "--max-files", "70", "--max-entry-kb", "30")
        self.assertIn("  format 1; limits per member folder 7.0 MB and 70 files, per entry "
                      "file 30.0 kB", out.splitlines())
        own = os.path.join(self.root, "game", "laptop-ui")
        with open(os.path.join(own, "CHANNEL.md"), encoding="utf-8") as f:
            self.assertEqual(charter.parse(f.read(), "laptop-ui"),
                             {"format": 1, "limits": {"max_mb": 7, "max_files": 70,
                                                      "max_entry_kb": 30}})
        self.assertEqual(self.record("game.laptop-ui")["limits"],
                         {"max_mb": 7, "max_files": 70, "max_entry_kb": 30})

    def test_create_flag_bounds(self):
        self.use_box("laptop")
        for flag, bad in (("--max-mb", "0"), ("--max-mb", "10001"), ("--max-files", "9"),
                          ("--max-files", "100001"), ("--max-entry-kb", "0"),
                          ("--max-entry-kb", "10001"), ("--max-mb", "x")):
            with self.subTest(flag=flag, bad=bad):
                code, _out, err = self.channel("create", "game", "--local", flag, bad)
                self.assertEqual(code, 3, err)
                self.assertIn("must be a whole number", err)
        # the highest values are taken
        self.ok("create", "top", "--local", "--max-mb", "10000", "--max-files", "100000",
                "--max-entry-kb", "10000")
        self.assertEqual(self.record("top.laptop-web")["limits"],
                         {"max_mb": 10000, "max_files": 100000, "max_entry_kb": 10000})
        shutil.rmtree(self.root)
        # an entry file can't be bigger than its folder
        self.assertEqual(self.refusal("create", "game", "--local", "--max-mb", "1",
                                      "--max-entry-kb", "1001", code=3), (
            "ERROR config: --max-entry-kb 1001 is more than the folder limit of 1 MB",
            "give a smaller --max-entry-kb, or a larger --max-mb"))
        self.assertEqual(self.server_tree(), {})

    def test_members_read_the_leaders_limits(self):
        self.use_box("laptop")
        self.ok("create", "game", "--local", "--project", "ui", "--max-mb", "200",
                "--max-files", "5000", "--max-entry-kb", "2000")
        self.use_box("mac")
        self.ok("join", "game", "--server", "fake-dest")
        raised = {"max_mb": 200, "max_files": 5000, "max_entry_kb": 2000}
        self.assertEqual(self.record("game.mac-web")["limits"], raised)
        # the section's down passes them to the pull, and up checks against them
        with open(os.path.join(self.homes["mac"], "channels.d", "game.mac-web.ini"),
                  encoding="utf-8") as f:
            text = f.read()
        self.assertIn("mailbox.max_mb = 200\nmailbox.max_files = 5000\n", text)
        cfg = cli.load_config()
        up, down = cfg.named("game.mac-web")
        sizes = {"max_bytes": str(200 * 1000 * 1000), "max_files": "5000"}
        for job in (up, down):
            self.assertEqual({k: job.source.options[k] for k in sizes}, sizes)
        # a local member reads them too
        self.use_box("linux")
        self.ok("join", "game", "--local")
        self.assertEqual(self.record("game.linux-web")["limits"], raised)

    def test_a_section_without_limits_takes_the_defaults(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        path = os.path.join(self.homes["mac"], "channels.d", "game.mac-web.ini")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace("mailbox.max_mb = 50\nmailbox.max_files = 1000\n", ""))
        _up, down = cli.load_config().named("game.mac-web")
        self.assertEqual((down.source.options["max_bytes"], down.source.options["max_files"]),
                         (str(50 * 1000 * 1000), "1000"))
        self.ok("sync", "game")
        # a bad value is the section's error
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace("mailbox.max_files = 1000", "mailbox.max_files = 9"))
        code, out, err = self.channel("sync", "game")
        self.assertEqual(code, 3, out + err)
        self.assertIn("mailbox.max_files: a whole number, 10 to 100000", err)

    # --- the writer ---

    def test_sync_refuses_an_own_folder_over_the_limit(self):
        self.lead(where=("--local",))
        self.use_box("mac")
        self.ok("join", "game", "--server", "fake-dest")
        before = self.server_tree()
        own = os.path.join(self.joined("game.mac-web"), "mac-web")
        # 1000 files are the limit: MEMBER.md, RESULTS.md and 999 more
        write_tree(own, {"out/f%d" % i: b"" for i in range(999)})
        code, out, err = self.channel("sync", "game")
        self.assertEqual(code, 1, out + err)
        lines = err.splitlines()
        self.assertTrue(lines[0].startswith("ERROR game.mac-web.up: too_big: %s holds " % own),
                        lines[0])
        self.assertTrue(lines[0].endswith("1001 files, 1 file over the limit of 50.0 MB and "
                                          "1000 files, so nothing was sent"), lines[0])
        self.assertEqual(lines[1], "  fix: move what isn't an entry (logs, builds, data) out of "
                         "%s, or delete it, until it holds at most 50.0 MB and 1000 files; "
                         "keep MEMBER.md and your .md files with entries" % own)
        # nothing sent
        self.assertEqual(self.server_tree(), before)
        os.remove(os.path.join(own, "out", "f0"))
        self.ok("sync", "game")
        self.assertIn("game/mac-web/out/f1", self.server_tree())

    def test_post_checks_the_folder_and_the_entry(self):
        self.use_box("laptop")
        self.ok("create", "game", "--local", "--project", "ui", "--max-mb", "1",
                "--max-entry-kb", "2")
        own = os.path.join(self.root, "game", "laptop-ui")
        post = ("post", "game", "--project", "ui", "--to", "@all", "--title", "t", "--body")
        before = read_tree(own)
        # a body over the entry limit: refused before anything is read or written
        self.assertEqual(self.refusal(*post, "x" * 2001), (
            "ERROR too_big: the body is 2.0 kB; an entry file holds at most 2.0 kB in this "
            "channel", "shorten it: put long output in a file outside the channel, and say in "
            "the body where it is"))
        # a body under it, but the file would grow past it
        self.ok(*post, "x" * 1500)
        line, fix = self.refusal(*post, "y" * 600)
        self.assertRegex(line, r"\AERROR too_big: .*RESULTS\.md would hold 2\.\d kB with this "
                               r"entry; an entry file holds at most 2\.0 kB in this channel\Z")
        self.assertEqual(fix, "post into a new file of your own folder: add --file NAME.md "
                              "(RESULTS-2.md, say)")
        self.ok(*post, "y" * 600, "--file", "RESULTS-2.md")
        # the folder: a 1 MB build output left in it
        write_tree(own, {"build.bin": b"b" * (1000 * 1000)})
        snapshot = read_tree(own)
        line, fix = self.refusal(*post, "z")
        self.assertRegex(line, r"\AERROR too_big: with this entry your folder (.*) would hold "
                               r"1\.0 MB in 5 files, \d\.\d kB over the limit of 1\.0 MB and "
                               r"1000 files\Z")
        self.assertIn(" folder %s would" % own, line)
        self.assertEqual(fix, "move what isn't an entry (logs, builds, data) out of %s, or "
                              "delete it, until it holds at most 1.0 MB and 1000 files; keep "
                              "MEMBER.md and your .md files with entries" % own)
        self.assertEqual(read_tree(own), snapshot)
        self.assertNotEqual(before, snapshot)
        os.remove(os.path.join(own, "build.bin"))
        self.ok(*post, "z")

    # --- the reader ---

    def down_sent(self, section="game.mac-web"):
        saved = state.load(section + ".down")
        return saved.source["sent"]

    def test_the_pull_skips_a_folder_over_the_limit(self):
        # the leader writes straight into the root (a local member); 10 files are its limit
        self.use_box("laptop")
        self.ok("create", "game", "--local", "--project", "ui", "--max-files", "10")
        lead = os.path.join(self.root, "game", "laptop-ui")
        write_tree(lead, {"keep.txt": b"k", "old.txt": b"o"})
        self.use_box("mac")
        self.ok("join", "game", "--server", "fake-dest")
        self.assertEqual(self.record("game.mac-web")["limits"]["max_files"], 10)
        copy = os.path.join(self.joined("game.mac-web"), "laptop-ui")
        self.assertTrue(os.path.exists(os.path.join(copy, "old.txt")))
        sent = self.down_sent()
        self.assertIn("laptop-ui/old.txt", sent)
        # over the limit: 9 new files, and old.txt deleted, at once
        write_tree(lead, {"new/n%d" % i: b"n" for i in range(9)})
        os.remove(os.path.join(lead, "old.txt"))
        out = self.ok("sync", "game")
        [note] = [l for l in out.splitlines() if "left out" in l]
        self.assertRegex(note, r"\A  note    left out laptop-ui/: \d+ B in 12 files, 2 files "
                               r"over the limit of 50\.0 MB and 10 files; this box's copy of it "
                               r"stays as it was until it is back under\Z")
        # nothing new arrived, nothing was deleted, and the state keeps the old entries
        self.assertFalse(os.path.exists(os.path.join(copy, "new")))
        self.assertTrue(os.path.exists(os.path.join(copy, "old.txt")))
        self.assertEqual(self.down_sent(), sent)
        # saved for the watcher and read, names only: a remote read shows the note, from
        # the limit
        self.assertEqual(charter.load_left_out("game.mac-web"),
                         {"limit": "50.0 MB and 10 files", "members": ["laptop-ui"]})
        saved = ("left out laptop-ui/: over the channel's limit of 50.0 MB and 10 files; this "
                 "box's copy of it stays as it was until it is back under")
        read = self.ok("read", "game").splitlines()
        self.assertIn("note: " + saved, read)
        doc = json.loads(self.ok("read", "game", "--json"))
        self.assertIn(saved, doc["notes"])
        # the writer cleans up: back under, and the delete is planned this time
        shutil.rmtree(os.path.join(lead, "new"))
        out = self.ok("sync", "game")
        self.assertNotIn("left out", out)
        self.assertIsNone(charter.load_left_out("game.mac-web"))
        self.assertNotIn("left out", self.ok("read", "game"))
        self.assertFalse(os.path.exists(os.path.join(copy, "old.txt")))
        self.assertTrue(os.path.exists(os.path.join(copy, "keep.txt")))
        self.assertNotIn("laptop-ui/old.txt", self.down_sent())
        self.assertIn("laptop-ui/keep.txt", self.down_sent())

    def test_a_full_pull_skips_it_too(self):
        self.use_box("laptop")
        self.ok("create", "game", "--local", "--project", "ui", "--max-files", "10")
        lead = os.path.join(self.root, "game", "laptop-ui")
        write_tree(lead, {"a.txt": b"a"})
        self.use_box("mac")
        self.ok("join", "game", "--server", "fake-dest")
        sent = self.down_sent()
        write_tree(lead, {"new/n%d" % i: b"n" for i in range(10)})
        out = self.ok("sync", "game", "--full")
        self.assertIn("left out laptop-ui/", out)
        self.assertEqual(self.down_sent(), sent)
        self.assertFalse(os.path.exists(os.path.join(self.joined("game.mac-web"), "laptop-ui",
                                                     "new")))

    def test_a_local_members_read_skips_it(self):
        self.use_box("laptop")
        self.ok("create", "game", "--local", "--project", "ui", "--max-files", "10")
        lead = os.path.join(self.root, "game", "laptop-ui")
        self.use_box("mac")
        self.ok("join", "game", "--local")
        out = self.ok("read", "game")
        self.assertIn("laptop-ui#2", out)
        write_tree(lead, {"new/n%d" % i: b"n" for i in range(9)})
        out = self.ok("read", "game")
        self.assertNotIn("laptop-ui#2", out)
        [note] = [l for l in out.splitlines() if "left out" in l]
        self.assertRegex(note, r"\Anote: left out laptop-ui/: \d+ B in 11 files, 1 file over "
                               r"the limit of 50\.0 MB and 10 files; it is read again once it "
                               r"is back under\Z")
        doc = json.loads(self.ok("read", "game", "--json"))
        self.assertEqual(doc["members"], ["mac-web"])
        self.assertTrue(any(n.startswith("left out laptop-ui/") for n in doc["notes"]))
        # the member's own folder is never left out
        self.use_box("laptop")
        out = self.ok("read", "game", "--project", "ui")
        self.assertIn("laptop-ui#2", out)


class SyncBrokenPipeTest(ChannelCase):
    def test_a_closed_stdout_is_no_jobs_error(self):
        # vcharon sync C | head -1: the job's own handler hands the BrokenPipeError on, so no
        # ERROR <job>: internal line; the command exits 1, and the log says why
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")

        class Gone(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        err = io.StringIO()
        with mock.patch("sys.stdout", Gone()), mock.patch("sys.stderr", err):
            code = cli.main(["sync", "game"])
        self.assertEqual((code, err.getvalue()), (1, ""))
        logs = os.path.join(self.homes["mac"], "logs")
        text = ""
        for name in os.listdir(logs):
            with open(os.path.join(logs, name), encoding="utf-8") as f:
                text += f.read()
        self.assertIn("stdout closed: [Errno 32] Broken pipe", text)

class WatchLimitTest(unittest.TestCase):
    """A local member's watch holds another member's folder over the limits as it was."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tree = os.path.join(self.tmp, "mb")
        patch = mock.patch.dict(os.environ, {"VCHARON_HOME": os.path.join(self.tmp, "vh")})
        patch.start()
        self.addCleanup(patch.stop)
        write_tree(self.tree, {"debian/MEMBER.md": member_md("debian", "debian"),
                               "mac/MEMBER.md": member_md("mac", "debian")})

    def entry(self, n, title):
        path = os.path.join(self.tree, "mac", "RESULTS.md")
        with open(path, "ab") as f:
            f.write(entries.build("2026-10-01 09:00", "mac", n, title, ["@debian"])
                    .encode("utf-8"))

    def test_held_while_over(self):
        lines = []
        write_tree(self.tree, {"mac/run.log": b"r"})
        warn = ("left out mac/: over the channel's limit of 1.0 kB and 10 files; it is read "
                "again once it is back under")

        def over():
            self.entry(2, "first")
            write_tree(self.tree, {"mac/big.bin": b"b" * 2000})
            os.remove(os.path.join(self.tree, "mac", "run.log"))

        def still():
            self.entry(3, "second")

        def under():
            os.remove(os.path.join(self.tree, "mac", "big.bin"))

        steps = [over, still, under]
        code = watch.watch_dir(self.tree, "debian", 1, out=lines.append,
                               sleep=lambda s: steps.pop(0)(), rounds=3,
                               folder_limits=(1000, 10))
        self.assertEqual(code, 0)
        self.assertEqual([line[20:] for line in lines][1:], [
            # over: nothing new, nothing gone, one warning while it lasts
            "WARN " + warn,
            # back under: what came while it was over, and the file deleted then
            "to you: mac#2 \u2014 first  (mac/RESULTS.md)",
            "to you: mac#3 \u2014 second  (mac/RESULTS.md)",
            "gone mac/run.log",
            "WARN cleared: " + warn])


    def test_over_at_a_fresh_start(self):
        # a folder already over the limits at the start is a baseline: back under, only what
        # came since is told
        lines = []
        self.entry(2, "old")
        write_tree(self.tree, {"mac/big.bin": b"b" * 2000})

        def still():
            self.entry(3, "while over")

        def under():
            os.remove(os.path.join(self.tree, "mac", "big.bin"))

        steps = [still, under]
        code = watch.watch_dir(self.tree, "debian", 1, out=lines.append,
                               sleep=lambda s: steps.pop(0)(), rounds=2, fresh=True,
                               folder_limits=(1000, 10))
        self.assertEqual(code, 0)
        warn = ("left out mac/: over the channel's limit of 1.0 kB and 10 files; it is read "
                "again once it is back under")
        self.assertEqual([line[20:] for line in lines][1:], [
            "WARN " + warn,
            "to you: mac#3 \u2014 while over  (mac/RESULTS.md)",
            "gone mac/big.bin",
            "WARN cleared: " + warn])

if __name__ == "__main__":
    unittest.main()
