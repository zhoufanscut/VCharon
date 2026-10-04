"""Frames, messages, errors and file streams."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from vcharon import helper, proto
from vcharon.proto import D, E, J, VCharonError


class Loopback:
    """An in-memory conn: what one side sends, the same object reads back."""

    def __init__(self):
        self.buf = io.BytesIO()
        self.rpos = 0

    def _put(self, data):
        self.buf.seek(0, io.SEEK_END)
        self.buf.write(data)

    def send_json(self, obj):
        self._put(proto.frame(J, proto.encode_json(obj)))

    def send_data(self, data):
        for f in proto.data_frames(data):
            self._put(f)

    def send_file_end(self, error=None):
        self._put(proto.end_frame(error))

    def _read_exact(self, n):
        self.buf.seek(self.rpos)
        data = self.buf.read(n)
        self.rpos += len(data)
        return data

    def next_frame(self):
        frame = proto.read_frame(self._read_exact)
        if frame is None:
            raise VCharonError("lost", "end of file")
        kind, payload = frame
        return (kind, proto.decode_json(payload)) if kind == J else (kind, payload)


def frames_of(data):
    """Every (kind, payload) in a byte string."""
    src = io.BytesIO(data)
    out = []
    while True:
        frame = proto.read_frame(src.read)
        if frame is None:
            return out
        out.append(frame)


class Reader(io.BytesIO):
    """A file whose read() fails after `fail_after` bytes."""

    def __init__(self, data, fail_after=None):
        io.BytesIO.__init__(self, data)
        self.fail_after = fail_after
        self.closed_by_sender = False

    def read(self, n=-1):
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise OSError(5, "Input/output error")
        return io.BytesIO.read(self, n)

    def close(self):
        self.closed_by_sender = True
        io.BytesIO.close(self)


def collect(files):
    """A stage that keeps each file's bytes in files[index]."""
    def stage(index, fin):
        files[index] = fin.read()
    return stage


