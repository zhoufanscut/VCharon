"""Log files: one line per event, written by the main thread and the reader threads."""

from __future__ import annotations

import os
import sys
import threading
import time

# A log rolls over to <path>.1 at this size, keeping one old file.
ROLL_BYTES = 1 << 20
LEVELS = ("debug", "info", "warn", "error")


def new_run_id():
    return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), os.urandom(3).hex())


class Log:
    """Appends to one log file. Logging never raises: it must never fail a run."""

    def __init__(self, path, run_id=None, console=False):
        self.path = path
        self.run_id = run_id or new_run_id()
        self.console = console
        self._lock = threading.Lock()
        self._made_dir = False

    def write(self, level, msg):
        self.emit(self.format(level, msg))

    def format(self, level, msg):
        """The lines write() appends for one message, stamped now."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = str(msg).splitlines() or [""]
        return "".join("%s  %s  %s  %s\n" % (stamp, self.run_id, level, line)
                       for line in lines)

    def emit(self, text):
        """Appends lines that format() made (and echoes them with console)."""
        with self._lock:
            if self.console:
                try:
                    sys.stderr.write(text)
                    sys.stderr.flush()
                except (OSError, ValueError):
                    pass
            self._append(text)

    def debug(self, msg):
        self.write("debug", msg)

    def info(self, msg):
        self.write("info", msg)

    def warn(self, msg):
        self.write("warn", msg)

    def error(self, msg):
        self.write("error", msg)

    def _append(self, text):
        try:
            if not self._made_dir:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                self._made_dir = True
            self._roll()
            # Open and close for each write: short-lived, and another vcharon may roll the file.
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except OSError:
            pass

    def _roll(self):
        try:
            if os.path.getsize(self.path) >= ROLL_BYTES:
                os.replace(self.path, self.path + ".1")
        except OSError:
            # Missing, or on Windows another vcharon has it open: keep appending.
            pass


class HeldLog(Log):
    """A Log whose lines wait in memory until flush() hands them to its target; dropped with
    the object otherwise. vcharon run --repeat logs a round only when it did something, so a
    quiet watch doesn't roll the log every half hour (DESIGN §14 M15)."""

    def __init__(self, target):
        Log.__init__(self, target.path, run_id=target.run_id)
        self.target = target
        self._held = []

    def emit(self, text):
        with self._lock:
            self._held.append(text)

    def flush(self):
        with self._lock:
            text, self._held = "".join(self._held), []
        if text:
            self.target.emit(text)
