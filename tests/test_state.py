"""State files, the fingerprint and the job lock (DESIGN §11.2, §11.3)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

from vcharon import config, state
from vcharon.proto import VCharonError

from tests.util import fd_count, use_test_jobs

JOB = """
[j]
ssh       = devbox
from      = local:path
from.path = ~/src
to        = remote:dir
to.path   = inbox
"""

GOOD = {"schema": 1, "job": "j", "fingerprint": "a" * 64, "identity": None,
        "sink": {"end": "local", "root": "/r"}, "source": None, "saved": "2026-09-28T08:00:00Z"}

SAVED = r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z"


class StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"VCHARON_HOME": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = os.path.join(self.tmp, "state")
        use_test_jobs(self)

    def job(self, text=JOB, name="j"):
        with open(os.path.join(self.tmp, "vcharon.ini"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(text))
        return config.load().jobs[name]

    def write(self, data, name="j"):
        os.makedirs(self.dir, exist_ok=True)
        with open(state.path(name), "wb") as f:
            f.write(data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))


class FingerprintTest(StateCase):
    def test_what_changes_it(self):
        base = state.fingerprint(self.job())
        self.assertRegex(base, r"\A[0-9a-f]{64}\Z")
        # stable
        self.assertEqual(state.fingerprint(self.job()), base)
        changed = [("ssh       = devbox", "ssh       = other"),
                   ("from      = local:path", "from      = local:svn-changes"),
                   ("to        = remote:dir", "to        = remote:file"),
                   ("from.path = ~/src", "from.path = ~/src2"),
                   ("to.path   = inbox", "to.path   = inbox2")]
        for old, new in changed:
            with self.subTest(new=new):
                self.assertNotEqual(state.fingerprint(self.job(JOB.replace(old, new))), base)
        # the other options can change without a reset
        for extra in ("from.exclude = *.log", "from.prune = yes", "to.create = yes",
                      "idle_timeout = 60", "to.max_deletes = 5"):
            with self.subTest(extra=extra):
                self.assertEqual(state.fingerprint(self.job(JOB + extra + "\n")), base)

    def test_missing_path_isnt_empty(self):
        missing = state.fingerprint(self.job(JOB.replace("to.path   = inbox\n", "")))
        empty = state.fingerprint(self.job(JOB.replace("to.path   = inbox", "to.path =")))
        self.assertNotEqual(missing, empty)
        # the raw values, with null for a missing path
        want = json.dumps(["devbox", "local:path", "remote:dir", "~/src", None],
                          separators=(",", ":"))
        self.assertEqual(missing, hashlib.sha256(want.encode("utf-8")).hexdigest())

    def test_jobs_keep_their_fingerprint(self):
        # M7b added the writer's name for mailbox jobs only: a state file saved before it
        # still matches its job
        self.assertEqual(state.fingerprint(self.job()),
                         "36763e7a62c050af30605c45b1d4951828978bf10747e05828128e99c453c7f4")


class SaveLoadTest(StateCase):
    def test_round_trip(self):
        self.assertIsNone(state.load("j"))
        st = state.State("b" * 64, {"end": "local", "path": "/src"},
                         {"end": "remote", "machine": "f" * 32, "root": "/home/me/中 文"},
                         {"sent": {"a": "d", "a/b": [1, 1790000000.25, False]}}, state.now())
        self.assertRegex(st.saved, SAVED)
        state.save("j", st)
        self.assertEqual(state.load("j"), st)
        with open(state.path("j"), "rb") as f:
            data = f.read()
        doc = json.loads(data.decode("utf-8"))
        self.assertEqual(list(doc), ["schema", "job", "fingerprint", "identity", "sink",
                                     "source", "saved"])
        self.assertEqual((doc["schema"], doc["job"]), (1, "j"))
        # compact UTF-8
        self.assertEqual(data, json.dumps(doc, ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8"))
        self.assertIn("中 文".encode("utf-8"), data)
        self.assertEqual(os.listdir(self.dir), ["j.json"])
        self.assertEqual(state.path("j"), os.path.join(self.tmp, "state", "j.json"))
        # saved again: replaced
        state.save("j", state.State("c" * 64, None, {"end": "local", "root": "/r"}, None,
                                    state.now()))
        self.assertEqual(state.load("j").fingerprint, "c" * 64)

    def test_failed_replace(self):
        state.save("j", state.State("b" * 64, None, {"end": "local", "root": "/r"}, None,
                                    state.now()))
        with open(state.path("j"), "rb") as f:
            before = f.read()
        failing = OSError(errno.EIO, os.strerror(errno.EIO))
        with mock.patch.object(os, "replace", side_effect=failing):
            with self.assertRaises(VCharonError) as cm:
                state.save("j", state.State("c" * 64, None, {"end": "local", "root": "/x"},
                                            None, state.now()))
        self.assertEqual(cm.exception.code, "io")
        self.assertIn(state.path("j"), cm.exception.message)
        # no temp file left, and the old file intact
        self.assertEqual(os.listdir(self.dir), ["j.json"])
        with open(state.path("j"), "rb") as f:
            self.assertEqual(f.read(), before)

    def test_load_refuses(self):
        cases = [b"not json", b"\xff\xfe", b"[]", b"{\"a\": NaN}", dict(GOOD, schema=2),
                 dict(GOOD, schema=True), {k: v for k, v in GOOD.items() if k != "saved"},
                 dict(GOOD, extra=1), dict(GOOD, fingerprint="A" * 64),
                 dict(GOOD, fingerprint="a" * 63), dict(GOOD, fingerprint=None),
                 dict(GOOD, identity=[]), dict(GOOD, sink={"end": "local"}),
                 dict(GOOD, sink={"end": "remote", "root": "/r"}),
                 dict(GOOD, sink={"end": "local", "root": "/r", "machine": "m"}),
                 dict(GOOD, sink={"end": "moon", "root": "/r"}),
                 dict(GOOD, sink={"end": "local", "root": 1}), dict(GOOD, sink=None),
                 dict(GOOD, source="x"), dict(GOOD, saved=1), dict(GOOD, job="other")]
        for data in cases:
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(VCharonError) as cm:
                    state.load("j")
                e = cm.exception
                self.assertEqual((e.code, e.exit_code), ("state_mismatch", 3))
                self.assertTrue(e.message.startswith("the state file %s can't be read: "
                                                     % state.path("j")), e.message)
                self.assertEqual(e.hint, "check the target; then: vcharon state reset j, and "
                                         "vcharon run j --full")
                st, why = state.read("j")
                self.assertIsNone(st)
                self.assertTrue(why)
        self.write(GOOD)
        self.assertEqual(state.load("j").sink, GOOD["sink"])
        self.write(dict(GOOD, sink={"end": "remote", "root": "/r", "machine": "m"}))
        self.assertEqual(state.load("j").sink["machine"], "m")

    def test_unreadable(self):
        os.makedirs(state.path("j"))
        with self.assertRaises(VCharonError) as cm:
            state.load("j")
        self.assertEqual(cm.exception.code, "state_mismatch")

    def test_messages(self):
        self.write(dict(GOOD, job="other"))
        self.assertEqual(state.read("j"), (None, 'it\'s the state of "other"'))
        self.write(dict(GOOD, schema=2))
        self.assertEqual(state.read("j"), (None, "its schema is 2, not 1"))
        self.write(b"\xff")
        self.assertEqual(state.read("j"), (None, "it isn't valid UTF-8 (byte 0)"))

    def test_remove(self):
        self.assertFalse(state.remove("j"))
        self.write(GOOD)
        self.assertTrue(state.remove("j"))
        self.assertFalse(os.path.exists(state.path("j")))
        self.assertIsNone(state.load("j"))


class LockTest(StateCase):
    @unittest.skipUnless(os.path.isdir("/dev/fd"), "needs /dev/fd")
    def test_busy(self):
        before = fd_count()
        held = state.lock("j")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "j.lock")))
        with self.assertRaises(VCharonError) as cm:
            state.lock("j")
        e = cm.exception
        self.assertEqual((e.code, e.exit_code), ("busy", 2))
        self.assertEqual(e.message, "another run of j is in progress")
        self.assertEqual(e.hint, "wait for it to finish")
        # another job's lock is its own
        state.lock("k").release()
        held.release()
        state.lock("j").release()
        self.assertEqual(fd_count(), before)


if __name__ == "__main__":
    unittest.main()