class FrameTest(unittest.TestCase):
    def test_round_trips(self):
        msg = {"t": "call", "id": 7, "fn": "echo", "args": {"name": "中文 ok"}}
        data = proto.frame(J, proto.encode_json(msg)) + proto.frame(D, b"\x00\xff")
        data += proto.end_frame() + proto.end_frame(VCharonError("vanished", "gone"))
        (k1, p1), (k2, p2), (k3, p3), (k4, p4) = frames_of(data)
        self.assertEqual((k1, proto.decode_json(p1)), (J, msg))
        self.assertEqual((k2, p2), (D, b"\x00\xff"))
        self.assertEqual((k3, p3), (E, b""))
        self.assertEqual(k4, E)
        err = proto.decode_error(p4)
        self.assertEqual((err.code, err.message), ("vanished", "gone"))

    def test_helper_conn_send_data_splits(self):
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        conn = helper.HelperConn(io.BytesIO(), fd)
        data = os.urandom(600000)
        conn.send_data(data)
        conn.send_data(b"")
        conn.close()
        with open(path, "rb") as f:
            frames = frames_of(f.read())
        self.assertEqual([len(p) for k, p in frames], [262144, 262144, 75712])
        self.assertEqual(b"".join(p for k, p in frames), data)

    def test_clean_end(self):
        self.assertIsNone(proto.read_frame(io.BytesIO().read))

    def test_short_header_and_payload_are_lost(self):
        for data in (b"J\x00", proto.frame(D, b"abc")[:-1]):
            with self.assertRaises(VCharonError) as cm:
                proto.read_frame(io.BytesIO(data).read)
            self.assertEqual(cm.exception.code, "lost")

    def test_bad_headers_are_protocol_without_reading(self):
        calls = []

        for head in (proto.HEADER.pack(0x51, 1), proto.HEADER.pack(D, 0),
                     proto.HEADER.pack(D, 262145), proto.HEADER.pack(J, proto.MAX_JSON + 1),
                     proto.HEADER.pack(E, proto.MAX_END + 1)):
            calls.clear()

            def read_exact(n, head=head):
                calls.append(n)
                if len(calls) > 1:
                    raise AssertionError("read the payload")
                return head

            with self.assertRaises(VCharonError) as cm:
                proto.read_frame(read_exact)
            self.assertEqual(cm.exception.code, "protocol")
            self.assertEqual(calls, [proto.HEADER.size])

    def test_bad_json_is_protocol(self):
        for payload in (b"\xff\xfe", b"{", b"[1, 2]", b'{"t": 1}', b'{"x": "y"}', b"NaN",
                        b'{"t": "x", "n": NaN}'):
            with self.assertRaises(VCharonError) as cm:
                proto.decode_json(payload)
            self.assertEqual(cm.exception.code, "protocol", payload)

    def test_encode_json(self):
        self.assertEqual(proto.encode_json({"t": "é"}), '{"t":"é"}'.encode())
        with self.assertRaises(ValueError):
            proto.encode_json({"t": float("nan")})
        with self.assertRaises(UnicodeEncodeError):
            proto.encode_json({"t": "\udcff"})

    @mock.patch.object(proto, "MAX_JSON", 1 << 20)
    def test_encode_json_too_big(self):
        # a 1 MiB limit: the real one's 64 MiB messages would take most of a second to build
        big = {"t": "ok", "result": "x" * proto.MAX_JSON}
        with self.assertRaises(VCharonError) as cm:
            proto.encode_json(big)
        self.assertEqual((cm.exception.code, cm.exception.exit_code), ("too_big", 1))
        self.assertEqual(cm.exception.message,
                         "a message of 1.1 MiB is over vcharon's 1 MiB limit")
        for n, text in ((1 << 20, "1"), ((1 << 20) + 1, "1.1"), (100 << 20, "100"),
                        ((100 << 20) + (300 << 10), "100.3"), (1, "0.1")):
            self.assertEqual(proto._mib(n), text, n)
        self.assertEqual(cm.exception.hint,
                         "the plan is too big: copy the tree in parts")
        # exactly at the limit is fine
        exact = {"t": ""}
        exact["t"] = "x" * (proto.MAX_JSON - len(proto.encode_json(exact)))
        self.assertEqual(len(proto.encode_json(exact)), proto.MAX_JSON)
        # the other end knows the code
        self.assertEqual(VCharonError.from_json(cm.exception.to_json()).code, "too_big")


class ErrorTest(unittest.TestCase):
    def test_json_round_trip(self):
        err = VCharonError("no_space", "the disk is full", hint="free some space",
                           detail="Traceback ...")
        obj = err.to_json()
        self.assertEqual(obj, {"code": "no_space", "message": "the disk is full",
                               "hint": "free some space", "detail": "Traceback ..."})
        back = VCharonError.from_json(obj)
        self.assertEqual((back.code, back.message, back.hint, back.detail),
                         ("no_space", "the disk is full", "free some space", "Traceback ..."))
        self.assertEqual(VCharonError("busy", "x").to_json(), {"code": "busy", "message": "x"})

    def test_from_json_on_junk(self):
        for junk in (None, 3, "x", [], {}, {"code": "nope", "message": "m"},
                     {"code": ["x"], "message": "m"}, {"code": "busy"},
                     {"code": "busy", "message": 5}, {"code": "busy", "message": "m", "hint": 1},
                     {"code": "nope", "message": "m" * 1000}):
            err = VCharonError.from_json(junk)
            self.assertEqual(err.code, "aborted", junk)
            self.assertIn("malformed", err.message)
            # it quotes what arrived, briefly
            self.assertIn(json.dumps(junk)[:20], err.message)
            self.assertLess(len(err.message), 300)

    def test_lone_surrogates_are_escaped(self):
        # a Linux file name that isn't valid UTF-8
        err = VCharonError("permission", "can't read /srv/\udcff.txt", hint="h \udcfe",
                           detail="d \udcfd")
        obj = err.to_json()
        self.assertEqual(obj["message"], "can't read /srv/\\udcff.txt")
        self.assertEqual((obj["hint"], obj["detail"]), ("h \\udcfe", "d \\udcfd"))
        # so an err reply can always be sent
        proto.encode_json(proto.err_msg(3, err))
        self.assertEqual(err.message, "can't read /srv/\udcff.txt")

    def test_err_msg_extra_keys(self):
        err = VCharonError("kind_change", "d is a directory")
        self.assertIsNone(err.reply)
        done = {"written": ["a"], "deleted": 0, "notes": []}
        self.assertEqual(proto.err_msg(7, err, done=done),
                         {"t": "err", "id": 7, "error": err.to_json(), "done": done})
        self.assertEqual(proto.err_msg(None, err), {"t": "err", "id": None,
                                                    "error": err.to_json()})
        # the reply never goes over the wire
        err.reply = {"t": "err"}
        self.assertNotIn("reply", err.to_json())

    def test_exit_codes(self):
        for code, exit_code in (("config", 3), ("busy", 2), ("connect", 4), ("timeout", 1),
                                ("lost", 1), ("protocol", 1), ("state_mismatch", 3),
                                ("internal", 1)):
            self.assertEqual(VCharonError(code, "m").exit_code, exit_code)


