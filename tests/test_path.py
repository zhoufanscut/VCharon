"""The path source (DESIGN, "The path source"): plans, refusals, and open() after the source
changed."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

from vcharon import fsops, pathrules, plan, plugin
from vcharon.plan import delete, put_dir, put_file
from vcharon.proto import VCharonError

from tests.util import fd_count, unblock_fifo, write_tree

POSIX = os.name != "nt"
WINDOWS = os.name == "nt"
MTIME = 1790000000.25
LINKS_HINT = "remove them, or skip them with symlinks = skip"


def paths(p):
    return [e.path for e in p.entries]


def unix_socket(case, path):
    """A bound AF_UNIX socket at path. It's bound from its own directory, so a long temp path
    can't overflow the socket address."""
    if not hasattr(socket, "AF_UNIX"):
        case.skipTest("no AF_UNIX sockets here")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    case.addCleanup(sock.close)
    cwd = os.getcwd()
    os.chdir(os.path.dirname(path))
    try:
        sock.bind(os.path.basename(path))
    finally:
        os.chdir(cwd)
    return sock


class PathCases:
    """Every case runs for each handle type."""

    impl = None

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="vcharon-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "src")
        self.outside = os.path.join(self.tmp, "outside")
        write_tree(self.outside, {"f.txt": b"OUTSIDE", "d/f.txt": b"OUTSIDE-D"})
        # what the sources' ctx.log got
        self.logged = []

    def at(self, rel):
        return os.path.join(self.src, *rel.split("/"))

    def source(self, path=None, end="local", **options):
        raw = {"path": self.src if path is None else path}
        raw.update(options)
        src = plugin.make(end, "path", "source", raw,
                          plugin.Ctx(end, home=self.tmp, log=self.logged.append))
        src.impl = self.impl
        self.addCleanup(src.close)
        return src

    def plan(self, path=None, **options):
        return self.source(path, **options).plan(None)

    def refused(self, code, path=None, **options):
        src = self.source(path, **options)
        with self.assertRaises(VCharonError) as cm:
            src.plan(None)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        return cm.exception

    def read(self, src, index):
        with src.open(index) as f:
            return f.read()

    def exec_bit(self, value):
        # a source on Windows leaves exec out
        return None if WINDOWS else value

    # --- a file ---

    def test_file(self):
        write_tree(self.tmp, {"data/report.txt": b"hello"})
        path = os.path.join(self.tmp, "data", "report.txt")
        os.utime(path, (MTIME, MTIME))
        src = self.source(path)
        p = src.plan(None)
        self.assertEqual(p.entries, [put_file("report.txt", 5, MTIME, self.exec_bit(False))])
        self.assertEqual(self.read(src, 0), b"hello")
        # keep_name changes nothing for a file
        self.assertEqual(paths(self.plan(path, keep_name="yes")), ["report.txt"])

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_file_through_a_symlink(self):
        # the name is the link's, the bytes are the target's
        write_tree(self.tmp, {"logs/2026-09-27.log": b"today"})
        link = os.path.join(self.tmp, "current.log")
        os.symlink(os.path.join("logs", "2026-09-27.log"), link)
        src = self.source(link)
        p = src.plan(None)
        self.assertEqual(paths(p), ["current.log"])
        self.assertEqual(p.entries[0].size, 5)
        self.assertEqual(p.identity["path"], link)
        self.assertEqual(self.read(src, 0), b"today")

    @unittest.skipUnless(POSIX, "exec bits are POSIX only")
    def test_exec_is_the_owners_bit(self):
        write_tree(self.src, {"a": b"", "b": b"", "c": b"", "d": b""})
        for name, mode in (("a", 0o755), ("b", 0o700), ("c", 0o611), ("d", 0o644)):
            os.chmod(self.at(name), mode)
        got = {e.path: e.executable for e in self.plan().entries}
        self.assertEqual(got, {"a": True, "b": True, "c": False, "d": False})

    def test_windows_leaves_exec_out(self):
        write_tree(self.src, {"a": b"a"})
        ctx = plugin.Ctx("local", osn="windows")
        # Only the exec bit is under test. The source reads a directory of this host, which a
        # Windows end's path rules needn't call absolute here (no drive), so take it as it is.
        ctx.resolve = lambda path, where: path
        src = plugin.make("local", "path", "source", {"path": self.src}, ctx)
        src.impl = self.impl
        self.addCleanup(src.close)
        self.assertIsNone(src.plan(None).entries[0].executable)

    # --- a tree ---

    def test_tree_order(self):
        write_tree(self.src, {"b.txt": b"b", "a/z.txt": b"z", "a/m/": None, "B/x": b"x",
                              "a.txt": b"1", "中/文 件.txt": b"zh", "a b/c": b"c", "e/": None})
        os.utime(self.at("a/z.txt"), (MTIME, MTIME))
        p = self.plan()
        # depth first, by code point, each directory right before what it holds
        self.assertEqual(paths(p), ["B", "B/x", "a", "a/m", "a/z.txt", "a b", "a b/c", "a.txt",
                                    "b.txt", "e", "中", "中/文 件.txt"])
        self.assertEqual(p.entries[1], put_file("B/x", 1, p.entries[1].mtime,
                                                self.exec_bit(False)))
        self.assertEqual(p.entries[3], put_dir("a/m"))
        self.assertEqual(p.entries[4].mtime, MTIME)
        self.assertEqual((p.state, p.notes), (None, []))
        # a plan any receiver takes, and that goes over the wire
        pathrules.check_plan(p, "linux")
        self.assertEqual(plan.from_json(plan.to_json(p)), p)

    def test_keep_name(self):
        write_tree(self.src, {"a/b.txt": b"b", "c/": None})
        p = self.plan(keep_name="yes")
        self.assertEqual(p.entries[0], put_dir("src"))
        self.assertEqual(paths(p), ["src", "src/a", "src/a/b.txt", "src/c"])
        self.assertEqual(paths(self.plan()), ["a", "a/b.txt", "c"])
        # the name is the last part of the absolute path, so "." gives its directory's name
        self.assertEqual(paths(self.plan(os.path.join(self.src, "a", ".."), keep_name="yes"))[0],
                         "src")

    def test_empty_directory(self):
        os.mkdir(self.src)
        self.assertEqual(self.plan().entries, [])
        self.assertEqual(self.plan(keep_name="yes").entries, [put_dir("src")])

    def test_identity(self):
        write_tree(self.src, {"a": b"a"})
        self.assertEqual(self.plan().identity, {"end": "local", "path": self.src, "kind": "dir"})
        with mock.patch.dict(os.environ, {"VCHARON_TEST_MACHINE_ID": "f" * 32}):
            # relative to the remote end's home
            p = self.source("src", end="remote").plan(None)
        self.assertEqual(p.identity, {"end": "remote", "machine": "f" * 32, "path": self.src,
                                      "kind": "dir"})
        self.assertEqual(list(p.identity), ["end", "machine", "path", "kind"])
        self.assertEqual(paths(p), ["a"])
        # a file, and the kind with a state too
        self.assertEqual(self.plan(self.at("a")).identity,
                         {"end": "local", "path": self.at("a"), "kind": "file"})
        self.assertEqual(self.source().plan({}).identity["kind"], "dir")

    # --- exclude ---

    def test_exclude(self):
        write_tree(self.src, {".git/config": b"g", "a/.git/x": b"g", "a/b.pyc": b"p",
                              "a/b.py": b"p", "c.pyc/inside.txt": b"x", "docs/a.md": b"m",
                              "docs/sub/b.md": b"m", "docs/keep.txt": b"k", "x/docs/c.md": b"m"})
        p = self.plan(exclude=".git, *.pyc,,docs/*.md")
        self.assertEqual(paths(p), ["a", "a/b.py", "docs", "docs/keep.txt", "docs/sub", "x",
                                    "x/docs", "x/docs/c.md"])
        self.assertEqual(p.notes, [])
        # without keep_name's prefix
        p = self.plan(keep_name="yes", exclude="src,docs/*.md,.git,*.pyc")
        self.assertEqual(paths(p), ["src", "src/a", "src/a/b.py", "src/docs", "src/docs/keep.txt",
                                    "src/docs/sub", "src/x", "src/x/docs", "src/x/docs/c.md"])

    def test_excluded_dirs_are_never_entered(self):
        write_tree(self.src, {"skip/deep/f": b"f", "keep/g": b"g"})
        entered = []
        cls = fsops.PathDir if self.impl == "path" else fsops.FdDir
        real = cls.enter

        def enter(d, name, owner_rule=True):
            entered.append(name)
            return real(d, name, owner_rule)

        with mock.patch.object(cls, "enter", enter):
            self.assertEqual(paths(self.plan(exclude="skip")), ["keep", "keep/g"])
        self.assertEqual(entered, ["keep"])

    def test_bad_exclude_patterns(self):
        for bad in ("/x", "x/", "a,/b", "docs/", "docs\\*.md", "a,\\b"):
            with self.subTest(bad=bad):
                with self.assertRaises(VCharonError) as cm:
                    plugin.check_side("local", "path", "source", {"path": "/p", "exclude": bad})
                self.assertEqual(cm.exception.code, "bad_options")
                self.assertTrue(cm.exception.message.startswith("from.exclude: "))
        # fnmatch would read \ as / on Windows only
        with self.assertRaises(VCharonError) as cm:
            plugin.check_side("local", "path", "source", {"path": "/p", "exclude": "docs\\*.md"})
        self.assertEqual(cm.exception.message, "from.exclude: docs\\*.md: use / in patterns, "
                                               "not \\")

    # --- symlinks and special files ---

    @unittest.skipUnless(POSIX, "needs symlinks and FIFOs")
    def test_symlinks_and_special_files(self):
        write_tree(self.src, {"a/f.txt": b"f", "z.txt": b"z"})
        os.symlink(os.path.join(self.outside, "f.txt"), self.at("lf"))
        os.symlink(os.path.join(self.outside, "d"), self.at("a/ld"))
        os.mkfifo(self.at("fifo"))
        e = self.refused("unsafe_path")
        self.assertEqual(e.message, "a/ld: a symlink (and 2 more; see the log)")
        self.assertEqual(e.detail.splitlines(), ["a/ld: a symlink", "fifo: a special file",
                                                 "lf: a symlink"])
        self.assertEqual(e.hint, LINKS_HINT)
        e = self.refused("unsafe_path", keep_name="yes")
        self.assertEqual(e.message, "src/a/ld: a symlink (and 2 more; see the log)")
        p = self.plan(symlinks="skip")
        self.assertEqual(paths(p), ["a", "a/f.txt", "z.txt"])
        self.assertEqual(p.notes, ["skipped 3 symlinks or special files"])
        os.remove(self.at("lf"))
        os.remove(self.at("fifo"))
        self.assertEqual(self.plan(symlinks="skip").notes, ["skipped 1 symlink or special file"])

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_link_to_a_directory_is_never_entered(self):
        write_tree(self.src, {"real/f": b"f"})
        os.symlink(os.path.join(self.outside, "d"), self.at("ld"))
        entered = []
        cls = fsops.PathDir if self.impl == "path" else fsops.FdDir
        real = cls.enter

        def enter(d, name, owner_rule=True):
            entered.append(name)
            return real(d, name, owner_rule)

        with mock.patch.object(cls, "enter", enter):
            self.assertEqual(paths(self.plan(symlinks="skip")), ["real", "real/f"])
            self.refused("unsafe_path")
        self.assertEqual(entered, ["real", "real"])

    def test_stage_dirs_are_left_out(self):
        write_tree(self.src, {".vcharon-stage-x/0": b"s", "d/.VCHARON-STAGE-y/lock": b"",
                              "d/f": b"f", ".VCharon-Stage-file": b"x"})
        p = self.plan()
        self.assertEqual(paths(p), ["d", "d/f"])
        self.assertEqual(p.notes, [])

    def test_name_that_isnt_utf8(self):
        os.mkdir(self.src)
        base = os.fsencode(self.src)
        try:
            with open(os.path.join(base, b"bad\xff.txt"), "wb"):
                pass
            os.mkdir(os.path.join(base, b"dir\xfe"))
            # never entered, so its own bad name never shows
            with open(os.path.join(base, b"dir\xfe", b"inside\xfd"), "wb"):
                pass
        except (OSError, ValueError):
            # APFS refuses such names; on Windows, bytes paths must be UTF-8 (ValueError)
            self.skipTest("this file system refuses names that aren't valid UTF-8")
        write_tree(self.src, {"ok.txt": b"ok"})
        if POSIX:
            # name problems come before link problems
            os.symlink(self.outside, self.at("link"))
        e = self.refused("unsafe_path")
        self.assertEqual(e.message, "bad\\xff.txt: the name isn't valid UTF-8 (and 1 more; see "
                                    "the log)")
        self.assertEqual(e.detail.splitlines(), ["bad\\xff.txt: the name isn't valid UTF-8",
                                                 "dir\\xfe: the name isn't valid UTF-8"])
        self.assertEqual(e.hint, "rename it at the source")
        # the error can be sent
        e.to_json()

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_unreadable_subdirectory(self):
        if os.geteuid() == 0:
            self.skipTest("root can read every directory")
        write_tree(self.src, {"ok/f": b"f", "locked/g": b"g"})
        os.chmod(self.at("locked"), 0)
        self.addCleanup(os.chmod, self.at("locked"), 0o755)
        e = self.refused("permission")
        self.assertEqual(e.message, "can't list %s: %s" % (self.at("locked"),
                                                           os.strerror(errno.EACCES)))
        self.assertEqual(e.hint, "fix its permissions, or exclude it")
        self.assertEqual(paths(self.plan(exclude="locked")), ["ok", "ok/f"])

    def test_a_root_that_cant_be_listed(self):
        # open but not listable (Windows can do that): the root's own hint, never LIST_HINT,
        # which a mailbox job swaps for a writer's folder (DESIGN, "Channel sections")
        write_tree(self.src, {"f": b"f"})
        handles = (fsops.FdDir, fsops.PathDir)
        real = {cls: cls.scan for cls in handles}
        calls = []

        def scan(d):
            calls.append(d)
            if len(calls) == 1:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
            return real[type(d)](d)

        with mock.patch.object(fsops.FdDir, "scan", scan), \
                mock.patch.object(fsops.PathDir, "scan", scan):
            e = self.refused("permission")
        self.assertEqual(e.message, "can't list %s: %s" % (self.src, os.strerror(errno.EACCES)))
        self.assertEqual(e.hint, "fix its permissions")

    def test_directory_that_vanishes_while_listed(self):
        write_tree(self.src, {"a/f": b"f", "b/g": b"g"})
        cls = fsops.PathDir if self.impl == "path" else fsops.FdDir
        real = cls.scan

        def scan(d):
            items = real(d)
            if d.rel == ():
                shutil.rmtree(self.at("b"))
            return items

        with mock.patch.object(cls, "scan", scan):
            e = self.refused("vanished")
        self.assertEqual(e.message, "%s changed while it was being listed" % self.at("b"))
        self.assertEqual(e.hint, "run again")

    # --- the top ---

    def test_errors_at_the_top(self):
        missing = os.path.join(self.tmp, "missing")
        e = self.refused("not_found", missing)
        self.assertEqual(e.message, "%s doesn't exist" % missing)
        self.assertEqual(e.hint, "check the path")
        write_tree(self.tmp, {"file": b"f"})
        self.refused("not_found", os.path.join(self.tmp, "file", "below"))
        root = os.path.abspath(os.sep)
        e = self.refused("bad_options", root, keep_name="yes")
        self.assertEqual(e.message, "from.path: keep_name needs a directory with a name, and %s "
                                    "is a file system root" % root)
        e = self.refused("bad_options", "relative")
        self.assertEqual(e.message, "from.path: must be an absolute path or start with ~")

    @unittest.skipUnless(POSIX, "needs FIFOs")
    def test_fifo_at_the_top(self):
        fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(fifo)
        for symlinks in ("error", "skip"):
            with self.subTest(symlinks=symlinks):
                e = self.refused("unsafe_path", fifo, symlinks=symlinks)
                self.assertEqual(e.message, "%s is neither a file nor a directory" % fifo)

    @unittest.skipUnless(os.path.isdir("/dev/fd"), "needs /dev/fd")
    def test_failed_plan_closes_its_handles(self):
        write_tree(self.src, {"a/b/c/f": b"f"})
        os.symlink(self.outside, self.at("a/b/c/link"))
        before = fd_count()
        self.refused("unsafe_path")
        self.assertEqual(fd_count(), before)

    # --- open() after the source changed ---

    @unittest.skipUnless(POSIX, "needs symlinks and FIFOs")
    def test_changes_after_the_plan(self):
        write_tree(self.src, {"a/f.txt": b"inside", "b.txt": b"b", "c.txt": b"c", "d.txt": b"d",
                              "e.txt": b"e", "grow.txt": b"12", "k/1": b"inside 1",
                              "k/2": b"inside 2"})
        write_tree(self.outside, {"k/1": b"OUTSIDE 1", "k/2": b"OUTSIDE 2"})
        src = self.source()
        index = {e.path: i for i, e in enumerate(src.plan(None).entries)}
        # The chain: after k/1, open() holds k; then k is swapped for a link to an outside
        # directory with the same names. The fd handle still reads the real k; the path handle
        # checks its chain again and refuses.
        self.assertEqual(self.read(src, index["k/1"]), b"inside 1")
        os.rename(self.at("k"), self.at("k-real"))
        os.symlink(os.path.join(self.outside, "k"), self.at("k"))
        if self.impl == "fd":
            self.assertEqual(self.read(src, index["k/2"]), b"inside 2")
        else:
            with self.assertRaises(VCharonError) as cm:
                reader = src.open(index["k/2"])
                self.fail("read %r" % reader.read())
            self.assertEqual((cm.exception.code, cm.exception.message),
                             ("vanished", "k/2 is gone, or isn't a regular file any more"))
        # a directory swapped for a link to an outside directory holding the same names
        os.rename(self.at("a"), self.at("a2"))
        os.symlink(os.path.join(self.outside, "d"), self.at("a"))
        # a file swapped for a link to an outside file, one for a FIFO, one removed
        os.remove(self.at("b.txt"))
        os.symlink(os.path.join(self.outside, "f.txt"), self.at("b.txt"))
        os.remove(self.at("c.txt"))
        os.mkfifo(self.at("c.txt"))
        unblock_fifo(self, self.at("c.txt"))
        os.remove(self.at("d.txt"))
        # and one for a socket: open() gives ENXIO on Linux and EOPNOTSUPP on macOS
        os.remove(self.at("e.txt"))
        unix_socket(self, self.at("e.txt"))
        with open(self.at("grow.txt"), "ab") as f:
            f.write(b"345")
        for name in ("a/f.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
            with self.subTest(name=name):
                started = time.monotonic()
                with self.assertRaises(VCharonError) as cm:
                    reader = src.open(index[name])
                    # a wrong implementation: show what it read
                    self.fail("read %r" % reader.read())
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(cm.exception.code, "vanished")
                self.assertEqual(cm.exception.message,
                                 "%s is gone, or isn't a regular file any more" % name)
                self.assertEqual(cm.exception.hint, "it changed during the run; run again")
        # a file that grew: all of its bytes
        self.assertEqual(self.read(src, index["grow.txt"]), b"12345")

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_file_in_a_search_only_directory(self):
        # a drop box: x without r. A single file's directory is searched, never listed.
        if os.geteuid() == 0:
            self.skipTest("root can read every directory")
        write_tree(self.tmp, {"drop/f.txt": b"readable"})
        drop = os.path.join(self.tmp, "drop")
        os.chmod(drop, 0o311)
        self.addCleanup(os.chmod, drop, 0o755)
        src = self.source(os.path.join(drop, "f.txt"))
        self.assertEqual(paths(src.plan(None)), ["f.txt"])
        self.assertEqual(self.read(src, 0), b"readable")

    def test_file_whose_directory_cant_be_opened(self):
        write_tree(self.tmp, {"data/f.txt": b"f"})
        path = os.path.join(self.tmp, "data", "f.txt")
        cls = fsops.PathDir if self.impl == "path" else fsops.FdDir
        denied = PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        with mock.patch.object(cls, "open_root", side_effect=denied):
            e = self.refused("permission", path)
        self.assertEqual(e.message, "can't read %s: %s" % (path, os.strerror(errno.EACCES)))
        self.assertEqual(e.hint, "check the permissions of its directory")

    def test_open_in_alternating_directories(self):
        spec = {"a/1": b"a1", "a/2": b"a2", "b/1": b"b1", "b/c/1": b"bc1", "top": b"t"}
        write_tree(self.src, spec)
        before = fd_count() if POSIX else None
        src = self.source()
        index = {e.path: i for i, e in enumerate(src.plan(None).entries)}
        for name in ("a/1", "b/1", "a/2", "b/c/1", "top", "a/1", "b/c/1", "b/1"):
            with self.subTest(name=name):
                self.assertEqual(self.read(src, index[name]), spec[name])
        src.close()
        src.close()
        if POSIX:
            self.assertEqual(fd_count(), before)
        with self.assertRaises(VCharonError) as cm:
            src.open(index["top"])
        self.assertEqual(cm.exception.code, "internal")

    def test_open_refuses_other_indexes(self):
        write_tree(self.src, {"d/f": b"f"})
        src = self.source()
        with self.assertRaises(VCharonError) as cm:
            src.open(0)
        self.assertEqual(cm.exception.code, "internal")
        src.plan(None)
        for index in (0, 2, -1, "1"):
            with self.subTest(index=index):
                with self.assertRaises(VCharonError) as cm:
                    src.open(index)
                self.assertEqual(cm.exception.code, "internal")
        with self.assertRaises(VCharonError) as cm:
            src.plan(None)
        self.assertEqual(cm.exception.code, "internal")

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_open_unreadable_file(self):
        if os.geteuid() == 0:
            self.skipTest("root can read every file")
        write_tree(self.src, {"secret": b"s"})
        src = self.source()
        src.plan(None)
        os.chmod(self.at("secret"), 0)
        with self.assertRaises(VCharonError) as cm:
            src.open(0)
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual(cm.exception.message, "secret: %s" % os.strerror(errno.EACCES))
        self.assertEqual(cm.exception.hint, "check its permissions at the source")


    # --- a state: only what changed (DESIGN, "The path source") ---

    def planned(self, state, full=False, path=None, **options):
        src = self.source(path, **options)
        return src, src.plan(state, full)

    def state_of(self, state=None, **options):
        """The state a plan with state gives, as a state file gives it back."""
        return json.loads(json.dumps(self.planned({} if state is None else state,
                                                  **options)[1].state))

    def value(self, rel):
        st = os.stat(self.at(rel))
        return [st.st_size, st.st_mtime, self.exec_bit(bool(st.st_mode & 0o100))]

    def test_no_state(self):
        # a one-off pull keeps none, and gets none
        write_tree(self.src, {"a/b.txt": b"b", "c.txt": b"c"})
        src, p = self.planned(None)
        self.assertEqual(paths(p), ["a", "a/b.txt", "c.txt"])
        self.assertIsNone(p.state)
        self.assertIsNone(src.state_after(["a", "a/b.txt"], []))

    def test_first_run(self):
        write_tree(self.src, {"a/b.txt": b"bb", "c.txt": b"c", "e/": None})
        os.utime(self.at("c.txt"), (MTIME, MTIME))
        src, p = self.planned({})
        self.assertEqual(paths(p), ["a", "a/b.txt", "c.txt", "e"])
        self.assertEqual(p.state, {"sent": {"a": "d", "a/b.txt": self.value("a/b.txt"),
                                            "c.txt": [1, MTIME, self.exec_bit(False)],
                                            "e": "d"}})
        # a float mtime survives JSON exactly
        self.assertEqual(json.loads(json.dumps(p.state)), p.state)
        self.assertEqual([e.sha256 for e in p.entries], [None] * 4)
        with src.open(1) as f:
            self.assertEqual(f.read(), b"bb")

    def test_1_nothing_changed(self):
        write_tree(self.src, {"a/b.txt": b"bb", "c.txt": b"c", "e/": None})
        state = self.state_of()
        src, p = self.planned(state)
        self.assertEqual((p.entries, p.state), ([], state))
        for index in (0, 1):
            with self.assertRaises(VCharonError) as cm:
                src.open(index)
            self.assertEqual(cm.exception.code, "internal")

    def test_2_only_what_changed(self):
        write_tree(self.src, {"size.txt": b"1", "mtime.txt": b"m", "exec.sh": b"x",
                              "same.txt": b"s", "d/in.txt": b"i"})
        for rel in ("size.txt", "mtime.txt", "exec.sh", "same.txt", "d/in.txt"):
            os.utime(self.at(rel), (MTIME, MTIME))
        state = self.state_of()
        write_tree(self.src, {"size.txt": b"22", "new.txt": b"n", "e/": None, "f/g.txt": b"g"})
        os.utime(self.at("size.txt"), (MTIME, MTIME))
        # the same bytes, a later mtime
        os.utime(self.at("mtime.txt"), (MTIME, MTIME + 1))
        want = ["e", "f", "f/g.txt", "mtime.txt", "new.txt", "size.txt"]
        if POSIX:
            os.chmod(self.at("exec.sh"), 0o755)
            want.insert(1, "exec.sh")
        src, p = self.planned(state)
        self.assertEqual(paths(p), want)
        sent = p.state["sent"]
        self.assertEqual(sent["size.txt"], [2, MTIME, self.exec_bit(False)])
        self.assertEqual(sent["mtime.txt"], [1, MTIME + 1, self.exec_bit(False)])
        self.assertEqual(sent["same.txt"], state["sent"]["same.txt"])
        self.assertEqual(sorted(sent), sorted(set(state["sent"]) | {"e", "f", "f/g.txt",
                                                                     "new.txt"}))
        # open() uses this plan's indexes
        index = {e.path: i for i, e in enumerate(p.entries)}
        with src.open(index["f/g.txt"]) as f:
            self.assertEqual(f.read(), b"g")
        with src.open(index["size.txt"]) as f:
            self.assertEqual(f.read(), b"22")

    def test_2_revert_is_sent_again(self):
        # svn revert writes the old bytes back, with a new mtime
        write_tree(self.src, {"r.txt": b"original"})
        os.utime(self.at("r.txt"), (MTIME, MTIME))
        state = self.state_of()
        write_tree(self.src, {"r.txt": b"original"})
        os.utime(self.at("r.txt"), (MTIME + 60, MTIME + 60))
        _src, p = self.planned(state)
        self.assertEqual(p.entries, [put_file("r.txt", 8, MTIME + 60, self.exec_bit(False))])

    def test_keep_name_with_a_state(self):
        write_tree(self.src, {"a/b.txt": b"b"})
        state = self.state_of(keep_name="yes")
        self.assertEqual(sorted(state["sent"]), ["src", "src/a", "src/a/b.txt"])
        self.assertEqual(state["sent"]["src"], "d")
        _src, p = self.planned(state, keep_name="yes")
        self.assertEqual(p.entries, [])

    def test_full(self):
        spec = {"a/b.txt": b"bb", "c.txt": os.urandom(3 << 18), "e/": None, "empty": b""}
        write_tree(self.src, spec)
        state = self.state_of()
        src, p = self.planned(state, full=True)
        self.assertEqual(paths(p), ["a", "a/b.txt", "c.txt", "e", "empty"])
        for e in p.entries:
            want = (hashlib.sha256(spec[e.path]).hexdigest() if e.kind == "file" else None)
            self.assertEqual(e.sha256, want, e.path)
        self.assertEqual(p.state, state)
        with src.open(2) as f:
            self.assertEqual(f.read(), spec["c.txt"])
        # without a state too
        self.assertEqual(self.planned(None, full=True)[1].entries[1].sha256,
                         hashlib.sha256(b"bb").hexdigest())

    def test_full_single_file(self):
        write_tree(self.tmp, {"one.bin": b"single"})
        _src, p = self.planned({}, full=True, path=os.path.join(self.tmp, "one.bin"))
        self.assertEqual(p.entries[0].sha256, hashlib.sha256(b"single").hexdigest())
        self.assertEqual(p.state["sent"], {"one.bin": [6, p.entries[0].mtime,
                                                       self.exec_bit(False)]})

    def test_full_file_gone_before_its_hash(self):
        write_tree(self.src, {"a.txt": b"a", "gone.txt": b"g", "z.txt": b"z"})
        cls = fsops.PathDir if self.impl == "path" else fsops.FdDir
        real = cls.open_read

        def open_read(d, name):
            if name == "gone.txt":
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
            return real(d, name)

        with mock.patch.object(cls, "open_read", open_read):
            src, p = self.planned({}, full=True)
        self.assertEqual(paths(p), ["a.txt", "z.txt"])
        self.assertNotIn("gone.txt", p.state["sent"])
        with src.open(1) as f:
            self.assertEqual(f.read(), b"z")

    @unittest.skipUnless(POSIX, "needs FIFOs")
    def test_full_fifo_swapped_in_before_its_hash(self):
        write_tree(self.src, {"a.txt": b"a", "b.txt": b"b"})
        cls = fsops.PathDir if self.impl == "path" else fsops.FdDir
        real = cls.scan

        def scan(d):
            items = real(d)
            os.remove(self.at("b.txt"))
            os.mkfifo(self.at("b.txt"))
            unblock_fifo(self, self.at("b.txt"))
            return items

        started = time.monotonic()
        with mock.patch.object(cls, "scan", scan):
            src = self.source()
            with self.assertRaises(VCharonError) as cm:
                src.plan({}, True)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ("vanished", "b.txt is gone, or isn't a regular file any more"))

    # --- fold partners: a receiver that folds names sees what a full plan shows it ---

    def both_kept(self, *names):
        """Skips unless this file system kept every one of these names apart."""
        if sorted(os.listdir(self.src)) != sorted(names):
            self.skipTest("this file system folds these names together")

    def test_fold_partners_case(self):
        write_tree(self.src, {"README": b"upper"})
        state = self.state_of()
        # a new, different file whose name only differs in case
        write_tree(self.src, {"readme": b"lower, another file"})
        self.both_kept("README", "readme")
        src, p = self.planned(state)
        self.assertEqual(paths(p), ["README", "readme"])
        # so Windows and macOS refuse it, as they refuse the full plan, and README survives
        for osn in ("windows", "darwin"):
            with self.subTest(osn=osn):
                with self.assertRaises(VCharonError) as cm:
                    pathrules.check_plan(p, osn)
                self.assertEqual(cm.exception.code, "collision")
        pathrules.check_plan(p, "linux")
        self.assertEqual(p.state["sent"], {"README": self.value("README"),
                                           "readme": self.value("readme")})
        with src.open(0) as f:
            self.assertEqual(f.read(), b"upper")

    def test_fold_partners_below_the_top(self):
        # the partner search folds only entries whose last name can match: nested ones too
        write_tree(self.src, {"docs/sub/README": b"upper", "docs/other.txt": b"o"})
        state = self.state_of()
        write_tree(self.src, {"docs/sub/readme": b"lower, another file"})
        if sorted(os.listdir(self.at("docs/sub"))) != ["README", "readme"]:
            self.skipTest("this file system folds these names together")
        _src, p = self.planned(state)
        self.assertEqual(paths(p), ["docs/sub/README", "docs/sub/readme"])
        with self.assertRaises(VCharonError) as cm:
            pathrules.check_plan(p, "windows")
        self.assertEqual(cm.exception.code, "collision")

    def test_fold_partners_of_a_stale_spelling(self):
        # sent holds an old spelling next to the current one: after a failed commit's
        # state_after, or when prune is turned on after a case-only rename
        write_tree(self.src, {"foo.cpp": b"v2"})
        value = self.value("foo.cpp")
        state = {"sent": {"Foo.cpp": [2, MTIME, self.exec_bit(False)], "foo.cpp": value}}
        src, p = self.planned(state, prune="yes")
        # the delete of Foo.cpp unlinks foo.cpp on NTFS and APFS: the put writes it again
        self.assertEqual(p.entries, [put_file("foo.cpp", 2, value[1], value[2]),
                                     delete("Foo.cpp", why="gone from the source")])
        self.assertEqual(p.state, {"sent": {"foo.cpp": value}})
        with src.open(0) as f:
            self.assertEqual(f.read(), b"v2")
        # without prune nothing is deleted, so nothing is planned
        self.assertEqual(self.planned(state)[1].entries, [])

    def test_fold_partners_normalization(self):
        nfc = unicodedata.normalize("NFC", "\u00e9.txt")
        nfd = unicodedata.normalize("NFD", nfc)
        write_tree(self.src, {nfc: b"composed"})
        state = self.state_of()
        write_tree(self.src, {nfd: b"decomposed, another file"})
        self.both_kept(nfc, nfd)
        _src, p = self.planned(state)
        # walk order: by code point
        self.assertEqual(paths(p), [nfd, nfc])
        with self.assertRaises(VCharonError) as cm:
            pathrules.check_plan(p, "darwin")
        self.assertEqual(cm.exception.code, "collision")

    def test_fold_partners_of_directories(self):
        write_tree(self.src, {"A/old.txt": b"o", "a/x.txt": b"x"})
        self.both_kept("A", "a")
        state = self.state_of()
        write_tree(self.src, {"A/new.txt": b"n"})
        src, p = self.planned(state)
        # the unchanged a folds like A, the directory the new file needs
        self.assertEqual(paths(p), ["A/new.txt", "a"])
        with self.assertRaises(VCharonError) as cm:
            pathrules.check_plan(p, "windows")
        self.assertEqual(cm.exception.code, "collision")
        self.assertEqual(p.state["sent"]["a"], "d")
        with src.open(0) as f:
            self.assertEqual(f.read(), b"n")

    def test_fold_partners_only_when_something_is_planned(self):
        write_tree(self.src, {"README": b"u", "readme": b"l", "A/x": b"x", "a/y": b"y"})
        self.both_kept("A", "README", "a", "readme")
        state = self.state_of()
        _src, p = self.planned(state, prune="yes")
        self.assertEqual((p.entries, p.state), ([], state))
        # an unrelated change brings no partners along
        write_tree(self.src, {"other.txt": b"o"})
        self.assertEqual(paths(self.planned(state, prune="yes")[1]), ["other.txt"])

    # --- prune ---

    def test_5_prune(self):
        write_tree(self.src, {"keep.txt": b"k", "gone.txt": b"g", "d/gone.txt": b"g",
                              "old/": None})
        state = self.state_of(prune="yes")
        os.remove(self.at("gone.txt"))
        shutil.rmtree(self.at("d"))
        os.rmdir(self.at("old"))
        _src, p = self.planned(state, prune="yes")
        # only what earlier runs sent, sorted by path, never as trees
        self.assertEqual(p.entries, [delete("d", why="gone from the source"),
                                     delete("d/gone.txt", why="gone from the source"),
                                     delete("gone.txt", why="gone from the source"),
                                     delete("old", why="gone from the source")])
        self.assertEqual(p.state, {"sent": {"keep.txt": state["sent"]["keep.txt"]}})
        # without prune nothing is deleted, and the gone paths stay in sent
        _src, p = self.planned(state)
        self.assertEqual((p.entries, p.state), ([], state))

    def test_5_kind_changes(self):
        write_tree(self.src, {"a/x": b"x", "b": b"file"})
        state = self.state_of()
        shutil.rmtree(self.at("a"))
        os.remove(self.at("b"))
        write_tree(self.src, {"a": b"now a file", "b/y": b"y"})
        src, p = self.planned(state, prune="yes")
        self.assertEqual(p.entries, [put_file("a", 10, p.entries[0].mtime, self.exec_bit(False)),
                                     put_dir("b"),
                                     put_file("b/y", 1, p.entries[2].mtime, self.exec_bit(False)),
                                     delete("a", why="replaced by a file"),
                                     delete("a/x", why="gone from the source"),
                                     delete("b", why="replaced by a directory")])
        self.assertEqual(p.state["sent"], {"a": self.value("a"), "b": "d",
                                           "b/y": self.value("b/y")})
        # a plan any receiver takes
        pathrules.check_plan(p, "linux")
        with src.open(0) as f:
            self.assertEqual(f.read(), b"now a file")
        # without prune: the puts only; the target refuses them if it still holds the old kind
        src, p = self.planned(state)
        self.assertEqual(paths(p), ["a", "b", "b/y"])
        self.assertEqual(p.state["sent"]["a/x"], state["sent"]["a/x"])

    def test_6_excluded_paths_are_never_deleted(self):
        write_tree(self.src, {"x.log": b"l", "build/o.bin": b"o", "build/sub/p.bin": b"p",
                              "keep.txt": b"k"})
        state = self.state_of()
        self.assertEqual(len(state["sent"]), 6)
        # still there, and gone: excluded either way
        for gone in (False, True):
            with self.subTest(gone=gone):
                if gone:
                    os.remove(self.at("x.log"))
                    shutil.rmtree(self.at("build"))
                _src, p = self.planned(state, prune="yes", exclude="*.log,build")
                self.assertEqual(p.entries, [])
                self.assertEqual(p.state, {"sent": {"keep.txt": state["sent"]["keep.txt"]}})
        # a pattern with / matches the whole relative path; keep_name's prefix is left out
        write_tree(self.src, {"docs/a.md": b"a", "docs/b.txt": b"b"})
        state = self.state_of(keep_name="yes")
        shutil.rmtree(self.at("docs"))
        _src, p = self.planned(state, keep_name="yes", prune="yes", exclude="docs/*.md,src")
        self.assertEqual(p.entries, [delete("src/docs", why="gone from the source"),
                                     delete("src/docs/b.txt", why="gone from the source")])
        self.assertEqual(sorted(p.state["sent"]), ["src", "src/keep.txt"])

    def test_6_excluded_stale_spelling_is_never_deleted(self):
        # sent holds Foo.cpp, a stale spelling of foo.cpp, which is excluded now: on a sink that
        # folds names, a delete of Foo.cpp would remove the excluded foo.cpp
        write_tree(self.src, {"foo.cpp": b"live", "keep.txt": b"k", "build/x.o": b"o",
                              "docs/a.md": b"a"})
        stale = [4, MTIME, self.exec_bit(False)]
        keep = self.value("keep.txt")
        state = {"sent": {"Foo.cpp": stale, "keep.txt": keep, "Build": "d",
                          "Build/x.o": stale, "Docs": "d", "Docs/a.md": stale}}
        _src, p = self.planned(state, prune="yes", exclude="foo.cpp,build,docs/*.md")
        # Docs itself matches nothing: it's gone from the source, so it's deleted (a directory
        # is removed only if empty)
        self.assertEqual([(e.op, e.path) for e in p.entries],
                         [("put", "docs"), ("delete", "Docs")])
        self.assertEqual(p.state, {"sent": {"keep.txt": keep, "docs": "d"}})

    def test_6_folded_patterns_leave_walked_paths_alone(self):
        # the walk's own rule decides what it lists; case counts (except on Windows), so
        # README.md is sent despite readme.md, and must not be sent again on every run
        if WINDOWS:
            self.skipTest("fnmatch ignores case on Windows")
        write_tree(self.src, {"README.md": b"r"})
        state = self.state_of(exclude="readme.md")
        self.assertEqual(list(state["sent"]), ["README.md"])
        _src, p = self.planned(state, prune="yes", exclude="readme.md")
        self.assertEqual((p.entries, p.state), ([], state))

    def test_6_empty_source(self):
        write_tree(self.src, {"a.txt": b"a", "d/b.txt": b"b"})
        state = self.state_of()
        shutil.rmtree(self.src)
        os.mkdir(self.src)
        e = self.refused_with(state, "empty_source", prune="yes")
        self.assertEqual(e.message, "%s is empty, but earlier runs sent 3 paths from it"
                         % self.src)
        self.assertEqual(e.hint, "check that its disk is mounted; if it's really empty, set "
                                 "from.allow_empty = yes")
        # only excluded entries, stage dirs and skipped links: still empty
        write_tree(self.src, {"x.log": b"l", ".vcharon-stage-0123/lock": b""})
        if POSIX:
            os.symlink(self.outside, self.at("link"))
        self.refused_with(state, "empty_source", prune="yes", exclude="*.log", symlinks="skip")
        # with allow_empty, every sent path is a delete
        _src, p = self.planned(state, prune="yes", allow_empty="yes", exclude="*.log",
                              symlinks="skip")
        self.assertEqual(p.entries, [delete(path, why="gone from the source")
                                     for path in ("a.txt", "d", "d/b.txt")])
        self.assertEqual(p.state, {"sent": {}})
        # allow_empty without prune does nothing
        self.assertEqual(self.planned(state, allow_empty="yes", exclude="*.log",
                                      symlinks="skip")[1].entries, [])
        # a first run with prune on an empty directory: an empty plan
        self.assertEqual(self.planned({}, prune="yes", exclude="*.log",
                                      symlinks="skip")[1].entries, [])

    def test_6_empty_source_with_keep_name(self):
        write_tree(self.src, {"a.txt": b"a"})
        state = self.state_of(keep_name="yes")
        os.remove(self.at("a.txt"))
        e = self.refused_with(state, "empty_source", keep_name="yes", prune="yes")
        # the source directory's own entry doesn't count
        self.assertEqual(e.message, "%s is empty, but earlier runs sent 1 path from it"
                         % self.src)
        _src, p = self.planned({"sent": {"src": "d"}}, keep_name="yes", prune="yes")
        self.assertEqual(p.entries, [])

    def refused_with(self, state, code, **options):
        src = self.source(**options)
        with self.assertRaises(VCharonError) as cm:
            src.plan(state)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        return cm.exception

    # --- state_after: never lists what a failed commit removed ---

    def test_9_state_after(self):
        write_tree(self.src, {"a/1": b"1", "old": b"o", "gone": b"g", "x.log": b"l",
                              "b/2": b"2"})
        state = self.state_of()
        os.remove(self.at("old"))
        os.remove(self.at("gone"))
        write_tree(self.src, {"a/1": b"11", "a/new": b"n", "b/2": b"22"})
        src, p = self.planned(state, prune="yes", exclude="*.log")
        self.assertEqual(paths(p), ["a/1", "a/new", "b/2", "gone", "old"])
        # the commit got through the delete of gone and wrote a/new, then failed
        after = src.state_after(["a/new", "not/in/the/plan"], ["gone", "b/2"])
        # the old sent without x.log (excluded), gone (deleted), and a/1 and b/2 (not written:
        # the next run sends them); old stays, as its delete didn't happen
        self.assertEqual(after, {"sent": {"a": "d", "a/new": self.value("a/new"), "b": "d",
                                          "old": state["sent"]["old"]}})
        self.assertEqual(self.logged, ["state_after: b/2 isn't a delete of this plan; ignored",
                                       "state_after: not/in/the/plan isn't a put of this plan; "
                                       "ignored"])
        # all of it done: the plan's own new state
        self.assertEqual(src.state_after(["a/1", "a/new", "b/2"], ["gone", "old"]), p.state)
        # nothing done: the old sent less the puts
        self.assertEqual(src.state_after([], []), {"sent": {"a": "d", "b": "d",
                                                            "gone": state["sent"]["gone"],
                                                            "old": state["sent"]["old"]}})
        # and the next run from there sends what's missing and deletes what's left
        next_p = self.planned(after, prune="yes", exclude="*.log")[1]
        self.assertEqual([(e.op, e.path) for e in next_p.entries],
                         [("put", "a/1"), ("put", "b/2"), ("delete", "old")])

    def test_9_state_after_a_kind_change_not_reached(self):
        write_tree(self.src, {"k/x": b"x"})
        state = self.state_of()
        shutil.rmtree(self.at("k"))
        write_tree(self.src, {"k": b"now a file"})
        src, p = self.planned(state, prune="yes")
        self.assertEqual([(e.op, e.path) for e in p.entries],
                         [("put", "k"), ("delete", "k"), ("delete", "k/x")])
        # the commit deleted k/x, then failed on k: the directory k is still at the target
        after = src.state_after([], ["k/x"])
        self.assertEqual(after, {"sent": {"k": "d"}})
        # so the next run deletes it again before it puts the file
        self.assertEqual([(e.op, e.path) for e in self.planned(after, prune="yes")[1].entries],
                         [("put", "k"), ("delete", "k")])
        # had it got through the delete of k too, nothing would be left
        self.assertEqual(src.state_after([], ["k/x", "k"]), {"sent": {}})

    def test_9_unwritten_partner_is_planned_again(self):
        # [put foo.cpp (a partner), delete Foo.cpp]: on NTFS or APFS the delete removed
        # foo.cpp too, and the commit failed before it wrote foo.cpp again
        write_tree(self.src, {"foo.cpp": b"v2"})
        value = self.value("foo.cpp")
        state = {"sent": {"Foo.cpp": [2, MTIME, self.exec_bit(False)], "foo.cpp": value}}
        src, p = self.planned(state, prune="yes")
        self.assertEqual(paths(p), ["foo.cpp", "Foo.cpp"])
        after = src.state_after([], ["Foo.cpp"])
        self.assertEqual(after, {"sent": {}})
        self.assertEqual(paths(self.planned(after, prune="yes")[1]), ["foo.cpp"])

    # --- a malformed state ---

    def test_malformed_states(self):
        write_tree(self.src, {"a": b"a"})
        good = [0, 0, None]
        cases = [[], "x", {"sent": {}, "other": 1}, {"sent": []}, {"sent": None},
                 {"sent": {"": "d"}}, {"sent": {1: "d"}}, {"sent": {"a": "D"}},
                 {"sent": {"a": None}}, {"sent": {"a": 1}}, {"sent": {"a": []}},
                 {"sent": {"a": [1, 2]}}, {"sent": {"a": [1, 2, None, 4]}},
                 {"sent": {"a": (1, 2, None)}}, {"sent": {"a": [-1, 2, None]}},
                 {"sent": {"a": [True, 2, None]}}, {"sent": {"a": [1.0, 2, None]}},
                 {"sent": {"a": ["1", 2, None]}}, {"sent": {"a": [1, True, None]}},
                 {"sent": {"a": [1, "2", None]}}, {"sent": {"a": [1, float("inf"), None]}},
                 {"sent": {"a": [1, float("nan"), None]}}, {"sent": {"a": [1, 1e12, None]}},
                 {"sent": {"a": [1, 10 ** 400, None]}}, {"sent": {"a": [1, 2, 1]}},
                 {"sent": {"a": [1, 2, 0]}}, {"sent": {"a": [1, 2, "yes"]}},
                 {"sent": {"b": good, "a": {"size": 1}}},
                 # not a plan path (DESIGN, "Plans"): with prune it would be a delete
                 {"sent": {"../x": good}}, {"sent": {"a/../../x": "d"}}, {"sent": {"a//b": good}},
                 {"sent": {"/a": good}}, {"sent": {"a/./b": good}}, {"sent": {"a/": "d"}},
                 {"sent": {".": "d"}}, {"sent": {"a\x00b": good}}, {"sent": {"a\udcffb": good}},
                 {"sent": {".vcharon-stage-0123/x": good}}, {"sent": {"d/.VCHARON-STAGE-x": "d"}}]
        for state in cases:
            with self.subTest(state=state):
                e = self.refused_with(state, "state_mismatch")
                self.assertTrue(e.message.startswith("the saved state is malformed: "),
                                e.message)
        e = self.refused_with({"sent": {"a/../x": "d"}}, "state_mismatch")
        self.assertEqual(e.message, 'the saved state is malformed: sent holds a/../x: has an '
                                    'empty, "." or ".." part')
        for state in ({}, {"sent": {}}, {"sent": {"a": good, "d": "d"}},
                      {"sent": {"a": [1, -5, False], "b": [2, 2.5, True], "c": [3, 1e11, None]}}):
            with self.subTest(state=state):
                self.planned(state)

    @unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"),
                         "needs svn and svnadmin")
    def test_2_real_svn_revert(self):
        repo = os.path.join(self.tmp, "repo")
        svn = ["svn", "--config-dir", os.path.join(self.tmp, "svn-config"), "--non-interactive"]

        def run(argv):
            subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=60)

        run(["svnadmin", "create", repo])
        # a Windows path needs the file:///C:/... form; pathlib gives it on every OS
        run(svn + ["checkout", pathlib.Path(repo).as_uri(), self.src])
        write_tree(self.src, {"f.txt": b"committed bytes\n"})
        run(svn + ["add", self.at("f.txt")])
        run(svn + ["commit", "-m", "f", self.src])
        state = self.state_of(exclude=".svn")
        # edited: planned, and that state saved
        write_tree(self.src, {"f.txt": b"edited\n"})
        os.utime(self.at("f.txt"), (MTIME, MTIME))
        _src, p = self.planned(state, exclude=".svn")
        self.assertEqual(paths(p), ["f.txt"])
        state = json.loads(json.dumps(p.state))
        run(svn + ["revert", self.at("f.txt")])
        _src, p = self.planned(state, exclude=".svn")
        self.assertEqual(paths(p), ["f.txt"])
        self.assertEqual(p.entries[0].size, len(b"committed bytes\n"))
        self.assertNotEqual(p.entries[0].mtime, MTIME)


    # --- a channel's folder limits ---

    def test_a_member_folder_over_the_limits(self):
        # the pull's source: each member folder over max_bytes or max_files is left out of
        # the plan, with a note, and keeps its sent entries as they were
        limits = {"mailbox_me": "me", "prune": "yes", "max_bytes": "100", "max_files": "3"}
        write_tree(self.src, {"me/x": b"x", "a/MEMBER.md": b"m", "a/old.txt": b"o",
                              "b/b.txt": b"b"})
        first = self.state_of(**limits)
        self.assertIn("a/old.txt", first["sent"])
        # over: three new files, and old.txt deleted, at once; b changes
        write_tree(self.src, {"a/n1": b"1", "a/n2": b"2", "a/n3": b"3", "b/b.txt": b"bb"})
        os.remove(self.at("a/old.txt"))
        src, p = self.planned(first, **limits)
        self.assertEqual(paths(p), ["b/b.txt"])
        self.assertEqual(p.notes, ["left out a/: 4 B in 4 files, 1 file over the limit of "
                                   "100 B and 3 files; this box's copy of it stays as it was "
                                   "until it is back under"])
        held = {k: v for k, v in first["sent"].items() if k.startswith("a")}
        self.assertEqual({k: v for k, v in p.state["sent"].items() if k.startswith("a")}, held)
        # a commit that wrote nothing keeps them too
        after = src.state_after([], [])
        self.assertEqual({k: v for k, v in after["sent"].items() if k.startswith("a")}, held)
        # full: the same, and the folder's files aren't read
        src, p = self.planned(first, full=True, **limits)
        self.assertEqual(paths(p), ["b", "b/b.txt"])
        # by size alone
        src, p = self.planned(first, **dict(limits, max_files="10", max_bytes="3"))
        self.assertEqual(paths(p), ["b/b.txt"])
        self.assertEqual(len(p.notes), 1)
        self.assertTrue(p.notes[0].startswith("left out a/: 4 B in 4 files, 1 B over the "
                                              "limit of 3 B and 10 files"), p.notes)
        # back under: old.txt's delete is planned, and the new files come
        for name in ("n2", "n3"):
            os.remove(self.at("a/" + name))
        src, p = self.planned(first, **limits)
        self.assertEqual(paths(p), ["a/n1", "b/b.txt", "a/old.txt"])
        self.assertEqual([e.op for e in p.entries], ["put", "put", "delete"])
        self.assertNotIn("a/old.txt", p.state["sent"])
        self.assertEqual(p.notes, [])

    def test_a_whole_source_over_the_limits(self):
        # the writer's own folder (up): refused, nothing planned
        write_tree(self.src, {"MEMBER.md": b"m", "a.txt": b"aaaa", "d/b.txt": b"b"})
        err = self.refused("too_big", max_bytes="5", max_files="10")
        self.assertEqual(err.message, "%s holds 6 B in 3 files, 1 B over the limit of 5 B and 10 "
                                      "files, so nothing was sent" % self.src)
        self.assertEqual(err.hint, "move what isn't an entry (logs, builds, data) out of %s, or "
                                   "delete it, until it holds at most 5 B and 10 files; keep "
                                   "MEMBER.md and your .md files with entries" % self.src)
        self.refused("too_big", max_bytes="100", max_files="2")
        self.assertEqual(paths(self.plan(max_bytes="6", max_files="3")),
                         ["MEMBER.md", "a.txt", "d", "d/b.txt"])
        # with a state too
        with self.assertRaises(VCharonError) as cm:
            self.planned({}, max_bytes="5", max_files="10")
        self.assertEqual(cm.exception.code, "too_big")

