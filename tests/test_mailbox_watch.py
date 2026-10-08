"""vcharon watch: scans, the lines it prints, both modes; the time on each line,
the saved snapshot, its lock, --until-change and the limits; a channel's entries,
snapshot version 2."""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import queue
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

from vcharon import (
    channel_cmd,
    channels,
    charter,
    cli,
    entries,
    fsops,
    install,
    platform,
    skill,
)
from vcharon.mailbox import read, watch
from vcharon.proto import VCharonError

from tests import util
from tests.util import PACKAGE_DIR, write_tree

# what follows `vcharon sync` for the remote member windows (ClientModeTest.write_config)
SYNC = ["mb", "--project", "p"]

# 2026-10-01 09:05:46 on this machine's clock, whatever its zone
T0 = time.mktime((2026, 10, 1, 9, 5, 46, 0, 0, -1))
STAMP = re.compile(r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
# exit 12's line: a watcher, or a create, join, leave or close of the member, holds the lock
LOCKED = ("ERROR another watcher is running on this mailbox (%s.lock), or a create, join, "
          "leave or close of this member")


def silent_fix(job="mb.windows"):
    """The fix of a sync child that exited with no line: its jobs' logs and vcharon.log."""
    logs = platform.log_dir()
    return "look at %s, %s and %s; if it happens again, tell your user" % (
        os.path.join(logs, job + ".up.log"), os.path.join(logs, job + ".down.log"),
        os.path.join(logs, "vcharon.log"))


def watching(tree, n, tail=""):
    """The watcher's first line: n counts the files outside the member's own folder."""
    return "watching %s, %d files in other folders%s" % (tree, n, tail)


class Clock:
    """A clock for the watch loops: both the lines' time and the limits' timer."""

    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Rounds:
    """A sleep for the watch loops: each call runs the next of the given steps, and moves the
    clock on if there is one."""

    def __init__(self, *steps, clock=None):
        self.steps = list(steps)
        self.seconds = []
        self.clock = clock

    def __call__(self, seconds):
        self.seconds.append(seconds)
        if self.clock is not None:
            self.clock.t += seconds
        self.steps.pop(0)()


def no_site_env():
    """The environment of a child run with -S (no site, so no .pth file read, as in a binary;
    site.py decodes .pth files with utf-8-sig, which would hide a lazy
    import of that codec from the import checks): the package found through PYTHONPATH, in
    place of the editable install's .pth.
    The environment's own scripts folder first on PATH: with an entry point named vcharon there
    (pip install -e . makes one), a fix line's spelling reads sysconfig's data, as on CI."""
    return dict(os.environ, PYTHONPATH=os.path.dirname(PACKAGE_DIR),
                PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))


def never(seconds):
    raise AssertionError("slept before the first round")


def member_md(name, leader, channel="mb"):
    """MEMBER.md as vcharon join writes it."""
    return ("# MEMBER\n\n## 2026-10-01 09:00 — %s#1 — member\nto: @%s\nchannel: %s\nname: %s\n"
            "leader: %s\n" % (name, leader, channel, name, leader)).encode("utf-8")


def gone(pid, wait=10):
    """Whether the process pid ends within wait seconds; on Linux a zombie (dead, not yet
    reaped by its new parent) counts as ended."""
    until = time.monotonic() + wait
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if sys.platform.startswith("linux"):
            try:
                with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
                    if f.read().rpartition(")")[2].split()[0] == "Z":
                        return True
            except (FileNotFoundError, ProcessLookupError):
                # gone between the kill and the read: the read raises ESRCH
                return True
        if time.monotonic() >= until:
            return False
        time.sleep(0.1)


def end_pid(pid):
    """Ends a process this test left behind, if it still runs."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class WatchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # a channel's folder, mb; server mode watches it as the member debian, its leader
        self.tree = os.path.join(self.tmp, "mb")
        # the saved snapshots and their locks go to vcharon's state dir: one per test
        self.vcharon_home = os.path.join(self.tmp, "vcharon-home")
        patch = mock.patch.dict(os.environ, {"VCHARON_HOME": self.vcharon_home})
        patch.start()
        self.addCleanup(patch.stop)
        self.lines = []
        self.member()

    def member(self, me="debian", leader="debian", root=None):
        """me's own folder with its MEMBER.md: server mode watches only such a folder's
        member."""
        write_tree(self.tree if root is None else root,
                   {"%s/MEMBER.md" % me: member_md(me, leader)})

    def post(self, folder, n, title, to="@debian", name=None, file="RESULTS.md",
             when="2026-10-01 09:00", root=None):
        """Appends one entry, as mailbox_post.py writes it, by name (folder's member unless
        given) to folder/file."""
        path = os.path.join(self.tree if root is None else root, folder, *file.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = entries.build(when, name or folder, n, title, to.split())
        with open(path, "ab") as f:
            if not f.tell():
                f.write(b"# RESULTS\n")
            f.write(text.encode("utf-8"))
        return path

    def said(self, lines=None):
        """The lines without their time, each checked to start with one."""
        out = []
        for line in self.lines if lines is None else lines:
            self.assertRegex(line, STAMP)
            out.append(line[20:])
        return out

    def saved_at(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)["saved"]

    def state(self, me="debian", job=None, root=None):
        return watch.snapshot_path(self.tree if root is None else root, me, job)

    def local_record(self, me="debian", leader="debian", project="q", role=None):
        """me's join record as a local member of mb, whose folder is self.tree: vcharon watch
        mb --project <project> then watches it as me (DESIGN, "Which membership")."""
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": me, "leader": leader,
                                  "ssh": None, "remote": self.tree,
                                  "machine": util.TEST_MACHINE_ID, "project": project,
                                  "role": role, **util.record_format()})

    def cli(self, *argv, project="q"):
        """vcharon watch mb --project <project> ARGV in this process: (exit code, stdout,
        stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(["watch", "mb", "--project", project] + list(argv))
        return code, out.getvalue(), err.getvalue()


class ExitLineTest(unittest.TestCase):
    """Every EXIT line ends with the code the watcher exits with: an agent's tool may report
    another."""

    def ending(self, run):
        lines = []
        w = types.SimpleNamespace(say=lines.append, updated=False, closed=False, busy=None,
                                  error=None)
        code = run(w)
        m = re.fullmatch(r"EXIT [a-z0-9 ]+ \(exit (\d+)\)", lines[-1])
        self.assertIsNotNone(m, lines)
        self.assertEqual(int(m.group(1)), code, lines)
        return lines[-1]

    def test_the_line(self):
        self.assertEqual(watch.exit_line("quiet 1 min", watch.EXIT_QUIET),
                         "EXIT quiet 1 min (exit 10)")

    def test_each_ending_says_its_code(self):
        def once(changes=(), saved=True, **state):
            def run(w):
                vars(w).update(state)
                return watch._once(w, lambda: (list(changes), False, saved))
            return run

        self.assertEqual(self.ending(once()), "EXIT nothing new (exit 16)")
        self.assertEqual(self.ending(once(["to you: a#1"])), "EXIT change (exit 0)")
        self.assertEqual(self.ending(once(saved=False)), "EXIT error (exit 11)")
        self.assertEqual(self.ending(once(closed=True)), "EXIT closed (exit 13)")
        self.assertEqual(self.ending(once(updated=True)), "EXIT updated (exit 14)")
        self.assertEqual(self.ending(
            lambda w: watch._once(w, None, orphaned=lambda: True)), "EXIT orphaned (exit 15)")
        clock = Clock()

        def sleep(seconds):
            clock.t += seconds

        self.assertEqual(self.ending(lambda w: watch._loop(
            w, 10, sleep, lambda: ([], False, True), clock, False, False, 1, 10, None)),
            "EXIT quiet 1 min (exit 10)")


class ScanTest(WatchCase):
    def test_what_it_skips(self):
        write_tree(self.tree, {"debian/STEPS.md": b"s", "debian/windows/x": b"x",
                               "windows/mine.md": b"m", ".vcharon-stage-0123/1": b"",
                               "mac/.VCHARON-STAGE-ab/2": b"", "mac/a b.md": b"ab", "empty/": None})
        if hasattr(os, "symlink"):
            try:
                os.symlink("debian", os.path.join(self.tree, "link"))
            except OSError:
                pass
        self.assertEqual(sorted(watch.scan(self.tree, "windows")),
                         ["debian/MEMBER.md", "debian/STEPS.md", "debian/windows/x",
                          "mac/a b.md"])
        # a missing root is an error, not an empty tree: every file would look gone
        with self.assertRaises(FileNotFoundError):
            watch.scan(os.path.join(self.tmp, "missing"), "windows")

    def test_fold(self):
        # a Windows or macOS client: Windows/ is the own folder there
        write_tree(self.tree, {"Windows/a": b"a", "debian/b": b"b"})
        self.assertEqual(sorted(watch.scan(self.tree, "windows", fold=True)),
                         ["debian/MEMBER.md", "debian/b"])
        self.assertEqual(sorted(watch.scan(self.tree, "windows")),
                         ["Windows/a", "debian/MEMBER.md", "debian/b"])

    def test_diff(self):
        old = {"a": (1, 1), "b": (1, 1), "c": (1, 1)}
        new = {"a": (1, 1), "b": (1, 2), "d": (1, 1)}
        self.assertEqual(watch.diff(old, new), ["changed b", "gone c", "new d"])
        self.assertEqual(watch.diff(new, new), [])
        # a member's .md files are read for entries, never shown as files; the top's are
        old = {"m/a.md": (1, 1), "m/b.md": (1, 1), "m/x.MD": (1, 1), "top.md": (1, 1)}
        new = {"m/a.md": (1, 2), "m/c.md": (1, 1), "m/x.MD": (1, 2), "top.md": (1, 2)}
        self.assertEqual(watch.diff(old, new), ["changed m/x.MD", "changed top.md"])


class ServerModeTest(WatchCase):
    def test_rounds(self):
        # files that aren't entry files; entries have their own tests (EntriesTest)
        write_tree(self.tree, {"windows/run.log": b"r", "debian/STEPS.md": b"s"})

        def change():
            write_tree(self.tree, {"windows/run.log": b"rr", "windows/new.patch": b"n",
                                   "mac/x": b"x"})
            os.utime(os.path.join(self.tree, "windows", "run.log"), ns=(1, 1))

        def own():
            # only the watcher's own folder changes: nothing to say
            write_tree(self.tree, {"debian/STEPS.md": b"s2", "debian/.vcharon-stage-1/x": b""})
            self.post("debian", 2, "own", to="@windows")

        def remove():
            shutil.rmtree(os.path.join(self.tree, "windows"))

        sleep = Rounds(lambda: None, change, own, remove)
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=sleep,
                               rounds=4)
        self.assertEqual(code, 0)
        self.assertEqual(self.said(), [watching(self.tree, 1),
                                       "new mac/x", "new windows/new.patch",
                                       "changed windows/run.log",
                                       "gone windows/new.patch", "gone windows/run.log"])
        self.assertEqual(sleep.seconds, [10] * 4)


class LocalStampTest(WatchCase):
    """A local member's rounds stamp its last-watched time beside the channel root, silently,
    and only for a channel in this machine's root."""

    def setUp(self):
        WatchCase.setUp(self)
        self.root = os.path.join(self.tmp, "channels")
        self.tree = os.path.join(self.root, "mb")
        self.member()
        patch = mock.patch.dict(os.environ, {"VCHARON_CHANNELS_ROOT": self.root})
        patch.start()
        self.addCleanup(patch.stop)
        self.stamp = os.path.join(self.tmp, "seen", "mb", "debian")

    def test_a_round_stamps(self):
        # a positive control: a file that isn't an entry still prints its line
        sleep = Rounds(lambda: write_tree(self.tree, {"windows/x.txt": b"x"}))
        code = watch.watch_dir(self.tree, "debian", 7, out=self.lines.append, sleep=sleep,
                               rounds=1)
        self.assertEqual(code, 0)
        self.assertEqual(self.said(), [watching(self.tree, 0), "new windows/x.txt"])
        with open(self.stamp, "rb") as f:
            self.assertEqual(f.read(), b"local 7\n")
        self.assertEqual(channels.list_seen(self.root, "mb")["debian"][1], "local 7")

    def test_a_failed_stamp_says_nothing(self):
        with open(os.path.join(self.tmp, "seen"), "wb") as f:
            f.write(b"not a folder")
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                               sleep=Rounds(lambda: None), rounds=1)
        self.assertEqual(code, 0)
        self.assertEqual(self.said(), [watching(self.tree, 0)])

    def test_a_failed_scan_isnt_stamped(self):
        # a round that couldn't read the channel isn't a watched round
        def scan(root, me, fold=False):
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(watch, "scan", scan):
            watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                            sleep=Rounds(lambda: None), rounds=1)
        self.assertTrue(any("Permission denied" in line for line in self.said()))
        self.assertFalse(os.path.lexists(self.stamp))

    def test_a_folder_outside_the_root_isnt_stamped(self):
        # even with a channel of the same name and member there: the stamp would be that one's
        os.environ["VCHARON_CHANNELS_ROOT"] = os.path.join(self.tmp, "elsewhere")
        write_tree(os.path.join(self.tmp, "elsewhere"), {"mb/debian/": None})
        watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                        sleep=Rounds(lambda: None), rounds=1)
        self.assertFalse(os.path.lexists(os.path.join(self.tmp, "seen")))


class TimeTest(WatchCase):
    def test_every_line_starts_with_the_local_time(self):
        write_tree(self.tree, {"mac/x": b"x"})
        real = watch.scan
        fail = [False]

        def scan(root, me, fold=False):
            if fail[0]:
                raise PermissionError(13, "Permission denied")
            return real(root, me, fold)

        def made():
            write_tree(self.tree, {"mac/y": b"y"})

        def broken():
            fail[0] = True

        def fixed():
            fail[0] = False

        clock = Clock()
        with mock.patch.object(watch, "scan", scan):
            code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                                   sleep=Rounds(made, broken, fixed, *[lambda: None] * 3,
                                                clock=clock),
                                   clock=clock, timer=clock, max_minutes=1)
        # a minute after the start, between rounds
        self.assertEqual(code, 10)
        self.assertEqual(self.lines, [
            "2026-10-01 09:05:46 " + watching(self.tree, 1),
            "2026-10-01 09:05:56 new mac/y",
            "2026-10-01 09:06:06 ERROR can't read %s: Permission denied" % self.tree,
            "2026-10-01 09:06:16 ok again",
            "2026-10-01 09:06:46 EXIT quiet 1 min (exit 10)"])

class RootTest(WatchCase):
    def test_missing_at_the_start_is_empty(self):
        # in client mode, before the first run (server mode's tree holds the member's
        # MEMBER.md, so it's there); what the first run brings is all new
        sync_args = ClientModeTest.write_config(self)
        shutil.rmtree(self.tree)

        def run(job, sync_args):
            self.post("debian", 2, "first", to="@windows")
            write_tree(self.tree, {"mac/x": b"x"})
            return 0, None, None

        watch.watch_job("mb.windows", sync_args, 1, out=self.lines.append, sleep=never, run=run,
                        rounds=1)
        self.assertEqual(self.said(), [watching(self.tree, 0),
                                       "to you: debian#2 — first  (debian/RESULTS.md)",
                                       "new mac/x"])


class SnapshotTest(WatchCase):
    def run_dir(self, *steps, rounds=None, **kw):
        lines = []
        code = watch.watch_dir(self.tree, "debian", 10, out=lines.append,
                               sleep=Rounds(*steps) if steps else never,
                               rounds=len(steps) if rounds is None else rounds, **kw)
        return code, self.said(lines)

    def test_saved_after_each_round(self):
        write_tree(self.tree, {"mac/x": b"x"})

        def made():
            write_tree(self.tree, {"mac/y": b"yy"})

        self.run_dir(made)
        path = self.state()
        self.assertEqual(os.path.dirname(path), os.path.join(self.vcharon_home, "state"))
        name = os.path.basename(path)
        self.assertRegex(name, r"\Amailbox-watch-dir-debian-[0-9a-f]{12}\.json\Z")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        files = watch.scan(self.tree, "debian")
        self.assertEqual(doc["version"], 2)
        self.assertEqual((doc["root"], doc["me"]), (self.tree, "debian"))
        self.assertEqual((doc["seen"], doc["heads"], doc["loose"]), ({}, {}, []))
        self.assertRegex(doc["saved"], r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\Z")
        self.assertEqual(doc["files"], {k: list(v) for k, v in files.items()})
        # another tree or another writer has its own file
        self.assertNotEqual(self.state(root=self.tree + "2"), path)
        self.assertNotEqual(self.state(me="mac"), path)
        # no temp file left
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), [name, name + ".lock"])

    def test_a_restart_prints_what_changed_while_stopped(self):
        write_tree(self.tree, {"mac/x": b"x", "mac/gone": b"g"})
        self.assertEqual(self.run_dir(rounds=0)[1], [watching(self.tree, 2)])
        saved = self.saved_at(self.state())
        write_tree(self.tree, {"mac/y": b"y", "mac/x": b"xx"})
        os.remove(os.path.join(self.tree, "mac", "gone"))
        # the first round comes at once, with no sleep
        _code, lines = self.run_dir(rounds=1)
        self.assertEqual(lines, [watching(self.tree, 2, ", since " + saved),
                                 "gone mac/gone", "changed mac/x", "new mac/y"])
        # and that round's snapshot is saved: nothing again
        self.assertEqual(self.run_dir(rounds=1)[1][1:], [])

    def test_unusable_snapshots_are_noted_and_ignored(self):
        write_tree(self.tree, {"mac/x": b"x"})
        self.run_dir(rounds=0)
        path = self.state()
        with open(path, encoding="utf-8") as f:
            good = json.load(f)
        os.utime(os.path.join(self.tree, "mac", "x"), ns=(1, 1))

        def other(**kw):
            doc = dict(good)
            doc.update(kw)
            return json.dumps(doc).encode("utf-8")

        bad_size = dict(good["files"])
        bad_size["mac/x"] = [True, 1]
        less = dict(good)
        del less["loose"]
        no_warnings = dict(good)
        del no_warnings["warnings"]
        for data, why in ((b"{", "it isn't JSON"), (b"\xff", "it isn't JSON"),
                          (b"[]", "it has another shape"),
                          (other(version=3), "it has another shape"),
                          (other(version=True), "it has another shape"),
                          (other(files={"a": [1]}), "it has another shape"),
                          (other(files=bad_size), "it has another shape"),
                          (other(extra=1), "it has another shape"),
                          (json.dumps(less).encode(), "it has another shape"),
                          (json.dumps(no_warnings).encode(), "it has another shape"),
                          (other(warnings=[1]), "it has another shape"),
                          (other(seen={"mac": {"low": 2, "more": [2]}}), "it has another shape"),
                          (other(seen={"mac": {"low": -1, "more": []}}), "it has another shape"),
                          (other(seen={"mac": {"low": 0}}), "it has another shape"),
                          (other(heads={"mac#1": "xyz"}), "it has another shape"),
                          (other(heads={"mac#1": ["mac/R.md"]}), "it has another shape"),
                          (other(heads={"mac#1": [1, "0123456789ab"]}),
                           "it has another shape"),
                          (other(heads={"mac#1": ["mac/R.md", "xyz"]}),
                           "it has another shape"),
                          (other(loose=[1]), "it has another shape"),
                          (other(root="/elsewhere"), "it is for /elsewhere"),
                          (other(me="mac"), "it is for the member mac")):
            with self.subTest(why=why, data=data[:30]):
                with open(path, "wb") as f:
                    f.write(data)
                # a fresh baseline: the changed mtime isn't printed
                _code, lines = self.run_dir(lambda: None)
                self.assertEqual(lines, ["note: ignoring the saved snapshot %s: %s" % (path, why),
                                         watching(self.tree, 1)])

    def test_the_optional_keys(self):
        # error, counted and fix: absent, null or empty when nothing is pending
        path = self.state()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"version": 2, "root": self.tree, "me": "debian", "saved": "then", "files": {},
               "warnings": [], "seen": {}, "heads": {}, "loose": []}
        for extra, error, fix in (
                ({}, None, None), ({"error": None}, None, None), ({"counted": []}, None, None),
                ({"counted": None}, None, None), ({"error": None, "counted": []}, None, None),
                ({"error": "ERROR x"}, "ERROR x", None),
                ({"error": "ERROR x", "fix": None}, "ERROR x", None),
                ({"error": "ERROR x", "fix": "do y"}, "ERROR x", "do y")):
            with self.subTest(extra=extra):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(dict(doc, **extra), f)
                loaded = watch.load_snapshot(path, self.tree, "debian")
                self.assertEqual(loaded[:6], ({}, "then", None, [], error, []))
                self.assertEqual(loaded[6].doc(), {"seen": {}, "heads": {}, "loose": []})
                self.assertEqual(loaded[7], fix)
        # a value of another type is another shape
        for bad in ({"error": 1}, {"counted": "transport"}, {"counted": [1]}, {"counted": ""},
                    {"error": "ERROR x", "fix": 1}):
            with self.subTest(bad=bad):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(dict(doc, **bad), f)
                self.assertEqual(watch.load_snapshot(path, self.tree, "debian")[2],
                                 "it has another shape")

    def test_fresh(self):
        write_tree(self.tree, {"mac/x": b"x"})
        self.run_dir(rounds=0)
        write_tree(self.tree, {"mac/y": b"y"})
        _code, lines = self.run_dir(lambda: None, fresh=True)
        self.assertEqual(lines, [watching(self.tree, 2, ", fresh start")])
        # the fresh baseline is saved
        self.assertEqual(self.run_dir(rounds=1)[1][1:], [])

    def test_the_save_comes_after_the_lines(self):
        write_tree(self.tree, {"mac/x": b"x"})
        self.run_dir(rounds=0)
        write_tree(self.tree, {"mac/y": b"y"})

        class Crash(Exception):
            pass

        def crash(*args):
            raise Crash

        # the process dies between the round's lines and its save
        lines = []
        with mock.patch.object(watch, "save_snapshot", crash), self.assertRaises(Crash):
            watch.watch_dir(self.tree, "debian", 10, out=lines.append, sleep=never, rounds=1)
        self.assertEqual(self.said(lines)[1:], ["new mac/y"])
        # printed again at the next start, never lost
        self.assertEqual(self.run_dir(rounds=1)[1][1:], ["new mac/y"])

    def test_an_unchanged_snapshot_isnt_written_again(self):
        # a streaming watch has a round every 2 s; a quiet one doesn't fsync each
        write_tree(self.tree, {"mac/x": b"x"})
        real = watch.save_snapshot
        saves = []

        def save(*args, **kw):
            saves.append(args[4])
            return real(*args, **kw)

        def made():
            write_tree(self.tree, {"mac/y": b"y"})

        clock = Clock()
        with mock.patch.object(watch, "save_snapshot", save):
            _code, lines = self.run_dir(lambda: None, lambda: None, made, lambda: None,
                                       clock=clock, timer=clock)
        self.assertEqual(lines, [watching(self.tree, 1), "new mac/y"])
        # the start's, then the change's; the quiet rounds wrote nothing
        self.assertEqual(len(saves), 2)
        # a restart says since the last change
        self.assertEqual(self.run_dir(rounds=0)[1], [watching(self.tree, 2, ", since "
                                                              + saves[-1])])

    def test_a_restart_with_nothing_new_keeps_since(self):
        # the snapshot is written only when it changed: a restart's first round that changes
        # nothing keeps its saved time, so since stays the last change's
        write_tree(self.tree, {"mac/x": b"x"})
        clock = Clock()
        self.run_dir(rounds=0, clock=clock, timer=clock)
        saved = self.saved_at(self.state())
        for hours in (1, 2):
            clock.t = T0 + 3600 * hours
            _code, lines = self.run_dir(rounds=1, clock=clock, timer=clock)
            self.assertEqual(lines, [watching(self.tree, 1, ", since " + saved)])
            self.assertEqual(self.saved_at(self.state()), saved)

    def test_a_replace_held_for_a_moment_on_windows(self):
        # a virus scanner holding the new file (32), or a program with the old one open
        # without delete sharing (5): tried again, as every other state file
        path = self.state()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        real = os.replace
        calls = []

        def replace(src, dst):
            calls.append(dst)
            if len(calls) <= 2:
                e = PermissionError(13, "The process cannot access the file")
                e.winerror = (32, 5)[len(calls) - 1]
                raise e
            return real(src, dst)

        with mock.patch.object(watch.fsops, "WINDOWS", True), \
                mock.patch.object(watch.fsops, "RETRY_DELAY", 0), \
                mock.patch.object(watch.os, "replace", replace):
            watch.save_snapshot(path, self.tree, "debian", {}, "then")
        self.assertEqual(calls, [path, path, path])
        self.assertEqual(watch.load_snapshot(path, self.tree, "debian")[1], "then")
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), [os.path.basename(path)])

    def test_an_older_snapshots_heads(self):
        # a head was a bare hash before it held its file: still read, and kept as it was
        # until its ID is seen again
        path = self.state()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"version": 2, "root": self.tree, "me": "debian", "saved": "then", "files": {},
               "warnings": [], "seen": {"mac": {"low": 2, "more": []}},
               "heads": {"mac#2": "0123456789ab", "mac#1": ["mac/MEMBER.md", "ba9876543210"]},
               "loose": []}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        marks = watch.load_snapshot(path, self.tree, "debian")[6]
        self.assertEqual(marks.heads, {"mac#2": [None, "0123456789ab"],
                                       "mac#1": ["mac/MEMBER.md", "ba9876543210"]})
        self.assertEqual(marks.doc()["heads"], doc["heads"])

    def test_a_failed_save_is_shown_once(self):
        write_tree(self.tree, {"mac/x": b"x"})

        def full(*args):
            raise OSError(28, "No space left on device")

        def made():
            write_tree(self.tree, {"mac/y": b"y"})

        with mock.patch.object(watch, "save_snapshot", full):
            code, lines = self.run_dir(made, lambda: None)
        line = "ERROR can't save the snapshot %s: No space left on device" % self.state()
        # and a continuous watch goes on
        self.assertEqual((code, lines), (0, [watching(self.tree, 1), line, "new mac/y"]))

    def test_client_mode_restart(self):
        sync_args = ClientModeTest.write_config(self)
        brings = [lambda: self.post("debian", 2, "steps", to="@all", file="STEPS.md"),
                  lambda: None, lambda: self.post("debian", 4, "results", to="@windows")]

        def run(job, sync_args):
            brings.pop(0)()
            return 0, None, None

        lines = []
        watch.watch_job("mb.windows", sync_args, 30, out=lines.append, sleep=never, run=run,
                        rounds=1)
        self.assertEqual(self.said(lines), [watching(self.tree, 0),
                                            "to all: debian#2 — steps  (debian/STEPS.md)"])
        path = self.state(me="windows", job="mb.windows", root=self.tree)
        self.assertEqual(os.path.basename(path), "mailbox-watch-mb.windows.json")
        saved = self.saved_at(path)
        # a manual run while no watcher ran brought an entry
        self.post("debian", 3, "answers", to="@windows", file="ANSWERS.md")
        lines = []
        watch.watch_job("mb.windows", sync_args, 30, out=lines.append, sleep=Rounds(lambda: None),
                        run=run, rounds=2)
        self.assertEqual(self.said(lines), [
            watching(self.tree, 1, ", since " + saved),
            "to you: debian#3 — answers  (debian/ANSWERS.md)",
            "to you: debian#4 — results  (debian/RESULTS.md)"])


