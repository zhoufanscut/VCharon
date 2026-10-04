"""End to end through fake ssh: a session's handshake, calls and failures."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import time
import unittest

import vcharon
from vcharon import helper, platform, proto, ssh
from vcharon.proto import VCharonError

from tests.util import TEST_MACHINE_ID, FakeSshCase, helper_override, package_source

# echo handlers for helper_override; each runs inside the replaced vcharon.helper

# takes the upload, then waits for a byte that never comes
STUCK = """
from vcharon import proto

def echo(h, call_id, args):
    proto.receive_stream(h.conn, [0], lambda index, fin: fin.read())
    h.stdin.read(1)

_real.HANDLERS["echo"] = echo
"""

# busy for 1.5 s, calling the real h.tick(), which here may tick every 0.2 s
TICKING = """
import io
import time
from vcharon import proto

_real.TICK_EVERY = 0.2

def echo(h, call_id, args):
    buf = io.BytesIO()
    proto.receive_stream(h.conn, [0], lambda index, fin: buf.write(fin.read()))
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        h.tick()
        time.sleep(0.05)
    h.ok(call_id, {})
    buf.seek(0)
    proto.send_stream(h.conn, [(0, lambda: buf)])

_real.HANDLERS["echo"] = echo
"""

# writes 20 MiB of D frames before it reads its upload, and notes its pid
FLOODING = """
import os
from vcharon import proto

def echo(h, call_id, args):
    with open(os.path.join(os.path.expanduser("~"), "helper.pid"), "w") as f:
        f.write(str(os.getpid()))
    chunk = bytes(proto.MAX_DATA)
    for _ in range(80):
        h.conn.send_data(chunk)
    proto.receive_stream(h.conn, [0], lambda index, fin: fin.read())

_real.HANDLERS["echo"] = echo
"""

# prints to stdout three ways before the real echo
CHATTY = """
import os
import subprocess
import sys

real_echo = _real.HANDLERS["echo"]

def echo(h, call_id, args):
    print("junk-print")
    os.write(1, b"junk-fd1\\n")
    subprocess.run([sys.executable, "-c", "print('junk-child')"], stdin=subprocess.DEVNULL)
    return real_echo(h, call_id, args)

_real.HANDLERS["echo"] = echo
"""

# sends log and tick frames between the D frames of the download
INTERLEAVED = """
import io
from vcharon import proto

def echo(h, call_id, args):
    buf = io.BytesIO()
    proto.receive_stream(h.conn, [0], lambda index, fin: buf.write(fin.read()))
    data = buf.getvalue()
    h.ok(call_id, {})
    h.conn.send_json({"t": "file", "index": 0})
    for start in range(0, len(data), 100000):
        h.conn.send_data(data[start:start + 100000])
        h.log("between frames at %d" % start)
        h.conn.send_json(proto.TICK)
    h.conn.send_file_end()
    h.log("before the end", level="warn")
    h.conn.send_json({"t": "end"})

_real.HANDLERS["echo"] = echo
"""

# takes the upload, then dies
DYING = """
import os
from vcharon import proto

def echo(h, call_id, args):
    proto.receive_stream(h.conn, [0], lambda index, fin: fin.read())
    os._exit(9)

_real.HANDLERS["echo"] = echo
"""

# fails the way handlers do, with and without a code
FAILING = """
from vcharon.proto import VCharonError

def missing(h, call_id, args):
    raise VCharonError("not_found", "no such thing: %s" % args["name"], hint="look elsewhere")

def buggy(h, call_id, args):
    return 1 / 0

_real.HANDLERS["missing"] = missing
_real.HANDLERS["buggy"] = buggy
"""

# an error whose text holds a lone surrogate, as a non-UTF-8 file name gives
UNREADABLE = """
from vcharon.proto import VCharonError

def unreadable(h, call_id, args):
    raise VCharonError("permission", "can't read /srv/\\udcff.txt")

_real.HANDLERS["unreadable"] = unreadable
"""

# fails after its ok: before the download starts, and inside its first file
FAILS_AFTER_OK = """
def before(h, call_id, args):
    h.ok(call_id, {})
    return 1 / 0

def during(h, call_id, args):
    h.ok(call_id, {})
    h.conn.send_json({"t": "file", "index": 0})
    h.conn.send_data(b"partial")
    return 1 / 0

_real.HANDLERS["before"] = before
_real.HANDLERS["during"] = during
"""

# reports which finder comes first in the helper's sys.meta_path
PROBE = """
import sys

