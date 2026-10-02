"""The Stager: check, stage, commit and abort against a real file system (DESIGN §10)."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

from vcharon import fsops, lock, pathrules, stage
from vcharon.lock import Lock
from vcharon.plan import Plan, delete, put_dir, put_file
from vcharon.proto import VCharonError

from tests.test_lock import hold_in_child, stop_child
from tests.test_path import unix_socket
from tests.util import (fd_count, patch_stats, read_tree, run_stager, umask, unblock_fifo,
                        write_tree)

POSIX = os.name != "nt"
WINDOWS = os.name == "nt"
PREFIX = pathrules.STAGE_PREFIX
MTIME = 1790000000.25


def fputs(spec, mtime=MTIME):
    """File puts for {path: bytes}, and the data for run_stager."""
    return [put_file(p, len(b), mtime) for p, b in spec.items()], dict(spec)


def stage_dirs(root):
    return sorted(n for n in os.listdir(root) if n.startswith(PREFIX))


def hashed(path, data, mtime=MTIME, executable=None):
    """A file put of a --full run: with its bytes' sha256."""
    return put_file(path, len(data), mtime, executable, hashlib.sha256(data).hexdigest())


class Failing:
    """A reader that fails after its first chunk."""

    def __init__(self):
        self.calls = 0

    def read(self, n):
        self.calls += 1
        if self.calls == 1:
            return b"partial"
        raise VCharonError("vanished", "the source file went away")


