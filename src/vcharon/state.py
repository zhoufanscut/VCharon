"""State files and job locks (DESIGN §11.2, §11.3); client."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import tempfile
import time

from . import fsops, platform
from .lock import Lock
from .proto import VCharonError, quote

SCHEMA = 1
# the keys of a state file, in the order it's written
KEYS = ("schema", "job", "fingerprint", "identity", "sink", "source", "saved")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
# for a server with no machine id, which no job's state can be tied to (DESIGN §7.3)
NO_MACHINE_HINT = "give the server one (systemd-machine-id-setup, as root)"
# for a remote end that isn't Linux (M11a), with its alias
LINUX_HINT = "point %s at a Linux server"


def not_linux(hello, where):
    """The refusal of a remote end whose helper doesn't run on Linux (DESIGN §14 M11a), or None
    for a Linux one. Until M11a only "no machine id" kept a Mac or Windows box from being one;
    now both have an id, so every place that checks the remote machine id checks this first."""
    osn = hello.get("os")
    if osn == "linux":
        return None
    return "%s runs %s: only a Linux server is supported as a remote end" % (where, osn)


def need_linux(hello, where, hint):
    message = not_linux(hello, where)
    if message is not None:
        raise VCharonError("state_mismatch", message, hint)


@dataclasses.dataclass
class State:
    """One job's state file, less its schema and job name (DESIGN §11.2)."""

    fingerprint: str
    identity: dict | None
    sink: dict              # {"end", "root"}, plus "machine" for a remote sink
    source: dict | None     # what the source wants back on the next run
    saved: str


def reset_hint(name):
    """The hint of every state_mismatch of the job name: for a channel section's job
    <C>.<name>.up or .down, the sync commands that forget its state and send by content."""
    job = split_job(name)
    if job is None:
        return "check the target; then reset the job's state, and sync it with --full"
    from . import channel_cmd
    channel, me, which = job
    flags = channel_cmd.name_flags(channel, me)
    # two commands, set apart by " ; " so that no mark sticks to a flag's value
    return ("check the target, then run both: vcharon sync %s --reset %s %s ; vcharon sync %s "
            "--full %s" % (channel, which, flags, channel, flags))


def split_job(name):
    """(channel, member, "up" or "down") of a channel section's job name <C>.<member>.up or
    .down; None for any other name."""
    section, dot, which = name.rpartition(".")
    channel, dot2, me = section.partition(".")
    if not (dot and dot2 and which in ("up", "down") and channel and me and "." not in me):
        return None
    return channel, me, which


def config_changed(job):
    """The message of a state saved for another config: the keys that make the fingerprint,
    as the job's section spells them."""
    keys = ("ssh, mailbox.me, mailbox.local or mailbox.remote" if job.mailbox is not None
            else "ssh, from, to, from.path or to.path")
    return "the state of %s was saved for another config: its %s changed" % (job.name, keys)


def fingerprint(job):
    """What ties a state to its config (DESIGN §11.2): a config.Job's ssh, from, to, from.path
    and to.path, as written; null for a missing path. Other options (exclude, prune, create)
    can change without a reset. A mailbox job's list also ends with its writer name, which
    reaches down only through its exclude: renaming the writer refuses both states."""
    raw = [job.ssh, job.from_text, job.to_text, job.source.options.get("path"),
           job.sink.options.get("path")]
    if job.mailbox is not None:
        raw.append(job.mailbox.me)
    text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def path(name):
    return os.path.join(platform.state_dir(), name + ".json")