def probe(h, call_id, args):
    h.ok(call_id, {"first_finder": type(sys.meta_path[0]).__name__})

_real.HANDLERS["probe"] = probe
"""

# reports whether the helper holds a source, a sink or a plan
PLUGINS_SET = """
def plugins_set(h, call_id, args):
    h.ok(call_id, {"source": h.source is not None, "sink": h.sink is not None,
                   "plan": h.plan is not None})

_real.HANDLERS["plugins_set"] = plugins_set
"""


def payload():
    return bytes(range(256)) + os.urandom(1 << 20)


def gone(pid, seconds=5):
    """True once no process has this pid (POSIX)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class SessionTest(FakeSshCase):
    def assert_echoes(self, s):
        data = payload()
        self.assertEqual(s.echo(data), data)

    def failure(self, call, *args, **kwargs):
        with self.assertRaises(VCharonError) as cm:
            call(*args, **kwargs)
        return cm.exception

    # 1
    def test_start_up(self):
        argv_file = os.path.join(self.tmp, "argv.json")
        os.environ["FAKE_SSH_ARGV_FILE"] = argv_file
        s = self.session()
        hello = s.open()
        self.assertEqual(hello["t"], "hello")
        self.assertEqual((hello["protocol"], hello["version"]), (vcharon.PROTOCOL, vcharon.VERSION))
        self.assertEqual(hello["machine"], TEST_MACHINE_ID)
        self.assertEqual(hello["home"], self.home)
        self.assertEqual(hello["python"], platform.python_version())
        # fake_ssh says linux, as a real server is
        self.assertEqual(hello["os"], "linux")
        self.assertIsInstance(hello["user"], str)
        # the server's clock and zone; the fake server is this machine
        self.assertEqual(hello["utc_offset"], time.localtime().tm_gmtoff)
        self.assertLessEqual(hello["time"], s.hello_received)
        self.assertAlmostEqual(s.clock_gap, hello["time"] - s.hello_received)
        self.assertLess(abs(s.clock_gap), 5)
        self.assertRegex(self.log_text(), r"hello from fake-dest, clock [+-]\d+\.\d s")
        self.assertIs(s.hello, hello)
        self.assertEqual(s.junk_bytes, 0)
        self.assertGreater(s.handshake_seconds, 0)
        self.assert_echoes(s)
        # a second call on the same connection
        self.assertEqual(s.echo(b""), b"")
        s.close()
        self.assertEqual(s.ssh_exit, 0)
        s.close()
        with open(argv_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f), ssh.ssh_command(s.settings, "fake-dest")[2:])
        self.assertIn("ssh exited with code 0", self.log_text())

    # 2
    def test_shell_text_before_the_marker(self):
        head = (b"motd-start\nVCHARON-READY " + b"f" * 32 + b"\n" + bytes(range(256))
                + b"\n")
        junk = head + b"." * (4096 - len(head)) + b"after-4k"
        junk += b"-" * (5000 - len(junk))
        os.environ["FAKE_SSH_JUNK_B64"] = base64.b64encode(junk).decode()
        s = self.session()
        s.open()
        self.assert_echoes(s)
        self.assertEqual(s.junk_bytes, 5000)
        log = self.log_text()
        self.assertIn("the server's shell printed 5000 bytes before vcharon started:", log)
        self.assertIn("| motd-start", log)
        self.assertIn("VCHARON-READY " + "f" * 32, log)
        self.assertIn("....", log)
        self.assertNotIn("after-4k", log)

    # 3
    def test_files_in_the_home_dont_shadow_vcharon(self):
        flag = os.path.join(self.tmp, "SHADOWED")
        evil = "open(%r, 'w').close()\nimport os\nos._exit(66)\n" % flag
        os.mkdir(os.path.join(self.home, "vcharon"))
        for rel in ("vcharon/__init__.py", "vcharon/helper.py", "base64.py"):
            with open(os.path.join(self.home, rel), "w") as f:
                f.write(evil)
        # The trap works on a Python without -I...
        subprocess.run([sys.executable, "-c", "import base64"], cwd=self.home,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        self.assertTrue(os.path.exists(flag))
        os.remove(flag)
        # ...but not on the helper's.
        s = self.session()
        s.open()
        self.assert_echoes(s)
        s.close()
        self.assertEqual(s.ssh_exit, 0)
        self.assertFalse(os.path.exists(flag))

    # 4
    def test_python_too_old(self):
        s = self.session(floor=(99, 0))
        err = self.failure(s.open)
        self.assertEqual((err.code, err.exit_code), ("connect", 4))
        # the floor is checked before any of vcharon's modules is compiled
        major, minor = sys.version_info[:2]
        self.assertEqual(err.message, "the server's python3 is %d.%d; vcharon needs 3.13 or "
                         "later" % (major, minor))
        self.assertEqual(err.hint, "install python3 3.13 or later on fake-dest (Debian 13's is "
                         "3.13), or point remote_python in vcharon.ini at one")
        self.assertTrue(any("vcharon needs 99.0 or later" in line for line in err.tail), err.tail)

    # 5
    def test_exits_before_the_marker(self):
        # one exit end to end; test_ssh's classify table has each code's message and hint
        os.environ["FAKE_SSH_EXIT"] = "1"
        os.environ["FAKE_SSH_STDERR"] = "boom"
        s = self.session()
        err = self.failure(s.open)
        self.assertEqual((err.code, err.exit_code), ("connect", 4))
        self.assertIn("ssh exited with code 1 before vcharon started", err.message)
        self.assertIn("a shell startup file", err.hint)
        self.assertEqual(err.tail, ["boom"])
        self.assertIsNone(s.hello)

    # 6
    def test_handshake_timeout(self):
        os.environ["FAKE_SSH_STALL"] = "30"
        # The bundle is too big for the pipe, so writing it blocks; a small one is
        # test_doctor's no-marker test.
        padding = base64.b64encode(os.urandom(1536 * 1024)).decode()
        s = self.session(extra_modules={"vcharon.padding": "DATA = %r\n" % padding},
                         handshake_timeout=1)
        started = time.monotonic()
        err = self.failure(s.open)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((err.code, err.exit_code), ("connect", 4))
        self.assertEqual(err.message, "no answer from vcharon on fake-dest within 1 s")
        self.assertIn("authentication or a jump host may be stuck", err.hint)

    # 7
    def test_idle_timeout(self):
        s = self.session(extra_modules=helper_override(STUCK), idle_timeout=1)
        s.open()
        started = time.monotonic()
        err = self.failure(s.echo, b"hello")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((err.code, err.exit_code), ("timeout", 1))
        self.assertEqual(err.message, "nothing moved for 1 s")
        self.assertIn("raise idle_timeout", err.hint)
        # fake ssh is gone
        self.assertIsNotNone(s._proc.wait(2))

    # 8
    def test_run_timeout(self):
        s = self.session(extra_modules=helper_override(STUCK), idle_timeout=60, run_timeout=2)
        s.open()
        started = time.monotonic()
        err = self.failure(s.echo, b"hello")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((err.code, err.exit_code), ("timeout", 1))
        self.assertEqual(err.message, "the run passed run_timeout (2 s)")

    def test_timeout_hint_at_the_cap(self):
        # a setting already at the cap can't be raised, so the hint doesn't say to
        from vcharon.config import MAX_TIMEOUT
        s = self.session(idle_timeout=MAX_TIMEOUT, run_timeout=MAX_TIMEOUT)
        s._killed = "idle"
        self.assertEqual(s._end_error().hint, "check the network")
        s._killed = "run"
        self.assertEqual(s._end_error().hint, "see the log, then run again")
        s = self.session(idle_timeout=MAX_TIMEOUT - 1, run_timeout=MAX_TIMEOUT - 1)
        s._killed = "run"
        self.assertEqual(s._end_error().hint, "raise run_timeout")

    def test_run_timeout_stops_between_rounds(self):
        # sync --repeat's wait between rounds: past run_timeout, and not killed
        s = self.session(idle_timeout=60, run_timeout=2)
        s.open()
        s.end_round()
        time.sleep(2.5)
        self.assert_echoes(s)
        s.start_round()
        self.assert_echoes(s)
        self.assertNotIn("killing ssh", self.log_text())

    # 9
    def test_ticks_keep_a_busy_helper_alive(self):
        s = self.session(extra_modules=helper_override(TICKING), idle_timeout=1)
        s.open()
        started = time.monotonic()
        self.assert_echoes(s)
        self.assertGreater(time.monotonic() - started, 1.4)

    # 10
    def test_local_work_isnt_idle(self):
        s = self.session(idle_timeout=1)
        s.open()
        time.sleep(1.5)
        self.assert_echoes(s)

    def test_slow_local_stage_isnt_idle(self):
        s = self.session(idle_timeout=1)
        s.open()
        data = payload()
        got = []

        def slow_stage(index, fin):
            parts = []
            while True:
                chunk = fin.read(proto.CHUNK)
                if not chunk:
                    break
                parts.append(chunk)
                # five D frames: 1.5 s in all, with the helper long done sending
                time.sleep(0.3)
            got.append(b"".join(parts))

        s.call("echo", upload=[(0, lambda: io.BytesIO(data))], receive=([0], slow_stage))
        self.assertEqual(got, [data])
        self.assert_echoes(s)
        self.assertNotIn("killing ssh", self.log_text())

    def test_both_sides_blocked_is_idle(self):
        # The helper writes without reading while the controller writes without reading: the
        # queue fills, then both pipes. Nothing moves, so the idle clock must fire.
        s = self.session(extra_modules=helper_override(FLOODING), idle_timeout=1)
        s.open()
        started = time.monotonic()
        err = self.failure(s.echo, bytes(8 << 20))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((err.code, err.message), ("timeout", "nothing moved for 1 s"))
        self.assertIsNotNone(s._proc.wait(2))
        if os.name == "posix":
            with open(os.path.join(self.home, "helper.pid")) as f:
                self.assertTrue(gone(int(f.read())), "the helper is still running")

    # 11
    def test_child_output_stays_out_of_the_protocol(self):
        s = self.session(extra_modules=helper_override(CHATTY))
        s.open()
        self.assert_echoes(s)
        s.close()
        self.assertEqual(s.ssh_exit, 0)
        log = self.log_text()
        for line in ("stderr: junk-print", "stderr: junk-fd1", "stderr: junk-child"):
            self.assertIn(line, log)

    # 12
    def test_log_and_tick_frames_inside_a_download(self):
        s = self.session(extra_modules=helper_override(INTERLEAVED))
        s.open()
        self.assert_echoes(s)
        log = self.log_text()
        self.assertIn("  info  helper: between frames at 0", log)
        self.assertIn("  info  helper: between frames at 1000000", log)
        self.assertIn("  warn  helper: before the end", log)

    # 13
    def test_connection_lost(self):
        s = self.session(extra_modules=helper_override(DYING))
        s.open()
        err = self.failure(s.echo, b"x" * 1000)
        self.assertEqual((err.code, err.exit_code), ("lost", 1))
        self.assertEqual(err.message,
                         "the connection to fake-dest closed unexpectedly (ssh exit 9)")
        self.assertIn("see the log", err.hint)
        # the session is done for
        self.assertEqual(self.failure(s.echo, b"x").code, "internal")

    # 14
    def test_unknown_fn(self):
        s = self.session()
        s.open()
        err = self.failure(s.call, "nope")
        self.assertEqual((err.code, err.exit_code), ("protocol", 1))
        self.assertIn("nope", err.message)
        s.close()
        # the helper stops after a protocol error
        self.assertEqual(s.ssh_exit, 3)

    # 15
    def test_hello_mismatch(self):
        source = package_source("__init__.py")
        line = 'VERSION = "%s"' % vcharon.VERSION
        self.assertIn(line, source)
        s = self.session(extra_modules={"vcharon": source.replace(line, 'VERSION = "9.9.9"')})
        err = self.failure(s.open)
        self.assertEqual(err.code, "protocol")
        self.assertIn("9.9.9", err.message)
        self.assertIn("bundling", err.hint)

    # 16
    def test_echo_over_16_mib(self):
        s = self.session()
        s.open()
        err = self.failure(s.echo, bytes(helper.ECHO_MAX + 1))
        self.assertEqual(err.code, "protocol")
        self.assertIn("at most %d bytes" % helper.ECHO_MAX, err.message)

    def test_bundled_code_that_wont_import(self):
        s = self.session(extra_modules={"vcharon.helper": "def main(nonce):\n    return (\n"})
        err = self.failure(s.open)
        self.assertEqual((err.code, err.exit_code), ("connect", 4))
        self.assertEqual(err.message, "the server couldn't load vcharon's code")
        self.assertIn("bug in vcharon", err.hint)
        self.assertTrue(any("SyntaxError" in line for line in err.tail), err.tail)

    def test_error_text_that_isnt_utf8(self):
        s = self.session(extra_modules=helper_override(UNREADABLE))
        s.open()
        err = self.failure(s.call, "unreadable")
        self.assertEqual((err.code, err.message), ("permission", "can't read /srv/\\udcff.txt"))
        self.assert_echoes(s)

    def test_helper_fails_after_its_ok(self):
        s = self.session(extra_modules=helper_override(FAILS_AFTER_OK))
        s.open()
        for fn in ("before", "during"):
            with self.subTest(fn=fn):
                err = self.failure(s.call, fn, receive=([0], lambda index, fin: fin.read()))
                self.assertEqual(err.code, "internal")
                self.assertIn("ZeroDivisionError", err.message)
                self.assertIn("Traceback (most recent call last)", err.detail)
                self.assertIn("return 1 / 0", err.detail)
        # both sides are still in step
        self.assert_echoes(s)

    def test_bundle_finder_comes_first(self):
        s = self.session(extra_modules=helper_override(PROBE))
        s.open()
        self.assertEqual(s.call("probe"), {"first_finder": "Finder"})

    def test_handler_errors_keep_the_session(self):
        s = self.session(extra_modules=helper_override(FAILING))
        self.assertFalse(s.usable)
        s.open()
        self.assertTrue(s.usable)
        err = self.failure(s.call, "missing", {"name": "x.txt"})
        self.assertEqual((err.code, err.message, err.hint),
                         ("not_found", "no such thing: x.txt", "look elsewhere"))
        # the whole err message, for extra keys such as a failed commit's done
        self.assertEqual(err.reply["t"], "err")
        self.assertEqual(err.reply["error"]["code"], "not_found")
        self.assertTrue(s.usable)
        err = self.failure(s.call, "buggy")
        self.assertEqual(err.code, "internal")
        self.assertIn("ZeroDivisionError", err.message)
        # the traceback shows the bundled source
        self.assertIn("vcharon-bundle/vcharon/helper.py", err.detail)
        self.assertIn("return 1 / 0", err.detail)
        self.assert_echoes(s)
        self.assertTrue(s.usable)
        # a handler's error isn't the helper's: it still ends cleanly at bye
        s.close()
        self.assertEqual(s.ssh_exit, 0)
        # not a call: the helper answers with "id": null and stops, out of step
        s = self.session()
        s.open()
        s.send_json({"t": "bogus"})
        err = self.failure(s.echo, b"x")
        self.assertEqual(err.code, "protocol")
        self.assertIn("bogus", err.message)
        self.assertFalse(s.usable)
        self.assertEqual(self.failure(s.echo, b"x").code, "internal")
        s.close()
        self.assertFalse(s.usable)
        self.assertEqual(s.ssh_exit, 3)

    def test_plugin_doctor(self):
        """The helper's plugin.doctor."""
        os.mkdir(os.path.join(self.home, "outbox"))
        s = self.session()
        s.open()
        args = {"plugin": "path", "role": "source", "options": {"path": "outbox"}}
        want = {"checks": [["ok", "from.path %s: a directory"
                            % os.path.join(self.home, "outbox"), None]]}
        # any number of times
        self.assertEqual(s.call("plugin.doctor", args), want)
        self.assertEqual(s.call("plugin.doctor", {"plugin": "dir", "role": "sink",
                                                  "options": {"path": "inbox"}})["checks"][0][0],
                         "FAIL")
        self.assertEqual(s.call("plugin.doctor", args), want)
        # it set no source: planning still works
        plan = s.call("source.plan", {"plugin": "path", "options": {"path": "outbox"},
                                      "state": None, "full": False})
        self.assertEqual(plan["entries"], [])
        err = self.failure(s.call, "plugin.doctor", {"plugin": "path", "role": "source",
                                                     "options": {"nope": "x"}})
        self.assertEqual(err.code, "bad_options")
        self.assertTrue(s.usable)
        s.close()
        self.assertEqual(s.ssh_exit, 0)

    def test_plugin_doctor_leaves_source_and_sink_unset(self):
        # pins h.source and h.sink themselves, not just the calls that read them
        os.mkdir(os.path.join(self.home, "outbox"))
        s = self.session(extra_modules=helper_override(PLUGINS_SET))
        s.open()
        s.call("plugin.doctor", {"plugin": "path", "role": "source",
                                 "options": {"path": "outbox"}})
        s.call("plugin.doctor", {"plugin": "dir", "role": "sink",
                                 "options": {"path": "outbox"}})
        self.assertEqual(s.call("plugins_set"), {"source": False, "sink": False, "plan": False})
        s.close()

    def test_plugin_doctor_malformed(self):
        good = {"plugin": "path", "role": "source", "options": {"path": "x"}}
        for bad in (dict(good, role="both"), dict(good, options={"path": 1}),
                    dict(good, extra=1), {"plugin": "path", "role": "source"}):
            with self.subTest(args=bad):
                s = self.session()
                s.open()
                err = self.failure(s.call, "plugin.doctor", bad)
                self.assertEqual(err.code, "protocol")
                self.assertIn("a malformed plugin.doctor call", err.message)
                s.close()


if __name__ == "__main__":
    unittest.main()
