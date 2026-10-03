"""Frames, messages and file streams: the wire protocol (DESIGN, "Wire protocol"), on both ends."""

from __future__ import annotations

import json
import struct
import traceback

# Error code -> vcharon's exit code (DESIGN, "Error codes").
EXIT = {"config": 3, "bad_options": 3, "missing_capability": 3, "state_mismatch": 3,
        "busy": 2, "connect": 4, "timeout": 1, "lost": 1, "protocol": 1, "not_found": 1,
        "unsafe_path": 1, "unsafe_dir": 1, "collision": 1, "kind_change": 1,
        "too_many_deletes": 1, "empty_source": 1, "vanished": 1, "in_use": 1,
        "permission": 1, "no_space": 1, "aborted": 1, "io": 1, "too_big": 1, "internal": 1,
        "channel": 1, "refused": 1}


class VCharonError(Exception):
    """An error the user sees: a code from EXIT, a message, and the one line that says what to
    do."""

    def __init__(self, code, message, hint=None, detail=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.hint = hint
        # a traceback, for the log only
        self.detail = detail
        # stderr lines to show under the message (client only)
        self.tail = []
        # the whole err message this error came from (client only; Session.call sets it)
        self.reply = None

    @property
    def exit_code(self):
        return EXIT.get(self.code, 1)

    def to_json(self):
        obj = {"code": self.code, "message": _printable(self.message)}
        if self.hint is not None:
            obj["hint"] = _printable(self.hint)
        if self.detail is not None:
            obj["detail"] = _printable(self.detail)
        return obj

    @classmethod
    def from_json(cls, obj):
        if (isinstance(obj, dict) and isinstance(obj.get("code"), str) and obj["code"] in EXIT
                and isinstance(obj.get("message"), str)
                and all(isinstance(obj.get(k), (str, type(None))) for k in ("hint", "detail"))):
            return cls(obj["code"], obj["message"], obj.get("hint"), obj.get("detail"))
        return cls("aborted", "the other side sent a malformed error: %s" % quote(obj))


def _printable(text):
    # A name that isn't valid UTF-8 reaches Python as lone surrogates, which encode_json refuses.
    # In an error's text they're shown escaped instead, so reporting the error can't fail.
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def quote(obj, limit=200):
    """A short, printable form of something that arrived over the wire."""
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        try:
            text = repr(obj)
        except Exception:  # noqa: BLE001
            text = "<%s>" % type(obj).__name__
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


# Frames (DESIGN, "Frames"): kind (1 byte), length (4 bytes, big-endian), payload.
J = 0x4A
D = 0x44
E = 0x45
HEADER = struct.Struct(">BI")
MAX_JSON = 64 << 20
MAX_DATA = 262144
MAX_END = MAX_JSON
_LIMITS = {J: MAX_JSON, D: MAX_DATA, E: MAX_END}

# How much of a file a sender reads at a time: one full D frame.
CHUNK = MAX_DATA


def _mib(n):
    # rounded up, so a message just over the limit never reads as equal to it
    return "%d" % (n >> 20) if n % (1 << 20) == 0 else "%.1f" % (-(-n * 10 >> 20) / 10)


def encode_json(obj):
    # A lone surrogate makes encode() raise. That's right: it's a bug on this end.
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    if len(data) > MAX_JSON:
        # Nothing was sent, so both sides are still in step. In practice it's a huge plan.
        raise VCharonError("too_big", "a message of %s MiB is over vcharon's %s MiB limit"
                           % (_mib(len(data)), _mib(MAX_JSON)),
                           "the plan is too big: exclude part of the tree, or copy it in parts")
    return data


def frame(kind, payload):
    return HEADER.pack(kind, len(payload)) + payload


def data_frames(data):
    """D frames of at most MAX_DATA bytes each; none for empty data."""
    view = memoryview(data)
    for start in range(0, len(view), MAX_DATA):
        yield frame(D, view[start:start + MAX_DATA])


def end_frame(error=None):
    """The E frame that ends one file: empty when complete, else the error."""
    return frame(E, b"" if error is None else encode_json(error.to_json()))


def read_frame(read_exact):
    """The next (kind, payload), or None at a clean end of file. read_exact(n) returns n bytes,
    or fewer only at end of file."""
    head = read_exact(HEADER.size)
    if not head:
        return None
    if len(head) < HEADER.size:
        raise VCharonError("lost", "the stream ended inside a frame")
    kind, size = HEADER.unpack(head)
    limit = _LIMITS.get(kind)
    if limit is None:
        raise VCharonError("protocol", "unknown frame kind 0x%02x" % kind)
    # Checked before reading, so a bad header never makes us allocate 64 MiB.
    if size > limit or (kind == D and size == 0):
        raise VCharonError("protocol", "a %s frame of %d bytes is outside its limits"
                           % (chr(kind), size))
    payload = read_exact(size) if size else b""
    if len(payload) < size:
        raise VCharonError("lost", "the stream ended inside a frame")
    return kind, payload


def _no_constant(name):
    raise ValueError("%s isn't valid JSON" % name)


def _loads(payload, what):
    try:
        return json.loads(bytes(payload).decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, RecursionError) as e:
        raise VCharonError("protocol", "%s isn't valid JSON: %s" % (what, e))


def decode_json(payload):
    """The message in a J frame."""
    obj = _loads(payload, "a J frame")
    if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
        raise VCharonError("protocol", "a J frame isn't a message: %s" % quote(obj))
    return obj


def decode_error(payload):
    """The error a non-empty E frame carries."""
    return VCharonError.from_json(_loads(payload, "an E frame"))


def call_msg(call_id, fn, args):
    return {"t": "call", "id": call_id, "fn": fn, "args": args}


def ok_msg(call_id, result):
    return {"t": "ok", "id": call_id, "result": result}


def err_msg(call_id, error, **extra):
    """extra keys go into the message too: a failed sink.commit sends what it did as done."""
    msg = {"t": "err", "id": call_id, "error": error.to_json()}
    msg.update(extra)
    return msg


def log_msg(level, msg):
    return {"t": "log", "level": level, "msg": msg}


TICK = {"t": "tick"}


def is_id(value):
    """True for a call id or a file index: an int, and not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


# File streams (DESIGN, "Messages and file streams"). A conn has send_json, send_data, send_file_end
# and next_frame; helper.HelperConn and ssh.Session both do.

def send_stream(conn, files):
    """Sends one file stream. files: an iterable of (index, opener); opener() returns a binary
    reader with read(n) and close(). It stops after the first file that fails: the run fails
    anyway, so the rest would move for nothing."""
    it = iter(files)
    while True:
        try:
            item = next(it)
        except StopIteration:
            break
        except Exception as e:  # noqa: BLE001
            # We can't go on at all: end the whole stream with the error.
            conn.send_json({"t": "end", "error": _stream_error(e).to_json()})
            return
        index, opener = item
        conn.send_json({"t": "file", "index": index})
        error = _send_file(conn, index, opener)
        conn.send_file_end(error)
        if error is not None:
            conn.send_json({"t": "end", "error": error.to_json()})
            return
    conn.send_json({"t": "end"})


def _stream_error(e):
    if isinstance(e, VCharonError):
        return e
    if isinstance(e, OSError):
        return VCharonError("aborted", "couldn't list the files to send: %s" % e)
    return VCharonError("internal", "%s: %s" % (type(e).__name__, e),
                        detail=traceback.format_exc())


def _send_file(conn, index, opener):
    """Sends one file's D frames. Returns the error for its E frame, or None."""
    try:
        reader = opener()
    except (VCharonError, OSError) as e:
        return _read_error(index, e)
    try:
        while True:
            try:
                data = reader.read(CHUNK)
            except (VCharonError, OSError) as e:
                return _read_error(index, e)
            if not data:
                return None
            conn.send_data(data)
    finally:
        try:
            reader.close()
        except OSError:
            pass


def _read_error(index, e):
    if isinstance(e, VCharonError):
        return e
    return VCharonError("aborted", "couldn't read file %d: %s" % (index, e))


class FileIn:
    """One incoming file: its bytes come from D frames, up to its E frame."""

    def __init__(self, conn, index):
        self.index = index
        # the error its E frame carried, once that frame has arrived
        self.error = None
        # a lost or protocol error while reading it: the two sides are out of step
        self.broken = None
        self._conn = conn
        self._chunk = b""
        self._pos = 0
        self._ended = False

    def read(self, n=-1):
        """Up to n bytes (all of them if n < 0); b"" at the end. Raises the E frame's error
        instead of returning b"" when it carries one."""
        if n is None or n < 0:
            parts = []
            while True:
                part = self.read(MAX_DATA)
                if not part:
                    return b"".join(parts)
                parts.append(part)
        if n == 0:
            return b""
        if self._pos >= len(self._chunk) and not self._fill():
            if self.error is not None:
                raise self.error
            return b""
        data = self._chunk[self._pos:self._pos + n]
        self._pos += len(data)
        return data

    def drain(self):
        """Consumes the rest of the file up to its E frame. Never raises: a stream error stays
        in `broken`."""
        try:
            while self._fill():
                pass
        except VCharonError:
            pass

    def _fill(self):
        """Takes the next D frame. False at the E frame."""
        if self._ended:
            return False
        if self.broken is not None:
            raise self.broken
        try:
            kind, payload = self._conn.next_frame()
            if kind == D:
                self._chunk, self._pos = payload, 0
                return True
            if kind == E:
                self._ended = True
                self._chunk, self._pos = b"", 0
                if payload:
                    self.error = decode_error(payload)
                return False
            if payload["t"] == "err":
                # The sender failed partway and said why; nothing more of this file comes.
                raise VCharonError.from_json(payload.get("error"))
            raise VCharonError("protocol", "a J frame arrived inside file %d: %s"
                               % (self.index, quote(payload)))
        except VCharonError as e:
            self.broken = e
            raise


def receive_stream(conn, indexes, stage):
    """Reads a whole file stream. indexes: the file indexes expected, in order. stage(index,
    file_in) takes each file. The first error, from stage or from an E frame, is raised once the
    stream has ended, so both sides stay in step."""
    expected = list(indexes)
    done = 0
    first = None
    while True:
        kind, msg = conn.next_frame()
        if kind != J:
            raise VCharonError("protocol", "a %s frame arrived between files" % chr(kind))
        t = msg["t"]
        if t == "file":
            index = msg.get("index")
            if done >= len(expected):
                raise VCharonError("protocol", "more than the %d expected files arrived"
                                   % len(expected))
            if not is_id(index) or index != expected[done]:
                raise VCharonError("protocol", "file %s arrived where file %d was expected"
                                   % (quote(index), expected[done]))
            done += 1
            fin = FileIn(conn, index)
            if first is None:
                try:
                    stage(index, fin)
                except Exception as e:  # noqa: BLE001
                    if fin.broken is not None:
                        raise fin.broken
                    first = e
            fin.drain()
            if fin.broken is not None:
                raise fin.broken
            if first is None and fin.error is not None:
                first = fin.error
        elif t == "end":
            error = msg.get("error")
            if error is None and done < len(expected):
                raise VCharonError("protocol", "the stream ended after %d of %d files"
                                   % (done, len(expected)))
            if first is not None:
                raise first
            if error is not None:
                raise VCharonError.from_json(error)
            return
        elif t == "err":
            # The sender failed partway and said why; the stream ends here.
            raise VCharonError.from_json(msg.get("error"))
        else:
            raise VCharonError("protocol", "a %s message arrived inside a file stream" % quote(t))
