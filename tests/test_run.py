"""The engine through fake ssh: each case as a push and as a
pull. The "remote" end is this machine, under the fake server's home.

Set VCHARON_TEST_REPORT=1 to print the times and memory peaks of the big cases to stderr."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
import tracemalloc
import unittest
from unittest import mock

from vcharon import pathrules, proto, remote, stage
from vcharon.proto import VCharonError
from vcharon.run import Engine, Side

from tests.util import (
    FakeSshCase,
    big_file,
    fd_count,
    helper_override,
    read_tree,
    sha256,
    unblock_fifo,
    write_tree,
)

POSIX = os.name != "nt"
REPORT = bool(os.environ.get("VCHARON_TEST_REPORT"))
MIB = 1 << 20
# A run blocked on a FIFO gets a writer after this many seconds.
FIFO_DELAY = 5.0

# the round trips of a run, before bye (DESIGN, "How a sync works")
CALLS = {"push": ["sink.check", "sink.receive", "sink.commit"],
         "pull": ["source.plan", "source.send"]}

TREE = {"文档/报告 2026.txt": "季度报告\n".encode() * 100,
        "with space/a b.txt": b"spaces\n",
        "empty dir/": None,
        "empty.txt": b"",
        "run.sh": b"#!/bin/sh\necho hi\n",
        "deep/er/est.bin": bytes(range(256)) * 1200}

# starts tracemalloc when the helper starts, and adds a call that reports the peak
PEAK = """
import tracemalloc

tracemalloc.start()

def peak(h, call_id, args):
    h.ok(call_id, {"peak": tracemalloc.get_traced_memory()[1]})

_real.HANDLERS["peak"] = peak
"""

# lowers the helper's message limit to 8 KiB
TINY = """
from vcharon import proto

proto.MAX_JSON = 8192
"""

# kills the helper once 4 MiB of one file have gone through its reader: staged into its sink
# (a push), or read from its source (a pull)
DIES = """
import os

LIMIT = 4 << 20

def dying(reader):
    seen = [0]

    class Dying:
        def read(self, n):
            data = reader.read(n)
            seen[0] += len(data)
            if seen[0] > LIMIT:
                os._exit(9)
            return data

        def close(self):
            reader.close()

    return Dying()

real_receive = _real.HANDLERS["sink.receive"]
real_send = _real.HANDLERS["source.send"]

def receive(h, call_id, args):
    stage = h.sink.stage
    h.sink.stage = lambda index, reader: stage(index, dying(reader))
    return real_receive(h, call_id, args)

def send(h, call_id, args):
    open_ = h.source.open
    h.source.open = lambda index: dying(open_(index))
    return real_send(h, call_id, args)

_real.HANDLERS["sink.receive"] = receive
_real.HANDLERS["source.send"] = send
"""


# a helper whose sink.check also lists entry LIE in have
LYING_SINK = """
real_check = _real.HANDLERS["sink.check"]

def sink_check(h, call_id, args):
    ok = h.ok

    def lying(cid, result):
        result["have"] = sorted(set(result["have"]) | {LIE})
        ok(cid, result)

    h.ok = lying
    try:
        return real_check(h, call_id, args)
    finally:
        h.ok = ok