class LockTest(WatchCase):
    @unittest.skipIf(os.name == "nt", "Windows can't delete an open file")
    def test_a_lock_file_deleted_under_the_taker(self):
        # leave and close delete the lock file while they hold it: a taker that opened it just
        # before holds a lock on a deleted file, while the next one locks a new file
        state = self.state()
        real = watch.Lock.open
        opened = []

        def deleted_first(path, *args, **kw):
            lk = real(path, *args, **kw)
            opened.append(path)
            if len(opened) == 1:
                os.unlink(path)
            return lk

        with mock.patch.object(watch.Lock, "open", deleted_first):
            lk = watch.take_lock(state)
        self.assertIsNotNone(lk)
        self.addCleanup(lk.release)
        self.assertEqual(len(opened), 2)
        st, held = os.stat(state + ".lock"), os.fstat(lk.fd)
        self.assertEqual((st.st_dev, st.st_ino), (held.st_dev, held.st_ino))
        self.assertIsNone(watch.take_lock(state))
        lk.release()

        # deleted under it every time: none, after a few tries
        def always(path, *args, **kw):
            lk = real(path, *args, **kw)
            opened.append(path)
            os.unlink(path)
            return lk

        del opened[:]
        with mock.patch.object(watch.Lock, "open", always):
            self.assertIsNone(watch.take_lock(state))
        self.assertEqual(len(opened), watch.LOCK_TRIES)

    def test_a_second_watcher_exits(self):
        write_tree(self.tree, {"mac/x": b"x"})
        other_tree = os.path.join(self.tmp, "other")
        self.member(root=other_tree)
        self.member("mac")
        seen = []

        def second():
            lines = []
            seen.append(watch.watch_dir(self.tree, "debian", 10, out=lines.append,
                                        sleep=never, rounds=0))
            seen.append(self.said(lines))
            # another mailbox, or another writer, has its own lock
            seen.append(watch.watch_dir(other_tree, "debian", 10, out=[].append, sleep=never,
                                        rounds=0))
            seen.append(watch.watch_dir(self.tree, "mac", 10, out=[].append, sleep=never,
                                        rounds=0))

        watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=Rounds(second),
                        rounds=1)
        self.assertEqual(seen, [12, [LOCKED % self.state()], 0, 0])
        # the lock goes with the first watcher
        self.assertEqual(watch.watch_dir(self.tree, "debian", 10, out=[].append, sleep=never,
                                         rounds=0), 0)

    def test_the_command_exits_12(self):
        write_tree(self.tree, {"mac/x": b"x"})
        self.local_record()
        codes = []

        def second():
            # a lock that let it through fails here, not by watching for 25 minutes
            ran = AssertionError("the second watch started")
            with mock.patch.object(watch, "_loop", side_effect=ran):
                code, out, _err = self.cli("--until-change")
            codes.extend([code, out])

        watch.watch_dir(self.tree, "debian", 10, out=[].append, sleep=Rounds(second), rounds=1)
        self.assertEqual(codes[0], 12)
        self.assertEqual(self.said(codes[1].splitlines()),
                         [LOCKED % self.state()])


class KeyTest(WatchCase):
    @unittest.skipUnless(util.CAN_SYMLINK, "no symlinks here")
    def test_two_spellings_of_one_dir_take_one_lock(self):
        # the snapshot and lock name come from normcase(realpath(--dir)), so a watcher
        # started through a link sees the one already running (exit 12)
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.tree, alias)
        self.assertEqual(watch.snapshot_path(alias, "debian"), self.state())
        seen = []

        def second():
            lines = []
            seen.append(watch.watch_dir(alias, "debian", 10, out=lines.append, sleep=never,
                                        rounds=0))
            seen.append(self.said(lines))

        watch.watch_dir(self.tree, "debian", 10, out=[].append, sleep=Rounds(second), rounds=1)
        self.assertEqual(seen, [12, [LOCKED % self.state()]])

    @unittest.skipUnless(os.name == "posix", "normcase folds case on Windows")
    def test_a_plain_path_keeps_its_name(self):
        # with no links the name is the sha256 of the path as main passed it, so a running
        # watcher keeps its snapshot
        real = os.path.realpath(self.tree)
        old = hashlib.sha256(os.fsencode(real)).hexdigest()[:12]
        self.assertTrue(watch.snapshot_path(real, "debian")
                        .endswith("mailbox-watch-dir-debian-%s.json" % old))

    @unittest.skipUnless(util.CAN_SYMLINK, "no symlinks here")
    def test_a_restart_under_another_spelling_goes_on(self):
        # the saved snapshot is the same folder's under either spelling: what came while no
        # watcher ran is printed, both ways (A -> B -> A)
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.tree, alias)

        def start(root, rounds=1):
            # a restart from a saved snapshot runs its first round at once; a fresh start
            # would sleep first, and never() fails the test
            lines = []
            watch.watch_dir(root, "debian", 10, out=lines.append, sleep=never, rounds=rounds)
            return self.said(lines)

        start(self.tree, rounds=0)
        self.post("mac", 2, "one")
        lines = start(alias)
        self.assertFalse(any(l.startswith("note: ignoring") for l in lines), lines)
        self.assertIn("to you: mac#2 — one  (mac/RESULTS.md)", lines)
        self.post("mac", 3, "two")
        lines = start(self.tree)
        self.assertFalse(any(l.startswith("note: ignoring") for l in lines), lines)
        self.assertIn("to you: mac#3 — two  (mac/RESULTS.md)", lines)
        self.assertNotIn("to you: mac#2 — one  (mac/RESULTS.md)", lines)


