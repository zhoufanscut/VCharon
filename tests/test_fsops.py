"""File kinds, OS errors, the directory rule, directory handles and tree walks."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from vcharon import fsops
from vcharon.proto import VCharonError

from tests.util import fd_count, patch_stats, read_tree, unblock_fifo, write_tree

POSIX = os.name != "nt"

if POSIX:
    import fcntl


def fake_st(mode=stat.S_IFDIR | 0o755, uid=None, gid=1000, dev=1, attrs=0, tag=0):
    if uid is None:
        uid = os.geteuid() if POSIX else 0
    return types.SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid, st_dev=dev,
                                 st_file_attributes=attrs, st_reparse_tag=tag)


def os_error(no, winerror=None):
    e = OSError(no, os.strerror(no))
    if winerror is not None:
        e.winerror = winerror
    return e


class KindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(fsops.kind(fake_st(stat.S_IFLNK | 0o777)), fsops.LINK)
        self.assertEqual(fsops.kind(fake_st(stat.S_IFDIR | 0o755)), fsops.DIR)
        self.assertEqual(fsops.kind(fake_st(stat.S_IFREG | 0o644)), fsops.FILE)
        self.assertEqual(fsops.kind(fake_st(stat.S_IFIFO | 0o644)), fsops.OTHER)
        self.assertEqual(fsops.kind(fake_st(stat.S_IFSOCK | 0o644)), fsops.OTHER)
        reparse = stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY
        # a junction (IO_REPARSE_TAG_MOUNT_POINT) and a directory symlink: name surrogates
        for tag in (0xA0000003, 0xA000000C):
            self.assertEqual(fsops.kind(fake_st(stat.S_IFDIR, attrs=reparse, tag=tag)),
                             fsops.LINK)
        # a OneDrive cloud placeholder (IO_REPARSE_TAG_CLOUD_6) is a real directory
        cloud = fake_st(stat.S_IFDIR, attrs=reparse, tag=0x9000001A)
        self.assertEqual(fsops.kind(cloud), fsops.DIR)
        self.assertEqual(fsops.kind(fake_st(stat.S_IFLNK, attrs=reparse, tag=0x9000001A)),
                         fsops.LINK)
        # a POSIX stat result has no st_file_attributes
        self.assertEqual(fsops.kind(types.SimpleNamespace(st_mode=stat.S_IFDIR)), fsops.DIR)


class ErrorTest(unittest.TestCase):
    def test_table(self):
        edquot = getattr(errno, "EDQUOT", errno.ENOSPC)
        cases = [
            (os_error(errno.EACCES, 32), "in_use", "p: another program has it open"),
            (os_error(errno.EACCES, 33), "in_use", "p: another program has it open"),
            (os_error(errno.EACCES, 5), "permission", "p: access denied"),
            (os_error(errno.ENOENT), "not_found", "p: no such file or directory"),
            (os_error(errno.EACCES), "permission", "p: " + os.strerror(errno.EACCES)),
            (os_error(errno.EPERM), "permission", "p: " + os.strerror(errno.EPERM)),
            (os_error(errno.EROFS), "permission", "p: " + os.strerror(errno.EROFS)),
            (os_error(errno.ENOSPC), "no_space", "p: the disk is full"),
            (os_error(edquot), "no_space", "p: the disk is full"),
            (os_error(errno.ENOTDIR), "kind_change", "p: " + os.strerror(errno.ENOTDIR)),
            (os_error(errno.EISDIR), "kind_change", "p: " + os.strerror(errno.EISDIR)),
            (os_error(errno.ELOOP), "unsafe_path", "p: a symlink is in the way"),
            (os_error(errno.ENAMETOOLONG), "unsafe_path",
             "p: the name is too long for this file system"),
            (os_error(errno.EXDEV), "unsafe_dir", "p is on another file system"),
            (os_error(errno.EIO), "io", "p: " + os.strerror(errno.EIO)),
            (os_error(errno.EMFILE), "io", "p: " + os.strerror(errno.EMFILE)),
        ]
        for e, code, message in cases:
            with self.subTest(e=e, winerror=getattr(e, "winerror", None)):
                got = fsops.error(e, "p")
                self.assertIsInstance(got, VCharonError)
                self.assertEqual((got.code, got.message), (code, message))
                self.assertTrue(got.hint)
        self.assertEqual(fsops.error(os_error(errno.EACCES, 5), "p").hint,
                         "another program may have it open")
        # its own text, which a mailbox job doesn't swap (a root path can be too long too)
        self.assertEqual(fsops.error(OSError(errno.ENAMETOOLONG, "x"), "p").hint,
                         fsops.TOO_LONG_HINT)
        self.assertEqual(fsops.error(os_error(errno.EPERM), "/r").hint,
                         "check the owner and permissions of /r")
        self.assertEqual(fsops.error(os_error(errno.EIO), "p").exit_code, 1)


class RetryTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fsops.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def flaky(self, failures, error):
        calls = []

        def fn(a, b):
            calls.append((a, b))
            if len(calls) <= failures:
                raise error
            return "done"
        return fn, calls

    def test_succeeds_on_third_try(self):
        fn, calls = self.flaky(2, os_error(errno.EACCES, 32))
        self.assertEqual(fsops.retry_in_use(fn, 1, 2), "done")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(0.2)] * 2)

    def test_gives_up_after_four_tries(self):
        fn, calls = self.flaky(10, os_error(errno.EACCES, 32))
        with self.assertRaises(OSError):
            fsops.retry_in_use(fn, 1, 2)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_touch_opens_with_retries_on_windows(self):
        # a virus scanner may hold the file for a moment, as for moves and deletes
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "f")
        open(path, "wb").close()
        real = os.open
        calls = []

        def flaky(p, flags, *args, **kw):
            calls.append(p)
            if len(calls) <= 2:
                raise os_error(errno.EACCES, 32)
            return real(p, flags, *args, **kw)

        with mock.patch.object(fsops, "WINDOWS", True), mock.patch.object(os, "open", flaky):
            fd = fsops._open_to_touch(path, ("f",))
        os.close(fd)
        self.assertEqual(calls, [path] * 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_other_errors_at_once(self):
        for error in (os_error(errno.EACCES, 5), os_error(errno.EACCES)):
            fn, calls = self.flaky(10, error)
            with self.assertRaises(OSError):
                fsops.retry_in_use(fn, 1, 2)
            self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()


@unittest.skipUnless(POSIX, "the owner and mode rule is POSIX only")
class DirProblemTest(unittest.TestCase):
    def setUp(self):
        fsops._private_groups.clear()
        self.addCleanup(fsops._private_groups.clear)
        self.me = os.geteuid()

    def groups(self, name, members):
        """Patched lookups: I'm "me" with primary group 4242, and 4242 is `name`."""
        me = types.SimpleNamespace(pw_name="me", pw_gid=4242)
        group = types.SimpleNamespace(gr_name=name, gr_mem=members)
        for patcher in (mock.patch.object(fsops.pwd, "getpwuid", return_value=me),
                        mock.patch.object(fsops.grp, "getgrgid", return_value=group)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plain(self):
        self.assertIsNone(fsops.dir_problem(fake_st(), 1))
        self.assertIsNone(fsops.dir_problem(fake_st(stat.S_IFDIR | 0o700), 1))

    def test_device(self):
        self.assertEqual(fsops.dir_problem(fake_st(dev=2), 1), "is on another file system")
        self.assertEqual(fsops.dir_problem(fake_st(dev=2), 1, owner_rule=False),
                         "is on another file system")

    def test_owner(self):
        other = fake_st(uid=self.me + 1)
        self.assertEqual(fsops.dir_problem(other, 1), "is owned by another user")
        self.assertIsNone(fsops.dir_problem(other, 1, owner_rule=False))
        if self.me:
            # root's, without the sticky bit
            self.assertEqual(fsops.dir_problem(fake_st(uid=0), 1), "is owned by another user")

    def test_other_write(self):
        st = fake_st(stat.S_IFDIR | 0o757)
        self.assertEqual(fsops.dir_problem(st, 1), "is writable by others")
        self.assertIsNone(fsops.dir_problem(st, 1, owner_rule=False))

    def test_group_write_private(self):
        self.groups("me", [])
        self.assertIsNone(fsops.dir_problem(fake_st(stat.S_IFDIR | 0o775, gid=4242), 1))
        fsops._private_groups.clear()
        self.groups("me", ["me"])
        self.assertIsNone(fsops.dir_problem(fake_st(stat.S_IFDIR | 0o775, gid=4242), 1))

    def test_group_write_shared(self):
        cases = [("me", ["me", "you"], 4242), ("staff", [], 4242), ("me", [], 4343)]
        for name, members, gid in cases:
            with self.subTest(name=name, members=members, gid=gid):
                fsops._private_groups.clear()
                self.groups(name, members)
                self.assertEqual(fsops.dir_problem(fake_st(stat.S_IFDIR | 0o775, gid=gid), 1),
                                 "is writable by its group")

    def test_group_lookup_fails(self):
        with mock.patch.object(fsops.grp, "getgrgid", side_effect=KeyError(4242)):
            self.assertFalse(fsops.private_group(4242))
        # cached per gid
        with mock.patch.object(fsops.grp, "getgrgid") as getgrgid:
            self.assertFalse(fsops.private_group(4242))
            getgrgid.assert_not_called()

    def test_sticky(self):
        for uid in (0, self.me):
            st = fake_st(stat.S_IFDIR | 0o1777, uid=uid)
            self.assertIsNone(fsops.dir_problem(st, 1))
        if self.me:
            other = fake_st(stat.S_IFDIR | 0o1777, uid=self.me + 1)
            self.assertEqual(fsops.dir_problem(other, 1), "is owned by another user")
        self.assertEqual(fsops.dir_problem(fake_st(stat.S_IFDIR | 0o1777, dev=2), 1),
                         "is on another file system")

    def test_no_device_rule(self):
        # root_dev None: a source, which may cross mount points
        self.assertIsNone(fsops.dir_problem(fake_st(dev=2), None))
        self.assertIsNone(fsops.dir_problem(fake_st(dev=2), None, owner_rule=False))
        # the owner rule still holds unless it's off too
        other = fake_st(uid=self.me + 1, dev=2)
        self.assertEqual(fsops.dir_problem(other, None), "is owned by another user")
        self.assertIsNone(fsops.dir_problem(other, None, owner_rule=False))


@unittest.skipIf(os.name == "nt", "Windows uses os.path.realpath")
class ResolveRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="vcharon-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write_tree(self.tmp, {"real/sub/": None, "file": b"f"})

    def link(self, name, target):
        path = os.path.join(self.tmp, name)
        os.symlink(target, path)
        return path

    def test_same_as_realpath(self):
        self.link("abs", os.path.join(self.tmp, "real"))
        self.link("rel", "real/sub")
        self.link("chain", "abs")
        self.link("up", "real/sub/..")
        self.link("dangling", "nowhere/deeper")
        cases = ["real", "abs", "abs/sub", "rel", "chain/sub", "up", "up/sub/../sub",
                 "missing", "missing/a/b", "abs/missing/c", "dangling", "dangling/x",
                 "file/x", "./real/./sub", "real/../abs"]
        for rel in cases:
            path = os.path.join(self.tmp, rel)
            with self.subTest(rel=rel):
                self.assertEqual(fsops.resolve_root(path), os.path.realpath(path))
        self.assertEqual(fsops.resolve_root("/"), "/")

    def test_root_owned_links(self):
        # macOS: /var and /tmp are root's links to /private/...
        for path in (tempfile.gettempdir(), "/tmp", "/var", "/etc"):
            with self.subTest(path=path):
                self.assertEqual(fsops.resolve_root(path), os.path.realpath(path))

    def test_foreign_link(self):
        link = self.link("drop", os.path.join(self.tmp, "real"))
        patch_stats(self, link, st_uid=os.geteuid() + 1)
        with self.assertRaises(VCharonError) as cm:
            fsops.resolve_root(os.path.join(link, "sub"))
        self.assertEqual(cm.exception.code, "unsafe_dir")
        self.assertEqual(cm.exception.hint,
                         "use the path it points to as the root, if you trust it")

    def test_root_owned_link(self):
        link = self.link("rootlink", os.path.join(self.tmp, "real"))
        patch_stats(self, link, st_uid=0)
        self.assertEqual(fsops.resolve_root(link), os.path.join(self.tmp, "real"))

    def test_loop(self):
        self.link("a", "b")
        self.link("b", "a")
        with self.assertRaises(VCharonError) as cm:
            fsops.resolve_root(os.path.join(self.tmp, "a"))
        self.assertEqual(cm.exception.code, "unsafe_path")


class HandleCases:
    """Runs for each handle type."""

    cls = None

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="vcharon-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        self.outside = os.path.join(self.tmp, "outside")
        write_tree(self.tmp, {"root/": None, "outside/x/y.txt": b"y", "outside/z.txt": b"z"})

    def open_root(self):
        d = self.cls.open_root(self.root)
        self.addCleanup(d.close)
        return d

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_enter_symlink(self):
        os.symlink(self.outside, os.path.join(self.root, "l"))
        with self.assertRaises(VCharonError) as cm:
            self.open_root().enter("l")
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(cm.exception.message,
                         "l is a symlink or junction; vcharon never goes through one")

    def test_enter_file(self):
        write_tree(self.root, {"d/f": b"f"})
        with self.open_root().enter("d") as d:
            self.assertEqual(d.rel, ("d",))
            with self.assertRaises(VCharonError) as cm:
                d.enter("f")
        self.assertEqual(cm.exception.code, "kind_change")
        self.assertEqual(cm.exception.message, "d/f isn't a directory")

    def test_enter_missing(self):
        with self.assertRaises(FileNotFoundError):
            self.open_root().enter("nope")

    @unittest.skipUnless(POSIX, "the owner and mode rule is POSIX only")
    def test_enter_checks_mode(self):
        write_tree(self.root, {"d/": None})
        os.chmod(os.path.join(self.root, "d"), 0o777)
        root = self.open_root()
        with self.assertRaises(VCharonError) as cm:
            root.enter("d")
        self.assertEqual(cm.exception.code, "unsafe_dir")
        self.assertEqual(cm.exception.message, "d is writable by others")
        self.assertIn("chmod go-w %s" % os.path.join(self.root, "d"), cm.exception.hint)
        root.enter("d", owner_rule=False).close()

    def test_changes(self):
        root = self.open_root()
        root.mkdir("d")
        self.assertIsNone(root.lstat("nope"))
        self.assertEqual(fsops.kind(root.lstat("d")), fsops.DIR)
        with self.assertRaises(FileExistsError):
            root.mkdir("d")
        with root.enter("d") as d:
            fd = d.create_file("f")
            os.write(fd, b"data")
            os.close(fd)
            with self.assertRaises(FileExistsError):
                d.create_file("f")
            root.move_in(d, "f", "g")
            self.assertEqual(sorted(d.listdir()), [])
        self.assertEqual(sorted(root.listdir()), ["d", "g"])
        root.rename("g", "h")
        root.unlink("h")
        root.rmdir("d")
        self.assertEqual(root.listdir(), [])

    def test_remove_tree_keeps_link_targets(self):
        write_tree(self.root, {"t/a.txt": b"a", "t/sub/b.txt": b"b", "t/sub/deeper/": None})
        if POSIX:
            os.symlink(self.outside, os.path.join(self.root, "t", "sub", "link"))
            os.symlink(os.path.join(self.outside, "z.txt"), os.path.join(self.root, "t", "fl"))
        # t, a.txt, sub, b.txt, deeper, and two links
        want = 7 if POSIX else 5
        root = self.open_root()
        ticks = []
        self.assertEqual(fsops.count_tree(root, "t", lambda: ticks.append(1)), want)
        self.assertEqual(len(ticks), want)
        self.assertEqual(fsops.remove_tree(root, "t", lambda: None), want)
        self.assertEqual(root.listdir(), [])
        self.assertEqual(read_tree(self.outside), {"x/": None, "x/y.txt": b"y", "z.txt": b"z"})

    def test_walks_close_what_they_open(self):
        write_tree(self.root, {"t/a/b/c/d.txt": b"d"})
        opened = []
        real_enter = self.cls.enter

        def enter(d, name, owner_rule=True):
            sub = real_enter(d, name, owner_rule)
            opened.append(sub)
            return sub

        with mock.patch.object(self.cls, "enter", enter):
            fsops.remove_tree(self.open_root(), "t", lambda: None)
        self.assertEqual(len(opened), 4)
        for d in opened:
            if isinstance(d, fsops.FdDir):
                self.assertIsNone(d.fd)

    # the source side (M3)

    def test_same_device_false(self):
        write_tree(self.root, {"d/": None})
        path = os.path.join(self.root, "d")
        patch_stats(self, path, st_dev=os.lstat(path).st_dev + 1)
        with self.assertRaises(VCharonError) as cm:
            self.open_root().enter("d", owner_rule=False)
        self.assertEqual(cm.exception.message, "d is on another file system")
        source = self.cls.open_root(self.root, same_device=False)
        self.addCleanup(source.close)
        self.assertIsNone(source.root_dev)
        with source.enter("d", owner_rule=False) as d:
            self.assertIsNone(d.root_dev)

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_search_only_root(self):
        # a drop box: x without r (W2 of the M3 review)
        if os.geteuid() == 0:
            self.skipTest("root can read every directory")
        if self.cls is fsops.FdDir and not fsops._SEARCH:
            self.skipTest("no O_PATH or O_SEARCH here")
        write_tree(self.root, {"f": b"data", "d/g": b"deeper"})
        os.chmod(self.root, 0o311)
        self.addCleanup(os.chmod, self.root, 0o755)
        root = self.cls.open_root(self.root, same_device=False, list=False)
        self.addCleanup(root.close)
        self.assertEqual(fsops.kind(root.lstat("f")), fsops.FILE)
        with root.open_read("f") as f:
            self.assertEqual(f.read(), b"data")
        with root.enter("d", owner_rule=False) as d, d.open_read("g") as f:
            self.assertEqual(f.read(), b"deeper")
        # searched, never listed
        with self.assertRaises(OSError):
            root.scan()
        if self.cls is fsops.FdDir:
            # opened to list, it needs r
            with self.assertRaises(PermissionError):
                self.cls.open_root(self.root, same_device=False)

    def test_scan(self):
        write_tree(self.root, {"f": b"data", "d/x": b"x"})
        if POSIX:
            os.symlink(self.outside, os.path.join(self.root, "l"))
            os.mkfifo(os.path.join(self.root, "p"))
        root = self.open_root()
        got = {name: (fsops.kind(st), st.st_size if fsops.kind(st) == fsops.FILE else None)
               for name, st in root.scan()}
        want = {"f": (fsops.FILE, 4), "d": (fsops.DIR, None)}
        if POSIX:
            want.update(l=(fsops.LINK, None), p=(fsops.OTHER, None))
        self.assertEqual(got, want)
        # listed again from the start
        self.assertEqual(sorted(n for n, st in root.scan()), sorted(want))
        with root.enter("d") as d:
            self.assertEqual([n for n, st in d.scan()], ["x"])

    def test_scan_leaves_out_what_vanished_before_its_stat(self):
        write_tree(self.root, {"a": b"a", "gone": b"g", "b": b"b"})
        real_scandir = os.scandir

        class Entry:
            def __init__(self, entry):
                self.name = entry.name
                self._entry = entry

            def stat(self, follow_symlinks=True):
                if self.name == "gone":
                    raise FileNotFoundError(errno.ENOENT, "gone", self.name)
                return self._entry.stat(follow_symlinks=follow_symlinks)

        class Entries:
            def __init__(self, it):
                self._it = it

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._it.close()

            def __iter__(self):
                return (Entry(e) for e in self._it)

        with mock.patch.object(os, "scandir", lambda arg: Entries(real_scandir(arg))):
            self.assertEqual(sorted(n for n, st in self.open_root().scan()), ["a", "b"])

    def test_open_read(self):
        write_tree(self.root, {"f": b"data", "d/g": b"deeper"})
        before = fd_count() if POSIX else None
        root = self.cls.open_root(self.root, same_device=False)
        with root.open_read("f") as f:
            self.assertEqual(f.read(), b"data")
            if POSIX:
                # the O_NONBLOCK of the open is cleared: reads block
                self.assertFalse(fcntl.fcntl(f.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK)
        with root.enter("d", owner_rule=False) as d:
            with d.open_read("g") as f:
                self.assertEqual(f.read(), b"deeper")
            with self.assertRaises(FileNotFoundError):
                d.open_read("missing")
        with self.assertRaises(VCharonError) as cm:
            root.open_read("d")
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ("vanished", "d isn't a regular file any more"))
        root.close()
        if POSIX:
            self.assertEqual(fd_count(), before)

    @unittest.skipUnless(POSIX, "needs symlinks and FIFOs")
    def test_open_read_link_and_fifo(self):
        write_tree(self.root, {"d/": None})
        os.symlink(os.path.join(self.outside, "z.txt"), os.path.join(self.root, "l"))
        os.mkfifo(os.path.join(self.root, "d", "p"))
        unblock_fifo(self, os.path.join(self.root, "d", "p"))
        before = fd_count()
        root = self.cls.open_root(self.root, same_device=False)
        if self.cls is fsops.FdDir:
            with self.assertRaises(OSError) as cm:
                root.open_read("l")
            self.assertEqual(cm.exception.errno, errno.ELOOP)
        else:
            with self.assertRaises(VCharonError) as cm:
                root.open_read("l")
            self.assertEqual((cm.exception.code, cm.exception.message),
                             ("vanished", "l isn't a regular file any more"))
        with root.enter("d", owner_rule=False) as d:
            started = time.monotonic()
            with self.assertRaises(VCharonError) as cm:
                d.open_read("p")
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ("vanished", "d/p isn't a regular file any more"))
        root.close()
        self.assertEqual(fd_count(), before)


