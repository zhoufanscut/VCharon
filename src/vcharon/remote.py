"""The proxies for a source or sink in the helper (DESIGN, "How a sync works"); client."""

from __future__ import annotations

import functools

from . import plan as planmod
from . import proto, stage
from .proto import VCharonError


class RemoteSource:
    """A source that runs in the helper: two round trips, plan and send; a third,
    state_after, only after a commit that failed partway."""

    remote = True

    def __init__(self, session, name, options, log=None):
        # options: the raw strings; the helper converts and checks them itself. log: the
        # running job's own; the session's belongs to the job that opened it (a sync's up, of
        # up and down).
        self.session = session
        self.log = log if log is not None else session.log
        self.name = name
        self.options = options

    def plan(self, state, full=False):
        result = self.session.call("source.plan", {"plugin": self.name,
                                                   "options": self.options, "state": state,
                                                   "full": bool(full)})
        return planmod.from_json(result)

    def send(self, indexes, stage_file):
        """Downloads the file puts at indexes, in that order; stage_file(index, reader) takes
        each one."""
        indexes = list(indexes)
        self.session.call("source.send", {"indexes": indexes}, receive=(indexes, stage_file))

    def state_after(self, written, deleted):
        """The state the source wants saved after a commit that failed having written these
        paths and got through these deletes; None saves nothing."""
        if not self.session.usable:
            self.log.warn("source.state_after skipped: the connection isn't usable")
            return None
        result = self.session.call("source.state_after", {"written": list(written),
                                                          "deleted": list(deleted)})
        if (not isinstance(result, dict) or set(result) != {"state"}
                or not isinstance(result["state"], (dict, type(None)))):
            raise VCharonError("protocol", "a malformed source.state_after result: %s"
                               % proto.quote(result))
        return result["state"]

    def close(self):
        # nothing: the helper closes its own source when it exits
        pass


class RemoteSink:
    """A sink that runs in the helper: three round trips, check, receive and commit."""

    remote = True

    def __init__(self, session, name, options, log=None):
        self.session = session
        self.log = log if log is not None else session.log
        self.name = name
        self.options = options
        # what the commit did so far; after a failed commit, from its err reply
        self.done = stage.Done([], 0, [])

    def check(self, plan):
        obj = planmod.to_json(plan)
        # The sink never reads the source's state, which holds every path sent so far; the
        # identity stays (the file sink reads it).
        obj["state"] = None
        result = self.session.call("sink.check", {"plugin": self.name, "options": self.options,
                                                  "plan": obj})
        return stage.checked_from_json(result)

    def receive(self, indexes, opener):
        """Uploads the file puts at indexes, in that order; opener(index) opens each one when
        its turn comes."""
        indexes = list(indexes)
        result = self.session.call("sink.receive", {"indexes": indexes},
                                   upload=[(i, functools.partial(opener, i)) for i in indexes])
        if (not isinstance(result, dict) or set(result) != {"staged"}
                or not proto.is_id(result["staged"]) or result["staged"] != len(indexes)):
            raise VCharonError("protocol", "a malformed sink.receive result: %s"
                               % proto.quote(result))

    def commit(self):
        try:
            result = self.session.call("sink.commit")
        except VCharonError as e:
            self.done = self._done_of(e)
            raise
        self.done = stage.done_from_json(result)
        return self.done

    def _done_of(self, err):
        """What a failed commit did, from its err reply; it never replaces the commit's own
        error."""
        done = err.reply.get("done") if isinstance(err.reply, dict) else None
        if done is None:
            self.log.warn("sink.commit failed without saying what it did")
            return stage.Done([], 0, [])
        try:
            return stage.done_from_json(done)
        except VCharonError as e:
            self.log.warn("sink.commit failed, and %s" % e.message)
            return stage.Done([], 0, [])

    def abort(self):
        """Drops what the helper staged. Never raises: it must not replace the error that made
        the run abort."""
        if not self.session.usable:
            return
        try:
            self.session.call("sink.abort")
        except Exception as e:  # noqa: BLE001
            self.log.warn("sink.abort failed: %s" % e)

    def close(self):
        # nothing: the helper closes its own sink when it exits, which aborts unless it
        # committed
        pass


def reset(session):
    """job.reset: between two jobs on one connection, the helper drops the last job's source
    and sink (a sink that didn't commit drops its stage dir), so the next job starts clean."""
    result = session.call("job.reset")
    if result != {}:
        raise VCharonError("protocol", "a malformed job.reset result: %s" % proto.quote(result))