class WarnTest(WatchCase):
    """Server mode's WARN lines: names clients leave out at the top, and case
    twins in a writer's folder."""

    def run_dir(self, *steps, rounds=None, **kw):
        return SnapshotTest.run_dir(self, *steps, rounds=rounds, **kw)

    @unittest.skipIf(util.folds_case(), util.FOLDS_CASE)
    def test_the_checks(self):
        write_tree(self.tree, {"debian/STEPS.md": b"s", "debian/Notes.md": b"N",
                               "debian/notes.md": b"n", "mac/Sub/a": b"a", "mac/sub/b": b"b",
                               "mac/X.md": b"x", "mac/x.md/": None, "Mac/CASE.md": b"c",
                               "Debian/": None, "notes.md": b"n", "con/": None,
                               "README": b"r", "windows/ok.md": b"o",
                               # a stray's own twins don't matter: clients leave it out
                               "Stray/a": b"a", "Stray/A": b"A",
                               # composed and decomposed: one name on macOS only
                               "windows/\u00e9.md": b"1", "windows/e\u0301.md": b"2",
                               ".vcharon-stage-x/": None, "debian/.vcharon-stage-a/A": b"a",
                               "debian/.vcharon-stage-a/a": b"a",
                               # deeper in a writer's folder
                               "debian/sub/deep/A.md": b"a", "debian/sub/deep/a.md": b"a",
                               # names a Windows client can't hold
                               "mac/CON.md": b"c", "mac/a:b.md": b"c", "mac/dot./": None})
        os.symlink("mac", os.path.join(self.tree, "link"))
        os.symlink("x.md", os.path.join(self.tree, "mac", "inner"))
        os.mkfifo(os.path.join(self.tree, "debian", "pipe"))
        with open(os.path.join(os.fsencode(self.tree), b"debian", b"\xff.md"), "wb"):
            pass
        self.assertEqual(watch.warnings(self.tree, "debian"), sorted([
            "Debian/ at the top isn't a writer's folder: clients leave it out",
            "Mac/ at the top isn't a writer's folder: clients leave it out",
            "README at the top isn't a writer's folder: clients leave it out",
            "Stray/ at the top isn't a writer's folder: clients leave it out",
            "con/ at the top isn't a writer's folder: clients leave it out",
            "link at the top isn't a writer's folder: clients leave it out",
            "notes.md at the top isn't a writer's folder: clients leave it out",
            "case twins debian/Notes.md and debian/notes.md: macOS and Windows clients get "
            "nothing until one is removed",
            "case twins mac/Sub/ and mac/sub/: macOS and Windows clients get nothing until "
            "one is removed",
            "case twins mac/X.md and mac/x.md/: macOS and Windows clients get nothing until "
            "one is removed",
            "case twins windows/e\u0301.md and windows/\u00e9.md: macOS clients get nothing "
            "until one is removed",
            "case twins debian/sub/deep/A.md and debian/sub/deep/a.md: macOS and Windows "
            "clients get nothing until one is removed",
            "mac/CON.md: CON.md is a reserved name on Windows: Windows clients get nothing "
            "until it's renamed",
            "mac/a:b.md: a name holds \":\", which Windows doesn't allow: Windows clients get "
            "nothing until it's renamed",
            "mac/dot./: a name ends with a dot or a space, which Windows drops: Windows "
            "clients get nothing until it's renamed",
            "mac/inner is a symlink: every client gets nothing until it's removed",
            "debian/pipe is a special file: every client gets nothing until it's removed",
            "debian/\\xff.md isn't valid UTF-8: every client gets nothing until it's renamed"]))

    @unittest.skipIf(util.folds_case(), util.FOLDS_CASE)
    def test_a_failed_start_takes_the_warnings_as_a_baseline(self):
        # the start's scan failed (not a missing root): the first good round shows what's
        # there, files, entries and warnings alike, and none of it is a change
        write_tree(self.tree, {"debian/STEPS.md": b"s", "Debian/": None, "mac/x": b"x"})
        self.post("mac", 1, "old", to="@debian")
        real = watch.scan
        calls = []

        def scan(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(13, "Permission denied")
            return real(*a, **kw)

        with mock.patch.object(watch, "scan", scan):
            code, lines = self.run_dir(lambda: None, lambda: None, fresh=True,
                                       until_change=True, max_minutes=25)
        # rounds ran out: no EXIT change on the baseline round, nor for the "ok again" of an
        # error that never counted
        self.assertEqual(code, 0)
        self.assertEqual(lines, [
            "ERROR can't read %s: Permission denied" % self.tree,
            watching(self.tree, 0, ", fresh start"),
            "WARN Debian/ at the top isn't a writer's folder: clients leave it out",
            "ok again"])

    def test_a_clean_tree_warns_nothing(self):
        write_tree(self.tree, {"debian/STEPS.md": b"s", "mac/x": b"x", "debian/mac/a": b"a",
                               "mac/Mac/b": b"b"})
        self.assertEqual(watch.warnings(self.tree, "debian"), [])
        _code, lines = self.run_dir(lambda: None)
        self.assertEqual(lines, [watching(self.tree, 2)])

    def test_once_while_it_lasts_then_cleared(self):
        write_tree(self.tree, {"debian/STEPS.md": b"s"})

        def stray():
            write_tree(self.tree, {"Mac/CASE.txt": b"c"})

        def removed():
            shutil.rmtree(os.path.join(self.tree, "Mac"))

        _code, lines = self.run_dir(stray, lambda: None, removed, lambda: None)
        text = "Mac/ at the top isn't a writer's folder: clients leave it out"
        self.assertEqual(lines, [watching(self.tree, 0), "new Mac/CASE.txt",
                                 "WARN " + text, "gone Mac/CASE.txt", "WARN cleared: " + text])

    @unittest.skipIf(util.folds_case(), util.FOLDS_CASE)
    def test_until_change_wakes_on_a_new_warning(self):
        write_tree(self.tree, {"debian/STEPS.md": b"s"})

        def twin():
            # an empty folder: no new, changed or gone line, only the WARN
            write_tree(self.tree, {"Debian/": None})

        code, lines = self.run_dir(lambda: None, twin, rounds=None, until_change=True,
                                   max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [watching(self.tree, 0),
                                 "WARN Debian/ at the top isn't a writer's folder: clients "
                                 "leave it out", "EXIT change (exit 0)"])

    @unittest.skipIf(util.folds_case(), util.FOLDS_CASE)
    def test_a_restart_shows_them_again_but_isnt_a_change(self):
        write_tree(self.tree, {"debian/STEPS.md": b"s", "Debian/": None})
        text = "Debian/ at the top isn't a writer's folder: clients leave it out"
        # a fresh start shows what's there already, as its file count
        code, lines = self.run_dir(rounds=0)
        self.assertEqual(lines, [watching(self.tree, 0), "WARN " + text])
        with open(self.state(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["warnings"], [text])
        saved = self.saved_at(self.state())
        # a restart shows it again, and its first round doesn't count it as a change
        clock = Clock()
        lines = []
        code = watch.watch_dir(self.tree, "debian", 10, out=lines.append,
                               sleep=Rounds(*[lambda: None] * 6, clock=clock), clock=clock,
                               timer=clock, until_change=True, max_minutes=1)
        lines = self.said(lines)
        self.assertEqual(lines, [watching(self.tree, 0, ", since " + saved),
                                 "WARN " + text, "EXIT quiet 1 min (exit 10)"])
        self.assertEqual(code, 10)
        # gone while no watcher ran: the next start shows it, its first round clears it
        os.rmdir(os.path.join(self.tree, "Debian"))
        code, lines = self.run_dir(rounds=1)
        self.assertEqual(lines[1:], ["WARN " + text, "WARN cleared: " + text])

    def test_client_mode_warns_nothing(self):
        sync_args = ClientModeTest.write_config(self)

        def run(job, sync_args):
            write_tree(self.tree, {"debian/Notes.md": b"N", "Debian/x": b"x", "notes.md": b"n"})
            return 0, None, None

        watch.watch_job("mb.windows", sync_args, 30, out=self.lines.append, sleep=never, run=run,
                        rounds=1)
        self.assertFalse(any("WARN" in line for line in self.said()), self.lines)


class UntilChangeTest(WatchCase):
    def setUp(self):
        WatchCase.setUp(self)
        self.sync_args = ClientModeTest.write_config(self)

    def fake_runs(self, results):
        # the client's tree is there: a failed scan would count as an error too
        os.makedirs(self.tree, exist_ok=True)
        runs = []

        def run(job, sync_args):
            # (code, line, what it brings) and, if given, the fix line's text
            result = results.pop(0)
            code, line, spec = result[:3]
            runs.append(code)
            # what the run brings: files, or a call that posts entries
            spec() if callable(spec) else write_tree(self.tree, spec)
            return code, line, result[3] if len(result) > 3 else None

        return run, runs

    def saved_error(self, error, saved="then", counted=None):
        """A client snapshot saved by a watcher that showed error last, and woke for it."""
        path = self.state(me="windows", job="mb.windows")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"version": 2, "root": self.tree, "me": "windows", "saved": saved, "files": {},
               "warnings": [], "seen": {}, "heads": {}, "loose": []}
        if error is not None:
            doc["error"] = error
            doc["counted"] = [watch.error_key(error)] if counted is None else counted
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return path

    def snapshot(self):
        with open(self.state(me="windows", job="mb.windows"), encoding="utf-8") as f:
            return json.load(f)

    def until_change(self, results, rounds=None, max_errors=10):
        """A client watch with --until-change over the fake runs: (exit code, the lines
        without the watching line, the runs made)."""
        run, runs = self.fake_runs(results)
        lines = []
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=lines.append,
                               sleep=Rounds(*[lambda: None] * (len(results) - 1)), run=run,
                               until_change=True, max_minutes=25, max_errors=max_errors,
                               rounds=rounds)
        return code, self.said(lines)[1:], runs

    def never_counted(self, results, **kw):
        """A client watch whose start's scan fails with T, an error that then never counts:
        the fake runs fail with T (results' None) or as given. (exit code, lines, runs)."""
        os.makedirs(self.tree, exist_ok=True)
        t = "ERROR can't read %s: Permission denied" % self.tree
        real = watch.scan
        calls = []

        def scan(root, me, fold=False):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(13, "Permission denied")
            return real(root, me, fold)

        run, runs = self.fake_runs([(1, t, {}) if r is None else r for r in results])
        with mock.patch.object(watch, "scan", scan):
            code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                                   sleep=Rounds(*[lambda: None] * (len(results) - 1)), run=run,
                                   until_change=True, max_minutes=25, fresh=True, **kw)
        return code, self.said(), runs, t

    def test_a_long_block_never_ends_with_error(self):
        # the agent was woken for it: its rounds don't feed --max-errors
        d = "ERROR collision: debian/N.md and debian/n.md are the same path on Windows"
        self.saved_error(d)
        code, lines, runs = self.until_change([(1, d, {})] * 12, rounds=12, max_errors=3)
        self.assertEqual((code, lines), (0, [d]))
        # an uncounted blip inside it still counts toward the limit, and isn't reset by d
        lost = "ERROR lost: the connection closed"
        code, lines, runs = self.until_change([(1, lost, {}), (1, d, {})] * 3, max_errors=3)
        self.assertEqual((code, lines, runs), (11, [lost, d, lost, d, lost, "EXIT error (exit 11)"],
                                               [1] * 5))

    def test_busy_is_neither_error_nor_change(self):
        lost = "ERROR lost: the connection closed"
        busy = "ERROR busy: another run of mailbox.up is in progress"
        self.saved_error(lost)
        run, _runs = self.fake_runs([(1, lost, {}), (2, busy, {}), (2, busy, {}),
                                    (2, busy, lambda: self.post("debian", 2, "steps",
                                                                to="@all", file="STEPS.md"))])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=Rounds(*[lambda: None] * 3), run=run, until_change=True,
                               max_minutes=25, max_errors=2)
        # two busy rounds after an error: no line, no error count, no "ok again"; the scan
        # still runs, and what the other run brought is a change
        self.assertEqual(code, 0)
        self.assertEqual(self.said(), [watching(self.tree, 0, ", since then"), lost,
                                       "to all: debian#2 — steps  (debian/STEPS.md)",
                                       "EXIT change (exit 0)"])
        # a busy first round after a restart keeps the saved error, and the next shows it
        self.lines = []
        os.remove(os.path.join(self.tree, "debian", "STEPS.md"))
        self.saved_error(lost)
        run, _runs = self.fake_runs([(2, busy, {}), (1, lost, {})])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=Rounds(lambda: None), run=run, until_change=True,
                               max_minutes=25, rounds=2)
        self.assertEqual(code, 0)
        self.assertEqual(self.said()[1:], [lost])

    def test_busy_doesnt_reset_the_error_count(self):
        busy = "ERROR busy: another run of mailbox.up is in progress"
        code, lines, runs, t = self.never_counted([None, (2, busy, {}), None], max_errors=2)
        self.assertEqual(code, 11)
        self.assertEqual(runs, [1, 2, 1])
        self.assertEqual(lines, [t, watching(self.tree, 0, ", fresh start"),
                                 "EXIT error (exit 11)"])

    def test_a_new_error_is_a_change(self):
        # a blocked down printed its ERROR and the watcher kept going
        collision = ("ERROR collision: debian/twin/Notes.md and debian/twin/notes.md are the "
                     "same path on Windows")
        run, runs = self.fake_runs([(0, None, {}), (1, collision, {}), (1, collision, {})])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=Rounds(*[lambda: None] * 2), run=run, until_change=True,
                               max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(runs, [0, 1])
        self.assertEqual(self.said(), [watching(self.tree, 0), collision,
                                       "EXIT change (exit 0)"])
        path = self.state(me="windows", job="mb.windows")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["error"], collision)
        # restarted, the same error: shown again, not a change; it keeps running
        self.lines = []
        run, runs = self.fake_runs([(1, collision, {})] * 2
                                   + [(1, collision, lambda: self.post("debian", 2, "a",
                                                                       to="@windows"))])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=Rounds(*[lambda: None] * 2), run=run, until_change=True,
                               max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(runs, [1, 1, 1])
        self.assertEqual(self.said()[1:], [collision,
                                           "to you: debian#2 — a  (debian/RESULTS.md)",
                                           "EXIT change (exit 0)"])
        # restarted again, still blocked: the saved text is still the error
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["error"], collision)
        # a different error after a restart is new: shown and counted
        self.lines = []
        other = "ERROR unsafe_path: debian/lnk: a symlink"
        run, runs = self.fake_runs([(1, other, {})])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=never, run=run, until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said()[1:], [other, "EXIT change (exit 0)"])
        # and the good round after it, from the saved error too: "ok again" is a change
        self.lines = []
        run, runs = self.fake_runs([(0, None, {})])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=never, run=run, until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said()[1:], ["ok again", "EXIT change (exit 0)"])
        # nothing pending: the keys aren't written, so an older watcher can read it
        self.assertEqual(sorted(self.snapshot()), ["files", "heads", "loose", "me", "root",
                                                   "saved", "seen", "version", "warnings"])
        # no error saved: a good round says nothing
        self.lines = []
        run, runs = self.fake_runs([(0, None, {})])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=never, run=run, until_change=True, max_minutes=25,
                               rounds=1)
        self.assertEqual(code, 0)
        self.assertEqual(self.said()[1:], [])

    def test_a_new_error_is_a_change_in_server_mode(self):
        self.member()
        write_tree(self.tree, {"mac/x": b"x"})
        real = watch.scan
        fail = [False]

        def scan(root, me, fold=False):
            if fail[0]:
                raise PermissionError(13, "Permission denied")
            return real(root, me, fold)

        def broken():
            fail[0] = True

        error = "ERROR can't read %s: Permission denied" % self.tree
        clock = Clock()
        with mock.patch.object(watch, "scan", scan):
            code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                                   sleep=Rounds(broken, clock=clock), clock=clock, timer=clock,
                                   until_change=True, max_minutes=25)
            self.assertEqual(code, 0)
            self.assertEqual(self.said(), [watching(self.tree, 1), error,
                                           "EXIT change (exit 0)"])
            # the failed scan saved the error, but not the files' time
            with open(self.state(), encoding="utf-8") as f:
                doc = json.load(f)
            self.assertEqual(doc["error"], error)
            self.assertEqual(sorted(doc["files"]), ["mac/x"])
            self.assertEqual(doc["saved"], "2026-10-01 09:05:46")
            # restarted, still failing: shown again, and never an EXIT error, since it woke
            # the agent already
            again = []
            code = watch.watch_dir(self.tree, "debian", 10, out=again.append,
                                   sleep=Rounds(*[lambda: None] * 4, clock=clock), clock=clock,
                                   timer=clock, until_change=True, max_minutes=25,
                                   max_errors=2, rounds=5)
            self.assertEqual(code, 0)
            self.assertEqual(self.said(again)[1:], [error])
            self.assertEqual(self.saved_at(self.state()), doc["saved"])
            fail[0] = False
            back = []
            code = watch.watch_dir(self.tree, "debian", 10, out=back.append, sleep=never,
                                   until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said(back), [watching(self.tree, 1, ", since " + doc["saved"]),
                                           "ok again", "EXIT change (exit 0)"])

    def test_a_fresh_starts_error_isnt_printed_again(self):
        self.member()
        write_tree(self.tree, {"mac/x": b"x"})

        def scan(root, me, fold=False):
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(watch, "scan", scan):
            code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                                   sleep=Rounds(lambda: None, lambda: None), fresh=True,
                                   until_change=True, max_minutes=25, max_errors=2)
        # a baseline: shown once, never a change; the error count ends it
        self.assertEqual(code, 11)
        self.assertEqual(self.said(), ["ERROR can't read %s: Permission denied" % self.tree,
                                       watching(self.tree, 0, ", fresh start"),
                                       "EXIT error (exit 11)"])

    def test_a_failed_first_save_then_a_failed_scan(self):
        self.member()
        # no saved time yet when the scan fails: the save uses the round's
        write_tree(self.tree, {"mac/x": b"x"})
        real = watch.save_snapshot
        calls = []
        fail = [False]

        def save(*args):
            calls.append(1)
            if len(calls) == 1:
                raise OSError(28, "No space left on device")
            return real(*args)

        real_scan = watch.scan

        def scan(root, me, fold=False):
            if fail[0]:
                raise PermissionError(13, "Permission denied")
            return real_scan(root, me, fold)

        def broken():
            fail[0] = True

        clock = Clock()
        with mock.patch.object(watch, "save_snapshot", save), \
                mock.patch.object(watch, "scan", scan):
            watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                            sleep=Rounds(broken, clock=clock), clock=clock, rounds=1)
        self.assertEqual(len(calls), 2)
        with open(self.state(), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["saved"], "2026-10-01 09:05:56")
        self.assertEqual(sorted(doc["files"]), ["mac/x"])

    def test_continuous_mode_shows_the_saved_error_again(self):
        lost = "ERROR lost: the connection closed"
        self.saved_error(lost)
        run, _runs = self.fake_runs([(1, lost, {}), (1, lost, {}),
                                    (4, "ERROR connect: x", {}), (0, None, {})])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=Rounds(*[lambda: None] * 3), run=run, rounds=4)
        self.assertEqual(code, 0)
        self.assertEqual(self.said(), [watching(self.tree, 0, ", since then"), lost,
                                       "ERROR connect: x", "ok again"])

    def test_the_keys(self):
        for line, key in (
                ("ERROR connect: couldn't reach devbox", "transport"),
                ("ERROR lost: the connection closed", "transport"),
                ("ERROR timeout: no answer in 60 s", "transport"),
                ("ERROR vcharon sync of mailbox didn't finish within 900 s", "transport"),
                ("ERROR couldn't start vcharon: No such file", "transport"),
                ("ERROR too_many_deletes: the plan deletes 612 files and directories, more "
                 "than max_deletes (500)", "too_many_deletes"),
                ("ERROR unsafe_path: debian/a: a symlink (and 3 more; see the log)",
                 "ERROR unsafe_path: debian/a: a symlink"),
                ("ERROR collision: debian/N.md and debian/n.md are the same path on macOS",
                 "ERROR collision: debian/N.md and debian/n.md are the same path on macOS"),
                ("ERROR vcharon sync of mailbox exited with 9",
                 "ERROR vcharon sync of mailbox exited with 9"),
                ("ERROR lostness: x", "ERROR lostness: x"),
                ("ERROR vanished: debian/a.md changed while it was being listed", "vanished"),
                ("ERROR aborted: couldn't read file 12: x", "aborted"),
                # a sync names the failing job; the connection's errors and the retry and
                # too_many_deletes codes are keyed as without it, a content error keeps it
                ("ERROR mailbox.up: connect: couldn't reach devbox", "transport"),
                ("ERROR Mail-Box_9.up: connect: x", "transport"),
                ("ERROR mailbox.down: lost: the connection closed", "transport"),
                ("ERROR mb4c.up: timeout: no answer in 60 s", "transport"),
                ("ERROR my..box.down: too_many_deletes: the plan deletes 612 files and "
                 "directories, more than max_deletes (500)", "too_many_deletes"),
                ("ERROR mailbox.down: vanished: debian/a.md changed while it was being listed",
                 "vanished"),
                ("ERROR mailbox.up: aborted: couldn't read file 12: x", "aborted"),
                ("ERROR mailbox.up: unsafe_path: lnk: a symlink (and 3 more; see the log)",
                 "ERROR mailbox.up: unsafe_path: lnk: a symlink"),
                # a name that looks like a code is a path, in either form
                ("ERROR unsafe_path: timeout: a symlink", "ERROR unsafe_path: timeout: a symlink"),
                ("ERROR mailbox.up: unsafe_path: lost: a symlink",
                 "ERROR mailbox.up: unsafe_path: lost: a symlink"),
                # only a mailbox job's name (S.up, S.down) is read as one
                ("ERROR mailbox.side: lost: x", "ERROR mailbox.side: lost: x")):
            with self.subTest(line=line):
                self.assertEqual(watch.error_key(line), key)
        # the same content error in up and in down: two problems, two keys
        self.assertNotEqual(watch.error_key("ERROR mailbox.up: unsafe_path: a: a symlink"),
                            watch.error_key("ERROR mailbox.down: unsafe_path: a: a symlink"))

    def test_an_error_saved_before_the_job_names(self):
        # a snapshot saved by an older watcher: read as ever; its content error's new text
        # is another key, one extra wake, and then no more
        old = "ERROR collision: debian/N.md and debian/n.md are the same path on Windows"
        new = "ERROR mailbox.down: collision: debian/N.md and debian/n.md are the same path on " \
              "Windows"
        self.saved_error(old)
        code, lines, runs = self.until_change([(1, new, {}), (1, new, {})], rounds=2)
        self.assertEqual((code, lines, runs), (0, [new, "EXIT change (exit 0)"], [1]))
        code, lines, runs = self.until_change([(1, new, {}), (1, new, {})], rounds=2)
        self.assertEqual((code, lines, runs), (0, [new], [1, 1]))
        # a saved connection error: the same key, no extra wake
        lost = "ERROR lost: the connection closed"
        self.saved_error(lost)
        new_lost = "ERROR mailbox.up: lost: the connection closed"
        code, lines, runs = self.until_change([(1, new_lost, {}), (1, new_lost, {})], rounds=2)
        self.assertEqual((code, lines, runs), (0, [new_lost], [1, 1]))

    def test_a_network_blip_wakes_nobody(self):
        lost = "ERROR lost: the connection closed"
        # one failed round: printed, and its "ok again", but no exit
        code, lines, runs = self.until_change([(1, lost, {}), (0, None, {})], rounds=2)
        self.assertEqual((code, lines), (0, [lost, "ok again"]))
        self.assertNotIn("error", self.snapshot())
        # two in a row: the second wakes the agent, though its line was printed already
        code, lines, runs = self.until_change([(1, lost, {}), (1, lost, {}), (0, None, {})])
        self.assertEqual((code, lines, runs), (0, [lost, "EXIT change (exit 0)"], [1, 1]))
        self.assertEqual(self.snapshot()["counted"], ["transport"])
        # a blip saved by a watcher that stopped right after it: the outage still wakes once
        self.saved_error(lost, counted=[])
        code, lines, runs = self.until_change([(1, lost, {}), (1, lost, {}), (0, None, {})])
        self.assertEqual((code, lines, runs), (0, [lost, "EXIT change (exit 0)"], [1, 1]))

    def test_vanished_and_aborted_need_two_rounds(self):
        for first, second in (("ERROR vanished: debian/a.md changed while it was being listed",
                               "ERROR vanished: debian/b.md changed while it was being listed"),
                              ("ERROR aborted: couldn't read file 3: x",
                               "ERROR aborted: couldn't read file 7: x")):
            with self.subTest(first=first):
                self.lines = []
                code, lines, _runs = self.until_change([(1, first, {}), (0, None, {})],
                                                      rounds=2)
                self.assertEqual((code, lines), (0, [first, "ok again"]))
                code, lines, _runs = self.until_change([(1, first, {}), (1, second, {})])
                self.assertEqual((code, lines), (0, [first, second, "EXIT change (exit 0)"]))
                code, lines, _runs = self.until_change([(0, None, {})])
                self.assertEqual(lines, ["ok again", "EXIT change (exit 0)"])

    def test_a_blip_right_after_a_recovery(self):
        lost = "ERROR lost: the connection closed"
        w = watch._Watch(self.tree, "windows", self.lines.append, Clock())
        self.assertEqual([w.status(lost), w.status(lost), w.status(None)], [0, 1, 1])
        # the next streak starts afresh: its first round is a blip again
        self.assertEqual([w.status(lost), w.status(None)], [0, 0])

    def test_two_retry_keys_in_a_row_are_two_blips(self):
        w = watch._Watch(self.tree, "windows", self.lines.append, Clock())
        self.assertEqual([w.status("ERROR lost: x"), w.status("ERROR vanished: a changed"),
                          w.status("ERROR aborted: couldn't read file 1: x")], [0, 0, 0])

    def test_a_start_error_counts_once_it_has_cleared(self):
        t = "ERROR can't read x: Permission denied"
        w = watch._Watch(self.tree, "windows", self.lines.append, Clock())
        self.assertEqual([w.status(t, baseline=True), w.status(t), w.status(None)], [0, 0, 0])
        self.assertEqual(w.status(t), 1)

    def test_alternating_connection_errors_wake_once_per_streak(self):
        a = "ERROR connect: couldn't reach devbox"
        b = "ERROR lost: the connection closed"
        code, lines, _runs = self.until_change([(4, a, {}), (1, b, {}), (4, a, {})])
        self.assertEqual((code, lines), (0, [a, b, "EXIT change (exit 0)"]))
        # restarted, still flapping: every new text printed, none a change
        code, lines, _runs = self.until_change([(4, a, {}), (1, b, {}), (4, a, {}), (1, b, {})],
                                              max_errors=2, rounds=4)
        self.assertEqual((code, lines), (0, [a, b, a, b]))
        # the next good round ends the streak, and wakes
        code, lines, _runs = self.until_change([(0, None, {})])
        self.assertEqual((code, lines), (0, ["ok again", "EXIT change (exit 0)"]))

    def test_a_blip_inside_a_blocked_down_wakes_nobody(self):
        d = "ERROR collision: debian/N.md and debian/n.md are the same path on Windows"
        lost = "ERROR lost: the connection closed"
        code, lines, _runs = self.until_change([(1, d, {})])
        self.assertEqual((code, lines), (0, [d, "EXIT change (exit 0)"]))
        code, lines, _runs = self.until_change([(1, d, {}), (1, lost, {}), (1, d, {}),
                                               (0, None, {})])
        self.assertEqual((code, lines), (0, [d, lost, d, "ok again", "EXIT change (exit 0)"]))

    def test_a_growing_count_wakes_once(self):
        one = "ERROR unsafe_path: debian/a: a symlink (and 1 more; see the log)"
        two = "ERROR unsafe_path: debian/a: a symlink (and 2 more; see the log)"
        code, lines, _runs = self.until_change([(1, one, {}), (1, two, {}), (1, two, {})],
                                              rounds=3)
        self.assertEqual((code, lines), (0, [one, "EXIT change (exit 0)"]))
        code, lines, _runs = self.until_change([(1, one, {}), (1, two, {})], rounds=2)
        self.assertEqual((code, lines), (0, [one, two]))

    def test_a_b_a_counts_each_once(self):
        # up's error can hide down's (the watcher reads vcharon's first stderr line)
        a = "ERROR unsafe_path: lnk: a symlink"
        b = "ERROR collision: debian/N.md and debian/n.md are the same path on Windows"
        self.assertEqual(self.until_change([(1, a, {})])[:2], (0, [a, "EXIT change (exit 0)"]))
        self.assertEqual(self.until_change([(1, a, {}), (1, b, {})])[:2],
                         (0, [a, b, "EXIT change (exit 0)"]))
        self.assertEqual(sorted(self.snapshot()["counted"]), [b, a])
        self.assertEqual(self.until_change([(1, b, {}), (1, a, {}), (1, b, {})], rounds=3)[:2],
                         (0, [b, a, b]))

    def test_client_mode_saves_a_failed_scans_error(self):
        os.makedirs(self.tree, exist_ok=True)
        real = watch.scan

        def scan(root, me, fold=False):
            raise PermissionError(13, "Permission denied")

        error = "ERROR can't read %s: Permission denied" % self.tree
        with mock.patch.object(watch, "scan", real):
            watch.watch_job("mb.windows", self.sync_args, 30, out=[].append, sleep=never,
                            run=lambda job, sync_args: (0, None, None), rounds=1)
        with mock.patch.object(watch, "scan", scan):
            code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                                   sleep=never, run=lambda job, sync_args: (0, None, None),
                                   until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said()[1:], [error, "EXIT change (exit 0)"])
        self.assertEqual(self.snapshot()["error"], error)

    def test_a_busy_round_whose_scan_fails(self):
        os.makedirs(self.tree, exist_ok=True)
        busy = "ERROR busy: another run of mailbox.up is in progress"
        calls = []

        def scan(root, me, fold=False):
            # the start's scan works, the round's doesn't
            calls.append(1)
            if len(calls) > 1:
                raise PermissionError(13, "Permission denied")
            return {}

        with mock.patch.object(watch, "scan", scan):
            code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                                   sleep=never, run=lambda job, sync_args: (2, busy, None),
                                   until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said()[1:], ["ERROR can't read %s: Permission denied"
                                           % self.tree, "EXIT change (exit 0)"])

    def test_a_failed_save_ends_it(self):
        self.member()
        write_tree(self.tree, {"mac/x": b"x"})
        real = watch.save_snapshot
        full = [False]

        def save(*args):
            if full[0]:
                raise OSError(28, "No space left on device")
            return real(*args)

        def made():
            full[0] = True
            write_tree(self.tree, {"mac/y": b"y"})

        line = "ERROR can't save the snapshot %s: No space left on device" % self.state()
        with mock.patch.object(watch, "save_snapshot", save):
            code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                                   sleep=Rounds(made), until_change=True, max_minutes=25)
            # the next start prints mac/y again, since it wasn't saved
            again = []
            code2 = watch.watch_dir(self.tree, "debian", 10, out=again.append,
                                    sleep=never, until_change=True, max_minutes=25)
            # with nothing new too: a quiet round that couldn't save ends it the same way
            quiet = []
            code3 = watch.watch_dir(self.tree, "debian", 10, out=quiet.append,
                                    sleep=Rounds(lambda: None), fresh=True, until_change=True,
                                    max_minutes=25)
        # exit 0 would have the agent restart it, and print mac/y again, for ever
        self.assertEqual(code, 11)
        self.assertEqual(self.said(), [watching(self.tree, 1), "new mac/y", line,
                                       "EXIT error (exit 11)"])
        self.assertEqual(code2, 11)
        self.assertEqual(self.said(again)[1:], ["new mac/y", line, "EXIT error (exit 11)"])
        self.assertEqual(code3, 11)
        self.assertEqual(self.said(quiet), [watching(self.tree, 2, ", fresh start"),
                                            line, "EXIT error (exit 11)"])

    def test_changes_pending_at_the_start(self):
        self.member()
        write_tree(self.tree, {"mac/x": b"x"})
        watch.watch_dir(self.tree, "debian", 10, out=[].append, sleep=never, rounds=0)
        write_tree(self.tree, {"mac/y": b"y"})
        self.post("mac", 1, "y", to="@debian")
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=never,
                               until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said()[1:], ["to you: mac#1 — y  (mac/RESULTS.md)", "new mac/y",
                                           "EXIT change (exit 0)"])

    def test_max_minutes_waits_for_the_round(self):
        # continuous mode: a run that takes the clock past the limit finishes its round first
        clock = Clock()
        runs = []

        def run(job, sync_args):
            runs.append(job)
            clock.t += 70
            self.post("debian", 2, "steps", to="@all", file="STEPS.md")
            return 0, None, None

        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append, sleep=never,
                               run=run, clock=clock, timer=clock, max_minutes=1)
        self.assertEqual(code, 10)
        self.assertEqual(runs, ["mb.windows"])
        self.assertEqual(self.said(), [watching(self.tree, 0),
                                       "to all: debian#2 — steps  (debian/STEPS.md)",
                                       "EXIT quiet 1 min (exit 10)"])
        path = self.state(me="windows", job="mb.windows")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)["files"]), ["debian/STEPS.md"])


# a channel section, in channels.d/ next to vcharon.ini: the member windows of the channel
# mb, led by debian
MAILBOX = """
[mb.windows]
ssh            = devbox
mailbox.me     = windows
mailbox.leader = debian
mailbox.local  = {local}
mailbox.remote = ~/.local/state/vcharon/channels/mb
"""


class ClientModeTest(WatchCase):
    def test_a_member_left_out_by_the_pull(self):
        # --no-stream: the same WARN, from the file the sync's down saved
        note = charter.skipped_note("debian", "2.0 kB in 3 files, 1.0 kB over the limit of "
                                    "1.0 kB and 10 files")
        steps = [[note], [note.replace("2.0 kB in 3", "3.0 kB in 4")], []]
        path = charter.left_out_path("mb.windows")
        seen = []

        def run(job, sync_args):
            charter.save_left_out("mb.windows", steps.pop(0), 1000, 10)
            if os.path.exists(path):
                st = os.stat(path)
                seen.append((st.st_ino, st.st_mtime_ns))
            return 0, None, None

        watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                        sleep=Rounds(*[lambda: None] * 2), run=run, rounds=3)
        warn = ("left out debian/: over the channel's limit of 1.0 kB and 10 files; this box's "
                "copy of it stays as it was until it is back under")
        # one warning while the folder grows, cleared once the pull takes it again
        self.assertEqual(self.said(), [watching(self.tree, 0), "WARN " + warn,
                                       "WARN cleared: " + warn])
        self.assertFalse(os.path.exists(charter.left_out_path("mb.windows")))
        # the file holds names only: not written again while the folder grows
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    @staticmethod
    def write_config(case):
        """vcharon.ini, channels.d/mb.windows.ini and windows's join record, a remote member
        of the project p; the client's tree starts empty (server mode's debian/MEMBER.md goes:
        a test of both modes writes it again). Returns what follows `vcharon sync` for it."""
        os.remove(os.path.join(case.tree, "debian", "MEMBER.md"))
        os.rmdir(os.path.join(case.tree, "debian"))
        os.makedirs(case.vcharon_home, exist_ok=True)
        with open(os.path.join(case.vcharon_home, "vcharon.ini"), "w", encoding="utf-8") as f:
            f.write("[vcharon]\n")
        folder = os.path.join(case.vcharon_home, "channels.d")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "mb.windows.ini"), "w", encoding="utf-8") as f:
            f.write(MAILBOX.format(local=case.tree))
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": "windows",
                                  "leader": "debian", "ssh": "devbox",
                                  "remote": "~/.local/state/vcharon/channels/mb",
                                  "machine": util.TEST_MACHINE_ID, "project": "p",
                                  "role": None, **util.record_format()})
        return list(SYNC)

    def setUp(self):
        WatchCase.setUp(self)
        self.sync_args = self.write_config(self)

    def test_rounds(self):
        # a fake sync: each round's result, and what it brings
        def steps():
            self.post("debian", 2, "steps", to="@all", file="STEPS.md")
            # the own folder: never told
            self.post("windows", 2, "mine", to="@debian")

        results = [
            (0, None, steps),
            (1, "ERROR lost: the connection closed", {}),
            (1, "ERROR lost: the connection closed",
             lambda: self.post("debian", 3, "answers", to="@windows", file="ANSWERS.md")),
            (4, "ERROR connect: couldn't reach devbox", {}),
            (0, None, lambda: self.post("windows", 3, "more", to="@debian")),
            (0, None, {}),
        ]
        runs = []

        def run(job, sync_args):
            runs.append((job, sync_args))
            code, line, spec = results.pop(0)
            spec() if callable(spec) else write_tree(self.tree, spec)
            return code, line, None

        sleep = Rounds(*[lambda: None] * 5)
        watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append, sleep=sleep,
                        run=run, rounds=6)
        self.assertEqual(self.said(), [watching(self.tree, 0),
                                       "to all: debian#2 — steps  (debian/STEPS.md)",
                                       "ERROR lost: the connection closed",
                                       "to you: debian#3 — answers  (debian/ANSWERS.md)",
                                       "ERROR connect: couldn't reach devbox",
                                       "ok again"])
        self.assertEqual(runs, [("mb.windows", self.sync_args)] * 6)
        # no sleep before the first run
        self.assertEqual(sleep.seconds, [30] * 5)

    def test_not_a_mailbox(self):
        with self.assertRaises(VCharonError) as cm:
            watch.watch_job("nope", self.sync_args, 30, out=self.lines.append, rounds=0)
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ("config", "%s has no channel section [nope]"
                          % os.path.join(self.vcharon_home, "channels.d")))

    def test_a_fake_vcharon_command(self):
        # run_sync runs self_argv() sync <args>, as a child; here self_argv runs a folder with
        # a __main__.py. What its stderr's lines mean is parse_failure's (StreamTest)
        fake = os.path.join(self.tmp, "fake-vcharon")
        os.mkdir(fake)
        with open(os.path.join(fake, "__main__.py"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""
                import sys
                print("some output")
                assert sys.argv[1] == "sync", sys.argv
                if sys.argv[2] == "gone":
                    # the sync's block for a closed channel, as measured, after a
                    # line that isn't an ERROR
                    sys.stderr.write("note: x\\nERROR mb.w.up: not_found: no root\\n"
                                     "  fix: the channel is closed: leave it\\n  log: l\\n")
                    sys.exit(1)
                assert sys.argv[2:] == ["mb", "--project", "p"], sys.argv
                """))
        with mock.patch.object(platform, "self_argv", lambda: [sys.executable, fake]):
            self.assertEqual(watch.sync_argv(SYNC), [sys.executable, fake, "sync"] + SYNC)
            self.assertEqual(watch.sync_argv(SYNC, repeat=3),
                             [sys.executable, fake, "sync"] + SYNC + ["--repeat", "3"])
            self.assertEqual(watch.run_sync("mb.windows", SYNC), (0, None, None))
            self.assertEqual(watch.run_sync("gone", ["gone"]),
                             (1, "ERROR mb.w.up: not_found: no root",
                              "the channel is closed: leave it\nl"))

    def test_the_child_is_this_vcharon(self):
        # --no-stream's child each round: self_argv, -P and all; a binary's unpacks its own copy;
        # through fsops.run, in a session of its own, so a timeout or Ctrl-C ends its whole
        # group, with SIGTERM first so a binary's bootloader removes its unpack folder
        ran = []

        def run(argv, timeout, new_session=False, env=None, term_wait=0):
            self.assertEqual((timeout, new_session, term_wait),
                             (watch.RUN_TIMEOUT, True, watch.TERM_WAIT))
            ran.append((argv, env))
            return fsops.Ran(0, b"", b"")

        with mock.patch.object(watch.fsops, "run", run):
            self.assertEqual(watch.run_sync("mb.windows", SYNC), (0, None, None))
            binary = os.path.join(self.tmp, "vcharon")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "_MEIPASS", self.tmp, create=True), \
                    mock.patch.object(sys, "executable", binary):
                self.assertEqual(watch.run_sync("mb.windows", SYNC), (0, None, None))
        (argv, env), (frozen_argv, frozen_env) = ran
        self.assertEqual(argv, [sys.executable, "-P", "-m", "vcharon", "sync"] + SYNC)
        self.assertNotIn("PYINSTALLER_RESET_ENVIRONMENT", env)
        self.assertEqual(env["VCHARON_HOME"], self.vcharon_home)
        self.assertEqual(frozen_argv, [binary, "sync"] + SYNC)
        self.assertEqual(frozen_env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        # not a watcher's child: its pull stamps nothing
        self.assertNotIn(watch.WATCH_ENV, env)

    def test_a_watchers_child_carries_its_pace(self):
        # --no-stream: each round's sync is marked with the watcher's pace, so its down plan
        # stamps the member's last-watched time at the server
        envs = []

        def run(argv, timeout, new_session=False, env=None, term_wait=0):
            envs.append(env)
            return fsops.Ran(0, b"", b"")

        with mock.patch.object(watch.fsops, "run", run):
            watch.watch_job("mb.windows", self.sync_args, 45, out=self.lines.append,
                            sleep=Rounds(lambda: None), rounds=2)
        self.assertEqual([env[watch.WATCH_ENV] for env in envs], ["run 45"] * 2)

    def test_a_stuck_vcharon_run(self):
        # fsops.run's rc None: its timeout killed the run
        stuck = fsops.Ran(None, b"", b"ERROR half a line")
        with mock.patch.object(watch.fsops, "run", return_value=stuck):
            self.assertEqual(watch.run_sync("mailbox", ["mb"]),
                             (1, "ERROR vcharon sync of mailbox didn't finish within 900 s",
                              None))

    def test_a_stuck_vcharon_run_is_killed_with_what_it_started(self):
        # past the timeout, the sync and the processes it started end: in a binary those are
        # the bootloader and its Python process, which would run on holding the job's locks
        fake = os.path.join(self.tmp, "stuck-vcharon")
        os.mkdir(fake)
        pidfile = os.path.join(self.tmp, "grandchild.pid")
        with open(os.path.join(fake, "__main__.py"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""
                import os, subprocess, sys, time
                if os.name != "nt":
                    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                         stdin=subprocess.DEVNULL)
                    with open(%r, "w") as f:
                        f.write(str(g.pid))
                time.sleep(30)
                """ % pidfile))
        with mock.patch.object(platform, "self_argv", lambda: [sys.executable, fake]), \
                mock.patch.object(watch, "RUN_TIMEOUT", 3):
            self.assertEqual(watch.run_sync("mb.windows", SYNC),
                             (1, "ERROR vcharon sync of mb.windows didn't finish within 3 s",
                              None))
        if os.name != "nt":
            with open(pidfile, encoding="utf-8") as f:
                pid = int(f.read())
            self.addCleanup(end_pid, pid)
            self.assertTrue(gone(pid), "the sync's own child still runs")

    def test_a_vcharon_run_that_cant_start(self):
        # --no-stream: a sync that can't be started is that round's error, not a crash
        missing = FileNotFoundError(2, "No such file or directory")
        with mock.patch.object(watch.fsops, "run", side_effect=missing):
            self.assertEqual(watch.run_sync("mailbox", ["mb"]),
                             (1, "ERROR couldn't start vcharon: No such file or directory",
                              None))


