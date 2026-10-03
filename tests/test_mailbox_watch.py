"""vcharon watch: scans, the lines it prints, both modes; the time on each line,
the saved snapshot, its lock, --until-change and the limits; a channel's entries,
snapshot version 2."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from vcharon import channel_cmd, charter, cli, entries, platform
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

from tests import util
from tests.util import write_tree

# what follows `vcharon sync` for the remote member windows (ClientModeTest.write_config)
SYNC = ["mb", "--project", "p"]

# 2026-10-01 09:05:46 on this machine's clock, whatever its zone
T0 = time.mktime((2026, 10, 1, 9, 5, 46, 0, 0, -1))
STAMP = re.compile(r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")


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


def never(seconds):
    raise AssertionError("slept before the first round")


def member_md(name, leader, channel="mb"):
    """MEMBER.md as vcharon join writes it."""
    return ("# MEMBER\n\n## 2026-10-01 09:00 — %s#1 — member\nto: @%s\nchannel: %s\nname: %s\n"
            "leader: %s\n" % (name, leader, channel, name, leader)).encode("utf-8")


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

    def test_read_errors_keep_the_snapshot(self):
        write_tree(self.tree, {"mac/x": b"x"})
        real = watch.scan
        fail = [False]

        def scan(root, me, fold=False):
            if fail[0]:
                raise PermissionError(13, "Permission denied")
            return real(root, me, fold)

        def broken():
            fail[0] = True

        def fixed():
            fail[0] = False

        with mock.patch.object(watch, "scan", scan):
            watch.watch_dir(self.tree, "debian", 1, out=self.lines.append,
                            sleep=Rounds(broken, lambda: None, fixed), rounds=3)
        self.assertEqual(self.said(), [watching(self.tree, 1),
                                       "ERROR can't read %s: Permission denied" % self.tree,
                                       "ok again"])


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
            "2026-10-01 09:06:46 EXIT quiet 1 min"])

    def test_the_format(self):
        self.assertEqual(watch.stamp(T0), "2026-10-01 09:05:46")
        self.assertEqual(watch.stamp(T0 + 3600 * 15 + 14), "2026-10-02 00:06:00")


class RootTest(WatchCase):
    def test_a_missing_root_ends_the_watch(self):
        write_tree(self.tree, {"mac/x": b"x"})
        why = []

        def gone():
            shutil.rmtree(self.tree)
            # the OS's own message, from a real error on the same path: it's localized
            # (Chinese Windows says 系统找不到指定的路径。)
            try:
                os.scandir(self.tree).close()
            except OSError as e:
                why.append(e.strerror)

        clock = Clock()
        code = watch.watch_dir(self.tree, "debian", 1, out=self.lines.append, clock=clock,
                               sleep=Rounds(gone, clock=clock), rounds=3)
        self.assertEqual(len(why), 1)
        self.assertTrue(why[0])
        # the channel's folder gone: a closed channel's fix, with a placeholder for the
        # flags, since this member has no record, its command as this box runs vcharon;
        # then EXIT closed, even in continuous mode
        self.assertEqual((code, self.lines), (watch.EXIT_CLOSED, [
            "2026-10-01 09:05:46 " + watching(self.tree, 1),
            "2026-10-01 09:05:47 ERROR can't read %s: %s" % (self.tree, why[0]),
            "2026-10-01 09:05:47   fix: " + platform.runnable(
                "the channel is closed, or your folder in it is gone: vcharon leave mb "
                "<the --project and --role that make debian>"),
            "2026-10-01 09:05:47 EXIT closed"]))

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
        v1 = dict(good, version=1)
        for key in ("seen", "heads", "loose"):
            del v1[key]
        less = dict(good)
        del less["loose"]
        for data, why in ((b"{", "it isn't JSON"), (b"\xff", "it isn't JSON"),
                          (b"[]", "it has another shape"),
                          (other(version=3), "it has another shape"),
                          (other(version=True), "it has another shape"),
                          (other(files={"a": [1]}), "it has another shape"),
                          (other(files=bad_size), "it has another shape"),
                          (other(extra=1), "it has another shape"),
                          (json.dumps(less).encode(), "it has another shape"),
                          (other(seen={"mac": {"low": 2, "more": [2]}}), "it has another shape"),
                          (other(seen={"mac": {"low": -1, "more": []}}), "it has another shape"),
                          (other(seen={"mac": {"low": 0}}), "it has another shape"),
                          (other(heads={"mac#1": "xyz"}), "it has another shape"),
                          (other(loose=[1]), "it has another shape"),
                          (other(root="/elsewhere"), "it is for /elsewhere"),
                          (other(me="mac"), "it is for the member mac"),
                          # a snapshot from before channels, whatever else it holds
                          (json.dumps(v1).encode(), "it is from before channels (version 1): "
                                                    "a fresh start"),
                          (other(version=1), "it is from before channels (version 1): a "
                                             "fresh start")):
            with self.subTest(why=why, data=data[:30]):
                with open(path, "wb") as f:
                    f.write(data)
                # a fresh baseline: the changed mtime isn't printed
                _code, lines = self.run_dir(lambda: None)
                self.assertEqual(lines, ["note: ignoring the saved snapshot %s: %s" % (path, why),
                                         watching(self.tree, 1)])

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

    def test_a_failed_save_is_shown_once(self):
        write_tree(self.tree, {"mac/x": b"x"})

        def full(*args):
            raise OSError(28, "No space left on device")

        def made():
            write_tree(self.tree, {"mac/y": b"y"})

        with mock.patch.object(watch, "save_snapshot", full):
            _code, lines = self.run_dir(made, lambda: None)
        line = "ERROR can't save the snapshot %s: No space left on device" % self.state()
        self.assertEqual(lines, [watching(self.tree, 1), line, "new mac/y"])

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
        self.assertEqual(seen, [12, ["ERROR another watcher is running on this mailbox (%s.lock)"
                                     % self.state()], 0, 0])
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
                         ["ERROR another watcher is running on this mailbox (%s.lock)"
                          % self.state()])


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
        self.assertEqual(seen, [12, ["ERROR another watcher is running on this mailbox (%s.lock)"
                                     % self.state()]])

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
                                 "leave it out", "EXIT change"])

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
                                 "WARN " + text, "EXIT quiet 1 min"])
        self.assertEqual(code, 10)
        # gone while no watcher ran: the next start shows it, its first round clears it
        os.rmdir(os.path.join(self.tree, "Debian"))
        code, lines = self.run_dir(rounds=1)
        self.assertEqual(lines[1:], ["WARN " + text, "WARN cleared: " + text])

    @unittest.skipIf(util.folds_case(), util.FOLDS_CASE)
    def test_a_snapshot_without_warnings(self):
        # no "warnings" key (version 2 always writes it now): usable; what
        # holds now is shown in its first round
        write_tree(self.tree, {"debian/STEPS.md": b"s", "Debian/": None})
        files = watch.scan(self.tree, "debian")
        os.makedirs(os.path.dirname(self.state()), exist_ok=True)
        with open(self.state(), "w", encoding="utf-8") as f:
            json.dump({"version": 2, "root": self.tree, "me": "debian", "saved": "then",
                       "files": {k: list(v) for k, v in files.items()}, "seen": {},
                       "heads": {}, "loose": []}, f)
        _code, lines = self.run_dir(rounds=1)
        self.assertEqual(lines, [watching(self.tree, 0, ", since then"),
                                 "WARN Debian/ at the top isn't a writer's folder: clients "
                                 "leave it out"])
        # a "warnings" that isn't a list of strings is another shape
        with open(self.state(), "w", encoding="utf-8") as f:
            json.dump({"version": 2, "root": self.tree, "me": "debian", "saved": "then",
                       "files": {}, "warnings": [1], "seen": {}, "heads": {}, "loose": []}, f)
        _code, lines = self.run_dir(rounds=0)
        self.assertEqual(lines[0], "note: ignoring the saved snapshot %s: it has another "
                                   "shape" % self.state())

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

    def test_exits_after_the_first_change(self):
        self.member()
        write_tree(self.tree, {"mac/x": b"x"})

        def files():
            # files alone wake nobody; an entry announces them
            write_tree(self.tree, {"mac/y": b"y"})

        def made():
            write_tree(self.tree, {"mac/z": b"z"})
            self.post("mac", 1, "y and z", to="@debian")

        # rounds=None: a round more than the steps would fail, not hang
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                               sleep=Rounds(lambda: None, files, made),
                               until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said(), [watching(self.tree, 1), "new mac/y",
                                       "to you: mac#1 — y and z  (mac/RESULTS.md)",
                                       "new mac/z", "EXIT change"])
        # saved: the next start prints nothing old
        with open(self.state(), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(sorted(doc["files"]), ["mac/RESULTS.md", "mac/x", "mac/y", "mac/z"])
        self.assertEqual(doc["seen"], {"mac": {"low": 1, "more": []}})

    def test_quiet(self):
        self.member()
        write_tree(self.tree, {"mac/x": b"x"})
        clock = Clock()
        sleep = Rounds(*[lambda: None] * 6, clock=clock)
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=sleep,
                               clock=clock, timer=clock, until_change=True, max_minutes=1)
        self.assertEqual(code, 10)
        self.assertEqual(self.said(), [watching(self.tree, 1), "EXIT quiet 1 min"])
        self.assertEqual(sleep.seconds, [10] * 6)
        self.assertEqual(self.lines[-1][:20], "2026-10-01 09:06:46 ")

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

    def test_errors(self):
        # an error that never counted (the start's own) keeps failing: the count of failed
        # rounds in a row, from the start, ends it
        code, lines, runs, t = self.never_counted([None] * 3, max_errors=3)
        self.assertEqual(code, 11)
        self.assertEqual(runs, [1, 1, 1])
        self.assertEqual(lines, [t, watching(self.tree, 0, ", fresh start"),
                                 "EXIT error"])

    def test_a_long_block_never_ends_with_error(self):
        # the agent was woken for it: its rounds don't feed --max-errors
        d = "ERROR collision: debian/N.md and debian/n.md are the same path on Windows"
        self.saved_error(d)
        code, lines, runs = self.until_change([(1, d, {})] * 12, rounds=12, max_errors=3)
        self.assertEqual((code, lines), (0, [d]))
        # an uncounted blip inside it still counts toward the limit, and isn't reset by d
        lost = "ERROR lost: the connection closed"
        code, lines, runs = self.until_change([(1, lost, {}), (1, d, {})] * 3, max_errors=3)
        self.assertEqual((code, lines, runs), (11, [lost, d, lost, d, lost, "EXIT error"],
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
                                       "EXIT change"])
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
                                 "EXIT error"])

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
                                       "EXIT change"])
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
                                           "EXIT change"])
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
        self.assertEqual(self.said()[1:], [other, "EXIT change"])
        # and the good round after it, from the saved error too: "ok again" is a change
        self.lines = []
        run, runs = self.fake_runs([(0, None, {})])
        code = watch.watch_job("mb.windows", self.sync_args, 30, out=self.lines.append,
                               sleep=never, run=run, until_change=True, max_minutes=25)
        self.assertEqual(code, 0)
        self.assertEqual(self.said()[1:], ["ok again", "EXIT change"])
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
                                           "EXIT change"])
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
                                           "ok again", "EXIT change"])

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
                                       "EXIT error"])

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

    def test_a_snapshot_without_the_error(self):
        # saved before the error was kept, or with none pending: usable, as one with no error
        path = self.saved_error(None)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        del doc["warnings"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        loaded = watch.load_snapshot(path, self.tree, "windows")
        self.assertEqual(loaded[:6], ({}, "then", None, [], None, []))
        self.assertEqual(loaded[6].doc(), {"seen": {}, "heads": {}, "loose": []})
        # an error that isn't a string, or counted keys that aren't, is another shape
        for bad in ({"error": 1}, {"counted": "transport"}, {"counted": [1]}, {"counted": ""}):
            with self.subTest(bad=bad):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(dict(doc, **bad), f)
                self.assertEqual(watch.load_snapshot(path, self.tree, "windows")[2],
                                 "it has another shape")

    def test_null_and_empty_are_absent(self):
        path = self.saved_error(None)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        for extra in ({"error": None}, {"counted": []}, {"counted": None},
                      {"error": None, "counted": []}):
            with self.subTest(extra=extra):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(dict(doc, **extra), f)
                self.assertEqual(watch.load_snapshot(path, self.tree, "windows")[:6],
                                 ({}, "then", None, [], None, []))

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
                ("ERROR aborted: couldn't read file 12: x", "aborted")):
            with self.subTest(line=line):
                self.assertEqual(watch.error_key(line), key)

    def test_the_keys_with_the_job(self):
        # a sync names the failing job; the connection's errors and the retry and
        # too_many_deletes codes are keyed as without it, a content error keeps it
        for line, key in (
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

    def test_up_and_down_losing_the_connection_is_one_problem(self):
        up = "ERROR mailbox.up: connect: couldn't reach devbox"
        down = "ERROR mailbox.down: connect: couldn't reach devbox"
        # the second failed round in a row wakes, whichever job's line it shows
        code, lines, runs = self.until_change([(4, up, {}), (4, down, {}), (4, up, {})],
                                              rounds=3)
        self.assertEqual((code, lines, runs), (0, [up, down, "EXIT change"], [4, 4]))
        self.assertEqual(self.snapshot()["counted"], ["transport"])

    def test_an_error_saved_before_the_job_names(self):
        # a snapshot saved by an older watcher: read as ever; its content error's new text
        # is another key, one extra wake, and then no more
        old = "ERROR collision: debian/N.md and debian/n.md are the same path on Windows"
        new = "ERROR mailbox.down: collision: debian/N.md and debian/n.md are the same path on " \
              "Windows"
        self.saved_error(old)
        code, lines, runs = self.until_change([(1, new, {}), (1, new, {})], rounds=2)
        self.assertEqual((code, lines, runs), (0, [new, "EXIT change"], [1]))
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
        self.assertEqual((code, lines, runs), (0, [lost, "EXIT change"], [1, 1]))
        self.assertEqual(self.snapshot()["counted"], ["transport"])
        # a blip saved by a watcher that stopped right after it: the outage still wakes once
        self.saved_error(lost, counted=[])
        code, lines, runs = self.until_change([(1, lost, {}), (1, lost, {}), (0, None, {})])
        self.assertEqual((code, lines, runs), (0, [lost, "EXIT change"], [1, 1]))

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
                self.assertEqual((code, lines), (0, [first, second, "EXIT change"]))
                code, lines, _runs = self.until_change([(0, None, {})])
                self.assertEqual(lines, ["ok again", "EXIT change"])

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
        self.assertEqual((code, lines), (0, [a, b, "EXIT change"]))
        # restarted, still flapping: every new text printed, none a change
        code, lines, _runs = self.until_change([(4, a, {}), (1, b, {}), (4, a, {}), (1, b, {})],
                                              max_errors=2, rounds=4)
        self.assertEqual((code, lines), (0, [a, b, a, b]))
        # the next good round ends the streak, and wakes
        code, lines, _runs = self.until_change([(0, None, {})])
        self.assertEqual((code, lines), (0, ["ok again", "EXIT change"]))

    def test_a_blip_inside_a_blocked_down_wakes_nobody(self):
        d = "ERROR collision: debian/N.md and debian/n.md are the same path on Windows"
        lost = "ERROR lost: the connection closed"
        code, lines, _runs = self.until_change([(1, d, {})])
        self.assertEqual((code, lines), (0, [d, "EXIT change"]))
        code, lines, _runs = self.until_change([(1, d, {}), (1, lost, {}), (1, d, {}),
                                               (0, None, {})])
        self.assertEqual((code, lines), (0, [d, lost, d, "ok again", "EXIT change"]))

    def test_a_growing_count_wakes_once(self):
        one = "ERROR unsafe_path: debian/a: a symlink (and 1 more; see the log)"
        two = "ERROR unsafe_path: debian/a: a symlink (and 2 more; see the log)"
        code, lines, _runs = self.until_change([(1, one, {}), (1, two, {}), (1, two, {})],
                                              rounds=3)
        self.assertEqual((code, lines), (0, [one, "EXIT change"]))
        code, lines, _runs = self.until_change([(1, one, {}), (1, two, {})], rounds=2)
        self.assertEqual((code, lines), (0, [one, two]))

    def test_a_b_a_counts_each_once(self):
        # up's error can hide down's (the watcher reads vcharon's first stderr line)
        a = "ERROR unsafe_path: lnk: a symlink"
        b = "ERROR collision: debian/N.md and debian/n.md are the same path on Windows"
        self.assertEqual(self.until_change([(1, a, {})])[:2], (0, [a, "EXIT change"]))
        self.assertEqual(self.until_change([(1, a, {}), (1, b, {})])[:2],
                         (0, [a, b, "EXIT change"]))
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
        self.assertEqual(self.said()[1:], [error, "EXIT change"])
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
                                           % self.tree, "EXIT change"])

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
                                       "EXIT error"])
        self.assertEqual(code2, 11)
        self.assertEqual(self.said(again)[1:], ["new mac/y", line, "EXIT error"])
        self.assertEqual(code3, 11)
        self.assertEqual(self.said(quiet), [watching(self.tree, 2, ", fresh start"),
                                            line, "EXIT error"])

    def test_a_failed_save_doesnt_end_a_continuous_watch(self):
        self.member()
        write_tree(self.tree, {"mac/x": b"x"})

        def full(*args):
            raise OSError(28, "No space left on device")

        with mock.patch.object(watch, "save_snapshot", full):
            code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                                   sleep=Rounds(lambda: None, lambda: None), rounds=2)
        self.assertEqual(code, 0)

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
                                           "EXIT change"])

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
                                       "EXIT quiet 1 min"])
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
        # a __main__.py
        fake = os.path.join(self.tmp, "fake-vcharon")
        os.mkdir(fake)
        with open(os.path.join(fake, "__main__.py"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""
                import sys
                print("some output")
                assert sys.argv[1] == "sync", sys.argv
                if sys.argv[2] == "bad":
                    sys.stderr.write("\\nERROR lost: gone\\n  fix: run again\\n")
                    sys.exit(1)
                if sys.argv[2] == "gone":
                    # the sync's block for a closed channel, as measured, after a
                    # line that isn't an ERROR
                    sys.stderr.write("note: x\\nERROR mb.w.up: not_found: no root\\n"
                                     "  fix: the channel is closed: leave it\\n  log: l\\n")
                    sys.exit(1)
                if sys.argv[2] == "nofix":
                    sys.stderr.write("ERROR lost: gone\\n  log: l\\nERROR other: x\\n"
                                     "  fix: other's\\n")
                    sys.exit(1)
                if sys.argv[2] == "tail":
                    sys.stderr.write("ERROR connect: no\\n  | ssh: refused\\n  fix: check\\n")
                    sys.exit(4)
                if sys.argv[2] == "quiet":
                    sys.exit(4)
                if sys.argv[2] == "busy":
                    sys.stderr.write("ERROR busy: another run of busy.up is in progress\\n")
                    sys.exit(2)
                assert sys.argv[2:] == ["mb", "--project", "p"], sys.argv
                """))
        with mock.patch.object(platform, "self_argv", lambda: [sys.executable, fake]):
            self.assertEqual(watch.sync_argv(SYNC), [sys.executable, fake, "sync"] + SYNC)
            self.assertEqual(watch.sync_argv(SYNC, repeat=3),
                             [sys.executable, fake, "sync"] + SYNC + ["--repeat", "3"])
            self.assertEqual(watch.run_sync("mb.windows", SYNC), (0, None, None))
            self.assertEqual(watch.run_sync("bad", ["bad"]), (1, "ERROR lost: gone",
                                                              "run again"))
            self.assertEqual(watch.run_sync("gone", ["gone"]),
                             (1, "ERROR mb.w.up: not_found: no root",
                              "the channel is closed: leave it\nl"))
            # the first ERROR's block holds no fix: its log, never a later line's fix
            self.assertEqual(watch.run_sync("nofix", ["nofix"]),
                             (1, "ERROR lost: gone", "the job's log has the rest: l"))
            # the connection's last words come between the ERROR line and its fix
            self.assertEqual(watch.run_sync("tail", ["tail"]), (4, "ERROR connect: no", "check"))
            self.assertEqual(watch.run_sync("quiet", ["quiet"]),
                             (4, "ERROR vcharon sync of quiet exited with 4", None))
            self.assertEqual(watch.run_sync("busy", ["busy"]),
                             (2, "ERROR busy: another run of busy.up is in progress", None))

    def test_the_child_is_this_vcharon(self):
        # --no-stream's child each round: self_argv, -P and all; a binary's unpacks its own copy
        ran = []

        def run(argv, **kw):
            ran.append((argv, kw["env"]))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.object(watch.subprocess, "run", run):
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

    def test_a_stuck_vcharon_run(self):
        stuck = subprocess.TimeoutExpired(["vcharon"], watch.RUN_TIMEOUT)
        with mock.patch.object(watch.subprocess, "run", side_effect=stuck):
            self.assertEqual(watch.run_sync("mailbox", ["mb"]),
                             (1, "ERROR vcharon sync of mailbox didn't finish within 900 s",
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
        """watch_job, streaming every 2 s, over the children: (exit code, the lines without
        their time). steps: with clock, the seconds each round moves the clock on."""
        self.children = [list(c) for c in children]
        self.procs = []
        clock = clock or Clock()
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
                                   clock=clock, timer=clock, stream=True, spawn=self.spawn,
                                   **kw)
        return code, self.said(lines)

    def assert_all_stopped(self):
        for proc in self.procs:
            self.assertIsNotNone(proc.poll(), "a child still runs")

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
        self.assertEqual(lines, [watching(self.tree, 0, ", streaming every 2 s"),
                                 "WARN " + warn])
        os.remove(path)
        # it counts as a change once
        self.children = []
        code, lines = self.watch([["append", path, doc], ["out", "ROUND 0"]],
                                 until_change=True, fresh=True)
        self.assertEqual((code, lines[-2:]), (0, ["WARN " + warn, "EXIT change"]))

    def test_rounds_from_one_child(self):
        up = "ERROR mb.windows.up: unsafe_path: lnk: a symlink"
        code, lines = self.watch(
            [["out", "ROUND 0"], self.entry("debian", 2, "steps", to="@all"), ["out", "ROUND 0"],
             ["out", up], ["out", "  fix: remove or rename it"], ["out", "  log: l"],
             ["out", "ROUND 1"], ["out", "ROUND 0"]],
            rounds=4)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [watching(self.tree, 0, ", streaming every 2 s"),
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
        self.assertEqual(self.slept, [])
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
        children = ([[["err", "ERROR config: no such file"], ["exit", 3]]] + [broken] * 5
                    + [[["out", "ROUND 0"], ["exit", 1]], [["out", "ROUND 0"]]])
        code, lines = self.watch(*children, rounds=9)
        self.assertEqual(code, 0)
        # the exit before any round is a step of its own, with stderr's line; an exit right
        # after a round whose error broke the connection isn't: the round said why; an exit
        # after a good round is (a crash)
        self.assertEqual(lines[1:], ["ERROR config: no such file", connect, "ok again",
                                     "ERROR vcharon sync of mb.windows exited with 1", "ok again"])
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

    def test_max_minutes_during_the_wait(self):
        connect = "ERROR mb.windows.up: connect: no"
        clock = Clock()
        # the first round takes the clock to 50 s before the limit; the restart's wait of
        # 2 s fits, the second's 4 s doesn't
        code, lines = self.watch([["out", connect], ["out", "ROUND 4"], ["exit", 4]],
                                 [["out", connect], ["out", "ROUND 4"], ["exit", 4]],
                                 clock=clock, steps=[10, 45], max_minutes=1)
        self.assertEqual((code, lines[1:]), (watch.EXIT_QUIET, [connect, "EXIT quiet 1 min"]))
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
        self.assertEqual((code, lines[1:]), (watch.EXIT_CHANGE, [lost, "EXIT change"]))
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
            t, watching(self.tree, 0, ", fresh start, streaming every 2 s"), "EXIT error"]))
        self.assert_all_stopped()

    def test_exit_closed_from_a_streamed_round(self):
        error = "ERROR mb.windows.up: not_found: the root x doesn't exist"
        gone = channel_cmd.CHANNEL_GONE_PREFIX + "vcharon leave mb --role w"
        code, lines = self.watch([["out", "ROUND 0"], ["out", error], ["out", "  fix: " + gone],
                                  ["out", "  log: l"], ["out", "ROUND 1"], ["out", "ROUND 0"]])
        self.assertEqual((code, lines[1:]), (watch.EXIT_CLOSED, [
            error, "  fix: " + gone, "  log: l", "EXIT closed"]))
        self.assert_all_stopped()

    def test_the_child_is_stopped_on_every_way_out(self):
        # EXIT change, quiet and closed above; here a change, a Ctrl-C, a crash, and a child
        # that ignores the end of its stdin
        code, lines = self.watch([self.entry("debian", 2, "hi"), ["out", "ROUND 0"]],
                                 until_change=True)
        self.assertEqual((code, lines[-1]), (watch.EXIT_CHANGE, "EXIT change"))
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
                                 stop_wait=1)
        self.assertEqual((code, lines[-1]), (watch.EXIT_CHANGE, "EXIT change"))
        self.assert_all_stopped()
        self.assertNotEqual(self.procs[0].returncode, 0)
        self.assertLess(time.monotonic() - started, 30)

    def test_stderr_after_a_broken_round_is_its_way_out(self):
        # vcharon sync --repeat exits after a round whose error broke the connection; what it
        # says on stderr then (seen once: an abort at shutdown) is no new error
        connect = "ERROR mb.windows.up: connect: ssh couldn't reach devbox"
        _code, lines = self.watch(
            [["out", connect], ["out", "ROUND 4"], ["err", "Fatal Python error: x"],
             ["exit", 134]],
            [["out", "ROUND 0"]], rounds=2)
        self.assertEqual(lines[1:], [connect, "ok again"])
        self.assertEqual(self.slept, [2])
        self.assert_all_stopped()

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

    def test_a_note_before_the_first_round_is_no_crash_line(self):
        # stderr from before round 1 is never a later crash's error line
        _code, lines = self.watch(
            [["err", "note: skipped channels.d/x.ini"], ["pause", 0.5], ["out", "ROUND 0"],
             ["exit", 1]],
            [["out", "ROUND 0"]], rounds=3)
        self.assertEqual(lines[1:], ["ERROR vcharon sync of mb.windows exited with 1", "ok again"])

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
                self.t += 5
                return self.t

        lines = []
        self.children = [[]]
        self.procs = []
        code = watch.watch_job("mb.windows", self.sync_args, 2, out=lines.append, sleep=never,
                               clock=Clock(), timer=Ticking(), stream=True, spawn=self.spawn,
                               max_minutes=1)
        self.assertEqual((code, self.said(lines)[1:]), (watch.EXIT_QUIET, ["EXIT quiet 1 min"]))
        self.assert_all_stopped()

    def test_parse_failure_is_run_vcharons(self):
        lines = ["ERROR mb.windows.up: connect: no", "  | ssh: refused", "  fix: check",
                 "  log: l", "ERROR other"]
        self.assertEqual(watch.parse_failure(4, lines, "mb.windows"),
                         (4, "ERROR mb.windows.up: connect: no", "check\nl"))
        self.assertEqual(watch.parse_failure(0, lines, "mb.windows"), (0, None, None))
        self.assertEqual(watch.parse_failure(2, [], "mb.windows"),
                         (2, "ERROR vcharon sync of mb.windows exited with 2", None))


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
            "EXIT change"])
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
            "EXIT change"])

    def test_the_leader_is_the_records(self):
        # a server member's leader is its record's (vcharon join --local wrote it), not
        # MEMBER.md's, nor the holder of CHANNEL.md
        from vcharon import channel_cmd
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": "mac",
                                  "leader": "windows", "ssh": None, "remote": self.tree,
                                  "machine": util.TEST_MACHINE_ID, **util.record_format()})
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

    def test_8_before_7(self):
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
                                                 "EXIT change"]))
        # once: not again after a restart
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])
        # edited again while no watcher ran: told at the restart, once
        edit("— 2", "— II")
        self.assertEqual(self.run_mac(rounds=1)[1][1:], ["WARN entry windows#2 was edited"])
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])
        # the same ID twice in one file: the first stands, nothing is told
        self.post("windows", 2, "a copy", to="@mac")
        self.assertEqual(self.run_mac(rounds=1)[1][1:], [])

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
            "EXIT change"])
        # the WARN counted, the files didn't: without it, no EXIT
        os.remove(os.path.join(self.tree, "top.md"))
        self.run_mac(rounds=1)
        code, lines = self.run_mac(lambda: write_tree(self.tree, {"windows/q.patch": b"q"}),
                                   lambda: None, until_change=True, max_minutes=25)
        self.assertEqual((code, lines[1:]), (0, ["new windows/q.patch"]))

    def test_a_version_1_snapshot_is_a_fresh_start(self):
        self.post("windows", 2, "two", to="@mac")
        files = watch.scan(self.tree, "mac")
        os.makedirs(os.path.dirname(self.state(me="mac")), exist_ok=True)
        with open(self.state(me="mac"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "root": self.tree, "me": "mac", "saved": "then",
                       "files": {}, "warnings": []}, f)
        _code, lines = self.run_mac(lambda: None)
        # its files would have made every entry new: a baseline instead
        self.assertEqual(lines, ["note: ignoring the saved snapshot %s: it is from before "
                                 "channels (version 1): a fresh start" % self.state(me="mac"),
                                 watching(self.tree, len(files))])
        self.assertEqual(self.snapshot()["version"], 2)

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
        for me in ("mac", "solo", "nobody"):
            with self.subTest(me=me):
                member = os.path.join(self.tree, me, "MEMBER.md")
                with self.assertRaises(VCharonError) as cm:
                    watch.watch_dir(self.tree, me, 10, out=self.lines.append, sleep=never,
                                    rounds=0)
                self.assertEqual((cm.exception.code, cm.exception.message, cm.exception.hint), (
                    "channel", "%s isn't there: your folder in the channel holds it, once "
                    "vcharon join --local has written it" % member,
                    "ask the user: your folder in mb lost its MEMBER.md"))
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

    def test_the_hint_is_channel_cmds(self):
        from vcharon import cli
        self.assertIs(cli.CHANNEL_GONE_HINT, channel_cmd.CHANNEL_GONE_HINT)
        self.assertTrue(channel_cmd.CHANNEL_GONE_HINT.startswith(channel_cmd.CHANNEL_GONE_PREFIX))

    def test_is_gone(self):
        # by the hint's start, after runnable() and run_vcharon's log part
        self.assertTrue(watch.is_gone(self.GONE))
        self.assertTrue(watch.is_gone(self.GONE + "\n/x/mb.windows.log"))
        self.assertTrue(watch.is_gone(channel_cmd.CHANNEL_GONE_HINT % ("mb", "--project p")))
        for fix in (None, "", "the channel is closed: leave it", "vcharon channel leave mb",
                    "x " + self.GONE):
            with self.subTest(fix=fix):
                self.assertFalse(watch.is_gone(fix))

    def test_dir_a_removed_channel_folder(self):
        self.record()
        write_tree(self.tree, {"mac/x": b"x"})

        def closed():
            # close's rename: every member's next round sees the folder gone. Into a new
            # name in a fresh folder: on Windows os.rename can't replace a folder
            os.rename(self.tree, os.path.join(
                tempfile.mkdtemp(prefix=".vcharon-closed-mb-", dir=self.tmp), "mb"))

        for until_change in (True, False):
            with self.subTest(until_change=until_change):
                if not os.path.isdir(self.tree):
                    write_tree(self.tree, {"mac/x": b"x", "debian/MEMBER.md":
                                           member_md("debian", "debian")})
                del self.lines[:]
                code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                                       sleep=Rounds(closed), until_change=until_change,
                                       max_minutes=25, fresh=True)
                error = self.said()[1]
                self.assertTrue(error.startswith("ERROR can't read %s: " % self.tree), error)
                # the fix right after the ERROR line, then EXIT closed, in both modes
                self.assertEqual((code, self.said()), (
                    watch.EXIT_CLOSED, [watching(self.tree, 1, ", fresh start"), error,
                                        "  fix: " + self.GONE, "EXIT closed"]))
                with open(self.state(), encoding="utf-8") as f:
                    doc = json.load(f)
                self.assertEqual((doc["error"], doc["fix"]), (error, self.GONE))
        # a restart while it's gone: the same three lines, before any lock or snapshot
        del self.lines[:]
        os.remove(self.state())
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append, sleep=never,
                               rounds=0)
        self.assertEqual((code, self.said()), (watch.EXIT_CLOSED, [
            error, "  fix: " + self.GONE, "EXIT closed"]))
        self.assertFalse(os.path.exists(self.state()))

    def test_dir_missing_at_the_start_through_the_command(self):
        self.record()
        shutil.rmtree(self.tree)
        for argv in ([], ["--until-change"]):
            with self.subTest(argv=argv):
                code, out, err = self.cli("--role", "b", *argv, project="web")
                lines = self.said(out.splitlines())
                self.assertEqual((code, err), (13, ""))
                self.assertEqual(lines[1:], ["  fix: " + self.GONE, "EXIT closed"])
                self.assertTrue(lines[0].startswith("ERROR can't read %s: " % self.tree), lines)
        # a channel folder that's there without <me>/MEMBER.md: still refused, exit 1
        write_tree(self.tree, {"mac/x": b"x"})
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
                    watch.EXIT_CLOSED, [error, "  fix: " + self.GONE, "  log: l", "EXIT closed"]))
                # a restart from the saved snapshot: shown again, and EXIT closed again,
                # though the saved error doesn't count again
                results = [(1, error, fix), (0, None, None)]
                self.assertEqual(self.job_watch(results, 2, until_change), (
                    watch.EXIT_CLOSED, [error, "  fix: " + self.GONE, "  log: l", "EXIT closed"]))

    def test_job_another_fix_goes_on(self):
        self.sync_args = ClientModeTest.write_config(self)
        os.makedirs(self.tree, exist_ok=True)
        error = "ERROR mb.windows.up: not_found: the root x doesn't exist"
        for fix in ("restore the folder", None):
            with self.subTest(fix=fix):
                results = [(1, error, fix), (1, error, fix)]
                code, lines = self.job_watch(results, 2, False, fresh=True)
                self.assertEqual(code, 0)
                self.assertNotIn("EXIT closed", lines)
                self.assertEqual(lines[0], error)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions, which root ignores")
    def test_dir_an_unreadable_channel_folder(self):
        self.record()
        write_tree(self.tree, {"mac/x": b"x"})
        self.addCleanup(os.chmod, self.tree, 0o755)
        code = watch.watch_dir(self.tree, "debian", 10, out=self.lines.append,
                               sleep=Rounds(lambda: os.chmod(self.tree, 0)), rounds=1)
        # an error on the folder that isn't "gone": no fix line
        self.assertEqual((code, self.said()), (0, [
            watching(self.tree, 1),
            "ERROR can't read %s: Permission denied" % self.tree]))

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
        self.assertEqual(watch_it(1), (0, [error, "  fix: " + fix, "EXIT change"]))
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

    def test_the_snapshots_fix(self):
        path = self.state()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"version": 2, "root": self.tree, "me": "debian", "saved": "then", "files": {},
               "warnings": [], "seen": {}, "heads": {}, "loose": [], "error": "ERROR x"}
        for extra, fix in (({}, None), ({"fix": None}, None), ({"fix": "do y"}, "do y")):
            with self.subTest(extra=extra):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(dict(doc, **extra), f)
                loaded = watch.load_snapshot(path, self.tree, "debian")
                self.assertEqual((loaded[2], loaded[4], loaded[7]), (None, "ERROR x", fix))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(doc, fix=1), f)
        self.assertEqual(watch.load_snapshot(path, self.tree, "debian")[2],
                         "it has another shape")


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

    def test_modes_and_ctrl_c(self):
        calls = []

        def interrupted(*args, **kw):
            calls.append((args, kw))
            raise KeyboardInterrupt

        with mock.patch.object(watch, "watch_dir", interrupted):
            code, _out, err = self.cli()
            self.assertEqual((code, err), (130, "vcharon: interrupted\n"))
        with mock.patch.object(watch, "watch_job", interrupted):
            for argv in (["--every", "5"], ["--until-change", "--fresh"],
                         ["--until-change", "--max-minutes", "5", "--max-errors", "3"],
                         ["--max-minutes", "29"], ["--no-stream"],
                         ["--no-stream", "--every", "3600"], ["--every", "300"]):
                with self.subTest(argv=argv):
                    self.assertEqual(self.cli(*argv, project="p")[0], 130)
        # the local member's channel folder, from its record; the remote member's section,
        # and the sync's arguments that find it again
        plain = {"fresh": False, "until_change": False, "max_minutes": None, "max_errors": 10}
        # a remote member streams by default, every 2 s; --no-stream syncs every 30 s
        streams = dict(plain, stream=True)
        self.assertEqual(calls, [
            # a local member's watch holds another folder over the channel's limits
            ((self.tree, "debian", 10), dict(plain, folder_limits=(50 * 1000 * 1000, 1000))),
            (("mb.windows", SYNC, 5), streams),
            (("mb.windows", SYNC, 2), dict(streams, fresh=True, until_change=True,
                                           max_minutes=25)),
            (("mb.windows", SYNC, 2), dict(streams, until_change=True, max_minutes=5,
                                           max_errors=3)),
            (("mb.windows", SYNC, 2), dict(streams, max_minutes=29)),
            (("mb.windows", SYNC, 30), dict(plain, stream=False)),
            (("mb.windows", SYNC, 3600), dict(plain, stream=False)),
            (("mb.windows", SYNC, 300), streams)])

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
        for code in (0, 10, 11, 12, 13):
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

    def test_a_real_until_change_run(self):
        # as a background command runs it: a child, its lines, its exit code
        write_tree(self.tree, {"windows/x": b"x"})
        argv = [sys.executable, "-P", "-m", "vcharon", "watch", "mb", "--project", "q",
                "--every", "1", "--until-change"]
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=dict(os.environ, VCHARON_HOME=self.vcharon_home))
        try:
            first = child.stdout.readline().decode("utf-8")
            # the entry and the file at once: one rename of a folder made aside
            aside = os.path.join(self.tmp, "aside")
            write_tree(aside, {"mac/y": b"y"})
            self.post("mac", 1, "y", to="@debian", root=aside)
            os.rename(os.path.join(aside, "mac"), os.path.join(self.tree, "mac"))
            out, err = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        lines = [first.rstrip("\r\n")] + out.decode("utf-8").splitlines()
        self.assertEqual((child.returncode, self.said(lines)),
                         (0, [watching(self.tree, 1),
                              "to you: mac#1 — y  (mac/RESULTS.md)", "new mac/y",
                              "EXIT change"]), err)


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

