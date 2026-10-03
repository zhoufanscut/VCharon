"""The helper: vcharon's code on the server for one run; it answers the controller's calls."""

from __future__ import annotations

import functools
import io
import os
import sys
import time
import traceback

from . import PROTOCOL, VERSION, channels, plan, platform, plugin, proto, stage
from .proto import VCharonError

# A busy helper sends a tick at most this often, so it never looks idle (DESIGN §6.3).
TICK_EVERY = 10
# echo's upload limit (DESIGN §7.4)
ECHO_MAX = 16 << 20

# Handlers return this to end the run.
STOP = object()


def take_stdout():
    """Moves the protocol off fd 1: a stray print, or a child process that inherits fd 1, then
    writes to stderr instead of into the frames. Returns the private protocol fd."""
    sys.stdout.flush()
    # os.dup makes a non-inheritable fd, so child processes never get it.
    out = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return out


class HelperConn:
    """The helper's end of the connection: frames in on stdin, out on the private fd."""

    def __init__(self, inp, out):
        self._inp = inp
        self._out = open(out, "wb", buffering=65536)

    def write_raw(self, data):
        self._write(data)

    def send_json(self, obj):
        self._write(proto.frame(proto.J, proto.encode_json(obj)))
        self._flush()

    def send_data(self, data):
        for f in proto.data_frames(data):
            self._write(f)

    def send_file_end(self, error=None):
        self._write(proto.end_frame(error))
        self._flush()

    def read_frame(self):
        """The next frame, with J payloads decoded, or None at a clean end of file."""
        frame = proto.read_frame(self._read_exact)
        if frame is not None and frame[0] == proto.J:
            return proto.J, proto.decode_json(frame[1])
        return frame

    def next_frame(self):
        frame = self.read_frame()
        if frame is None:
            raise VCharonError("lost", "the controller closed the connection")
        return frame

    def close(self):
        try:
            self._out.close()
        except OSError:
            pass

    def _read_exact(self, n):
        try:
            data = self._inp.read(n)
            if len(data) == n or not data:
                return data
            parts = [data]
            got = len(data)
            while got < n:
                more = self._inp.read(n - got)
                if not more:
                    break
                parts.append(more)
                got += len(more)
            return b"".join(parts)
        except OSError as e:
            raise VCharonError("lost", "reading from the controller failed: %s" % e)

    def _write(self, data):
        try:
            self._out.write(data)
        except OSError as e:
            raise VCharonError("lost", "writing to the controller failed: %s" % e)

    def _flush(self):
        try:
            self._out.flush()
        except OSError as e:
            raise VCharonError("lost", "writing to the controller failed: %s" % e)


class Helper:
    """What a handler gets: the connection, stdin, ways to reply and report, and the run's
    one source and one sink (DESIGN §7.4)."""

    def __init__(self, conn, stdin):
        self.conn = conn
        self.stdin = stdin
        self._last_tick = None
        # A dead controller stops a long walk: tick raises lost like any other write.
        self.ctx = plugin.Ctx("remote", log=self._plugin_log, tick=self.tick)
        # set by the calls below
        self.source = self.plan = self.sink = None
        # the calls that come at most once, once they have
        self.called = set()

    def _plugin_log(self, msg):
        # Logging never fails a run (decision 17 of the M3 plan).
        try:
            self.log(msg)
        except (VCharonError, OSError):
            pass

    def close_plugins(self):
        """Closes the sink, which aborts unless it committed, then the source. Never raises:
        a failure goes to stderr, which the controller logs."""
        for obj in (self.sink, self.source):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:
                try:
                    sys.stderr.write("vcharon: closing a plugin failed:\n%s"
                                     % traceback.format_exc())
                    sys.stderr.flush()
                except Exception:
                    pass

    def ok(self, call_id, result):
        self.conn.send_json(proto.ok_msg(call_id, result))

    def err(self, call_id, error):
        self.conn.send_json(proto.err_msg(call_id, error))

    def log(self, msg, level="info"):
        self.conn.send_json(proto.log_msg(level, msg))

    def tick(self):
        now = time.monotonic()
        if self._last_tick is None or now - self._last_tick >= TICK_EVERY:
            self._last_tick = now
            self.conn.send_json(proto.TICK)


def hello():
    """DESIGN §7.3. VCHARON_TEST_OS stands in for the OS's name in it, as VCHARON_TEST_MACHINE_ID
    does for the id: tests on a Mac or Windows run their helper there, and the client takes
    only a Linux one (M11a). VCHARON_TEST_CLOCK_SHIFT (seconds) moves the clock, and
    VCHARON_TEST_UTC_OFFSET (seconds east of UTC) replaces the zone, for doctor's tests (M12b).
    VCHARON_TEST_OS_RELEASE names the file read in place of /etc/os-release (tests)."""
    offset = os.environ.get("VCHARON_TEST_UTC_OFFSET")
    test_release = os.environ.get("VCHARON_TEST_OS_RELEASE")
    release = platform.os_release((test_release,) if test_release else platform.OS_RELEASE)
    release = release or {}
    msg = {"t": "hello", "protocol": PROTOCOL, "version": VERSION,
           "python": platform.python_version(),
           "os": os.environ.get("VCHARON_TEST_OS") or platform.os_name(),
           "distro": release.get("PRETTY_NAME"), "distro_id": release.get("ID"),
           "distro_version": release.get("VERSION_ID"), "machine": platform.machine_id(),
           "user": platform.user(), "home": platform.home(),
           "utc_offset": int(offset) if offset else time.localtime().tm_gmtoff}
    # last, so the gap leaves out the time the other fields took
    msg["time"] = time.time() + float(os.environ.get("VCHARON_TEST_CLOCK_SHIFT") or 0)
    return msg