# A stand-in for the streaming child, `vcharon sync C --repeat <every>`: it does the
# actions in its argument, in order, then waits for the end of its stdin and exits 0, as vcharon
# sync --repeat does. out/err: a line on stdout/stderr; raw: hex bytes on stdout; append: text
# at the end of a file; exit: exit with that code now; deaf: ignore the end of stdin for 60 s;
# pause: sleep that many seconds (stdout and stderr are two pipes: a pause orders them).
FAKE_CHILD = """
import json, sys, time
for act in json.loads(sys.argv[1]):
    kind = act[0]
    if kind == "out":
        sys.stdout.write(act[1] + "\\n")
        sys.stdout.flush()
    elif kind == "err":
        sys.stderr.write(act[1] + "\\n")
        sys.stderr.flush()
    elif kind == "raw":
        sys.stdout.buffer.write(bytes.fromhex(act[1]))
        sys.stdout.flush()
    elif kind == "append":
        with open(act[1], "ab") as f:
            f.write(act[2].encode("utf-8"))
    elif kind == "exit":
        sys.exit(act[1])
    elif kind == "deaf":
        time.sleep(60)
    elif kind == "pause":
        time.sleep(act[1])
sys.stdin.buffer.read()
"""


# A child that leaves a grandchild (sleeping 30 s) holding its stdout, the grandchild's pid in
# argv[1]; "detached" in argv[2] puts it in a session of its own on POSIX, out of the child's
# process group. Then one round, and it ignores the end of its stdin.
GRANDCHILD = """
import os, subprocess, sys, time
kw = {"start_new_session": True} if sys.argv[2] == "detached" and os.name != "nt" else {}
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                     stdin=subprocess.DEVNULL, stdout=sys.stdout, **kw)
with open(sys.argv[1], "w") as f:
    f.write(str(g.pid))
sys.stdout.write("ROUND 0\\n")
sys.stdout.flush()
time.sleep(60)
"""