@unittest.skipIf(WINDOWS, "directory fds are POSIX only")
class FdPathSourceTest(PathCases, unittest.TestCase):
    impl = "fd"


class PathPathSourceTest(PathCases, unittest.TestCase):
    impl = "path"


class PathDoctorTest(unittest.TestCase):
    """The path source's doctor: it only reads."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="vcharon-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def doctor(self, path):
        return plugin.doctor("local", "path", "source", {"path": path},
                             plugin.Ctx("local", home=self.tmp))

    def test_file_and_directory(self):
        write_tree(self.tmp, {"d/f.txt": b"f"})
        d = os.path.join(self.tmp, "d")
        self.assertEqual(self.doctor(d), [("ok", "from.path %s: a directory" % d, None)])
        f = os.path.join(d, "f.txt")
        self.assertEqual(self.doctor(f), [("ok", "from.path %s: a file" % f, None)])
        # ~ is this end's home
        self.assertEqual(self.doctor("~/d"), [("ok", "from.path %s: a directory" % d, None)])

    def test_missing(self):
        self.assertEqual(self.doctor("~/nope"), [("FAIL", "from.path ~/nope doesn't exist",
                                                  "check the path")])
        self.assertEqual(os.listdir(self.tmp), [])

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_unreadable(self):
        if os.geteuid() == 0:
            self.skipTest("root reads anything")
        write_tree(self.tmp, {"d/x": b"x", "f.txt": b"f"})
        d, f = os.path.join(self.tmp, "d"), os.path.join(self.tmp, "f.txt")
        os.chmod(d, 0)
        self.addCleanup(os.chmod, d, 0o700)
        os.chmod(f, 0)
        [(level, message, hint)] = self.doctor(d)
        self.assertEqual((level, hint), ("FAIL", "check its permissions"))
        self.assertTrue(message.startswith("can't list %s: " % d), message)
        self.assertEqual(self.doctor(f), [("FAIL", "can't read %s" % f, "check its permissions")])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_fifo(self):
        fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(fifo)
        self.assertEqual(self.doctor(fifo), [("FAIL", "from.path %s is neither a file nor a "
                                              "directory" % fifo,
                                              "point from.path at a file or a directory")])


if __name__ == "__main__":
    unittest.main()