_real.HANDLERS["sink.check"] = sink_check
"""

MTIME = 1790000000.25


def spec_under(prefix, spec):
    """spec under the directory prefix, as read_tree shows it: every parent directory too."""
    tree = {prefix + "/": None}
    for rel, data in spec.items():
        parts = rel.rstrip("/").split("/")
        for k in range(1, len(parts)):
            tree[prefix + "/" + "/".join(parts[:k]) + "/"] = None
        tree[prefix + "/" + rel] = data
    return tree


class RunTest(FakeSshCase):
    def setUp(self):
        FakeSshCase.setUp(self)
        # the client's own files; the server's are under self.home
        self.local = os.path.join(self.tmp, "local")
        os.mkdir(self.local)

    def report(self, line):
        if REPORT:
            sys.stderr.write("\n  [%s] %s\n" % (self.id().rsplit(".", 1)[-1], line))

    def ends(self, direction):
        """(where the source's files are, where the sink's root is)."""
        return (self.local, self.home) if direction == "push" else (self.home, self.local)

    def setup_run(self, direction, spec):
        """A source directory src holding spec, and an empty sink root dst."""
        src_base, dst_base = self.ends(direction)
        src, dst = os.path.join(src_base, "src"), os.path.join(dst_base, "dst")
        write_tree(src, spec)
        os.mkdir(dst)
        return src, dst

    def on_server(self, path):
        # relative to the server's home, as a remote path may be written
        return os.path.relpath(path, self.home)

    def sides(self, direction, src, dst, sink="dir", **source_options):
        """Sides that copy src into dst; path with keep_name unless told otherwise."""
        options = {"path": src, "keep_name": "yes"}
        options.update(source_options)
        if direction == "push":
            return (Side("local", "path", options),
                    Side("remote", sink, {"path": self.on_server(dst)}))
        options["path"] = self.on_server(src)
        return Side("remote", "path", options), Side("local", sink, {"path": dst})

    def stage_dirs(self):
        """Every stage dir under the test's temp dir: on either end."""
        found = []
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            found += [n for n in dirnames + filenames if n.startswith(pathrules.STAGE_PREFIX)]
        return found

    def failure(self, eng, **kw):
        with self.assertRaises(VCharonError) as cm:
            eng.run(**kw)
        return cm.exception

    # a tree with Chinese names and names with spaces, byte-identical, both ways

    def tree(self, direction):
        src, dst = self.setup_run(direction, TREE)
        mtimes = {}
        for i, rel in enumerate(sorted(k for k in TREE if not k.endswith("/"))):
            # distinct microseconds
            ns = 1790000000123456000 + i * 1000001000
            os.utime(os.path.join(src, *rel.split("/")), ns=(ns, ns))
            mtimes[rel] = ns
        if POSIX:
            os.chmod(os.path.join(src, "run.sh"), 0o755)
        eng = self.engine(*self.sides(direction, src, dst))
        done = eng.run()
        self.assertEqual(read_tree(dst), spec_under("src", TREE))
        for rel, ns in mtimes.items():
            got = os.stat(os.path.join(dst, "src", *rel.split("/"))).st_mtime_ns
            self.assertLess(abs(got - ns), 1000, rel)
        if POSIX:
            self.assertTrue(os.stat(os.path.join(dst, "src", "run.sh")).st_mode & 0o100)
            self.assertFalse(os.stat(os.path.join(dst, "src", "empty.txt")).st_mode & 0o111)
        self.assertEqual(sorted(done.written), sorted(k.rstrip("/")
                                                      for k in spec_under("src", TREE)))
        self.assertEqual((done.deleted, done.notes), (0, []))
        self.assertEqual(self.calls, CALLS[direction])
        self.assertEqual(self.stage_dirs(), [])
        # the source is as it was
        self.assertEqual(spec_under("src", read_tree(src)), spec_under("src", TREE))

    def test_tree_push(self):
        self.tree("push")

    def test_tree_pull(self):
        self.tree("pull")

    # a 200 MB file, the tracemalloc peaks under 64 MiB on both ends

    def big(self, direction):
        src_base, dst_base = self.ends(direction)
        src, dst = os.path.join(src_base, "big.bin"), os.path.join(dst_base, "dst")
        os.mkdir(dst)
        big_file(src, 200 * 1000 * 1000)
        want = sha256(src)
        eng = self.engine(*self.sides(direction, src, dst), extra_modules=helper_override(PEAK))
        tracemalloc.start()
        try:
            started = time.monotonic()
            done = eng.run()
            seconds = time.monotonic() - started
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        helper_peak = eng.session.call("peak")["peak"]
        self.report("200 MB in %.1f s; controller peak %.1f MiB, helper peak %.1f MiB"
                    % (seconds, peak / MIB, helper_peak / MIB))
        self.assertEqual(done.written, ["big.bin"])
        self.assertEqual(sha256(os.path.join(dst, "big.bin")), want)
        self.assertLess(peak, 64 * MIB)
        self.assertLess(helper_peak, 64 * MIB)
        self.assertEqual(self.stage_dirs(), [])

    def test_big_file_push(self):
        self.big("push")

    def test_big_file_pull(self):
        self.big("pull")

    # 10,000 small files in 100 directories, in the same round trips as 3 files

    def many(self, direction, count, dirs):
        spec = {"d%03d/f%05d.txt" % (i % dirs, i): b"file %d\n" % i for i in range(count)}
        src, dst = self.setup_run(direction, spec)
        eng = self.engine(*self.sides(direction, src, dst))
        started = time.monotonic()
        done = eng.run()
        seconds = time.monotonic() - started
        self.assertEqual(read_tree(dst), spec_under("src", spec))
        self.assertEqual(len(done.written), 1 + dirs + count)
        shutil.rmtree(src)
        shutil.rmtree(dst)
        return seconds

    def many_both(self, direction):
        seconds = self.many(direction, 10000, 100)
        self.report("10,000 files in %.1f s" % seconds)
        calls = self.calls
        self.assertEqual(calls, CALLS[direction])
        self.many(direction, 3, 3)
        self.assertEqual(self.calls, calls)

    def test_many_files_push(self):
        self.many_both("push")

    def test_many_files_pull(self):
        self.many_both("pull")

    # a dry run changes nothing

    def dry_run(self, direction):
        src, dst = self.setup_run(direction, TREE)
        write_tree(dst, {"old.txt": b"o"})
        before = (read_tree(dst), os.stat(dst).st_mtime_ns)
        seen = []
        eng = self.engine(*self.sides(direction, src, dst),
                          after_check=lambda p, checked: seen.append((p, checked)))
        self.assertIsNone(eng.run(dry_run=True))
        self.assertEqual(seen, [(eng.plan, eng.checked)])
        self.assertEqual(len(eng.plan.entries), len(spec_under("src", TREE)))
        self.assertEqual(eng.checked.root, os.path.realpath(dst))
        self.assertIsNone(eng.done)
        self.assertEqual(self.calls, CALLS[direction][:1])
        self.assertEqual((read_tree(dst), os.stat(dst).st_mtime_ns), before)
        self.assertEqual(self.stage_dirs(), [])
        # create on a missing root leaves it missing
        new = os.path.join(os.path.dirname(dst), "new")
        source, sink = self.sides(direction, src, os.path.join(new, "root"))
        sink.options["create"] = "yes"
        eng = self.engine(source, sink)
        self.assertIsNone(eng.run(dry_run=True))
        self.assertFalse(os.path.exists(new))

    def test_dry_run_push(self):
        self.dry_run("push")

    def test_dry_run_pull(self):
        self.dry_run("pull")

    # the helper killed mid-transfer leaves the root as it was, apart from its stage dir

    def killed(self, direction):
        src, dst = self.setup_run(direction, {"a.txt": b"a"})
        big_file(os.path.join(src, "big.bin"), 16 * MIB)
        write_tree(dst, {"kept.txt": b"k", "src/old.txt": b"o"})
        before = read_tree(dst)
        eng = self.engine(*self.sides(direction, src, dst), extra_modules=helper_override(DIES))
        e = self.failure(eng)
        self.assertEqual(e.code, "lost")
        after = read_tree(dst)
        if direction == "push":
            # the helper died, so nobody removed its stage dir; the next run does
            stage = [k for k in after if k.startswith(pathrules.STAGE_PREFIX) and k.count("/") == 1
                     and k.endswith("/")]
            self.assertEqual(len(stage), 1, sorted(after))
            self.assertEqual({k: v for k, v in after.items() if not k.startswith(stage[0])},
                             before)
        else:
            # the local sink was aborted
            self.assertEqual(after, before)
            self.assertEqual(self.stage_dirs(), [])

    def test_helper_killed_push(self):
        self.killed("push")

    def test_helper_killed_pull(self):
        self.killed("pull")

    # an unreadable subdirectory fails the plan, and no bytes move

    def unreadable(self, direction):
        if os.geteuid() == 0:
            self.skipTest("root can read every directory")
        src, dst = self.setup_run(direction, {"ok/f": b"f", "locked/g": b"g"})
        locked = os.path.join(src, "locked")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        e = self.failure(self.engine(*self.sides(direction, src, dst)))
        self.assertEqual(e.code, "permission")
        self.assertEqual(e.message, "can't list %s: Permission denied" % locked)
        self.assertEqual(read_tree(dst), {})
        self.assertNotIn("sink.receive", self.calls)
        self.assertNotIn("source.send", self.calls)
        self.assertNotIn("sink.check", self.calls)

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_unreadable_subdirectory_push(self):
        self.unreadable("push")

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_unreadable_subdirectory_pull(self):
        self.unreadable("pull")

    # a FIFO or a symlink swapped in after the plan isn't followed

    def swapped(self, direction, how, stops):
        src, dst = self.setup_run(direction, {"a/f.txt": b"inside", "b.txt": b"inside too",
                                              "c.txt": b"c"})
        outside = os.path.join(self.tmp, "outside")
        write_tree(outside, {"f.txt": b"OUTSIDE", "b.txt": b"OUTSIDE"})

        swapped_at = []

        def after_check(p, checked):
            b = os.path.join(src, "b.txt")
            if how == "fifo":
                os.remove(b)
                os.mkfifo(b)
                stops.append(unblock_fifo(self, b, delay=FIFO_DELAY))
            elif how == "file link":
                os.remove(b)
                os.symlink(os.path.join(outside, "b.txt"), b)
            else:
                os.rename(os.path.join(src, "a"), os.path.join(src, "a2"))
                os.symlink(outside, os.path.join(src, "a"))
            swapped_at.append(time.monotonic())

        eng = self.engine(*self.sides(direction, src, dst), after_check=after_check)
        e = self.failure(eng)
        # never blocked on the FIFO: its writer would have come by now
        self.assertLess(time.monotonic() - swapped_at[0], FIFO_DELAY)
        self.assertEqual(e.code, "vanished", e.message)
        gone = "src/a/f.txt" if how == "dir link" else "src/b.txt"
        self.assertEqual(e.message, "%s is gone, or isn't a regular file any more" % gone)
        # nothing committed, nothing from outside, and the engine aborted the sink
        self.assertIsNone(eng.done)
        self.assertEqual(read_tree(dst), {})
        self.assertEqual(self.stage_dirs(), [])
        self.assertNotIn("sink.commit", self.calls)

    def swaps(self, direction):
        for how in ("fifo", "file link", "dir link"):
            with self.subTest(how=how):
                stops = []
                try:
                    self.swapped(direction, how, stops)
                finally:
                    # even after a failure: the FIFO's writer ends with its case, and the
                    # next case's write_tree would block on a FIFO left behind
                    for stop in stops:
                        stop()
                    for base, name in zip(self.ends(direction), ("src", "dst")):
                        shutil.rmtree(os.path.join(base, name), ignore_errors=True)

    @unittest.skipUnless(POSIX, "needs symlinks and FIFOs")
    def test_swapped_after_the_plan_push(self):
        self.swaps("push")

    @unittest.skipUnless(POSIX, "needs symlinks and FIFOs")
    def test_swapped_after_the_plan_pull(self):
        self.swaps("pull")

    # a commit that fails partway reports what it wrote

    def commit_fails(self, direction):
        src, dst = self.setup_run(direction, {"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"})

        def after_check(p, checked):
            # a directory where a later file put goes
            os.makedirs(os.path.join(dst, "src", "b.txt", "inner"))

        eng = self.engine(*self.sides(direction, src, dst), after_check=after_check)
        e = self.failure(eng)
        self.assertEqual(e.code, "kind_change")
        self.assertEqual(e.message, "src/b.txt is a directory at the target, but the plan puts "
                                    "a file there")
        # for a push, from the err reply's done
        self.assertEqual(eng.done.written, ["src", "src/a.txt"])
        self.assertEqual(eng.done.deleted, 0)
        self.assertEqual(read_tree(dst), {"src/": None, "src/a.txt": b"a", "src/b.txt/": None,
                                          "src/b.txt/inner/": None})
        self.assertEqual(self.stage_dirs(), [])

    def test_commit_fails_partway_push(self):
        self.commit_fails("push")

    def test_commit_fails_partway_pull(self):
        self.commit_fails("pull")

    # directories only: nothing to stream, and the commit still runs

    def dirs_only(self, direction):
        src, dst = self.setup_run(direction, {"a/b/": None, "c/": None})
        eng = self.engine(*self.sides(direction, src, dst))
        done = eng.run()
        self.assertEqual(read_tree(dst), {"src/": None, "src/a/": None, "src/a/b/": None,
                                          "src/c/": None})
        self.assertEqual(sorted(done.written), ["src", "src/a", "src/a/b", "src/c"])
        # no sink.receive or source.send
        self.assertEqual(self.calls, {"push": ["sink.check", "sink.commit"],
                                      "pull": ["source.plan"]}[direction])
        self.assertEqual(self.stage_dirs(), [])

    def test_dirs_only_push(self):
        self.dirs_only("push")

    def test_dirs_only_pull(self):
        self.dirs_only("pull")

    # the helper checks every call

    def test_helper_call_checks(self):
        write_tree(os.path.join(self.home, "src"), {"d/f": b"f"})
        good = {"plugin": "path", "options": {"path": "src"}, "state": None, "full": False}
        plan_first = [("source.plan", good, {})]

        def stage(index, reader):
            reader.read()

        cases = [
            ("sink.receive before sink.check", [], ("sink.receive", {"indexes": []},
                                                    {"upload": []})),
            ("sink.commit before sink.check", [], ("sink.commit", {}, {})),
            ("source.send before source.plan", [], ("source.send", {"indexes": []},
                                                    {"receive": ([], stage)})),
            ("source.send with a dir put", plan_first,
             ("source.send", {"indexes": [0]}, {"receive": ([0], stage)})),
            ("source.send with a repeat", plan_first,
             ("source.send", {"indexes": [1, 1]}, {"receive": ([1, 1], stage)})),
            ("source.send out of range", plan_first,
             ("source.send", {"indexes": [-1]}, {"receive": ([-1], stage)})),
            ("a second source.plan", plan_first, ("source.plan", good, {})),
            ("options that aren't strings", [],
             ("source.plan", dict(good, options={"path": 1}), {})),
            ("options that aren't a dict", [], ("source.plan", dict(good, options=["x"]), {})),
            ("a missing state", [], ("source.plan", {"plugin": "path",
                                                      "options": {"path": "src"}}, {})),
            ("an extra key", [], ("source.plan", dict(good, extra=1), {})),
            ("indexes that aren't ids", plan_first,
             ("source.send", {"indexes": [True]}, {"receive": ([], stage)})),
            ("a malformed plan", [], ("sink.check", {"plugin": "dir", "options": {"path": "x"},
                                                     "plan": {"entries": "x"}}, {})),
            ("a missing full", [], ("source.plan", {"plugin": "path", "options": {"path": "src"},
                                                    "state": None}, {})),
            ("a full that isn't a bool", [], ("source.plan", dict(good, full=1), {})),
            ("source.state_after before source.plan", [],
             ("source.state_after", {"written": [], "deleted": []}, {})),
            ("a written that isn't a list", plan_first,
             ("source.state_after", {"written": "d/f", "deleted": []}, {})),
            ("a written that isn't strings", plan_first,
             ("source.state_after", {"written": ["d", 1], "deleted": []}, {})),
            ("a missing deleted", plan_first, ("source.state_after", {"written": []}, {})),
            ("a deleted that isn't strings", plan_first,
             ("source.state_after", {"written": [], "deleted": [None]}, {})),
            ("a second source.state_after",
             plan_first + [("source.state_after", {"written": [], "deleted": []}, {})],
             ("source.state_after", {"written": [], "deleted": []}, {})),
        ]
        for what, first, (fn, args, kw) in cases:
            with self.subTest(what=what):
                s = self.session()
                s.open()
                for call in first:
                    s.call(call[0], call[1], **call[2])
                with self.assertRaises(VCharonError) as cm:
                    s.call(fn, args, **kw)
                self.assertEqual(cm.exception.code, "protocol", cm.exception.message)
                s.close()
                # the helper stops after a protocol error
                self.assertEqual(s.ssh_exit, 3)

    def test_helper_removes_its_stage_dir_when_it_exits(self):
        # after bye, and at end of file, without any sink.abort
        dst = os.path.join(self.home, "dst")
        os.mkdir(dst)
        args = {"plugin": "dir", "options": {"path": "dst"},
                "plan": {"entries": [{"op": "put", "path": "x", "kind": "file", "size": 1,
                                      "mtime": 1790000000.0}]}}
        for how in ("bye", "end of file"):
            with self.subTest(how=how):
                s = self.session()
                s.open()
                s.call("sink.check", args)
                s.call("sink.receive", {"indexes": [0]}, upload=[(0, lambda: io.BytesIO(b"x"))])
                self.assertEqual(len(self.stage_dirs()), 1)
                if how == "end of file":
                    # no bye: stdin just closes
                    s._healthy = False
                s.close()
                self.assertEqual(s.ssh_exit, 0)
                self.assertEqual(read_tree(dst), {})

    # job.reset between two jobs on one connection

    def test_job_reset(self):
        write_tree(os.path.join(self.home, "src"), {"f": b"f"})
        dst = os.path.join(self.home, "dst")
        os.mkdir(dst)
        plan_args = {"plugin": "path", "options": {"path": "src"}, "state": None, "full": False}
        check_args = {"plugin": "dir", "options": {"path": "dst"},
                      "plan": {"entries": [{"op": "put", "path": "x", "kind": "file", "size": 1,
                                            "mtime": 1790000000.0}]}}
        s = self.session()
        s.open()
        # before anything was planned, and twice: fine
        self.assertEqual(s.call("job.reset"), {})
        self.assertEqual(s.call("job.reset"), {})
        # a sink that staged and never committed: the reset drops its stage dir
        s.call("sink.check", check_args)
        s.call("sink.receive", {"indexes": [0]}, upload=[(0, lambda: io.BytesIO(b"x"))])
        self.assertEqual(len(self.stage_dirs()), 1)
        s.call("source.plan", plan_args)
        self.assertEqual(s.call("job.reset"), {})
        self.assertEqual(self.stage_dirs(), [])
        self.assertEqual(read_tree(dst), {})
        # the next job may plan and check again, and send what it planned
        self.assertEqual(s.call("source.plan", plan_args)["entries"][0]["path"], "f")
        s.call("sink.check", check_args)
        out = []
        s.call("source.send", {"indexes": [0]},
               receive=([0], lambda i, reader: out.append(reader.read())))
        self.assertEqual(out, [b"f"])
        s.call("job.reset")
        # a reset takes no args
        with self.assertRaises(VCharonError) as cm:
            s.call("job.reset", {"x": 1})
        self.assertEqual(cm.exception.code, "protocol")
        s.close()
        self.assertEqual(s.ssh_exit, 3)
        # right after a reset, the last job's source and sink are gone: sending or receiving
        # is out of order
        refused = (("source.send", {"indexes": [0]}, {"receive": ([0], lambda i, r: r.read())}),
                   ("sink.receive", {"indexes": [0]},
                    {"upload": [(0, lambda: io.BytesIO(b"x"))]}),
                   ("sink.commit", {}, {}))
        for fn, args, kw in refused:
            with self.subTest(fn=fn):
                s = self.session()
                s.open()
                s.call("source.plan", plan_args)
                s.call("sink.check", check_args)
                s.call("job.reset")
                with self.assertRaises(VCharonError) as cm:
                    s.call(fn, args, **kw)
                self.assertEqual(cm.exception.code, "protocol", cm.exception.message)
                self.assertIn("came before", cm.exception.message)
                s.close()

    def test_job_reset_after_a_failed_commit(self):
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("needs POSIX modes, and a user other than root")
        # a remote sink whose commit failed partway: its stage dir is gone before the next
        # job plans, and the next job's check is accepted
        dst = os.path.join(self.home, "dst")
        write_tree(dst, {"ro/": None})
        os.chmod(os.path.join(dst, "ro"), 0o555)
        self.addCleanup(os.chmod, os.path.join(dst, "ro"), 0o755)
        src = os.path.join(self.local, "src")
        write_tree(src, {"a": b"a", "ro/b": b"b"})
        s = self.session()
        s.open()
        first = Engine(s, Side("local", "path", {"path": src}),
                       Side("remote", "dir", {"path": "dst"}), self.log)
        self.assertEqual(self.failure(first).code, "permission")
        self.assertTrue(s.usable)
        remote.reset(s)
        self.assertEqual(self.stage_dirs(), [])
        os.chmod(os.path.join(dst, "ro"), 0o755)
        second = Engine(s, Side("local", "path", {"path": src}),
                        Side("remote", "dir", {"path": "dst"}), self.log)
        second.run()
        self.assertEqual(read_tree(dst), read_tree(src))
        self.assertEqual(self.stage_dirs(), [])

    def test_bad_options_keep_the_session(self):
        write_tree(os.path.join(self.home, "src"), {"f": b"f"})
        s = self.session()
        s.open()
        with self.assertRaises(VCharonError) as cm:
            s.call("source.plan", {"plugin": "path", "options": {"pth": "src"}, "state": None,
                                   "full": False})
        self.assertEqual(cm.exception.code, "bad_options")
        with self.assertRaises(VCharonError) as cm:
            s.call("sink.check", {"plugin": "nope", "options": {}, "plan": {"entries": []}})
        self.assertEqual(cm.exception.code, "config")
        self.assertTrue(s.usable)
        self.assertEqual(s.echo(b"still in step"), b"still in step")
        s.close()
        self.assertEqual(s.ssh_exit, 0)
        self.assertFalse(s.usable)

    # a plan over the message limit: too_big, and the session stays in step

    def test_plan_too_big_pull(self):
        src = os.path.join(self.home, "src")
        write_tree(src, {"f%03d.txt" % i: b"x" for i in range(300)})
        dst = os.path.join(self.local, "dst")
        os.mkdir(dst)
        eng = self.engine(*self.sides("pull", src, dst), extra_modules=helper_override(TINY))
        e = self.failure(eng)
        self.assertEqual((e.code, e.exit_code), ("too_big", 1))
        self.assertRegex(e.message, r"\Aa message of [0-9.]+ MiB is over vcharon's [0-9.]+ MiB "
                                    r"limit\Z")
        self.assertEqual(e.hint, "the plan is too big: exclude part of the tree, or copy it in "
                                 "parts")
        self.assertTrue(eng.session.usable)
        self.assertEqual(eng.session.echo(b"still in step"), b"still in step")
        self.assertEqual(read_tree(dst), {})

    def test_plan_too_big_push(self):
        src, dst = self.setup_run("push", {"f%03d.txt" % i: b"x" for i in range(300)})
        eng = self.engine(*self.sides("push", src, dst))
        with mock.patch.object(proto, "MAX_JSON", 8192):
            e = self.failure(eng)
        self.assertEqual(e.code, "too_big")
        # sink.check never went out; the abort after it did
        self.assertEqual(self.calls, ["sink.check", "sink.abort"])
        self.assertTrue(eng.session.usable)
        self.assertEqual(eng.session.echo(b"still in step"), b"still in step")
        self.assertEqual(read_tree(dst), {})

    # no fd leak in the controller

    @unittest.skipUnless(os.path.isdir("/dev/fd"), "needs /dev/fd")
    def test_no_fd_leak(self):
        before = fd_count()
        for direction in ("push", "pull"):
            src, dst = self.setup_run(direction, TREE)
            eng = self.engine(*self.sides(direction, src, dst))
            eng.run()
            eng.session.close()
        self.assertEqual(fd_count(), before)

    # runs that keep state (DESIGN, "The path source", "Full syncs")

    def job_sides(self, direction, src, dst, **options):
        """A job's sides: path, without keep_name, onto dir."""
        return self.sides(direction, src, dst, keep_name="no", **options)

    def job_run(self, direction, src, dst, state, full=False, **options):
        """One run of the engine with a state, as vcharon run does; the engine."""
        eng = self.engine(*self.job_sides(direction, src, dst, **options))
        eng.run(state=state, full=full)
        return eng

    def state_of(self, eng):
        # as the state file gives it back
        return json.loads(json.dumps(eng.plan.state))

    def moved(self, eng):
        """The paths whose bytes the transfer moved, from the call args."""
        for fn, args in self.call_args:
            if fn in ("sink.receive", "source.send"):
                return sorted(eng.plan.entries[i].path for i in args["indexes"])
        return []

    def snapshot(self, root):
        """The tree under root, with every mtime and mode, and the root's own mtime (a stage
        dir made and removed changes it)."""
        tree = read_tree(root)
        stats = {rel: os.lstat(os.path.join(root, *rel.rstrip("/").split("/")))
                 for rel in tree}
        return (tree, {rel: (st.st_mtime_ns, st.st_mode) for rel, st in stats.items()},
                os.stat(root).st_mtime_ns)

    def set_mtimes(self, root, mtime):
        for rel in read_tree(root):
            if not rel.endswith("/"):
                os.utime(os.path.join(root, *rel.split("/")), (mtime, mtime))

    def assert_same_files(self, src, dst):
        """dst holds src's tree, each file with src's mtime (to the microsecond) and exec
        bit."""
        self.assertEqual(read_tree(dst), read_tree(src))
        for rel in read_tree(src):
            if rel.endswith("/"):
                continue
            a = os.stat(os.path.join(src, *rel.split("/")))
            b = os.stat(os.path.join(dst, *rel.split("/")))
            self.assertLess(abs(a.st_mtime_ns - b.st_mtime_ns), 1000, rel)
            if POSIX:
                self.assertEqual(bool(a.st_mode & 0o100), bool(b.st_mode & 0o100), rel)

    # a second run with nothing changed plans nothing and moves no file bytes

    def nothing_changed(self, direction):
        src, dst = self.setup_run(direction, TREE)
        state = self.state_of(self.job_run(direction, src, dst, {}))
        before = self.snapshot(dst)
        eng = self.engine(*self.job_sides(direction, src, dst))
        done = eng.run(state=state)
        self.assertEqual((eng.plan.entries, eng.plan.state), ([], state))
        self.assertEqual(done, stage.Done([], 0, []))
        # no transfer and no commit
        self.assertEqual(self.calls, {"push": ["sink.check"], "pull": ["source.plan"]}[direction])
        self.assertEqual(self.snapshot(dst), before)
        self.assertEqual(self.stage_dirs(), [])

    def test_nothing_changed_push(self):
        self.nothing_changed("push")

    def test_nothing_changed_pull(self):
        self.nothing_changed("pull")

    # a file whose size, mtime or execute bit changed is sent again, and so is one that
    # a revert restored

    def only_changes(self, direction):
        spec = {"size.txt": b"1", "mtime.txt": b"m", "exec.sh": b"#!/bin/sh\n",
                "revert.txt": b"original", "same.txt": b"s", "d/deep.txt": b"d"}
        src, dst = self.setup_run(direction, spec)
        self.set_mtimes(src, MTIME)
        state = self.state_of(self.job_run(direction, src, dst, {}))
        write_tree(src, {"size.txt": b"22", "revert.txt": b"original"})
        os.utime(os.path.join(src, "size.txt"), (MTIME, MTIME))
        os.utime(os.path.join(src, "mtime.txt"), (MTIME + 5, MTIME + 5))
        # a revert writes the same bytes back, with a new mtime
        os.utime(os.path.join(src, "revert.txt"), (MTIME + 60, MTIME + 60))
        want = ["mtime.txt", "revert.txt", "size.txt"]
        if POSIX:
            os.chmod(os.path.join(src, "exec.sh"), 0o755)
            want.insert(0, "exec.sh")
        eng = self.job_run(direction, src, dst, state)
        self.assertEqual([e.path for e in eng.plan.entries], want)
        self.assertEqual(self.moved(eng), want)
        self.assert_same_files(src, dst)

    def test_only_changes_are_sent_push(self):
        self.only_changes("push")

    def test_only_changes_are_sent_pull(self):
        self.only_changes("pull")

    # a first --full run onto a copy sends only what differs, and gives the rest the
    # source's mtime

    def full_onto_a_copy(self, direction):
        spec = {"a.txt": b"aaa", "same-size.txt": b"1234", "other-size.txt": b"xyz",
                "run.sh": b"#!/bin/sh\n", "d/deep.bin": bytes(range(256)) * 100,
                "empty dir/": None}
        src, dst = self.setup_run(direction, spec)
        write_tree(dst, dict(spec, **{"same-size.txt": b"5678", "other-size.txt": b"xy"}))
        self.set_mtimes(dst, MTIME - 1000)
        for i, rel in enumerate(sorted(k for k in spec if not k.endswith("/"))):
            os.utime(os.path.join(src, *rel.split("/")), (MTIME + i, MTIME + i))
        if POSIX:
            os.chmod(os.path.join(src, "run.sh"), 0o755)
        inode = os.stat(os.path.join(dst, "d", "deep.bin")).st_ino
        eng = self.job_run(direction, src, dst, {}, full=True)
        self.assertEqual(self.moved(eng), ["other-size.txt", "same-size.txt"])
        self.assertEqual(len(eng.checked.have), 3)
        # have files count as written
        self.assertEqual(sorted(eng.done.written), sorted(e.path for e in eng.plan.entries))
        self.assert_same_files(src, dst)
        # touched in place, not replaced
        self.assertEqual(os.stat(os.path.join(dst, "d", "deep.bin")).st_ino, inode)
        self.assertIn("transfer: 2 files, 7 bytes in ", self.log_text())
        self.assertIn("check: root %s, 0 deletes, 3 already there" % os.path.realpath(dst),
                      self.log_text())
        # a normal run after it sends nothing
        eng = self.job_run(direction, src, dst, self.state_of(eng))
        self.assertEqual(eng.plan.entries, [])

    def test_full_onto_a_copy_push(self):
        self.full_onto_a_copy("push")

    def test_full_onto_a_copy_pull(self):
        self.full_onto_a_copy("pull")

    # --full repairs a file edited or removed at the target

    def full_repairs(self, direction):
        spec = {"edited.txt": b"12345", "truncated.txt": b"abcdef", "removed.txt": b"r",
                "fine.txt": b"f", "d/fine.txt": b"f2"}
        src, dst = self.setup_run(direction, spec)
        state = self.state_of(self.job_run(direction, src, dst, {}))
        write_tree(dst, {"edited.txt": b"54321", "truncated.txt": b"abc"})
        os.remove(os.path.join(dst, "removed.txt"))
        # vcharon trusts the target between runs
        eng = self.job_run(direction, src, dst, state)
        self.assertEqual(eng.plan.entries, [])
        eng = self.job_run(direction, src, dst, state, full=True)
        self.assertEqual(self.moved(eng), ["edited.txt", "removed.txt", "truncated.txt"])
        self.assert_same_files(src, dst)
        self.assertEqual(self.state_of(eng), state)

    def test_full_repairs_push(self):
        self.full_repairs("push")

    def test_full_repairs_pull(self):
        self.full_repairs("pull")

    # prune deletes only paths it sent, and never a directory that holds others' files

    def prune_only_sent(self, direction):
        spec = {"keep.txt": b"k", "gone.txt": b"g", "d/gone.txt": b"g", "d2/x.txt": b"x"}
        src, dst = self.setup_run(direction, spec)
        state = self.state_of(self.job_run(direction, src, dst, {}, prune="yes"))
        # added at the target by someone else
        write_tree(dst, {"mine.txt": b"m", "d2/mine.txt": b"m"})
        os.remove(os.path.join(src, "gone.txt"))
        shutil.rmtree(os.path.join(src, "d"))
        shutil.rmtree(os.path.join(src, "d2"))
        eng = self.job_run(direction, src, dst, state, prune="yes")
        self.assertEqual([(e.op, e.path) for e in eng.plan.entries],
                         [("delete", p) for p in ("d", "d/gone.txt", "d2", "d2/x.txt",
                                                  "gone.txt")])
        self.assertEqual(eng.checked.notes, ["kept d2: it isn't empty"])
        self.assertEqual(eng.done.notes, ["kept d2: it isn't empty"])
        self.assertEqual(eng.done.deleted, 4)
        self.assertEqual(read_tree(dst), {"keep.txt": b"k", "mine.txt": b"m", "d2/": None,
                                          "d2/mine.txt": b"m"})
        self.assertEqual(eng.plan.state, {"sent": {"keep.txt": state["sent"]["keep.txt"]}})

    def test_prune_deletes_only_what_it_sent_push(self):
        self.prune_only_sent("push")

    def test_prune_deletes_only_what_it_sent_pull(self):
        self.prune_only_sent("pull")

    # a commit that fails partway saves only what state_after returns, and a
    # run after it reaches the right result

    def failed_commit(self, direction):
        if os.geteuid() == 0:
            self.skipTest("root can write in a read-only directory")
        src, dst = self.setup_run(direction, {"top.txt": b"t", "b/": None})
        old = self.state_of(self.job_run(direction, src, dst, {}))
        write_tree(src, {"a/1": b"1", "a/2": b"2", "b/1": b"b1", "b/2": b"b2"})
        b = os.path.join(dst, "b")
        os.chmod(b, 0o555)
        self.addCleanup(os.chmod, b, 0o755)
        eng = self.engine(*self.job_sides(direction, src, dst))
        with self.assertRaises(VCharonError) as cm:
            eng.run(state=old)
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual(eng.done.written, ["a", "a/1", "a/2"])
        if direction == "pull":
            self.assertIn("source.state_after", self.calls)
        values = {e.path: e for e in eng.plan.entries}
        want = dict(old["sent"], **{"a": "d", "a/1": [1, values["a/1"].mtime,
                                                      values["a/1"].executable],
                                    "a/2": [1, values["a/2"].mtime, values["a/2"].executable]})
        self.assertEqual(eng.state_after, {"sent": want})
        self.assertIn("state_after: 3 written, 0 deleted", self.log_text())
        # with b/ writable, only what's missing moves
        os.chmod(b, 0o755)
        eng = self.job_run(direction, src, dst, json.loads(json.dumps(eng.state_after)))
        self.assertEqual(self.moved(eng), ["b/1", "b/2"])
        self.assert_same_files(src, dst)
        self.assertEqual(self.stage_dirs(), [])

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_a_failed_commit_saves_what_it_wrote_push(self):
        self.failed_commit("push")

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_a_failed_commit_saves_what_it_wrote_pull(self):
        self.failed_commit("pull")

    # a failed commit's state never lists what it removed

    def restored_after_a_failed_prune(self, direction):
        # x.txt goes to the trash, a commit deletes it at the target and then fails;
        # x.txt comes back with its old bytes and mtime, and must be sent again
        if os.geteuid() == 0:
            self.skipTest("root can write in a read-only directory")
        src, dst = self.setup_run(direction, {"x.txt": b"precious", "b/": None})
        os.utime(os.path.join(src, "x.txt"), (MTIME, MTIME))
        state = self.state_of(self.job_run(direction, src, dst, {}, prune="yes"))
        trash = os.path.join(self.tmp, "trash-x.txt")
        os.rename(os.path.join(src, "x.txt"), trash)
        write_tree(src, {"a.txt": b"a", "b/new.txt": b"n"})
        b = os.path.join(dst, "b")
        os.chmod(b, 0o555)
        self.addCleanup(os.chmod, b, 0o755)
        eng = self.engine(*self.job_sides(direction, src, dst, prune="yes"))
        with self.assertRaises(VCharonError) as cm:
            eng.run(state=state)
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual((eng.done.written, eng.done.deletes_done), (["a.txt"], ["x.txt"]))
        self.assertEqual(sorted(eng.state_after["sent"]), ["a.txt", "b"])
        self.assertFalse(os.path.exists(os.path.join(dst, "x.txt")))
        os.rename(trash, os.path.join(src, "x.txt"))
        os.chmod(b, 0o755)
        eng = self.job_run(direction, src, dst, json.loads(json.dumps(eng.state_after)),
                           prune="yes")
        self.assertEqual(self.moved(eng), ["b/new.txt", "x.txt"])
        self.assert_same_files(src, dst)

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_restored_after_a_failed_prune_push(self):
        self.restored_after_a_failed_prune("push")

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_restored_after_a_failed_prune_pull(self):
        self.restored_after_a_failed_prune("pull")

    def failed_delete_phase(self, direction):
        # an unlink in a read-only directory fails: the deletes done before it leave sent, and
        # a state is saved although nothing was written; the rest go in the next run
        if os.geteuid() == 0:
            self.skipTest("root can delete in a read-only directory")
        spec = {"d1/d2/a.txt": b"a", "ro/x.txt": b"x", "z.txt": b"z", "keep.txt": b"k"}
        src, dst = self.setup_run(direction, spec)
        state = self.state_of(self.job_run(direction, src, dst, {}, prune="yes"))
        shutil.rmtree(os.path.join(src, "d1"))
        os.remove(os.path.join(src, "ro", "x.txt"))
        os.remove(os.path.join(src, "z.txt"))
        ro = os.path.join(dst, "ro")
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        eng = self.engine(*self.job_sides(direction, src, dst, prune="yes"))
        with self.assertRaises(VCharonError) as cm:
            eng.run(state=state)
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual((eng.done.written, eng.done.deletes_done),
                         ([], ["d1/d2/a.txt", "d1/d2"]))
        self.assertEqual(sorted(eng.state_after["sent"]),
                         ["d1", "keep.txt", "ro", "ro/x.txt", "z.txt"])
        if direction == "pull":
            self.assertIn("source.state_after", self.calls)
        self.assertIn("state_after: 0 written, 2 deleted", self.log_text())
        os.chmod(ro, 0o755)
        eng = self.job_run(direction, src, dst, json.loads(json.dumps(eng.state_after)),
                           prune="yes")
        self.assertEqual([(e.op, e.path) for e in eng.plan.entries],
                         [("delete", "d1"), ("delete", "ro/x.txt"), ("delete", "z.txt")])
        self.assertEqual(read_tree(dst), read_tree(src))

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_failed_delete_phase_push(self):
        self.failed_delete_phase("push")

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_failed_delete_phase_pull(self):
        self.failed_delete_phase("pull")

    def case_rename_then_failure(self, direction):
        # a case-only rename at the source (svn mv keeps the mtime), and a commit that fails:
        # the next run still deletes Foo.cpp and puts foo.cpp, so a sink that folds names ends
        # with foo.cpp
        if os.geteuid() == 0:
            self.skipTest("root can delete in a read-only directory")
        src, dst = self.setup_run(direction, {"code/Foo.cpp": b"v1", "code/deep/old.txt": b"o"})
        state = self.state_of(self.job_run(direction, src, dst, {}, prune="yes"))
        os.rename(os.path.join(src, "code", "Foo.cpp"), os.path.join(src, "code", "foo.cpp"))
        os.remove(os.path.join(src, "code", "deep", "old.txt"))
        # the target's code/ is read-only: the commit gets through deep/old.txt, then fails
        code = os.path.join(dst, "code")
        os.chmod(code, 0o555)
        self.addCleanup(os.chmod, code, 0o755)
        eng = self.engine(*self.job_sides(direction, src, dst, prune="yes"))
        with self.assertRaises(VCharonError) as cm:
            eng.run(state=state)
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual(eng.done.deletes_done, ["code/deep/old.txt"])
        os.chmod(code, 0o755)
        eng = self.job_run(direction, src, dst, json.loads(json.dumps(eng.state_after)),
                           prune="yes")
        self.assertEqual([(e.op, e.path) for e in eng.plan.entries],
                         [("put", "code/foo.cpp"), ("delete", "code/Foo.cpp")])
        self.assertEqual(read_tree(dst), read_tree(src))

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_case_rename_then_a_failed_commit_push(self):
        self.case_rename_then_failure("push")

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_case_rename_then_a_failed_commit_pull(self):
        self.case_rename_then_failure("pull")

    def test_no_state_after_without_a_state(self):
        # a one-off pull keeps no state: a failed commit asks for none
        src, dst = self.setup_run("pull", {"a.txt": b"a", "b.txt": b"b"})

        def after_check(p, checked):
            os.makedirs(os.path.join(dst, "src", "b.txt", "inner"))

        eng = self.engine(*self.sides("pull", src, dst), after_check=after_check)
        with self.assertRaises(VCharonError):
            eng.run()
        self.assertEqual(eng.done.written, ["src", "src/a.txt"])
        self.assertNotIn("source.state_after", self.calls)
        self.assertIsNone(eng.state_after)

    def test_push_check_leaves_the_state_out(self):
        # the sink never reads it, and it holds every path sent so far
        src, dst = self.setup_run("push", {"a/b.txt": b"b", "c.txt": b"c"})
        state = self.state_of(self.job_run("push", src, dst, {}))
        write_tree(src, {"new.txt": b"n"})
        eng = self.job_run("push", src, dst, state)
        [args] = [a for fn, a in self.call_args if fn == "sink.check"]
        self.assertIsNone(args["plan"]["state"])
        # the identity stays
        self.assertEqual(args["plan"]["identity"], eng.plan.identity)
        self.assertEqual([e["path"] for e in args["plan"]["entries"]], ["new.txt"])
        # the controller's plan keeps its state, for the state file
        self.assertEqual(sorted(eng.plan.state["sent"]), ["a", "a/b.txt", "c.txt", "new.txt"])

    # the engine refuses a have that names anything but a hashed file put

    def test_lying_sink(self):
        src, dst = self.setup_run("push", {"d/f.txt": b"f"})
        write_tree(dst, {"d/f.txt": b"f"})
        before = self.snapshot(dst)
        # 0 is the directory d, and 1 a file put without a hash
        for lie in (0, 1):
            with self.subTest(lie=lie):
                eng = self.engine(*self.job_sides("push", src, dst),
                                  extra_modules=helper_override("LIE = %d\n" % lie + LYING_SINK))
                with self.assertRaises(VCharonError) as cm:
                    eng.run(state={})
                self.assertEqual((cm.exception.code, cm.exception.message),
                                 ("protocol", "the sink says entry %d is already at the target, "
                                              "but the plan doesn't hash it" % lie))
                self.assertEqual(self.calls, ["sink.check", "sink.abort"])
                self.assertEqual(self.snapshot(dst), before)

    def test_sides_must_be_one_local_one_remote(self):
        from vcharon import run
        s = self.session()
        for ends in (("local", "local"), ("remote", "remote")):
            with self.subTest(ends=ends):
                with self.assertRaises(VCharonError) as cm:
                    run.Engine(s, Side(ends[0], "path", {}), Side(ends[1], "dir", {}), self.log)
                self.assertEqual(cm.exception.code, "internal")


if __name__ == "__main__":
    unittest.main()
