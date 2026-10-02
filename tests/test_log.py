"""Log files: line format, rollover, and never raising."""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from vcharon import log as logmod
from vcharon.log import Log

LINE = r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  \d{8}-\d{6}-[0-9a-f]{6}  %s  %s\Z"


class LogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ferry-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "logs", "ferry.log")

    def lines(self, path=None):
        with open(path or self.path, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_line_format(self):
        log = Log(self.path)
        self.assertRegex(log.run_id, r"\A\d{8}-\d{6}-[0-9a-f]{6}\Z")
        log.info("hello there")
        log.warn("ünïcode")
        log.debug("d")
        log.error("e")
        lines = self.lines()
        self.assertRegex(lines[0], LINE % ("info", "hello there"))
        self.assertRegex(lines[1], LINE % ("warn", "ünïcode"))
        self.assertRegex(lines[2], LINE % ("debug", "d"))
        self.assertRegex(lines[3], LINE % ("error", "e"))
        self.assertTrue(all(line.split("  ")[1] == log.run_id for line in lines))

    def test_multi_line_message(self):
        log = Log(self.path, run_id="20260926-080000-abcdef")
        log.info("first\nsecond\r\nthird")
        lines = self.lines()
        self.assertEqual(len(lines), 3)
        for line, text in zip(lines, ("first", "second", "third")):
            self.assertRegex(line, LINE % ("info", text))
            self.assertIn("  20260926-080000-abcdef  ", line)

    def test_lone_surrogate_is_replaced(self):
        Log(self.path).info("bad \udcff byte")
        self.assertIn("bad ? byte", self.lines()[0])

    def test_rollover(self):
        with mock.patch.object(logmod, "ROLL_BYTES", 500):
            log = Log(self.path)
            count = 0
            while not os.path.exists(self.path) or os.path.getsize(self.path) < 500:
                log.info("first %d" % count)
                count += 1
            self.assertFalse(os.path.exists(self.path + ".1"))
            log.info("rolls")
            self.assertEqual(len(self.lines(self.path + ".1")), count)
            self.assertEqual(len(self.lines()), 1)
            while os.path.getsize(self.path) < 500:
                log.info("second")
            log.info("rolls again")
            # the second rollover replaces the old .1
            old = self.lines(self.path + ".1")
            self.assertIn("rolls", old[0])
            self.assertFalse(any("first" in line for line in old))
            self.assertEqual(len(self.lines()), 1)
            self.assertIn("rolls again", self.lines()[0])

    def test_replace_failing_keeps_appending(self):
        with mock.patch.object(logmod, "ROLL_BYTES", 10), \
                mock.patch("os.replace", side_effect=PermissionError(13, "in use")):
            log = Log(self.path)
            log.info("one")
            log.info("two")
            log.info("three")
        self.assertEqual(len(self.lines()), 3)
        self.assertFalse(os.path.exists(self.path + ".1"))

    def test_unwritable_places_never_raise(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as f:
            f.write("x")
        # a directory can't be made under a file
        Log(os.path.join(blocker, "logs", "ferry.log")).info("lost")
        locked = os.path.join(self.tmp, "locked")
        os.mkdir(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        Log(os.path.join(locked, "ferry.log")).error("lost too")
        if os.name == "posix" and os.geteuid() != 0:
            self.assertFalse(os.path.exists(os.path.join(locked, "ferry.log")))

    def test_console(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            Log(self.path, console=True).info("to both")
            Log(os.path.join(self.tmp, "quiet.log")).info("file only")
        self.assertRegex(err.getvalue().strip(), LINE % ("info", "to both"))
        self.assertIn("to both", self.lines()[0])


if __name__ == "__main__":
    unittest.main()
