"""The client's end of a session: start ssh, send the bundle, read frames, watch the clock."""

from __future__ import annotations

import collections
import contextlib
import io
import ntpath
import os
import posixpath
import re
import subprocess
import threading
import time
import traceback

from . import FLOOR, PROTOCOL, VERSION, bundle, fsops, platform, proto
from .log import LEVELS
from .proto import VCharonError

# The reader thread stops reading while this many bytes wait in the queue (DESIGN §6.3).
QUEUE_BYTES = 16 << 20
# Writes to ssh go in pieces this big, so the idle clock sees progress on a slow link.
WRITE_CHUNK = 256 << 10
# How much of the server shell's output before the marker goes to the log.
JUNK_KEEP = 4096

_NETWORK = ("Connection refused", "timed out", "Could not resolve", "No route to host",
            "Network is unreachable")

# queue items besides frames
_EOF = object()
_EMPTY = object()


def check_dest(dest):
    """Refuses a destination ssh would misread. vcharon never parses it otherwise."""
    hint = "use an ~/.ssh/config alias, user@host, or ssh://user@host:port"
    if not dest:
        raise VCharonError("config", "the destination is empty", hint=hint)
    if dest.startswith("-"):
        raise VCharonError("config", "the destination %r starts with '-', which ssh would read as "
                           "an option" % dest, hint=hint)
    if any(c.isspace() or ord(c) < 32 or 127 <= ord(c) < 160 for c in dest):
        raise VCharonError("config", "the destination %r has whitespace or control characters"
                           % dest, hint=hint)


def ssh_prefix(settings):
    # Tests replace this with [python, fake_ssh.py].
    return [settings.ssh_path]


def ssh_add_prefix(settings):
    """[<ssh-add>]: the one next to ssh_path, never one found on PATH (launch rules, DESIGN
    §13). Tests replace it with [python, fake_ssh_add.py]."""
    path = settings.ssh_path
    mod = ntpath if platform.os_name() == "windows" else posixpath
    folder, name = mod.split(path)
    return [mod.join(folder, "ssh-add.exe" if name.lower().endswith(".exe") else "ssh-add")]


