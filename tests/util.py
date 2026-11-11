"""Shared test helpers: fake ssh sessions and engines, helper overrides, configs and the
command line, file trees and big files."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

import vcharon
from vcharon import (
    channel_cmd,
    charter,
    cli,
    config,
    doctor,
    entries,
    lock,
    platform,
    run,
    skill,
    ssh,
    stage,
    sync,
)
from vcharon.log import Log
from vcharon.mailbox import watch
from vcharon.proto import VCharonError

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
FAKE_SSH = os.path.join(TESTS_DIR, "fake_ssh.py")
FAKE_SSH_ADD = os.path.join(TESTS_DIR, "fake_ssh_add.py")
PACKAGE_DIR = os.path.dirname(os.path.abspath(vcharon.__file__))
TEST_MACHINE_ID = "0123456789abcdef0123456789abcdef"
DEBIAN_13 = (b'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\n'
             b'VERSION_ID="13"\nVERSION="13 (trixie)"\nID=debian\n')


def _can_symlink():
    """True when os.symlink works here, probed once in a temp dir: Windows needs a privilege
    (Developer Mode or admin) that an ordinary shell may not hold."""
    with tempfile.TemporaryDirectory(prefix="vcharon-test-") as d:
        try:
            os.symlink("target", os.path.join(d, "link"))
        except (OSError, NotImplementedError):
            return False
    return True


CAN_SYMLINK = _can_symlink()


def read_all(stream, timeout=60):
    """stream.read() of a child's pipe, to its end, or an AssertionError after timeout seconds,
    as readline."""
    got = []
    reader = threading.Thread(target=lambda: got.append(stream.read()), daemon=True)
    reader.start()
    reader.join(timeout)
    if not got:
        raise AssertionError("the child's output didn't end within %d s" % timeout)
    return got[0]


def readline(stream, timeout=60):
    """stream.readline() of a child's pipe, or an AssertionError after timeout seconds: a child
    that hangs fails the test instead of hanging the suite (the caller then kills it, which
    ends the read)."""
    got = []
    reader = threading.Thread(target=lambda: got.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if not got:
        raise AssertionError("no line from the child within %d s" % timeout)
    return got[0]


def package_source(name):
    """The source of one module of the package, as the bundle carries it."""
    with open(os.path.join(PACKAGE_DIR, name), "rb") as f:
        return f.read().decode("utf-8")


def helper_override(code):
    """extra_modules that keep the real helper as vcharon.helper_real and replace vcharon.helper
    with a module that runs `code` (which may change _real.HANDLERS) and uses the real main."""
    wrapper = ("import vcharon.helper_real as _real\n" + textwrap.dedent(code)
               + "\nmain = _real.main\n")
    return {"vcharon.helper_real": package_source("helper.py"), "vcharon.helper": wrapper}


class FakeSshCase(unittest.TestCase):
    """A temp dir with a fake server home and a log file; ssh is fake_ssh.py. What setUp stores
    on self hides a subclass's method of that name, and a subclass's method hides a helper
    here: grep tests for a new name first."""

    def setUp(self):
        self.tmp = self.make_tmp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.vcharon_home = os.path.join(self.tmp, "vcharon-home")
        os.mkdir(self.home)
        os.mkdir(self.vcharon_home)
        self.log_path = os.path.join(self.tmp, "test.log")
        self.log = Log(self.log_path)
        # nor the agent this suite runs under: MEMBER.md's agent: is a test's to set
        agent_vars = {var for _, var in platform.AGENT_ENV}
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("FAKE_SSH_") and k not in agent_vars}
        # the fake server is Debian 13, whatever this box runs: no distro warning
        self.os_release = os.path.join(self.tmp, "os-release")
        with open(self.os_release, "wb") as f:
            f.write(DEBIAN_13)
        env.update(FAKE_SSH_HOME=self.home, VCHARON_TEST_MACHINE_ID=TEST_MACHINE_ID,
                   VCHARON_HOME=self.vcharon_home, VCHARON_TEST_OS_RELEASE=self.os_release)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ssh, "ssh_prefix",
                                    lambda settings: [sys.executable, FAKE_SSH])
        patcher.start()
        self.addCleanup(patcher.stop)
        # so no test ever reaches the real agent
        patcher = mock.patch.object(ssh, "ssh_add_prefix",
                                    lambda settings: [sys.executable, FAKE_SSH_ADD])
        patcher.start()
        self.addCleanup(patcher.stop)
        # the box's PATH may hold other vcharon installs (a developer's): doctor's lines must
        # not depend on it. Doctor's PATH tests run the real check on their own folders
        patcher = mock.patch.object(doctor, "path_check", lambda *a, **kw: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def server_shows(self, path):
        """path, under the fake server's home, as an error raised on the server shows it: its
        home as server:~ (the helper's, with the server's home)."""
        return "server:~" + path[len(self.home):]

    def make_tmp(self):
        """The test's own temp folder, new and empty; removed when the test ends."""
        return tempfile.mkdtemp(prefix="vcharon-test-")

    def settings(self, **overrides):
        # Generous by default, so a busy machine can't fail the tests that aren't about time.
        base = config.Settings(ssh_path="/usr/bin/ssh", handshake_timeout=20, idle_timeout=20)
        return dataclasses.replace(base, **overrides)

    def session(self, floor=vcharon.FLOOR, extra_modules=None, **overrides):
        s = ssh.Session(self.settings(**overrides), "fake-dest", self.log, floor=floor,
                        extra_modules=extra_modules)
        self.addCleanup(s.close)
        return s

    def engine(self, source, sink, after_check=None, after_plan=None, **session_kw):
        """An Engine for two run.Sides on an opened session. self.calls then lists the fns
        the session calls, in order, and self.call_args (fn, args) for each."""
        s = self.session(**session_kw)
        s.open()
        self.call_args = []
        self.calls = record_calls(s, self.call_args)
        return run.Engine(s, source, sink, self.log, after_check=after_check,
                          after_plan=after_plan)

    def write_config(self, text):
        """Writes text, dedented, as $VCHARON_HOME/vcharon.ini; its path."""
        path = os.path.join(self.vcharon_home, "vcharon.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(text))
        return path

    def run_cli(self, *argv, stdin=None):
        """cli.main(argv) in this process: (exit code, stdout, stderr). stdin: bytes for a
        command that reads it (post without --body)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(mock.patch.object(cli, "_stdin_bytes", lambda: stdin))
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_jobs(self, *argv):
        """The sync's runner (sync.run_jobs) for the jobs named, in this process, with the
        errors shown as every command shows them: (exit code, stdout, stderr). argv: job names,
        and --full, --dry-run, --repeat S, -v. The engine's own tests drive jobs this way:
        vcharon sync runs only a channel section's two."""
        names, kw = [], {}
        args = iter(argv)
        for arg in args:
            if arg == "--full":
                kw["full"] = True
            elif arg == "--dry-run":
                kw["dry_run"] = True
            elif arg == "--repeat":
                kw["repeat"] = int(next(args))
            elif arg in ("-v", "--verbose"):
                kw["verbose"] = True
            else:
                names.append(arg)
        out, err = io.StringIO(), io.StringIO()
        run = cli._Run()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._guarded(lambda: sync.run_jobs(names, run=run, **kw), run)
        return code, out.getvalue(), err.getvalue()

    def reset_job(self, name, verbose=False):
        """vcharon sync C --reset's work for the job name, in this process: (exit code, stdout,
        stderr)."""
        out, err = io.StringIO(), io.StringIO()
        run = cli._Run()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._guarded(lambda: sync._reset(name, verbose, run), run)
        return code, out.getvalue(), err.getvalue()

    def write_record(self, channel, name, leader, ssh="fake-dest", remote="vcharon_mailbox",
                     project="p", role=None):
        """A join record, as vcharon join writes it, for a membership a test made by hand: so
        the commands that find it by channel, project and role (DESIGN, "Which membership")
        find it."""
        from vcharon import channel_cmd
        doc = {"version": channel_cmd.RECORD_VERSION, "channel": channel, "name": name,
               "leader": leader, "ssh": ssh, "remote": remote,
               "machine": os.environ.get("VCHARON_TEST_MACHINE_ID", TEST_MACHINE_ID),
               "project": project, "role": role}
        doc.update(record_format())
        channel_cmd.write_record(doc)
        return doc

    def log_text(self):
        try:
            with open(self.log_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""


def skill_home(case):
    """Points HOME (USERPROFILE on Windows) at a new folder for case's test, where skill.path
    puts the agents' skill copies; its path."""
    home = tempfile.mkdtemp(prefix="vcharon-test-home-")
    case.addCleanup(shutil.rmtree, home, True)
    patcher = mock.patch.dict(os.environ, HOME=home, USERPROFILE=home)
    patcher.start()
    case.addCleanup(patcher.stop)
    return home


def write_skill(agent, text=None):
    """agent's skill copy under HOME: this version's text, or text; its path."""
    target = skill.path(agent)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(skill.text() if text is None else text)
    return target


# what an older vcharon wrote: the marker, another text
OLD_SKILL = "---\nname: vcharon\n---\n%s\nold text\n" % skill.MARKER


def record_format(limits=None):
    """A join record's format and limits, as join writes them: a work channel's format, and
    the default limits unless limits ({max_mb, max_files, max_entry_kb}) are given."""
    return {"format": charter.WORK_FORMAT, "limits": dict(limits or charter.default_limits())}


_REAL_READ_JOBS = config._read_jobs


def _read_jobs_with_test_jobs(parser, name, hint, settings):
    """config._read_jobs, where a section with ssh, from and to (and no mailbox keys) is also
    a job, in file order. The config file holds no jobs of its own; the engine's tests use
    these to drive the sync's runner (FakeSshCase.run_jobs) with jobs a channel section
    doesn't make (other options, other names). Not checked like a channel section: the
    fixtures are trusted."""
    jobs = {}
    folded = {}
    for section in parser.sections():
        keys = parser.options(section)
        if section == "vcharon" or any(k.startswith("mailbox.") for k in keys):
            continue
        values = dict(parser.items(section))
        settings_ = dataclasses.replace(settings)
        options = {"from": {}, "to": {}}
        for key, value in values.items():
            side, dot, option = key.partition(".")
            if dot and side in options:
                options[side][option] = value
            elif key in config.JOB_SETTINGS:
                config._setting(settings_, key, value, "%s [%s] %s" % (name, section, key), hint)
        sides = []
        for key in ("from", "to"):
            end, _, plugin_name = values[key].partition(":")
            sides.append(run.Side(end, plugin_name, options[key]))
        jobs[section] = config.Job(section, values["ssh"], sides[0], sides[1], settings_,
                                   values["from"], values["to"])
        folded[section.casefold()] = "[%s]" % section
        parser.remove_section(section)
    _REAL_READ_JOBS(parser, name, hint, settings)
    return jobs, folded


def use_test_jobs(case):
    """Lets the config file hold test jobs (_read_jobs_with_test_jobs) for the rest of case;
    in this process only. The engine's tests (the job runner, state, doctor's job checks) use
    it: a channel section makes only its own two jobs, with their fixed options."""
    patcher = mock.patch.object(config, "_read_jobs", _read_jobs_with_test_jobs)
    patcher.start()
    case.addCleanup(patcher.stop)


def record_calls(session, args=None):
    """Wraps the session's call; returns the list the fns it calls go into. When args is a
    list, it also gets (fn, call args) for each call."""
    calls = []
    real = session.call

    def call(fn, *a, **kw):
        calls.append(fn)
        if args is not None:
            args.append((fn, a[0] if a else kw.get("args")))
        return real(fn, *a, **kw)

    session.call = call
    return calls


def big_file(path, size):
    """size pseudo-random bytes, written 1 MiB at a time: one random block, each copy prefixed
    with its number, so no two blocks match."""
    block = os.urandom(1 << 20)
    with open(path, "wb") as f:
        n = 0
        while size > 0:
            data = struct.pack(">Q", n) + block[8:]
            f.write(data[:size])
            size -= len(data)
            n += 1


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(1 << 20)
            if not data:
                return h.hexdigest()
            h.update(data)


def fd_count():
    """How many fds this process has open (POSIX)."""
    return len(os.listdir("/dev/fd"))


FIFO_BYTES = b"FIFO BYTES"


def unblock_fifo(case, path, delay=3.0):
    """If anything blocks opening the FIFO at path for reading, a writer comes after delay
    seconds and writes FIFO_BYTES: a wrong implementation then fails on the content instead of
    hanging the suite. Returns stop(), which ends the writer; call it when the subtest ends.
    The test's end stops it too."""
    done = threading.Event()

    def writer():
        if done.wait(delay):
            return
        while True:
            try:
                # fails with ENXIO while nobody has it open for reading; never follows a link
                fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                if done.wait(0.2):
                    return
                continue
            try:
                # only ever into a FIFO: by now the path may be another test's regular file
                if stat.S_ISFIFO(os.fstat(fd).st_mode):
                    os.write(fd, FIFO_BYTES)
            finally:
                os.close(fd)
            return

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()

    def stop():
        done.set()
        thread.join(60)

    case.addCleanup(stop)
    return stop


def write_tree(root, spec):
    """Makes files and directories under root: {"a/b.txt": b"bytes", "d/": None}; a trailing
    "/" means a directory. Parents are made as needed."""
    for rel, data in spec.items():
        path = os.path.join(root, *rel.rstrip("/").split("/"))
        if rel.endswith("/"):
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


@functools.cache
def folds_case():
    """True if the temp dir takes a and A as one name (APFS and NTFS by default): a tree with
    case twins can't be made there."""
    with tempfile.TemporaryDirectory(prefix="vcharon-test-") as d:
        open(os.path.join(d, "a"), "wb").close()
        return os.path.exists(os.path.join(d, "A"))


FOLDS_CASE = "the temp dir folds case: it can't hold case twins"


def read_tree(root):
    """Everything under root in write_tree's form, stage dirs included, so a leftover one
    shows. A symlink shows as "<path>@": its target."""
    out = {}
    stack = [""]
    while stack:
        rel = stack.pop()
        for name in os.listdir(os.path.join(root, *rel.split("/")) if rel else root):
            path = rel + name
            full = os.path.join(root, *path.split("/"))
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                out[path + "@"] = os.readlink(full)
            elif stat.S_ISDIR(st.st_mode):
                out[path + "/"] = None
                stack.append(path + "/")
            else:
                with open(full, "rb") as f:
                    out[path] = f.read()
    return out


def umask():
    """The process umask, which can only be read by setting it."""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def start_failure_text(path):
    """[<path>] run with this Python: the message vcharon's "couldn't start <path>: ..." shows.
    Raising the same OSError is the only way to get it: this OS localizes it (Windows says
    "系统找不到指定的文件。" on a Chinese code page), so no literal English string fits every
    machine."""
    try:
        subprocess.run([path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=False)
    except OSError as e:
        return e.strerror or str(e)
    raise AssertionError("%s could be started, so it can't test a start failure" % path)


def run_stager(root, plan, data, **kw):
    """check, stage each file put not in have from data[path] (as the engine does), commit;
    (checked, done)."""
    with stage.Stager(root, **kw) as s:
        checked = s.check(plan)
        for i, e in enumerate(plan.entries):
            if e.op == "put" and e.kind == "file" and i not in checked.have:
                s.stage(i, io.BytesIO(data[e.path]))
        done = s.commit()
    return checked, done


class StatWith:
    """A stat result with some fields replaced: StatWith(st, st_uid=0)."""

    def __init__(self, st, **fields):
        self._st = st
        self._fields = fields

    def __getattr__(self, name):
        if name in self._fields:
            return self._fields[name]
        return getattr(self._st, name)


def patch_stats(case, path, **fields):
    """For the rest of a test, every os.stat, os.lstat and os.fstat of the file at path (by its
    inode) reports these fields instead."""
    ino = os.lstat(path).st_ino

    def wrap(fn):
        def inner(*args, **kw):
            st = fn(*args, **kw)
            return StatWith(st, **fields) if st.st_ino == ino else st
        return inner

    for name in ("stat", "lstat", "fstat"):
        patcher = mock.patch.object(os, name, wrap(getattr(os, name)))
        patcher.start()
        case.addCleanup(patcher.stop)


# Holds an exclusive lock on argv[1], says so, then waits to be killed.
if os.name == "nt":
    HOLDER = """
import msvcrt, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_BINARY)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print("held", flush=True)
time.sleep(60)
"""
else:
    HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
time.sleep(60)
"""


def hold_in_child(path):
    """A child process that holds the lock on path, once it has said so. path must exist: the
    child opens it without O_CREAT (test_channel's hold makes it)."""
    child = subprocess.Popen([sys.executable, "-c", HOLDER, path], stdout=subprocess.PIPE,
                             stdin=subprocess.DEVNULL)
    try:
        line = readline(child.stdout)
    except AssertionError:
        line = b"(nothing)"
    if line.strip() != b"held":
        child.kill()
        child.communicate(timeout=60)
        raise AssertionError("the child didn't take the lock: %r" % line)
    return child


# Takes the watcher's lock on the snapshot argv[1] as a watcher does (it writes its PID, with
# argv[2] as its PID namespace, "-" for none), says that PID, then waits to be killed.
TAKE_LOCK = """
import os, sys, time
from vcharon import lock
from vcharon.mailbox import watch
ns = None if sys.argv[2] == "-" else sys.argv[2]
lock.pid_ns = lambda: ns
lk = watch.take_lock(sys.argv[1])
print(os.getpid() if lk is not None else "not held", flush=True)
time.sleep(60)
"""


def take_in_child(snapshot):
    """(a child process holding the watcher's lock on snapshot, its PID) once it has said so.
    Its own os.getpid(), not Popen.pid: on Windows a venv's python.exe runs the real one as a
    child. It writes the PID namespace this process's pid_ns gives (show_pid's, in a test that
    called it), so this process reads its PID back."""
    package = os.path.dirname(os.path.abspath(vcharon.__file__))
    ns = lock.pid_ns()
    child = subprocess.Popen([sys.executable, "-c", TAKE_LOCK, snapshot,
                              "-" if ns is None else ns],
                             stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             env=dict(os.environ, PYTHONPATH=os.path.dirname(package)))
    try:
        line = readline(child.stdout)
    except AssertionError:
        line = b"(nothing)"
    if not line.strip().isdigit():
        stop_child(child)
        raise AssertionError("the child didn't take the lock: %r" % line)
    return child, int(line)


# the PID namespace show_pid gives this process: Linux's initial one, or none off Linux
SHOWN_NS = lock.INIT_PID_NS if sys.platform.startswith("linux") else None


def show_pid(case, shown=True):
    """Makes write_pid in this process write its PID and holder_pid read it (or neither, shown
    False: as with an unreadable /proc/self/ns/pid) for the rest of the test case, in whatever
    PID namespace the suite runs."""
    ns = SHOWN_NS if shown else ""
    patcher = mock.patch.object(lock, "pid_ns", lambda: ns)
    patcher.start()
    case.addCleanup(patcher.stop)


def stop_child(child):
    child.kill()
    child.communicate(timeout=60)


def unix_socket(case, path):
    """A bound AF_UNIX socket at path. It's bound from its own directory, so a long temp path
    can't overflow the socket address."""
    if not hasattr(socket, "AF_UNIX"):
        case.skipTest("no AF_UNIX sockets here")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    case.addCleanup(sock.close)
    cwd = os.getcwd()
    os.chdir(os.path.dirname(path))
    try:
        sock.bind(os.path.basename(path))
    finally:
        os.chdir(cwd)
    return sock


VCHARON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class Between:
    """sync._wait_or_end for --repeat tests: each wait between rounds runs the next action;
    with none left, stdin has ended."""

    def __init__(self, *actions):
        self.actions = list(actions)
        self.waits = []

    def __call__(self, ended, seconds):
        self.waits.append(seconds)
        if not self.actions:
            return True
        self.actions.pop(0)()
        return False


def hold(case, path):
    """A child process holding the lock file path (made first), until the test ends. A held
    post lock (entries.lock_path) stalls whatever posts into that folder, leave's LEAVE too,
    for entries.LOCK_WAIT, then busy."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "ab").close()
    child = hold_in_child(path)
    case.addCleanup(release, child)
    return child


def release(child):
    """Ends a holder; once is enough."""
    if child.poll() is None:
        stop_child(child)


class ChannelCase(FakeSshCase):
    """One fake server (its root under the fake home) and boxes, each a VCHARON_HOME of its own
    with [vcharon] box set. The current directory is a git checkout named Web."""

    def setUp(self):
        FakeSshCase.setUp(self)
        self.root = os.path.join(self.home, "channels")
        os.environ["VCHARON_CHANNELS_ROOT"] = self.root
        self.homes = {}
        self.use_box("mac", self.vcharon_home)
        self.project = os.path.join(self.tmp, "work", "Web")
        os.makedirs(os.path.join(self.project, ".git"))
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    # A class that sets TEMPLATE builds its channel once (from_template) and gives every test
    # a copy. The files hold absolute paths into the temp folder (records, job state, logs),
    # so each test of such a class gets the same temp folder path: a copy put back there
    # reads as the build left it. The tests stay apart: each starts from a fresh copy.
    TEMPLATE = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.TEMPLATE:
            # made again each time the class runs: a later run in the same process (tests of
            # two classes named in turn) finds the last one's folder gone
            cls._template_root = tempfile.mkdtemp(prefix="vcharon-class-")
            cls._template = None
            cls.addClassCleanup(shutil.rmtree, cls._template_root, True)

    def make_tmp(self):
        if not self.TEMPLATE:
            return FakeSshCase.make_tmp(self)
        tmp = os.path.join(self._template_root, "case")
        shutil.rmtree(tmp, True)
        if os.path.exists(tmp):
            # never hand a test a folder that isn't new
            left = [os.path.join(d, n) for d, dirs, files in os.walk(tmp) for n in dirs + files]
            self.fail("the last test left files that can't be removed in %s: %s"
                      % (tmp, ", ".join(left[:5]) or "the folder itself"))
        os.mkdir(tmp)
        return tmp

    def from_template(self, build):
        """build() in this class's first test, then keep a copy of the temp folder, the
        environment and the boxes it left; every later test gets that copy in place of its
        fresh setUp's, as if it had run build() itself."""
        assert self.TEMPLATE, "from_template needs TEMPLATE = True on the class"
        cls = type(self)
        saved = os.path.join(cls._template_root, "template")
        if cls._template is None:
            build()
            shutil.copytree(self.tmp, saved, symlinks=True)
            cls._template = (dict(os.environ), dict(self.homes))
            return
        env, homes = cls._template
        # out of the folder first: Windows can't remove the current directory
        os.chdir(cls._template_root)
        shutil.rmtree(self.tmp)
        shutil.copytree(saved, self.tmp, symlinks=True)
        os.environ.clear()
        os.environ.update(env)
        self.homes = dict(homes)
        os.chdir(self.project)

    def use_box(self, box, home=None):
        """Switches to the box named box (a VCHARON_HOME of its own, made on first use)."""
        if box not in self.homes:
            home = home or os.path.join(self.tmp, "box-" + box)
            os.makedirs(home, exist_ok=True)
            with open(os.path.join(home, "vcharon.ini"), "w", encoding="utf-8") as f:
                f.write("[vcharon]\nbox = %s\n" % box)
            self.homes[box] = home
        os.environ["VCHARON_HOME"] = self.homes[box]
        return self.homes[box]

    def channel(self, *argv):
        return self.run_cli(*argv)

    def ok(self, *argv):
        code, out, err = self.channel(*argv)
        self.assertEqual(code, 0, out + err)
        return out

    def refused(self, *argv, code=1):
        """The ERROR line of a refused command."""
        return self.refusal(*argv, code=code)[0]

    def refusal(self, *argv, code=1):
        """The ERROR line and the fix line (as printed, after runnable) of a refused
        command."""
        got, out, err = self.channel(*argv)
        self.assertEqual(got, code, out + err)
        lines = err.splitlines()
        fixes = [l[len("  fix: "):] for l in lines if l.startswith("  fix: ")]
        return lines[0], fixes[0] if fixes else None

    def server_tree(self):
        return read_tree(self.root) if os.path.isdir(self.root) else {}

    def joined(self, section, box=None):
        """A remote member's local tree on the current box."""
        return os.path.join(os.environ["VCHARON_HOME"], "joined", section)

    def record(self, section):
        with open(os.path.join(os.environ["VCHARON_HOME"], "state", "channels",
                               section + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def lead(self, channel="game", where=("--server", "fake-dest"), box="laptop", project="ui"):
        """box creates channel and leads it; back to the mac box after."""
        self.use_box(box)
        self.ok("create", channel, *where, "--project", project)
        self.use_box("mac")
        return "%s-%s" % (box, project)


# a spelling of vcharon that runnable() has to write, so a fix line that it skips shows
SPELLED = "/opt/vc/bin/vcharon"


def read_only(test, folder):
    """folder/sub/f with sub read-only, so a delete of folder fails at f; writable again at
    the cleanup, for the test's own removal."""
    sub = os.path.join(folder, "sub")
    write_tree(sub, {"f": b"x"})
    os.chmod(sub, 0o555)

    def writable():
        for dirpath, dirnames, _ in os.walk(test.root):
            for n in dirnames:
                os.chmod(os.path.join(dirpath, n), 0o755)

    test.addCleanup(writable)


NO_READ_ONLY = os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0)


OTHER_CLIENT = "f" * 32


LIMITS = {"max_mb": 50, "max_files": 1000, "max_entry_kb": 1000}


def channel_md(leader, fmt="1", limits=LIMITS, extra=()):
    """CHANNEL.md's text as create writes it; fmt None leaves the format: line out."""
    header = [("leader", leader), ("created", "2026-10-01 09:00")]
    if fmt is not None:
        header.append(("format", fmt))
    header += [(charter.HEADER_KEYS[k], str(limits[k])) for k in charter.LIMIT_KEYS
               if k in limits] + list(extra)
    return ("# CHANNEL\n" + entries.build("2026-10-01 09:00", leader, 2, "channel game created",
                                          ["@all"], header=header)).encode("utf-8")


def charter_text(name, number, term=None, after=None, leader=None):
    """A CHANNEL.md holding one charter of name's."""
    # a taker's is to itself, as vcharon lead writes it; create's to @all
    lines = ["# CHANNEL", "", "## 2026-10-23 10:00:00 — %s#%d — channel" % (name, number),
             "to: %s" % ("@all" if term is None else "@" + name),
             "leader: %s" % (leader or name), "created: 2026-10-23 10:00:00",
             "rules: vcharon guide rules"]
    if term is not None:
        lines.append("term: %d" % term)
    if after is not None:
        lines.append("after: %s" % after)
    lines += ["format: 1", "created by: vcharon 0.8.5", "max mb: 50", "max files: 1000",
              "max entry kb: 1000", ""]
    return "\n".join(lines).encode("utf-8")


# what follows `vcharon sync` for the remote member windows (ClientModeTest.write_config)
SYNC = ["mb", "--project", "p"]


# 2026-10-01 09:05:46 on this machine's clock, whatever its zone
T0 = time.mktime((2026, 10, 1, 9, 5, 46, 0, 0, -1))


STAMP = re.compile(r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")


class Clock:
    """A clock for the watch loops: both the lines' time and the limits' timer."""

    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Rounds:
    """A sleep for the watch loops: each call runs the next of the given steps, and moves the
    clock on if there is one."""

    def __init__(self, *steps, clock=None):
        self.steps = list(steps)
        self.seconds = []
        self.clock = clock

    def __call__(self, seconds):
        self.seconds.append(seconds)
        if self.clock is not None:
            self.clock.t += seconds
        self.steps.pop(0)()


def never(seconds):
    raise AssertionError("slept before the first round")


def member_md(name, leader, channel="mb"):
    """MEMBER.md as vcharon join writes it."""
    return ("# MEMBER\n\n## 2026-10-01 09:00 — %s#1 — member\nto: @%s\nchannel: %s\nname: %s\n"
            "leader: %s\n" % (name, leader, channel, name, leader)).encode("utf-8")


class WatchCase(unittest.TestCase):
    """watch.scan keys a file by its size and mtime_ns only: a test that rewrites a file
    changes its length, else the rewrite can land in the same mtime tick and go unseen."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # a channel's folder, mb; server mode watches it as the member debian, its leader
        self.tree = os.path.join(self.tmp, "mb")
        # the saved snapshots and their locks go to vcharon's state dir: one per test
        self.vcharon_home = os.path.join(self.tmp, "vcharon-home")
        patch = mock.patch.dict(os.environ, {"VCHARON_HOME": self.vcharon_home})
        patch.start()
        self.addCleanup(patch.stop)
        self.lines = []
        self.member()

    def member(self, me="debian", leader="debian", root=None):
        """me's own folder with its MEMBER.md: server mode watches only such a folder's
        member."""
        write_tree(self.tree if root is None else root,
                   {"%s/MEMBER.md" % me: member_md(me, leader)})

    def post(self, folder, n, title, to="@debian", name=None, file="RESULTS.md",
             when="2026-10-01 09:00", root=None):
        """Appends one entry, as mailbox_post.py writes it, by name (folder's member unless
        given) to folder/file."""
        path = os.path.join(self.tree if root is None else root, folder, *file.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = entries.build(when, name or folder, n, title, to.split())
        with open(path, "ab") as f:
            if not f.tell():
                f.write(b"# RESULTS\n")
            f.write(text.encode("utf-8"))
        return path

    def said(self, lines=None):
        """The lines without their time, each checked to start with one."""
        out = []
        for line in self.lines if lines is None else lines:
            self.assertRegex(line, STAMP)
            out.append(line[20:])
        return out

    def saved_at(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)["saved"]

    def state(self, me="debian", job=None, root=None):
        return watch.snapshot_path(self.tree if root is None else root, me, job)

    def local_record(self, me="debian", leader="debian", project="q", role=None):
        """me's join record as a local member of mb, whose folder is self.tree: vcharon watch
        mb --project <project> then watches it as me (DESIGN, "Which membership")."""
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": me, "leader": leader,
                                  "ssh": None, "remote": self.tree,
                                  "machine": TEST_MACHINE_ID, "project": project,
                                  "role": role, **record_format()})

    def cli(self, *argv, project="q"):
        """vcharon watch mb --project <project> ARGV in this process: (exit code, stdout,
        stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(["watch", "mb", "--project", project] + list(argv))
        return code, out.getvalue(), err.getvalue()


    @staticmethod
    def write_config(case):
        """vcharon.ini, channels.d/mb.windows.ini and windows's join record, a remote member
        of the project p; the client's tree starts empty (server mode's debian/MEMBER.md goes:
        a test of both modes writes it again). Returns what follows `vcharon sync` for it."""
        os.remove(os.path.join(case.tree, "debian", "MEMBER.md"))
        os.rmdir(os.path.join(case.tree, "debian"))
        os.makedirs(case.vcharon_home, exist_ok=True)
        with open(os.path.join(case.vcharon_home, "vcharon.ini"), "w", encoding="utf-8") as f:
            f.write("[vcharon]\n")
        folder = os.path.join(case.vcharon_home, "channels.d")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "mb.windows.ini"), "w", encoding="utf-8") as f:
            f.write(MAILBOX.format(local=case.tree))
        channel_cmd.write_record({"version": 1, "channel": "mb", "name": "windows",
                                  "leader": "debian", "ssh": "devbox",
                                  "remote": "~/.local/state/vcharon/channels/mb",
                                  "machine": TEST_MACHINE_ID, "project": "p",
                                  "role": None, **record_format()})
        return list(SYNC)


# a channel section, in channels.d/ next to vcharon.ini: the member windows of the channel
# mb, led by debian
MAILBOX = """
[mb.windows]
ssh            = devbox
mailbox.me     = windows
mailbox.leader = debian
mailbox.local  = {local}
mailbox.remote = ~/.local/state/vcharon/channels/mb
"""


# A stand-in for the streaming child, `vcharon sync C --repeat <every>`: it does the
# actions in its argument, in order, then waits for the end of its stdin and exits 0, as vcharon
# sync --repeat does. out/err: a line on stdout/stderr; raw: hex bytes on stdout; append: text
# at the end of a file; exit: exit with that code now; deaf: ignore the end of stdin for 60 s;
# pause: sleep that many seconds (stdout and stderr are two pipes: a pause orders them).
FAKE_CHILD = """
import json, sys, time
for act in json.loads(sys.argv[1]):
    kind = act[0]
    if kind == "out":
        sys.stdout.write(act[1] + "\\n")
        sys.stdout.flush()
    elif kind == "err":
        sys.stderr.write(act[1] + "\\n")
        sys.stderr.flush()
    elif kind == "raw":
        sys.stdout.buffer.write(bytes.fromhex(act[1]))
        sys.stdout.flush()
    elif kind == "append":
        with open(act[1], "ab") as f:
            f.write(act[2].encode("utf-8"))
    elif kind == "exit":
        sys.exit(act[1])
    elif kind == "deaf":
        time.sleep(60)
    elif kind == "pause":
        time.sleep(act[1])
sys.stdin.buffer.read()
"""


class StreamCase(WatchCase):
    """A streaming watcher over scripted child processes, also used by lobby tests."""

    def setUp(self):
        WatchCase.setUp(self)
        self.sync_args = self.write_config(self)
        os.makedirs(self.tree, exist_ok=True)
        self.spawned = []
        self.children = []
        self.slept = []

    def entry(self, folder, n, title, to="@windows", file="RESULTS.md"):
        """("append", path, text): an entry, as mailbox_post.py writes it, for a child to add."""
        path = os.path.join(self.tree, folder, file)
        # the folder now, the file only when the child appends: the start sees no file
        os.makedirs(os.path.dirname(path), exist_ok=True)
        head = "" if os.path.exists(path) else "# RESULTS\n"
        return ["append", path,
                head + entries.build("2026-10-01 09:00", folder, n, title, to.split())]

    def spawn(self, argv, env):
        self.spawned.append((argv, env))
        acts = self.children.pop(0)
        proc = subprocess.Popen([sys.executable, "-c", FAKE_CHILD, json.dumps(acts)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        self.addCleanup(self.reap, proc)
        self.procs.append(proc)
        return proc

    @staticmethod
    def reap(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()

    def watch(self, *children, clock=None, steps=None, **kw):
        """watch_job, syncing every 2 s, over the children: (exit code, the lines without
        their time). steps: with clock, the seconds each round moves the clock on. timer: the
        limits' clock, clock unless given. A child does all its actions at once, then waits for
        its stdin's end, and the fake clock never reaches the stuck rule: a watcher that gets
        fewer rounds than rounds= hangs on the last child, so end that one with ["exit", 0]."""
        self.children = [list(c) for c in children]
        self.procs = []
        clock = clock or Clock()
        timer = kw.pop("timer", clock)
        lines = []
        real = watch.Stream.next_round

        def next_round(stream, deadline=None):
            got = real(stream, deadline)
            if steps:
                clock.t += steps.pop(0)
            return got

        def sleep(seconds):
            self.slept.append(seconds)
            clock.t += seconds

        kw.setdefault("max_minutes", 25)
        with mock.patch.object(watch.Stream, "next_round", next_round):
            code = watch.watch_job("mb.windows", self.sync_args, 2, out=lines.append, sleep=sleep,
                                   clock=clock, timer=timer, stream=True, spawn=self.spawn,
                                   **kw)
        return code, self.said(lines)

    def assert_all_stopped(self):
        for proc in self.procs:
            self.assertIsNotNone(proc.poll(), "a child still runs")

# where a command ends inside a text: always a space first, so that a mark stuck to the last
# value (a "--role b," copied along) shows in parse()
_STOP = re.compile(r" ; | again\b| once | takes | makes it| works\b| \(|\Z")
_COMMAND = re.compile(r"(?:\A|(?<=[ (`:]))vcharon (?=\S)")


def commands(text):
    """Each `vcharon …` command in text, as argv after `vcharon`. One that opens a parenthesis,
    `(vcharon guide watch)`, ends at its closing one."""
    out = []
    for m in _COMMAND.finditer(text):
        rest = text[m.end():]
        end = _STOP.search(rest).start()
        if text[m.start() - 1:m.start()] == "(" and ")" in rest:
            end = min(end, rest.index(")"))
        out.append(shlex.split(rest[:end]))
    return out


def parse(case, argv):
    """argv (after `vcharon`) parses with the command line's own parser. --update isn't one of
    runnable()'s commands (DESIGN, "Fix lines"), but it parses."""
    case.assertIn(argv[0], platform.COMMANDS + ("--update",))
    # a command ends before any mark: none sticks to its last value
    case.assertNotRegex(argv[-1], r"[,;:.)]\Z")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cli._parser().parse_args(argv)
    except VCharonError as e:
        case.fail("%r doesn't parse: %s" % (argv, e.message))
    except SystemExit:
        # a verb's --help
        case.assertEqual(argv[-1], "--help")
