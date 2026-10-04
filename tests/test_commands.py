"""The membership a command means (DESIGN, "Which membership"), whoami, list --json, sync,
and fix lines that parse back into the command line (DESIGN, "Fix lines")."""

from __future__ import annotations

import ast
import contextlib
import glob
import io
import json
import os
import re
import shlex
import shutil
import unittest
from unittest import mock

from vcharon import VERSION, channel_cmd, cli, config, keys, platform
from vcharon.proto import VCharonError

from tests.test_channel import ChannelCase
from tests.util import CAN_SYMLINK, PACKAGE_DIR, read_tree, write_tree


class IdentityTest(ChannelCase):
    """post, read, watch, sync, leave, close and whoami find the member by channel + project +
    role, through this box's join records; join and create look there first too."""

    def name_of(self, *argv):
        code, out, err = self.run_cli("whoami", "game", "--json", *argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)["name"]

    def test_by_channel_project_and_role(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.ok("join", "game", "--server", "fake-dest", "--role", "b")
        self.assertEqual(self.name_of(), "mac-web")
        self.assertEqual(self.name_of("--role", "b"), "mac-web-b")
        # from elsewhere: --project says which
        os.chdir(self.tmp)
        self.assertEqual(self.name_of("--project", "web"), "mac-web")
        self.assertEqual(self.name_of("--project", "Web", "--role", "b"), "mac-web-b")

    def test_only_role_memberships(self):
        self.lead()
        for role in ("c", "b"):
            self.ok("join", "game", "--server", "fake-dest", "--role", role)
        for verb in (["sync", "game"], ["read", "game"], ["watch", "game"], ["leave", "game"],
                     ["whoami", "game"],
                     ["post", "game", "--to", "@laptop-ui", "--title", "t", "--body", "b"]):
            with self.subTest(verb=verb):
                self.assertEqual(self.refusal(*verb), (
                    "ERROR channel: you are in game from web only with a role",
                    "pass --role b or --role c"))

    def test_none(self):
        self.lead()
        self.assertEqual(self.refusal("sync", "game"), (
            "ERROR channel: you aren't in game as --project web (no join record on this box)",
            platform.runnable("join it first, with --server ALIAS (or --local on the machine "
                              "that holds the channel): vcharon join game --server ALIAS "
                              "--project web")))
        self.ok("join", "game", "--server", "fake-dest", "--project", "api", "--role", "x")
        self.assertEqual(self.refusal("sync", "game")[1], "pass the --project and --role you "
                         "joined with: --project api --role x")
        # a bad --role or channel is a usage error, before any record is read
        self.assertTrue(self.refused("sync", "game", "--role", "B", code=3).startswith(
            "ERROR config: --role B: 1 to 6 characters"))
        self.assertTrue(self.refused("sync", "Game", code=3).startswith("ERROR config: "))

    def test_a_join_notes_the_other_roles(self):
        # only step 1 runs at join: a forgotten --role makes another member, and says so
        self.lead()
        out = self.ok("join", "game", "--server", "fake-dest", "--role", "b")
        self.assertNotIn("note: you also hold", out)
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertEqual(out.splitlines()[0], "note: you also hold game here as --role b")
        self.assertEqual(out.splitlines()[1], "vcharon: join game  as mac-web on fake-dest")
        out = self.ok("join", "game", "--server", "fake-dest", "--role", "c")
        self.assertEqual(out.splitlines()[:2], [
            "note: you also hold game here as --role b",
            "note: you also hold game here as the member without a role"])
        # a rejoin of a membership found by its record: nothing to note
        out = self.ok("join", "game", "--server", "fake-dest", "--role", "c")
        self.assertNotIn("note: you also hold", out)
        # another project's memberships aren't this one's
        out = self.ok("join", "game", "--server", "fake-dest", "--project", "api")
        self.assertNotIn("note: you also hold", out)

    def test_a_box_renamed_after_the_join(self):
        # the record keeps the name it joined with: a join after the box changed takes the
        # membership back, and makes no second one
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        out = self.ok("setup", "--box", "macbook")
        self.assertIn("  note: your channels keep the names you joined with", out)
        self.assertEqual(self.name_of(), "mac-web")
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("vcharon: join game  as mac-web on fake-dest", out)
        self.assertIn("  took back game/mac-web; the leader is laptop-ui", out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "game"))),
                         ["laptop-ui", "mac-web"])
        self.assertEqual(sorted(f for f in os.listdir(channel_cmd.records_dir())),
                         ["game.mac-web.json"])
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        # whoami without a channel: the name a new join would take, and the one kept
        _code, out, _err = self.run_cli("whoami", "--json")
        doc = json.loads(out)
        self.assertEqual((doc["box"], doc["name"], [c["name"] for c in doc["channels"]]),
                         ("macbook", "macbook-web", ["mac-web"]))
        # create looks too: the leader, renamed, closes as itself
        self.use_box("laptop")
        with open(os.path.join(self.homes["laptop"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = lap\n")
        self.assertEqual(self.refusal("create", "game", "--server", "fake-dest", "--project",
                                      "ui")[0],
                         "ERROR channel: the channel game already exists")
        self.assertFalse(os.path.exists(os.path.join(self.root, "game", "lap-ui")))

    def test_two_records_for_one_membership(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        doc = dict(self.record("game.mac-web"), name="mac2-web")
        channel_cmd.write_record(doc)
        self.assertEqual(self.refusal("sync", "game"), (
            "ERROR channel: 2 records on this box are for game --project web: mac-web, mac2-web",
            "ask the user which membership is this one"))

    def test_a_broken_record_is_an_error(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        with open(channel_cmd.record_path("game", "x"), "w") as f:
            f.write("{")
        line = self.refused("sync", "game", code=3)
        self.assertTrue(line.startswith("ERROR config: the record %s can't be read"
                                        % channel_cmd.record_path("game", "x")), line)


class WhoamiTest(ChannelCase):
    def test_a_membership(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest", "--role", "b")
        code, out, err = self.run_cli("whoami", "game", "--role", "b", "--json")
        self.assertEqual((code, err), (0, ""))
        tree = self.joined("game.mac-web-b")
        self.assertEqual(json.loads(out), {
            "channel": "game", "name": "mac-web-b", "project": "web", "role": "b",
            "leader": "laptop-ui", "leads": False, "mode": "remote", "server": "fake-dest",
            "folder": os.path.join(tree, "mac-web-b"), "tree": tree, "box": "mac",
            "box_source": "config"})
        code, out, err = self.run_cli("whoami", "game", "--role", "b")
        self.assertEqual(out.splitlines(), [
            "vcharon: whoami game",
            "  channel  game, led by laptop-ui",
            "  name     mac-web-b  (--project web --role b)",
            "  mode     remote, server fake-dest",
            "  folder   %s" % os.path.join(tree, "mac-web-b"),
            "  box      mac (set in %s)" % os.path.join(self.homes["mac"], "vcharon.ini"),
            "  vcharon  %s" % VERSION])
        # the leader, a local member
        self.lead("docs", where=("--local",), box="linux", project="d")
        self.use_box("linux")
        doc = json.loads(self.run_cli("whoami", "docs", "--project", "d", "--json")[1])
        self.assertEqual((doc["leads"], doc["mode"], doc["server"], doc["folder"], doc["role"]),
                         (True, "local", None, os.path.join(self.root, "docs", "linux-d"), None))

    def test_without_a_channel(self):
        self.lead()
        self.lead("docs")
        self.ok("join", "game", "--server", "fake-dest")
        self.ok("join", "docs", "--server", "fake-dest", "--role", "r")
        code, out, err = self.run_cli("whoami", "--json")
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertEqual(sorted(doc), ["box", "box_source", "channels", "name", "project",
                                       "role"])
        self.assertEqual((doc["box"], doc["box_source"], doc["project"], doc["role"],
                          doc["name"]), ("mac", "config", "web", None, "mac-web"))
        self.assertEqual([(c["channel"], c["name"]) for c in doc["channels"]],
                         [("docs", "mac-web-r"), ("game", "mac-web")])
        # --role narrows it to that role's
        doc = json.loads(self.run_cli("whoami", "--role", "r", "--json")[1])
        self.assertEqual((doc["name"], [c["name"] for c in doc["channels"]]),
                         ("mac-web-r", ["mac-web-r"]))
        lines = self.run_cli("whoami")[1].splitlines()
        self.assertEqual(lines[:5], [
            "vcharon: whoami",
            "  box      mac (set in %s)" % os.path.join(self.homes["mac"], "vcharon.ini"),
            "  project  web", "  name     mac-web (a join from here)", "  vcharon  %s" % VERSION])
        # no box set, no channels: the OS's
        self.use_box("nobox")
        with open(os.path.join(self.homes["nobox"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\n")
        with mock.patch.object(platform, "os_word", return_value="win"):
            doc = json.loads(self.run_cli("whoami", "--json")[1])
            lines = self.run_cli("whoami")[1].splitlines()
        self.assertEqual(doc, {"box": "win", "box_source": "os", "project": "web",
                               "role": None, "name": "win-web", "channels": []})
        self.assertEqual(lines[1], "  box      win (default, from the OS)")


class SetupTest(ChannelCase):
    """vcharon setup [--box NAME]: writes the config, never prompts, names where it is."""

    def setUp(self):
        ChannelCase.setUp(self)
        self.home_ = self.use_box("fresh")
        self.ini = os.path.join(self.home_, "vcharon.ini")
        os.remove(self.ini)

    def text(self):
        with open(self.ini, "rb") as f:
            return f.read().decode("utf-8")

    def test_bare_setup_makes_the_file_once(self):
        with mock.patch.object(platform, "os_word", return_value="win"):
            out = self.ok("setup")
            self.assertEqual(out.splitlines()[:3], [
                "vcharon: setup", "  config   %s (written)" % self.ini,
                "  box      win (default, from the OS)"])
            # the box line commented out: the default stays the OS's
            self.assertEqual(self.text(), "[vcharon]\n# box = win   (the default: this OS); to "
                             "set another: vcharon setup --box NAME\n")
            out = self.ok("setup")
            self.assertEqual(out.splitlines()[1], "  config   %s" % self.ini)
            self.assertEqual(json.loads(self.run_cli("whoami", "--json")[1])["box"], "win")

    def test_box_write_update_and_invalid(self):
        out = self.ok("setup", "--box", "laptop")
        self.assertEqual(out.splitlines()[2], "  box      laptop (set in %s)" % self.ini)
        self.assertEqual(self.text(), "[vcharon]\nbox = laptop\n")
        # an existing file: one line changed, comments and other keys kept (newline="": \n on
        # every host, since setup keeps an existing file's line ends)
        with open(self.ini, "w", encoding="utf-8", newline="") as f:
            f.write("# my notes\n[vcharon]\n; old box\nbox = laptop\ncompress = yes\n")
        self.ok("setup", "--box", "desk")
        self.assertEqual(self.text(), "# my notes\n[vcharon]\n; old box\nbox = desk\n"
                         "compress = yes\n")
        doc = json.loads(self.run_cli("whoami", "--json")[1])
        self.assertEqual((doc["box"], doc["box_source"], doc["name"]),
                         ("desk", "config", "desk-web"))
        # invalid: a usage error, the file unchanged
        for box in ("x" * 11, "Mac", "con", "a.b", ""):
            with self.subTest(box=box):
                line, fix = self.refusal("setup", "--box", box, code=3)
                self.assertTrue(line.startswith("ERROR config: --box "), line)
                self.assertEqual(fix, platform.runnable("pick another name: vcharon setup "
                                                        "--box NAME"))
                self.assertEqual(self.text(), "# my notes\n[vcharon]\n; old box\nbox = desk\n"
                                 "compress = yes\n")
        # a broken file is the user's to fix: never rewritten
        with open(self.ini, "w", encoding="utf-8", newline="") as f:
            f.write("[vcharon]\ncompress = maybe\n")
        self.assertTrue(self.refused("setup", "--box", "desk", code=3).startswith(
            "ERROR config: vcharon.ini [vcharon] compress"))
        self.assertEqual(self.text(), "[vcharon]\ncompress = maybe\n")

    @unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
    def test_a_symlinked_config_stays_a_link(self):
        # a dotfiles manager's link: the file it points at is written, the link kept
        target = os.path.join(self.tmp, "dotfiles", "vcharon.ini")
        os.makedirs(os.path.dirname(target))
        with open(target, "w", encoding="utf-8") as f:
            f.write("[vcharon]\nbox = old\n")
        os.symlink(target, self.ini)
        out = self.ok("setup", "--box", "desk")
        self.assertEqual(out.splitlines()[1], "  config   %s (written)" % self.ini)
        self.assertTrue(os.path.islink(self.ini))
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[vcharon]\nbox = desk\n")
        self.assertEqual(os.listdir(os.path.dirname(target)), ["vcharon.ini"])

    def test_a_bom_and_the_mode_are_kept(self):
        with open(self.ini, "wb") as f:
            f.write(b"\xef\xbb\xbf[vcharon]\r\ncompress = yes\r\n")
        os.chmod(self.ini, 0o640)
        self.ok("setup", "--box", "desk")
        with open(self.ini, "rb") as f:
            self.assertEqual(f.read(), b"\xef\xbb\xbf[vcharon]\r\nbox = desk\r\n"
                             b"compress = yes\r\n")
        if os.name != "nt":
            self.assertEqual(os.stat(self.ini).st_mode & 0o777, 0o640)

    def test_a_read_only_config_on_windows(self):
        # Windows can't replace a read-only file: its flag is cleared first, then set again
        with open(self.ini, "w", encoding="utf-8", newline="") as f:
            f.write("[vcharon]\n")
        os.chmod(self.ini, 0o444)
        self.addCleanup(lambda: os.path.exists(self.ini) and os.chmod(self.ini, 0o644))
        made_writable = []
        real_chmod = os.chmod

        def chmod(path, mode):
            if path == os.path.realpath(self.ini):
                made_writable.append(mode)
            return real_chmod(path, mode)
        with mock.patch.object(config.fsops, "WINDOWS", True), \
                mock.patch.object(config.os, "chmod", chmod):
            self.ok("setup", "--box", "desk")
        self.assertEqual(made_writable, [0o444 | 0o200])
        self.assertEqual(self.text(), "[vcharon]\nbox = desk\n")
        if os.name != "nt":
            self.assertEqual(os.stat(self.ini).st_mode & 0o777, 0o444)


@unittest.skipUnless(CAN_SYMLINK, "no symlinks here")
class PostLinkTest(ChannelCase):
    """post never writes through a link at the own folder itself, nor at a remote member's
    tree above it."""

    def post(self):
        return self.refusal("post", "game", "--to", "@laptop-ui", "--title", "t", "--body", "b")

    def swap(self, path):
        """path moved aside, and a symlink to it put in its place; the moved folder's
        contents."""
        moved = os.path.join(self.tmp, "elsewhere")
        shutil.move(path, moved)
        os.symlink(moved, path)
        return moved, read_tree(moved)

    def test_a_local_members_own_folder(self):
        self.lead(where=("--local",))
        self.ok("join", "game", "--local")
        own = os.path.join(self.root, "game", "mac-web")
        moved, before = self.swap(own)
        self.assertEqual(self.post(), (
            "ERROR unsafe_path: %s is a symlink: a post never writes through one" % own,
            "remove the link by hand (vcharon never makes one there), then post again"))
        self.assertEqual(read_tree(moved), before)

    def test_a_remote_members_own_folder_and_tree(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        tree = self.joined("game.mac-web")
        for path in (os.path.join(tree, "mac-web"), tree):
            with self.subTest(path=path):
                moved, before = self.swap(path)
                self.assertEqual(self.post()[0], "ERROR unsafe_path: %s is a symlink: a post "
                                 "never writes through one" % path)
                self.assertEqual(read_tree(moved), before)
                os.remove(path)
                shutil.move(moved, path)
        self.assertEqual(self.run_cli("post", "game", "--to", "@laptop-ui", "--title", "t",
                                      "--body", "b")[0], 0)


class ListJsonTest(ChannelCase):
    def test_list_json(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        # a member whose MEMBER.md has none of the fields, and one with a value of another
        # shape: blanks
        write_tree(self.root, {"game/Stray/": None, "notes": b"n",
                               "game/old/MEMBER.md": b"# MEMBER\n\n## t \xe2\x80\x94 old#1 "
                               b"\xe2\x80\x94 member\nto: @laptop-ui\nchannel: game\n"
                               b"box: Bad Box\n"})
        code, out, err = self.run_cli("list", "--server", "fake-dest", "--json")
        self.assertEqual((code, err), (0, ""))
        [line] = out.splitlines()
        doc = json.loads(line)
        self.assertEqual(sorted(doc), ["channels", "others", "server"])
        self.assertEqual(doc["server"], "fake-dest")
        [ch] = doc["channels"]
        self.assertEqual(sorted(ch), ["format", "leader", "leaders", "limits", "member_info",
                                      "members", "name", "newest", "strays"])
        self.assertEqual((ch["format"], ch["limits"]),
                         (1, {"max_mb": 50, "max_files": 1000, "max_entry_kb": 1000}))
        self.assertEqual((ch["name"], ch["leader"], ch["leaders"], ch["members"], ch["strays"]),
                         ("game", "laptop-ui", ["laptop-ui"], ["laptop-ui", "mac-web", "old"],
                          ["Stray"]))
        osw = platform.os_word()
        self.assertEqual(ch["member_info"], [
            {"name": "laptop-ui", "box": "laptop", "os": osw, "agent": "other",
             "project": "ui"},
            {"name": "mac-web", "box": "mac", "os": osw, "agent": "other", "project": "web"},
            {"name": "old", "box": None, "os": None, "agent": None, "project": None}])
        lines = self.run_cli("list", "--server", "fake-dest")[1].splitlines()
        self.assertEqual(lines[2:5], [
            "    laptop-ui  box laptop  os %s  agent other  project ui" % osw,
            "    mac-web  box mac  os %s  agent other  project web" % osw,
            "    old  box -  os -  agent -  project -"])
        self.assertRegex(ch["newest"], r"\A\d{4}-\d\d-\d\d \d\d:\d\d\Z")
        self.assertEqual([sorted(o) for o in doc["others"]], [["name", "why"]])
        self.assertEqual(doc["others"][0]["name"], "notes")
        # --local: no server
        doc = json.loads(self.run_cli("list", "--local", "--json")[1])
        self.assertEqual(doc["server"], None)
        # one of --server and --local
        self.assertEqual(self.run_cli("list", "--json")[0], 3)


class SyncTest(ChannelCase):
    def test_a_local_member_has_nothing_to_sync(self):
        self.lead("docs", where=("--local",), box="linux", project="d")
        self.use_box("linux")
        before = read_tree(self.homes["linux"])
        self.assertEqual(self.refusal("sync", "docs", "--project", "d"), (
            "ERROR channel: you are a local member of docs: your folder is in the channel "
            "itself, so there is nothing to sync",
            platform.runnable("vcharon read docs --project d")))
        self.assertEqual(set(read_tree(self.homes["linux"])),
                         set(before) | {"logs/", "logs/vcharon.log"})

    def test_reset_up_and_down(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        state = os.path.join(self.homes["mac"], "state")
        for job, other in (("up", "down"), ("down", None)):
            with self.subTest(job=job):
                path = os.path.join(state, "game.mac-web.%s.json" % job)
                self.assertTrue(os.path.exists(path))
                lines = self.ok("sync", "game", "--reset", job).splitlines()
                self.assertEqual(lines[0], "vcharon: state of game.mac-web.%s  (%s)"
                                 % (job, path))
                self.assertEqual(lines[-1], "removed %s" % path)
                self.assertFalse(os.path.exists(path))
                # the reset is one job's: the other's state stays
                if other:
                    self.assertTrue(os.path.exists(
                        os.path.join(state, "game.mac-web.%s.json" % other)))
        # the next full sync goes by content: nothing new reaches the server, and down
        # changes nothing here
        before = read_tree(os.path.join(self.root, "game"))
        here = read_tree(self.joined("game.mac-web"))
        out = self.ok("sync", "game", "--full")
        self.assertIn("already there", out)
        self.assertEqual(read_tree(os.path.join(self.root, "game")), before)
        self.assertEqual(read_tree(self.joined("game.mac-web")), here)

    def test_a_lost_own_folder_comes_back_by_the_rejoin(self):
        # the fix as printed: a rejoin pulls the own folder back from the server
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        own = os.path.join(self.joined("game.mac-web"), "mac-web")
        self.assertEqual(self.run_cli("post", "game", "--to", "@laptop-ui", "--title", "t",
                                      "--body", "b")[0], 0)
        self.assertEqual(self.run_cli("sync", "game")[0], 0)
        before = read_tree(own)
        shutil.rmtree(own)
        code, _out, err = self.run_cli("sync", "game")
        self.assertEqual(code, 1)
        fixes = {l[len("  fix: "):] for l in err.splitlines() if l.startswith("  fix: ")}
        rejoin = platform.runnable("vcharon join game --server fake-dest --project web takes "
                                   "its files back from the server (a rejoin)")
        self.assertEqual(fixes, {rejoin})
        # and --reset up is refused meanwhile, with the same fix
        self.assertEqual(self.refusal("sync", "game", "--reset", "up")[1], rejoin)
        command = rejoin.split(" takes ")[0]
        argv = command[len(platform.self_command()):].split()
        self.assertIn("  took back game/mac-web", self.ok(*argv))
        # every file back as it was; the rejoin adds its REJOIN entry to RESULTS.md
        after = read_tree(own)
        self.assertEqual(sorted(after), sorted(before))
        for path, data in before.items():
            self.assertTrue(after[path].startswith(data), path)
        self.assertEqual(self.run_cli("sync", "game")[0], 0)

    def test_busy_is_2(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        from vcharon import state
        held = state.lock("game.mac-web.up")
        self.addCleanup(held.release)
        self.assertEqual(self.refused("sync", "game", code=2),
                         "ERROR busy: another run of game.mac-web.up is in progress")
        self.assertEqual(self.refused("sync", "game", "--reset", "up", code=2),
                         "ERROR busy: another run of game.mac-web.up is in progress")


# Where a command ends inside a text: what follows it in the hints
# where a command ends inside a text: always a space first, so that a mark stuck to the last
# value (a "--role b," copied along) shows in parse()
_STOP = re.compile(r" ; | again\b| once | takes | makes it| works\b| \(|\Z")
_COMMAND = re.compile(r"(?:\A|(?<=[ (`:]))vcharon (?=\S)")


def commands(text):
    """Each `vcharon …` command in text, as argv after `vcharon`."""
    out = []
    for m in _COMMAND.finditer(text):
        rest = text[m.end():]
        out.append(shlex.split(rest[:_STOP.search(rest).start()]))
    return out


class FixRoundTripTest(ChannelCase):
    """Every refusal whose fix line names a command, triggered here: the command, as printed,
    parses with the command line's own parser (DESIGN, "Fix lines"). The spelling is `vcharon` here
    (self_command faked); test_platform checks each install's spelling."""

    def setUp(self):
        ChannelCase.setUp(self)
        patcher = mock.patch.object(platform, "self_command", lambda *a, **kw: "vcharon")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seen = []

    def lines(self, *argv, code=None):
        """The fix lines and the lines that say what to run, of vcharon ARGV."""
        got, out, err = self.run_cli(*argv)
        if code is not None:
            self.assertEqual(got, code, out + err)
        found = [l.split("fix: ", 1)[1] for l in (out + err).splitlines() if "fix: " in l]
        found += [l for l in out.splitlines() if l.startswith("vcharon: the sync failed")]
        self.seen.extend(found)
        return found

    def assert_parses(self, texts):
        n = 0
        for text in texts:
            for argv in commands(text):
                with self.subTest(text=text, argv=argv):
                    parse(self, argv)
                    n += 1
        return n

    def test_every_command_in_a_fix_line_parses(self):
        # a usage error points at the verb's help
        self.lines("post", "game", code=3)
        # no membership; then a channel that isn't there
        self.lines("sync", "game", code=1)
        self.lines("join", "game", "--server", "fake-dest", code=1)
        self.lead()
        self.ok("join", "game", "--server", "fake-dest", "--role", "b")
        # another machine holds the folder: with this box's record, and from a box without
        other = ("f" * 32, "machine id")
        with mock.patch.object(platform, "client_id", return_value=other):
            self.lines("join", "game", "--server", "fake-dest", "--role", "b", code=1)
            self.use_box("mac2")
            with open(os.path.join(self.homes["mac2"], "vcharon.ini"), "w") as f:
                f.write("[vcharon]\nbox = mac\n")
            self.lines("join", "game", "--server", "fake-dest", "--role", "b", code=1)
        self.lines("setup", "--box", "Bad", code=3)
        self.use_box("mac")
        # the leader leaves, a member closes
        self.use_box("laptop")
        self.lines("leave", "game", "--project", "ui", code=1)
        self.use_box("mac")
        self.lines("close", "game", "--role", "b", code=1)
        # a sync that fails after a join, a create, a leave
        with mock.patch.object(channel_cmd, "_run_section", lambda *a, **kw: 4):
            self.lines("join", "game", "--server", "fake-dest", "--role", "c", code=4)
            self.lines("create", "docs", "--server", "fake-dest", "--role", "c", code=4)
            self.lines("leave", "game", "--role", "c", code=4)
        self.assertEqual(self.run_cli("sync", "game", "--role", "b")[0], 0)
        own = os.path.join(self.joined("game.mac-web-b"), "mac-web-b")
        # MEMBER.md gone after up sent it: a rejoin takes it back
        os.rename(os.path.join(own, "MEMBER.md"), os.path.join(self.tmp, "MEMBER.md"))
        self.lines("sync", "game", "--role", "b", code=1)
        os.rename(os.path.join(self.tmp, "MEMBER.md"), os.path.join(own, "MEMBER.md"))
        # the own folder gone, after up sent files from it
        shutil.move(own, os.path.join(self.tmp, "own"))
        self.lines("sync", "game", "--role", "b", code=1)
        self.lines("doctor", code=1)
        shutil.move(os.path.join(self.tmp, "own"), own)
        # a state saved for another config: reset and sync --full
        section = os.path.join(self.homes["mac"], "channels.d", "game.mac-web-b.ini")
        with open(section, encoding="utf-8") as f:
            text = f.read()
        with open(section, "w", encoding="utf-8") as f:
            f.write(text.replace("ssh            = fake-dest", "ssh            = fake-dest2"))
        self.lines("sync", "game", "--role", "b", code=3)
        with open(section, "w", encoding="utf-8") as f:
            f.write(text)
        # the channel closed under the member
        shutil.rmtree(os.path.join(self.root, "game"))
        self.lines("sync", "game", "--role", "b", code=1)
        # a local member's sync
        self.lead("docs2", where=("--local",), box="linux", project="d")
        self.use_box("linux")
        self.lines("sync", "docs2", "--project", "d", code=1)
        # keys: an example, and a locked key's doctor line
        with mock.patch.object(keys, "terminal", return_value=True):
            self.lines("key", code=3)
        os.environ.update(FAKE_SSH_EXIT="255", FAKE_SSH_STDERR="Permission denied (publickey).")
        self.lines("ping", "fake-dest", code=4)
        n = self.assert_parses(self.seen)
        # what the cases above printed: each command (twice for reset_hint's)
        self.assertGreaterEqual(n, 20, self.seen)
        verbs = {argv[0] for text in self.seen for argv in commands(text)}
        self.assertEqual(verbs, {"post", "join", "list", "close", "leave", "sync", "read", "key",
                                 "setup"}, self.seen)
        self.assertEqual(len([t for t in self.seen if "--takeover" in t]), 2, self.seen)

    def test_the_help_examples_parse(self):
        parser = cli._parser()
        for verb in platform.COMMANDS:
            if verb.startswith("--"):
                continue
            with self.subTest(verb=verb):
                code, out, err = self.run_cli(verb, "--help")
                self.assertEqual((code, err), (0, ""))
                [example] = [l for l in out.splitlines() if l.startswith("example: ")]
                self.assertTrue(example.startswith("example: vcharon %s" % verb), example)
                [argv] = commands(example[len("example: "):])
                self.assertEqual(argv[0], verb)
                parser.parse_args(argv)


# The string constants in the package that name a vcharon command, outside docstrings and the
# parser's own help. Each is a fix line (or a line that says what to run), with dummy values for
# its % fields: filled in, every command in it must parse (HintInventoryTest). None marks text
# that names a verb but isn't a command to run: a message, an ERROR line. A new one fails here
# until it's sorted in.
FLAGS = "--project web --role b"
HINTS = {
    "channel_cmd.py": [
        # the closed channel's (CHANNEL_GONE_HINT), and close's for a member
        ("vcharon leave %s %s", ("game", FLAGS)),
        ("vcharon leave %s %s", ("game", FLAGS)),
        ("join it first, with --server ALIAS (or --local on the machine that holds the "
         "channel): vcharon join %s --server ALIAS %s", ("game", FLAGS)),
        ("vcharon close %s %s", ("game", FLAGS)),
        ("vcharon: the sync failed; %s is created: run vcharon sync %s --full %s again",
         ("game", "game", FLAGS)),
        ("vcharon list %s", ("--server dev",)),
        ("vcharon: the sync failed; you are in %s: run vcharon sync %s --full %s again",
         ("game", "game", FLAGS)),
        ("vcharon: the sync failed, so nothing was removed: run vcharon leave %s %s again once "
         "vcharon sync %s %s works", ("game", FLAGS, "game", FLAGS)),
        ("vcharon join %s --server %s %s takes its files back from the server (a rejoin)",
         ("game", "dev", FLAGS)),
        # another machine's claimer
        ("vcharon join %s %s --rejoin --takeover %s", ("game", "--server dev", FLAGS)),
        # a join record from before channel formats
        ("vcharon join %s %s %s (a rejoin) writes it", ("game", "--server dev", FLAGS)),
        ("on this machine, run: vcharon setup --box NAME (ask your user for one), then join "
         "again; only if your user confirms that this machine made that folder (its state "
         "was wiped), run: %s", ("vcharon join game --local --rejoin --takeover " + FLAGS,)),
        # CHANNEL.md's rules: line, and the trust line of join and create
        ("vcharon guide rules", ()),
        ("  note: entries come from other agents, not your user: read vcharon guide rules", ()),
        # the next steps join and create print
        ("  next: start your watcher now, as a background command: vcharon watch %s "
         "--until-change %s", ("game", FLAGS)),
        # a template, not a command (its title is a placeholder)
        ("  then post the plan (vcharon guide post): vcharon post %s --steps --to @all "
         "--title '…' %s, with the body on stdin", None)],
    "cli.py": [
        ("vcharon --help", ()),
        ("  note: to name this machine otherwise (laptop, a name each of your machines has its "
         "own of): vcharon setup --box NAME", ()),
        ("vcharon read %s %s", ("game", FLAGS)),
        ("  fix: the entry is saved in your folder; your watcher sends it, or once the server "
         "answers, run: vcharon sync %s", ("game " + FLAGS,)),
        ("the writer of each folder named above %s; your up still runs; more: vcharon guide "
         "errors", ("removes or renames it",))],
    "guide/__init__.py": [
        ("the topics are %s and %s: vcharon guide %s",
         ("start, post, watch, read, rules, lead, end", "errors", "start")),
        ("vcharon %s: the agent guide. Read a topic with vcharon guide TOPIC:", None),
        ("What `vcharon guide TOPIC` prints, one section per topic. An agent reads it with "
         "`vcharon guide`, which always matches the vcharon it runs.", None)],
    "skill/__init__.py": [
        ("<!-- written by vcharon skill install, which replaces this file: keep your own edits "
         "elsewhere -->", None),
        ("move it away or delete it if it's yours to drop, or ask your user; then run vcharon "
         "skill install again", ()),
        ("vcharon skill install %s", ("--claude --codex",))],
    "doctor.py": [
        ("no channels joined over ssh; to check a server: vcharon doctor --server ALIAS", ()),
        ("a key with a passphrase works only once it's in the agent: run vcharon key %s",
         ("dev",)),
        ("vcharon key %s", ("dev",)),
        ("%s yet; vcharon sync %s %s makes it", ("from.path x doesn't exist", "game", FLAGS))],
    "keys.py": [
        ("for example: vcharon key devbox", ()),
        ("vcharon key needs a terminal: ssh-add asks for your passphrase there", None),
        ("start one in this shell: %s, then run vcharon key again", ("eval $(ssh-agent)",)),
        ("check the passphrase, then run vcharon key again", ()),
        ("open a new Terminal window, then run vcharon key again", ())],
    "ssh.py": [("add your key to the server, or run: vcharon key %s", ("dev",))],
    "state.py": [("check the target, then run both: vcharon sync %s --reset %s %s ; vcharon "
                  "sync %s --full %s", ("game", "up", FLAGS, "game", FLAGS))],
    "config.py": [("%s [%s]: %s holds only [vcharon]; a channel's section goes in %s, which "
                   "vcharon join writes", None),
                  ("# box = %s   (the default: this OS); to set another: vcharon setup --box "
                   "NAME", ("linux",)),
                  ("pick another name: vcharon setup --box NAME", ())],
    "mailbox/watch.py": [
        ("ERROR vcharon sync of ", None), ("ERROR vcharon sync of %s exited with %d", None),
        ("ERROR vcharon sync of %s didn't finish within %d s", None),
        ("ERROR vcharon sync of %s didn't finish within %d s", None),
        ("%s isn't there: your folder in the channel holds it, once vcharon join --local has "
         "written it", None)],
}


def parse(case, argv):
    """argv (after `vcharon`) parses with the command line's own parser. --update isn't one of
    runnable()'s commands (DESIGN, "Fix lines"), but it parses."""
    case.assertIn(argv[0], platform.COMMANDS + ("--update",))
    # a command ends before any mark: none sticks to its last value
    case.assertNotRegex(argv[-1], r"[,;:.)]\Z")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cli._parser().parse_args(argv)
    except VCharonError as e:
        case.fail("%r doesn't parse: %s" % (argv, e.message))
    except SystemExit:
        # a verb's --help
        case.assertEqual(argv[-1], "--help")


class HintInventoryTest(unittest.TestCase):
    def found(self):
        out = {}
        for path in sorted(glob.glob(os.path.join(PACKAGE_DIR, "**", "*.py"), recursive=True)):
            rel = os.path.relpath(path, PACKAGE_DIR).replace(os.sep, "/")
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            skip = set()
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)):
                    skip.add(id(body[0].value))
                if isinstance(node, ast.FunctionDef) and node.name == "_parser":
                    skip.update(id(n) for n in ast.walk(node))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in skip and platform._VCHARON_WORD.search(node.value)):
                    out.setdefault(rel, []).append(node.value)
        return {k: sorted(v) for k, v in out.items()}

    def test_every_command_hint_is_known(self):
        self.assertEqual(self.found(),
                         {k: sorted(text for text, _ in v) for k, v in HINTS.items()})

    def test_every_command_hint_parses(self):
        for module, hints in HINTS.items():
            for text, values in hints:
                if values is None:
                    continue
                filled = text % values
                with self.subTest(module=module, text=filled):
                    found = commands(filled)
                    self.assertTrue(found, filled)
                    for argv in found:
                        parse(self, argv)


if __name__ == "__main__":
    unittest.main()
