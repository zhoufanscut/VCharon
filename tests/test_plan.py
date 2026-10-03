"""Plan types and their JSON form (DESIGN, "Plans")."""

from __future__ import annotations

import json
import unittest

from vcharon import plan
from vcharon.proto import VCharonError

EXAMPLE = {
    "entries": [
        {"op": "put", "path": "docs", "kind": "dir"},
        {"op": "put", "path": "docs/a.txt", "kind": "file", "size": 1234,
         "mtime": 1790000000.25, "exec": False},
        {"op": "delete", "path": "old.txt", "why": "pruned"},
        {"op": "delete", "path": "build", "tree": True, "why": "deleted in svn"},
    ],
    "identity": None,
    "state": {},
    "notes": ["skipped 2 symlinks"],
}

FILE = {"op": "put", "path": "a", "kind": "file", "size": 1, "mtime": 2}
HASH = "0123456789abcdef" * 4


def with_entry(entry):
    return {"entries": [entry]}


class RoundTripTest(unittest.TestCase):
    def test_example(self):
        p = plan.from_json(json.loads(json.dumps(EXAMPLE)))
        self.assertEqual(p.entries[1], plan.put_file("docs/a.txt", 1234, 1790000000.25, False))
        self.assertEqual(p.entries[3], plan.delete("build", tree=True, why="deleted in svn"))
        text = json.dumps(plan.to_json(p))
        self.assertEqual(json.loads(text), EXAMPLE)

    def test_defaults(self):
        p = plan.from_json({"entries": [FILE]})
        self.assertIsNone(p.entries[0].executable)
        self.assertEqual((p.identity, p.state, p.notes), (None, None, []))
        self.assertEqual(plan.to_json(p), {"entries": [FILE], "identity": None, "state": None,
                                           "notes": []})
        # a new list per Plan
        self.assertIsNot(plan.Plan([]).notes, plan.Plan([]).notes)

    def test_builders(self):
        self.assertEqual(plan.put_dir("d"), plan.Entry("put", "d", kind="dir"))
        self.assertEqual(plan.to_json(plan.Plan([plan.delete("x")]))["entries"],
                         [{"op": "delete", "path": "x"}])

    def test_sha256(self):
        # file puts of a --full run (DESIGN, "Plans") carry their bytes' sha256
        e = plan.put_file("a", 1, 2.5, True, HASH)
        self.assertEqual(e.sha256, HASH)
        obj = plan.to_json(plan.Plan([e]))["entries"][0]
        self.assertEqual(obj, {"op": "put", "path": "a", "kind": "file", "size": 1,
                               "mtime": 2.5, "exec": True, "sha256": HASH})
        self.assertEqual(plan.from_json(json.loads(json.dumps({"entries": [obj]}))).entries,
                         [e])
        # without it: none, and no key
        e = plan.put_file("a", 1, 2.5)
        self.assertIsNone(e.sha256)
        self.assertNotIn("sha256", plan.to_json(plan.Plan([e]))["entries"][0])
        self.assertIsNone(plan.from_json(with_entry(FILE)).entries[0].sha256)


class RefusalTest(unittest.TestCase):
    def refused(self, obj):
        with self.assertRaises(VCharonError) as cm:
            plan.from_json(obj)
        self.assertEqual(cm.exception.code, "protocol")
        self.assertIn("a malformed plan", cm.exception.message)
        return cm.exception

    def test_structure(self):
        cases = [
            [],
            "plan",
            {},
            {"entries": {}},
            {"entries": [], "extra": 1},
            {"entries": [], "notes": "x"},
            {"entries": [], "notes": [1]},
            {"entries": [], "identity": []},
            {"entries": [], "identity": "id"},
            {"entries": [], "state": 3},
            with_entry("a"),
        ]
        for obj in cases:
            with self.subTest(obj=obj):
                self.refused(obj)

    def test_entries(self):
        cases = [
            dict(FILE, extra=1),
            {"op": "put", "path": "d", "kind": "dir", "size": 1},
            {"op": "put", "path": "d", "kind": "dir", "extra": 1},
            {"op": "delete", "path": "d", "extra": 1},
            {"op": "copy", "path": "a"},
            {"path": "a"},
            {"op": "put", "path": "a", "kind": "link"},
            {"op": "put", "path": "a"},
            {"op": "put", "path": "a", "kind": "file", "mtime": 1},
            dict(FILE, size=True),
            dict(FILE, size=-1),
            dict(FILE, size=1.0),
            dict(FILE, size="1"),
            dict(FILE, mtime=float("inf")),
            dict(FILE, mtime=float("nan")),
            dict(FILE, mtime=False),
            dict(FILE, mtime="1"),
            dict(FILE, mtime=1e12),
            dict(FILE, mtime=-1e12),
            dict(FILE, mtime=10 ** 400),
            dict(FILE, mtime=-10 ** 400),
            dict(FILE, exec=1),
            dict(FILE, exec=None),
            {"op": "delete", "path": "a", "tree": 1},
            {"op": "delete", "path": "a", "why": 3},
            {"op": "delete", "path": 3},
            {"op": "delete"},
            dict(FILE, path=None),
            dict(FILE, sha256=HASH.upper()),
            dict(FILE, sha256=HASH[:63]),
            dict(FILE, sha256=HASH + "0"),
            dict(FILE, sha256=HASH[:63] + "g"),
            dict(FILE, sha256=HASH + "\n"),
            dict(FILE, sha256=None),
            dict(FILE, sha256=12),
            dict(FILE, sha256=[HASH]),
            {"op": "put", "path": "d", "kind": "dir", "sha256": HASH},
            {"op": "delete", "path": "d", "sha256": HASH},
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                self.refused(with_entry(entry))

    def test_bad_sha256_message(self):
        e = self.refused(with_entry(dict(FILE, sha256="ABC")))
        self.assertEqual(e.message, 'a malformed plan: entry 0 has a bad sha256 "ABC"')
        e = self.refused(with_entry({"op": "put", "path": "d", "kind": "dir", "sha256": HASH}))
        self.assertEqual(e.message, 'a malformed plan: entry 0 has unknown keys: ["sha256"]')

    def test_accepts(self):
        p = plan.from_json({"entries": [dict(FILE, mtime=1.5, exec=True),
                                        {"op": "delete", "path": "x", "tree": False}]})
        self.assertTrue(p.entries[0].executable)
        self.assertFalse(p.entries[1].tree)
        for mtime in (1e11, -1e11, 0, 10 ** 11):
            self.assertEqual(plan.from_json(with_entry(dict(FILE, mtime=mtime))).entries[0].mtime,
                             mtime)


if __name__ == "__main__":
    unittest.main()
