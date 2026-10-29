"""A sweep of a remote channel's whole life over the fake ssh for what must never be written to
a channel or shown: the home folders and the raw machine ids (DESIGN, "Rules for the code")."""

from __future__ import annotations

import contextlib
import io
import os
import re
import unittest
from unittest import mock

from vcharon import cli, platform
from vcharon.mailbox import watch

from tests.test_channel import ChannelCase
from tests.util import read_tree

# The machine ids: the fake server's, the client's, and another server's; each unlike the
# others and unlike the hashes vcharon shows
SERVER_ID = "5e4e45e400000000000000000000a001"
CLIENT_ID = "c11e47c100000000000000000000b002"
OTHER_ID = "07e4007e00000000000000000000c003"
# folder names found nowhere but in the paths of the two homes, and in the project's path
CLIENT_HOME = "client-hq7k"
SERVER_HOME = "server-zx9w"
WORK = "work-m3vd"
# a path from a home shown as ~ (DESIGN, "Tests": never any ~, which a Windows short name holds)
_TILDE_PATH = re.compile(r"(?<![^\s(`'\"])~[/\\]")
# a watcher line's time
_STAMP = re.compile(r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
# The lines that keep the client's home whole (DESIGN, "Rules for the code"): doctor's config
# and dirs lines and its folders note. Not a fix line: the flow's fix lines name no command
# with a path in the home, and a prose path in one shows as ~ (platform.prose_hint)
CLIENT_KEPT = ("ok    config ", "ok    dirs ", "vcharon's folders: ")
# The lines that keep the server's home whole, past its <ssh>:<path> (sync's job lines): a fix
# line raised on the server, doctor's dirs line (the test's channel root, this machine's, is
# in the fake server's home) and ping's server line, a person's check of the server
SERVER_KEPT = ("fix: ", "ok    dirs ", "server   ")


class LeakSweepTest(ChannelCase):
    """Three remote members of one channel on the fake server, each a box of its own under one
    client home; the server's home is elsewhere, and the server's machine id and the
    client's differ."""

    def setUp(self):
        ChannelCase.setUp(self)
        # realpath: macOS's temp folder is a link (/var to /private/var), and the paths vcharon
        # prints from the current folder are the resolved ones
        tmp = os.path.realpath(self.tmp)
        self.client_home = os.path.join(tmp, CLIENT_HOME)
        self.home = os.path.join(tmp, SERVER_HOME)
        self.root = os.path.join(self.home, "channels")
        os.mkdir(self.client_home)
        os.mkdir(self.home)
        os.environ.update(HOME=self.client_home, USERPROFILE=self.client_home,
                          FAKE_SSH_HOME=self.home, VCHARON_CHANNELS_ROOT=self.root,
                          VCHARON_TEST_MACHINE_ID=SERVER_ID)
        # the fake server's id comes from the environment, the client's from here
        patcher = mock.patch.object(platform, "machine_id", lambda *a, **kw: CLIENT_ID)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.homes = {}
        for box in ("mac", "laptop", "pc"):
            self.use_box(box, os.path.join(self.client_home, "vc-" + box))
        self.project = os.path.join(self.client_home, WORK, "Web")
        os.makedirs(os.path.join(self.project, ".git"))
        os.chdir(self.project)
        # (box, argv, line) of every human-readable line printed
        self.human = []
        # all output, --json's too
        self.printed = []

    def sync_in_process(self, job, sync_args, pace=None, once=False):
        """watch.run_sync's result, for a sync run in this process: a child process wouldn't
        reach the fake ssh. Its stderr is the child's, which the watcher reads."""
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.sync_section(job)
        lines = err.getvalue().splitlines()
        self.human += [("child", ("sync", job), line) for line in lines]
        self.printed += lines
        return watch.parse_failure(code, lines, job)

    def on(self, box, *argv, code=0):
        """vcharon argv on box: its stdout and stderr."""
        self.use_box(box)
        with mock.patch.object(watch, "run_sync", self.sync_in_process):
            got, out, err = self.run_cli(*argv)
        self.assertEqual(got, code, out + err)
        lines = (out + err).splitlines()
        self.printed += lines
        if "--json" not in argv:
            self.human += [(box, argv, _STAMP.sub("", line).strip()) for line in lines]
        return out + err

    def channel_files(self):
        """{where:name: bytes} of every file of the channel: the server's tree and each member's
        local copy."""
        files = {}
        trees = [("server", os.path.join(self.root, "game"))]
        trees += [(box, os.path.join(home, "joined")) for box, home in self.homes.items()]
        for where, root in trees:
            for name, data in (read_tree(root) if os.path.isdir(root) else {}).items():
                files["%s:%s" % (where, name)] = data or b""
        return files

    def logs(self):
        """The text of each box's logs."""
        out = []
        for home in self.homes.values():
            logs = os.path.join(home, "logs")
            for name in os.listdir(logs) if os.path.isdir(logs) else ():
                with open(os.path.join(logs, name), encoding="utf-8", errors="replace") as f:
                    out.append(f.read())
        return out

    def flow(self):
        """A channel's life, as its members' agents run it, with two errors; returns the
        channel's files as they were before the close deleted the server's tree."""
        self.on("laptop", "create", "game", "--server", "fake-dest", "--project", "ui")
        self.on("mac", "join", "game", "--server", "fake-dest")
        self.on("pc", "join", "game", "--server", "fake-dest", "--project", "api")
        self.on("mac", "post", "game", "--to", "@laptop-ui", "--title", "step 1 done",
                "--body", "tests pass")
        self.on("laptop", "watch", "game", "--once", "--project", "ui")
        self.on("laptop", "post", "game", "--to", "@all", "--title", "thanks", "--body", "ok",
                "--project", "ui")
        self.on("mac", "watch", "game", "--once")
        self.on("mac", "read", "game", "--full")
        self.on("mac", "read", "game", "--json")
        self.on("mac", "whoami", "game")
        self.on("mac", "whoami", "game", "--json")
        self.on("mac", "list", "--server", "fake-dest")
        self.on("mac", "list", "--server", "fake-dest", "--json")
        self.on("mac", "doctor")
        self.on("mac", "doctor", "--json")
        self.on("mac", "ping", "fake-dest")
        self.on("mac", "sync", "game")
        # the alias now reaches another machine: refused, with both ids
        os.environ["VCHARON_TEST_MACHINE_ID"] = OTHER_ID
        self.assertIn(" both hashed)", self.on("pc", "leave", "game", "--project", "api",
                                               code=1))
        os.environ["VCHARON_TEST_MACHINE_ID"] = SERVER_ID
        self.on("pc", "leave", "game", "--project", "api")
        files = self.channel_files()
        self.on("laptop", "close", "game", "--project", "ui")
        # the channel is gone: errors raised on the server, in sync's lines and the watcher's
        self.assertIn(" not_found: ", self.on("mac", "sync", "game", code=1))
        self.on("mac", "watch", "game", "--once", code=watch.EXIT_CLOSED)
        self.on("mac", "leave", "game")
        return files

    def test_no_home_or_machine_id_leaks(self):
        files = self.flow()
        # In a channel's files, no path, user name or raw machine id: each home's folder name
        # is in every path under it, the project's folder in a path relative to the home, and a
        # path shown from a home starts with ~
        self.assertIn("server:mac-web/RESULTS.md", files.keys())
        self.assertIn("mac:game.mac-web/laptop-ui/CHANNEL.md", files.keys())
        for name, data in sorted(files.items()):
            text = (name + "\n" + data.decode("utf-8", "replace")).casefold()
            for word in (CLIENT_HOME, SERVER_HOME, WORK, SERVER_ID, CLIENT_ID, OTHER_ID):
                with self.subTest(file=name, word=word):
                    self.assertNotIn(word, text)
            with self.subTest(file=name, word="~/"):
                self.assertEqual(_TILDE_PATH.findall(text), [])

        # Printed and logged, a machine id only hashed, --json included
        logs = self.logs()
        self.assertTrue(logs)
        everything = "\n".join(self.printed + logs)
        for word in (SERVER_ID, CLIENT_ID, OTHER_ID):
            self.assertEqual([line for line in everything.splitlines() if word in line], [])
        self.assertIn("machine  id %s (hashed)" % platform.shown_machine(SERVER_ID), everything)
        self.assertIn("machine           id %s (hashed)" % platform.shown_machine(CLIENT_ID),
                      everything)

        def whole(home, line):
            return home.casefold() in line.casefold() if os.name == "nt" else home in line

        # The client's home as ~ in a human-readable line, but where DESIGN keeps it whole
        self.assertTrue([l for _, _, l in self.human if l.startswith("ok    dirs ")
                         and whole(self.client_home, l)], self.human)
        self.assertIn("your folder is ~%svc-mac%s" % (os.sep, os.sep), everything)
        self.assertEqual([(box, " ".join(argv), line) for box, argv, line in self.human
                          if whole(self.client_home, line) and not line.startswith(CLIENT_KEPT)],
                         [])

        if os.name == "nt":
            # the fake server is this machine: on Windows its home has backslashes, which a
            # real server's POSIX home never has, so it is shown whole
            return
        # The server's home as server:~, but where DESIGN keeps it whole
        ssh_path = "fake-dest:" + self.home
        self.assertIn(ssh_path, everything)
        self.assertEqual([(box, " ".join(argv), line) for box, argv, line in self.human
                          if self.home in line.replace(ssh_path, "")
                          and not line.startswith(SERVER_KEPT)],
                         [])
        self.assertIn("ERROR game.mac-web.down: not_found: server:~/channels/game doesn't exist",
                      everything)


if __name__ == "__main__":
    unittest.main()