class StreamTest(WatchCase):
    """A streaming watch: one long-lived vcharon sync --repeat child, its rounds
    as steps, its restarts with the backoff, the wake rules in time, and the child stopped on
    every way out. The child is FAKE_CHILD."""

    def setUp(self):
        WatchCase.setUp(self)
        self.sync_args = ClientModeTest.write_config(self)
        os.makedirs(self.tree, exist_ok=True)
        self.spawned = []
        self.children = []
        self.slept = []

    def entry(self, folder, n, title, to="@windows", file="RESULTS.md"):
        """("append", path, text): an entry, as mailbox_post.py writes it, for a child to add."""
        path = os.path.join(self.tree, folder, file)
        # the folder now, the file only when the child appends: the start sees no file
        os.makedirs(os.path.dirname(path), exist_ok=True)
        head = "" if os.path.exists(path) else "# RESULTS\n"
        return ["append", path,
                head + entries.build("2026-10-01 09:00", folder, n, title, to.split())]

    def spawn(self, argv, env):
        self.spawned.append((argv, env))
        acts = self.children.pop(0)
        proc = subprocess.Popen([sys.executable, "-c", FAKE_CHILD, json.dumps(acts)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        self.addCleanup(self.reap, proc)
        self.procs.append(proc)
        return proc

    @staticmethod
    def reap(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()

    def watch(self, *children, clock=None, steps=None, **kw):
        """watch_job, syncing every 2 s, over the children: (exit code, the lines without
        their time). steps: with clock, the seconds each round moves the clock on. timer: the
        limits' clock, clock unless given."""
        self.children = [list(c) for c in children]
        self.procs = []
        clock = clock or Clock()
        timer = kw.pop("timer", clock)
        lines = []
        real = watch.Stream.next_round

        def next_round(stream, deadline=None):
            got = real(stream, deadline)
            if steps:
                clock.t += steps.pop(0)
            return got

        def sleep(seconds):
            self.slept.append(seconds)
            clock.t += seconds

        kw.setdefault("max_minutes", 25)
        with mock.patch.object(watch.Stream, "next_round", next_round):
            code = watch.watch_job("mb.windows", self.sync_args, 2, out=lines.append, sleep=sleep,
                                   clock=clock, timer=timer, stream=True, spawn=self.spawn,
                                   **kw)
        return code, self.said(lines)

    def assert_all_stopped(self):
        for proc in self.procs:
            self.assertIsNotNone(proc.poll(), "a child still runs")

    def test_a_real_streaming_watcher_imports_nothing_after_its_start(self):
        # the watcher in a child, its sync child FAKE_CHILD: rounds with an entry and an
        # error, then the child's exit 14 (DESIGN, "Running watchers")
        imported = os.path.join(self.tmp, "imported.json")
        acts = [["out", "ROUND 0"], self.entry("debian", 1, "hello"), ["out", "ROUND 0"],
                ["out", "ERROR mb.windows.down: lost: the connection closed"],
                ["out", "  fix: run it again"], ["out", "ROUND 1"], ["pause", 0.5],
                ["out", "ROUND 0"], ["pause", 0.5], ["exit", 14]]
        code = ("import json, sys\n"
                "from vcharon import cli, install\n"
                "from vcharon.mailbox import watch\n"
                "watch.sync_argv = lambda sync_args, repeat=None: [sys.executable, '-c', %r, %r]\n"
                "start = []\n"
                "init = install.Watchdog.__init__\n"
                "def watched(self, *args, **kwargs):\n"
                "    init(self, *args, **kwargs)\n"
                "    start.append(set(sys.modules))\n"
                "install.Watchdog.__init__ = watched\n"
                "code = cli.main(sys.argv[1:])\n"
                "with open(%r, 'w') as f:\n"
                "    json.dump(sorted(set(sys.modules) - start[0]), f)\n"
                "sys.exit(code)\n" % (FAKE_CHILD, json.dumps(acts), imported))
        child = subprocess.Popen([sys.executable, "-S", "-c", code, "watch", "mb", "--project",
                                  "p", "--fresh"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=no_site_env())
        try:
            out, err = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        lines = self.said(out.decode("utf-8").splitlines())
        self.assertEqual(child.returncode, 14, (lines, err))
        self.assertIn("to you: debian#1 — hello  (debian/RESULTS.md)", lines)
        self.assertEqual(lines[-1], "EXIT updated (exit 14)")
        with open(imported, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_a_child_that_exits_updated(self):
        # vcharon sync --repeat saw vcharon replaced under it: no restart, EXIT updated
        code, lines = self.watch([["out", "ROUND 0"], ["out", "EXIT updated (exit 14)"],
                                  ["exit", 14]],
                                 [["out", "ROUND 0"]])
        self.assertEqual((code, lines[-1]), (14, "EXIT updated (exit 14)"))
        self.assertNotIn("ERROR", " ".join(lines))
        self.assertEqual(len(self.spawned), 1)
        self.assert_all_stopped()

    def test_a_childs_exit_line_is_never_relayed(self):
        # a Windows child whose bootloader was killed from outside prints its EXIT orphaned,
        # and the parent reaps another code: the watcher's only EXIT line is its own last one
        code, lines = self.watch([["out", "ROUND 0"], ["out", "EXIT orphaned (exit 15)"],
                                  ["exit", 1]],
                                 [["out", "ROUND 0"]], until_change=True, rounds=2)
        self.assertFalse(any(l.startswith("EXIT orphaned") for l in lines), lines)
        self.assertEqual([l for l in lines if l.startswith("EXIT ")], lines[-1:], lines)
        # the round's error is a silent exit's, and the last line says the code returned
        self.assertIn("ERROR vcharon sync of mb.windows exited with 1", lines)
        self.assertTrue(lines[-1].endswith("(exit %d)" % code), (code, lines))
        self.assert_all_stopped()

    def test_a_member_left_out_by_the_pull(self):
        # the streaming sync prints no notes: the members its down left out reach the
        # watcher through the file the sync saves, a WARN each while it lasts
        path = charter.left_out_path("mb.windows")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = json.dumps({"limit": "1.0 kB and 10 files", "members": ["debian"]})
        warn = ("left out debian/: over the channel's limit of 1.0 kB and 10 files; this box's "
                "copy of it stays as it was until it is back under")
        # the child doesn't wait for the watcher's rounds: the WARN comes in round 1 or 2, and
        # once (its clearing is ClientModeTest's)
        code, lines = self.watch(
            [["out", "ROUND 0"], ["append", path, doc], ["out", "ROUND 0"], ["out", "ROUND 0"]],
            rounds=3)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [watching(self.tree, 0, ", syncing every 2 s"),
                                 "WARN " + warn])
        os.remove(path)
        # it counts as a change once
        self.children = []
        code, lines = self.watch([["append", path, doc], ["out", "ROUND 0"]],
                                 until_change=True, fresh=True)
        self.assertEqual((code, lines[-2:]), (0, ["WARN " + warn, "EXIT change (exit 0)"]))

    def test_rounds_from_one_child(self):
        up = "ERROR mb.windows.up: unsafe_path: lnk: a symlink"
        code, lines = self.watch(
            [["out", "ROUND 0"], self.entry("debian", 2, "steps", to="@all"), ["out", "ROUND 0"],
             ["out", up], ["out", "  fix: remove or rename it"], ["out", "  log: l"],
             ["out", "ROUND 1"], ["out", "ROUND 0"]],
            rounds=4)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [watching(self.tree, 0, ", syncing every 2 s"),
                                 "to all: debian#2 — steps  (debian/RESULTS.md)",
                                 up, "  fix: remove or rename it", "  log: l", "ok again"])
        # one child, as vcharon sync C --project P --repeat <every>, its prints in UTF-8
        self.assertEqual(len(self.spawned), 1)
        argv, env = self.spawned[0]
        self.assertEqual(argv, [sys.executable, "-P", "-m", "vcharon", "sync", "mb",
                                "--project", "p", "--repeat", "2"])
        self.assertEqual((env["PYTHONIOENCODING"], env["PYTHONUTF8"]), ("utf-8", "1"))
        self.assertEqual(env["VCHARON_HOME"], self.vcharon_home)
        self.assertNotIn("PYINSTALLER_RESET_ENVIRONMENT", env)
        # the watcher's pace: the child's down plans stamp the member's last-watched time
        self.assertEqual(env[watch.WATCH_ENV], "stream 2")
        self.assertEqual(self.slept, [])
        self.assert_all_stopped()

    def test_the_leaders_closed(self):
        # streaming: the leave command right after the leader's CLOSED to @all, and the
        # watch runs on until it is stopped
        code, lines = self.watch(
            [["out", "ROUND 0"], self.entry("debian", 2, "CLOSED", to="@all"),
             ["out", "ROUND 0"], ["out", "ROUND 0"]], rounds=3)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [
            watching(self.tree, 0, ", syncing every 2 s"),
            "to all: debian#2 — CLOSED  (debian/RESULTS.md)",
            platform.runnable("  next: the leader closed the channel: stop your watcher and "
                              "don't start it again, then run: vcharon leave mb --project p")])
        self.assert_all_stopped()

    def test_a_binarys_child(self):
        # a PyInstaller binary starts itself, and its child unpacks its own copy (it outlives
        # the parent's unpacked folder)
        binary = os.path.join(self.tmp, "vcharon")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", self.tmp, create=True), \
                mock.patch.object(sys, "executable", binary):
            code, _lines = self.watch([["out", "ROUND 0"]], rounds=1)
        self.assertEqual(code, 0)
        argv, env = self.spawned[0]
        self.assertEqual(argv, [binary, "sync", "mb", "--project", "p", "--repeat", "2"])
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_utf8_lines(self):
        line = "ERROR mb.windows.down: unsafe_path: mac/中文: x"
        _code, lines = self.watch(
            [["raw", (line + "\n").encode("utf-8").hex()],
             ["raw", b"ERROR mb.windows.down: lost: \xff\r\n".hex()], ["out", "ROUND 1"],
             ["out", "ROUND 0"]], rounds=2)
        self.assertEqual(lines[1:], [line, "ok again"])
        _code, lines = self.watch(
            [["raw", b"ERROR mb.windows.down: lost: \xff\r\n".hex()], ["out", "ROUND 1"]],
            rounds=1, fresh=True)
        self.assertEqual(lines[1:], ["ERROR mb.windows.down: lost: �"])

    def test_the_child_restarts_with_the_backoff(self):
        connect = "ERROR mb.windows.up: connect: ssh couldn't reach devbox"
        broken = [["out", connect], ["out", "ROUND 4"], ["exit", 4]]
        # a child that says something on stderr after such a round crashed on its way out:
        # that is a round of its own, with its line
        # (the pause orders the two pipes)
        loud = [["out", connect], ["out", "ROUND 4"], ["pause", 0.5],
                ["err", "Fatal Python error: x"], ["exit", 134]]
        children = ([[["err", "ERROR config: no such file"], ["exit", 3]], loud] + [broken] * 4
                    + [[["out", "ROUND 0"], ["exit", 1]], [["out", "ROUND 0"], ["exit", 0]]])
        code, lines = self.watch(*children, rounds=10)
        self.assertEqual(code, 0)
        # the exit before any round is a step of its own, with stderr's line; a silent exit
        # with the code of the round before isn't: the round said why; an exit after a good
        # round is (a crash)
        self.assertEqual(lines[1:], ["ERROR config: no such file", connect,
                                     "Fatal Python error: x", connect, "ok again",
                                     "ERROR vcharon sync of mb.windows exited with 1",
                                     "  fix: " + silent_fix(), "ok again"])
        self.assertEqual(self.slept, [2, 4, 8, 16, 30, 30, 2])
        self.assertEqual(len(self.spawned), 8)
        self.assert_all_stopped()

    def test_a_round_cut_off(self):
        lost = "ERROR mb.windows.down: lost: the connection closed"
        _code, lines = self.watch(
            [["out", "ROUND 0"], ["out", lost], ["out", "  fix: run again"], ["exit", 1]],
            [["out", "ROUND 0"]], rounds=3)
        self.assertEqual(lines[1:], [lost, "  fix: run again", "ok again"])
        self.assertEqual(self.slept, [2])

    def test_a_child_is_stuck_after_run_timeout_and_one_wait(self):
        # the limit itself: a line can take RUN_TIMEOUT plus one wait between rounds
        limit = watch.RUN_TIMEOUT + 2
        times = iter([0, limit - 0.1, limit])
        lines = mock.Mock(get=mock.Mock(side_effect=queue.Empty))
        fake = types.SimpleNamespace(_lines=lines, timer=lambda: next(times), every=2)
        self.assertIs(watch.Stream._next_line(fake), watch._STUCK)
        self.assertEqual(list(times), [])

    def test_a_silent_child_is_stopped_and_started_again(self):
        # no round within 900 s plus --every (DESIGN, "The watcher in a channel"): the child
        # is stuck, so it is stopped, the round is an error, and a new child starts after the
        # first wait of the backoff. The timer jumps 451 s a reading while the first child
        # runs, so the limit passes in two of _next_line's 0.5 s waits, not 900 real seconds.
        clock = Clock()
        readings = []

        def timer():
            readings.append(clock.t)
            if len(readings) > 20:
                raise AssertionError("the silent child was never stopped")
            if len(self.spawned) == 1:
                clock.t += 451
            return clock.t

        code, lines = self.watch([], [["out", "ROUND 0"]], clock=clock, rounds=2,
                                 max_minutes=None, timer=timer)
        self.assertEqual((code, lines[1:]), (0, [
            "ERROR vcharon sync of mb.windows didn't finish within 900 s", "ok again"]))
        self.assertEqual(self.slept, [2])
        self.assertEqual(len(self.spawned), 2)
        self.assert_all_stopped()
        # stopped by the end of its stdin, as on every way out, not killed
        self.assertEqual(self.procs[0].returncode, 0)

    def test_a_child_that_cant_start(self):
        # spawn's OSError is that round's error line; the next start waits the backoff
        def spawn(argv, env):
            if not self.spawned:
                self.spawned.append((argv, env))
                raise FileNotFoundError(2, "No such file or directory")
            return StreamTest.spawn(self, argv, env)

        self.spawn = spawn
        code, lines = self.watch([["out", "ROUND 0"]], rounds=2)
        self.assertEqual((code, lines[1:]), (0, [
            "ERROR couldn't start vcharon: No such file or directory", "ok again"]))
        self.assertEqual(self.slept, [2])
        self.assertEqual(len(self.procs), 1)
        self.assert_all_stopped()

    def test_max_minutes_during_the_wait(self):
        connect = "ERROR mb.windows.up: connect: no"
        clock = Clock()
        # the first round takes the clock to 50 s before the limit; the restart's wait of
        # 2 s fits, the second's 4 s doesn't
        code, lines = self.watch([["out", connect], ["out", "ROUND 4"], ["exit", 4]],
                                 [["out", connect], ["out", "ROUND 4"], ["exit", 4]],
                                 clock=clock, steps=[10, 45], max_minutes=1)
        self.assertEqual((code, lines[1:]),
                         (watch.EXIT_QUIET, [connect, "EXIT quiet 1 min (exit 10)"]))
        self.assertEqual(self.slept, [2, 3])
        self.assertEqual(len(self.spawned), 2)
        self.assert_all_stopped()

    def test_a_blip_under_60_s_wakes_nobody(self):
        lost = "ERROR mb.windows.up: lost: the connection closed"
        down = [["out", lost], ["out", "ROUND 1"]]
        # three failed rounds 20 s apart: held 40 s, not 60; ok again counts nothing
        code, lines = self.watch(down + down + down + [["out", "ROUND 0"]], clock=Clock(),
                                 steps=[20, 20, 20, 20], until_change=True, rounds=4)
        self.assertEqual((code, lines[1:]), (0, [lost, "ok again"]))
        # held 60 s in a row: it counts, once
        code, lines = self.watch(down * 4, clock=Clock(), steps=[30, 30, 30, 30],
                                 until_change=True)
        self.assertEqual((code, lines[1:]), (watch.EXIT_CHANGE, [lost, "EXIT change (exit 0)"]))
        self.assert_all_stopped()
        # --no-stream keeps the round count: its second round counts
        self.assertEqual(watch._Watch(self.tree, "windows", [].append, Clock()).hold, None)

    def test_300_s_of_failing_that_woke_nobody(self):
        t = "ERROR can't read %s: Permission denied" % self.tree

        def scan(root, me, fold=False):
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(watch, "scan", scan):
            code, lines = self.watch([["out", "ROUND 0"]] * 5, clock=Clock(),
                                     steps=[100] * 5, until_change=True, fresh=True)
        # each failed round adds its time, from the end of the round before: 300 s at the third
        self.assertEqual((code, lines), (watch.EXIT_ERROR, [
            t, watching(self.tree, 0, ", fresh start, syncing every 2 s"), "EXIT error (exit 11)"]))
        self.assert_all_stopped()

    def test_exit_closed_from_a_streamed_round(self):
        error = "ERROR mb.windows.up: not_found: the root x doesn't exist"
        gone = channel_cmd.CHANNEL_GONE_PREFIX + "vcharon leave mb --role w"
        code, lines = self.watch([["out", "ROUND 0"], ["out", error], ["out", "  fix: " + gone],
                                  ["out", "  log: l"], ["out", "ROUND 1"], ["out", "ROUND 0"]])
        self.assertEqual((code, lines[1:]), (watch.EXIT_CLOSED, [
            error, "  fix: " + gone, "  log: l", "EXIT closed (exit 13)"]))
        self.assert_all_stopped()

    def test_the_child_is_stopped_on_every_way_out(self):
        # EXIT change, quiet and closed above; here a change, a Ctrl-C, a crash, and a child
        # that ignores the end of its stdin
        code, lines = self.watch([self.entry("debian", 2, "hi"), ["out", "ROUND 0"]],
                                 until_change=True)
        self.assertEqual((code, lines[-1]), (watch.EXIT_CHANGE, "EXIT change (exit 0)"))
        self.assert_all_stopped()
        for error in (KeyboardInterrupt, RuntimeError):
            with self.subTest(error=error):
                def out(line, error=error):
                    if "to you:" in line:
                        raise error()

                self.children = [[self.entry("debian", 3 + len(self.spawned), "hi"),
                                  ["out", "ROUND 0"]]]
                self.procs = []
                with self.assertRaises(error):
                    watch.watch_job("mb.windows", self.sync_args, 2, out=out, sleep=never,
                                    stream=True, spawn=self.spawn)
                self.assert_all_stopped()
        started = time.monotonic()
        code, lines = self.watch([["out", "ROUND 0"], self.entry("debian", 9, "hi"),
                                  ["out", "ROUND 0"], ["deaf"]], until_change=True,
                                 stop_wait=0.2)
        self.assertEqual((code, lines[-1]), (watch.EXIT_CHANGE, "EXIT change (exit 0)"))
        self.assert_all_stopped()
        self.assertNotEqual(self.procs[0].returncode, 0)
        self.assertLess(time.monotonic() - started, 30)

    def test_a_mixed_round_that_broke(self):
        # up's content error first, then down's lost: the round broke the
        # connection, so the child's exit is no step of its own (no "exited with" key)
        up = "ERROR mb.windows.up: unsafe_path: lnk: a symlink"
        lost = "ERROR mb.windows.down: lost: the connection closed"
        mixed = [["out", up], ["out", "  fix: remove or rename it"], ["out", lost],
                 ["out", "ROUND 1"], ["exit", 1]]
        code, lines = self.watch(mixed, mixed, rounds=2)
        self.assertEqual(lines[1:], [up, "  fix: remove or rename it"])
        self.assertEqual(self.slept, [2])
        self.assert_all_stopped()
        # with --until-change: the up error was told before; the blip wakes nobody
        UntilChangeTest.saved_error(self, up)
        code, lines = self.watch(mixed, mixed, until_change=True, rounds=2)
        self.assertEqual((code, lines[1:]), (0, [up, "  fix: remove or rename it"]))

    def test_a_child_that_exits_with_its_rounds_code(self):
        # vcharon sync --repeat exits with the round's code after any round that left its
        # session unusable, an internal error too: by design, no line of its own
        internal = "ERROR mb.windows.down: internal: TypeError: boom"
        fix = "  fix: this is a bug in vcharon; tell your user"
        _code, lines = self.watch([["out", internal], ["out", fix], ["out", "ROUND 1"],
                                   ["exit", 1]], [["out", "ROUND 0"]], rounds=2)
        self.assertEqual(lines[1:], [internal, fix, "ok again"])
        self.assertEqual(self.slept, [2])
        # another code after a round whose error broke the connection is a crash: told
        connect = "ERROR mb.windows.up: connect: no route"
        # (the second child's exit 0 is a third round where the crash goes untold)
        _code, lines = self.watch([["out", connect], ["out", "ROUND 4"], ["exit", 9]],
                                  [["out", "ROUND 0"], ["exit", 0]], rounds=3, fresh=True)
        self.assertEqual(lines[1:], [connect, "ERROR vcharon sync of mb.windows exited with 9",
                                     "  fix: " + silent_fix(), "ok again"])
        self.assert_all_stopped()

    def test_a_crash_with_its_rounds_code_is_told(self):
        # Python's uncaught exception exits 1, as a round with a content error does: stderr
        # after that round tells the crash, a round of its own with its line (the pause orders
        # the two pipes)
        up = "ERROR mb.windows.up: unsafe_path: lnk: a symlink"
        fix = "  fix: remove or rename it"
        crash = "Traceback (most recent call last):"
        _code, lines = self.watch([["out", up], ["out", fix], ["out", "ROUND 1"],
                                   ["pause", 0.5], ["err", crash], ["exit", 1]],
                                  [["out", "ROUND 0"]], rounds=2)
        self.assertEqual((lines[1:], len(self.spawned)), ([up, fix, crash], 1))
        self.assert_all_stopped()

    def test_an_exit_0_after_a_good_round_is_a_round(self):
        # vcharon sync --repeat never ends by itself after a good round: its exit is a round of
        # its own (a good one, by its code), not a silent restart
        _code, lines = self.watch([["out", "ROUND 0"], ["exit", 0]], [["out", "ROUND 0"]],
                                  rounds=2)
        self.assertEqual((lines[1:], self.slept, len(self.spawned)), ([], [], 1))
        self.assert_all_stopped()

    def test_a_grandchild_holding_the_pipes_doesnt_hold_stop(self):
        # a child that ignores the end of its stdin and leaves a process holding its stdout,
        # as a binary's bootloader leaves its Python process: stop ends it all on POSIX (its
        # process group), and returns on every OS
        for detached in (False, True):
            with self.subTest(detached=detached):
                pidfile = os.path.join(self.tmp, "grandchild-%d.pid" % detached)
                stream = watch.Stream("mb.windows", self.sync_args, 2, stop_wait=0.2)
                stream.argv = [sys.executable, "-c", GRANDCHILD, pidfile,
                               "detached" if detached else "group"]
                self.addCleanup(self.close_stream, stream)
                self.assertEqual(stream.next_round(), (0, None, None))
                with open(pidfile, encoding="utf-8") as f:
                    pid = int(f.read())
                self.addCleanup(end_pid, pid)
                started = time.monotonic()
                stream.stop()
                self.assertLess(time.monotonic() - started, 0.2 + watch.TERM_WAIT + 2 + 5)
                if watch.GROUP and not detached:
                    self.assertTrue(gone(pid), "the child's own child still runs")

    @staticmethod
    def close_stream(stream):
        # after end_pid (cleanups run last first): the readers have their end of input
        for t, pipe in stream._threads:
            t.join(10)
            pipe.close()

    def test_a_note_before_the_first_round_is_no_crash_line(self):
        # stderr from before round 1 is never a later crash's error line
        _code, lines = self.watch(
            [["err", "note: skipped channels.d/x.ini"], ["pause", 0.5], ["out", "ROUND 0"],
             ["exit", 1]],
            [["out", "ROUND 0"]], rounds=3)
        self.assertEqual(lines[1:], ["ERROR vcharon sync of mb.windows exited with 1",
                                     "  fix: " + silent_fix(), "ok again"])

    def test_a_crash_after_a_good_round_is_a_round(self):
        _code, lines = self.watch(
            [["out", "ROUND 0"], ["pause", 0.5], ["err", "Traceback (most recent call last):"],
             ["err", "RuntimeError: x"], ["exit", 1]],
            [["out", "ROUND 0"]], rounds=3)
        self.assertEqual(lines[1:], ["Traceback (most recent call last):", "ok again"])
        self.assertEqual(self.slept, [2])

    def test_told_rounds_dont_add_to_the_300_s(self):
        # a short blip, then an error that woke the agent before (told) for 6 minutes, then the
        # blip again: 20 s of failing that woke nobody, no EXIT error
        lost = "ERROR mb.windows.up: lost: the connection closed"
        d = "ERROR mb.windows.down: collision: debian/N.md and debian/n.md are one name"
        UntilChangeTest.saved_error(self, d)
        down = [["out", lost], ["out", "ROUND 1"]]
        told = [["out", d], ["out", "ROUND 1"]]
        code, lines = self.watch(down + told + told + down, clock=Clock(),
                                 steps=[10, 180, 180, 10], until_change=True, rounds=4)
        self.assertEqual((code, lines[1:]), (0, [lost, d, lost]))
        self.assert_all_stopped()

    def test_max_minutes_cuts_a_round_off(self):
        # a round that doesn't come (a dying link, a hung helper): the watch ends on time
        class Ticking(Clock):
            def __call__(self):
                self.t += 30
                return self.t

        lines = []
        self.children = [[]]
        self.procs = []
        code = watch.watch_job("mb.windows", self.sync_args, 2, out=lines.append, sleep=never,
                               clock=Clock(), timer=Ticking(), stream=True, spawn=self.spawn,
                               max_minutes=1)
        self.assertEqual((code, self.said(lines)[1:]),
                         (watch.EXIT_QUIET, ["EXIT quiet 1 min (exit 10)"]))
        self.assert_all_stopped()

    def test_parse_failure_is_run_vcharons(self):
        lines = ["ERROR mb.windows.up: connect: no", "  | ssh: refused", "  fix: check",
                 "  log: l", "ERROR other"]
        busy = "ERROR busy: another run of busy.up is in progress"
        for code, got, want in (
                # the connection's last words come between the ERROR line and its fix
                (4, lines, (4, "ERROR mb.windows.up: connect: no", "check\nl")),
                (0, lines, (0, None, None)),
                # nothing said (killed from outside): the logs to look at
                (2, [], (2, "ERROR vcharon sync of mb.windows exited with 2", silent_fix())),
                (1, ["", "ERROR lost: gone", "  fix: run again"],
                 (1, "ERROR lost: gone", "run again")),
                # the first ERROR's block holds no fix: its log, never a later line's fix
                (1, ["ERROR lost: gone", "  log: l", "ERROR other: x", "  fix: other's"],
                 (1, "ERROR lost: gone", "the job's log has the rest: l")),
                (2, [busy], (2, busy, None)),
                # no ERROR line: the first that isn't blank
                (1, ["", "Traceback (most recent call last):", "RuntimeError: x"],
                 (1, "Traceback (most recent call last):", None))):
            with self.subTest(got=got):
                self.assertEqual(watch.parse_failure(code, got, "mb.windows"), want)


class EntriesTest(WatchCase):
    """A channel's entries ("The watcher in a channel"). Server mode, as the
    member mac of the channel mb, led by debian (setUp's debian/MEMBER.md)."""

    def setUp(self):
        WatchCase.setUp(self)
        self.member("mac", "debian")
        self.member("windows", "debian")

    def run_mac(self, *steps, rounds=None, **kw):
        lines = []
        code = watch.watch_dir(self.tree, "mac", 10, out=lines.append,
                               sleep=Rounds(*steps) if steps else never,
                               rounds=len(steps) if rounds is None else rounds, **kw)
        return code, self.said(lines)

    def snapshot(self):
        with open(self.state(me="mac"), encoding="utf-8") as f:
            return json.load(f)

    def test_to_you_and_to_all_in_time_order(self):
        def posted():
            self.post("debian", 2, "the steps", to="@all", file="STEPS.md",
                      when="2026-10-01 09:10")
            self.post("windows", 2, "for mac", to="@mac @debian", when="2026-10-01 09:05")
            self.post("windows", 3, "for debian", to="@debian", when="2026-10-01 09:06")
            self.post("windows", 4, "everyone", to="@all", when="2026-10-01 09:07")

        code, lines = self.run_mac(posted, until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [
            watching(self.tree, 2),
            "to you: windows#2 — for mac  (windows/RESULTS.md)",
            "to all: debian#2 — the steps  (debian/STEPS.md)",
            "1 other entry (windows)",
            "note: @all from windows, not the leader: ignored",
            "EXIT change (exit 0)"])
        # each one once
        self.assertEqual(self.run_mac(lambda: None)[1][1:], [])

    def test_what_doesnt_count(self):
        # entries to others, without an ID, or with another member's ID, and a member's
        # @all: shown, summed, and none wakes --until-change
        def posted():
            self.post("windows", 2, "for debian", to="@debian")
            self.post("windows", 3, "everyone", to="@all")
            self.post("debian", 2, "for windows", to="@windows")
            with open(os.path.join(self.tree, "windows", "NOTES.md"), "ab") as f:
                f.write("# NOTES\n\n## 2026-10-01 09:00 — hand-written\n\n"
                        "## 2026-10-01 09:01 — debian#9 — copied\nto: @mac\n".encode())

        code, lines = self.run_mac(posted, lambda: None, until_change=True, max_minutes=25)
        # the rounds ran out: no EXIT
        self.assertEqual(code, 0)
        self.assertEqual(lines, [
            watching(self.tree, 2),
            "WARN entry debian#9 in windows/: not its folder's",
            "4 other entries (debian, windows)",
            "note: @all from windows, not the leader: ignored"])
        # told once, also after a restart
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])
        self.assertEqual(len(self.snapshot()["loose"]), 2)

    def test_a_join_wakes_the_leader_once(self):
        # MEMBER.md's #1 is addressed to the leader by design; only the JOIN wakes it
        def joined():
            self.member("linux", "debian")
            self.post("linux", 2, "JOIN", to="@debian")

        lines = []
        code = watch.watch_dir(self.tree, "debian", 10, out=lines.append,
                               sleep=Rounds(joined), rounds=1, until_change=True,
                               max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said(lines), [
            watching(self.tree, 2),
            "to you: linux#2 — JOIN  (linux/RESULTS.md)",
            "1 other entry (linux)",
            "EXIT change (exit 0)"])

    def test_the_leader_is_the_records(self):
        # a server member's leader is its record's (vcharon join --local wrote it), not
        # MEMBER.md's, nor the holder of CHANNEL.md
        from vcharon import channel_cmd
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": "mac",
                                  "leader": "windows", "ssh": None, "remote": self.tree,
                                  "machine": util.TEST_MACHINE_ID, "project": "p",
                                  "role": None, **util.record_format()})
        write_tree(self.tree, {"debian/CHANNEL.md": b"# CHANNEL\n"})

        def posted():
            self.post("debian", 2, "from debian", to="@all")
            self.post("windows", 2, "from windows", to="@all")

        _code, lines = self.run_mac(posted)
        self.assertEqual(lines[1:], ["to all: windows#2 — from windows  (windows/RESULTS.md)",
                                     "note: @all from debian, not the leader: ignored"])

    def test_the_client_leader_is_the_sections(self):
        sync_args = ClientModeTest.write_config(self)
        os.remove(os.path.join(self.tree, "mac", "MEMBER.md"))

        def run(job, sync_args):
            self.post("debian", 2, "from debian", to="@all")
            self.post("mac", 2, "from mac", to="@all")
            return 0, None, None

        watch.watch_job("mb.windows", sync_args, 30, out=self.lines.append, sleep=never, run=run,
                        rounds=1)
        self.assertEqual(self.said()[1:], ["to all: debian#2 — from debian  (debian/RESULTS.md)",
                                           "note: @all from mac, not the leader: ignored"])

    def test_a_later_entry_can_come_a_round_first(self):
        # two files, and one run moves them in name order: #3 can come a round before #2
        def three():
            self.post("windows", 3, "three", to="@mac", file="B.md")

        def two():
            self.post("windows", 2, "two", to="@mac", file="A.md")

        _code, lines = self.run_mac(three, two, lambda: None)
        self.assertEqual(lines[1:], ["to you: windows#3 — three  (windows/B.md)",
                                     "to you: windows#2 — two  (windows/A.md)"])
        self.assertEqual(self.snapshot()["seen"]["windows"], {"low": 3, "more": []})
        # half way: what's seen is kept as low and more
        marks = watch.Marks({"windows": {"low": 1, "more": [3]}})
        self.assertEqual([marks.has("windows", n) for n in (1, 2, 3, 4)],
                         [True, False, True, False])
        marks.add("windows", 2)
        self.assertEqual(marks.doc()["seen"], {"windows": {"low": 3, "more": []}})

    def test_a_restart_tells_each_entry_once(self):
        self.run_mac(rounds=0)
        # while no watcher ran
        self.post("windows", 2, "two", to="@mac", when="2026-10-01 09:01")
        self.post("debian", 2, "steps", to="@all", file="STEPS.md", when="2026-10-01 09:02")
        _code, lines = self.run_mac(rounds=1)
        self.assertEqual(lines[1:], ["to you: windows#2 — two  (windows/RESULTS.md)",
                                     "to all: debian#2 — steps  (debian/STEPS.md)"])
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])
        # a file that changed with no new entry: nothing either
        with open(os.path.join(self.tree, "windows", "RESULTS.md"), "ab") as f:
            f.write(b"\na line under the last entry\n")
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])

    def test_an_edited_entry_is_warned_once(self):
        path = self.post("windows", 2, "two", to="@mac")
        self.run_mac(rounds=0)

        def edit(old, new):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new))

        code, lines = self.run_mac(lambda: edit("— two", "— 2"), lambda: None,
                                   until_change=True, max_minutes=25)
        self.assertEqual((code, lines[1:]), (0, ["WARN entry windows#2 was edited",
                                                 "EXIT change (exit 0)"]))
        # once: not again after a restart
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])
        # edited again while no watcher ran: told at the restart, once
        edit("— 2", "— II")
        self.assertEqual(self.run_mac(rounds=1)[1][1:], ["WARN entry windows#2 was edited"])
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])
        # the same ID twice in one file: the first stands, nothing is told
        self.post("windows", 2, "a copy", to="@mac")
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])

    def test_an_id_in_two_files_is_noted_once(self):
        # an entry copied into a file that sorts later, and retitled: the first in path order
        # stands, as in read; the copy is noted once, counts for nothing, is never "edited"
        self.post("windows", 2, "two", to="@mac", file="STEPS.md")
        self.run_mac(rounds=0)
        note = ("note: duplicate entry windows#2 in windows/WORK.md: the one in "
                "windows/STEPS.md stands")
        code, lines = self.run_mac(
            lambda: self.post("windows", 2, "two, retitled", to="@mac", file="WORK.md"),
            lambda: self.post("windows", 3, "three", to="@debian", file="STEPS.md"),
            lambda: self.post("windows", 4, "four", to="@debian", file="WORK.md"),
            rounds=4, until_change=True, max_minutes=25)
        self.assertEqual((code, lines[1:]), (0, [note, "1 other entry (windows)",
                                                 "1 other entry (windows)"]))
        self.assertEqual(self.snapshot()["heads"]["windows#2"][0], "windows/STEPS.md")

    def test_a_copy_in_a_file_that_sorts_first_stands(self):
        # read orders the copy first in path order, so the watcher checks that one: retitled,
        # it was edited, and the file seen first is now the duplicate
        self.post("windows", 2, "two", to="@mac", file="STEPS.md")
        self.run_mac(rounds=0)
        code, lines = self.run_mac(
            lambda: self.post("windows", 2, "two, retitled", to="@mac", file="A.md"),
            lambda: self.post("windows", 3, "three", to="@debian", file="STEPS.md"),
            rounds=3, until_change=True, max_minutes=25)
        self.assertEqual((code, lines[1:]), (0, [
            "WARN entry windows#2 was edited",
            "note: duplicate entry windows#2 in windows/STEPS.md: the one in windows/A.md "
            "stands", "EXIT change (exit 0)"]))
        self.assertEqual(self.snapshot()["heads"]["windows#2"][0], "windows/A.md")
        # as read has it
        _folders, items, _notes = read.read_tree(self.tree)
        # naive, as read's own clock is: headings carry local time with no zone
        ordered, _ = read.order(items, datetime.datetime(2026, 10, 2))  # noqa: DTZ001
        notes = [n for item in ordered for n in item.notes]
        self.assertIn("note: windows#2 again in windows/STEPS.md: the one in windows/A.md is "
                      "ordered", notes)

    def test_a_copy_takes_over_when_the_heads_file_is_gone(self):
        # the file seen first with the ID is deleted: the copy left is checked for edits, and
        # no note names the file that is gone
        self.post("windows", 2, "two", to="@mac", file="A.md")
        self.post("windows", 2, "two", to="@mac", file="STEPS.md")
        self.run_mac(rounds=0)
        self.assertEqual(self.snapshot()["heads"]["windows#2"][0], "windows/A.md")

        def retitle():
            path = os.path.join(self.tree, "windows", "STEPS.md")
            with open(path, "rb") as f:
                text = f.read()
            with open(path, "wb") as f:
                f.write(text.replace(b"\xe2\x80\x94 two", b"\xe2\x80\x94 2"))

        code, lines = self.run_mac(
            lambda: os.remove(os.path.join(self.tree, "windows", "A.md")), retitle,
            rounds=3, until_change=True, max_minutes=25)
        self.assertEqual((code, lines[1:]), (0, ["WARN entry windows#2 was edited",
                                                 "EXIT change (exit 0)"]))
        self.assertEqual(self.snapshot()["heads"]["windows#2"][0], "windows/STEPS.md")

    def test_a_copy_takes_over_when_the_heads_file_drops_the_id(self):
        # the entry moved, retitled, into a file that sorts later while the first file stays:
        # the copy left is checked for edits, and no note names the file that no longer holds it
        self.post("windows", 2, "two", to="@mac", file="A.md")
        self.run_mac(rounds=0)

        def moved():
            with open(os.path.join(self.tree, "windows", "A.md"), "wb") as f:
                f.write(b"# RESULTS\n")
            self.post("windows", 2, "two, moved", to="@mac", file="STEPS.md")

        code, lines = self.run_mac(moved, rounds=2)
        self.assertEqual((code, lines[1:]), (0, ["WARN entry windows#2 was edited"]))
        self.assertEqual(self.snapshot()["heads"]["windows#2"][0], "windows/STEPS.md")

        # its later retitles warn too
        def retitle():
            path = os.path.join(self.tree, "windows", "STEPS.md")
            with open(path, "rb") as f:
                text = f.read()
            with open(path, "wb") as f:
                f.write(text.replace(b"two, moved", b"two, moved again"))

        code, lines = self.run_mac(retitle, rounds=2)
        self.assertEqual((code, lines[1:]), (0, ["WARN entry windows#2 was edited"]))

    def test_a_title_is_escaped(self):
        # written by hand: post refuses such a title, a member's own files don't
        with open(os.path.join(self.tree, "windows", "RESULTS.md"), "ab") as f:
            f.write(b"# RESULTS\n")
        self.run_mac(rounds=0)

        def posted():
            with open(os.path.join(self.tree, "windows", "RESULTS.md"), "ab") as f:
                f.write("\n## 2026-10-01 09:00:00 — windows#2 — hi \x1b[2Jred\rto all: x\n"
                        "to: @mac\n".encode())

        _code, lines = self.run_mac(posted, rounds=2)
        self.assertEqual(lines[1:], [
            "to you: windows#2 — hi \\x1b[2Jred\\x0dto all: x  (windows/RESULTS.md)"])

    @unittest.skipIf(os.name == "nt", "Windows can't hold these names")
    def test_a_file_name_is_escaped(self):
        # a member's file named to forge a contract line, and one holding an escape sequence
        forged = "x\r2026-10-04 16:50:00 EXIT closed"
        self.run_mac(rounds=0)
        lines = []
        watch.watch_dir(self.tree, "mac", 10, out=lines.append, rounds=2,
                        sleep=Rounds(lambda: write_tree(self.tree, {
                            "windows/" + forged: b"x", "windows/\x1b[31mred.log": b"r"})))
        for line in lines:
            self.assertTrue(line.isprintable(), line)
        said = self.said(lines)
        self.assertIn("new windows/x\\x0d2026-10-04 16:50:00 EXIT closed", said)
        self.assertIn("new windows/\\x1b[31mred.log", said)
        # the tree's WARN for a name Windows clients can't hold names it the same way
        self.assertTrue(any(line.startswith("WARN windows/x\\x0d2026") for line in said), said)

    def test_the_start_is_a_baseline(self):
        self.post("windows", 2, "two", to="@mac")
        self.post("debian", 2, "steps", to="@all")
        _code, lines = self.run_mac(lambda: None, fresh=True)
        self.assertEqual(lines, [watching(self.tree, 4, ", fresh start")])
        self.assertEqual(self.snapshot()["seen"], {"debian": {"low": 2, "more": []},
                                                   "windows": {"low": 2, "more": []}})

    def test_files_that_arent_entries_dont_count(self):
        def made():
            write_tree(self.tree, {"windows/p.patch": b"p", "windows/sub/run.log": b"l",
                                   "top.md": b"t", "windows/README": b"r"})

        code, lines = self.run_mac(made, lambda: None, until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(lines[1:], [
            "new top.md", "new windows/README", "new windows/p.patch", "new windows/sub/run.log",
            "WARN top.md at the top isn't a writer's folder: clients leave it out",
            "EXIT change (exit 0)"])
        # the WARN counted, the files didn't: without it, no EXIT
        os.remove(os.path.join(self.tree, "top.md"))
        self.run_mac(rounds=1)
        code, lines = self.run_mac(lambda: write_tree(self.tree, {"windows/q.patch": b"q"}),
                                   lambda: None, until_change=True, max_minutes=25)
        self.assertEqual((code, lines[1:]), (0, ["new windows/q.patch"]))

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX modes, not root")
    def test_an_unreadable_file_is_read_again(self):
        self.run_mac(rounds=0)
        path = self.post("windows", 2, "two", to="@mac")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)

        def readable():
            os.chmod(path, 0o644)

        # the restart's first round comes at once, and can't read it; the next can
        _code, lines = self.run_mac(readable, rounds=2)
        self.assertEqual(lines[1:], ["to you: windows#2 — two  (windows/RESULTS.md)"])

    def test_server_mode_needs_member_md(self):
        os.remove(os.path.join(self.tree, "mac", "MEMBER.md"))
        write_tree(self.tree, {"mac/RESULTS.md": b"r", "solo/MEMBER.md/": None})
        for me in ("mac", "solo"):
            with self.subTest(me=me):
                member = os.path.join(self.tree, me, "MEMBER.md")
                with self.assertRaises(VCharonError) as cm:
                    watch.watch_dir(self.tree, me, 10, out=self.lines.append, sleep=never,
                                    rounds=0)
                self.assertEqual((cm.exception.code, cm.exception.message, cm.exception.hint), (
                    "channel", "%s isn't there: your folder in the channel holds it, once "
                    "vcharon join --local has written it" % member,
                    "ask the user: your folder in mb lost its MEMBER.md"))
        # no own folder at all: join refuses that record, so the fix is the leave, as post's
        with self.assertRaises(VCharonError) as cm:
            watch.watch_dir(self.tree, "nobody", 10, out=self.lines.append, sleep=never,
                            rounds=0)
        self.assertEqual((cm.exception.code, cm.exception.message, cm.exception.hint), (
            "not_found", "your folder %s in the channel is gone"
            % os.path.join(self.tree, "nobody"), watch.gone_fix(self.tree, "nobody")))
        self.assertIn("vcharon leave mb", cm.exception.hint)
        # refused before any line, lock or snapshot
        self.assertEqual(self.lines, [])
        self.assertFalse(os.path.exists(os.path.join(self.vcharon_home, "state")))
        # the command: exit 1, the ERROR and fix lines on stderr
        self.local_record("mac")
        code, out, err = self.cli()
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("ERROR channel: %s isn't there: "
                                       % os.path.join(self.tree, "mac", "MEMBER.md")), err)
        self.assertIn("\n  fix: ask the user: ", err)
        os.remove(channel_cmd.record_path("mb", "mac"))
        # a MEMBER.md without a leader, and no record: refused too
        write_tree(self.tree, {"mac/MEMBER.md": b"# MEMBER\n"})
        with self.assertRaises(VCharonError) as cm:
            watch.watch_dir(self.tree, "mac", 10, out=self.lines.append, sleep=never, rounds=0)
        self.assertEqual(cm.exception.message, "%s names no leader, and there's no record of "
                         "mac in mb" % os.path.join(self.tree, "mac", "MEMBER.md"))


