"""The client sync runner: jobs, state, one shared session and repeated rounds.

The transfer engine is run.py (DESIGN, "How a sync works")."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import posixpath
import sys
import threading
import time
import traceback

from . import (
    VERSION,
    channel_cmd,
    channels,
    charter,
    charter_state,
    config,
    entries,
    fsops,
    install,
    pathrules,
    platform,
    plugin,
    proto,
    remote,
    ssh,
    stage,
    state,
)
from . import run as engine
from .command import _guarded, _Run, _say, load_config
from .log import HeldLog, Log
from .mailbox import watch as watch_mod
from .plugins import path as path_plugin
from .proto import VCharonError

# A dry run's listing shows this many paths, then "… and N more".
LIST_MAX = 50


def run_jobs(names, full=False, dry_run=False, repeat=None, verbose=False, run=None,
             held=None):
    """The jobs named, as vcharon sync runs a section's two (_run_job); its exit code. Errors
    are raised, for the caller's _guarded. held: a _Held that takes the console lines."""
    args = argparse.Namespace(job=list(names), full=full, dry_run=dry_run, repeat=repeat,
                              verbose=verbose, held=held)
    return _run_job(args, run if run is not None else _Run())


class _Held:
    """A join's or create's sync on the console: its lines held back, and on success one
    line in their place. A failure releases them: what was held, then the rest as it comes,
    so the error reads as vcharon sync shows it."""

    def __init__(self):
        # the lines held; None once released
        self.lines = []
        # each job that ended OK: (its side, up or down; files written; deleted)
        self.done = []

    def say(self, line):
        if self.lines is None:
            _say(line)
        else:
            self.lines.append(line)

    def release(self):
        lines, self.lines = self.lines or [], None
        for line in lines:
            _say(line)

    def summary(self, seconds):
        """The one line, then the sync's notes, which say what the counts don't (a member
        left out for its size)."""
        sides = ["%s %d written%s" % (side, written, ", %d deleted" % deleted if deleted else "")
                 for side, written, deleted in self.done]
        _say("  synced: %s  (%.1f s)" % ("; ".join(sides), seconds))
        for line in self.lines or []:
            if line.startswith("  note "):
                _say(line)
        self.lines = None


def _console(args):
    """How a sync's lines reach the console: as they come, or into join's or create's _Held."""
    return _say if args.held is None else args.held.say


def sync_section(section, full=False, verbose=False, brief=False):
    """A sync of the channel section, in this process, as vcharon sync would show it: join's
    and create's first sync, leave's last one. Its exit code. brief (join's and create's, but
    not with -v): one line on success, all of it on a failure."""
    run = _Run()
    held = _Held() if brief and not verbose else None
    started = time.monotonic()

    def sync():
        try:
            return run_jobs([section], full=full, verbose=verbose, run=run, held=held)
        except BaseException:
            # before _guarded's error block, in the order vcharon sync prints them
            if held is not None:
                held.release()
            raise

    code = _guarded(sync, run)
    if held is not None:
        if code == 0:
            held.summary(time.monotonic() - started)
        else:
            held.release()
    return code


PULL_HINT = ("the vcharon on the server may be broken, or the server compromised; nothing was "
             "changed")


def pull_name(path):
    """The name that a pull of the remote path gives its top entry, from the text alone; None
    when the text can't tell (empty, ~, ~user, or a name that comes out "", "." or "..")."""
    if not path or (path.startswith("~") and "/" not in path):
        return None
    path = path.removeprefix("~/")
    name = posixpath.basename(posixpath.normpath(path))
    return None if name in ("", ".", "..") else name


def pull_guard(path):
    """An Engine after_plan for a one-off pull of the remote path (a channel's own folder, at
    join). The server's plan may only put what the pull asked for, under that one name, and
    never delete: a broken or hostile server can't make a pull write elsewhere in the local
    folder (scp's CVE-2019-6111). It runs before anything is checked or staged."""
    name = pull_name(path)

    def refuse(what):
        raise VCharonError("unsafe_path", "the server's plan %s" % what, PULL_HINT)

    def guard(p):
        entries = p.entries
        if not entries:
            return
        for e in entries:
            if e.op != "put":
                refuse("deletes %s, and a pull never deletes" % pathrules.show(e.path))
        top = entries[0].path
        if not top or "/" in top:
            refuse("starts with %s, not with one name" % proto.quote(top))
        if len(entries) > 1:
            if entries[0].kind != "dir":
                refuse("puts %s as a file, and more after it" % pathrules.show(top))
            for e in entries[1:]:
                if not e.path.startswith(top + "/"):
                    refuse("puts %s, outside %s" % (pathrules.show(e.path),
                                                    pathrules.show(top)))
        if name is not None and top != name:
            refuse("is for %s, but the pull asked for %s" % (pathrules.show(top),
                                                             pathrules.show(name)))
        if name is None and top.startswith("."):
            refuse("is for %s, a dot name that the pull's path doesn't spell out"
                   % pathrules.show(top))

    return guard