def echo(h, call_id, args):
    buf = io.BytesIO()

    def stage(index, fin):
        while True:
            data = fin.read(proto.CHUNK)
            if not data:
                return
            if buf.tell() + len(data) > ECHO_MAX:
                raise VCharonError("protocol", "an echo may carry at most %d bytes" % ECHO_MAX)
            buf.write(data)

    proto.receive_stream(h.conn, [0], stage)
    h.ok(call_id, {})
    buf.seek(0)
    proto.send_stream(h.conn, [(0, lambda: buf)])


def bye(h, call_id, args):
    h.ok(call_id, {})
    return STOP


# Checks for the args of the plugin calls.

def _is_str(value):
    return isinstance(value, str)


def _is_options(value):
    return (isinstance(value, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()))


def _is_state(value):
    return value is None or isinstance(value, dict)


def _is_bool(value):
    return isinstance(value, bool)


def _is_indexes(value):
    return isinstance(value, list) and all(proto.is_id(i) for i in value)


def _is_strings(value):
    return isinstance(value, list) and all(isinstance(s, str) for s in value)


def _is_any(value):
    return True


def _check_args(fn, args, checks):
    """Refuses args unless they have exactly the keys of checks, each passing its check."""
    if set(args) != set(checks) or not all(check(args[k]) for k, check in checks.items()):
        raise VCharonError("protocol", "a malformed %s call: %s" % (fn, proto.quote(args)))


def _once(h, fn):
    if fn in h.called:
        raise VCharonError("protocol", "a second %s call" % fn)
    h.called.add(fn)


def _out_of_order(fn, before):
    return VCharonError("protocol", "%s came before %s" % (fn, before))


def _is_role(value):
    return isinstance(value, str) and value in ("source", "sink")


def plugin_doctor(h, call_id, args):
    # Any number of times, and it sets neither h.source nor h.sink: vcharon doctor only reads.
    _check_args("plugin.doctor", args, {"plugin": _is_str, "role": _is_role,
                                        "options": _is_options})
    checks = plugin.doctor("remote", args["plugin"], args["role"], args["options"], h.ctx)
    h.ok(call_id, plugin.checks_to_json(checks))


def source_plan(h, call_id, args):
    _check_args("source.plan", args, {"plugin": _is_str, "options": _is_options,
                                      "state": _is_state, "full": _is_bool})
    _once(h, "source.plan")
    # Set before planning, so its handles close at exit even if planning fails.
    h.source = plugin.make("remote", args["plugin"], "source", args["options"], h.ctx)
    h.plan = h.source.plan(args["state"], full=args["full"])
    h.ok(call_id, plan.to_json(h.plan))


def source_send(h, call_id, args):
    _check_args("source.send", args, {"indexes": _is_indexes})
    if h.plan is None:
        raise _out_of_order("source.send", "source.plan")
    entries = h.plan.entries
    seen = set()
    for i in args["indexes"]:
        # Only planned file puts can move (DESIGN §7.4).
        if not 0 <= i < len(entries) or entries[i].op != "put" or entries[i].kind != "file":
            raise VCharonError("protocol", "entry %d isn't a file put of the plan" % i)
        if i in seen:
            raise VCharonError("protocol", "file %d was asked for twice" % i)
        seen.add(i)
    h.ok(call_id, {})
    proto.send_stream(h.conn, [(i, functools.partial(h.source.open, i))
                               for i in args["indexes"]])


def source_state_after(h, call_id, args):
    _check_args("source.state_after", args, {"written": _is_strings, "deleted": _is_strings})
    if h.plan is None:
        raise _out_of_order("source.state_after", "source.plan")
    _once(h, "source.state_after")
    h.ok(call_id, {"state": h.source.state_after(args["written"], args["deleted"])})


def sink_check(h, call_id, args):
    _check_args("sink.check", args, {"plugin": _is_str, "options": _is_options,
                                     "plan": _is_any})
    _once(h, "sink.check")
    p = plan.from_json(args["plan"])
    # Set before checking, so it closes at exit even if the check fails.
    h.sink = plugin.make("remote", args["plugin"], "sink", args["options"], h.ctx)
    h.ok(call_id, stage.checked_to_json(h.sink.check(p)))


def sink_receive(h, call_id, args):
    _check_args("sink.receive", args, {"indexes": _is_indexes})
    if h.sink is None:
        raise _out_of_order("sink.receive", "sink.check")
    proto.receive_stream(h.conn, args["indexes"], h.sink.stage)
    h.ok(call_id, {"staged": len(args["indexes"])})