@unittest.skipUnless(POSIX, "directory fds are POSIX only")
class FdDirTest(HandleCases, unittest.TestCase):
    cls = fsops.FdDir


class PathDirTest(HandleCases, unittest.TestCase):
    cls = fsops.PathDir

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_scan_rechecks(self):
        write_tree(self.root, {"a/inside.txt": b"i"})
        with self.open_root().enter("a") as a:
            self.assertEqual([n for n, st in a.scan()], ["inside.txt"])
            os.rename(os.path.join(self.root, "a"), os.path.join(self.root, "a2"))
            os.symlink(self.outside, os.path.join(self.root, "a"))
            # never lists the outside tree
            with self.assertRaises(VCharonError) as cm:
                a.scan()
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(cm.exception.message,
                         "a is a symlink or junction; vcharon never goes through one")

    @unittest.skipUnless(POSIX, "needs symlinks")
    def test_recheck(self):
        write_tree(self.root, {"a/b/": None})
        with self.open_root().enter("a") as a, a.enter("b") as b:
            os.rename(os.path.join(self.root, "a"), os.path.join(self.root, "a2"))
            os.symlink(self.outside, os.path.join(self.root, "a"))
            with self.assertRaises(VCharonError) as cm:
                b.mkdir("new")
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(read_tree(self.outside), {"x/": None, "x/y.txt": b"y", "z.txt": b"z"})


