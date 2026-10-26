"""The shared root (DESIGN, "The shared root"): --shared on create and join, the join record
that keeps it, the modes of what vcharon makes there, the checks of the folders it finds,
list's group of its own, and the refusal on macOS and Windows."""

from __future__ import annotations

import json
import os
import stat
import time
import unittest
from unittest import mock

from vcharon import channel_cmd, channels, fsops, helper, platform
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

from tests.test_channel import OTHER_CLIENT, ChannelCase, hold
from tests.util import read_tree

DAY = 86400


class SharedCase(ChannelCase):
    """A ChannelCase whose shared root is the test's own folder."""

    def setUp(self):
        ChannelCase.setUp(self)
        self.top = os.path.realpath(os.path.join(self.tmp, "shared"))
        os.environ["VCHARON_SHARED_ROOT"] = self.top
        self.shared = os.path.join(self.top, "channels")

    def modes(self):
        """{path below the shared folder ("" for itself): its mode}, every file and folder."""
        out = {"": stat.S_IMODE(os.lstat(self.top).st_mode)}
        for path in read_tree(self.top):
            path = path.rstrip("/")
            out[path] = stat.S_IMODE(os.lstat(os.path.join(self.top, path)).st_mode)
        return out


@unittest.skipUnless(fsops.LINUX, "the shared root is Linux only")
class SharedRootTest(SharedCase):
    def setUp(self):
        SharedCase.setUp(self)
        # what an account that keeps its files to itself has: nothing for group or others
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)

    def test_modes_of_everything_made(self):
        # laptop-ui leads over the fake server, mac-web joins over it, linux-e is a local
        # member; then a post, a handoff to linux-e and its watcher's stamp
        self.lead(where=("--server", "fake-dest", "--shared"))
        self.ok("join", "game", "--server", "fake-dest", "--shared")
        self.use_box("linux")
        self.ok("join", "game", "--local", "--shared", "--project", "e")
        self.use_box("mac")
        # a file the up replaces gets the shared mode too, whatever the one it replaces had
        os.chmod(os.path.join(self.shared, "game", "mac-web", "RESULTS.md"), 0o600)
        self.ok("post", "game", "--to", "@laptop-ui", "--title", "step 1", "--body", "done")
        self.use_box("laptop")
        self.ok("lead", "game", "--to", "@linux-e", "--project", "ui")
        self.use_box("linux")
        self.assertIn("OK  you lead game (term 2)", self.ok("lead", "game", "--project", "e"))
        self.ok("watch", "game", "--once", "--project", "e")
        # a remote member's watcher's sync (its environment carries the pace): the helper
        # stamps it beside the shared root
        self.use_box("mac")
        with mock.patch.dict(os.environ, {watch.WATCH_ENV: "stream 2"}):
            self.ok("sync", "game")
        modes = self.modes()
        # every kind of thing was made: folders, entry files, a stamp, a term marker
        for path in ("channels/game/mac-web/RESULTS.md", "channels/game/laptop-ui/CHANNEL.md",
                     "channels/game/linux-e/CHANNEL.md", "seen/game/linux-e",
                     "seen/game/mac-web", "lead/game/2/linux-e"):
            self.assertIn(path, modes)
        wrong = {p: oct(m) for p, m in modes.items()
                 if m != (0o777 if stat.S_ISDIR(os.lstat(os.path.join(self.top, p)).st_mode)
                          else 0o666)}
        self.assertEqual(wrong, {})
        # the home's root is untouched
        self.assertFalse(os.path.exists(self.root))

    def test_record_whoami_and_list(self):
        self.lead(where=("--server", "fake-dest", "--shared"))
        out = self.ok("join", "game", "--server", "fake-dest", "--shared")
        self.assertIn("vcharon: join game  as mac-web on fake-dest's shared root", out)
        record = self.record("game.mac-web")
        self.assertIs(record["shared"], True)
        self.assertEqual(record["remote"], os.path.join(self.shared, "game"))
        with open(os.path.join(self.vcharon_home, "channels.d", "game.mac-web.ini"),
                  encoding="utf-8") as f:
            self.assertIn("mailbox.remote = %s\n" % os.path.join(self.shared, "game"), f.read())
        doc = json.loads(self.ok("whoami", "game", "--json"))
        self.assertIs(doc["shared"], True)
        self.assertIn("  mode     remote, server fake-dest, its shared root",
                      self.ok("whoami", "game").splitlines())
        # list: the root in the home first, then the shared root's group
        lines = self.ok("list", "--server", "fake-dest").splitlines()
        self.assertEqual(lines[:3], ["vcharon: list  (fake-dest)", "  no channels",
                                     "vcharon: list  (fake-dest's shared root)"])
        self.assertTrue(lines[3].startswith("  game  leader laptop-ui  members laptop-ui, "
                                            "mac-web  "), lines)
        doc = json.loads(self.ok("list", "--server", "fake-dest", "--json"))
        self.assertEqual(doc["channels"], [])
        self.assertEqual([c["name"] for c in doc["shared"]["channels"]], ["game"])
        # a rejoin takes the root from the record: no --shared
        out = self.ok("join", "game", "--server", "fake-dest")
        self.assertIn("  took back game/mac-web; the leader is laptop-ui", out)
        # the leader's close, from its record too
        self.use_box("laptop")
        self.ok("close", "game", "--project", "ui")
        self.assertEqual(os.listdir(self.shared), [])

    def test_a_name_taken_and_the_other_root(self):
        self.lead(where=("--server", "fake-dest", "--shared"))
        self.ok("join", "game", "--server", "fake-dest", "--shared")
        # the same name from another account's box: the claim refuses it
        self.use_box("mac2", os.path.join(self.tmp, "second-mac"))
        with open(os.path.join(self.homes["mac2"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = mac\n")
        self.assertEqual(self.refused("join", "game", "--server", "fake-dest", "--shared"),
                         "ERROR channel: the name mac-web is taken in game")
        # a membership never moves between the roots: a home channel of the same name
        self.use_box("mac")
        self.lead("home", box="win")
        self.ok("join", "home", "--server", "fake-dest")
        self.lead("home", where=("--server", "fake-dest", "--shared"), box="linux")
        self.assertEqual(self.refusal("join", "home", "--server", "fake-dest", "--shared"),
                         ("ERROR channel: you are in home in that server's channel root in its "
                          "home", "pass --role R to join from here as another member"))

    def test_folders_found_with_other_modes_are_refused(self):
        os.makedirs(self.shared)
        os.chmod(self.top, 0o755)
        os.chmod(self.shared, 0o777)
        self.assertEqual(self.refusal("create", "game", "--local", "--shared"), (
            "ERROR unsafe_dir: %s, in the shared root, has mode 0755, not 0777" % self.top,
            channels.SHARED_MODE_FIX % self.top))
        # never repaired
        self.assertEqual(stat.S_IMODE(os.lstat(self.top).st_mode), 0o755)
        self.assertEqual(os.listdir(self.shared), [])
        # list doesn't stop at it: the home's channels as usual, and a note naming it
        self.ok("create", "home", "--server", "fake-dest")
        problem = "%s, in the shared root, has mode 0755, not 0777" % self.top
        for where, who in ((("--server", "fake-dest"), "fake-dest's"),
                           (("--local",), "this machine's")):
            note = "  note: %s shared root isn't listed: %s; fix: %s" % (
                who, problem, channels.SHARED_MODE_FIX % self.top)
            lines = self.ok("list", *where).splitlines()
            self.assertTrue(lines[1].startswith("  home  leader mac-web  "), lines)
            self.assertEqual(lines[-1], note)
            doc = json.loads(self.ok("list", *where, "--json"))
            self.assertEqual([c["name"] for c in doc["channels"]], ["home"])
            self.assertEqual(doc["shared"], {"error": {
                "code": "unsafe_dir", "message": problem,
                "fix": channels.SHARED_MODE_FIX % self.top}})
            lines = self.ok("list", "--remove-idle", *where).splitlines()
            self.assertEqual(lines[1:], ["  none", note])
            doc = json.loads(self.ok("list", "--remove-idle", *where, "--json"))
            self.assertEqual(doc["shared"]["error"]["message"], problem)
        os.chmod(self.top, 0o777)
        os.mkdir(os.path.join(self.top, "seen"), 0o700)
        os.chmod(os.path.join(self.top, "seen"), 0o700)
        self.assertEqual(self.refused("create", "game", "--server", "fake-dest", "--shared"),
                         "ERROR unsafe_dir: %s, in the shared root, has mode 0700, not 0777"
                         % os.path.join(self.top, "seen"))

    def test_a_link_for_the_shared_folder_is_refused(self):
        target = os.path.join(self.tmp, "elsewhere")
        os.mkdir(target)
        os.chmod(target, 0o777)
        os.symlink(target, self.top)
        self.assertEqual(self.refusal("create", "game", "--local", "--shared"), (
            "ERROR unsafe_dir: %s, in the shared root, isn't a folder (a link, or a file)"
            % self.top, channels.SHARED_KIND_FIX))
        self.assertEqual(os.listdir(target), [])

    def test_remove_idle_has_a_group_of_its_own(self):
        self.lead(where=("--local", "--shared"))
        self.ok("join", "game", "--local", "--shared")
        self.use_box("linux")
        self.ok("join", "game", "--local", "--shared")
        self.use_box("mac")
        os.environ["VCHARON_TEST_CLOCK_SHIFT"] = str(20 * DAY)
        channels.stamp_seen(self.shared, "game", "laptop-ui", "local 10",
                            now=time.time() + 20 * DAY)
        code, out, err = self.channel("list", "--remove-idle", "--local", "--yes")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(lines[:4], [
            "vcharon: list --remove-idle  (this machine)  members idle over 14 days",
            "  none",
            "vcharon: list --remove-idle  (this machine's shared root)  members idle over 14 "
            "days",
            "  game"])
        self.assertRegex(lines[4], r"\A    removed  linux-web  idle 20 d: \d+ files and "
                                   r"folders deleted\Z")
        self.assertEqual(sorted(os.listdir(os.path.join(self.shared, "game"))),
                         ["laptop-ui", "mac-web"])

    def test_remove_idle_names_a_shared_root_with_nothing_idle(self):
        # a shared root that holds anything gets its group and its "none", as list shows it:
        # without it, a reader can't tell that root was checked
        self.ok("create", "home", "--local")
        lines = self.ok("list", "--remove-idle", "--local").splitlines()
        self.assertEqual(lines, [
            "vcharon: list --remove-idle  (this machine)  members idle over 14 days",
            "  none"])
        # a leftover name alone counts too, as in list
        leftover = os.path.join(self.shared, ".vcharon-closed-x")
        os.makedirs(leftover)
        for folder in (self.top, self.shared):
            os.chmod(folder, 0o777)
        lines = self.ok("list", "--remove-idle", "--local").splitlines()
        self.assertEqual(lines[2:], [
            "vcharon: list --remove-idle  (this machine's shared root)  members idle over 14 "
            "days",
            "  none"])
        os.rmdir(leftover)
        self.lead(where=("--local", "--shared"))
        lines = self.ok("list", "--remove-idle", "--local").splitlines()
        self.assertEqual(lines, [
            "vcharon: list --remove-idle  (this machine)  members idle over 14 days",
            "  none",
            "vcharon: list --remove-idle  (this machine's shared root)  members idle over 14 "
            "days",
            "  none"])


@unittest.skipUnless(fsops.LINUX, "the shared root is Linux only")
class SharedMoreTest(SharedCase):
    """The rest of the shared root's rules: each test names the one it covers."""

    def idle_setup(self):
        """Every member but the leaders idle 20 days by the server's clock."""
        os.environ["VCHARON_TEST_CLOCK_SHIFT"] = str(20 * DAY)

    def fresh(self, root, channel, name):
        channels.stamp_seen(root, channel, name, "local 10", now=time.time() + 20 * DAY)

    def test_handle_modes_and_the_stage_lock(self):
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        os.makedirs(self.shared)
        os.chmod(self.top, 0o777)
        os.chmod(self.shared, 0o777)
        root = fsops.FdDir.open_root(self.shared)
        self.addCleanup(root.close)
        self.assertTrue(root.shared)
        root.mkdir("d", 0o700)
        with root.enter("d") as d:
            self.assertTrue(d.shared)
            os.close(d.create_file("f"))
            d.open_lock(create=True, exclusive=True).release()
        got = {p: oct(stat.S_IMODE(os.lstat(os.path.join(self.shared, p)).st_mode))
               for p in ("d", "d/f", "d/lock")}
        self.assertEqual(got, {"d": "0o777", "d/f": "0o666", "d/lock": "0o666"})

    def test_a_link_above_the_shared_folder_is_refused(self):
        real = os.path.realpath(os.path.join(self.tmp, "real"))
        os.mkdir(real)
        os.symlink(real, os.path.join(self.tmp, "via"))
        top = os.path.join(self.tmp, "via", "shared")
        os.environ["VCHARON_SHARED_ROOT"] = top
        self.assertEqual(self.refusal("create", "game", "--local", "--shared"), (
            "ERROR unsafe_dir: %s, the shared root's folder, is reached through a link (%s is "
            "%s)" % (top, os.path.dirname(top), real), channels.SHARED_LINK_FIX))
        self.assertEqual(os.listdir(real), [])

    def test_every_groups_removals_run_after_a_failure(self):
        self.lead("home", where=("--local",))
        self.use_box("linux")
        self.ok("join", "home", "--local")
        self.use_box("laptop")
        self.ok("create", "game", "--local", "--shared", "--project", "ui")
        self.use_box("linux")
        self.ok("join", "game", "--local", "--shared")
        self.use_box("mac")
        self.idle_setup()
        self.fresh(self.root, "home", "laptop-ui")
        self.fresh(self.shared, "game", "laptop-ui")
        real = channels.prune

        def prune(root, *args, **kw):
            if not fsops.in_shared(root):
                raise VCharonError("io", "the home's removal failed")
            return real(root, *args, **kw)

        with mock.patch.object(channels, "prune", prune):
            code, out, err = self.channel("list", "--remove-idle", "--local", "--yes", "--json")
        self.assertEqual(code, 1, out + err)
        doc = json.loads(out)
        self.assertEqual([(m["name"], m["action"]) for m in doc["members"]],
                         [("linux-web", "failed")])
        self.assertEqual([(m["name"], m["action"]) for m in doc["shared"]["members"]],
                         [("linux-web", "removed")])
        self.assertEqual(sorted(os.listdir(os.path.join(self.shared, "game"))), ["laptop-ui"])

    def test_a_record_of_the_other_root_isnt_this_machines_own(self):
        # mac-web holds a record of game in the home's root; game in the shared root has a
        # mac-web of another box: that one is no membership of this machine's
        self.lead("game", where=("--local",))
        self.ok("join", "game", "--local")
        self.use_box("win")
        self.ok("create", "game", "--local", "--shared", "--project", "ui")
        self.use_box("mac2", os.path.join(self.tmp, "second-mac"))
        with open(os.path.join(self.homes["mac2"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = mac\n")
        self.ok("join", "game", "--local", "--shared")
        self.use_box("mac")
        self.idle_setup()
        self.fresh(self.root, "game", "laptop-ui")
        self.fresh(self.shared, "game", "win-ui")
        doc = json.loads(self.ok("list", "--remove-idle", "--local", "--json"))
        self.assertEqual([(m["name"], m["action"], m["why"]) for m in doc["members"]],
                         [("mac-web", "keep", "own")])
        self.assertEqual([(m["name"], m["action"]) for m in doc["shared"]["members"]],
                         [("mac-web", "remove")])

    def test_a_live_session_with_a_record_of_the_other_root(self):
        self.lead("game", where=("--server", "fake-dest"))
        self.ok("join", "game", "--server", "fake-dest")
        self.lead("game", where=("--server", "fake-dest", "--shared"), box="win")
        hold(self, channel_cmd.watcher_snapshot(None, "game.mac-web", "mac-web") + ".lock")
        self.assertEqual(self.refusal("join", "game", "--server", "fake-dest", "--shared"), (
            "ERROR channel: a live session holds mac-web in game (this machine's record: a "
            "membership in that server's channel root in its home)",
            "pass --role R to join from here as another member"))

    def test_another_error_still_stops_list(self):
        real = channel_cmd._Local.list

        def listing(server):
            if server.shared:
                raise VCharonError("io", "the shared root can't be read")
            return real(server)

        with mock.patch.object(channel_cmd._Local, "list", listing):
            self.assertEqual(self.refused("list", "--local"),
                             "ERROR io: the shared root can't be read")

    def test_join_fix_lines_name_shared(self):
        self.lead(where=("--server", "fake-dest", "--shared"))
        self.ok("join", "game", "--server", "fake-dest", "--shared")
        # another machine (another client id) with the same box: the takeover's command
        self.use_box("mac2", os.path.join(self.tmp, "second-mac"))
        with open(os.path.join(self.homes["mac2"], "vcharon.ini"), "w") as f:
            f.write("[vcharon]\nbox = mac\n")
        with mock.patch.object(platform, "client_id", return_value=(OTHER_CLIENT, "machine id")):
            line, fix = self.refusal("join", "game", "--server", "fake-dest", "--shared")
        self.assertEqual(line, "ERROR channel: another machine holds mac-web in game")
        self.assertTrue(fix.endswith(platform.runnable(
            "vcharon join game --server fake-dest --shared --rejoin --takeover --project web")),
            fix)
        # the lobby is made by its first join: create's fix keeps --shared
        self.assertEqual(self.refusal("create", "lobby", "--local", "--shared", code=3), (
            "ERROR config: the lobby is made by its first join",
            platform.runnable("join it: vcharon join lobby --local --shared --project web")))

    def test_create_takes_the_root_from_the_record(self):
        self.ok("create", "game", "--server", "fake-dest", "--shared")
        # no --shared: the record's root, where the channel is
        self.assertEqual(self.refused("create", "game", "--server", "fake-dest"),
                         "ERROR channel: the channel game already exists")


class NotLinuxTest(SharedCase):
    def test_refused_off_linux(self):
        with mock.patch.object(fsops, "LINUX", False):
            self.assertEqual(self.refusal("create", "game", "--local", "--shared", code=3),
                             ("ERROR config: " + channels.SHARED_NOT_LINUX,
                              channels.SHARED_NOT_LINUX_FIX))
            # list shows no shared group there, and --json says there is none
            self.assertEqual(self.ok("list", "--local").splitlines(),
                             ["vcharon: list  (this machine)", "  no channels"])
            self.assertIsNone(json.loads(self.ok("list", "--local", "--json"))["shared"])
        self.assertFalse(os.path.exists(self.top))
        self.assertEqual(platform.os_name() == "linux", fsops.LINUX)

    def test_the_helper_says_whether_it_can_hold_one(self):
        # the gate list asks: the helper's own test, the one that refuses --shared
        self.assertIs(helper.hello()["shared_root"], fsops.LINUX)
        with mock.patch.object(fsops, "LINUX", False):
            self.assertIs(helper.hello()["shared_root"], False)

    def test_a_server_that_cant_hold_one(self):
        # a server whose helper can't hold a shared root, whatever its hello's os says (a BSD
        # says linux): list asks it nothing and shows no shared group, as off Linux
        init = channel_cmd._Remote.__init__

        def no_shared(remote, session, alias, hello, log, shared=False):
            init(remote, session, alias, dict(hello, shared_root=False), log, shared)

        with mock.patch.object(channel_cmd._Remote, "__init__", no_shared):
            self.assertEqual(self.ok("list", "--server", "fake-dest").splitlines(),
                             ["vcharon: list  (fake-dest)", "  no channels"])
            self.assertIsNone(json.loads(self.ok("list", "--server", "fake-dest",
                                                 "--json"))["shared"])
            doc = json.loads(self.ok("list", "--server", "fake-dest", "--remove-idle",
                                     "--json"))
            self.assertIsNone(doc["shared"])
        self.assertFalse(os.path.exists(self.top))