def sink_commit(h, call_id, args):
    _check_args("sink.commit", args, {})
    if h.sink is None:
        raise _out_of_order("sink.commit", "sink.check")
    try:
        done = h.sink.commit()
    except Exception as e:
        if isinstance(e, VCharonError) and e.code == "protocol":
            # serve() stops the helper after it
            raise
        if not isinstance(e, VCharonError):
            e = VCharonError("internal", "%s: %s" % (type(e).__name__, e),
                             detail=traceback.format_exc())
        # The reply says what was done before the failure (decision 3 of the M3 plan).
        h.conn.send_json(proto.err_msg(call_id, e, done=stage.done_to_json(h.sink.done)))
        return None
    h.ok(call_id, stage.done_to_json(done))


def sink_abort(h, call_id, args):
    _check_args("sink.abort", args, {})
    if h.sink is not None:
        h.sink.abort()
    h.ok(call_id, {})


def job_reset(h, call_id, args):
    """Between two jobs on one connection (a sync of up and down): closes this job's plugins as the
    helper's exit does, so a sink that didn't commit drops its stage dir, then forgets them
    and the calls made, so the next job may plan and check again. Fine when nothing was
    planned."""
    _check_args("job.reset", args, {})
    h.close_plugins()
    h.source = h.plan = h.sink = None
    h.called = set()
    h.ok(call_id, {})


# The channel root's calls (DESIGN §14 M10). The root is never an argument: the helper uses the
# fixed root, or VCHARON_CHANNELS_ROOT in its environment (tests). Each function checks the names
# again and works only directly below the root.

def channel_list(h, call_id, args):
    _check_args("channel.list", args, {})
    h.ok(call_id, channels.list_channels(channels.root_path(), h.tick))


def channel_claim(h, call_id, args):
    _check_args("channel.claim", args, {"channel": _is_str, "name": _is_str,
                                        "create": _is_bool})
    h.ok(call_id, channels.claim(channels.root_path(), args["channel"], args["name"],
                                 args["create"], h.tick))


def channel_release(h, call_id, args):
    _check_args("channel.release", args, {"channel": _is_str, "name": _is_str})
    h.ok(call_id, channels.release(channels.root_path(), args["channel"], args["name"], h.tick))


def channel_remove(h, call_id, args):
    _check_args("channel.remove", args, {"channel": _is_str, "name": _is_str})
    h.ok(call_id, channels.remove(channels.root_path(), args["channel"], args["name"], h.tick))


# fn -> handler(h, call_id, args). A handler sends its own ok, since echo must send it between
# the upload and the download. Tests replace entries here.
HANDLERS = {"echo": echo, "bye": bye, "plugin.doctor": plugin_doctor,
            "source.plan": source_plan, "source.send": source_send,
            "source.state_after": source_state_after, "sink.check": sink_check,
            "sink.receive": sink_receive, "sink.commit": sink_commit, "sink.abort": sink_abort,
            "job.reset": job_reset, "channel.list": channel_list,
            "channel.claim": channel_claim, "channel.release": channel_release,
            "channel.remove": channel_remove}


def serve(h):
    """Answers calls until bye or end of file. Returns the helper's exit code."""
    while True:
        call_id = None
        try:
            frame = h.conn.read_frame()
            if frame is None:
                return 0
            kind, msg = frame
            if kind != proto.J or msg["t"] != "call" or not proto.is_id(msg.get("id")):
                what = proto.quote(msg) if kind == proto.J else "a %s frame" % chr(kind)
                raise VCharonError("protocol", "expected a call, got %s" % what)
            call_id = msg["id"]
            fn = msg.get("fn")
            args = msg.get("args", {})
            if not isinstance(fn, str) or not isinstance(args, dict):
                raise VCharonError("protocol", "a malformed call: %s" % proto.quote(msg))
            handler = HANDLERS.get(fn)
            if handler is None:
                raise VCharonError("protocol", "unknown fn %s" % proto.quote(fn))
            if handler(h, call_id, args) is STOP:
                return 0
        except VCharonError as e:
            if e.code == "lost":
                raise
            h.err(call_id, e)
            # After a protocol error the two sides may be out of step (DESIGN §7.2).
            if e.code == "protocol":
                return 3
        except Exception as e:
            h.err(call_id, VCharonError("internal", "%s: %s" % (type(e).__name__, e),
                                        detail=traceback.format_exc()))


def main(nonce):
    out = take_stdout()
    conn = HelperConn(sys.stdin.buffer, out)
    h = None
    try:
        conn.write_raw(("VCHARON-READY %s\n" % nonce).encode("ascii"))
        conn.send_json(hello())
        h = Helper(conn, sys.stdin.buffer)
        return serve(h)
    except VCharonError as e:
        if e.code != "lost":
            raise
        # The controller is gone; there's nobody to tell.
        return 1
    except BrokenPipeError:
        return 1
    finally:
        # Every way out: after bye, at end of file, after an error (decision 17 of the M3
        # plan). A sink that didn't commit removes its stage dir here.
        if h is not None:
            h.close_plugins()
        conn.close()
