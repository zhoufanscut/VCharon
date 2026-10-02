"""The membership a command means (DESIGN §7.2), whoami, list --json, sync, and fix lines that
parse back into the command line (DESIGN §7.16)."""

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

from vcharon import channel_cmd, cli, keys, platform
from vcharon.proto import VCharonError

from tests.test_channel import ChannelCase
from tests.util import PACKAGE_DIR, read_tree, write_tree


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
        with open(os.path.join(self.homes["mac"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = macbook\n")
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
        code, out, err = self.run_cli("whoami", "--json")
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

    def test_a_record_from_before_the_project_field(self):
        # matched by the name this box builds
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        path = channel_cmd.record_path("game", "mac-web")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        del doc["project"], doc["role"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.assertEqual(self.name_of(), "mac-web")
        self.assertEqual(self.refusal("sync", "game", "--role", "b")[0],
                         "ERROR channel: you aren't in game as --project web --role b (no join "
                         "record on this box)")

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
            "folder": os.path.join(tree, "mac-web-b"), "tree": tree})
        code, out, err = self.run_cli("whoami", "game", "--role", "b")
        self.assertEqual(out.splitlines(), [
            "vcharon: whoami game",
            "  channel  game, led by laptop-ui",
            "  name     mac-web-b  (--project web --role b)",
            "  mode     remote, server fake-dest",
            "  folder   %s" % os.path.join(tree, "mac-web-b")])
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
        self.assertEqual(sorted(doc), ["box", "channels", "name", "project", "role"])
        self.assertEqual((doc["box"], doc["project"], doc["role"], doc["name"]),
                         ("mac", "web", None, "mac-web"))
        self.assertEqual([(c["channel"], c["name"]) for c in doc["channels"]],
                         [("docs", "mac-web-r"), ("game", "mac-web")])
        # --role narrows it to that role's
        doc = json.loads(self.run_cli("whoami", "--role", "r", "--json")[1])
        self.assertEqual((doc["name"], [c["name"] for c in doc["channels"]]),
                         ("mac-web-r", ["mac-web-r"]))
        lines = self.run_cli("whoami")[1].splitlines()
        self.assertEqual(lines[:4], ["vcharon: whoami", "  box      mac", "  project  web",
                                     "  name     mac-web (a join from here)"])
        # no box, no channels
        self.use_box("nobox")
        with open(os.path.join(self.homes["nobox"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\n")
        doc = json.loads(self.run_cli("whoami", "--json")[1])
        self.assertEqual(doc, {"box": None, "project": "web", "role": None, "name": None,
                               "channels": []})


class ListJsonTest(ChannelCase):
    def test_list_json(self):
        self.lead()
        self.ok("join", "game", "--server", "fake-dest")
        write_tree(self.root, {"game/Stray/": None, "notes": b"n"})
        code, out, err = self.run_cli("list", "--server", "fake-dest", "--json")
        self.assertEqual((code, err), (0, ""))
        [line] = out.splitlines()
        doc = json.loads(line)
        self.assertEqual(sorted(doc), ["channels", "others", "server"])
        self.assertEqual(doc["server"], "fake-dest")
        [ch] = doc["channels"]
        self.assertEqual(sorted(ch), ["leader", "leaders", "members", "name", "newest",
                                      "strays"])
        self.assertEqual((ch["name"], ch["leader"], ch["leaders"], ch["members"], ch["strays"]),
                         ("game", "laptop-ui", ["laptop-ui"], ["laptop-ui", "mac-web"],
                          ["Stray"]))
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
        for job in ("up", "down"):
            with self.subTest(job=job):
                path = os.path.join(state, "game.mac-web.%s.json" % job)
                self.assertTrue(os.path.exists(path))
                lines = self.ok("sync", "game", "--reset", job).splitlines()
                self.assertEqual(lines[-1], "removed %s" % path)
                self.assertFalse(os.path.exists(path))
        # the next full sync sends by content: nothing new reaches the server
        before = read_tree(os.path.join(self.root, "game"))
        out = self.ok("sync", "game", "--full")
        self.assertIn("already there", out)
        self.assertEqual(read_tree(os.path.join(self.root, "game")), before)

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
        code, out, err = self.run_cli("sync", "game")
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
    parses with the command line's own parser (DESIGN §7.16). The spelling is `vcharon` here
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
        self.assertGreaterEqual(n, 16, self.seen)
        verbs = {argv[0] for text in self.seen for argv in commands(text)}
        self.assertEqual(verbs, {"post", "join", "list", "close", "leave", "sync", "read", "key"},
                         self.seen)

    def test_the_help_examples_parse(self):
        parser = cli._parser()
        for verb in platform.COMMANDS:
            if verb.startswith("--"):
                continue
            with self.subTest(verb=verb):
                code, out, err = self.run_cli(verb, "--help")
                [example] = [l for l in out.splitlines() if l.startswith("example: ")]
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
         ("game", "dev", FLAGS))],
    "cli.py": [
        ("vcharon --help", ()),
        ("vcharon read %s %s", ("game", FLAGS))],
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
        ("check the passphrase, then run vcharon key again", ())],
    "ssh.py": [("add your key to the server, or run: vcharon key %s", ("dev",))],
    "state.py": [("check the target, then run both: vcharon sync %s --reset %s %s ; vcharon "
                  "sync %s --full %s", ("game", "up", FLAGS, "game", FLAGS))],
    "config.py": [("%s [%s]: %s holds only [vcharon]; a channel's section goes in %s, which "
                   "vcharon join writes", None)],
    "mailbox/watch.py": [
        ("ERROR vcharon sync of ", None), ("ERROR vcharon sync of %s exited with %d", None),
        ("ERROR vcharon sync of %s didn't finish within %d s", None),
        ("ERROR vcharon sync of %s didn't finish within %d s", None),
        ("%s isn't there: your folder in the channel holds it, once vcharon join --local has "
         "written it", None)],
}


def parse(case, argv):
    """argv (after `vcharon`) parses with the command line's own parser."""
    case.assertIn(argv[0], platform.COMMANDS)
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
