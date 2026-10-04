"""Shared test helpers: fake ssh sessions and engines, helper overrides, configs and the
command line, file trees and big files."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import io
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

import vcharon
from vcharon import charter, cli, config, doctor, platform, run, ssh, stage
from vcharon.log import Log

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
    """A temp dir with a fake server home and a log file; ssh is fake_ssh.py."""

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
        """The sync's runner (cli.run_jobs) for the jobs named, in this process, with the
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
            code = cli._guarded(lambda: cli.run_jobs(names, run=run, **kw), run)
        return code, out.getvalue(), err.getvalue()

    def reset_job(self, name, verbose=False):
        """vcharon sync C --reset's work for the job name, in this process: (exit code, stdout,
        stderr)."""
        out, err = io.StringIO(), io.StringIO()
        run = cli._Run()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._guarded(lambda: cli._reset(name, verbose, run), run)
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


def record_format(limits=None):
    """A join record's format and limits, as join writes them: this vcharon's format, and
    the default limits unless limits ({max_mb, max_files, max_entry_kb}) are given."""
    return {"format": charter.FORMAT, "limits": dict(limits or charter.default_limits())}


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
