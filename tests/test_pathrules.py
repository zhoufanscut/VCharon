"""The pure path rules, every receiver OS's set on this OS (DESIGN, "Path rules")."""

from __future__ import annotations

import unicodedata
import unittest

from vcharon import pathrules, plan
from vcharon.plan import delete, put_dir, put_file
from vcharon.proto import VCharonError

OSES = ("linux", "darwin", "windows")


def files(*paths):
    return [put_file(p, 1, 0) for p in paths]


class SplitTest(unittest.TestCase):
    def test_refused(self):
        for path in ["", "/a", "a/", "a//b", ".", "..", "a/./b", "a/../b", "a\0b", "a\udc80",
                     ".vcharon-stage-x", "x/.VCHARON-STAGE-1", None]:
            with self.subTest(path=path):
                with self.assertRaises(VCharonError) as cm:
                    pathrules.split(path)
                self.assertEqual(cm.exception.code, "unsafe_path")

    def test_normal(self):
        self.assertEqual(pathrules.split("a/b c/中.txt"), ("a", "b c", "中.txt"))
        self.assertEqual(pathrules.split(".vcharon-stag"), (".vcharon-stag",))


class PartTest(unittest.TestCase):
    def test_lengths(self):
        self.assertIsNone(pathrules.part_problem("a" * 255, "linux"))
        self.assertIsNotNone(pathrules.part_problem("a" * 256, "linux"))
        self.assertIsNone(pathrules.part_problem("中" * 85, "linux"))
        self.assertIsNotNone(pathrules.part_problem("中" * 86, "linux"))
        for osn in ("darwin", "windows"):
            with self.subTest(osn=osn):
                self.assertIsNone(pathrules.part_problem("中" * 255, osn))
                self.assertIsNotNone(pathrules.part_problem("中" * 256, osn))
                self.assertIsNotNone(pathrules.part_problem("\U0001F600" * 128, osn))
                self.assertIsNone(pathrules.part_problem("a" * 255, osn))
                self.assertIsNotNone(pathrules.part_problem("a" * 256, osn))

    def test_windows_characters(self):
        for c in '<>:"|?*\\' + "\x01\x1f":
            with self.subTest(c=c):
                self.assertIsNotNone(pathrules.part_problem("a%sb" % c, "windows"))
                self.assertIsNone(pathrules.part_problem("a%sb" % c, "linux"))
        for name in ("a.", "a ", "..."):
            self.assertIsNotNone(pathrules.part_problem(name, "windows"))
        self.assertIsNone(pathrules.part_problem("a.b c", "windows"))
        self.assertIsNone(pathrules.part_problem("a.", "darwin"))

    def test_reserved(self):
        for name in ("CON", "con", "NUL .txt", "AUX.tar.gz", "COM\u00b9", "LPT9.log", "CONIN$",
                     "conout$", "Prn"):
            with self.subTest(name=name):
                self.assertTrue(pathrules.is_reserved_windows(name))
                self.assertIsNotNone(pathrules.part_problem(name, "windows"))
                self.assertIsNone(pathrules.part_problem(name, "darwin"))
        for name in ("CONX", "COM0", "LPT", "xCON", "CON-1", "COM10"):
            with self.subTest(name=name):
                self.assertFalse(pathrules.is_reserved_windows(name))
                self.assertIsNone(pathrules.part_problem(name, "windows"))


class FoldTest(unittest.TestCase):
    def test_fold(self):
        nfc = unicodedata.normalize("NFC", "caf\u00e9")
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertEqual(pathrules.fold("\u00df", "windows"), pathrules.fold("ss", "windows"))
        self.assertEqual(pathrules.fold("A", "windows"), pathrules.fold("a", "windows"))
        self.assertEqual(pathrules.fold(nfc, "darwin"), pathrules.fold(nfd, "darwin"))
        self.assertEqual(pathrules.fold("README", "darwin"), pathrules.fold("readme", "darwin"))
        self.assertEqual(pathrules.fold(nfc.upper(), "darwin"), pathrules.fold(nfd, "darwin"))
        self.assertNotEqual(pathrules.fold("A", "linux"), pathrules.fold("a", "linux"))
        self.assertNotEqual(pathrules.fold(nfc, "linux"), pathrules.fold(nfd, "linux"))