class StagerCases:
    """Every case runs for each handle type."""

    impl = None

    def setUp(self):
        fsops._private_groups.clear()
        self.addCleanup(fsops._private_groups.clear)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ferry-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        self.outside = os.path.join(self.tmp, "outside")
        os.mkdir(self.root)
        write_tree(self.outside, {"z.txt": b"z", "b/": None})

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def stager(self, **kw):
        s = stage.Stager(kw.pop("root", self.root), impl=self.impl, **kw)
        self.addCleanup(s.close)
        return s

    def run_plan(self, entries, data=None, **kw):
        return run_stager(kw.pop("root", self.root), Plan(entries), data or {}, impl=self.impl,
                          **kw)

    def refused(self, entries, code, data=None, **kw):
        with self.assertRaises(VCharonError) as cm:
            self.run_plan(entries, data, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        self.assertEqual(stage_dirs(self.root) if os.path.isdir(self.root) else [], [])
        return cm.exception

    def refused_at_check(self, entries, code, **kw):
        """check() alone refuses the plan, and the target is as it was."""
        before = read_tree(self.root)
        s = self.stager(**kw)
        with self.assertRaises(VCharonError) as cm:
            s.check(Plan(entries))
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        s.close()
        self.assertEqual(read_tree(self.root), before)
        return cm.exception

    def backdate(self, rel):
        """Makes a stage dir old enough to be cleaned."""
        t = time.time() - 2 * stage.STALE_AGE
        # Windows has no os.utime(follow_symlinks=...); a stage dir there is never a link.
        if os.utime in os.supports_follow_symlinks:
            os.utime(self.path(rel), (t, t), follow_symlinks=False)
        else:
            os.utime(self.path(rel), (t, t))

    def patch(self, target, name, new):
        patcher = mock.patch.object(target, name, new)
        patcher.start()
        self.addCleanup(patcher.stop)

    # 1
    def test_basics(self):
        big = os.urandom(1 << 20)
        entries = [put_dir("a"), put_dir("a/b"),
                   put_file("a/b/big.bin", len(big), 1790000000.123456),
                   put_file("empty", 0, 1700000000.5),
                   put_file("a/run.sh", 3, MTIME, executable=True),
                   put_file("a/c/中 文.txt", 2, MTIME, executable=False)]
        data = {"a/b/big.bin": big, "empty": b"", "a/run.sh": b"#!\n", "a/c/中 文.txt": b"zh"}
        checked, done = self.run_plan(entries, data)
        self.assertEqual(checked, stage.Checked(root=os.path.realpath(self.root), notes=[],
                                                deletes=0))
        self.assertEqual(read_tree(self.root), {
            "a/": None, "a/b/": None, "a/c/": None, "a/b/big.bin": big, "empty": b"",
            "a/run.sh": b"#!\n", "a/c/中 文.txt": b"zh"})
        self.assertEqual(done.written, ["a", "a/b", "a/b/big.bin", "empty", "a/run.sh",
                                        "a/c/中 文.txt"])
        self.assertEqual((done.deleted, done.notes), (0, []))
        for rel, e in (("a/b/big.bin", entries[2]), ("empty", entries[3])):
            ns = os.stat(self.path(rel)).st_mtime_ns
            self.assertLess(abs(ns - e.mtime * 1e9), 1000, rel)
        if POSIX:
            new = 0o666 & ~umask()
            self.assertEqual(stat.S_IMODE(os.stat(self.path("empty")).st_mode), new)
            self.assertEqual(stat.S_IMODE(os.stat(self.path("a/run.sh")).st_mode),
                             new | ((new & 0o444) >> 2))
            self.assertEqual(stat.S_IMODE(os.stat(self.path("a/c/中 文.txt")).st_mode), new)
            self.assertEqual(stat.S_IMODE(os.stat(self.path("a/c")).st_mode), 0o777 & ~umask())

    # 2
    def test_dry_run_changes_nothing(self):
        write_tree(self.root, {"old.txt": b"o", "d/x": b"x"})
        before = (read_tree(self.root), os.stat(self.root).st_mtime_ns)
        entries, _ = fputs({"n/new.txt": b"n"})
        s = self.stager()
        checked = s.check(Plan(entries + [delete("old.txt"), delete("d", tree=True)]))
        self.assertEqual(checked.deletes, 3)
        s.close()
        self.assertEqual((read_tree(self.root), os.stat(self.root).st_mtime_ns), before)
        missing = os.path.join(self.tmp, "new", "root")
        with stage.Stager(missing, create=True, impl=self.impl) as s:
            s.check(Plan(entries))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "new")))

    # 3
    def test_commit_order(self):
        write_tree(self.root, {"old/deep/f": b"f", "gone.txt": b"g"})
        calls = []

        def recording(name):
            real = getattr(self.cls, name)

            def method(d, *args):
                target = "/".join(d.rel + (args[-1] if name == "move_in" else args[0],))
                if not target.startswith(PREFIX):
                    calls.append((name, target))
                return real(d, *args)
            return method

        for name in ("mkdir", "unlink", "rmdir", "move_in"):
            self.patch(self.cls, name, recording(name))
        entries = [put_file("x/f1", 1, MTIME), delete("old/deep/f"), put_dir("n"),
                   delete("gone.txt"), put_dir("n/m"), delete("old/deep"),
                   put_file("f0", 1, MTIME)]
        self.run_plan(entries, {"x/f1": b"1", "f0": b"0"})
        self.assertEqual(calls, [("unlink", "old/deep/f"), ("rmdir", "old/deep"),
                                 ("unlink", "gone.txt"), ("mkdir", "n"), ("mkdir", "n/m"),
                                 ("mkdir", "x"), ("move_in", "x/f1"), ("move_in", "f0")])

    def test_order_outcomes(self):
        write_tree(self.root, {"a": b"file", "d/e": b"e"})
        checked, done = self.run_plan([put_file("a/b.txt", 1, MTIME), delete("a"),
                                       delete("d"), delete("d/e")], {"a/b.txt": b"b"})
        self.assertEqual((checked.deletes, done.deleted), (3, 3))
        self.assertEqual(read_tree(self.root), {"a/": None, "a/b.txt": b"b"})

    # 4
    def test_deletes(self):
        write_tree(self.root, {"keep/x": b"x", "t/a": b"a", "t/s/b": b"b", "t/s/c": b"c",
                               "p/a": b"a", "p/b/": None, "f.txt": b"f"})
        if POSIX:
            os.symlink(self.outside, self.path("t/s/link"))
            # inside a tree delete only the device rule holds
            os.chmod(self.path("t/s"), 0o777)
        tree_count = 6 if POSIX else 5
        entries = [delete("keep"), delete("t", tree=True), delete("missing"),
                   delete("missing/below"), delete("f.txt/below"), delete("p"), delete("p/a"),
                   delete("p/b"), delete("f.txt")]
        checked, done = self.run_plan(entries)
        self.assertEqual(checked.notes, ["kept keep: it isn't empty"])
        self.assertEqual(checked.deletes, tree_count + 4)
        self.assertEqual(done.deleted, tree_count + 4)
        self.assertEqual(done.notes, ["kept keep: it isn't empty"])
        self.assertEqual(read_tree(self.root), {"keep/": None, "keep/x": b"x"})
        self.assertEqual(read_tree(self.outside), {"z.txt": b"z", "b/": None})

    # 5
    def test_kind_change(self):
        write_tree(self.root, {"d/": None, "full/x": b"x", "f": b"f", "a": b"a"})
        cases = [
            ([put_file("d", 1, MTIME)], "d is a directory at the target, but the plan puts a "
                                        "file there"),
            ([delete("full"), put_file("full", 1, MTIME)],
             "full is a directory at the target, but the plan puts a file there"),
            ([put_dir("f")], "f is a file at the target, but the plan needs a directory there"),
            ([put_file("a/b.txt", 1, MTIME)],
             "a is a file at the target, but the plan needs a directory there"),
            ([put_file("f/x/y", 1, MTIME)],
             "f is a file at the target, but the plan needs a directory there"),
        ]
        data = {"d": b"1", "full": b"1", "a/b.txt": b"1", "f/x/y": b"1"}
        for entries, message in cases:
            with self.subTest(message=message):
                e = self.refused_at_check(entries, "kind_change")
                self.assertEqual(e.message, message)
                self.assertEqual(e.hint, "remove or rename it at the target, then run again")
        self.run_plan([delete("full", tree=True), put_file("full", 1, MTIME), delete("d"),
                       put_file("d", 1, MTIME), delete("f"), put_dir("f"), delete("a"),
                       put_file("a/b.txt", 1, MTIME)], data)
        self.assertEqual(read_tree(self.root), {"full": b"1", "d": b"1", "f/": None, "a/": None,
                                                "a/b.txt": b"1"})

    # 6
    def test_spelling(self):
        probe = os.path.join(self.tmp, "Probe")
        open(probe, "wb").close()
        if not os.path.exists(os.path.join(self.tmp, "pROBE")):
            self.skipTest("this file system tells case apart")
        nfc = unicodedata.normalize("NFC", "café.txt")
        nfd = unicodedata.normalize("NFD", nfc)
        # The NFC put onto the file held as NFD needs a file system that folds the two as one
        # name, as APFS does. NTFS folds case only (DESIGN §10.1): there only the case part runs.
        open(self.path(nfd), "wb").close()
        try:
            folds_nfd = os.path.exists(self.path(nfc))
        finally:
            os.remove(self.path(nfd))
        tree = {"README.md": b"old", "Docs/": None}
        entries = [put_file("readme.md", 3, MTIME), put_file("docs/a.txt", 1, MTIME)]
        data = {"readme.md": b"new", "docs/a.txt": b"a"}
        want = {"Docs/": None, "Docs/a.txt": b"a", "readme.md": b"new"}
        if folds_nfd:
            tree[nfd] = b"old"
            entries.insert(1, put_file(nfc, 3, MTIME))
            data[nfc] = want[nfc] = b"new"
        write_tree(self.root, tree)
        self.run_plan(entries, data)
        self.assertEqual(sorted(os.listdir(self.root)),
                         sorted(["Docs", "readme.md"] + ([nfc] if folds_nfd else [])))
        self.assertEqual(read_tree(self.root), want)

    # 7
    def test_max_deletes(self):
        write_tree(self.root, {"t/1": b"", "t/2": b"", "t/3": b"", "t/s/4": b"", "t/s/5": b""})
        entries = [delete("t", tree=True)]
        e = self.refused_at_check(entries, "too_many_deletes", max_deletes=6)
        self.assertEqual(e.message, "the plan deletes 7 files and directories, more than "
                                    "max_deletes (6)")
        for limit in (7, 0):
            with self.stager(max_deletes=limit) as s:
                self.assertEqual(s.check(Plan(entries)).deletes, 7)
        self.assertEqual(self.run_plan(entries, max_deletes=7)[1].deleted, 7)

    # 8
    @unittest.skipUnless(POSIX, "permission bits are POSIX only")
    def test_permission_bits(self):
        modes = {"a": 0o640, "b": 0o640, "c": 0o755, "d": 0o4755, "e": 0o600}
        write_tree(self.root, {name: b"old" for name in modes})
        for name, mode in modes.items():
            os.chmod(self.path(name), mode)
        # e is deleted first, so the put makes a new file
        entries = [put_file("a", 3, MTIME), put_file("b", 3, MTIME, executable=True),
                   put_file("c", 3, MTIME, executable=False), put_file("d", 3, MTIME),
                   delete("e"), put_file("e", 3, MTIME)]
        self.run_plan(entries, {name: b"new" for name in modes})
        got = {name: stat.S_IMODE(os.stat(self.path(name)).st_mode) for name in modes}
        self.assertEqual(got, {"a": 0o640, "b": 0o750, "c": 0o644, "d": 0o755,
                               "e": 0o666 & ~umask()})
        self.assertEqual(read_tree(self.root), {name: b"new" for name in modes})

    # 9
    @unittest.skipUnless(POSIX, "the owner and mode rule is POSIX only")
    def test_owner_and_mode(self):
        entries, data = fputs({"a/x": b"x"})
        self.patch(fsops, "private_group", lambda gid: False)
        os.chmod(self.root, 0o775)
        e = self.refused_at_check(entries, "unsafe_dir")
        self.assertEqual(e.message, "the root %s is writable by its group" % self.root)
        self.assertEqual(e.hint, "fix its owner or mode (for example: chmod go-w %s), then run "
                                 "again" % self.root)
        os.chmod(self.root, 0o755)
        write_tree(self.root, {"a/": None, "s/": None})
        os.chmod(self.path("a"), 0o777)
        e = self.refused_at_check(entries, "unsafe_dir")
        self.assertEqual(e.message, "a is writable by others")
        os.chmod(self.path("a"), 0o755)
        os.chmod(self.path("s"), 0o1777)
        self.run_plan(*fputs({"s/x": b"x"}))
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            e = self.refused_at_check(entries, "unsafe_dir")
        self.assertEqual(e.message, "the root %s is owned by another user" % self.root)

    def set_umask(self, mask):
        old = os.umask(mask)
        self.addCleanup(os.umask, old)

    @unittest.skipUnless(POSIX, "the owner and mode rule is POSIX only")
    def test_group_writable_dirs(self):
        # made before the umask changes, but a umask of 0002 (as on Debian) makes it 0o775
        os.chmod(self.root, 0o755)
        self.set_umask(0o002)
        with mock.patch.object(fsops, "private_group", return_value=True):
            self.run_plan(*fputs({"a/b/x": b"x"}))
            self.assertEqual(stat.S_IMODE(os.stat(self.path("a/b")).st_mode), 0o775)
            self.run_plan(*fputs({"a/b/y": b"y"}))
        with mock.patch.object(fsops, "private_group", return_value=False):
            entries, data = fputs({"a/b/z": b"z"})
            e = self.refused_at_check(entries, "unsafe_dir")
        self.assertEqual(e.message, "a is writable by its group")

    @unittest.skipUnless(POSIX, "the owner and mode rule is POSIX only")
    def test_group_writable_dirs_unpatched(self):
        import pwd
        if not fsops.private_group(pwd.getpwuid(os.geteuid()).pw_gid):
            self.skipTest("your primary group isn't a private group here")
        self.set_umask(0o002)
        os.chmod(self.root, 0o775)
        self.run_plan(*fputs({"a/b/x": b"x"}))
        self.assertEqual(stat.S_IMODE(os.stat(self.path("a/b")).st_mode), 0o775)
        self.run_plan(*fputs({"a/b/y": b"y"}))
        self.assertEqual(read_tree(self.root), {"a/": None, "a/b/": None, "a/b/x": b"x",
                                                "a/b/y": b"y"})

    # 10
    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_links_at_check(self):
        os.symlink(self.outside, self.path("l"))
        for entries in ([put_file("l/x", 1, MTIME)], [delete("l/z.txt")],
                        [delete("l"), put_file("l/x", 1, MTIME)]):
            with self.subTest(entries=entries):
                e = self.refused_at_check(entries, "unsafe_path")
                self.assertEqual(e.message,
                                 "l is a symlink or junction; ferry never goes through one")
        # a link that the plan replaces or deletes is fine: it's never followed
        self.run_plan([delete("l")])
        self.assertEqual(read_tree(self.outside), {"z.txt": b"z", "b/": None})

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_symlinked_root(self):
        link = os.path.join(self.tmp, "rootlink")
        os.symlink(self.root, link)
        checked, done = self.run_plan(*fputs({"x": b"x"}), root=link)
        self.assertEqual(checked.root, self.root)
        self.assertEqual(read_tree(self.root), {"x": b"x"})

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_root_through_foreign_link(self):
        link = os.path.join(self.tmp, "drop")
        os.symlink(self.root, link)
        patch_stats(self, link, st_uid=os.geteuid() + 1)
        before = read_tree(self.tmp)
        with self.stager(root=os.path.join(link, "sub"), create=True) as s:
            with self.assertRaises(VCharonError) as cm:
                s.check(Plan(fputs({"x": b"x"})[0]))
        self.assertEqual(cm.exception.code, "unsafe_dir")
        self.assertEqual(cm.exception.message, "the root %s/sub goes through %s, a symlink "
                         "owned by another user" % (link, link))
        self.assertEqual(read_tree(self.tmp), before)

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_missing_root_planted_as_foreign_link(self):
        new = os.path.join(self.tmp, "new")
        entries, data = fputs({"x": b"x"})
        s = self.stager(root=os.path.join(new, "root"), create=True)
        s.check(Plan(entries))
        os.symlink(self.outside, new)
        patch_stats(self, new, st_uid=os.geteuid() + 1)
        with self.assertRaises(VCharonError) as cm:
            s.stage(0, io.BytesIO(b"x"))
        self.assertEqual(cm.exception.code, "unsafe_dir")
        s.close()
        self.assertEqual(read_tree(self.outside), {"z.txt": b"z", "b/": None})

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_missing_root_planted_as_own_link(self):
        new = os.path.join(self.tmp, "new")
        entries, data = fputs({"x": b"x"})
        s = self.stager(root=os.path.join(new, "root"), create=True)
        s.check(Plan(entries))
        os.symlink(self.outside, new)
        with self.assertRaises(VCharonError) as cm:
            s.stage(0, io.BytesIO(b"x"))
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(cm.exception.message,
                         "the root %s/root changed during the run" % new)
        s.close()
        self.assertEqual(read_tree(self.outside), {"z.txt": b"z", "b/": None})

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_dir_swapped_after_check(self):
        write_tree(self.root, {"a/": None})
        entries, data = fputs({"a/z.txt": b"new", "a/b/y": b"y"})
        s = self.stager()
        s.check(Plan(entries))
        s.stage(0, io.BytesIO(b"new"))
        s.stage(1, io.BytesIO(b"y"))
        os.rename(self.path("a"), self.path("a2"))
        os.symlink(self.outside, self.path("a"))
        with self.assertRaises(VCharonError) as cm:
            s.commit()
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(read_tree(self.outside), {"z.txt": b"z", "b/": None})
        self.assertEqual(stage_dirs(self.root), [])
        self.assertEqual(s.done.written, [])

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_parent_swapped_during_commit(self):
        write_tree(self.root, {"a/b/": None})
        real = self.cls.move_in

        def move_in(d, src_dir, src_name, name):
            if not os.path.islink(self.path("a")):
                os.rename(self.path("a"), self.path("a2"))
                os.symlink(self.outside, self.path("a"))
            return real(d, src_dir, src_name, name)

        self.patch(self.cls, "move_in", move_in)
        entries, data = fputs({"a/b/x": b"x"})
        if self.impl == "path":
            e = self.refused(entries, "unsafe_path", data)
            self.assertEqual(e.message, "a is a symlink or junction; ferry never goes through one")
        else:
            self.run_plan(entries, data)
            self.assertEqual(read_tree(self.path("a2")), {"b/": None, "b/x": b"x"})
        self.assertEqual(read_tree(self.outside), {"z.txt": b"z", "b/": None})

    # 11
    def fake_device(self, rel):
        """Every stat of the directory at rel reports another device."""
        path = self.path(rel)
        patch_stats(self, path, st_dev=os.lstat(path).st_dev + 1)

    def test_other_file_system(self):
        write_tree(self.root, {"a/": None, "t/sub/x": b"x"})
        self.fake_device("a")
        entries, data = fputs({"a/x": b"x"})
        e = self.refused_at_check(entries, "unsafe_dir")
        self.assertEqual(e.message, "a is on another file system")
        self.assertEqual(e.hint, "pick a root with no mount points under it")

    def test_other_file_system_in_tree_delete(self):
        write_tree(self.root, {"t/sub/x": b"x"})
        self.fake_device("t/sub")
        e = self.refused_at_check([delete("t", tree=True)], "unsafe_dir")
        self.assertEqual(e.message, "t/sub is on another file system")

    # 12
    def test_stage_dir(self):
        entries, data = fputs({"x": b"x", "y": b"y"})
        s = self.stager()
        s.check(Plan(entries))
        self.assertEqual(stage_dirs(self.root), [])
        s.stage(0, io.BytesIO(b"x"))
        [name] = stage_dirs(self.root)
        sdir = self.path(name)
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(sdir).st_mode), 0o700)
        with Lock.open(os.path.join(sdir, "lock"), create=False) as lk:
            self.assertFalse(lk.try_acquire())
        self.assertEqual(sorted(os.listdir(sdir)), ["0", "lock"])
        s.stage(1, io.BytesIO(b"y"))
        s.commit()
        self.assertEqual(read_tree(self.root), {"x": b"x", "y": b"y"})

    def test_stage_dir_for_deletes_only(self):
        write_tree(self.root, {"old": b"o"})
        made = []
        real = self.cls.mkdir

        def mkdir(d, name, mode=0o777):
            made.append((name[:len(PREFIX)], mode))
            return real(d, name, mode)

        self.patch(self.cls, "mkdir", mkdir)
        self.run_plan([delete("old")])
        self.assertEqual(made, [(PREFIX, 0o700)])
        self.assertEqual(read_tree(self.root), {})

    def test_abort_and_close(self):
        entries, data = fputs({"x": b"x"})
        for how in ("abort", "close"):
            with self.subTest(how=how):
                s = self.stager()
                s.check(Plan(entries))
                s.stage(0, io.BytesIO(b"x"))
                self.assertEqual(len(stage_dirs(self.root)), 1)
                getattr(s, how)()
                getattr(s, how)()
                self.assertEqual(read_tree(self.root), {})
                with self.assertRaises(VCharonError) as cm:
                    s.commit()
                self.assertEqual(cm.exception.code, "internal")

    def test_commit_fails_partway(self):
        write_tree(self.root, {"d/": None})
        entries, data = fputs({"x.txt": b"x", "d/y.txt": b"y", "z.txt": b"z"})
        s = self.stager()
        s.check(Plan(entries))
        for i, e in enumerate(entries):
            s.stage(i, io.BytesIO(data[e.path]))
        os.rmdir(self.path("d"))
        write_tree(self.root, {"d": b"now a file"})
        with self.assertRaises(VCharonError) as cm:
            s.commit()
        self.assertEqual(cm.exception.code, "kind_change")
        self.assertEqual(s.done.written, ["x.txt"])
        self.assertEqual(read_tree(self.root), {"x.txt": b"x", "d": b"now a file"})
        with self.assertRaises(VCharonError):
            s.commit()

    # 13
    def test_stale_stage_dirs(self):
        write_tree(self.root, {PREFIX + "unlocked/lock": b"", PREFIX + "unlocked/3": b"3",
                               PREFIX + "nolock/sub/7": b"7", PREFIX + "f": b"a file",
                               PREFIX + "live/lock": b""})
        if POSIX:
            os.symlink(self.outside, self.path(PREFIX + "x"))
        for name in ("unlocked", "nolock", "f", "live") + (("x",) if POSIX else ()):
            self.backdate(PREFIX + name)
        child = hold_in_child(self.path(PREFIX + "live/lock"))
        try:
            checked, done = self.run_plan(*fputs({"n": b"n"}))
            self.assertEqual(done.notes, [])
            left = {PREFIX + "f", PREFIX + "live"} | ({PREFIX + "x"} if POSIX else set())
            self.assertEqual(set(stage_dirs(self.root)), left)
        finally:
            stop_child(child)
        self.run_plan([delete("n")])
        self.assertEqual(set(stage_dirs(self.root)), left - {PREFIX + "live"})
        self.assertEqual(read_tree(self.outside), {"z.txt": b"z", "b/": None})

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_stale_stage_dir_that_cant_go(self):
        stuck = self.path(PREFIX + "stuck")
        write_tree(stuck, {"sub/f": b"f"})
        os.chmod(os.path.join(stuck, "sub"), 0o500)
        self.addCleanup(os.chmod, os.path.join(stuck, "sub"), 0o700)
        self.backdate(PREFIX + "stuck")
        logged = []
        checked, done = self.run_plan(*fputs({"n": b"n"}), log=logged.append)
        self.assertEqual(len(done.notes), 1)
        self.assertTrue(done.notes[0].startswith(
            "couldn't remove the old stage dir %s: " % (PREFIX + "stuck")), done.notes)
        self.assertEqual(logged, done.notes)
        self.assertEqual(read_tree(self.root)["n"], b"n")

    def test_young_stage_dir_is_kept(self):
        write_tree(self.root, {PREFIX + "young/0": b"staged"})
        self.run_plan(*fputs({"n": b"n"}))
        self.assertEqual(stage_dirs(self.root), [PREFIX + "young"])
        self.backdate(PREFIX + "young")
        self.run_plan([delete("n")])
        self.assertEqual(read_tree(self.root), {})

    def test_stale_lock_error_is_a_note(self):
        write_tree(self.root, {PREFIX + "old/lock": b""})
        self.backdate(PREFIX + "old")
        real = Lock.try_acquire
        calls = []

        def try_acquire(lk):
            calls.append(lk)
            # the run's own lock first, then the old dir's
            if len(calls) == 1:
                return real(lk)
            raise OSError(5, "Input/output error")

        self.patch(lock.Lock, "try_acquire", try_acquire)
        fds = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
        checked, done = self.run_plan(*fputs({"n": b"n"}))
        self.assertEqual(done.notes, ["couldn't remove the old stage dir %sold: Input/output "
                                      "error" % PREFIX])
        self.assertEqual([lk.fd for lk in calls], [None, None])
        if fds is not None:
            self.assertEqual(len(os.listdir("/dev/fd")), fds)

    def test_root_listing_error_is_a_note(self):
        real = self.cls.listdir

        def listdir(d):
            if d.rel == ():
                raise OSError(5, "Input/output error")
            return real(d)

        self.patch(self.cls, "listdir", listdir)
        checked, done = self.run_plan(*fputs({"n": b"n"}))
        self.assertEqual(done.notes, ["couldn't look for old stage dirs: Input/output error"])
        self.assertEqual(done.written, ["n"])

    # 14
    def test_create(self):
        missing = os.path.join(self.tmp, "new", "root")
        entries, data = fputs({"a/x": b"x"})
        e = self.refused_at_check(entries, "not_found", root=missing)
        self.assertEqual(e.message, "the root %s doesn't exist" % missing)
        self.assertEqual(e.hint, "create it first (in a job: to.create = yes)")
        checked, done = self.run_plan(entries, data, root=missing, create=True)
        self.assertEqual(checked, stage.Checked(root=missing, notes=[], deletes=0))
        self.assertEqual(read_tree(missing), {"a/": None, "a/x": b"x"})
        write_tree(self.tmp, {"file": b"f"})
        below = os.path.join(self.tmp, "file", "sub", "root")
        e = self.refused_at_check(entries, "not_found", root=below, create=True)
        self.assertEqual(e.message, "can't create %s: %s isn't a directory"
                         % (below, os.path.join(self.tmp, "file")))
        e = self.refused_at_check(entries, "not_found", root=os.path.join(self.tmp, "file"),
                                  create=True)
        self.assertEqual(e.message, "%s isn't a directory" % os.path.join(self.tmp, "file"))

    def test_relative_root(self):
        with self.assertRaises(VCharonError) as cm:
            stage.Stager("relative/root", impl=self.impl)
        self.assertEqual(cm.exception.code, "internal")

    # 15
    def test_staging_errors(self):
        write_tree(self.root, {"keep": b"k"})
        entries = [put_dir("d"), put_file("f", 3, MTIME), delete("keep")]
        s = self.stager()
        with self.assertRaises(VCharonError) as cm:
            s.stage(1, io.BytesIO(b"f"))
        self.assertEqual(cm.exception.code, "internal")
        s.check(Plan(entries))
        with self.assertRaises(VCharonError) as cm:
            s.check(Plan(entries))
        self.assertEqual(cm.exception.code, "internal")
        for index in (0, 2, 3, -1, "1", True):
            with self.subTest(index=index):
                with self.assertRaises(VCharonError) as cm:
                    s.stage(index, io.BytesIO(b"f"))
                self.assertEqual(cm.exception.code, "protocol")
        reader = Failing()
        with self.assertRaises(VCharonError) as cm:
            s.stage(1, reader)
        self.assertEqual(cm.exception.code, "vanished")
        [name] = stage_dirs(self.root)
        self.assertEqual(os.listdir(self.path(name)), ["lock"])
        with self.assertRaises(VCharonError) as cm:
            s.commit()
        self.assertEqual(cm.exception.code, "protocol")
        self.assertEqual(cm.exception.message, "file 1 wasn't staged")
        s.stage(1, io.BytesIO(b"fff"))
        with self.assertRaises(VCharonError) as cm:
            s.stage(1, io.BytesIO(b"f"))
        self.assertEqual(cm.exception.message, "file 1 was staged twice")
        s.close()
        self.assertEqual(read_tree(self.root), {"keep": b"k"})

    # 16
    @unittest.skipUnless(POSIX, "setgid is POSIX only")
    def test_setgid(self):
        write_tree(self.root, {"g/": None})
        gdir = self.path("g")
        root_gid = os.stat(self.root).st_gid
        for gid in os.getgroups():
            if gid == root_gid:
                continue
            try:
                os.chown(gdir, -1, gid)
            except OSError:
                continue
            break
        else:
            self.skipTest("no other group to use")
        os.chmod(gdir, 0o2755)
        if not os.stat(gdir).st_mode & stat.S_ISGID:
            self.skipTest("this file system doesn't keep setgid")
        checked, done = self.run_plan(*fputs({"g/f.txt": b"f", "top.txt": b"t"}))
        self.assertEqual(os.stat(self.path("g/f.txt")).st_gid, gid)
        self.assertEqual(os.stat(self.path("top.txt")).st_gid, root_gid)
        self.assertEqual(done.notes, [])

    # 19: have, the files the target already holds (DESIGN §10.5)

    def count_reads(self):
        """Counts the handles' open_read calls for the rest of the test."""
        opened = []
        real = self.cls.open_read

        def open_read(d, name):
            opened.append("/".join(d.rel + (name,)))
            return real(d, name)

        self.patch(self.cls, "open_read", open_read)
        return opened

    def assert_mtime(self, rel, mtime=MTIME):
        self.assertLess(abs(os.stat(self.path(rel)).st_mtime_ns - mtime * 1e9), 1000, rel)

    def test_have_same_bytes(self):
        modes = {"x.txt": 0o644, "y.txt": 0o755, "z.txt": 0o640, "s.txt": 0o4755}
        spec = {name: ("bytes of %s" % name).encode() for name in modes}
        spec["d/deep.bin"] = os.urandom(3 << 18)
        write_tree(self.root, spec)
        if POSIX:
            for name, mode in modes.items():
                os.chmod(self.path(name), mode)
        inodes = {rel: os.stat(self.path(rel)).st_ino for rel in spec}
        entries = [put_dir("d"), hashed("d/deep.bin", spec["d/deep.bin"]),
                   hashed("x.txt", spec["x.txt"], executable=True),
                   hashed("y.txt", spec["y.txt"], executable=False),
                   hashed("z.txt", spec["z.txt"]),
                   hashed("s.txt", spec["s.txt"], executable=True),
                   hashed("new.txt", b"new")]
        logged = []
        opened = self.count_reads()
        before = fd_count() if POSIX else None
        s = self.stager(log=logged.append)
        checked = s.check(Plan(entries))
        # the missing new.txt is never opened
        self.assertEqual(sorted(opened), sorted(spec))
        self.assertEqual(checked.have, [1, 2, 3, 4, 5])
        self.assertEqual(logged, ["check: hashed 5 files (%d bytes), 5 already there"
                                  % sum(len(b) for b in spec.values())])
        # only new.txt is staged, and the commit needs nothing more
        s.stage(6, io.BytesIO(b"new"))
        done = s.commit()
        self.assertEqual(done.written, ["d", "d/deep.bin", "x.txt", "y.txt", "z.txt", "s.txt",
                                        "new.txt"])
        self.assertEqual(read_tree(self.root), dict(spec, **{"d/": None, "new.txt": b"new"}))
        for rel in spec:
            self.assertEqual(os.stat(self.path(rel)).st_ino, inodes[rel], rel)
            self.assert_mtime(rel)
        if POSIX:
            got = {name: stat.S_IMODE(os.stat(self.path(name)).st_mode) for name in modes}
            # True adds x where there is r, False clears x, None keeps them; setuid goes
            self.assertEqual(got, {"x.txt": 0o755, "y.txt": 0o644, "z.txt": 0o640,
                                   "s.txt": 0o755})
            s.close()
            self.assertEqual(fd_count(), before)

    def test_have_other_bytes_or_size(self):
        write_tree(self.root, {"same-size": b"aaaa", "other-size": b"aaa"})
        inode = os.stat(self.path("same-size")).st_ino
        opened = self.count_reads()
        entries = [hashed("same-size", b"bbbb"), hashed("other-size", b"bbbb")]
        checked, done = self.run_plan(entries, {"same-size": b"bbbb", "other-size": b"bbbb"})
        self.assertEqual(checked.have, [])
        # another size is never read
        self.assertEqual(opened, ["same-size"])
        self.assertEqual(read_tree(self.root), {"same-size": b"bbbb", "other-size": b"bbbb"})
        self.assertNotEqual(os.stat(self.path("same-size")).st_ino, inode)
        self.assert_mtime("same-size")

    def test_have_needs_a_put_that_isnt_deleted_first(self):
        write_tree(self.root, {"d/x": b"same", "y": b"same"})
        opened = self.count_reads()
        # a put under a deleted parent, and a delete-and-put of the same path: new files
        entries = [delete("d", tree=True), hashed("d/x", b"same"), delete("y"),
                   hashed("y", b"same")]
        checked, done = self.run_plan(entries, {"d/x": b"same", "y": b"same"})
        self.assertEqual((checked.have, opened), ([], []))
        self.assertEqual(read_tree(self.root), {"d/": None, "d/x": b"same", "y": b"same"})

    @unittest.skipUnless(POSIX, "needs symlinks and FIFOs")
    def test_have_needs_a_regular_file(self):
        write_tree(self.outside, {"same.txt": b"same"})
        os.symlink(os.path.join(self.outside, "same.txt"), self.path("link"))
        os.mkfifo(self.path("fifo"))
        unblock_fifo(self, self.path("fifo"))
        opened = self.count_reads()
        entries = [hashed("link", b"same"), hashed("fifo", b"same")]
        checked, done = self.run_plan(entries, {"link": b"same", "fifo": b"same"})
        self.assertEqual((checked.have, opened), ([], []))
        # replaced, never followed
        self.assertEqual(read_tree(self.root), {"link": b"same", "fifo": b"same"})
        self.assertEqual(read_tree(self.outside), {"z.txt": b"z", "b/": None,
                                                   "same.txt": b"same"})
        # a directory is still M2's kind_change
        write_tree(self.root, {"dir/": None})
        e = self.refused_at_check([hashed("dir", b"same")], "kind_change")
        self.assertEqual(e.message, "dir is a directory at the target, but the plan puts a file "
                                    "there")

    @unittest.skipUnless(POSIX, "owners are POSIX only")
    def test_have_needs_your_file(self):
        # the commit sets its mtime and mode, which takes the owner
        write_tree(self.root, {"theirs": b"same"})
        patch_stats(self, self.path("theirs"), st_uid=os.geteuid() + 1)
        opened = self.count_reads()
        s = self.stager()
        self.assertEqual(s.check(Plan([hashed("theirs", b"same")])).have, [])
        self.assertEqual(opened, [])

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_have_unreadable_target(self):
        if os.geteuid() == 0:
            self.skipTest("root can read every file")
        write_tree(self.root, {"locked": b"same"})
        os.chmod(self.path("locked"), 0)
        inode = os.stat(self.path("locked")).st_ino
        logged = []
        checked, done = self.run_plan([hashed("locked", b"same")], {"locked": b"same"},
                                      log=logged.append)
        self.assertEqual(checked.have, [])
        self.assertTrue(logged[0].startswith("check: couldn't hash locked, so it's sent: "),
                        logged)
        # replaced: a new file, with the old one's bits
        st = os.stat(self.path("locked"))
        self.assertNotEqual(st.st_ino, inode)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0)
        os.chmod(self.path("locked"), 0o600)
        self.assertEqual(read_tree(self.root), {"locked": b"same"})

    def test_stage_of_a_have_put(self):
        write_tree(self.root, {"x": b"same"})
        s = self.stager()
        self.assertEqual(s.check(Plan([hashed("x", b"same")])).have, [0])
        with self.assertRaises(VCharonError) as cm:
            s.stage(0, io.BytesIO(b"same"))
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ("protocol", "entry 0 is already at the target"))
        # nothing was staged for it, and the commit needs nothing
        self.assertEqual(s.commit().written, ["x"])

    OLD = 1700000000

    def have_one(self, **plan_kw):
        """A Stager whose check put x (b"same", mtime OLD) in have."""
        write_tree(self.root, {"x": b"same"})
        os.utime(self.path("x"), (self.OLD, self.OLD))
        s = self.stager()
        self.assertEqual(s.check(Plan([hashed("x", b"same", **plan_kw)])).have, [0])
        return s

    def refused_touch(self, s):
        with self.assertRaises(VCharonError) as cm:
            s.commit()
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ("vanished", "x changed at the target during the run"))
        self.assertEqual(s.done.written, [])
        self.assertEqual(stage_dirs(self.root), [])

    def test_have_changed_before_the_commit(self):
        # the commit never stamps the source's mtime on another file, or other bytes
        def new_file():
            # made while the old one still exists, so its inode can't be reused
            write_tree(self.root, {"x.new": b"same"})
            os.replace(self.path("x.new"), self.path("x"))

        def grown():
            with open(self.path("x"), "ab") as f:
                f.write(b"+")

        def edited():
            # in place, the same size: only the mtime tells
            with open(self.path("x"), "r+b") as f:
                f.write(b"SAME")

        for how, change in (("a new file", new_file), ("grown", grown), ("edited", edited)):
            with self.subTest(how=how):
                shutil.rmtree(self.root)
                s = self.have_one()
                change()
                self.refused_touch(s)
                s.close()
                # not stamped with the plan's mtime
                self.assertGreater(abs(os.stat(self.path("x")).st_mtime - MTIME), 1)

    def test_have_edited_while_it_was_hashed(self):
        # the ident comes from the hashing fd before the bytes are read: an edit during the
        # hash shows at the commit, even one that writes the same bytes back
        write_tree(self.root, {"x": b"same"})
        os.utime(self.path("x"), (self.OLD, self.OLD))
        real = self.cls.open_read
        path = self.path("x")

        class Editing:
            def __init__(self, reader):
                self.reader = reader
                self.edited = False

            def fileno(self):
                return self.reader.fileno()

            def read(self, n):
                if not self.edited:
                    self.edited = True
                    with open(path, "r+b") as f:
                        f.write(b"same")
                return self.reader.read(n)

            def close(self):
                self.reader.close()

        self.patch(self.cls, "open_read", lambda d, name: Editing(real(d, name)))
        s = self.stager()
        self.assertEqual(s.check(Plan([hashed("x", b"same")])).have, [0])
        self.refused_touch(s)

    @unittest.skipUnless(POSIX, "needs symlinks, hard links and sockets")
    def test_have_swapped_before_the_commit(self):
        target = os.path.join(self.outside, "target.txt")

        def link():
            os.remove(self.path("x"))
            os.symlink(target, self.path("x"))

        def hard_link():
            # another name for x, outside the root: touching x would change it
            os.link(self.path("x"), target)

        def socket():
            os.remove(self.path("x"))
            unix_socket(self, self.path("x"))

        for how, change in (("a link", link), ("a hard link", hard_link), ("a socket", socket)):
            with self.subTest(how=how):
                shutil.rmtree(self.root)
                if os.path.lexists(target):
                    os.remove(target)
                if how != "a hard link":
                    write_tree(self.outside, {"target.txt": b"same"})
                    os.chmod(target, 0o640)
                    os.utime(target, (self.OLD, self.OLD))
                s = self.have_one(executable=True)
                if how == "a hard link":
                    os.chmod(self.path("x"), 0o640)
                    os.utime(self.path("x"), (self.OLD, self.OLD))
                    # the chmod changed the ctime only; the ident still matches
                change()
                self.refused_touch(s)
                s.close()
                st = os.stat(target)
                self.assertEqual((stat.S_IMODE(st.st_mode), st.st_mtime), (0o640, self.OLD))

    def test_have_ident_from_the_hashing_fd(self):
        # A (other bytes, the plan's size) is at x when the check lstats it, B (the plan's
        # bytes) when the check opens it to hash, and A again at the commit. An ident from the
        # lstat would pass A at the commit, and stamp the source's mtime on other bytes.
        side = os.path.join(self.outside, "side")
        write_tree(side, {"b": b"same"})
        write_tree(self.root, {"x": b"AAAA"})
        if POSIX:
            os.chmod(self.path("x"), 0o640)
        os.utime(self.path("x"), (self.OLD, self.OLD))
        real = self.cls.open_read

        def open_read(d, name):
            os.rename(self.path("x"), os.path.join(side, "a"))
            os.rename(os.path.join(side, "b"), self.path("x"))
            return real(d, name)

        self.patch(self.cls, "open_read", open_read)
        s = self.stager()
        self.assertEqual(s.check(Plan([hashed("x", b"same", executable=True)])).have, [0])
        os.rename(self.path("x"), os.path.join(side, "b"))
        os.rename(os.path.join(side, "a"), self.path("x"))
        self.refused_touch(s)
        s.close()
        st = os.stat(self.path("x"))
        self.assertEqual(st.st_mtime, self.OLD)
        if POSIX:
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)
        self.assertEqual(read_tree(self.root), {"x": b"AAAA"})

    # the deletes a commit got through (DESIGN §9.2: state_after drops them)

    def test_deletes_done(self):
        write_tree(self.root, {"t/a/1": b"1", "full/x": b"x", "f.txt": b"f", "e/": None,
                               "d/d2/g": b"g"})
        entries = [delete("t", tree=True), delete("missing"), delete("full"), delete("f.txt"),
                   delete("e"), delete("d/d2/g"), delete("d/d2"), put_dir("n")]
        checked, done = self.run_plan(entries)
        # in the order done, deepest first: removed, already gone, kept, and a tree once
        self.assertEqual(done.deletes_done, ["d/d2/g", "d/d2", "e", "f.txt", "full", "missing",
                                             "t"])
        self.assertEqual(done.notes, ["kept full: it isn't empty"])
        self.assertEqual(done.written, ["n"])

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_deletes_done_before_a_failure(self):
        if os.geteuid() == 0:
            self.skipTest("root can delete in a read-only directory")
        write_tree(self.root, {"d1/d2/a.txt": b"a", "ro/x.txt": b"x", "z.txt": b"z"})
        os.chmod(self.path("ro"), 0o555)
        self.addCleanup(os.chmod, self.path("ro"), 0o755)
        s = self.stager()
        s.check(Plan([delete("d1"), delete("d1/d2"), delete("d1/d2/a.txt"), delete("ro/x.txt"),
                      delete("z.txt")]))
        with self.assertRaises(VCharonError) as cm:
            s.commit()
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual(s.done.deletes_done, ["d1/d2/a.txt", "d1/d2"])
        self.assertEqual(s.done.written, [])

    @unittest.skipUnless(POSIX, "needs hard links and POSIX modes")
    def test_have_needs_one_link(self):
        # a hard link may lead outside the root: the file is replaced, never touched
        data = b"#!/bin/sh\necho outside\n"
        outside = os.path.join(self.outside, "outside.sh")
        write_tree(self.outside, {"outside.sh": data})
        os.chmod(outside, 0o4644)
        os.utime(outside, (self.OLD, self.OLD))
        os.link(outside, self.path("x.sh"))
        opened = self.count_reads()
        checked, done = self.run_plan([hashed("x.sh", data, executable=True)], {"x.sh": data})
        self.assertEqual((checked.have, opened), ([], []))
        self.assertEqual(done.written, ["x.sh"])
        self.assertNotEqual(os.stat(self.path("x.sh")).st_ino, os.stat(outside).st_ino)
        st = os.stat(outside)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_mtime, st.st_nlink),
                         (0o4644, self.OLD, 1))
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assert_mtime("x.sh")

    def test_have_spelling(self):
        # DESIGN §10.2 and §10.5: a have file still takes the plan's spelling
        probe = os.path.join(self.tmp, "Probe")
        open(probe, "wb").close()
        if not os.path.exists(os.path.join(self.tmp, "pROBE")):
            self.skipTest("this file system tells case apart")
        write_tree(self.root, {"README.md": b"same"})
        checked, done = self.run_plan([hashed("readme.md", b"same")])
        self.assertEqual(checked.have, [0])
        self.assertEqual(os.listdir(self.root), ["readme.md"])
        self.assert_mtime("readme.md")

    # 17
    @unittest.skipUnless(os.path.isdir("/dev/fd"), "needs /dev/fd")
    def test_no_fd_leak(self):
        write_tree(self.root, {"t/a/b/c": b"c", "p/q/r": b"r", "d/": None})
        before = len(os.listdir("/dev/fd"))
        entries, data = fputs({"n/m/o.txt": b"o", "p/q/s": b"s"})
        self.run_plan(entries + [delete("t", tree=True), delete("p/q/r"), delete("d")], data)
        self.assertEqual(len(os.listdir("/dev/fd")), before)
        s = self.stager()
        s.check(Plan(entries))
        for i, e in enumerate(entries):
            s.stage(i, io.BytesIO(data[e.path]))
        shutil.rmtree(self.path("n"))
        write_tree(self.root, {"n": b"a file"})
        with self.assertRaises(VCharonError):
            s.commit()
        s.close()
        self.assertEqual(len(os.listdir("/dev/fd")), before)
        write_tree(self.root, {"e/": None})
        with self.assertRaises(VCharonError):
            self.run_plan([put_file("e", 1, MTIME)], {"e": b"e"})
        self.assertEqual(len(os.listdir("/dev/fd")), before)


