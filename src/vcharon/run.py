"""The engine: steps 5-8 of DESIGN §4, one piece of code for either direction; client."""

from __future__ import annotations

import dataclasses
import time

from . import pathrules, plugin, remote, stage
from .proto import VCharonError

# At debug level, the log lists this many put paths and this many delete paths.
LOG_PATHS = 50


@dataclasses.dataclass
class Side:
    """One side of a run."""

    end: str          # "local" or "remote"
    plugin: str
    options: dict     # option -> raw string


def _files(p):
    return [e for e in p.entries if e.op == "put" and e.kind == "file"]


class Engine:
    """Steps 5-8 of DESIGN §4, for either direction: plan, check, transfer, commit. The side
    on the remote end is a proxy; the other one runs here."""

    def __init__(self, session, source, sink, log, after_check=None, after_plan=None):
        # session: an open ssh.Session. after_plan(plan) runs right after the plan, before the
        # check: it may refuse a plan the caller never asked for. after_check(plan, checked)
        # runs after the check, before any bytes move.
        if [source.end, sink.end] not in (["local", "remote"], ["remote", "local"]):
            raise VCharonError("internal", "exactly one side of a run is remote, not the %s "
                               "source and the %s sink" % (source.end, sink.end))
        self.session = session
        self.source_side = source
        self.sink_side = sink
        self.log = log
        self.after_check = after_check
        self.after_plan = after_plan
        # filled in as the run goes
        self.plan = self.checked = self.done = None
        # after a commit that failed partway in a run that keeps state: what the source wants
        # saved (DESIGN §9.2), or None
        self.state_after = None

    def _make(self, side, role):
        if side.end == "remote":
            cls = remote.RemoteSource if role == "source" else remote.RemoteSink
            # Its warnings go to this job's log, not the session's (ferry run a b).
            return cls(self.session, side.plugin, side.options, self.log)
        log, prefix = self.log, role + ": "
        # No tick: local work never counts as idle (DESIGN §6.3).
        ctx = plugin.Ctx("local", log=lambda msg: log.info("%s%s" % (prefix, msg)))
        return plugin.make("local", side.plugin, role, side.options, ctx)

    def run(self, dry_run=False, state=None, full=False):
        """Returns the commit's stage.Done, or None for a dry run. state: the source's saved
        state ({} if none), or None when the caller keeps no state (a one-off pull). full: the
        source plans everything, with hashes, and the sink skips what it holds (DESIGN
        §10.5)."""
        local = role = None
        try:
            source = self._make(self.source_side, "source")
            if not source.remote:
                local, role = source, "source"
            sink = self._make(self.sink_side, "sink")
            if not sink.remote:
                local, role = sink, "sink"
            try:
                return self._run(source, sink, dry_run, state, full)
            except Exception:
                # A Ctrl-C isn't caught here: the session kills ssh, and the helper cleans up.
                self._abort(sink)
                raise
        finally:
            # A local sink that didn't commit aborts as it closes.
            if local is not None:
                self._close(local, role)

    def _run(self, source, sink, dry_run, state, full):
        log = self.log
        self.plan = p = source.plan(state, full=full)
        self._log_plan(p)
        if self.after_plan is not None:
            self.after_plan(p)
        self.checked = checked = sink.check(p)
        # The sink may skip only what the plan hashed (DESIGN §10.5).
        for i in checked.have:
            e = p.entries[i] if 0 <= i < len(p.entries) else None
            if e is None or e.op != "put" or e.kind != "file" or e.sha256 is None:
                raise VCharonError("protocol", "the sink says entry %d is already at the target, "
                                   "but the plan doesn't hash it" % i)
        log.info("check: root %s, %d deletes, %d already there"
                 % (checked.root, checked.deletes, len(checked.have)))
        for note in checked.notes:
            log.info("check note: %s" % note)
        if self.after_check is not None:
            self.after_check(p, checked)
        if dry_run:
            return None
        if not p.entries:
            # Nothing to apply: no transfer and no commit, so a run with nothing to do is one
            # round trip.
            self.done = done = stage.Done([], 0, [])
            return done
        have = set(checked.have)
        files = [i for i, e in enumerate(p.entries)
                 if e.op == "put" and e.kind == "file" and i not in have]
        # A plan with no file puts to move has nothing to stream: one round trip fewer.
        if files:
            started = time.monotonic()
            if source.remote:
                source.send(files, sink.stage)
            else:
                sink.receive(files, source.open)
            log.info("transfer: %d files, %d bytes in %.2f s"
                     % (len(files), sum(p.entries[i].size for i in files),
                        time.monotonic() - started))
        try:
            self.done = done = sink.commit()
        except BaseException as e:
            self.done = done = sink.done
            if (isinstance(e, Exception) and state is not None
                    and (done.written or done.deletes_done)):
                self._state_after(source, done)
            raise
        log.info("commit: %d written, %d deleted" % (written_files(p, done), done.deleted))
        for note in done.notes:
            log.info("commit note: %s" % note)
        return done

    def _log_plan(self, p):
        log = self.log
        files = _files(p)
        puts = [e for e in p.entries if e.op == "put"]
        deletes = [e for e in p.entries if e.op == "delete"]
        log.info("plan: %d files, %d dirs, %d deletes, %d bytes"
                 % (len(files), len(puts) - len(files), len(deletes),
                    sum(e.size for e in files)))
        for note in p.notes:
            log.info("plan note: %s" % note)
        for e in puts[:LOG_PATHS]:
            log.debug("put %s%s" % (pathrules.show(e.path), "/" if e.kind == "dir" else ""))
        for e in deletes[:LOG_PATHS]:
            log.debug("delete %s%s" % (pathrules.show(e.path), " (tree)" if e.tree else ""))

    def _state_after(self, source, done):
        """What the source wants saved after the commit failed, having done what done says.
        Never raises: it must not replace the commit's error."""
        try:
            result = source.state_after(list(done.written), list(done.deletes_done))
        except Exception as e:
            self.log.warn("state_after failed: %s" % e)
            return
        if isinstance(result, dict):
            self.state_after = result
            self.log.info("state_after: %d written, %d deleted"
                          % (len(done.written), len(done.deletes_done)))
        elif result is None:
            self.log.info("state_after: the source saves nothing")
        else:
            self.log.warn("state_after returned %s, not an object; nothing is saved"
                          % type(result).__name__)

    def _abort(self, sink):
        try:
            sink.abort()
        except Exception as e:
            # never in place of the error that made the run abort
            self.log.warn("aborting the sink failed: %s" % e)

    def _close(self, obj, role):
        try:
            obj.close()
        except Exception as e:
            self.log.warn("closing the local %s failed: %s" % (role, e))


def written_files(p, done):
    """How many file puts the commit wrote: what done.written holds, less the plan's
    directories (DESIGN §13: "put 12 files, 2 dirs" gives "OK 12 written")."""
    dirs = {e.path for e in p.entries if e.op == "put" and e.kind == "dir"}
    return sum(1 for path in done.written if path not in dirs)