class CheckPlanTest(unittest.TestCase):
    def check(self, entries, osn):
        return pathrules.check_plan(plan.Plan(entries), osn)

    def refused(self, entries, osn, code="collision"):
        with self.assertRaises(VCharonError) as cm:
            self.check(entries, osn)
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    def test_parts(self):
        self.assertEqual(self.check([put_dir("a"), put_file("a/b", 1, 0), delete("c")], "linux"),
                         [("a",), ("a", "b"), ("c",)])

    def test_file_below_file(self):
        for osn in OSES:
            for entries in (files("a", "a/b"), files("a/b", "a")):
                with self.subTest(osn=osn, entries=entries):
                    e = self.refused(entries, osn)
                    self.assertEqual(e.message,
                                     "a is put as a file, but a/b needs it to be a directory")

    def test_a_folder_spelled_twice(self):
        # a file or a directory, then a path below another spelling of it: the spellings are
        # compared before the kinds
        for entries, names in (([put_dir("Docs"), put_file("docs/a.txt", 1, 0)], "Docs and docs"),
                               ([put_file("A", 1, 0), put_file("a/b.txt", 1, 0)], "A and a")):
            with self.subTest(names=names):
                for osn, shown in (("windows", "Windows"), ("darwin", "macOS")):
                    e = self.refused(entries, osn)
                    self.assertEqual(e.message, "%s are the same path on %s" % (names, shown))
                self.check(entries, "linux")
        self.check([put_dir("docs"), put_file("docs/a.txt", 1, 0)], "darwin")

    def test_nfc_and_nfd(self):
        nfc = unicodedata.normalize("NFC", "caf\u00e9")
        nfd = unicodedata.normalize("NFD", nfc)
        self.refused(files(nfc, nfd), "darwin")
        self.check(files(nfc, nfd), "linux")

    def test_twice(self):
        for osn in OSES:
            self.assertIn("put twice", self.refused(files("x", "x"), osn).message)
            self.assertIn("put twice", self.refused([put_dir("d"), put_dir("d")], osn).message)
            self.assertIn("deleted twice",
                          self.refused([delete("x"), delete("x")], osn).message)
            # a replacement
            self.check([delete("x"), put_file("x", 1, 0)], osn)
            self.check([put_dir("x"), delete("x", tree=True)], osn)

    def test_deletes_folded(self):
        self.refused([delete("X"), delete("x")], "windows")
        self.check([delete("X"), delete("x")], "linux")

    def test_below_tree_delete(self):
        for osn in OSES:
            e = self.refused([delete("d/e/f"), delete("d", tree=True)], osn)
            self.assertEqual(e.message, "d/e/f is below d, which the plan deletes as a tree")
            self.check([delete("d/e"), delete("d")], osn)
            self.check([delete("d", tree=True), put_file("d/e", 1, 0)], osn)
        self.refused([delete("D", tree=True), delete("d/e")], "darwin")
        self.check([delete("D", tree=True), delete("d/e")], "linux")

    def test_many_bad_paths(self):
        e = self.refused(files("ok", "a/", "/b", "c//d", "..") + files(*["e%d:" % i
                                                                         for i in range(200)]),
                         "windows", "unsafe_path")
        self.assertEqual(e.message, "a/: has an empty, \".\" or \"..\" part "
                                    "(and 203 more; see the log)")
        self.assertEqual(e.hint, "rename or exclude these paths at the source")
        lines = e.detail.split("\n")
        self.assertEqual(len(lines), 100)
        self.assertEqual(lines[1], "/b: has an empty, \".\" or \"..\" part")
        self.assertTrue(lines[4].startswith("e0:: a name holds"))

    def test_one_bad_path(self):
        e = self.refused(files("a" * 256), "linux", "unsafe_path")
        self.assertNotIn("more", e.message)
        self.assertEqual(e.detail, e.message)

    def test_many_collisions(self):
        e = self.refused(files("a", "a", "b", "b"), "linux")
        self.assertEqual(e.message, "a is put twice (and 1 more; see the log)")
        self.assertEqual(e.hint, "rename or exclude one of them at the source")


class WindowsPathTest(unittest.TestCase):
    def test_long_path(self):
        cases = [
            (("C:\\work", ("a", "b")), "\\\\?\\C:\\work\\a\\b"),
            (("C:\\", ("a",)), "\\\\?\\C:\\a"),
            (("C:\\", ()), "\\\\?\\C:\\"),
            (("\\\\srv\\share\\x", ("a",)), "\\\\?\\UNC\\srv\\share\\x\\a"),
            (("\\\\?\\C:\\x", ("a",)), "\\\\?\\C:\\x\\a"),
            (("C:/work/sub/../x", ()), "\\\\?\\C:\\work\\x"),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(pathrules.win_long_path(*args), want)

    def test_display(self):
        self.assertEqual(pathrules.win_display("\\\\?\\UNC\\srv\\share"), "\\\\srv\\share")
        self.assertEqual(pathrules.win_display("\\\\?\\C:\\x"), "C:\\x")
        self.assertEqual(pathrules.win_display("C:\\x"), "C:\\x")

    def test_is_absolute(self):
        # the same answers on every host and every Python: on Windows a drive or a share is
        # needed, since the rest depends on the current directory
        cases = [("C:\\x", "windows", True), ("C:/x", "windows", True),
                 ("\\\\srv\\share\\x", "windows", True), ("//srv/share/x", "windows", True),
                 ("\\\\?\\C:\\x", "windows", True), ("/srv/x", "windows", False),
                 ("\\x", "windows", False), ("C:x", "windows", False), ("x", "windows", False),
                 ("/srv/x", "linux", True), ("/srv/x", "darwin", True), ("C:\\x", "linux", False),
                 ("x", "linux", False)]
        for path, osn, want in cases:
            with self.subTest(path=path, osn=osn):
                self.assertIs(pathrules.is_absolute(path, osn), want)


if __name__ == "__main__":
    unittest.main()