def _gone(pid, seconds=5):
    """True once no process has this pid (POSIX)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class RunTest(unittest.TestCase):
    """fsops.run and run_terminal (decisions 1 and 2 of the M5 plan)."""

    def py(self, code):
        return [sys.executable, "-c", code]

    def test_captures_bytes(self):
        ran = fsops.run(self.py("import sys; sys.stdout.buffer.write(b'out\\xff');"
                                "sys.stderr.buffer.write(b'err\\n'); sys.exit(3)"), timeout=30)
        self.assertEqual(ran, fsops.Ran(3, b"out\xff", b"err\n"))

    def test_env(self):
        # decision 8 of the M6 plan: None inherits; a dict is the whole environment
        code = "import os; print(os.environ.get('VCHARON_TEST_RUN_ENV'))"
        with mock.patch.dict(os.environ, {"VCHARON_TEST_RUN_ENV": "inherited"}):
            self.assertEqual(fsops.run(self.py(code), timeout=30).out.strip(), b"inherited")
            env = dict(os.environ, VCHARON_TEST_RUN_ENV="given")
            self.assertEqual(fsops.run(self.py(code), timeout=30, env=env).out.strip(),
                             b"given")
            env.pop("VCHARON_TEST_RUN_ENV")
            self.assertEqual(fsops.run(self.py(code), timeout=30, env=env).out.strip(), b"None")

    def test_stdin_is_at_end_of_file(self):
        ran = fsops.run(self.py("import sys; print(repr(sys.stdin.read()))"), timeout=30)
        self.assertEqual((ran.rc, ran.out.strip()), (0, b"''"))

    def test_timeout_kills(self):
        started = time.monotonic()
        ran = fsops.run(self.py("import sys, time; print('early', flush=True); "
                                "time.sleep(60)"), timeout=1)
        self.assertLess(time.monotonic() - started, 1 + 6)
        self.assertIsNone(ran.rc)
        self.assertEqual(ran.out.strip(), b"early")

    @unittest.skipUnless(POSIX, "process groups")
    def test_timeout_kills_the_group(self):
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        pid_file = os.path.join(tmp, "pid")
        # a grandchild that holds the pipes, as ssh's ProxyCommand would
        code = ("import subprocess, sys, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "open(%r, 'w').write(str(p.pid))\n"
                "time.sleep(60)\n" % pid_file)
        started = time.monotonic()
        ran = fsops.run(self.py(code), timeout=2, new_session=True)
        self.assertLess(time.monotonic() - started, 2 + 6)
        self.assertIsNone(ran.rc)
        with open(pid_file) as f:
            grandchild = int(f.read())
        self.assertTrue(_gone(grandchild))

    def test_missing_program(self):
        with self.assertRaises(OSError):
            fsops.run([os.path.join(tempfile.gettempdir(), "vcharon-no-such-program")], timeout=5)

    @unittest.skipUnless(POSIX, "dup2 on fd 0")
    def test_stdin_isnt_inherited(self):
        # fd 0 is a pipe nobody writes to or closes: a child that inherits it would block
        r, w = os.pipe()
        saved = os.dup(0)
        os.dup2(r, 0)
        os.close(r)

        def restore():
            os.dup2(saved, 0)
            os.close(saved)
            os.close(w)

        self.addCleanup(restore)
        ran = fsops.run(self.py("import sys; print(len(sys.stdin.buffer.read()))"), timeout=5)
        self.assertEqual((ran.rc, ran.out.strip()), (0, b"0"))

    def test_ctrl_c_kills_and_reraises(self):
        procs = []
        real = subprocess.Popen

        class Popen(real):
            def __init__(self, *args, **kw):
                real.__init__(self, *args, **kw)
                procs.append(self)

            def communicate(self, *args, **kw):
                raise KeyboardInterrupt

        def cleanup():
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

        self.addCleanup(cleanup)
        with (mock.patch.object(subprocess, "Popen", Popen),
              self.assertRaises(KeyboardInterrupt)):
            fsops.run(self.py("import time; time.sleep(60)"), timeout=30)
        [proc] = procs
        # killed, not left running
        self.assertIsNotNone(proc.wait(5))

    @unittest.skipUnless(POSIX, "sh")
    def test_exited_before_the_timeout(self):
        # review W6: the program exited; only a child it left behind held the pipes
        ran = fsops.run(["/bin/sh", "-c", "sleep 8 & echo done"], timeout=1, new_session=True)
        self.assertEqual((ran.rc, ran.out.strip()), (0, b"done"))

    def stuck_proc(self):
        """A Popen stand-in whose pipes stay held after the kill."""
        proc = mock.Mock()
        proc.pid = 12345
        proc.poll.return_value = None
        proc.communicate.side_effect = subprocess.TimeoutExpired(["x"], 1, output=b"o",
                                                                 stderr=b"e")
        proc.wait.return_value = -9
        return proc

    def test_stuck_pipes(self):
        # review W1: on Windows a reader thread of communicate still holds the pipes, and
        # close() would block; POSIX closes them
        for windows in (True, False):
            with self.subTest(windows=windows):
                proc = self.stuck_proc()
                with mock.patch.object(fsops, "WINDOWS", windows), \
                        mock.patch.object(subprocess, "Popen", return_value=proc):
                    ran = fsops.run(["x"], timeout=1)
                self.assertEqual(ran, fsops.Ran(None, b"o", b"e"))
                proc.kill.assert_called()
                for stream in (proc.stdout, proc.stderr):
                    self.assertEqual(stream.close.called, not windows)

    def test_run_terminal(self):
        self.assertEqual(fsops.run_terminal(self.py("import sys; sys.exit(7)")), 7)
        self.assertEqual(fsops.run_terminal(self.py("pass")), 0)


if __name__ == "__main__":
    unittest.main()