class StreamTest(unittest.TestCase):
    def test_three_files_with_an_empty_one(self):
        conn = Loopback()
        blobs = {3: os.urandom(300000), 5: b"", 8: b"small"}
        proto.send_stream(conn, [(i, lambda i=i: Reader(blobs[i])) for i in (3, 5, 8)])
        got = {}
        proto.receive_stream(conn, [3, 5, 8], collect(got))
        self.assertEqual(got, blobs)
        with self.assertRaises(VCharonError):
            conn.next_frame()

    def test_the_middle_opener_fails(self):
        conn = Loopback()
        first, last = b"a" * 10, os.urandom(300000)
        readers = [Reader(first), Reader(last)]
        opened = []

        def gone():
            raise VCharonError("vanished", "b.txt is gone", "it changed during the run; run again")

        def third():
            opened.append(2)
            return readers[1]

        proto.send_stream(conn, [(0, lambda: readers[0]), (1, gone), (2, third)])
        self.assertTrue(readers[0].closed_by_sender)
        # the sender stopped after the failed file, without opening the next: the run fails
        # anyway
        self.assertEqual(opened, [])
        frames = frames_of(conn.buf.getvalue())
        self.assertEqual(b"".join(p for k, p in frames if k == D), first)
        self.assertEqual(frames[-1][0], J)
        self.assertEqual(json.loads(frames[-1][1].decode("utf-8")),
                         {"t": "end", "error": {"code": "vanished", "message": "b.txt is gone",
                                                "hint": "it changed during the run; run again"}})
        got = {}
        with self.assertRaises(VCharonError) as cm:
            proto.receive_stream(conn, [0, 1, 2], collect(got))
        # the file's own error, hint and all, from its E frame
        self.assertEqual((cm.exception.code, cm.exception.message, cm.exception.hint),
                         ("vanished", "b.txt is gone", "it changed during the run; run again"))
        self.assertEqual(got, {0: first})
        with self.assertRaises(VCharonError) as cm:
            conn.next_frame()
        self.assertEqual(cm.exception.code, "lost")

    def test_read_error_ends_the_stream_too(self):
        conn = Loopback()
        opened = []

        def second():
            opened.append(1)
            return Reader(b"never")

        proto.send_stream(conn, [(0, lambda: Reader(b"xyz", fail_after=0)), (1, second)])
        self.assertEqual(opened, [])
        with self.assertRaises(VCharonError) as cm:
            proto.receive_stream(conn, [0, 1], collect({}))
        self.assertEqual(cm.exception.code, "aborted")
        self.assertIn("couldn't read file 0", cm.exception.message)

    def test_stage_error_drains_the_rest(self):
        conn = Loopback()
        proto.send_stream(conn, [(i, lambda: Reader(os.urandom(400000))) for i in range(3)])
        conn.send_json({"t": "after"})
        staged = []

        def stage(index, fin):
            staged.append(index)
            fin.read(10)
            raise OSError(28, "No space left on device")

        with self.assertRaises(OSError):
            proto.receive_stream(conn, [0, 1, 2], stage)
        self.assertEqual(staged, [0])
        # read exactly to the end: the next frame is the one after it
        self.assertEqual(conn.next_frame(), (J, {"t": "after"}))

    def test_end_with_an_error(self):
        conn = Loopback()

        def files():
            yield 0, lambda: Reader(b"first")
            raise VCharonError("permission", "can't list /secret")

        proto.send_stream(conn, files())
        got = {}
        with self.assertRaises(VCharonError) as cm:
            proto.receive_stream(conn, [0, 1], collect(got))
        self.assertEqual(cm.exception.code, "permission")
        self.assertEqual(got, {0: b"first"})

    def test_out_of_step_streams_are_protocol(self):
        cases = []
        conn = Loopback()
        proto.send_stream(conn, [(1, lambda: Reader(b"x"))])
        cases.append((conn, [0]))
        conn = Loopback()
        proto.send_stream(conn, [(0, lambda: Reader(b"x"))])
        cases.append((conn, [0, 1]))
        conn = Loopback()
        proto.send_stream(conn, [(0, lambda: Reader(b"x")), (1, lambda: Reader(b"y"))])
        cases.append((conn, [0]))
        conn = Loopback()
        conn.send_data(b"x")
        cases.append((conn, [0]))
        conn = Loopback()
        conn.send_json({"t": "file", "index": 0})
        conn.send_json({"t": "tick"})
        cases.append((conn, [0]))
        for conn, indexes in cases:
            with self.assertRaises(VCharonError) as cm:
                proto.receive_stream(conn, indexes, collect({}))
            self.assertEqual(cm.exception.code, "protocol")

    def test_err_between_files(self):
        conn = Loopback()
        proto.send_stream(conn, [(0, lambda: Reader(b"x"))])
        conn.buf = io.BytesIO(conn.buf.getvalue()[:-len(proto.frame(J, b'{"t":"end"}'))])
        conn.send_json(proto.err_msg(4, VCharonError("internal", "ZeroDivisionError: boom",
                                                     detail="Traceback ...")))
        conn.send_json({"t": "after"})
        got = {}
        with self.assertRaises(VCharonError) as cm:
            proto.receive_stream(conn, [0, 1], collect(got))
        self.assertEqual((cm.exception.code, cm.exception.detail), ("internal", "Traceback ..."))
        self.assertEqual(got, {0: b"x"})
        self.assertEqual(conn.next_frame(), (J, {"t": "after"}))

    def test_err_inside_a_file(self):
        conn = Loopback()
        conn.send_json({"t": "file", "index": 0})
        conn.send_data(b"partial")
        conn.send_json(proto.err_msg(4, VCharonError("no_space", "the disk is full")))
        conn.send_json({"t": "after"})
        seen = []

        def stage(index, fin):
            seen.append(fin.read(100))
            fin.read(100)

        with self.assertRaises(VCharonError) as cm:
            proto.receive_stream(conn, [0], stage)
        self.assertEqual(cm.exception.code, "no_space")
        self.assertEqual(seen, [b"partial"])
        # nothing past the err was drained
        self.assertEqual(conn.next_frame(), (J, {"t": "after"}))

    def test_err_inside_a_file_that_stage_swallowed(self):
        conn = Loopback()
        conn.send_json({"t": "file", "index": 0})
        conn.send_json(proto.err_msg(4, VCharonError("vanished", "gone")))

        def stage(index, fin):
            try:
                fin.read()
            except VCharonError:
                pass

        with self.assertRaises(VCharonError) as cm:
            proto.receive_stream(conn, [0], stage)
        self.assertEqual(cm.exception.code, "vanished")

    def test_file_in_reads_in_pieces(self):
        conn = Loopback()
        data = os.urandom(600000)
        conn.send_data(data)
        conn.send_file_end()
        fin = proto.FileIn(conn, 0)
        parts = []
        while True:
            part = fin.read(100000)
            if not part:
                break
            self.assertLessEqual(len(part), 100000)
            parts.append(part)
        self.assertEqual(b"".join(parts), data)
        self.assertEqual(fin.read(), b"")


if __name__ == "__main__":
    unittest.main()