class ClosingTest(WatchCase):
    """The leader's CLOSED to @all: right after its line, the leave command for this membership
    (vcharon guide end). Server mode, as the member mac of mb, led by debian."""

    def setUp(self):
        WatchCase.setUp(self)
        self.member("mac", "debian")
        self.member("windows", "debian")
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": "mac",
                                  "leader": "debian", "ssh": None, "remote": self.tree,
                                  "machine": util.TEST_MACHINE_ID, "project": "web",
                                  "role": "b", **util.record_format()})

    @staticmethod
    def next_line(flags):
        return platform.runnable("  next: the leader closed the channel: stop your watcher and "
                                 "don't start it again, then run: vcharon leave mb " + flags)

    def watch(self, me, posted, **kw):
        lines = []
        code = watch.watch_dir(self.tree, me, 10, out=lines.append, sleep=Rounds(posted),
                               rounds=1, **kw)
        return code, self.said(lines)[1:]

    def test_the_leaders_closed_to_all(self):
        def posted():
            self.post("debian", 2, "CLOSED", to="@all", when="2026-10-01 09:10")
            self.post("windows", 2, "later", to="@mac", when="2026-10-01 09:11")

        # one change, as before: the next line follows its entry's, in time order, and the
        # watcher ends as for any change (its exit code and EXIT line unchanged)
        self.assertEqual(self.watch("mac", posted, until_change=True, max_minutes=25), (
            watch.EXIT_CHANGE, ["to all: debian#2 — CLOSED  (debian/RESULTS.md)",
                                self.next_line("--project web --role b"),
                                "to you: windows#2 — later  (windows/RESULTS.md)",
                                "EXIT change (exit 0)"]))
        # told once: a restart from the snapshot prints neither again
        self.assertEqual(self.watch("mac", lambda: None), (0, []))
        # with @mac too it prints as to you, and is still the leader's end of the channel
        self.post("debian", 3, "CLOSED", to="@all @mac")
        self.assertEqual(self.watch("mac", lambda: None), (0, [
            "to you: debian#3 — CLOSED  (debian/RESULTS.md)",
            self.next_line("--project web --role b")]))

    def test_the_flags_come_from_the_record(self):
        # windows has no record here: the placeholder gone_fix uses too, never made-up flags
        self.assertEqual(self.watch("windows", lambda: self.post("debian", 2, "CLOSED",
                                                               to="@all"))[1],
                         ["to all: debian#2 — CLOSED  (debian/RESULTS.md)",
                          self.next_line("<the --project and --role that make windows>")])

    def test_no_next_line(self):
        cases = [
            # a member's @all is ignored: only the leader ends the channel
            ("windows", 2, "CLOSED", "@all",
             ["note: @all from windows, not the leader: ignored"]),
            ("windows", 3, "CLOSED", "@all @mac",
             ["to you: windows#3 — CLOSED  (windows/RESULTS.md)"]),
            # the leader's CLOSED to one member, and a member's to mac
            ("debian", 2, "CLOSED", "@mac", ["to you: debian#2 — CLOSED  (debian/RESULTS.md)"]),
            ("windows", 4, "CLOSED", "@mac",
             ["to you: windows#4 — CLOSED  (windows/RESULTS.md)"]),
            # the title whole and exact, but for the spaces around it
            ("debian", 3, "Closed", "@all", ["to all: debian#3 — Closed  (debian/RESULTS.md)"]),
            ("debian", 4, "CLOSED soon", "@all",
             ["to all: debian#4 — CLOSED soon  (debian/RESULTS.md)"]),
            ("debian", 5, "NOT CLOSED", "@all",
             ["to all: debian#5 — NOT CLOSED  (debian/RESULTS.md)"])]
        for folder, n, title, to, want in cases:
            with self.subTest(folder=folder, title=title, to=to):
                # fresh: the round comes after the post, not at once from the saved snapshot
                def posted(folder=folder, n=n, title=title, to=to):
                    self.post(folder, n, title, to=to)

                self.assertEqual(self.watch("mac", posted, fresh=True)[1], want)

    def test_spaces_around_the_title(self):
        def posted():
            # by hand: a heading with spaces around its title
            with open(os.path.join(self.tree, "debian", "RESULTS.md"), "ab") as f:
                f.write("# RESULTS\n\n## 2026-10-01 09:00 — debian#2 —  CLOSED \nto: @all\n"
                        .encode())

        self.assertEqual(self.watch("mac", posted)[1], [
            "to all: debian#2 —  CLOSED   (debian/RESULTS.md)",
            self.next_line("--project web --role b")])

    def test_not_for_the_leader(self):
        # the leader closes instead; its own folder, which holds its CLOSED, is never read
        self.assertIsNone(watch.closed_next("mb", "debian", "debian"))
        self.assertEqual(self.watch("debian", lambda: self.post("debian", 2, "CLOSED",
                                                              to="@all"))[1], [])

    def test_a_remote_member(self):
        # its flags from its record (no --server: leave reads it there), in every round of
        # a --no-stream watch as in a streaming one (StreamTest)
        sync_args = ClientModeTest.write_config(self)

        def run(job, sync_args):
            self.post("debian", 2, "CLOSED", to="@all")
            return 0, None, None

        watch.watch_job("mb.windows", sync_args, 30, out=self.lines.append, sleep=never,
                        run=run, rounds=1)
        self.assertEqual(self.said()[1:], ["to all: debian#2 — CLOSED  (debian/RESULTS.md)",
                                           self.next_line("--project p")])


class ClosedTest(WatchCase):
    """A closed channel says so in the watcher: the fix line after the ERROR
    line."""

    # the fix as the watcher prints it: the leave command as this box runs vcharon
    GONE = platform.runnable("the channel is closed, or your folder in it is gone: vcharon leave "
                             "mb --project web --role b")

    def record(self):
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": "debian",
                                  "leader": "debian", "ssh": None, "remote": self.tree,
                                  "machine": util.TEST_MACHINE_ID, "project": "web",
                                  "role": "b", **util.record_format()})

    def test_is_gone(self):
        # by the hint's start, after runnable() and run_vcharon's log part
        self.assertTrue(channel_cmd.CHANNEL_GONE_HINT.startswith(channel_cmd.CHANNEL_GONE_PREFIX))
        self.assertTrue(watch.is_gone(self.GONE))
        self.assertTrue(watch.is_gone(self.GONE + "\n/x/mb.windows.log"))
        self.assertTrue(watch.is_gone(channel_cmd.CHANNEL_GONE_HINT % ("mb", "--project p")))
        for fix in (None, "", "the channel is closed: leave it", "vcharon channel leave mb",
                    "x " + self.GONE):
            with self.subTest(fix=fix):
                self.assertFalse(watch.is_gone(fix))

    def test_dir_a_removed_channel_folder(self):
        write_tree(self.tree, {"mac/x": b"x"})
        why = []

        def closed():
            # close's rename: every member's next round sees the folder gone. Into a new
            # name in a fresh folder: on Windows os.rename can't replace a folder
            os.rename(self.tree, os.path.join(
                tempfile.mkdtemp(prefix=".vcharon-closed-mb-", dir=self.tmp), "mb"))
            # the OS's own message, from a real error on the same path: it's localized
            # (Chinese Windows says 系统找不到指定的路径。)
            try:
                os.scandir(self.tree).close()
            except OSError as e:
                why.append(e.strerror)

        # a member with no record: its fix has a placeholder for the flags
        nameless = platform.runnable("the channel is closed, or your folder in it is gone: "
                                     "vcharon leave mb <the --project and --role that make "
                                     "debian>")
        for record, until_change in ((False, False), (True, True), (True, False)):
            with self.subTest(record=record, until_change=until_change):
                if record:
                    self.record()
                if not os.path.isdir(self.tree):
                    write_tree(self.tree, {"mac/x": b"x", "debian/MEMBER.md":
                                           member_md("debian", "debian")})
                del self.lines[:]
                code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                                       sleep=Rounds(closed), until_change=until_change,
                                       max_minutes=25, fresh=True)
                self.assertTrue(why[-1])
                error = "ERROR can't read %s: %s" % (self.tree, why[-1])
                fix = self.GONE if record else nameless
                # the fix right after the ERROR line, then EXIT closed, in both modes
                self.assertEqual((code, self.said()), (
                    watch.EXIT_CLOSED, [watching(self.tree, 1, ", fresh start"), error,
                                        "  fix: " + fix, "EXIT closed (exit 13)"]))
                with open(self.state(), encoding="utf-8") as f:
                    doc = json.load(f)
                self.assertEqual((doc["error"], doc["fix"]), (error, fix))
        # a restart while it's gone: the same three lines, before any lock or snapshot
        del self.lines[:]
        os.remove(self.state())
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=never,
                               rounds=0)
        self.assertEqual((code, self.said()), (watch.EXIT_CLOSED, [
            error, "  fix: " + self.GONE, "EXIT closed (exit 13)"]))
        self.assertFalse(os.path.exists(self.state()))

    def test_dir_missing_at_the_start_through_the_command(self):
        self.record()
        shutil.rmtree(self.tree)
        for argv in ([], ["--until-change"]):
            with self.subTest(argv=argv):
                code, out, err = self.cli("--role", "b", *argv, project="web")
                lines = self.said(out.splitlines())
                self.assertEqual((code, err), (13, ""))
                self.assertEqual(lines[1:], ["  fix: " + self.GONE, "EXIT closed (exit 13)"])
                self.assertTrue(lines[0].startswith("ERROR can't read %s: " % self.tree), lines)
        # a channel folder that's there without <me>/: refused, exit 1, with the leave fix
        write_tree(self.tree, {"mac/x": b"x"})
        code, out, err = self.cli("--role", "b", project="web")
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), [
            "ERROR not_found: your folder %s in the channel is gone"
            % os.path.join(self.tree, "debian"), "  fix: " + self.GONE])
        # <me>/ there without its MEMBER.md: still refused, exit 1
        write_tree(self.tree, {"debian/x": b"x"})
        code, out, err = self.cli("--role", "b", project="web")
        self.assertEqual(code, 1)
        self.assertIn("isn't there", err)

    def job_watch(self, results, rounds, until_change, fresh=False):
        """watch_job on a fake sync: (exit code, the lines without their time but the
        watching line)."""
        def run(job, sync_args):
            return results.pop(0)

        lines = []
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=lines.append,
                               sleep=Rounds(*[lambda: None] * (rounds - 1)), run=run,
                               until_change=until_change, max_minutes=25, rounds=rounds,
                               fresh=fresh)
        return code, self.said(lines)[1:]

    def test_job_the_gone_fix_ends_it(self):
        self.sync_args = ClientModeTest.write_config(self)
        os.makedirs(self.tree, exist_ok=True)
        error = "ERROR mb.windows.up: not_found: the root x doesn't exist"
        fix = self.GONE + "\nl"
        for until_change in (True, False):
            with self.subTest(until_change=until_change):
                # a good round first; the gone round ends it, with rounds to spare
                results = [(0, None, None), (1, error, fix), (0, None, None)]
                self.assertEqual(self.job_watch(results, 3, until_change, fresh=True), (
                    watch.EXIT_CLOSED,
                    [error, "  fix: " + self.GONE, "  log: l", "EXIT closed (exit 13)"]))
                # a restart from the saved snapshot: shown again, and EXIT closed again,
                # though the saved error doesn't count again
                results = [(1, error, fix), (0, None, None)]
                self.assertEqual(self.job_watch(results, 2, until_change), (
                    watch.EXIT_CLOSED,
                    [error, "  fix: " + self.GONE, "  log: l", "EXIT closed (exit 13)"]))

    def test_job_another_fix_goes_on(self):
        self.sync_args = ClientModeTest.write_config(self)
        os.makedirs(self.tree, exist_ok=True)
        error = "ERROR mb.windows.up: not_found: the root x doesn't exist"
        for fix in ("restore the folder", None):
            with self.subTest(fix=fix):
                results = [(1, error, fix), (1, error, fix)]
                code, lines = self.job_watch(results, 2, False, fresh=True)
                self.assertEqual(code, 0)
                self.assertFalse(any(l.startswith("EXIT closed") for l in lines), lines)
                self.assertEqual(lines[0], error)

    def test_job_the_fix_shown_with_its_error(self):
        sync_args = ClientModeTest.write_config(self)
        os.makedirs(self.tree, exist_ok=True)
        error = "ERROR mb.windows.up: not_found: the root x doesn't exist"
        fix = "the channel is closed: vcharon leave mb --project web"
        results = [(1, error, {}, fix), (1, error, {}, fix), (1, error, {}, "another text")]

        def run(job, sync_args):
            code, line, _spec, why = results.pop(0)
            return code, line, why

        def watch_it(rounds, until_change=True):
            lines = []
            code = watch.watch_job("mb.windows", sync_args, 30, out=lines.append,
                                   sleep=Rounds(*[lambda: None] * (rounds - 1)), run=run,
                                   until_change=until_change, max_minutes=25, rounds=rounds)
            return code, self.said(lines)[1:]

        # once with its ERROR, which alone counts
        self.assertEqual(watch_it(1), (0, [error, "  fix: " + fix, "EXIT change (exit 0)"]))
        path = self.state(me="windows", job="mb.windows")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual((doc["error"], doc["fix"], doc["counted"]),
                         (error, fix, [watch.error_key(error)]))
        # a restart: both again, not a change; a new fix text with the same ERROR line is
        # neither shown nor counted
        self.assertEqual(watch_it(2), (0, [error, "  fix: " + fix]))
        # a good round: "ok again", and no fix kept
        results[:] = [(0, None, {}, None)]
        self.assertEqual(watch_it(1, until_change=False), (0, ["ok again"]))
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("fix", json.load(f))

class CommandTest(WatchCase):
    """vcharon watch C: its options, the membership it watches, its exit codes."""

    def setUp(self):
        WatchCase.setUp(self)
        # debian, a local member of the project q; windows, a remote one of p
        self.local_record()
        self.client = ClientModeTest.write_config(self)
        self.member()

    def test_usage_errors(self):
        for project, argv in (
                ("q", ["--dir", "x"]), ("q", ["--job", "y"]), ("q", ["--me", "d"]),
                ("q", ["--config", "c"]), ("q", ["--every", "0"]), ("q", ["--every", "²"]),
                ("q", ["--max-minutes", "0"]), ("q", ["--max-minutes", "1441"]),
                ("q", ["--until-change", "--max-errors", "0"]),
                ("q", ["--until-change", "--max-errors", "1001"]),
                ("q", ["--max-errors", "3"]),
                # a streaming watch's --every is vcharon sync --repeat's, 1 to 300
                ("p", ["--every", "301"]), ("p", ["--every", "0"]),
                # a local member has no sync to stream
                ("q", ["--no-stream"])):
            with self.subTest(project=project, argv=argv):
                # never a watch loop: an argument that got through fails here, not by hanging
                ran = AssertionError("the watch started")
                with mock.patch.object(watch, "watch_dir", side_effect=ran), \
                        mock.patch.object(watch, "watch_job", side_effect=ran):
                    code, out, err = self.cli(*argv, project=project)
                self.assertEqual((code, out), (3, ""))
                self.assertTrue(err.startswith("ERROR config: "), err)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(["watch"]), 3)

    # the last line a stopped watcher prints, after its time
    INTERRUPTED = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d EXIT interrupted \(exit %d\)"

    def test_ctrl_c_ends_with_exit_interrupted(self):
        for target, project in (("watch_dir", "q"), ("watch_job", "p")):
            with self.subTest(target=target), \
                    mock.patch.object(watch, target, side_effect=KeyboardInterrupt):
                code, out, err = self.cli(project=project)
                self.assertEqual((code, err), (130, "vcharon: interrupted\n"))
                self.assertRegex(out.splitlines()[-1], self.INTERRUPTED % 130)

    @unittest.skipIf(os.name == "nt", "Windows has no SIGTERM to catch")
    def test_sigterm_ends_with_exit_interrupted(self):
        # the test's own handler: a SIGTERM the watch doesn't catch fails the test, not the
        # whole run, and the watch must put it back when it ends
        def uncaught(signum, frame):
            raise AssertionError("SIGTERM reached the test's handler")

        before = signal.signal(signal.SIGTERM, uncaught)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        stops = []

        def terminated(*args, **kw):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                # the handler runs at the next bytecode; never reached
                time.sleep(5)
            except KeyboardInterrupt:
                # a second SIGTERM, while the watch stops its child, changes nothing
                os.kill(os.getpid(), signal.SIGTERM)
                stops.append(signal.getsignal(signal.SIGTERM))
                raise

        for target, project in (("watch_dir", "q"), ("watch_job", "p")):
            with self.subTest(target=target), mock.patch.object(watch, target, terminated):
                code, out, err = self.cli(project=project)
                self.assertEqual((code, err), (143, "vcharon: interrupted\n"))
                self.assertRegex(out.splitlines()[-1], self.INTERRUPTED % 143)
                # SIG_IGN stays to the exit: a second SIGTERM can't cut the EXIT line short.
                # The next run's must reach the test's handler again
                self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
                signal.signal(signal.SIGTERM, uncaught)
        self.assertEqual(stops, [signal.SIG_IGN] * 2)
        # a run that got no SIGTERM puts the handler before it back
        with mock.patch.object(watch, "watch_dir", side_effect=KeyboardInterrupt):
            self.assertEqual(self.cli()[0], 130)
        self.assertIs(signal.getsignal(signal.SIGTERM), uncaught)
        # the run ended: a later command's Ctrl-C is 130 again
        with mock.patch.object(watch, "watch_dir", side_effect=KeyboardInterrupt):
            self.assertEqual(self.cli()[0], 130)

    def test_no_second_exit_line(self):
        # a Ctrl-C while the watch stops its child after its own EXIT line: that line stays
        # the last one, with the code of the interrupt
        def ended_then_interrupted(*args, out, **kw):
            out("2026-10-08 12:00:00 EXIT change (exit 0)")
            raise KeyboardInterrupt

        with mock.patch.object(watch, "watch_job", ended_then_interrupted):
            code, out, err = self.cli(project="p")
        self.assertEqual((code, err), (130, "vcharon: interrupted\n"))
        self.assertEqual(out.splitlines()[-1], "2026-10-08 12:00:00 EXIT change (exit 0)")

    def test_modes_and_ctrl_c(self):
        calls = []

        def interrupted(*args, **kw):
            # each gets the watchdog's check for its rounds (UpdatedTest), and the orphan
            # check, false outside a binary
            self.assertIsInstance(kw.pop("updated").__self__, install.Watchdog)
            self.assertFalse(kw.pop("orphaned")())
            # the run's printer, which notes an EXIT line (test_no_second_exit_line)
            self.assertTrue(callable(kw.pop("out")))
            calls.append((args, kw))
            raise KeyboardInterrupt

        with mock.patch.object(watch, "watch_dir", interrupted):
            code, _out, err = self.cli()
            self.assertEqual((code, err), (130, "vcharon: interrupted\n"))
            self.assertEqual(self.cli("--until-change")[0], 130)
        with mock.patch.object(watch, "watch_job", interrupted):
            for argv in (["--every", "5"], ["--until-change", "--fresh"],
                         ["--until-change", "--max-minutes", "5", "--max-errors", "3"],
                         ["--max-minutes", "29"], ["--no-stream"],
                         ["--no-stream", "--every", "3600"], ["--every", "300"]):
                with self.subTest(argv=argv):
                    self.assertEqual(self.cli(*argv, project="p")[0], 130)
        # the local member's channel folder, from its record; the remote member's section,
        # and the sync's arguments that find it again
        plain = {"fresh": False, "until_change": False, "max_minutes": None, "max_errors": 10,
                 "once": False}
        # a remote member streams by default, every 2 s; --no-stream syncs every 30 s
        streams = dict(plain, stream=True)
        self.assertEqual(calls, [
            # a local member's watch holds another folder over the channel's limits
            ((self.tree, "debian", 10), dict(plain, folder_limits=(50 * 1000 * 1000, 1000))),
            ((self.tree, "debian", 10), dict(plain, until_change=True, max_minutes=25,
                                             folder_limits=(50 * 1000 * 1000, 1000))),
            (("mb.windows", SYNC, 5), streams),
            (("mb.windows", SYNC, 2), dict(streams, fresh=True, until_change=True,
                                           max_minutes=25)),
            (("mb.windows", SYNC, 2), dict(streams, until_change=True, max_minutes=5,
                                           max_errors=3)),
            (("mb.windows", SYNC, 2), dict(streams, max_minutes=29)),
            (("mb.windows", SYNC, 30), dict(plain, stream=False)),
            (("mb.windows", SYNC, 3600), dict(plain, stream=False)),
            (("mb.windows", SYNC, 300), streams)])

    def test_once(self):
        for project, flag in (("q", ["--until-change"]), ("q", ["--max-minutes", "1"]),
                              ("q", ["--max-errors", "3"]), ("q", ["--every", "5"]),
                              ("p", ["--fresh"]), ("p", ["--until-change", "--fresh"])):
            with self.subTest(flag=flag):
                ran = AssertionError("the watch started")
                with mock.patch.object(watch, "watch_dir", side_effect=ran), \
                        mock.patch.object(watch, "watch_job", side_effect=ran):
                    code, out, err = self.cli("--once", *flag, project=project)
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(err.splitlines(), ["ERROR config: --once doesn't go with %s"
                                                    % flag[0], "  fix: leave out %s" % flag[0]])
        # a local member keeps its --no-stream refusal; a remote one never streams a check,
        # with or without --no-stream, at --no-stream's pace
        self.assertEqual(self.cli("--once", "--no-stream")[0], 3)
        calls = []
        with mock.patch.object(watch, "watch_dir", lambda *a, **kw: calls.append((a, kw)) or 16):
            self.assertEqual(self.cli("--once")[0], 16)
        with mock.patch.object(watch, "watch_job", lambda *a, **kw: calls.append((a, kw)) or 16):
            self.assertEqual(self.cli("--once", project="p")[0], 16)
            self.assertEqual(self.cli("--once", "--no-stream", project="p")[0], 16)
        self.assertEqual([(a, kw["once"], kw.get("stream")) for a, kw in calls], [
            ((self.tree, "debian", 10), True, None),
            (("mb.windows", SYNC, 30), True, False),
            (("mb.windows", SYNC, 30), True, False)])
        # the real check with no snapshot: the fix runs as printed
        code, out, err = self.cli("--once")
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err.splitlines(), [
            "ERROR config: --once needs your watcher's saved snapshot: there is none for debian "
            "on this machine (a join or create by an older vcharon or one stopped early, or a "
            "snapshot that couldn't be saved or was removed)",
            "  fix: " + platform.runnable("run vcharon read mb --to-me --project q (what came to "
                                          "you may not all have been printed), then vcharon "
                                          "watch mb --until-change --max-minutes 1 --project q "
                                          "(the one-minute check)")])

    def test_a_role_is_part_of_the_sync_args(self):
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": "windows-b",
                                  "leader": "debian", "ssh": "devbox", "remote": "r",
                                  "machine": util.TEST_MACHINE_ID, "project": "p",
                                  "role": "b", **util.record_format()})
        write_tree(self.vcharon_home, {"channels.d/mb.windows-b.ini": MAILBOX.format(
            local=os.path.join(self.tmp, "b")).replace("windows", "windows-b").encode()})
        calls = []
        with mock.patch.object(watch, "watch_job", lambda *a, **kw: calls.append(a) or 0):
            self.assertEqual(self.cli("--role", "b", project="p")[0], 0)
        self.assertEqual(calls, [("mb.windows-b", ["mb", "--project", "p", "--role", "b"], 2)])

    def test_exit_codes(self):
        for code in (0, 10, 11, 12, 13, 14, 16):
            with self.subTest(code=code), \
                    mock.patch.object(watch, "watch_dir", return_value=code):
                self.assertEqual(self.cli()[0], code)

    def test_not_a_member(self):
        code, out, err = self.cli(project="nope")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines()[0], "ERROR channel: you aren't in mb as --project nope "
                                              "(no join record on this box)")

    def test_utf8_whatever_the_console(self):
        # a console whose code page can't hold ñ (PYTHONIOENCODING stands in for Windows' 936)
        code = ("import sys\n"
                "from vcharon import cli\n"
                "from vcharon.mailbox import watch as w\n"
                "w.watch_dir = lambda *a, **k: w.say('new mañana.md')\n"
                "sys.exit(cli.main(['watch', 'mb', '--project', 'q']))\n")
        env = dict(os.environ, PYTHONIOENCODING="gbk")
        ran = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, timeout=60, check=False)
        want = "new mañana.md\n".encode().replace(b"\n", os.linesep.encode())
        self.assertEqual((ran.returncode, ran.stdout), (0, want), ran.stderr)


