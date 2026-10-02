"""The dir sink (DESIGN §9.2), through the Stager on a real file system."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest

POSIX = os.name != "nt"

from vcharon import plugin, stage
from vcharon.plan import Plan, delete, put_dir, put_file
from vcharon.proto import VCharonError

from tests.util import patch_stats, read_tree, write_tree

MTIME = 1790000000.25


class SinkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ferry-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        os.mkdir(self.root)

    def sink(self, name, end="local", **raw):
        s = plugin.make(end, name, "sink", raw, plugin.Ctx(end, home=self.tmp))
        self.addCleanup(s.close)
        return s

    def assert_mtime(self, path):
        # to the microsecond: the Stager's float arithmetic is off by up to 256 ns here
        self.assertLess(abs(os.stat(path).st_mtime_ns - MTIME * 1e9), 1000)

    def refused_at_check(self, s, plan, code):
        with self.assertRaises(VCharonError) as cm:
            s.check(plan)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        return cm.exception


class DirSinkTest(SinkCase):
    def test_check_stage_commit(self):
        write_tree(self.root, {"old.txt": b"o"})
        s = self.sink("dir", path=self.root)
        p = Plan([put_dir("d"), put_file("d/x.txt", 1, MTIME), delete("old.txt")])
        self.assertEqual(s.check(p), stage.Checked(root=self.root, notes=[], deletes=1))
        s.stage(1, io.BytesIO(b"x"))
        done = s.commit()
        self.assertEqual(done, stage.Done(written=["d", "d/x.txt"], deleted=1, notes=[],
                                          deletes_done=["old.txt"]))
        self.assertEqual(s.done, done)
        self.assertEqual(read_tree(self.root), {"d/": None, "d/x.txt": b"x"})
        self.assert_mtime(os.path.join(self.root, "d", "x.txt"))

    def test_abort_leaves_the_root(self):
        s = self.sink("dir", path=self.root)
        s.check(Plan([put_file("x", 1, MTIME)]))
        s.stage(0, io.BytesIO(b"x"))
        self.assertEqual(len(os.listdir(self.root)), 1)
        s.abort()
        s.close()
        self.assertEqual(read_tree(self.root), {})

    def test_create(self):
        missing = os.path.join(self.tmp, "new", "root")
        p = Plan([put_file("x", 1, MTIME)])
        e = self.refused_at_check(self.sink("dir", path=missing), p, "not_found")
        self.assertEqual(e.hint, "create it first (in a job: to.create = yes)")
        s = self.sink("dir", path=missing, create="yes")
        self.assertEqual(s.check(p).root, missing)
        s.stage(0, io.BytesIO(b"x"))
        s.commit()
        self.assertEqual(read_tree(missing), {"x": b"x"})

    def test_max_deletes(self):
        write_tree(self.root, {"t/1": b"", "t/2": b""})
        p = Plan([delete("t", tree=True)])
        e = self.refused_at_check(self.sink("dir", path=self.root, max_deletes="2"), p,
                                  "too_many_deletes")
        self.assertIn("max_deletes (2)", e.message)
        self.assertEqual(self.sink("dir", path=self.root, max_deletes="0").check(p).deletes, 3)
        self.assertEqual(self.sink("dir", path=self.root).options["max_deletes"], 500)

    def test_relative_path(self):
        p = Plan([put_file("x", 1, MTIME)])
        # on the remote end: under ctx.home, never the current directory
        s = self.sink("dir", end="remote", path="root")
        self.assertEqual(s.check(p).root, self.root)
        s.stage(0, io.BytesIO(b"x"))
        s.commit()
        self.assertEqual(read_tree(self.root), {"x": b"x"})
        e = self.refused_at_check(self.sink("dir", end="local", path="root"), p, "bad_options")
        self.assertEqual(e.message, "to.path: must be an absolute path or start with ~")
        s = self.sink("dir", end="local", path="~/root")
        self.assertEqual(s.check(p).root, self.root)

    def test_check_runs_once(self):
        s = self.sink("dir", path=self.root)
        s.check(Plan([]))
        self.refused_at_check(s, Plan([]), "internal")


STAMP = "20260930-123456"


def stage_from(sink, source):
    """Stages the source's file 0 into the sink, as the engine does."""
    reader = source.open(0)
    try:
        sink.stage(0, reader)
    finally:
        reader.close()


class SinkDoctorTest(SinkCase):
    """The dir sink's doctor (decision 21 of the M5 plan): it only reads."""

    def doctor(self, name, **raw):
        return plugin.doctor("local", name, "sink", raw, plugin.Ctx("local", home=self.tmp))

    def assert_untouched(self):
        # no root or parent made, and no stage dir anywhere
        self.assertEqual(read_tree(self.tmp), {"root/": None})

    def test_dir_writable(self):
        self.assertEqual(self.doctor("dir", path=self.root),
                         [("ok", "to.path %s: a directory you can write" % self.root, None)])
        self.assert_untouched()

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_dir_read_only(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        os.chmod(self.root, 0o555)
        self.addCleanup(os.chmod, self.root, 0o755)
        self.assertEqual(self.doctor("dir", path=self.root),
                         [("FAIL", "you can't write in %s" % self.root, "fix its permissions")])

    def test_dir_missing(self):
        missing = os.path.join(self.root, "a", "b")
        [(level, message, hint)] = self.doctor("dir", path=missing)
        self.assertEqual((level, message), ("FAIL", "the root %s doesn't exist" % missing))
        self.assertEqual(self.doctor("dir", path=missing, create="yes"),
                         [("ok", "to.path %s doesn't exist yet; the first run creates it"
                           % missing, None)])
        self.assert_untouched()

    @unittest.skipUnless(POSIX, "the owner rule is POSIX only")
    def test_dir_owned_by_another_user(self):
        patch_stats(self, self.root, st_uid=os.geteuid() + 1)
        [(level, message, hint)] = self.doctor("dir", path=self.root)
        self.assertEqual(level, "FAIL")
        self.assertEqual(message, "the root %s is owned by another user" % self.root)
        self.assert_untouched()

    @unittest.skipUnless(POSIX, "needs POSIX modes")
    def test_missing_root_under_a_read_only_parent(self):
        # review W5: the first run couldn't create it
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        ro = os.path.join(self.root, "ro")
        os.mkdir(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        new = os.path.join(ro, "new")
        fix = "fix its permissions, or create %s yourself" % new
        self.assertEqual(self.doctor("dir", path=new, create="yes"),
                         [("FAIL", "can't create %s: you can't write in %s" % (new, ro), fix)])
        deeper = os.path.join(new, "deeper")
        self.assertEqual(self.doctor("dir", path=deeper, create="yes"),
                         [("FAIL", "can't create %s: you can't write in %s" % (deeper, ro),
                           "fix its permissions, or create %s yourself" % deeper)])

if __name__ == "__main__":
    unittest.main()
