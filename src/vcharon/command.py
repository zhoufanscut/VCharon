"""The command context, console errors and signals shared by the CLI and sync runner."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import traceback

from . import config, install, platform, proto
from .log import Log
from .mailbox import watch as watch_mod
from .proto import VCharonError

# a Ctrl-C's exit code, and a SIGTERM's in watch and sync --repeat (_Run.stoppable): the
# shell's 128 + the signal's number
EXIT_INTERRUPTED = 130
EXIT_TERMINATED = 143

def _guarded(fn, run):
    """fn()'s exit code; an error it raises is shown, as every command shows one, and gives
    its exit code."""
    try:
        return fn()
    except VCharonError as e:
        run.show_error(e)
        return e.exit_code
    except BrokenPipeError as e:
        # stdout's reader went away (vcharon doctor | head -3): no ERROR line, and nothing
        # more to say; stdout goes to devnull so Python's own flush at exit can't fail too.
        # Logged, in case the pipe was another one.
        run.log_line("error", "stdout closed: %s" % e, create=True)
        _stdout_to_devnull()
        return 1
    except KeyboardInterrupt:
        # The session has killed ssh on its way out; a watch has stopped its sync child. A
        # SIGTERM to watch or sync --repeat arrives as this too (_Run.stoppable), with 143.
        code = run.stop_code or EXIT_INTERRUPTED
        run.log_line("error", "interrupted" if code == EXIT_INTERRUPTED
                     else "stopped by SIGTERM")
        sys.stderr.write("vcharon: interrupted\n")
        if run.stop_say is not None and not run.exit_said:
            # their last line, as for every other way they end; not after one they printed
            # already (a stop that came while the watch stopped its child after EXIT change)
            try:
                run.stop_say(watch_mod.exit_line("interrupted", code))
            except (OSError, ValueError):
                # a reader that is gone (a closed pipe) gets none; the line left in stdout's
                # buffer must not fail Python's own flush at exit, which would change the code
                _stdout_to_devnull()
        return code
    except Exception as e:  # noqa: BLE001
        if run.watchdog is not None and run.watchdog.after_crash():
            # a long-running command's binary was swapped under it: the error may be a read
            # of the new file at the old offsets (DESIGN, "Running watchers"). Nothing that
            # could import more: no traceback.
            run.log_line("info", "vcharon changed under this process (%s: %s); exiting with %d"
                         % (type(e).__name__, e, install.EXIT_UPDATED), create=True)
            run.updated_line()
            return install.EXIT_UPDATED
        run.log_line("error", traceback.format_exc(), create=True)
        run.show_error(VCharonError("internal", "%s: %s" % (type(e).__name__, e)),
                       logged=True)
        return 1


def _stdout_to_devnull():
    """Points stdout's file descriptor at devnull (the Python docs' recipe for a closed
    pipe). A stdout without one (a test's) is left as it is."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, ValueError, OSError):
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


class _Run:
    """What the error path needs to know about the run so far."""

    def __init__(self):
        # None until a command opens its log; a usage error writes no log.
        self.log = None
        # what a failed commit did, as the error block shows it; None if nothing
        self.done_line = None
        # the job whose error this is, in a sync of a section's two jobs: its console ERROR
        # line names it; None for one job and every other command
        self.job_name = None
        # where the error block goes: stderr, or with vcharon sync --repeat the round's lines,
        # which go to stdout
        self.out = None
        # a long-running command's check that its code didn't change (install.Watchdog), and
        # how it says EXIT updated; None for the others
        self.watchdog = None
        self.updated_line = None
        self.orphaned_line = None
        # a Windows binary's bootloader (install.exit_with_parent); None elsewhere
        self.parent = None
        # watch and sync --repeat: how they print EXIT interrupted, and the exit code a
        # SIGTERM set (stoppable); None for the others, and before a SIGTERM
        self.stop_say = None
        self.stop_code = None
        # whether this run printed an EXIT line (noting_exit)
        self.exit_said = False

    def noting_exit(self, out, skip=0):
        """out, noting in exit_said each line that is an EXIT line once its first skip
        characters (a watcher's time and its space) are left out."""
        def say(line):
            if line[skip:].startswith(watch_mod.EXIT_PREFIX):
                self.exit_said = True
            out(line)
        return say

    @contextlib.contextmanager
    def stoppable(self, say):
        """While watch or sync --repeat runs: a Ctrl-C or, on POSIX, a SIGTERM ends it with
        EXIT interrupted, printed by say once the KeyboardInterrupt has unwound the command
        (its sync child stopped, its lock released) (_guarded): exit 130 for a Ctrl-C, 143 for
        a SIGTERM. So an agent whose tool stops the watcher sees its last line, and output
        that just ends means a kill nothing can catch (DESIGN, "The watcher in a channel").
        The SIGTERM handler is this process's only while the command runs; Windows has no
        SIGTERM to catch."""
        self.stop_say = say
        installed = (os.name != "nt"
                     and threading.current_thread() is threading.main_thread())
        if installed:
            before = signal.signal(signal.SIGTERM, self._terminated)
        try:
            yield
        finally:
            # after a SIGTERM, SIG_IGN stays: a second one must not cut short the EXIT line
            # (_guarded) or the exit after it
            if installed and self.stop_code is None:
                signal.signal(signal.SIGTERM, signal.SIG_DFL if before is None else before)

    def _terminated(self, signum, frame):
        # once: further SIGTERMs (a binary's bootloader passes one on, and a user may end
        # both of its processes) are ignored, so they can't cut short the stop of the child
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.stop_code = EXIT_TERMINATED
        raise KeyboardInterrupt

    def watch_code(self, say):
        """Starts the watchdog of a long-running command (watch, sync --repeat); say prints
        its EXIT updated and EXIT orphaned lines. Returns the watchdog."""
        # Before the start the watchdog marks: how fix lines spell vcharon (platform.runnable)
        # reads sysconfig's data when an entry point named vcharon is on PATH, and Python
        # imports that data at its first use (_sysconfigdata_*; _osx_support on macOS). A
        # long-running command imports nothing after its start (DESIGN, "Running watchers");
        # sysconfig keeps what it read, so later fix lines import nothing.
        platform.self_command()
        self.watchdog = install.Watchdog(parent=self.parent)
        self.updated_line = lambda: say(watch_mod.exit_line("updated", install.EXIT_UPDATED))
        self.orphaned_line = lambda: say(watch_mod.exit_line("orphaned", install.EXIT_ORPHANED))
        return self.watchdog

    def orphaned(self, what):
        """Whether the watchdog's binary lost its bootloader parent; logs one line when it
        did. what: the command, for the log."""
        if self.watchdog is None or not self.watchdog.orphaned():
            return False
        self.log_line("info", "%s: the process that started this one (pid %d) is gone; "
                      "exiting with %d" % (what, self.watchdog.parent.pid,
                                           install.EXIT_ORPHANED), create=True)
        return True

    def parent_gone(self, parent):
        """What a Windows binary does when its bootloader (parent, an install.Parent) is
        gone, from the thread that waits on it (install.exit_with_parent), before it exits 15:
        the log line, and in watch and sync --repeat their EXIT orphaned."""
        self.log_line("info", "the process that started this one (pid %d) is gone; exiting "
                      "with %d" % (parent.pid, install.EXIT_ORPHANED), create=True)
        if self.orphaned_line is not None:
            self.orphaned_line()

    def log_line(self, level, msg, create=False):
        if self.log is None and create:
            with contextlib.suppress(Exception):
                self.log = Log(os.path.join(platform.log_dir(), "vcharon.log"))
        if self.log is not None:
            self.log.write(level, msg)

    def show_error(self, err, logged=False):
        hint = err.hint
        if not hint and err.code == "internal":
            hint = "this is a bug in vcharon; see the log"
        if not logged:
            self.log_line("error", "%s: %s" % (err.code, err.message))
            if hint:
                self.log_line("error", "fix: %s" % hint)
            if err.detail:
                self.log_line("error", err.detail)
            if self.done_line:
                self.log_line("error", self.done_line.strip())
        # The log is the job's own, so only the console line names the job. The paths in the
        # message with the home as ~: agents quote this line into channel entries; the log
        # keeps them whole. The fix line keeps its commands whole, as they must run as
        # printed; only a path its text names in prose shows the home as ~ (proto.Hint). A
        # server's error arrives with the server's home as server:~ already
        # (helper.HelperConn).
        lines = ["ERROR %s%s: %s" % ("%s: " % self.job_name if self.job_name else "", err.code,
                                     platform.tilde_text(err.message))]
        lines += ["  | %s" % platform.tilde_text(line) for line in err.tail]
        if self.done_line:
            lines.append(self.done_line)
        if hint:
            # as this box runs vcharon; the log keeps the plain text, read later by a
            # person, maybe on another box
            lines.append("  fix: %s" % platform.runnable(proto.shown_hint(hint)))
        if self.log is not None:
            lines.append("  log: %s" % platform.tilde(self.log.path))
        if self.out is not None:
            self.out.extend(lines)
            return
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()


def _say(line):
    print(line)
    # Show progress before the next slow step, even when stdout is a pipe.
    sys.stdout.flush()


def load_config():
    """config.load, and each skipped section's line in vcharon.log: never on stderr, whose
    first line a remote member's watcher takes for the round's error."""
    cfg = config.load()
    if cfg.skipped:
        log = Log(os.path.join(platform.log_dir(), "vcharon.log"))
        for skip in cfg.skipped:
            log.info(skip.line)
    return cfg