class OnceTest(WatchCase):
    """vcharon watch C --once: one round at once from the saved snapshot, then an ending of its
    own (DESIGN, "The watcher in a channel")."""

    def setUp(self):
        WatchCase.setUp(self)
        self.member("mac")

    def baseline(self):
        """A watcher's first start: what --once needs, its saved snapshot."""
        self.assertEqual(watch.watch_dir(self.tree, "debian", 10, out=[].append, sleep=never,
                                         rounds=0), 0)
        self.assertTrue(os.path.exists(self.state()))

    def once(self, **kw):
        """(exit code, lines without their time) of one local --once check."""
        lines = []
        code = watch.watch_dir(self.tree, "debian", 10, out=lines.append, sleep=never,
                               once=True, **kw)
        return code, self.said(lines)

    def test_nothing_new_then_a_change(self):
        # the code is in Stable: 10's rule, "start it again at once", would make a check a loop
        self.assertEqual(watch.EXIT_NOTHING, 16)
        self.baseline()
        since = "since %s" % self.saved_at(self.state())
        self.assertEqual(self.once(), (watch.EXIT_NOTHING, [
            watching(self.tree, 1, ", " + since), "EXIT nothing new (exit 16)"]))
        self.post("mac", 2, "go")
        code, lines = self.once()
        self.assertEqual((code, lines[1:]), (watch.EXIT_CHANGE, [
            "to you: mac#2 — go  (mac/RESULTS.md)", "EXIT change (exit 0)"]))
        # told once: the next check prints it no more
        self.assertEqual(self.once()[0], watch.EXIT_NOTHING)

    def test_no_snapshot_is_refused(self):
        # a first start is a baseline: what came since the join would be lost for good, so
        # the fix reads it first
        self.local_record(project="web")
        self.post("mac", 2, "go")
        with self.assertRaises(VCharonError) as cm:
            self.once()
        e = cm.exception
        self.assertEqual((e.code, e.message, e.hint), (
            "config", "--once needs your watcher's saved snapshot: there is none for debian on "
            "this machine (a join or create by an older vcharon or one stopped early, or a "
            "snapshot that couldn't be saved or was removed)",
            "run vcharon read mb --to-me --project web "
            "(what came to you may not all have been printed), then vcharon watch mb "
            "--until-change --max-minutes 1 --project web (the one-minute check)"))
        self.assertFalse(os.path.exists(self.state()))
        self.assertFalse(os.path.exists(self.state() + ".lock"))

    def test_an_unusable_snapshot_is_refused(self):
        # a start would save a baseline over it: what came since the last look would be lost
        self.baseline()
        self.local_record(project="web")
        self.post("mac", 2, "go")
        # before the lock: refused even while a watcher holds it
        lk = watch.take_lock(self.state())
        self.addCleanup(lk.release)
        for text, why in (("{", "it isn't JSON"), ("[]", "it has another shape")):
            with open(self.state(), "w", encoding="utf-8") as f:
                f.write(text)
            with self.assertRaises(VCharonError) as cm:
                self.once()
            e = cm.exception
            self.assertEqual((e.code, e.message, e.hint), (
                "config", "--once can't use your watcher's saved snapshot %s: %s"
                % (self.state(), why), "run vcharon read mb --to-me --project web (what came "
                "to you may not all have been printed), then vcharon watch mb --until-change "
                "--max-minutes 1 --project web (the one-minute check)"))
            # left as it was
            with open(self.state(), encoding="utf-8") as f:
                self.assertEqual(f.read(), text)

    def test_a_snapshot_spoilt_before_the_lock(self):
        # between the first check and the lock: checked again under it, so it is refused
        # before the start saves a baseline over it
        self.baseline()
        self.local_record(project="web")
        taken = watch._locked

        def spoil(w, state):
            with open(state, "w", encoding="utf-8") as f:
                f.write("{")
            return taken(w, state)

        with mock.patch.object(watch, "_locked", spoil), \
                self.assertRaises(VCharonError) as cm:
            self.once()
        self.assertEqual(cm.exception.message, "--once can't use your watcher's saved snapshot "
                         "%s: it isn't JSON" % self.state())
        self.assertTrue(cm.exception.hint.startswith("run vcharon read mb --to-me"))
        with open(self.state(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{")
        # and the lock was let go
        watch.take_lock(self.state()).release()

    def test_a_snapshot_spoilt_after_the_check(self):
        # the start can't use it though both checks could: its baseline can't tell what came
        # before, so the read fix comes before EXIT error
        self.baseline()
        self.local_record(project="web")
        with open(self.state(), "w", encoding="utf-8") as f:
            f.write("{")
        with mock.patch.object(watch, "_need_snapshot", lambda *a: None):
            code, lines = self.once()
        self.assertEqual(code, watch.EXIT_ERROR)
        self.assertTrue(lines[0].startswith("note: ignoring the saved snapshot "), lines)
        self.assertEqual(lines[-2:], ["  fix: " + platform.runnable(
            "run vcharon read mb --to-me --project web (what came to you may not all have been "
            "printed), then vcharon watch mb --until-change --max-minutes 1 --project web (the "
            "one-minute check)"), "EXIT error (exit 11)"])

    def test_another_watcher(self):
        self.baseline()
        lk = watch.take_lock(self.state())
        self.addCleanup(lk.release)
        self.assertEqual(self.once(), (watch.EXIT_LOCKED, [LOCKED % self.state()]))

    def test_closed(self):
        self.baseline()
        shutil.rmtree(self.tree)
        code, lines = self.once()
        self.assertEqual((code, lines[-1]), (watch.EXIT_CLOSED, "EXIT closed (exit 13)"))

    def test_a_failed_save(self):
        self.baseline()
        self.post("mac", 2, "go")
        with mock.patch.object(watch, "save_snapshot", side_effect=OSError(28, "No space")):
            code, lines = self.once()
        self.assertEqual(code, watch.EXIT_ERROR)
        self.assertEqual(lines[-3:], ["to you: mac#2 — go  (mac/RESULTS.md)",
                                      "ERROR can't save the snapshot %s: No space"
                                      % self.state(), "EXIT error (exit 11)"])

    def test_updated_first(self):
        self.baseline()
        self.post("mac", 2, "go")
        code, lines = self.once(updated=lambda: True)
        self.assertEqual((code, lines[1:]), (watch.EXIT_UPDATED, ["EXIT updated (exit 14)"]))
        # nothing read: the next check tells it
        self.assertEqual(self.once()[0], watch.EXIT_CHANGE)

    def test_orphaned_first(self):
        self.baseline()
        self.post("mac", 2, "go")
        code, lines = self.once(orphaned=lambda: True)
        self.assertEqual((code, lines[1:]), (watch.EXIT_ORPHANED, ["EXIT orphaned (exit 15)"]))
        # nothing read: the next check tells it
        code, lines = self.once()
        self.assertEqual((code, lines[1:]), (watch.EXIT_CHANGE, [
            "to you: mac#2 — go  (mac/RESULTS.md)", "EXIT change (exit 0)"]))


class OnceRemoteTest(WatchCase):
    """A remote member's --once: one sync, never streamed, capped at ONCE_TIMEOUT."""

    def setUp(self):
        WatchCase.setUp(self)
        self.sync_args = ClientModeTest.write_config(self)

    def no_spawn(self, argv, env):
        raise AssertionError("a --once check streamed")

    def job(self, results, once=True, rounds=None):
        """(exit code, lines without their time) of a watch_job whose syncs give results."""
        lines, runs = [], []

        def run(job, sync_args):
            runs.append(sync_args)
            return results.pop(0)

        # streaming asked for: --once overrides it
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=lines.append, sleep=never,
                               run=run, once=once, rounds=rounds, stream=once,
                               spawn=self.no_spawn)
        self.assertEqual(results, [])
        return code, self.said(lines)

    def baseline(self):
        self.assertEqual(self.job([], once=False, rounds=0), (0, [watching(self.tree, 0)]))

    def test_one_sync(self):
        self.baseline()
        code, lines = self.job([(0, None, None)])
        self.assertEqual((code, lines[1:]), (watch.EXIT_NOTHING, ["EXIT nothing new (exit 16)"]))
        self.assertIn(", since ", lines[0])

    def test_no_snapshot_is_refused(self):
        with self.assertRaises(VCharonError) as cm:
            self.job([])
        self.assertEqual(cm.exception.hint, "run vcharon read mb --to-me --project p (what "
                         "came to you may not all have been printed), then vcharon watch mb "
                         "--until-change --max-minutes 1 --project p (the one-minute check)")

    def test_an_unusable_snapshot_is_refused(self):
        self.baseline()
        state = self.state("windows", "mb.windows")
        with open(state, "w", encoding="utf-8") as f:
            f.write("{")
        with self.assertRaises(VCharonError) as cm:
            self.job([])
        self.assertEqual((cm.exception.message, cm.exception.hint), (
            "--once can't use your watcher's saved snapshot %s: it isn't JSON" % state,
            "run vcharon read mb --to-me --project p (what came to you may not all have been "
            "printed), then vcharon watch mb --until-change --max-minutes 1 --project p (the "
            "one-minute check)"))

    def test_a_snapshot_spoilt_before_the_lock(self):
        # checked again under the lock: refused before the start saves a baseline over it
        self.baseline()
        state = self.state("windows", "mb.windows")
        taken = watch._locked

        def spoil(w, state):
            with open(state, "w", encoding="utf-8") as f:
                f.write("{")
            return taken(w, state)

        with mock.patch.object(watch, "_locked", spoil), \
                self.assertRaises(VCharonError) as cm:
            self.job([])
        self.assertEqual(cm.exception.message, "--once can't use your watcher's saved snapshot "
                         "%s: it isn't JSON" % state)
        with open(state, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{")
        watch.take_lock(state).release()

    def test_a_snapshot_spoilt_after_the_check(self):
        # the start can't use it though both checks could: no sync runs, and the read fix
        # comes before EXIT error
        self.baseline()
        with open(self.state("windows", "mb.windows"), "w", encoding="utf-8") as f:
            f.write("{")
        with mock.patch.object(watch, "_need_snapshot", lambda *a: None):
            code, lines = self.job([])
        self.assertEqual(code, watch.EXIT_ERROR)
        self.assertTrue(lines[0].startswith("note: ignoring the saved snapshot "), lines)
        self.assertEqual(lines[-2:], ["  fix: " + platform.runnable(
            "run vcharon read mb --to-me --project p (what came to you may not all have been "
            "printed), then vcharon watch mb --until-change --max-minutes 1 --project p (the "
            "one-minute check)"), "EXIT error (exit 11)"])

    def test_a_closed_channel(self):
        # the sync's fix is the closed channel's: EXIT closed (don't start it again), not
        # EXIT change
        self.baseline()
        code, lines = self.job([(1, "ERROR not_found: no channel mb",
                                 channel_cmd.CHANNEL_GONE_PREFIX + " x")])
        self.assertEqual((code, lines[-1]), (watch.EXIT_CLOSED, "EXIT closed (exit 13)"))

    def test_busy_is_no_answer(self):
        # a post's sync holds the job: nothing was synced, so "nothing new" would be a guess
        self.baseline()
        code, lines = self.job([(watch.BUSY, "ERROR busy: another run", None)])
        self.assertEqual((code, lines[1:]), (watch.EXIT_ERROR, [
            "ERROR busy: a sync of mb is running (a post's, or one a stopped watcher left): "
            "nothing was synced",
            "  fix: " + platform.runnable("run vcharon watch mb --once --project p again after "
                                          "your next step; 3 times in a row, tell your user"),
            "EXIT error (exit 11)"]))

    def test_busy_after_what_the_post_brought(self):
        # what is in the copy already still counts
        self.baseline()
        self.post("debian", 2, "go", to="@windows")
        code, lines = self.job([(watch.BUSY, "ERROR busy: another run", None)])
        self.assertEqual((code, lines[1:]), (watch.EXIT_CHANGE, [
            "to you: debian#2 — go  (debian/RESULTS.md)", "EXIT change (exit 0)"]))

    def test_a_network_blip_is_an_error(self):
        # one round never waits out a blip, so it never counts: still not "nothing new"
        self.baseline()
        code, lines = self.job([(4, "ERROR connect: couldn't reach devbox", None)])
        self.assertEqual((code, lines[1:]), (watch.EXIT_ERROR, [
            "ERROR connect: couldn't reach devbox", "EXIT error (exit 11)"]))

    def test_a_saved_error_that_holds(self):
        self.baseline()
        error = "ERROR permission: can't write x"
        # a watcher saw it first and was woken for it
        self.assertEqual(self.job([(1, error, "fix it")], once=False, rounds=1)[0], 0)
        code, lines = self.job([(1, error, "fix it")])
        self.assertEqual((code, lines[1:]), (watch.EXIT_ERROR, [error, "  fix: fix it",
                                                                "EXIT error (exit 11)"]))
        # over: "ok again" counts, as for any watcher
        code, lines = self.job([(0, None, None)])
        self.assertEqual((code, lines[1:]),
                         (watch.EXIT_CHANGE, ["ok again", "EXIT change (exit 0)"]))

    def test_a_failed_sync_that_brought_an_entry(self):
        # a run that failed may still have brought files: what came is printed and wakes the
        # agent (EXIT change), the ERROR line among the lines above it, never EXIT error
        for code_, error in ((1, "ERROR permission: can't write x"),
                             (4, "ERROR connect: couldn't reach devbox")):
            with self.subTest(code=code_):
                self.baseline()
                self.post("debian", 2, "go", to="@windows")
                code, lines = self.job([(code_, error, None)])
                self.assertEqual(code, watch.EXIT_CHANGE, lines)
                self.assertEqual(lines[-1], "EXIT change (exit 0)")
                self.assertIn("to you: debian#2 — go  (debian/RESULTS.md)", lines)
                self.assertIn(error, lines)
                os.remove(self.state("windows", "mb.windows"))
                os.remove(os.path.join(self.tree, "debian", "RESULTS.md"))

    def test_the_sync_is_capped_and_stamps(self):
        # run_sync's timeout is ONCE_TIMEOUT, and the child is marked as a watcher's, at
        # --no-stream's pace: a check stamps the member's last-watched time
        self.baseline()
        ran = []

        def run(argv, timeout, new_session=False, env=None, term_wait=0):
            ran.append((timeout, new_session, term_wait, env.get(watch.WATCH_ENV)))
            return fsops.Ran(None, b"", b"")

        lines = []
        with mock.patch.object(watch.fsops, "run", run):
            code = watch.watch_job("mb.windows", self.sync_args, 30, out=lines.append,
                                   sleep=never, once=True, stream=True, spawn=self.no_spawn)
        self.assertEqual(ran, [(60, True, watch.TERM_WAIT, "run 30")])
        self.assertEqual((code, self.said(lines)[1:]), (watch.EXIT_ERROR, [
            "ERROR vcharon sync of mb.windows didn't finish within 60 s", "EXIT error (exit 11)"]))


class FirstLookTest(WatchCase):
    """join's and create's snapshot for the member's watcher (first_look): the first start goes
    on from it, and it is the snapshot a start without one would save (DESIGN, "Create, join,
    leave, close")."""

    def setUp(self):
        WatchCase.setUp(self)
        self.member("mac")

    def doc(self, path):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        del doc["saved"]
        return doc

    def test_the_start_goes_on_from_it(self):
        self.local_record()
        self.post("mac", 2, "before the look")
        before = watch.stamp(time.time())
        marks = watch.first_look(channel_cmd.read_record("mb", "debian"), None)
        after = watch.stamp(time.time())
        self.assertTrue(marks.has("mac", 2))
        self.assertFalse(marks.has("mac", 3))
        # the time of the look itself: the watching line's since says when what follows began
        saved = self.saved_at(self.state())
        self.assertTrue(before <= saved <= after, (before, saved, after))
        since = "since %s" % saved
        # between the look and the first start: printed once, by that start's first round
        self.post("mac", 3, "after the look")
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=never,
                               rounds=1)
        self.assertEqual((code, self.said()), (0, [
            watching(self.tree, 2, ", " + since),
            "to you: mac#3 — after the look  (mac/RESULTS.md)"]))
        lines = []
        watch.watch_dir(self.tree, "debian", 10, out=lines.append, sleep=never, rounds=1)
        self.assertEqual([l for l in self.said(lines) if l.startswith("to ")], [])

    def test_a_local_member_s_is_the_start_s(self):
        # warnings of both kinds: a name at the top, and a folder over the limits, which the
        # snapshot holds as it was
        limits = {"max_mb": 1, "max_files": 10, "max_entry_kb": 1}
        self.local_record(project="q")
        channel_cmd.write_record(dict(channel_cmd.read_record("mb", "debian"),
                                      **util.record_format(limits)))
        self.post("mac", 2, "for you")
        self.post("mac", 3, "not its folder's", name="zed")
        write_tree(self.tree, {"stray.txt": b"x"})
        write_tree(self.tree, {"big/f%02d.txt" % i: b"y" for i in range(11)})
        watch.first_look(channel_cmd.read_record("mb", "debian"), None)
        looked = self.doc(self.state())
        self.assertEqual(len(looked["warnings"]), 2, looked["warnings"])
        self.assertTrue(looked["loose"])
        os.remove(self.state())
        watch.watch_dir(self.tree, "debian", 10, out=[].append, sleep=never, rounds=0,
                        folder_limits=(1 * charter.MB, 10))
        self.assertEqual(self.doc(self.state()), looked)

    def test_a_remote_member_s_is_the_start_s(self):
        sync_args = ClientModeTest.write_config(self)
        self.post("debian", 2, "steps", to="@all", file="STEPS.md")
        self.post("mac", 2, "for you", to="@windows")
        write_tree(self.tree, {"stray.txt": b"x"})
        # the members the last pull left out: a WARN each
        charter.save_left_out("mb.windows", [charter.skipped_note(
            "big", "2.0 kB in 3 files, 1.0 kB over the limit of 1.0 kB and 10 files")],
            1000, 10)
        state = self.state("windows", "mb.windows")
        watch.first_look(channel_cmd.read_record("mb", "windows"), "mb.windows")
        looked = self.doc(state)
        self.assertEqual(len(looked["warnings"]), 1, looked["warnings"])
        self.assertEqual(sorted(looked["seen"]), ["debian", "mac"])
        os.remove(state)
        watch.watch_job("mb.windows", sync_args, 30, out=[].append, sleep=never,
                        run=lambda job, args: (0, None, None), rounds=0)
        self.assertEqual(self.doc(state), looked)

    def test_create_s_marks_nothing_in_a_member_s_folder(self):
        self.local_record()
        self.post("mac", 2, "JOIN")
        write_tree(self.tree, {"stray.txt": b"x"})
        marks = watch.first_look(channel_cmd.read_record("mb", "debian"), None, created=True)
        self.assertEqual(marks.doc(), watch.Marks().doc())
        looked = self.doc(self.state())
        self.assertEqual(sorted(looked["files"]), ["stray.txt"])
        self.assertEqual(looked["warnings"], [
            "stray.txt at the top isn't a writer's folder: clients leave it out"])
        # the JOIN is new to the first start; the top's file and warning are not
        watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=never, rounds=1)
        lines = self.said()
        self.assertIn("to you: mac#2 — JOIN  (mac/RESULTS.md)", lines)
        self.assertNotIn("new stray.txt", lines)
        self.assertEqual(lines.count("WARN stray.txt at the top isn't a writer's folder: "
                                     "clients leave it out"), 1)

    def test_keep_keeps_a_usable_snapshot(self):
        self.local_record()
        record = channel_cmd.read_record("mb", "debian")
        watch.watch_dir(self.tree, "debian", 10, out=[].append, sleep=never, rounds=0)
        with open(self.state(), "rb") as f:
            before = f.read()
        self.post("mac", 2, "while away")
        self.assertIsNone(watch.first_look(record, None, keep=True))
        with open(self.state(), "rb") as f:
            self.assertEqual(f.read(), before)
        # one it can't use is replaced
        with open(self.state(), "w", encoding="utf-8") as f:
            f.write("{")
        self.assertTrue(watch.first_look(record, None, keep=True).has("mac", 2))
        self.assertIsNotNone(watch.load_snapshot(self.state(), self.tree, "debian")[0])

    def test_marked(self):
        self.local_record()
        self.post("mac", 2, "own")
        self.post("mac", 3, "not its folder's", name="zed")
        marks = watch.first_look(channel_cmd.read_record("mb", "debian"), None)
        found = entries.parse_file(os.path.join(self.tree, "mac", "RESULTS.md"))
        self.assertEqual([watch.marked(marks, "mac", e) for e in found], [True, True])
        self.post("mac", 4, "later")
        self.post("mac", 5, "later, not its folder's", name="zed")
        found = entries.parse_file(os.path.join(self.tree, "mac", "RESULTS.md"))
        self.assertEqual([watch.marked(marks, "mac", e) for e in found],
                         [True, True, False, False])


class OutputInChannelTest(WatchCase):
    """A watcher whose stdout or stderr is a file in the channel tree is refused before it
    writes anything: that file would go to every member (DESIGN, "The watcher in a
    channel")."""

    def setUp(self):
        WatchCase.setUp(self)
        # debian, a local member of the project q; windows, a remote one of p; both trees are
        # self.tree
        self.local_record()
        ClientModeTest.write_config(self)
        self.member()
        write_tree(self.tree, {"windows/MEMBER.md": member_md("windows", "debian")})

    def files(self):
        """Every file under the tree, and the state dir's, with its bytes."""
        out = {}
        for root in (self.tree, self.vcharon_home):
            for top, _dirs, names in os.walk(root):
                for name in names:
                    with open(os.path.join(top, name), "rb") as f:
                        out[os.path.join(top, name)] = f.read()
        return out

    def open_file(self, *parts):
        return self.enterContext(open(os.path.join(*parts), "w", encoding="utf-8"))

    def run_watch(self, stdout=None, stderr=None, project="q"):
        """(exit code, stdout's text if a StringIO, stderr's likewise, whether a watch
        started)."""
        out = io.StringIO() if stdout is None else stdout
        err = io.StringIO() if stderr is None else stderr
        started = []
        with mock.patch.object(watch, "watch_dir", lambda *a, **kw: started.append(a) or 0), \
                mock.patch.object(watch, "watch_job", lambda *a, **kw: started.append(a) or 0), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(["watch", "mb", "--project", project])
        text = [s.getvalue() if isinstance(s, io.StringIO) else None for s in (out, err)]
        return code, text[0], text[1], bool(started)

    def refused(self, rel, named=None):
        """The refusal's lines; named: the fix's name of the file, rel in backticks unless
        given."""
        return ("ERROR config: the watcher's output goes to %s, a file in the channel: every "
                "member gets that file\n  fix: send the watcher's output to a file outside the "
                "channel; if your redirect created %s, delete it\n"
                % (rel, "`%s`" % rel if named is None else named))

    def test_stdout_in_the_own_folder(self):
        f = self.open_file(self.tree, "debian", "vcharon-watch.log")
        before = self.files()
        code, _out, err, started = self.run_watch(stdout=f)
        self.assertEqual((code, err, started),
                         (3, self.refused("debian/vcharon-watch.log"), False))
        self.assertNotIn(self.tmp, err)
        # no MEMBER.md refresh, no snapshot, no lock, nothing in the file
        self.assertEqual(self.files(), before)

    def test_stderr_in_the_own_folder(self):
        f = self.open_file(self.tree, "debian", "vcharon-watch.log")
        before = self.files()
        code, out, _err, started = self.run_watch(stderr=f)
        self.assertEqual((code, out, started), (3, "", False))
        f.flush()
        # the refusal goes where stderr goes, the file itself: no path of this machine in it
        path = os.path.join(self.tree, "debian", "vcharon-watch.log")
        # written in text mode: os.linesep line ends
        before[path] = self.refused("debian/vcharon-watch.log").encode("utf-8").replace(
            b"\n", os.linesep.encode())
        self.assertEqual(self.files(), before)

    def test_a_file_in_another_members_folder(self):
        f = self.open_file(self.tree, "windows", "out.log")
        code, _out, err, started = self.run_watch(stdout=f)
        self.assertEqual((code, err, started), (3, self.refused("windows/out.log"), False))

    def test_a_remote_members_copy(self):
        f = self.open_file(self.tree, "windows", "vcharon-watch.log")
        code, _out, err, started = self.run_watch(stdout=f, project="p")
        self.assertEqual((code, err, started),
                         (3, self.refused("windows/vcharon-watch.log"), False))

    def test_appended_to_an_entry_file(self):
        # >> RESULTS.md: the file was there before the redirect, so the fix doesn't say to
        # delete it outright
        path = self.post("debian", 2, "results")
        with open(path, "rb") as f:
            text = f.read()
        f = self.enterContext(open(path, "a", encoding="utf-8"))
        code, _out, err, started = self.run_watch(stdout=f)
        self.assertEqual((code, err, started), (3, self.refused("debian/RESULTS.md"), False))
        self.assertIn("if your redirect created", err)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), text)

    @unittest.skipIf(os.name == "nt", "Windows can't hold the name")
    def test_a_control_character_in_the_name_is_escaped(self):
        f = self.open_file(self.tree, "debian", "a\x1bb.log")
        code, _out, err, _started = self.run_watch(stdout=f)
        self.assertEqual((code, err), (3, self.refused("debian/a\\x1bb.log")))
        self.assertNotIn("\x1b", err)

    def test_a_command_in_the_name_isnt_respelled(self):
        f = self.open_file(self.tree, "debian", "x vcharon post y.log")
        with mock.patch.object(platform, "self_command", return_value="/opt/vc/bin/vcharon"):
            code, _out, err, _started = self.run_watch(stdout=f)
        self.assertEqual((code, err), (3, self.refused("debian/x vcharon post y.log",
                                                       named="that file")))

    def test_the_same_inode_on_another_device(self):
        f = self.open_file(self.tree, "debian", "vcharon-watch.log")
        real = os.fstat
        mine = real(f.fileno())

        def fstat(fd):
            st = real(fd)
            if (st.st_dev, st.st_ino) != (mine.st_dev, mine.st_ino):
                return st
            fields = list(st)
            fields[stat.ST_DEV] = st.st_dev + 1
            return os.stat_result(fields)

        with mock.patch.object(os, "fstat", fstat):
            self.assertEqual(self.run_watch(stdout=f)[0::3], (0, True))

    def test_a_file_outside_the_channel(self):
        f = self.open_file(self.tmp, "vcharon-watch.log")
        for project in ("q", "p"):
            with self.subTest(project=project):
                self.assertEqual(self.run_watch(stdout=f, stderr=f, project=project)[0::3],
                                 (0, True))

    def test_a_pipe(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        f = self.enterContext(open(w, "w", encoding="utf-8"))
        self.assertEqual(self.run_watch(stdout=f, stderr=f)[0::3], (0, True))


class StaleSkillTest(WatchCase):
    """A watcher's start notes a skill copy vcharon wrote that isn't this version's, once, as
    a status line that wakes nobody (DESIGN, "The watcher in a channel")."""

    def setUp(self):
        WatchCase.setUp(self)
        util.skill_home(self)
        # debian, a local member of the project q; windows, a remote one of p
        self.local_record()
        ClientModeTest.write_config(self)
        self.member()
        write_tree(self.tree, {"windows/MEMBER.md": member_md("windows", "debian")})
        util.write_skill("claude", util.OLD_SKILL)
        util.write_skill("codex", util.OLD_SKILL)
        self.note = ("note: your vcharon skills at %s and %s are from another version: %s"
                     % (skill.path("claude"), skill.path("codex"),
                        platform.runnable("vcharon skill install --claude --codex")))

    def watch(self, *argv, step=lambda: None):
        """vcharon watch mb --project q --until-change --max-minutes 1 --every 60 ARGV, one
        round on a fake clock, step run in its sleep: (exit code, the lines without their time,
        stderr)."""
        real = watch.watch_dir
        clock = Clock()

        def one_round(*a, **kw):
            return real(*a, sleep=Rounds(step, clock=clock), clock=clock, timer=clock,
                        rounds=1, **kw)

        with mock.patch.object(watch, "watch_dir", one_round):
            code, out, err = self.cli("--until-change", "--max-minutes", "1", "--every", "60",
                                      *argv)
        return code, self.said(out.splitlines()), err

    def test_once_at_the_start_and_it_wakes_nobody(self):
        code, lines, err = self.watch()
        self.assertEqual((code, err), (watch.EXIT_QUIET, ""))
        self.assertEqual(lines, [self.note, watching(self.tree, 1), "EXIT quiet 1 min (exit 10)"])
        # an entry in that round wakes it, and the note is still printed once
        os.remove(self.state())
        code, lines, err = self.watch(step=lambda: self.post("windows", 2, "go"))
        self.assertEqual((code, err), (watch.EXIT_CHANGE, ""))
        self.assertEqual([l for l in lines if "skill" in l], [self.note])
        self.assertEqual(lines[-1], "EXIT change (exit 0)")

    def test_a_remote_member(self):
        started = []
        with mock.patch.object(watch, "watch_job", lambda *a, **kw: started.append(a) or 0):
            code, out, err = self.cli("--until-change", project="p")
        self.assertEqual((code, err, len(started)), (0, "", 1))
        self.assertEqual(self.said(out.splitlines()), [self.note])

    def test_not_in_a_check(self):
        # a --once check runs between every two steps: join and every watcher start say it
        started = []
        with mock.patch.object(watch, "watch_job", lambda *a, **kw: started.append(a) or 16):
            code, out, err = self.cli("--once", project="p")
        self.assertEqual((code, out, err, len(started)), (16, "", "", 1))

    def test_no_note(self):
        # this version's copies, and the user's own file
        util.write_skill("claude")
        util.write_skill("codex", "---\nname: vcharon\n---\nmy own notes\n")
        code, lines, err = self.watch()
        self.assertEqual((code, err), (watch.EXIT_QUIET, ""))
        self.assertEqual(lines, [watching(self.tree, 1), "EXIT quiet 1 min (exit 10)"])
        # a check that fails: the watch runs as ever (a fresh start again, so it sleeps)
        util.write_skill("codex", util.OLD_SKILL)
        os.remove(self.state())
        with mock.patch.object(skill, "installed", side_effect=OSError("broken")):
            code, lines, err = self.watch()
        self.assertEqual((code, err), (watch.EXIT_QUIET, ""))
        self.assertEqual(lines, [watching(self.tree, 1), "EXIT quiet 1 min (exit 10)"])

    @unittest.skipIf(os.name == "nt", "Windows names can't hold a control character")
    def test_a_control_character_in_the_path_is_escaped(self):
        home = os.path.join(self.tmp, "a\x1bb")
        os.mkdir(home)
        os.environ.update(HOME=home, USERPROFILE=home)
        target = util.write_skill("claude", util.OLD_SKILL)
        self.assertIn("\x1b", target)
        started = []
        with mock.patch.object(watch, "watch_job", lambda *a, **kw: started.append(a) or 0):
            code, out, err = self.cli("--until-change", project="p")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("\x1b", out)
        self.assertEqual(self.said(out.splitlines()), [
            "note: your vcharon skill at %s is from another version: %s"
            % (target.replace("\x1b", "\\x1b"),
               platform.runnable("vcharon skill install --claude"))])

    def test_never_into_the_channel(self):
        # stdout in the channel: refused before the note, which names this machine's home
        path = os.path.join(self.tree, "debian", "vcharon-watch.log")
        f = self.enterContext(open(path, "w", encoding="utf-8"))
        err = io.StringIO()
        with mock.patch("sys.stdout", f), mock.patch("sys.stderr", err):
            code = cli.main(["watch", "mb", "--project", "q"])
        f.flush()
        self.assertEqual(code, 3)
        with open(path, encoding="utf-8") as g:
            self.assertEqual(g.read(), "")
        self.assertNotIn("skill", err.getvalue())


class UpdatedTest(WatchCase):
    """EXIT updated (exit 14): vcharon's code changed under a running watcher (DESIGN,
    "Running watchers"), seen at the top of a round, or in a binary after an unexpected
    error. The streaming child's exit 14 is StreamTest's."""

    def setUp(self):
        WatchCase.setUp(self)
        self.local_record()
        # the file whose identity the watchdog compares: a stand-in for the binary
        self.code = os.path.join(self.tmp, "vcharon-binary")
        with open(self.code, "wb") as f:
            f.write(b"old")
        patcher = mock.patch.object(install, "code_path", return_value=self.code)
        patcher.start()
        self.addCleanup(patcher.stop)

    def swap(self):
        """The binary replaced as an update does: a new file moved over it."""
        new = self.code + ".new"
        with open(new, "wb") as f:
            f.write(b"new!")
        os.replace(new, self.code)

    def watch_with(self, sleep=None, fail=None):
        """vcharon watch mb through the command line, its rounds with sleep (no wait), and
        fail, if given, raised by the watch itself."""
        real = watch.watch_dir

        def watch_dir(*args, **kw):
            if fail is not None:
                raise fail()
            return real(*args, sleep=sleep, **kw)

        with mock.patch.object(watch, "watch_dir", watch_dir):
            return self.cli()

    def test_at_the_top_of_a_round(self):
        # the check comes before the round's work: what came meanwhile waits for the next
        # watcher, which goes on from the saved snapshot
        checks = iter([False, True])

        def sleep(seconds):
            write_tree(self.tree, {"windows/x": b"x"})

        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=sleep,
                               updated=lambda: next(checks))
        self.assertEqual(code, watch.EXIT_UPDATED)
        self.assertEqual(self.said(), [watching(self.tree, 0), "new windows/x",
                                       "EXIT updated (exit 14)"])

    def test_an_orphan_exits_after_the_update_check(self):
        # a binary's bootloader killed: the round top ends the watch, and the lock is freed
        def sleep(seconds):
            write_tree(self.tree, {"windows/x": b"x"})

        checks = iter([False, True])
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=sleep,
                               updated=lambda: False, orphaned=lambda: next(checks))
        self.assertEqual(code, watch.EXIT_ORPHANED)
        self.assertEqual(self.said(), [watching(self.tree, 0), "new windows/x",
                                       "EXIT orphaned (exit 15)"])
        # free: another watcher starts
        self.lines.clear()
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=sleep,
                               rounds=1)
        self.assertEqual(code, 0)

    def test_an_orphan_through_the_command_line(self):
        # a frozen binary whose bootloader parent went after the start; the log says why. The
        # parent is faked (OrphanTest has its POSIX and Windows checks), on Windows too, where
        # exit_with_parent builds it: with no handle it starts no thread, so the round's check
        # is what sees this test's parent go
        gone = []
        rounds = []

        class Parent:
            pid = 4242
            handle = None
            followed = False

            def gone(self):
                return bool(gone)

        def sleep(seconds):
            rounds.append(seconds)
            if len(rounds) == 2:
                gone.append(True)
            if len(rounds) > 10:
                raise AssertionError("the orphan check never ended the watch")

        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.object(install, "Parent", Parent):
            code, out, err = self.watch_with(sleep=sleep)
        self.assertEqual((code, err), (15, ""))
        self.assertEqual(self.said(out.splitlines()),
                         [watching(self.tree, 0), "EXIT orphaned (exit 15)"])
        with open(os.path.join(platform.log_dir(), "vcharon.log"), encoding="utf-8") as f:
            self.assertIn("watch: the process that started this one (pid 4242) is gone; "
                          "exiting with 15", f.read())

    def test_through_the_command_line(self):
        rounds = []

        def sleep(seconds):
            rounds.append(seconds)
            if len(rounds) == 3:
                self.swap()

        code, out, err = self.watch_with(sleep=sleep)
        self.assertEqual((code, err), (14, ""))
        self.assertEqual(self.said(out.splitlines()),
                         [watching(self.tree, 0), "EXIT updated (exit 14)"])
        self.assertEqual(len(rounds), 3)
        self.assertEqual(install.EXIT_UPDATED, 14)

    def test_a_real_watcher_imports_nothing_after_its_start(self):
        # in a child, as an agent runs it: entries and files come, then the binary is swapped;
        # every module the rounds needed was there at the start (DESIGN, "Running watchers")
        imported = os.path.join(self.tmp, "imported.json")
        code = ("import json, os, sys\n"
                "from vcharon import cli, install\n"
                "install.code_path = lambda: %r\n"
                "start = []\n"
                "init = install.Watchdog.__init__\n"
                "def watched(self, *args, **kwargs):\n"
                "    init(self, *args, **kwargs)\n"
                "    start.append(set(sys.modules))\n"
                "install.Watchdog.__init__ = watched\n"
                "code = cli.main(sys.argv[1:])\n"
                "with open(%r, 'w') as f:\n"
                "    json.dump(sorted(set(sys.modules) - start[0]), f)\n"
                "sys.exit(code)\n" % (self.code, imported))
        child = subprocess.Popen([sys.executable, "-S", "-c", code, "watch", "mb", "--project",
                                  "q", "--every", "1"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=no_site_env())
        try:
            lines = [util.readline(child.stdout).decode("utf-8")]
            self.post("mac", 1, "first", to="@debian")
            write_tree(self.tree, {"mac/run.log": b"x", "mac/A.md": b"a", "mac/a.md": b"a"})
            lines.append(util.readline(child.stdout).decode("utf-8"))
            self.swap()
            out, err = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        lines = [line.rstrip("\r\n") for line in lines] + out.decode("utf-8").splitlines()
        self.assertEqual(child.returncode, 14, err)
        said = self.said(lines)
        self.assertEqual((said[0], said[1], said[-1]),
                         (watching(self.tree, 0), "to you: mac#1 — first  (mac/RESULTS.md)",
                          "EXIT updated (exit 14)"))
        with open(imported, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_an_unexpected_error_in_a_binary_after_a_swap(self):
        # a read of the new file at the old offsets can be any exception
        def fail():
            self.swap()
            return ImportError("bad marshal data (unknown type code)")

        with mock.patch.object(platform, "is_frozen", return_value=True):
            code, out, err = self.watch_with(fail=fail)
        self.assertEqual((code, err), (14, ""))
        self.assertEqual(self.said(out.splitlines()), ["EXIT updated (exit 14)"])
        with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"),
                  encoding="utf-8") as f:
            self.assertIn("vcharon changed under this process (ImportError: bad marshal data "
                          "(unknown type code)); exiting with 14", f.read())

    def test_an_unexpected_error_without_a_swap_is_a_bug(self):
        # a package's files are whole files either way: an error is the error, swap or not
        def fail():
            self.swap()
            return ImportError("no module named x")

        for frozen, error in ((True, lambda: ImportError("no module named x")), (False, fail)):
            with self.subTest(frozen=frozen):
                with mock.patch.object(platform, "is_frozen", return_value=frozen):
                    code, out, err = self.watch_with(fail=error)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("ERROR internal: ImportError: no module named "
                                               "x\n"), err)