def now():
    """The "saved" stamp. Never parsed."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _no_constant(name):
    raise ValueError("%s isn't valid JSON" % name)


def _good_sink(sink):
    if not isinstance(sink, dict) or sink.get("end") not in ("local", "remote"):
        return False
    keys = {"end", "root", "machine"} if sink["end"] == "remote" else {"end", "root"}
    return set(sink) == keys and all(isinstance(sink[k], str) for k in keys)


def _problem(name, doc):
    """Why doc isn't a state file of the job name, or None."""
    if not isinstance(doc, dict):
        return "it isn't a JSON object"
    missing = [k for k in KEYS if k not in doc]
    if missing:
        return "it has no %s" % ", ".join(missing)
    extra = [k for k in doc if k not in KEYS]
    if extra:
        return "it has an unknown key %s" % quote(extra[0])
    schema = doc["schema"]
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != SCHEMA:
        return "its schema is %s, not %d" % (quote(schema), SCHEMA)
    if doc["job"] != name:
        return "it's the state of %s" % quote(doc["job"])
    if not isinstance(doc["fingerprint"], str) or not _HEX64.match(doc["fingerprint"]):
        return "its fingerprint isn't 64 hex digits"
    if not isinstance(doc["identity"], (dict, type(None))):
        return "its identity isn't an object or null"
    if not _good_sink(doc["sink"]):
        return ("its sink %s isn't {\"end\", \"root\"}, with \"machine\" for a remote one"
                % quote(doc["sink"]))
    if not isinstance(doc["source"], (dict, type(None))):
        return "its source isn't an object or null"
    if not isinstance(doc["saved"], str):
        return "its saved stamp isn't a string"
    return None


def read(name):
    """(the State of the job name, None); (None, None) if it has none; (None, why) if its
    file can't be read."""
    p = path(name)
    try:
        with open(p, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, e.strerror or str(e)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, "it isn't valid UTF-8 (byte %d)" % e.start
    try:
        doc = json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError) as e:
        return None, "it isn't valid JSON: %s" % e
    why = _problem(name, doc)
    if why is not None:
        return None, why
    return State(doc["fingerprint"], doc["identity"], doc["sink"], doc["source"],
                 doc["saved"]), None


def load(name):
    """The State of the job name, or None if it has none. vcharon never guesses: a file that
    can't be read is state_mismatch."""
    st, why = read(name)
    if why is not None:
        raise VCharonError("state_mismatch", "the state file %s can't be read: %s"
                           % (path(name), why), reset_hint(name))
    return st


def save(name, st):
    """Writes the job's state file: a temp file in the same directory, flushed and fsynced,
    then renamed over the old one, so a crash leaves the old file or the new one."""
    doc = {"schema": SCHEMA, "job": name, "fingerprint": st.fingerprint,
           "identity": st.identity, "sink": st.sink, "source": st.source, "saved": st.saved}
    data = json.dumps(doc, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    target = path(name)
    tmp = None
    try:
        os.makedirs(platform.state_dir(), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=platform.state_dir(), prefix=name + ".", suffix=".tmp")
        with open(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if fsops.WINDOWS:
            # a virus scanner may hold the new file open for a moment
            fsops.retry_in_use(os.replace, tmp, target)
        else:
            os.replace(tmp, target)
    except BaseException as e:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if isinstance(e, OSError):
            raise fsops.error(e, target)
        raise


def remove(name):
    """True if it removed the job's state file."""
    p = path(name)
    try:
        os.remove(p)
    except FileNotFoundError:
        return False
    except OSError as e:
        raise fsops.error(e, p)
    return True


def lock(name):
    """The job's lock (DESIGN §11.3), held; the caller releases it. busy if another run holds
    it; the OS drops it when the process dies."""
    folder = platform.state_dir()
    p = os.path.join(folder, name + ".lock")
    try:
        os.makedirs(folder, exist_ok=True)
        lk = Lock.open(p)
    except OSError as e:
        raise fsops.error(e, p)
    try:
        held = lk.try_acquire()
    except BaseException as e:
        lk.release()
        if isinstance(e, OSError):
            raise fsops.error(e, p)
        raise
    if not held:
        lk.release()
        raise VCharonError("busy", "another run of %s is in progress" % name,
                           "wait for it to finish")
    return lk