@unittest.skipIf(WINDOWS, "directory fds are POSIX only")
class FdStagerTest(StagerCases, unittest.TestCase):
    impl = "fd"
    cls = fsops.FdDir


class PathStagerTest(StagerCases, unittest.TestCase):
    impl = "path"
    cls = fsops.PathDir


class JsonFormTest(unittest.TestCase):
    """sink.check's and sink.commit's results on the wire (DESIGN §7.4)."""

    def test_round_trips(self):
        c = stage.Checked(root="/r", notes=["kept a: it isn't empty"], deletes=3)
        self.assertEqual(stage.checked_to_json(c),
                         {"root": "/r", "notes": ["kept a: it isn't empty"], "deletes": 3,
                          "have": []})
        self.assertEqual(stage.checked_from_json(stage.checked_to_json(c)), c)
        c = stage.Checked(root="/r", notes=[], deletes=0, have=[1, 4])
        self.assertEqual(stage.checked_to_json(c)["have"], [1, 4])
        self.assertEqual(stage.checked_from_json(stage.checked_to_json(c)), c)
        d = stage.Done(written=["a", "a/中 文.txt"], deleted=2, notes=["n"],
                       deletes_done=["old", "t"])
        self.assertEqual(stage.done_to_json(d),
                         {"written": ["a", "a/中 文.txt"], "deleted": 2, "notes": ["n"],
                          "deletes_done": ["old", "t"]})
        self.assertEqual(stage.done_from_json(stage.done_to_json(d)), d)
        self.assertEqual(stage.Done([], 0, []).deletes_done, [])

    def test_malformed(self):
        checked = {"root": "/r", "notes": [], "deletes": 0, "have": []}
        done = {"written": [], "deleted": 0, "notes": [], "deletes_done": []}
        cases = [(stage.checked_from_json, "sink.check", bad) for bad in (
            None, [], {}, dict(checked, extra=1), {"root": "/r", "notes": []},
            {"root": "/r", "notes": [], "deletes": 0},
            dict(checked, root=1), dict(checked, notes="n"), dict(checked, notes=[1]),
            dict(checked, deletes=-1), dict(checked, deletes=True), dict(checked, deletes=1.0),
            dict(checked, deletes="1"), dict(checked, have=None), dict(checked, have="1"),
            dict(checked, have=[True]), dict(checked, have=[1, 1]), dict(checked, have=[-1]),
            dict(checked, have=[1.0]))]
        cases += [(stage.done_from_json, "sink.commit", bad) for bad in (
            None, "x", dict(done, root="/r"), {"written": [], "deleted": 0},
            dict(done, written=[None]), dict(done, written="a"), dict(done, deleted=False),
            dict(done, deleted=-2), dict(done, notes=None),
            {"written": [], "deleted": 0, "notes": []}, dict(done, deletes_done=None),
            dict(done, deletes_done="a"), dict(done, deletes_done=[1]),
            dict(done, deletes_done=[["a"]]))]
        for fn, name, bad in cases:
            with self.subTest(fn=name, bad=bad):
                with self.assertRaises(VCharonError) as cm:
                    fn(bad)
                self.assertEqual(cm.exception.code, "protocol")
                self.assertTrue(cm.exception.message.startswith(
                    "a malformed %s result: " % name), cm.exception.message)