# Runs cli.main as a Windows binary's Python child would, on Linux: _winapi's wait on the
# bootloader backed by a pidfd, and "a Windows binary" answered only to cli.main's check and to
# install.Parent, so the rest runs as on this OS. _exit is wrapped to say which thread ended
# the process (the real os._exit still ends it); FILL_STDOUT fills stdout from a thread once
# the start is done, as a pipe no one reads would.
BOOTLOADER_CHILD = """\
import os, select, sys, threading, time
from vcharon import cli, install, platform


class PidfdWinapi:
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    INFINITE = 0xFFFFFFFF

    def OpenProcess(self, access, inherit, pid):
        return os.pidfd_open(pid)

    def WaitForSingleObject(self, handle, ms):
        ready = select.select([handle], [], [], None if ms == self.INFINITE else ms / 1000)[0]
        return self.WAIT_OBJECT_0 if ready else self.WAIT_TIMEOUT


install._winapi = PidfdWinapi()
install.sweep_old = lambda *args: None
real_os_name, real_frozen = platform.os_name, platform.is_frozen
AS_WINDOWS = {("vcharon.cli", "main"), ("vcharon.install", "__init__"),
              ("vcharon.install", "gone")}


def caller():
    frame = sys._getframe(2)
    return frame.f_globals.get("__name__"), frame.f_code.co_name


platform.os_name = lambda: "windows" if caller() in AS_WINDOWS else real_os_name()
platform.is_frozen = lambda: caller() == ("vcharon.cli", "main") or real_frozen()
real_exit = install._exit


def recorded_exit(code):
    with open(os.environ["EXITS"], "a") as f:
        f.write("%s %d\\n" % (threading.current_thread().name, code))
    real_exit(code)


install._exit = recorded_exit
with open(os.environ["PID_FILE"] + ".part", "w") as f:
    f.write(str(os.getpid()))
os.replace(os.environ["PID_FILE"] + ".part", os.environ["PID_FILE"])
if os.environ.get("FILL_STDOUT"):
    def fill():
        # after main has started its thread: what comes before it reconfigures stdout
        time.sleep(0.5)
        sys.stdout.write("x" * (4 << 20))
        sys.stdout.flush()

    threading.Thread(target=fill, daemon=True).start()
sys.exit(cli.main(sys.argv[1:]))
"""


@unittest.skipUnless(hasattr(os, "pidfd_open"), "a pidfd stands in for the bootloader's handle")
class BootloaderKillTest(WatchCase):
    """A Windows binary's watcher whose bootloader is killed alone, run for real on Linux: the
    bootloader a stand-in process, killed with SIGKILL, and its handle a pidfd. The watcher
    must end at once, not at its next round (300 s off), and free its lock."""

    def setUp(self):
        WatchCase.setUp(self)
        self.local_record()
        self.exits = os.path.join(self.tmp, "exits")
        self.pid_file = os.path.join(self.tmp, "pid")

    def kill_bootloader(self, fill=False):
        """Starts the watcher under a stand-in bootloader, kills the stand-in once the watcher
        runs: (seconds from the kill to the watcher's end, its stdout lines or None when
        filled, the exits it recorded)."""
        env = dict(no_site_env(), EXITS=self.exits, PID_FILE=self.pid_file)
        if fill:
            env["FILL_STDOUT"] = "1"
        stand_in = subprocess.Popen(
            [sys.executable, "-S", "-c", "import subprocess, sys; subprocess.run(sys.argv[1:])",
             sys.executable, "-S", "-c", BOOTLOADER_CHILD, "watch", "mb", "--project", "q",
             "--every", "300"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.addCleanup(stand_in.stdout.close)
        self.addCleanup(stand_in.wait)
        self.addCleanup(stand_in.kill)
        lines = None
        if fill:
            deadline = time.monotonic() + 30
            while not os.path.exists(self.pid_file):
                self.assertLess(time.monotonic(), deadline, "the watcher never started")
                time.sleep(0.05)
            # the fill is under way, and main's own first line is behind it
            time.sleep(1.5)
        else:
            lines = [util.readline(stand_in.stdout).decode("utf-8").rstrip("\n")]
            # the start prints its watching line before it saves the snapshot; a kill in
            # between leaves none, and the next start would take a baseline and sleep first
            deadline = time.monotonic() + 30
            while not os.path.exists(self.state()):
                self.assertLess(time.monotonic(), deadline, "the watcher never saved")
                time.sleep(0.05)
        with open(self.pid_file, encoding="utf-8") as f:
            pid = int(f.read())
        # opened while it runs: the pid can't be another process's yet
        handle = os.pidfd_open(pid)
        self.addCleanup(os.close, handle)
        start = time.monotonic()
        stand_in.kill()
        stand_in.wait()
        ended = select.select([handle], [], [], 30)[0]
        took = time.monotonic() - start
        if not ended:
            os.kill(pid, signal.SIGKILL)
            self.fail("the watcher still ran 30 s after its bootloader was killed")
        if not fill:
            lines += stand_in.stdout.read().decode("utf-8").splitlines()
        with open(self.exits, encoding="utf-8") as f:
            exits = f.read().splitlines()
        return took, lines, exits

    def gone_lines(self):
        with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"),
                  encoding="utf-8") as f:
            return [line for line in f if "is gone; exiting with" in line]

    def test_the_watcher_ends_at_once_and_frees_its_lock(self):
        took, lines, exits = self.kill_bootloader()
        self.assertLess(took, 10)
        # the thread's exit, once; the round's check never ran, so one EXIT orphaned
        self.assertEqual(exits, ["bootloader-watch 15"])
        self.assertEqual(self.said(lines), [watching(self.tree, 0), "EXIT orphaned (exit 15)"])
        gone = self.gone_lines()
        self.assertEqual(len(gone), 1, gone)
        # the thread's line, not the round check's ("watch: the process ...")
        self.assertIn("  info  the process that started this one (pid ", gone[0])
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=never,
                               rounds=1)
        self.assertEqual(code, 0)

    def test_a_full_stdout_cant_hold_the_exit(self):
        took, _, exits = self.kill_bootloader(fill=True)
        # the timer's exit: the thread is stuck in its EXIT orphaned
        self.assertEqual(len(exits), 1, exits)
        self.assertNotIn("bootloader-watch", exits[0])
        self.assertTrue(exits[0].endswith(" 15"), exits)
        self.assertGreaterEqual(took, install.ORPHAN_WRITE_WAIT - 0.1)
        self.assertLess(took, 10)
        # the log line comes before the write that blocks
        self.assertEqual(len(self.gone_lines()), 1)


class LooseTest(WatchCase):
    def test_an_entry_without_its_folders_id_is_told_once(self):
        # loose entries are remembered by hash (a mutant other tests let survive)
        self.post("mac", 1, "no id", name="other")
        with open(os.path.join(self.tree, "mac", "RESULTS.md"), "a", encoding="utf-8") as f:
            f.write("\n## 2026-10-01 09:01 — an old heading\n\nbody\n")
        marks = watch.Marks()
        first = watch.read_entries(self.tree, ["mac/RESULTS.md"], marks, "debian", "debian")
        self.assertEqual(sorted(first.others), ["mac", "mac"])
        self.assertEqual(first.misplaced, ["WARN entry other#1 in mac/: not its folder's"])
        again = watch.read_entries(self.tree, ["mac/RESULTS.md"], marks, "debian", "debian")
        self.assertEqual((again.others, again.misplaced, again.counted()), ([], [], 0))