def ssh_command(settings, dest, probe=False):
    """The argument list of DESIGN §6.1. Never a shell string."""
    argv = ssh_prefix(settings) + [
        "-T", "-e", "none",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=%d" % settings.connect_timeout,
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    if probe or platform.os_name() == "windows":
        argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    if settings.compress:
        argv.append("-C")
    argv += [dest, bundle.remote_command(settings.remote_python)]
    return argv


# the loader's refusal of an old Python (bundle._LOADER)
_OLD_PYTHON = re.compile(r"the server's Python is ([0-9]+\.[0-9]+);")


def classify_exit(rc, tail, dest, settings, killed=None):
    """Why ssh ended before the ready marker (DESIGN §6.4). Always `connect`."""
    text = "\n".join(tail)
    if killed:
        seconds = {"handshake": settings.handshake_timeout, "idle": settings.idle_timeout,
                   "run": settings.run_timeout}.get(killed, settings.handshake_timeout)
        err = VCharonError("connect", "no answer from vcharon on %s within %d s" % (dest, seconds),
                           hint="authentication or a jump host may be stuck; run ssh %s in a "
                           "terminal to see" % dest)
    elif rc == 90:
        # the loader's line says which version it found
        found = _OLD_PYTHON.search(text)
        err = VCharonError("connect", "the server's %s is %s; vcharon needs %d.%d or later"
                           % (settings.remote_python, found.group(1) if found else "too old",
                              FLOOR[0], FLOOR[1]),
                           hint="install python3 %d.%d or later on %s (Debian 13's is 3.13), "
                           "or point remote_python in vcharon.ini at one"
                           % (FLOOR[0], FLOOR[1], dest))
    elif rc == 91:
        err = VCharonError("connect", "the server couldn't load vcharon's code",
                           hint="this is a bug in vcharon; see the log")
    elif rc == 126:
        err = VCharonError("connect", "%s isn't runnable on %s" % (settings.remote_python, dest),
                           hint="check remote_python in vcharon.ini")
    elif rc == 127:
        err = VCharonError("connect", "%s wasn't found on %s" % (settings.remote_python, dest),
                           hint="install %s on %s, or set remote_python in vcharon.ini"
                           % (settings.remote_python, dest))
    elif rc == 255 and "Host key verification failed" in text:
        err = VCharonError("connect", "ssh couldn't verify the host key of %s" % dest,
                           hint="run ssh %s once in a terminal" % dest)
    elif rc == 255 and "Permission denied" in text:
        err = VCharonError("connect", "ssh couldn't log in to %s (Permission denied)" % dest,
                           hint="add your key to the server, or run: vcharon key %s" % dest)
    elif rc == 255 and any(phrase in text for phrase in _NETWORK):
        phrase = next(phrase for phrase in _NETWORK if phrase in text)
        err = VCharonError("connect", "ssh couldn't reach %s (%s)" % (dest, phrase),
                           hint="check the host, port, VPN")
    else:
        if rc is not None and rc < 0:
            what = "ssh was killed by signal %d" % -rc
        else:
            what = "ssh exited with code %s" % rc
        if rc == 255:
            # ssh's own failure; the stderr tail says what it was.
            hint = "see ssh's messages above"
        else:
            # A startup file that eats stdin garbles the bootstrap, which then exits 1.
            hint = "a shell startup file on the server may have read stdin; see the log"
        err = VCharonError("connect", "%s before vcharon started on the server" % what, hint=hint)
    err.tail = list(tail)
    return err


START_HINT = "install the OpenSSH client, or set ssh_path in vcharon.ini"


# --- the -v probe of vcharon doctor and vcharon key (DESIGN §6.4, §6.5) ---

def probe_command(settings, dest):
    """A probe's ssh command with -v: BatchMode on, ControlMaster off, and the bootstrap line
    as the remote command. With stdin at the null device the bootstrap reads an empty line,
    runs nothing and exits 0."""
    argv = ssh_command(settings, dest, probe=True)
    at = len(ssh_prefix(settings))
    return argv[:at] + ["-v"] + argv[at:]


# rc: ssh's exit code, None when the timeout killed it; lines: its stderr, one string per
# line; accepted: an AcceptedKey for each "Server accepts key" line, in order; error: the
# VCharonError a session would raise for this exit (DESIGN §6.4), None when rc is 0.
Probe = collections.namedtuple("Probe", "rc lines accepted error")

# ident: the key file, or the agent's comment for the key, exactly as ssh printed it;
# type: RSA, ED25519, ... or ""; agent: the agent holds the key.
AcceptedKey = collections.namedtuple("AcceptedKey", "ident type agent")

# Only ssh's own debug line counts, from its start: the server can't print one (review W2).
_ACCEPTS = "debug1: Server accepts key: "
# After this line, stderr can carry the server's own text.
_AUTHENTICATED = "Authenticated to "
_FINGERPRINTS = ("SHA256:", "MD5:")
_OCTAL = "01234567"


def unvis(text):
    """The text ssh's log escaped as strnvis(VIS_SAFE | VIS_OCTAL) does, decoded: \\\\ is a
    backslash, \\ and 3 octal digits a byte, anything else itself (as UTF-8); a backslash not
    followed by either stays. The bytes are then read as UTF-8, with surrogateescape."""
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            if text[i + 1:i + 2] == "\\":
                out.append(0x5C)
                i += 2
                continue
            digits = text[i + 1:i + 4]
            if len(digits) == 3 and all(d in _OCTAL for d in digits) and int(digits, 8) < 256:
                out.append(int(digits, 8))
                i += 4
                continue
        out += c.encode("utf-8", "surrogateescape")
        i += 1
    return out.decode("utf-8", "surrogateescape")


def shown(text):
    """text as output and logs show it: a byte that isn't UTF-8, which unvis() or the OS kept
    as a surrogate, becomes \\xNN, since a real UTF-8 stdout refuses surrogates (review N1).
    Only for showing: files and ssh-add get text itself."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def parse_accepted(line):
    """The AcceptedKey of an ssh -v line that starts with "debug1: Server accepts key: ",
    else None. OpenSSH prints <file or agent comment> <type> <fingerprint>[ explicit]
    [ token| authenticator][ agent] there, with the file or comment escaped (unvis)."""
    if not line.startswith(_ACCEPTS):
        return None
    rest = line[len(_ACCEPTS):]
    agent = False
    # each at most once, from the right
    for suffix in (" agent", " token", " authenticator", " explicit"):
        if rest.endswith(suffix):
            rest = rest[:-len(suffix)]
            if suffix == " agent":
                agent = True
    parts = rest.rsplit(" ", 2)
    if len(parts) == 3 and parts[2].startswith(_FINGERPRINTS):
        return AcceptedKey(unvis(parts[0]), parts[1], agent)
    return AcceptedKey(unvis(rest), "", agent)


def verbose_probe(settings, dest, log):
    """ssh -v through fsops.run, with the budget a session has for its ready marker; a
    Probe. Only ssh's own messages, never its debug lines, are classified."""
    argv = probe_command(settings, dest)
    log.info("probe: %r" % (argv,))
    try:
        ran = fsops.run(argv, timeout=settings.handshake_timeout, new_session=os.name == "posix")
    except OSError as e:
        raise VCharonError("connect", "couldn't start %s: %s" % (argv[0], e.strerror or e),
                           hint=START_HINT)
    lines = ran.err.decode("utf-8", "replace").replace("\r", "").split("\n")
    if lines and not lines[-1]:
        lines.pop()
    for line in lines:
        log.debug("probe stderr: %s" % line)
    log.info("probe: ssh exited with code %s" % ran.rc)
    accepted = []
    for line in lines:
        if line.startswith(_AUTHENTICATED):
            break
        key = parse_accepted(line)
        if key is not None:
            accepted.append(key)
    error = None
    if ran.rc != 0:
        own = [line for line in lines if not line.startswith("debug")]
        error = classify_exit(ran.rc, own[-20:], dest, settings,
                              killed="handshake" if ran.rc is None else None)
    return Probe(ran.rc, lines, accepted, error)


def denied(probe):
    """True when ssh itself failed (255, as classify_exit asks) and its own messages, not its
    debug lines, say Permission denied. bash's "Permission denied" for a remote_python it
    can't run exits 126 (review B1)."""
    return probe.rc == 255 and any("Permission denied" in line for line in probe.lines
                                   if not line.startswith("debug"))


def _key_file(ident):
    """True when ident names a key file here: an absolute path, so a name the server made up
    can't point at a file in the current directory (review W2)."""
    return os.path.isabs(ident) and os.path.isfile(ident)


def probe_key(probe):
    """The key the probe is about: after a login, the last accepted key (the one that logged
    in); after a failure, the first accepted key that is a file here and not the agent's (one
    ssh found but couldn't unlock), or else the first accepted key; None if none."""
    if not probe.accepted:
        return None
    if probe.rc == 0:
        return probe.accepted[-1]
    for key in probe.accepted:
        if not key.agent and _key_file(key.ident):
            return key
    return probe.accepted[0]


def key_kind(key):
    """"file": ssh read the key from a file here; "file+agent": a file here that the agent
    also holds, unlocked; "agent": only the agent holds it, with no file here (an agent
    forwarded by the ssh login); "other": anything else."""
    is_file = _key_file(key.ident)
    if is_file:
        return "file+agent" if key.agent else "file"
    return "agent" if key.agent else "other"


def clock_gap(hello, received):
    """The server's clock minus this machine's, in seconds, + when the server is ahead (M12b):
    the hello's time against `received`, this machine's time.time() when the hello was read.
    So it is the true gap minus the hello's one-way delay. None without a time in the hello."""
    t = hello.get("time")
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        return None
    return t - received


class _FrameQueue:
    """Frames from the reader thread to the main thread, bounded by bytes."""

    def __init__(self, limit):
        self._limit = limit
        self._items = collections.deque()
        self._used = 0
        self._cond = threading.Condition()
        self.closed = False

    def put(self, item, size=0):
        """Adds an item, waiting for room; one bigger than the limit still passes when the queue
        is empty."""
        with self._cond:
            while self._items and self._used + size > self._limit and not self.closed:
                self._cond.wait()
            if not self.closed:
                self._items.append((item, size))
                self._used += size
                self._cond.notify_all()

    def get(self, timeout):
        """The next item, or _EMPTY if none came within `timeout` seconds."""
        with self._cond:
            if not self._items:
                self._cond.wait(timeout)
                if not self._items:
                    return _EMPTY
            item, size = self._items.popleft()
            self._used -= size
            self._cond.notify_all()
            return item

    def close(self):
        """Drops what's queued and wakes a blocked put()."""
        with self._cond:
            self.closed = True
            self._items.clear()
            self._used = 0
            self._cond.notify_all()


class _Stdout:
    """ssh's stdout for the reader thread, after the marker. Every read marks activity."""

    def __init__(self, raw, rest, touch):
        self._raw = raw
        self._buf = rest
        self._pos = 0
        self._touch = touch

    def read_exact(self, n):
        avail = len(self._buf) - self._pos
        if avail >= n:
            data = self._buf[self._pos:self._pos + n]
            self._pos += n
            return data
        parts = [self._buf[self._pos:]] if avail else []
        got = avail
        self._buf, self._pos = b"", 0
        while got < n:
            chunk = self._raw.read(max(n - got, 65536))
            self._touch()
            if not chunk:
                break
            if got + len(chunk) > n:
                # keep the rest for the next frame
                self._buf, self._pos = chunk, n - got
                chunk = chunk[:n - got]
            parts.append(chunk)
            got += len(chunk)
        return b"".join(parts)


class Session:
    """One ssh connection with vcharon's helper at the other end (DESIGN §6)."""

    def __init__(self, settings, dest, log, probe=False, floor=FLOOR, extra_modules=None):
        self.settings = settings
        self.dest = dest
        self.log = log
        # where the helper's relayed lines go: the job the helper works for now, which the
        # controller sets for each later job on a shared connection (DESIGN §13)
        self.helper_log = log
        self.probe = probe
        self.floor = floor
        self.extra_modules = extra_modules
        self.hello = None
        self.junk_bytes = 0
        self.ssh_exit = None
        self.handshake_seconds = None
        # time.time() when the hello was read, and the server's clock minus it (M12b)
        self.hello_received = None
        self.clock_gap = None
        self._nonce = os.urandom(16).hex()
        self._proc = None
        self._queue = _FrameQueue(QUEUE_BYTES)
        self._lock = threading.Lock()
        self._tail = collections.deque(maxlen=20)
        self._junk_head = b""
        # set once the reader has seen the marker, or has stopped before it
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._marker_seen = False
        self._reader_done = False
        self._started = 0.0
        # where run_timeout counts from: the start, or with vcharon sync --repeat the round's
        # start; None between rounds, when it doesn't count (start_round, end_round)
        self._run_from = None
        self._activity = 0.0
        # > 0 while the main thread is blocked on the helper; only then does the idle clock run
        self._waiting = 0
        self._killed = None
        self._ended = None
        self._healthy = True
        self._closed = False
        self._next_id = 1
        self._stdout_thread = self._stderr_thread = self._watchdog = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and not issubclass(exc_type, Exception):
            # Ctrl-C: no bye, just stop ssh.
            self._healthy = False
            self.kill("interrupted")
        self.close()

    # -- start-up ------------------------------------------------------------------------

    def open(self):
        """Starts ssh and the helper. Returns the hello."""
        if self._proc is not None or self._closed:
            raise VCharonError("internal", "a session can be opened only once")
        loader = bundle.loader_line(self.floor)
        blob = bundle.build(self._nonce, self.extra_modules)
        argv = ssh_command(self.settings, self.dest, probe=self.probe)
        self.log.info("starting ssh: %r" % (argv,))
        # Without a controlling terminal ssh can't prompt even where BatchMode doesn't reach,
        # so a terminal run behaves like a hotkey run. Ctrl-C still reaches us.
        extra = {"start_new_session": True} if os.name == "posix" else {}
        try:
            self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, bufsize=0, **extra)
        except OSError as e:
            self._closed = True
            raise VCharonError("connect", "couldn't start %s: %s" % (argv[0], e.strerror or e),
                               hint="install the OpenSSH client, or set ssh_path in vcharon.ini")
        try:
            return self._start(loader, blob)
        except BaseException as e:
            self._healthy = False
            if not isinstance(e, Exception):
                self.kill("interrupted")
            self.close()
            raise

    def _start(self, loader, blob):
        self._started = self._activity = time.monotonic()
        with self._lock:
            # a round that opens the session counts from its start (start_round)
            if self._run_from is None:
                self._run_from = self._started
        self._stdout_thread = self._thread(self._read_stdout)
        self._stderr_thread = self._thread(self._read_stderr)
        self._watchdog = self._thread(self._watch)
        self.log.debug("sending the loader (%d bytes) and the bundle (%d bytes)"
                       % (len(loader), len(blob)))
        try:
            self._write(loader)
            self._write(blob)
        except OSError as e:
            # ssh died or was killed; the reader tells why.
            self.log.debug("writing the bundle stopped: %s" % e)
        self._wait_ready()
        if not self._marker_seen:
            killed = self._killed
            rc = self._wait_exit(5)
            self._stderr_thread.join(2)
            raise classify_exit(rc, self._tail_lines(), self.dest, self.settings, killed=killed)
        kind, msg = self._next()
        # the clock gap's other end (M12b): as soon as the hello is read
        received = time.time()
        if (kind != proto.J or msg["t"] != "hello" or msg.get("protocol") != PROTOCOL
                or msg.get("version") != VERSION):
            self._healthy = False
            got = proto.quote(msg) if kind == proto.J else "a %s frame" % chr(kind)
            raise VCharonError("protocol",
                               "the helper on %s isn't vcharon %s, protocol %d: it sent %s"
                               % (self.dest, VERSION, PROTOCOL, got),
                               hint="this is a bug in vcharon's bundling")
        self.hello = msg
        self.handshake_seconds = time.monotonic() - self._started
        self.hello_received = received
        self.clock_gap = clock_gap(msg, received)
        gap = "" if self.clock_gap is None else ", clock %+.1f s" % self.clock_gap
        self.log.info("hello from %s%s: %s" % (self.dest, gap, proto.quote(msg, limit=1000)))
        return msg

    def _thread(self, target):
        thread = threading.Thread(target=target, name="vcharon" + target.__name__, daemon=True)
        thread.start()
        return thread

    def _wait_ready(self):
        exited = None
        while not self._ready.wait(0.1):
            if self._proc.poll() is not None:
                # A leftover child of ssh may hold the pipe open; don't wait for it long.
                exited = exited or time.monotonic()
                if time.monotonic() - exited >= 2:
                    return

    def _wait_exit(self, seconds):
        """ssh's exit code, killing it if it doesn't exit within `seconds`."""
        try:
            return self._proc.wait(seconds)
        except subprocess.TimeoutExpired:
            self.log.warn("ssh didn't exit within %d s; killing it" % seconds)
            self._proc.kill()
            return self._proc.wait()

    # -- threads -------------------------------------------------------------------------

    def _touch(self):
        self._activity = time.monotonic()

    def _read_stdout(self):
        try:
            rest = self._find_marker()
            if rest is not None:
                self._read_frames(rest)
        except Exception as e:
            # A bug here must not leave the main thread waiting forever.
            self.log.error("the stdout reader failed:\n%s" % traceback.format_exc())
            self._queue.put(VCharonError("internal", "the stdout reader failed: %r" % e))
        finally:
            self._reader_done = True
            self._ready.set()
            self._queue.put(_EOF)

    def _find_marker(self):
        """Skips what the server's shell printed before the marker. Returns the bytes after
        the marker, or None at end of file."""
        marker = b"VCHARON-READY " + self._nonce.encode("ascii") + b"\n"
        keep = len(marker) - 1
        buf = b""
        try:
            while True:
                chunk = self._proc.stdout.read(65536)
                self._touch()
                if not chunk:
                    self._junk(buf)
                    return None
                buf += chunk
                at = buf.find(marker)
                if at >= 0:
                    self._junk(buf[:at])
                    with self._lock:
                        self._marker_seen = True
                    self._ready.set()
                    return buf[at + len(marker):]
                if len(buf) > keep:
                    # Keep what could be the start of a marker split across reads.
                    self._junk(buf[:-keep])
                    buf = buf[-keep:]
        finally:
            if self.junk_bytes:
                text = self._junk_head.decode("utf-8", "replace")
                self.log.warn("the server's shell printed %d bytes before vcharon started:\n%s"
                              % (self.junk_bytes, "\n".join("| " + line
                                                            for line in text.splitlines())))

    def _junk(self, data):
        self.junk_bytes += len(data)
        room = JUNK_KEEP - len(self._junk_head)
        if room > 0:
            self._junk_head += data[:room]

    def _read_frames(self, rest):
        stdout = _Stdout(self._proc.stdout, rest, self._touch)
        while not self._queue.closed:
            try:
                frame = proto.read_frame(stdout.read_exact)
                if frame is None:
                    return
                kind, payload = frame
                item = frame
                if kind == proto.J:
                    msg = proto.decode_json(payload)
                    if msg["t"] == "log":
                        level = msg.get("level") if msg.get("level") in LEVELS else "info"
                        text = msg.get("msg")
                        self.helper_log.write(level, "helper: %s"
                                             % (text if isinstance(text, str)
                                                else proto.quote(text)))
                        continue
                    if msg["t"] == "tick":
                        continue
                    item = (kind, msg)
            except VCharonError as e:
                self._queue.put(e)
                return
            self._queue.put(item, len(payload))

    def _read_stderr(self):
        # Buffered, for readline; the limit keeps a line without a newline from growing.
        stream = io.BufferedReader(self._proc.stderr, 8192)
        try:
            while True:
                line = stream.readline(8192)
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                with self._lock:
                    self._tail.append(text)
                self.log.info("stderr: %s" % text)
        except (OSError, ValueError):
            return
        finally:
            # closes ssh's stderr pipe too
            stream.close()

    def _watch(self):
        s = self.settings
        while not self._stop.wait(0.1):
            now = time.monotonic()
            with self._lock:
                marker, waiting, run_from = self._marker_seen, self._waiting, self._run_from
            reason = None
            if not marker and now - self._started >= s.handshake_timeout:
                reason = "handshake"
            elif marker and waiting and now - self._activity >= s.idle_timeout:
                reason = "idle"
            elif s.run_timeout and run_from is not None and now - run_from >= s.run_timeout:
                reason = "run"
            if reason and self.kill(reason):
                return

    def start_round(self):
        """vcharon sync --repeat: run_timeout counts from now, within this round (M15)."""
        with self._lock:
            self._run_from = time.monotonic()

    def end_round(self):
        """vcharon sync --repeat: the wait between rounds doesn't count for run_timeout."""
        with self._lock:
            self._run_from = None

    def kill(self, reason):
        """Stops ssh at once; the reason decides the error the run fails with. Returns False
        only when it declines: a handshake kill once the marker has arrived."""
        proc = self._proc
        if proc is None:
            return True
        with self._lock:
            # The marker may have arrived since the watchdog looked.
            if reason == "handshake" and self._marker_seen:
                return False
            if proc.poll() is not None:
                return True
            # Once ssh's stdout has ended on its own, a kill only cleans up: the run failed
            # for the reason the reader found, not for this one.
            if self._killed is None and not self._reader_done:
                self._killed = reason
            try:
                proc.kill()
            except OSError:
                pass
        self.log.warn("killing ssh: %s" % reason)
        return True

    # -- frames --------------------------------------------------------------------------

    def _write(self, data):
        view = memoryview(data)
        stdin = self._proc.stdin
        # A write that blocks is waiting on the helper to read.
        with self._waiting_on_helper():
            while view:
                # A raw pipe may take less than it was given.
                done = stdin.write(view[:WRITE_CHUNK])
                view = view[done:]
                self._touch()

    def send_json(self, obj):
        self._write(proto.frame(proto.J, proto.encode_json(obj)))

    def send_data(self, data):
        for f in proto.data_frames(data):
            self._write(f)

    def send_file_end(self, error=None):
        self._write(proto.end_frame(error))

    def next_frame(self):
        return self._next()

    def _next(self, timeout=None):
        """The next (kind, message or bytes) from the helper."""
        if self._ended is not None:
            raise self._ended
        deadline = None if timeout is None else time.monotonic() + timeout
        exited = None
        with self._waiting_on_helper():
            while True:
                item = self._queue.get(0.1)
                if item is not _EMPTY:
                    break
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    self._healthy = False
                    raise VCharonError("timeout", "no reply from the helper on %s within %g s"
                                       % (self.dest, timeout))
                if self._proc.poll() is not None:
                    # ssh has exited, but a leftover child may still hold the pipe open.
                    exited = exited or now
                    if now - exited >= 2:
                        item = _EOF
                        break
        if item is _EOF or (isinstance(item, VCharonError) and item.code == "lost"):
            # A frame cut short is the connection ending too, often because the watchdog
            # killed ssh mid-frame: report why it ended.
            self._healthy = False
            self._ended = self._end_error()
            raise self._ended
        if isinstance(item, VCharonError):
            self._healthy = False
            self._ended = item
            raise item
        return item

    def _end_error(self):
        s = self.settings
        if self._killed == "idle":
            return VCharonError("timeout", "nothing moved for %d s" % s.idle_timeout,
                                hint="check the network, or raise idle_timeout")
        if self._killed == "run":
            return VCharonError("timeout", "the run passed run_timeout (%d s)" % s.run_timeout,
                                hint="raise run_timeout")
        try:
            rc = "ssh exit %d" % self._proc.wait(2)
        except subprocess.TimeoutExpired:
            rc = "ssh still running"
        # the last stderr lines explain it; let the reader catch up
        self._stderr_thread.join(2)
        err = VCharonError("lost", "the connection to %s closed unexpectedly (%s)"
                           % (self.dest, rc),
                           hint="see the log, then run again")
        err.tail = self._tail_lines()
        return err

    def _tail_lines(self):
        with self._lock:
            return list(self._tail)

    @contextlib.contextmanager
    def _waiting_on_helper(self):
        """The idle clock runs only inside this: while the main thread is blocked reading from
        the helper or writing to it. Local work between those waits never counts."""
        with self._lock:
            self._waiting += 1
            if self._waiting == 1:
                # Whatever happened before this wait wasn't the helper's silence.
                self._activity = time.monotonic()
        try:
            yield
        finally:
            with self._lock:
                self._waiting -= 1

    # -- calls ---------------------------------------------------------------------------

    @property
    def usable(self):
        """True while a call can go through: the hello has arrived, the session isn't closed,
        and both sides are still in step. Proxies check it before a call they may skip."""
        return self.hello is not None and not self._closed and self._healthy

    def call(self, fn, args=None, upload=None, receive=None, timeout=None):
        """One call. upload: [(index, opener)] to stream after it. receive: (indexes, stage) for
        a stream that follows the ok. Returns the result."""
        if self._closed or not self._healthy or self.hello is None:
            raise VCharonError("internal", "call %s on a session that isn't open" % fn)
        call_id = self._next_id
        self._next_id += 1
        try:
            return self._call(call_id, fn, {} if args is None else args, upload, receive,
                              timeout)
        except VCharonError as e:
            if e.code in ("lost", "timeout", "protocol"):
                self._healthy = False
            raise
        except BaseException:
            self._healthy = False
            raise

    def _call(self, call_id, fn, args, upload, receive, timeout):
        try:
            self.send_json(proto.call_msg(call_id, fn, args))
            if upload is not None:
                proto.send_stream(self, upload)
        except OSError as e:
            # The helper may have exited with an err first: read to the end for it.
            self.log.debug("writing call %s stopped: %s" % (fn, e))
            self._healthy = False
            while True:
                kind, msg = self._next()
                if kind == proto.J and msg["t"] == "err":
                    err = VCharonError.from_json(msg.get("error"))
                    err.reply = msg
                    raise err
        kind, msg = self._next(timeout)
        t = msg["t"] if kind == proto.J else None
        reply_id = msg.get("id") if kind == proto.J else None
        mine = proto.is_id(reply_id) and reply_id == call_id
        if t == "err" and (mine or reply_id is None):
            err = VCharonError.from_json(msg.get("error"))
            err.reply = msg
            # The helper exits after a protocol error.
            if err.code == "protocol" or reply_id is None:
                self._healthy = False
            raise err
        if t != "ok" or not mine:
            self._healthy = False
            got = proto.quote(msg) if kind == proto.J else "a %s frame" % chr(kind)
            raise VCharonError("protocol", "expected the reply to %s, got %s" % (fn, got))
        if receive is not None:
            indexes, stage = receive
            proto.receive_stream(self, indexes, stage)
        return msg.get("result")

    def echo(self, data):
        """Sends data to the helper and returns what came back."""
        out = io.BytesIO()

        def stage(index, fin):
            while True:
                chunk = fin.read(proto.CHUNK)
                if not chunk:
                    return
                out.write(chunk)

        self.call("echo", upload=[(0, lambda: io.BytesIO(data))], receive=([0], stage))
        return out.getvalue()

    # -- shutdown ------------------------------------------------------------------------

    def close(self):
        """Says bye if the helper is up and in step, then stops ssh. Safe to call twice."""
        proc = self._proc
        if self._closed or proc is None:
            self._closed = True
            return
        if self._healthy and self.hello is not None:
            try:
                self.call("bye", timeout=5)
            except Exception as e:
                self.log.debug("bye failed: %s" % e)
        self._closed = True
        try:
            proc.stdin.close()
        except OSError:
            pass
        self._wait_exit(5)
        self._stop.set()
        self._queue.close()
        threads = [t for t in (self._stdout_thread, self._stderr_thread, self._watchdog) if t]
        for thread in threads:
            thread.join(2)
        # A reader still running after that is stuck on a pipe some leftover process holds
        # open; leave that pipe to it. The stderr reader closes its own pipe.
        if self._stdout_thread is None or not self._stdout_thread.is_alive():
            proc.stdout.close()
        if self._stderr_thread is None:
            proc.stderr.close()
        self.ssh_exit = proc.returncode
        self.log.info("ssh exited with code %s" % proc.returncode)