# the sync's sizes, and the limits' (charter)
size_text = charter.size_text


def _counted(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _listing(say, items):
    for item in items[:LIST_MAX]:
        say("          %s" % item)
    if len(items) > LIST_MAX:
        say("          … and %d more" % (len(items) - LIST_MAX))


def _summary(say, p, checked, dry_run, job=False, full=False):
    """The lines after the check, before any bytes move. job: a sync's form, which says
    "nothing to do" for an empty plan, and with full how many files the target holds."""
    puts = [e for e in p.entries if e.op == "put"]
    files = [e for e in puts if e.kind == "file"]
    deletes = [e for e in p.entries if e.op == "delete"]
    if job and not p.entries:
        say("  nothing to do")
    else:
        line = "  put     %s, %s (%s)" % (_counted(len(files), "file"),
                                          _counted(len(puts) - len(files), "dir"),
                                          size_text(sum(e.size for e in files)))
        if full:
            line += ", %d already there" % len(checked.have)
        say(line)
        if dry_run:
            _listing(say, [pathrules.show(e.path) + ("/" if e.kind == "dir" else "")
                           for e in puts])
        if deletes:
            say("  delete  %d" % checked.deletes)
            if dry_run:
                _listing(say, [pathrules.show(e.path) + ("/ (tree)" if e.tree else "")
                               for e in deletes])
    for note in list(p.notes) + list(checked.notes):
        say("  note    %s" % note)


def _commit_notes(say, eng, done):
    """The commit's notes, less those the check already showed: a directory the check found
    not empty is kept at the commit too, and would otherwise be noted twice."""
    shown = set(eng.checked.notes) if eng.checked is not None else set()
    for note in done.notes:
        if note not in shown:
            say("  note    %s" % note)


def _done_line(eng):
    """The error block's line for a failed commit that did something, or None."""
    if eng.done is None or eng.plan is None:
        return None
    written = engine.written_files(eng.plan, eng.done)
    if not written and not eng.done.deleted:
        return None
    return "  done    %d written, %d deleted before the failure" % (written, eng.done.deleted)


# --- the sync's jobs ---

# a membership whose record is here but whose section isn't: join writes the section
NO_SECTION_HINT = ("join the channel again, with the --project and --role you joined with: it "
                   "writes the section")
SAVE_HINT = "the files were written; fix that, then run again"


def _dumps(obj):
    """A saved or current identity or target, as a state_mismatch message or a reset shows it:
    its server's machine id hashed (platform.shown_machine), as every output shows one."""
    return json.dumps(platform.shown_doc(obj), ensure_ascii=False, sort_keys=True)


def side_text(job, side, role):
    """One side of a job as a sync shows it: <ssh>:<path> on the remote end, <path> on the
    local one, with the plugin unless it's path for a source or dir for a sink. A side with
    no path is its plugin's name alone."""
    if "path" not in side.options:
        return side.plugin
    path = side.options["path"]
    text = "%s:%s" % (job.ssh, path) if side.end == "remote" else path
    if side.plugin != ("path" if role == "source" else "dir"):
        text += " (%s)" % side.plugin
    return text


def _jobs_named(cfg, name):
    """The jobs a name runs: a job, or a mailbox section's two (DESIGN, "Channel sections")."""
    jobs = cfg.named(name)
    skip = cfg.skipped_for(name) if jobs is None else None
    if skip is not None:
        # its own file's error; every other job still runs
        raise skip.error
    if jobs is None:
        raise VCharonError("config", "no channel section %s in %s"
                           % (pathrules.show(name), config.channels_dir(cfg.path)),
                           hint=NO_SECTION_HINT)
    return jobs


@dataclasses.dataclass
class _JobRun:
    """One job of a sync, and how it ended."""

    job: config.Job
    log: Log
    shown: tuple          # the source's and the sink's side_text
    # its error block's side of _Run: its log, its done line, its first error line
    errors: _Run = dataclasses.field(default_factory=_Run)
    started: float = 0.0
    # "ok", "failed" or "skipped" once it has ended, with its exit code
    status: str = None
    code: int = 0
    ok_line: str = None
    # vcharon sync --repeat: the job's own log, while log is the round's HeldLog; and whether
    # the round wrote its state
    real_log: Log = None
    saved_state: bool = False

    @property
    def name(self):
        return self.job.name


class _Conn:
    """The connection a sync's jobs share: opened by the first job that needs it, closed
    after the last job, or as soon as it breaks."""

    def __init__(self):
        self.session = None
        # the job that opened it; the session's own lines go to its log
        self.opener = None
        # the server's machine id and OS, from the hello
        self.machine = None
        self.os = None
        # the error that broke it; the later jobs are then skipped
        self.broke = None
        # sync --repeat's install.Watchdog
        self.watchdog = None


def _run_job(args, run):
    """The jobs of a sync: the arguments, every job's config and plugins, the logs, every
    lock, then each job in the order given, all on one connection (a sync runs one section,
    whose jobs share a destination). One job prints and logs exactly as ever; several end
    with a summary line."""
    started = time.monotonic()
    if args.repeat is not None:
        _repeat_flags(args, run)
    cfg = load_config()
    jobs = [job for name in args.job for job in _jobs_named(cfg, name)]
    # Every usage, config, option and capability error fails before anything else: one bad
    # job, and none runs.
    for job in jobs:
        try:
            plugin.check_side(job.source.end, job.source.plugin, "source", job.source.options)
            plugin.check_side(job.sink.end, job.sink.plugin, "sink", job.sink.options)
        except VCharonError:
            # Its text names an option, not the job; with several jobs, say which.
            if len(jobs) > 1:
                run.job_name = job.name
            raise
    flags = ((" --full" if args.full else "") + (" --dry-run" if args.dry_run else "")
             + (" --repeat %d" % args.repeat if args.repeat is not None else ""))
    names = " ".join(job.name for job in jobs)
    todo = []
    for job in jobs:
        log = Log(os.path.join(platform.log_dir(), job.name + ".log"), console=args.verbose)
        log.info("vcharon %s sync %s%s; Python %s (%s) on %s; config %s"
                 % (VERSION, names, flags, platform.python_version(), sys.executable,
                    platform.os_name(), cfg.path))
        shown = (side_text(job, job.source, "source"), side_text(job, job.sink, "sink"))
        log.info("job %s: from %s %s, to %s %s"
                 % (job.name, job.from_text, shown[0], job.to_text, shown[1]))
        errors = _Run()
        errors.log = log
        # With several jobs, a job's console ERROR line names it; one job's is as ever.
        if len(jobs) > 1:
            errors.job_name = job.name
        todo.append(_JobRun(job, log, shown, errors, real_log=log))
    if args.held is not None:
        # join's and create's held lines show nothing until the sync ends: a sync that hangs
        # or is killed must not look like a silent command
        _say("  syncing with %s …" % jobs[0].ssh)
    if args.repeat is not None:
        return _repeat(args, run, todo)
    locks = []
    try:
        # Every lock before the first connection, in the order given. They never block, so
        # the order can't deadlock; one busy job and none runs.
        for jr in todo:
            run.log = jr.log
            try:
                locks.append(state.lock(jr.name))
            except VCharonError as e:
                if e.code == "busy":
                    for other in todo:
                        if other is not jr:
                            other.log.info("not run: %s" % e.message)
                raise
        mark = started
        conn = _Conn()
        with contextlib.ExitStack() as stack:
            for i, jr in enumerate(todo):
                if conn.broke is not None:
                    _skip(args, jr, conn)
                    continue
                run.log = jr.log
                jr.started = mark
                _one_job(args, jr, conn, stack, last=i == len(todo) - 1)
                mark = time.monotonic()
    finally:
        for lock in locks:
            lock.release()
    code = next((jr.code for jr in todo if jr.code), 0)
    if len(todo) == 1:
        return code
    failed = sum(1 for jr in todo if jr.status == "failed")
    skipped = sum(1 for jr in todo if jr.status == "skipped")
    total = time.monotonic() - started
    if not failed and not skipped:
        line = "OK  %d jobs  (%.1f s)" % (len(todo), total)
    else:
        line = ("FAILED  %d of %d jobs failed%s  (%.1f s)"
                % (failed, len(todo), ", %d skipped" % skipped if skipped else "", total))
    _console(args)(line)
    return code


def _skip(args, jr, conn):
    """A job whose connection broke under an earlier job: one line, and the exit code of the
    failure that broke it."""
    line = "vcharon: %s  skipped: the connection to %s broke" % (jr.name, jr.job.ssh)
    _console(args)(line)
    jr.log.info(line)
    jr.status = "skipped"
    jr.code = conn.broke.exit_code if isinstance(conn.broke, VCharonError) else 1


# --- vcharon sync --repeat ---

# The opener's log gets one line this often, in seconds, while rounds have nothing to do.
REPEAT_SUMMARY = 600


def _repeat_flags(args, run):
    """--repeat's refusals of the flags it can't go with, before the config loads."""
    for flag, on in (("--full", args.full), ("--dry-run", args.dry_run)):
        if on:
            raise VCharonError("config", "--repeat doesn't go with %s" % flag,
                               hint="leave one of them out")


def _watch_stdin(ended):
    """Sets ended at end of file on stdin, on every OS: a thread that only reads, and drops
    what it reads. The watcher stops its child by closing the child's stdin. It reads the raw
    fd, never sys.stdin.buffer: a thread blocked in a BufferedReader holds its lock, and an
    exit while stdin is still open (a broken connection, a Ctrl-C) then aborts at interpreter
    shutdown ("Fatal Python error: _enter_buffered_busy"), on stderr, which the watcher would
    take for a new error."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        # no stdin at all (pythonw), or one without a descriptor: as good as its end
        ended.set()
        return

    def read():
        try:
            while os.read(fd, 65536):
                pass
        except OSError:
            # closed: as good as its end
            pass
        ended.set()

    threading.Thread(target=read, name="vcharon-stdin", daemon=True).start()


def _wait_or_end(ended, seconds):
    """Waits seconds, or until stdin ends; True if it ended. Short waits, so a Ctrl-C gets
    through on Windows too."""
    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if ended.is_set():
            return True
        if left <= 0:
            return False
        ended.wait(min(left, 0.5))


class _Tally:
    """The rounds since the opener's last summary line, and how many had nothing to do."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.since = time.monotonic()
        self.since_text = time.strftime("%Y-%m-%d %H:%M:%S")
        self.rounds = self.idle = 0


def _repeat(args, run, todo):
    """vcharon sync C --repeat SECONDS: one session, opened by the first round, kept for
    every round; rounds until stdin ends (exit 0, after the round under way) or a round's
    error breaks the connection (exit with that round's code). Between rounds it waits
    SECONDS from the end of a round."""
    if watch_mod.WATCH_ENV in os.environ:
        # a watcher's own streaming child: the watcher stops it (stdin, then SIGTERM) and
        # tells a SIGTERM from outside by its silent exit (DESIGN, "The watcher in a
        # channel"), so neither signal prints an EXIT line here
        return _repeat_rounds(args, run, todo, _say)
    say = run.noting_exit(_say)
    with run.stoppable(say):
        return _repeat_rounds(args, run, todo, say)


def _repeat_rounds(args, run, todo, say):
    """_repeat's rounds; say prints its EXIT lines."""
    dog = run.watchdog or run.watch_code(say)
    ended = threading.Event()
    _watch_stdin(ended)
    conn = _Conn()
    conn.watchdog = dog
    tally = _Tally()
    run.log = todo[0].real_log
    with contextlib.ExitStack() as stack:
        while True:
            if dog.changed():
                # before any other work: the code that would do it may be another now
                run.log.info("repeat: vcharon changed under it; exiting with %d"
                             % install.EXIT_UPDATED)
                run.updated_line()
                return install.EXIT_UPDATED
            if run.orphaned("repeat"):
                run.orphaned_line()
                return install.EXIT_ORPHANED
            if conn.session is not None:
                conn.session.start_round()
            code, idle = _round(args, todo, conn, stack)
            opener = conn.opener.real_log if conn.opener is not None else todo[0].real_log
            if conn.session is not None:
                conn.session.end_round()
                # the round's logs are flushed or dropped: what the helper says between rounds
                # goes to the opener's own log
                conn.session.helper_log = opener
            run.log = opener
            tally.rounds += 1
            tally.idle += 1 if idle else 0
            if time.monotonic() - tally.since >= REPEAT_SUMMARY:
                opener.info("repeat: %d rounds since %s, nothing to do in %d"
                            % (tally.rounds, tally.since_text, tally.idle))
                tally.reset()
            if conn.broke is not None:
                # It never connects again by itself: the watcher starts it again.
                opener.info("repeat: the connection broke; exiting with %d" % code)
                return code
            if _wait_or_end(ended, args.repeat):
                opener.info("repeat: stdin ended; stopping after %d rounds since %s"
                            % (tally.rounds, tally.since_text))
                # the stack says bye and stops ssh
                return 0


def _round(args, todo, conn, stack):
    """One round of --repeat: each job once, in the order given, each under its lock taken
    for this job and round only (a busy job sits this round out). The round's lines go to
    stdout at its end: each failed job's error block, then ROUND <code>, the code a sync
    of the jobs would exit with (2 for a busy job when nothing failed). A job's log gets its
    lines only for a round that planned something, saved its state, failed or was busy.
    Returns (the code, whether every job ran and had nothing to do)."""
    out = []
    idle = True
    for jr in todo:
        jr.log = HeldLog(jr.real_log)
        jr.errors = _Run()
        jr.errors.log = jr.log
        jr.errors.out = out
        # named, as with several jobs: the watcher's error keys read the name
        jr.errors.job_name = jr.name
        jr.status, jr.code, jr.saved_state = None, 0, False
        try:
            planned = _repeat_one(args, jr, conn, stack)
        except BaseException:
            # a Ctrl-C: what the job logged so far stays
            jr.log.flush()
            raise
        if jr.status != "ok" or planned or jr.saved_state:
            jr.log.flush()
            idle = False
    busy = any(jr.status == "busy" for jr in todo)
    code = next((jr.code for jr in todo if jr.code and jr.status != "busy"), 2 if busy else 0)
    out.append("ROUND %d" % code)
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()
    return code, idle


def _repeat_one(args, jr, conn, stack):
    """One job of a --repeat round: its lock, then _run_one. Prints nothing for a good job;
    a failed one's error block goes to the round's lines. Returns whether its plan had
    entries."""
    if conn.broke is not None:
        line = "vcharon: %s  skipped: the connection to %s broke" % (jr.name, jr.job.ssh)
        jr.errors.out.append(line)
        jr.log.info(line)
        jr.status = "skipped"
        jr.code = conn.broke.exit_code if isinstance(conn.broke, VCharonError) else 1
        return False
    try:
        lock = state.lock(jr.name)
    except VCharonError as e:
        jr.code = e.exit_code
        if e.code == "busy":
            # the agent's own run of the job holds it: this round only
            jr.status = "busy"
            jr.log.info("not run this round: %s" % e.message)
        else:
            jr.status = "failed"
            # its path names the job (DESIGN, "Command line")
            jr.errors.job_name = None
            jr.errors.show_error(e)
        return False
    jr.started = time.monotonic()
    try:
        eng, done = _run_one(args, jr, conn, stack)
    except BrokenPipeError:
        # stdout's reader is gone: no job's error, the whole command's (_guarded)
        raise
    except Exception as e:
        if (not isinstance(e, VCharonError) and conn.watchdog is not None
                and conn.watchdog.after_crash()):
            # maybe a read of the swapped binary: _guarded's EXIT updated, not a round's error
            raise
        if conn.session is not None and not _still_usable(conn.session, e):
            conn.broke = e
        jr.status = "failed"
        if isinstance(e, VCharonError):
            jr.errors.show_error(e)
            jr.code = e.exit_code
        else:
            jr.errors.log_line("error", traceback.format_exc())
            jr.errors.show_error(VCharonError("internal", "%s: %s" % (type(e).__name__, e)),
                                 logged=True)
            jr.code = 1
        return True
    finally:
        lock.release()
    jr.status = "ok"
    _commit_notes(jr.log.info, eng, done)
    jr.log.info("OK  %d written, %d deleted  (%.1f s)"
                % (engine.written_files(eng.plan, done), done.deleted,
                   time.monotonic() - jr.started))
    return bool(eng.plan.entries)


def _still_usable(session, err):
    """Whether the next job may use the session after err: not after a protocol error, a
    kill or a lost connection (the helper may be out of step, or gone)."""
    if isinstance(err, VCharonError) and err.code in ("protocol", "timeout", "lost"):
        return False
    return session.usable


def _one_job(args, jr, conn, stack, last):
    """Runs one job, closes the connection when no later job will use it, and
    only then prints how the job ended, as a one-job run always has."""
    error = trace = None
    try:
        eng, done = _run_one(args, jr, conn, stack)
    except BrokenPipeError:
        # stdout's reader is gone: no job's error, the whole command's (_guarded)
        raise
    except Exception as e:  # noqa: BLE001
        error, trace = e, traceback.format_exc()
        if conn.session is not None and not _still_usable(conn.session, e):
            conn.broke = e
    if last or conn.broke is not None:
        # Says bye (when the helper is in step) and stops ssh.
        stack.close()
    if error is not None:
        jr.status = "failed"
        if args.held is not None:
            args.held.release()
        if isinstance(error, VCharonError):
            jr.errors.show_error(error)
            jr.code = error.exit_code
        else:
            jr.errors.log_line("error", trace)
            jr.errors.show_error(VCharonError("internal", "%s: %s" % (type(error).__name__, error)),
                                 logged=True)
            jr.code = 1
        return

    console = _console(args)

    def say(line):
        console(line)
        jr.log.info(line)

    jr.status = "ok"
    total = time.monotonic() - jr.started
    if done is None:
        jr.ok_line = "OK  dry run, nothing changed  (%.1f s)" % total
        say(jr.ok_line)
        return
    _commit_notes(say, eng, done)
    written = engine.written_files(eng.plan, done)
    jr.ok_line = "OK  %d written, %d deleted  (%.1f s)" % (written, done.deleted, total)
    say(jr.ok_line)
    if args.held is not None:
        args.held.done.append((jr.name.rsplit(".", 1)[-1], written, done.deleted))


def _session(conn, stack, jr):
    """The sync's session: opened by the first job that needs it, whose log gets the
    session's own lines. A later job logs where they are, and starts with job.reset: the
    helper then holds nothing of the job before it."""
    if conn.session is None:
        # the job's own log, not a --repeat round's HeldLog: the session outlives the round
        conn.session = stack.enter_context(ssh.Session(jr.job.settings, jr.job.ssh,
                                                       jr.real_log or jr.log))
        conn.opener = jr
        conn.session.helper_log = jr.log
        # once per connection
        hello = conn.session.open()
        conn.machine = hello.get("machine")
        conn.os = hello.get("os")
        return conn.session
    jr.log.info("shares the connection of %s (run %s)"
                % (conn.opener.name, conn.opener.log.run_id))
    remote.reset(conn.session)
    # After the reset: what the helper says while it closes the job before stays in that
    # job's log; what it says from here on is this job's.
    conn.session.helper_log = jr.log
    return conn.session


def _own_folder(job, log, dry_run):
    """A mailbox job's folders on the client, <local> and <local>/<me>: up's path source
    never creates its own path. Only while up has sent nothing: a folder that vanished after
    that must not come back empty, or up's prune would empty the server's copy. Down checks it
    too, so that a run of down alone reports it as well; down itself never plans <me>/,
    so it wouldn't make the folder again. Nothing is made in a dry run."""
    path = plugin.Ctx("local").resolve(job.mailbox.own_folder, "mailbox.local")
    up = job.mailbox.section + ".up"
    try:
        saved = state.load(up)
    except VCharonError:
        # up's own run reports that
        saved = None
    sent = saved.source.get("sent") if saved is not None and isinstance(saved.source,
                                                                          dict) else None
    if os.path.isdir(path):
        # A channel member's folder always holds MEMBER.md. Gone while up has sent it,
        # the folder was emptied or replaced (a failed restore, a stray .DS_Store in a new
        # one): up's prune would delete the server's copy, so neither job runs.
        if (job.mailbox.channel and isinstance(sent, dict) and entries.MEMBER_FILE in sent
                and fsops.missing(os.path.join(path, entries.MEMBER_FILE))):
            raise VCharonError("not_found", "the own folder %s has no %s, which %s has sent: it "
                               "was emptied or replaced" % (path, entries.MEMBER_FILE, up),
                               channel_cmd.rejoin_hint(job.mailbox.channel, job.ssh,
                                                       job.mailbox.me)
                               + "; MEMBER.md is vcharon's: to drop other files, delete them "
                               "one by one and keep it")
        return
    if sent:
        # never a reset of up: its next sync would make the folder again, empty, and a rejoin
        # is what brings the files back
        raise VCharonError("not_found", "the mailbox's own folder %s is gone, but %s has sent "
                           "files from it" % (path, up),
                           channel_cmd.rejoin_hint(job.mailbox.channel, job.ssh,
                                                   job.mailbox.me))
    if dry_run:
        log.info("a real run would create %s, the mailbox's own folder" % path)
        return
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise fsops.error(e, path)
    log.info("created %s, the mailbox's own folder" % path)


# The general hints for a name the source can't send or list speak to the owner of the whole
# source; only a folder's writer can change a mailbox folder, so _mailbox_hint swaps them.
# Compared as text, since an error from the helper arrives with its hint as text.
SOURCE_HINTS = frozenset([pathrules.UNSAFE_HINT, pathrules.COLLISION_HINT,
                          path_plugin.LINKS_HINT, path_plugin.NAME_HINT])
MAILBOX_DOWN_HINT = ("the writer of each folder named above %s; your up still runs; more: "
                     "vcharon guide errors")
# with the writer's folder, since up's paths are relative to it
MAILBOX_UP_HINT = "%s in your own folder (%s/)"
# kept in channel_cmd: a local member's watch prints it too
CHANNEL_GONE_HINT = channel_cmd.CHANNEL_GONE_HINT


def channel_gone_hint(job, code, hint):
    """CHANNEL_GONE_HINT for a channel job's not_found on up's sink root or down's source
    root, else None; vcharon sync and vcharon doctor both swap it in."""
    if job.mailbox is None or code != "not_found":
        return None
    up = job.name == job.mailbox.section + ".up"
    if hint == (stage.ROOT_HINT if up else path_plugin.MISSING_HINT):
        return CHANNEL_GONE_HINT % (job.mailbox.channel,
                                    channel_cmd.name_flags(job.mailbox.channel, job.mailbox.me))
    return None


def _mailbox_hint(e, job):
    """A mailbox job's fix line for a name its source can't send or list: what a mailbox
    writer can do, in place of the general hint. Other jobs, and other errors, keep theirs."""
    if job.mailbox is None or not isinstance(e, VCharonError):
        return
    gone = channel_gone_hint(job, e.code, e.hint)
    if gone is not None:
        e.hint = gone
        return
    up = job.name == job.mailbox.section + ".up"
    if e.code in ("unsafe_path", "collision") and e.hint in SOURCE_HINTS:
        which = "one of them" if e.code == "collision" else "it"
        what = ("remove or rename %s" if up else "removes or renames %s") % which
    elif e.code == "permission" and e.hint == path_plugin.LIST_HINT:
        what = "fix its permissions" if up else "fixes its permissions"
    else:
        return
    e.hint = MAILBOX_UP_HINT % (what, job.mailbox.me) if up else MAILBOX_DOWN_HINT % what


def _mismatch(e, name):
    """The sync's hint for every state_mismatch that names no hint of its own."""
    if isinstance(e, VCharonError) and e.code == "state_mismatch":
        e.hint = state.reset_hint(name)


def _run_one(args, jr, conn, stack):
    """One job: its state, its header line, the sync's session, the engine, the state
    saved. Returns (engine, done); done is None for a dry run."""
    job, log, shown = jr.job, jr.log, jr.shown
    name = job.name
    hint = state.reset_hint(name)
    if job.mailbox is not None:
        _own_folder(job, log, args.dry_run)
    try:
        saved = state.load(name)
    except VCharonError as e:
        _mismatch(e, name)
        raise
    fingerprint = state.fingerprint(job)
    if saved is not None and saved.fingerprint != fingerprint:
        raise VCharonError("state_mismatch", state.config_changed(job), hint)

    # --repeat prints only a round's errors and its ROUND line; the log keeps the rest
    quiet = args.repeat is not None
    console = _console(args)

    def say(line):
        if not quiet:
            console(line)
        log.info(line)

    mode = [word for word, on in (("full", args.full), ("dry run", args.dry_run)) if on]
    # a local side's home as ~; a remote side is the server's path, which the client's home
    # doesn't name (a mailbox's is spelled from ~ already)
    say("vcharon: %s  %s -> %s%s" % (name, platform.tilde(shown[0]), platform.tilde(shown[1]),
                                     "  (%s)" % ", ".join(mode) if mode else ""))
    # the sink binding, once the check has passed
    bound = []
    session = _session(conn, stack, jr)
    machine = conn.machine
    # Every job has a remote end, and its state is tied to that server (DESIGN, "hello"), which
    # must be Linux.
    state.need_linux({"os": conn.os}, job.ssh, state.LINUX_HINT % job.ssh)
    if not machine:
        raise VCharonError("state_mismatch", "the server %s has no machine id, so vcharon can't "
                           "tie the state of %s to it" % (job.ssh, name),
                           state.NO_MACHINE_HINT)

    def after_plan(p):
        if saved is not None and p.identity != saved.identity:
            raise VCharonError("state_mismatch", "the state of %s was saved for another "
                               "source: %s, now %s"
                               % (name, _dumps(saved.identity), _dumps(p.identity)), hint)

    def after_check(p, checked):
        binding = {"end": job.sink.end, "root": checked.root}
        if job.sink.end == "remote":
            binding["machine"] = machine
        # So the devbox alias pointed at another server, or a moved root, can't make
        # prune delete on the wrong machine (DESIGN, "State file").
        if saved is not None and binding != saved.sink:
            raise VCharonError("state_mismatch", "the state of %s was saved for another "
                               "target: %s, now %s"
                               % (name, _dumps(saved.sink), _dumps(binding)), hint)
        bound.append(binding)
        _summary(say, p, checked, args.dry_run, job=True, full=args.full)

    down = job.mailbox is not None and name == job.mailbox.section + ".down"
    section = job.mailbox.section if down else None
    eng = engine.Engine(session, job.source, job.sink, log, after_check=after_check,
                        after_plan=after_plan, plan_args=_down_args(down, session, section))
    # a first run, or a source that saved nothing: {}
    source_state = saved.source if saved is not None and saved.source is not None else {}
    try:
        done = eng.run(dry_run=args.dry_run, state=source_state, full=args.full)
    except Exception as e:
        _mismatch(e, name)
        _mailbox_hint(e, job)
        jr.errors.done_line = _done_line(eng)
        if bound and isinstance(eng.state_after, dict):
            _save_after_failure(name, fingerprint, eng, bound[0], log)
        if down:
            # A failed round keeps no ages, but the helper counts a seen it sent as had: the
            # next round asks for them, so a new member or pace isn't held back until the copy
            # is due.
            session.seen_at.pop(section, None)
        raise
    new = (state.State(fingerprint, eng.plan.identity, bound[0], eng.plan.state, state.now())
           if done is not None else None)
    if new is not None and quiet and _same_state(saved, new):
        # --repeat: a round that changed nothing writes nothing, or every round would fsync
        # two files
        new = None
    if new is not None:
        try:
            state.save(name, new)
        except (OSError, VCharonError) as e:
            err = fsops.error(e, state.path(name)) if isinstance(e, OSError) else e
            if down:
                # as after a failed run: this round's ages are dropped, so the next one asks
                session.seen_at.pop(section, None)
            raise VCharonError(err.code, "couldn't save the state of %s: %s"
                               % (name, err.message), SAVE_HINT)
        log.info("saved the state of %s" % name)
        jr.saved_state = True
    if done is not None and down:
        # the members this pull left out for their size: the watcher's WARN and read's note
        # come from there (with --repeat the notes reach only the log); after the state,
        # which holds those members' entries as they were
        try:
            charter_state.save_left_out(job.mailbox.section, eng.plan.notes,
                                  int(job.source.options["max_bytes"]),
                                  int(job.source.options["max_files"]))
        except OSError as e:
            log.warn("couldn't save the members left out: %s" % e)
    if down:
        if "seen" in eng.plan_reply:
            session.seen_at[section] = time.monotonic()
        _save_seen(section, eng.plan_reply, log)
    return eng, done


def _watch_args(down):
    """A channel down's more source.plan args: {"watch": the pace} when a watcher started this
    sync (watch_mod.WATCH_ENV in its environment), so the server stamps the member's last
    watched time; else None. Only a watcher's pull counts: a by-hand sync or a join's pull
    would make a member look watched."""
    pace = os.environ.get(watch_mod.WATCH_ENV) if down else None
    if pace is None or channels.parse_pace(pace) is None:
        return None
    return {"watch": pace}


def _down_args(down, session, section):
    """A channel down's more source.plan args: _watch_args', and want_seen when the copy of
    the members' ages is due to be written: the helper sends them otherwise only when a member
    or a pace changed (DESIGN, "Calls"). Due at this session's first plan of the section, then
    once SEEN_SAVE_EVERY has passed both since the last ones came and by the copy's mtime, so a
    copy that save_seen didn't write (no time moved) is asked for again, never every round.
    None when there are none."""
    args = dict(_watch_args(down) or {})
    if down:
        got = session.seen_at.get(section)
        if (got is None or (time.monotonic() - got >= charter_state.SEEN_SAVE_EVERY
                            and charter_state.seen_due(section, time.time()))):
            args["want_seen"] = True
    return args or None


def _save_seen(section, reply, log):
    """Keeps the members' last-watched ages a down plan's reply brought, for whoami and
    read --json. Best effort: a failure, or the server's own (seen_error), is only logged, never
    printed: a watcher's rounds print nothing for it."""
    if "seen_error" in reply:
        log.warn("the server couldn't give the members' last-watched times: %s"
                 % pathrules.printable(str(reply["seen_error"])[:300]))
    if "seen" not in reply:
        return
    try:
        charter_state.save_seen(section, reply["seen"], time.time())
    except (OSError, TypeError, ValueError) as e:
        log.warn("couldn't keep the members' last-watched times: %s" % e)


def _same_state(saved, new):
    """Whether new holds what saved does, its saved stamp aside. Compared as JSON, as the
    file holds them: a tuple in a plan's state and the list it's read back as are the same.
    Key order counts, as in a state's reference (proto.state_ref): a state the source gave
    back in another order (a member folder over the limits has its entries moved to the end)
    is written once, so the next round sends it by reference and gets it back as one; else
    every round would carry it whole."""
    if saved is None:
        return False

    def text(st):
        return json.dumps([st.fingerprint, st.identity, st.sink, st.source],
                          ensure_ascii=False)

    return text(saved) == text(new)


def _save_after_failure(name, fingerprint, eng, binding, log):
    """Saves what a commit that failed partway wrote (DESIGN, "The path source"). A failure here is
    only logged: it never replaces the run's own error."""
    try:
        state.save(name, state.State(fingerprint, eng.plan.identity, binding, eng.state_after,
                                     state.now()))
    except Exception as e:  # noqa: BLE001
        log.warn("couldn't save the state of %s after the failure: %s" % (name, e))
        return
    log.info("saved the state of %s: what the failed commit wrote" % name)


def _show_state(name, st, why=None):
    """What a reset prints first: a summary, since a mirror's state can hold 100,000 paths."""
    if why is not None:
        print("vcharon: the state of %s can't be read (%s)" % (name, why))
        return
    if st is None:
        print("vcharon: no state for %s" % name)
        return
    print("vcharon: state of %s  (%s)" % (name, platform.tilde(state.path(name))))
    print("  saved     %s" % st.saved)
    # JSON, with the home as ~ where it shows as it is (not in Windows's escaped backslashes)
    print("  source    %s" % ("none" if st.identity is None
                              else platform.tilde_text(_dumps(st.identity))))
    print("  target    %s" % platform.tilde_text(_dumps(st.sink)))
    source = st.source
    if isinstance(source, dict) and set(source) == {"sent"} and isinstance(source["sent"], dict):
        values = list(source["sent"].values())
        files = sum(1 for v in values if isinstance(v, list))
        dirs = sum(1 for v in values if v == "d")
        print("  sent      %s, %s" % (_counted(files, "file"), _counted(dirs, "dir")))


def _reset(name, verbose, run):
    """vcharon sync C --reset up|down: forgets the job's saved state, under its lock, so its
    next sync plans from nothing (with --full, by content)."""
    log = run.log = Log(os.path.join(platform.log_dir(), name + ".log"), console=verbose)
    lock = state.lock(name)
    try:
        st, why = state.read(name)
        _show_state(name, st, why)
        p = state.path(name)
        if state.remove(name):
            print("removed %s" % platform.tilde(p))
            log.info("state reset: removed %s" % p)
        else:
            log.info("state reset: there was no state at %s" % p)
    finally:
        lock.release()
    return 0