# 18
@unittest.skipUnless(WINDOWS, "Windows only")
class WindowsStagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ferry-test-"))
        self.addCleanup(self.remove_all)
        self.root = os.path.join(self.tmp, "root")
        os.mkdir(self.root)

    def remove_all(self):
        long_tmp = pathrules.win_long_path(self.tmp)
        for dirpath, dirnames, filenames in os.walk(long_tmp):
            for name in filenames:
                os.chmod(os.path.join(dirpath, name), stat.S_IWRITE)
        shutil.rmtree(long_tmp, True)

    def run_plan(self, entries, data=None, **kw):
        return run_stager(self.root, Plan(entries), data or {}, impl="path", **kw)

    def test_read_only(self):
        write_tree(self.root, {"ro.txt": b"old", "gone.txt": b"g"})
        for name in ("ro.txt", "gone.txt"):
            os.chmod(os.path.join(self.root, name), stat.S_IREAD)
        self.run_plan([put_file("ro.txt", 3, MTIME), delete("gone.txt")], {"ro.txt": b"new"})
        self.assertEqual(read_tree(self.root), {"ro.txt": b"new"})
        attrs = os.stat(os.path.join(self.root, "ro.txt")).st_file_attributes
        self.assertTrue(attrs & stat.FILE_ATTRIBUTE_READONLY)

    def test_long_path(self):
        parts = ["d%02d-%s" % (i, "x" * 60) for i in range(5)]
        rel = "/".join(parts + ["f.txt"])
        self.assertGreater(len(os.path.join(self.root, *rel.split("/"))), 260)
        self.run_plan([put_file(rel, 1, MTIME)], {rel: b"1"})
        long_path = pathrules.win_long_path(self.root, tuple(rel.split("/")))
        with open(long_path, "rb") as f:
            self.assertEqual(f.read(), b"1")
        self.run_plan([delete(parts[0], tree=True)])
        self.assertEqual(os.listdir(self.root), [])

    def test_case_only_replace(self):
        write_tree(self.root, {"README.md": b"old"})
        self.run_plan([put_file("readme.md", 3, MTIME)], {"readme.md": b"new"})
        self.assertEqual(os.listdir(self.root), ["readme.md"])
        self.assertEqual(read_tree(self.root), {"readme.md": b"new"})

    def test_in_use(self):
        write_tree(self.root, {"held.txt": b"h"})
        with open(os.path.join(self.root, "held.txt"), "rb"):
            with mock.patch.object(fsops.time, "sleep") as sleep:
                with self.assertRaises(VCharonError) as cm:
                    self.run_plan([delete("held.txt")])
        self.assertEqual(cm.exception.code, "in_use")
        self.assertEqual(sleep.call_count, 3)


if __name__ == "__main__":
    unittest.main()
