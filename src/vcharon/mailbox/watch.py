"""vcharon watch C: print what reached you in a channel, one line per entry (the guide's
watch topic).

Two ways to run it. Continuous, the default, for a Claude Code Monitor: it prints only when
something changed and runs until --max-minutes, a usage error or Ctrl-C. --until-change, for a
session without a Monitor (a background command, which notifies only when it exits): it exits
after the first round that printed something that counts, or whose error counts (see
status()).

A local member (joined with --local, on the machine that holds the channel root) watches the
channel's folder as plain files; its own folder <dir>/<me>/ must hold MEMBER.md. Every 10 s by
default (watch_dir).

A remote member: each round is a sync of the channel (its section's up and down jobs), then a
comparison of this box's copy of the channel with the round before (watch_job). It streams by
default: one long-lived child, `vcharon sync C --repeat <every>`, keeps one
ssh connection and prints `ROUND <code>` after each round; each ROUND line is one round here.
When the child exits it is started again after 2, 4, 8, 16, 30, 30… s (back to 2 after a round
that worked); every way out of the watch closes its stdin, waits up to 10 s, then kills it.
Streaming, the wake rules count time: an error of RETRY_KEYS counts once it has held 60 s in a
row, and --until-change's EXIT error comes after --max-errors × 30 s of failing that woke
nobody. --no-stream runs `vcharon sync C` each round, every 30 s by default, with the rules in
rounds.

Both skip the member's own folder <me>/ and vcharon's stage dirs. In the other members' folders
it reads the entries of every .md file (vcharon/entries.py's format) and prints the new ones
addressed to <me>, or to @all from the leader (the section's mailbox.leader; in server mode
the record's, else MEMBER.md's). Every line starts with the local time, `YYYY-mm-dd HH:MM:SS `.
Lines; * marks the ones that count for --until-change:

    watching <dir>, <n> files in other folders
                                   at the start; <n> counts the files outside <me>/; `, since
                                   <time>` when it goes on from a saved snapshot (the last
                                   round that changed it), `, fresh
                                   start` with --fresh; then `, streaming every <n> s` for a
                                   streaming remote member
    note: ignoring the saved snapshot <path>: <why>
                                   at the start, for a snapshot it can't use
  * to you: <id> — <title>  (<path>)
  * to all: <id> — <title>  (<path>)
                                   a new entry, in the entries' own time order
  * WARN entry <id> was edited     a heading seen before with another text; once
    WARN entry <id> in <folder>/: not its folder's
                                   an ID whose name isn't its folder's
    <n> other entries (<folders>)  new entries addressed to others, without an ID, in the
                                   wrong folder, or a member's MEMBER.md (its #1, addressed to
                                   the leader, wakes nobody); one line a round
    note: @all from <folders>, not the leader: ignored
                                   one line a round
    new | changed | gone <path>    a file that isn't a .md file in a member's folder
  * WARN <text>                    server mode: another member's folder over the
                                   channel's limits, held as it was until it is back under;
                                   a name clients leave out; in a member's
                                   folder, two names that are one on macOS or Windows, a name
                                   such a client can't hold, a symlink or a special file;
                                   a remote member: another member's folder its last sync
                                   left out for the same reason; once, while it lasts
    WARN cleared: <text>           that problem is gone
  * ERROR ...                      a failed round's first error line, once, and again only
                                   when it changes (status() says when it counts)
      fix: <text>                  what to do, right after its ERROR line when there is one:
                                   the sync's fix line, or for a local member the leave command
                                   for a channel folder that's gone (a closed channel); a command
                                   in it is as this box runs vcharon
    ok again                       the first good round after a failed one
    EXIT change | EXIT quiet <n> min | EXIT error | EXIT closed | EXIT updated | EXIT orphaned
                                   the last line, when it exits on its own (exit 0, 10, 11,
                                   13, 14, 15); EXIT closed right after the ERROR and fix (and log)
                                   lines of a
                                   channel that's gone, in every mode and on every start while
                                   it stays gone: don't restart, run the fix line's leave;
                                   EXIT updated when vcharon was replaced while it ran (an
                                   update), seen at the top of a round, from the streaming
                                   child's exit 14, or after an unexpected error in a binary
                                   (cli.py): start it again, which runs the new one;
                                   EXIT orphaned when a binary's bootloader process is gone
                                   (killed with SIGKILL): no one is left to read the watcher

The snapshot (version 2) is saved in vcharon's state dir at the start and after every round whose
scan worked (a failed sync doesn't stop that), after the round's lines are printed, so a
restart prints what came while no watcher ran; a round that changed nothing in it (its saved
time aside) doesn't write it again, so its saved time, the watching line's `since`, is
that of the last round that changed it. Per member folder it holds the entry numbers
seen (every number up to "low", and those in "more": a member's #8 can arrive before its #7),
each ID's heading hash for the edit check, and the hashes of the entries that have no ID of
their folder ("loose"), so each is told once. It holds the warnings shown too: a restart
prints them again right after the watching line, and only a new one counts. It holds the ERROR
line shown and its fix line, and the keys of the errors that counted in the failing streak: a
restart whose first round fails with the saved text prints it again without counting it, so a
blocked client isn't woken in a loop. With --until-change, a round whose save failed ends the
watch with EXIT error (exit 11). A lock on it keeps a second watcher of the same membership from
starting (exit 12).
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

# Imported here, not in the functions: a long-running watch has every module it needs loaded
# before anything can change the files under it (DESIGN, "Running watchers").
from .. import (
    channel_cmd,
    charter,
    config,
    entries,
    fsops,
    install,
    pathrules,
    platform,
    plugin,
)
from ..lock import Lock
from ..proto import VCharonError

# vcharon's stage dirs, in any case (DESIGN, "Staging and commit")
STAGE_PREFIX = ".vcharon-stage-"
# how the sync's fix and log lines start, among its ERROR line's own (cli.py's show_error)
FIX = "  fix: "
LOG = "  log: "
# between a fix's text and its log path, in the fix that run_sync returns and the snapshot
# keeps; status() prints them as two lines
LOG_SEP = "\n"
# the fix parse_failure makes for an error that has a log but no fix of its own
LOG_ONLY_FIX = "the job's log has the rest: "
# A round's sync has its own timeouts; this only keeps a stuck one from stopping the watch.
RUN_TIMEOUT = 900
# a remote member's default seconds between rounds: streaming, and with --no-stream
STREAM_EVERY, RUN_EVERY = 2, 30
# a local member's
DIR_EVERY = 10
# a streaming watch's --every, which is vcharon sync --repeat's SECONDS: 1 to this
STREAM_EVERY_MAX = 300
# Streaming, a round is seconds, so the wake rules count time, not rounds: an error of
# RETRY_KEYS counts once it has held this long in a row (as its second 30-s round did), and
# with --until-change each --max-errors round is this many seconds of failing that woke nobody.
STREAM_HOLD = 60
STREAM_ERROR_ROUND = 30
# the waits before starting the vcharon sync --repeat child again after it exited; back to the
# first after a round that worked
BACKOFF = (2, 4, 8, 16, 30)
# how long the child may take to end after its stdin closes, before it's killed
STOP_WAIT = 10
# the child's line that ends one round: the sync's exit code for it
_ROUND = re.compile(r"\AROUND (\d+)\Z")
# Windows and macOS clients ignore case in names, so Windows/ there is the own folder.
FOLDS = sys.platform in ("win32", "darwin")
# vcharon's exit code for busy: another run of the job holds its lock (DESIGN, "Lock")
BUSY = 2
# exit codes of the watcher itself (vcharon guide watch)
EXIT_CHANGE, EXIT_QUIET, EXIT_ERROR, EXIT_LOCKED, EXIT_CLOSED = 0, 10, 11, 12, 13
EXIT_UPDATED = install.EXIT_UPDATED
EXIT_ORPHANED = install.EXIT_ORPHANED
# the saved snapshot's format: 2 since channels, whose entries it keeps
SNAPSHOT_VERSION = 2
# the hex digits of a heading's sha256 kept for the edit check
HEAD_HEX = 12
# The key an ERROR line counts under for --until-change (error_key)
TRANSPORT = "transport"
# keys that count only in their second failed round in a row: a one-round blip wakes nobody
RETRY_KEYS = frozenset([TRANSPORT, "vanished", "aborted"])
# ERROR <code>: ..., or ERROR <job>: <code>: ... from a sync of a section's two jobs. A
# mailbox job's name, S.up or S.down, holds a dot; a code never does.
_CODE = re.compile(r"\AERROR (?:[A-Za-z0-9][A-Za-z0-9._-]*\.(?:up|down): )?([a-z_]+): ")
_MORE = re.compile(r" \(and \d+ more; see the log\)")


def error_key(line):
    """What an ERROR line is about, for --until-change: one wake per key and failing streak.
    The connection's errors are one key, whatever their text; too_many_deletes, vanished (a
    file changed during the run) and aborted (its text can hold a file's index) are their code,
    since their text changes from run to run; anything else is the text without its "(and N
    more; see the log)" part, which can grow while the cause stays the same. The job's name in
    the line splits none of the first two kinds: up and down losing the connection are
    one problem. It stays in the text key: a content error in up and one in down are two."""
    m = _CODE.match(line)
    code = m.group(1) if m else None
    if (code in ("connect", "timeout", "lost") or line.startswith("ERROR couldn't start vcharon")
            or (line.startswith("ERROR vcharon sync of ") and " didn't finish within " in line)):
        return TRANSPORT
    if code in ("too_many_deletes", "vanished", "aborted"):
        return code
    return _MORE.sub("", line)


def scan(root, me, fold=False):
    """{path: (size, mtime_ns)} for every regular file under root, paths relative with "/";
    leaves out <me>/ at the top (in any case with fold), stage dirs anywhere, and symlinks.
    An error on the root itself, a missing root too, raises OSError: every file would look
    gone. Below it, an entry that vanishes during the scan is left out."""
    files = {}
    with os.scandir(root) as it:
        top = list(it)
    stack = [("", None, top)]
    while stack:
        rel, full, entries = stack.pop()
        if entries is None:
            try:
                with os.scandir(full) as it:
                    entries = list(it)
            except (FileNotFoundError, NotADirectoryError):
                continue
        for entry in entries:
            name = entry.name
            if name.casefold().startswith(STAGE_PREFIX):
                continue
            if not rel and (name.casefold() == me.casefold() if fold else name == me):
                continue
            path = rel + name
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((path + "/", entry.path, None))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    files[path] = (st.st_size, st.st_mtime_ns)
            except FileNotFoundError:
                continue
    return files


def _twins(rel, names, fold):
    """The warnings for names in one directory that are one name on macOS or Windows."""
    groups = {}
    for name, is_dir in names:
        shown = rel + name + ("/" if is_dir else "")
        for osn in ("darwin", "windows"):
            groups.setdefault((osn, fold(name, osn)), []).append(shown)
    ons = {}
    for (osn, _), shown in groups.items():
        if len(shown) > 1:
            ons.setdefault(tuple(sorted(shown)), set()).add(osn)
    out = []
    for shown, osns in ons.items():
        who = " and ".join(n for o, n in (("darwin", "macOS"), ("windows", "Windows"))
                           if o in osns)
        out.append("case twins %s: %s clients get nothing until one is removed"
                   % (", ".join(shown[:-1]) + " and " + shown[-1], who))
    return out


def warnings(root, me):
    """Server mode's checks of the tree (DESIGN, "The watcher in a channel"), as the texts of WARN
    lines: every name at the top that isn't a writer's folder, which clients leave out; and in a
    writer's folder, <me>'s too, every two names that fold together on macOS or Windows and every
    name such a client can't hold, which make that client refuse the whole run, and every symlink or
    special file, which fails every client's run. Stage dirs and symlinks are never entered. An
    error on the root raises OSError, as scan's does; below it, an entry that vanishes is left
    out."""
    out = []
    with os.scandir(root) as it:
        top = sorted(it, key=lambda e: e.name)
    stack = []
    for entry in top:
        name = entry.name
        if name.casefold().startswith(STAGE_PREFIX):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not is_dir or pathrules.writer_problem(name) is not None:
            out.append("%s at the top isn't a writer's folder: clients leave it out"
                       % (name + ("/" if is_dir else "")))
        else:
            stack.append((name + "/", entry.path))
    while stack:
        rel, full = stack.pop()
        try:
            with os.scandir(full) as it:
                entries = list(it)
        except (FileNotFoundError, NotADirectoryError):
            continue
        names = []
        for entry in entries:
            if entry.name.casefold().startswith(STAGE_PREFIX):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
                is_link = entry.is_symlink()
            except FileNotFoundError:
                continue
            path = rel + entry.name + ("/" if is_dir else "")
            names.append((entry.name, is_dir))
            if is_dir:
                stack.append((path, entry.path))
            elif not is_file:
                # the path source refuses them below the top (DESIGN, "The path source")
                out.append("%s is a %s: every client gets nothing until it's removed"
                           % (path, "symlink" if is_link else "special file"))
            try:
                entry.name.encode("utf-8")
            except UnicodeEncodeError:
                # the path source refuses it (DESIGN, "The path source"); shown with \xNN escapes,
                # so the line itself prints anywhere
                out.append("%s isn't valid UTF-8: every client gets nothing until it's renamed"
                           % os.fsencode(path).decode("utf-8", "backslashreplace"))
                continue
            # Windows only: macOS's rule adds only a length limit, which a name from this
            # Linux server (255 bytes at most) never passes
            problem = pathrules.part_problem(entry.name, "windows")
            if problem:
                out.append("%s: %s: Windows clients get nothing until it's renamed"
                           % (path, problem))
        out.extend(_twins(rel, names, pathrules.fold))
    return sorted(out)


def over_limit(files, max_bytes, max_files):
    """{member: the WARN line's text} for each member folder among files (scan's) that holds
    more than the channel's limits. The text names the limit, not the folder's size, so it
    stays one warning while the folder grows."""
    totals = {}
    for path, (size, _) in files.items():
        top, sep, _ = path.partition("/")
        if sep:
            total = totals.setdefault(top, [0, 0])
            total[0] += size
            total[1] += 1
    out = {}
    for member, (size, n) in sorted(totals.items()):
        if size > max_bytes or n > max_files:
            out[member] = charter.skipped_note(
                member, "over the channel's limit of %s"
                % charter.limit_text(max_bytes, max_files), pulled=False)
    return out


def hold_over(new, old, over):
    """new (scan's), with each member folder in over as it was in old: nothing new or gone
    from it is told while it is over the limit."""
    out = {p: v for p, v in new.items() if p.partition("/")[0] not in over}
    out.update((p, v) for p, v in old.items() if p.partition("/")[0] in over)
    return out


def is_entry_file(path):
    """Whether path (relative, with "/") is read for entries: a .md file in a member's
    folder, not at the top."""
    return "/" in path and path.endswith(".md")


def diff(old, new):
    """The lines for what changed from old to new, sorted by path; entry files left out."""
    lines = []
    for path in sorted(set(old) | set(new)):
        if is_entry_file(path):
            continue
        if path not in old:
            lines.append("new %s" % path)
        elif path not in new:
            lines.append("gone %s" % path)
        elif old[path] != new[path]:
            lines.append("changed %s" % path)
    return lines


def _hex(text):
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:HEAD_HEX]


class Marks:
    """The entries told so far: per member folder, the numbers of its own IDs
    seen, every one up to low plus those in more; each ID's heading hash, for the edit check;
    and the hashes of the entries without an ID of their folder (loose), each told once."""

    def __init__(self, seen=None, heads=None, loose=()):
        self.seen = {f: [s["low"], set(s["more"])] for f, s in (seen or {}).items()}
        self.heads = dict(heads or {})
        self.loose = set(loose)

    def has(self, folder, n):
        s = self.seen.get(folder)
        return s is not None and (n <= s[0] or n in s[1])

    def add(self, folder, n):
        s = self.seen.setdefault(folder, [0, set()])
        if n <= s[0]:
            return
        s[1].add(n)
        while s[0] + 1 in s[1]:
            s[0] += 1
            s[1].discard(s[0])

    def doc(self):
        return {"seen": {f: {"low": s[0], "more": sorted(s[1])}
                         for f, s in sorted(self.seen.items())},
                "heads": dict(sorted(self.heads.items())), "loose": sorted(self.loose)}


class Told:
    """One round's entry lines: the ones that count, in the entries' time order, and the
    ones that don't."""

    def __init__(self):
        self.entries = []     # (time, path, line number, line)
        self.edited = []      # the WARN lines for edited entries: they count, once
        self.misplaced = []   # the WARN lines for IDs in another member's folder
        self.others = []      # the folders of the entries addressed elsewhere
        self.all_from = []    # the folders whose @all was ignored
        self.unread = []      # the entry files that couldn't be read: tried again next round

    def lines(self):
        out = [line for _, _, _, line in sorted(self.entries, key=lambda e: e[:3])]
        out += self.edited + self.misplaced
        if self.others:
            n = len(self.others)
            out.append("%d other %s (%s)" % (n, "entry" if n == 1 else "entries",
                                             ", ".join(sorted(set(self.others)))))
        if self.all_from:
            out.append("note: @all from %s, not the leader: ignored"
                       % ", ".join(sorted(set(self.all_from))))
        return out

    def counted(self):
        return len(self.entries) + len(self.edited)


def read_entries(root, paths, marks, me, leader, baseline=False):
    """Reads the entries of the entry files paths (relative to root) into marks; returns the
    round's Told. baseline: marks them seen, tells nothing. The leader's @all is to all; a
    member's is ignored, since any member can write anything into its own folder."""
    told = Told()
    ids = set()
    for path in sorted(paths):
        folder = path.split("/", 1)[0]
        try:
            found = entries.parse_file(os.path.join(root, *path.split("/")))
        except FileNotFoundError:
            continue
        except OSError:
            told.unread.append(path)
            continue
        for e in found:
            head = _hex(e.heading)
            if e.name != folder:
                key = _hex(folder + "/\0" + e.heading)
                if key in marks.loose:
                    continue
                marks.loose.add(key)
                if baseline:
                    continue
                told.others.append(folder)
                if e.name is not None:
                    told.misplaced.append("WARN entry %s in %s/: not its folder's"
                                          % (e.id, folder))
                continue
            if e.id in ids:
                # the same ID twice in this round's files: the first one stands
                continue
            ids.add(e.id)
            if marks.has(folder, e.number):
                if marks.heads.get(e.id, head) != head and not baseline:
                    told.edited.append("WARN entry %s was edited" % e.id)
                marks.heads[e.id] = head
                continue
            marks.add(folder, e.number)
            marks.heads[e.id] = head
            if baseline:
                continue
            where = "%s — %s  (%s)" % (e.id, e.title, path)
            if path == "%s/%s" % (folder, entries.MEMBER_FILE):
                # a member's #1, addressed to the leader by design: its JOIN is what tells
                told.others.append(folder)
            elif "@" + me in e.to:
                told.entries.append((e.time or "", path, e.line, "to you: " + where))
            elif entries.ALL in e.to and folder == leader:
                told.entries.append((e.time or "", path, e.line, "to all: " + where))
            elif entries.ALL in e.to:
                told.all_from.append(folder)
            else:
                told.others.append(folder)
    return told


def stamp(t):
    """A line's time: local, to the second, no zone (entry headings are dated that way)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def say(line):
    print(line)
    # a Monitor reads a pipe, a background command a file: every line at once
    sys.stdout.flush()


def _root_key(root):
    return os.path.normcase(os.path.realpath(root))


def snapshot_path(root, me, job=None):
    """Where the saved snapshot lives: vcharon's state dir, one file per mailbox job on a client,
    one per writer and tree on the server."""
    if job is not None:
        name = "mailbox-watch-%s.json" % job
    else:
        # normcase(realpath), as the post lock: two spellings of one folder (a link, and on
        # Windows another case) get one lock, so exit 12 and leave's and close's lock checks see
        # every watcher of it. Keep the name: it is v0.1.0's, so a change would lose saved
        # snapshots, and a running watcher's lock.
        key = _root_key(root)
        digest = hashlib.sha256(os.fsencode(key)).hexdigest()[:12]
        name = "mailbox-watch-dir-%s-%s.json" % (me, digest)
    return os.path.join(platform.state_dir(), name)


def _why(e):
    return e.strerror or str(e)


def load_snapshot(path, root, me):
    """(files, saved, note, warnings, error, counted, marks, fix). files and saved are None
    when there is no usable snapshot; note is the line for one that's there but can't be used,
    else None; warnings the texts of the WARN lines shown; error the ERROR line shown last and
    still holding, or None; counted the keys (error_key) that woke --until-change in that
    failing streak; marks the entries told (Marks); fix the text of the fix line shown with
    error, or None. A snapshot without error, counted or fix has none."""
    nothing = [], None, [], None, None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return (None, None, None) + nothing
    except OSError as e:
        return (None, None, "can't read it: %s" % _why(e)) + nothing
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return (None, None, "it isn't JSON") + nothing
    if not _snapshot_shape(doc):
        return (None, None, "it has another shape") + nothing
    # by the key snapshot_path names it with, not as typed: a restart under another spelling
    # of the same folder (a link, another case) goes on from it
    if _root_key(doc["root"]) != _root_key(root):
        return (None, None, "it is for %s" % doc["root"]) + nothing
    if doc["me"] != me:
        return (None, None, "it is for the member %s" % doc["me"]) + nothing
    return ({k: (v[0], v[1]) for k, v in doc["files"].items()}, doc["saved"], None,
            list(doc["warnings"]), doc.get("error"), list(doc.get("counted") or []),
            Marks(doc["seen"], doc["heads"], doc["loose"]), doc.get("fix"))


def _int(v):
    # JSON's true and false are ints to Python
    return isinstance(v, int) and not isinstance(v, bool)


_SNAPSHOT_KEYS = {"version", "root", "me", "saved", "files", "warnings", "seen", "heads",
                  "loose"}
# keys a snapshot may lack: "error" and "counted" are written only while a round fails, "fix"
# only while its error has one
_SNAPSHOT_OPTIONAL = {"error", "counted", "fix"}
_HEX = re.compile(r"\A[0-9a-f]{%d}\Z" % HEAD_HEX)


def _strings(v):
    return isinstance(v, list) and all(isinstance(w, str) for w in v)


def _marks_shape(doc):
    seen, heads, loose = doc["seen"], doc["heads"], doc["loose"]
    if not (isinstance(seen, dict) and isinstance(heads, dict) and _strings(loose)):
        return False
    for s in seen.values():
        if not (isinstance(s, dict) and set(s) == {"low", "more"} and _int(s["low"])
                and s["low"] >= 0 and isinstance(s["more"], list)
                and all(_int(n) and n > s["low"] for n in s["more"])):
            return False
    return (all(isinstance(h, str) and _HEX.match(h) for h in heads.values())
            and all(_HEX.match(h) for h in loose))


def _snapshot_shape(doc):
    if not isinstance(doc, dict) or set(doc) - _SNAPSHOT_OPTIONAL != _SNAPSHOT_KEYS:
        return False
    # null and [] are what "absent" means
    if not (isinstance(doc.get("error"), (str, type(None)))
            and isinstance(doc.get("fix"), (str, type(None)))
            and (doc.get("counted") is None or _strings(doc["counted"]))):
        return False
    if not _strings(doc["warnings"]):
        return False
    if not (_int(doc["version"]) and doc["version"] == SNAPSHOT_VERSION):
        return False
    if not all(isinstance(doc[k], str) for k in ("root", "me", "saved")):
        return False
    files = doc["files"]
    if not isinstance(files, dict):
        return False
    return (all(isinstance(v, list) and len(v) == 2 and _int(v[0]) and _int(v[1])
                for v in files.values()) and _marks_shape(doc))


def snapshot_doc(root, me, files, saved, warns=(), error=None, counted=(), marks=None,
                 fix=None):
    """The snapshot's JSON object, as save_snapshot writes it."""
    doc = {"version": SNAPSHOT_VERSION, "root": root, "me": me, "saved": saved,
           "files": {k: [v[0], v[1]] for k, v in sorted(files.items())},
           "warnings": sorted(warns)}
    doc.update((marks if marks is not None else Marks()).doc())
    if error is not None:
        doc["error"] = error
        if fix is not None:
            doc["fix"] = fix
    if counted:
        doc["counted"] = sorted(counted)
    return doc


def save_snapshot(path, root, me, files, saved, warns=(), error=None, counted=(), marks=None,
                  fix=None):
    """Writes the snapshot atomically: a temp file in the same dir, then os.replace."""
    doc = snapshot_doc(root, me, files, saved, warns, error, counted, marks, fix)
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            # ASCII escapes: a name that isn't valid UTF-8 (a surrogate escape on POSIX) comes
            # back the same
            f.write(json.dumps(doc).encode("ascii"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def take_lock(path):
    """The held lock on <snapshot>.lock, or None if another watcher holds it. vcharon's own
    lock: the OS drops it when the process dies (DESIGN, "Lock")."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lk = Lock.open(path + ".lock")
    try:
        held = lk.try_acquire()
    except BaseException:
        lk.release()
        raise
    if not held:
        lk.release()
        return None
    return lk


class _Watch:
    """One channel tree's snapshots, the entries told, the error shown last, and the saved
    snapshot."""

    def __init__(self, root, me, out, clock, fold=False, state=None, check=None, leader=None,
                 gone=None, timer=time.monotonic, hold=None, suffix="", folder_limits=None):
        self.root = root
        self.me = me
        # a local member's: (max bytes, max files) of each other member's folder; one over
        # them is held as it was (hold_over), with a WARN line while it lasts
        self.folder_limits = folder_limits
        # streaming: an error of RETRY_KEYS counts once it has held hold seconds in a
        # row by timer, not in its second round; and the watching line's end
        self.timer = timer
        self.hold = hold
        self.key_since = None
        self.suffix = suffix
        # whose @all is to all: the record's, never the holder of CHANNEL.md
        self.leader = leader
        self.marks = Marks()
        self.out = out
        self.clock = clock
        self.fold = fold
        # server mode: warnings(root, me), and the texts of the WARN lines shown and still
        # holding
        self.check = check
        self.warns = set()
        # the saved snapshot's path; None keeps nothing
        self.state = state
        # the ERROR line shown last and still holding, and whether it came from the saved
        # snapshot and the first round hasn't shown it again yet
        self.error = None
        self.restored = False
        # the fix line's text shown with that error, or None; and server mode's fix for a
        # channel folder that's gone, printed after its ERROR line
        self.fix = None
        self.gone = gone
        # whether the last round's error was the channel gone (its fix is CHANNEL_GONE_HINT's):
        # the watch ends with EXIT closed
        self.closed = False
        # whether the streaming child exited because vcharon was updated: EXIT updated
        self.updated = False
        # In the failing streak: the keys (error_key) that counted as a change, saved with the
        # snapshot; the keys that never count in it (the start's own error); and the key of
        # the round before, for a TRANSPORT error's second round.
        self.counted = set()
        self.quiet = set()
        self.last_key = None
        self.save_error = None
        self.snap = None
        # the saved time of the last snapshot whose scan worked
        self.saved_at = None
        # what the last save wrote, its saved time aside (save)
        self._written = None

    def say(self, line):
        self.out("%s %s" % (stamp(self.clock()), line))

    def start(self, fresh=False):
        """The first snapshot: the saved one, or a scan. True if it came from the saved one,
        so the first round comes at once."""
        if self.state is not None and not fresh:
            files, saved, note, warns, error, counted, marks, fix = load_snapshot(
                self.state, self.root, self.me)
            if note is not None:
                self.say("note: ignoring the saved snapshot %s: %s" % (self.state, note))
            if files is not None:
                self.snap = files
                self.marks = marks
                self.saved_at = saved
                # shown again by the first round if it still holds, then not a change
                self.error = error
                self.fix = fix
                self.restored = error is not None
                # its key, if it woke the agent, doesn't again; a saved network blip's second
                # round in a row still does
                self.counted = set(counted)
                self.say("watching %s, %d files in other folders, since %s%s"
                         % (self.root, len(files), saved, self.suffix))
                # shown again, as a reminder; the first round clears what's gone
                self.warns = set(warns)
                for text in sorted(self.warns):
                    self.say("WARN %s" % text)
                return True
        try:
            # a folder over the limits already is a baseline too: held as it is now, its
            # entries seen, so once it's back under only what came since is told
            first = scan(self.root, self.me, self.fold)
            self.snap, over = self._limited(first, first)
            # the entries there already are a baseline: told to nobody
            self.entries(self.snap, list(self.snap), baseline=True)
            warns = self.check(self.root, self.me) if self.check is not None else []
            warns = list(warns) + list(over)
        except FileNotFoundError:
            # not there yet: the first sync makes it
            self.snap = {}
            warns = []
        except OSError as e:
            # the start's own error, like its warnings, is a baseline: not a change
            self.status(*self._error(e), baseline=True)
            warns = []
        self.say("watching %s, %d files in other folders%s%s"
                 % (self.root, len(self.snap or {}), ", fresh start" if fresh else "",
                    self.suffix))
        self.warns = set(warns)
        for text in sorted(self.warns):
            self.say("WARN %s" % text)
        self.save()
        return False

    def entries(self, files, paths, baseline=False):
        """Reads the entry files among paths into the marks; returns the round's Told. A file
        that couldn't be read leaves files, so the next round reads it again."""
        told = read_entries(self.root, [p for p in paths if is_entry_file(p)], self.marks,
                            self.me, self.leader, baseline)
        for path in told.unread:
            files.pop(path, None)
        return told

    def round(self):
        """Prints what reached the member since the last good scan. Returns (the error line of
        a scan that failed, which keeps the last snapshot, or None; its fix line's text, or
        None; the number of lines that count)."""
        try:
            new, over = self._limited(scan(self.root, self.me, self.fold), self.snap or {})
            warns = set(self.check(self.root, self.me)) if self.check is not None else set()
            warns |= over
        except OSError as e:
            return self._error(e) + (0,)
        baseline = self.snap is None
        old = self.snap or {}
        told = self.entries(new, [p for p in new if p not in old or old[p] != new[p]],
                            baseline)
        added = ["WARN %s" % text for text in sorted(warns - self.warns)]
        # what isn't an entry, a non-.md file, is shown but wakes nobody: in a channel an
        # entry announces it
        lines = told.lines() + (diff(self.snap, new) if not baseline else [])
        counted = told.counted()
        if not baseline:
            # a new warning is a change: --until-change wakes the agent for it
            counted += len(added)
        for line in lines + added:
            # on a baseline (the start's scan failed) only the warnings are shown, and they
            # aren't a change
            self.say(line)
        for text in sorted(self.warns - warns):
            self.say("WARN cleared: %s" % text)
        self.snap = new
        self.warns = warns
        return None, None, counted

    def _limited(self, new, old):
        """(new with the folders over the limits held as in old, the WARN texts of those)."""
        if self.folder_limits is None:
            return new, set()
        over = over_limit(new, *self.folder_limits)
        if not over:
            return new, set()
        return hold_over(new, old, over), set(over.values())

    def status(self, error, fix=None, baseline=False):
        """A failed round's error line once, until its text changes, with its fix line (fix,
        the text) right after it; "ok again" after it.
        Returns 1 if this round is a change for --until-change, else 0. An error counts once
        per key (error_key) in a failing streak, and one of RETRY_KEYS only in its second failed
        round in a row, so a one-round network blip wakes nobody; "ok again" counts only if an
        error of the streak counted. The keys counted are saved, so the saved error, which the
        first round after a restart prints again if it still holds, doesn't count again: else
        every restart would wake the agent while a down stays blocked. The
        start's own error never counts. The fix line is shown with its error, and never
        counts or decides when the error is shown again. Streaming (hold set), "its
        second failed round in a row" is "once it has held hold seconds in a row"."""
        change = 0
        if error is not None:
            if error != self.error or self.restored:
                self.say(error)
                if fix is not None:
                    # the fix's own line, then the log's, kept apart so the fix's command
                    # can be run as printed
                    text, _, log = fix.partition(LOG_SEP)
                    self.say("  fix: %s" % text)
                    if log:
                        self.say("  log: %s" % log)
            key = error_key(error)
            again = self.last_key == key
            if not again:
                self.key_since = self.timer()
            held = again and (self.hold is None or self.timer() - self.key_since >= self.hold)
            if baseline:
                self.quiet.add(key)
            elif (key not in self.counted and key not in self.quiet
                  and (key not in RETRY_KEYS or held)):
                self.counted.add(key)
                change = 1
            self.last_key = key
        else:
            if self.error is not None:
                self.say("ok again")
                change = 1 if self.counted else 0
            self.counted = set()
            self.quiet = set()
            self.last_key = None
            self.key_since = None
        self.error = error
        self.fix = fix if error is not None else None
        self.closed = error is not None and is_gone(fix)
        self.restored = False
        return change

    def told(self):
        """Whether the error shown counted already in this streak: the agent was woken for
        it, so its rounds don't feed --max-errors, and a long block ends only by a change,
        "ok again" or --max-minutes."""
        return self.error is not None and error_key(self.error) in self.counted

    def save(self, scanned=True):
        """Saves the snapshot, after the round's lines: a crash in between prints them again
        at the next start, never loses them. scanned False: the round's scan failed, so the
        files and their saved time stay as they were; only the error shown changes. A failed
        save is shown once, until it works. Returns False if it failed. A snapshot that
        holds what the last save wrote, its saved time aside, isn't written again (a
        streaming watch has a round every 2 s), so its saved time is that of the last round
        that changed it: what it holds is still true for every round since."""
        if self.state is None or self.snap is None:
            return True
        same = json.dumps(snapshot_doc(self.root, self.me, self.snap, None, self.warns,
                                       self.error, self.counted, self.marks, self.fix),
                          sort_keys=True)
        if same == self._written and self.save_error is None:
            return True
        saved = stamp(self.clock()) if scanned or self.saved_at is None else self.saved_at
        try:
            save_snapshot(self.state, self.root, self.me, self.snap, saved, self.warns,
                          self.error, self.counted, self.marks, self.fix)
        except OSError as e:
            line = "ERROR can't save the snapshot %s: %s" % (self.state, _why(e))
            if line != self.save_error:
                self.say(line)
            self.save_error = line
            return False
        self.save_error = None
        self.saved_at = saved
        self._written = same
        return True

    def _error(self, e):
        """(the ERROR line, the fix's text or None) of an error on the scan: the fix for
        server mode's channel folder gone, which is how a closed channel looks there (close
        renames the folder, then deletes it). Not for an error below it, which scan skips,
        nor for any other error on it (permission, ...)."""
        fix = self.gone if isinstance(e, FileNotFoundError) else None
        return "ERROR can't read %s: %s" % (self.root, _why(e)), fix


def _loop(w, every, sleep, step, timer, at_once, until_change, max_minutes, max_errors, rounds,
          error_seconds=None, start=None, updated=None, orphaned=None):
    """Runs rounds; returns the exit code. step() runs one round and returns (change lines,
    failed, saved): failed is None for a skipped (busy) round; saved is False if the snapshot
    couldn't be saved. The limits are checked between rounds, never during one. error_seconds
    (streaming): --until-change's EXIT error comes after that many seconds of failed
    rounds, by timer, in place of max_errors rounds. start: the timer's value the limits count
    from, if the caller took it (a streaming child's --max-minutes deadline counts from it
    too). updated(): whether vcharon's code changed since the start (install.Watchdog),
    asked at the top of each round, before any other work: EXIT updated (DESIGN, "Running
    watchers"); orphaned(): whether a binary's bootloader parent is gone, asked next: EXIT
    orphaned."""
    start = timer() if start is None else start
    errors = 0
    # error_seconds: the time of the failed rounds in a row, each from the end of the round
    # before; a told or busy round neither adds nor resets, as it neither counts nor resets
    # errors
    failing = 0.0
    last = start
    done = 0
    while rounds is None or done < rounds:
        if done or not at_once:
            sleep(every)
        if updated is not None and updated():
            w.say("EXIT updated")
            return EXIT_UPDATED
        if orphaned is not None and orphaned():
            w.say("EXIT orphaned")
            return EXIT_ORPHANED
        changes, failed, saved = step()
        done += 1
        if w.updated:
            # the streaming child saw it first: its round brought nothing
            w.say("EXIT updated")
            return EXIT_UPDATED
        if w.closed:
            # in every mode, before the rules below: a closed channel's rule is "don't
            # restart; leave", not a change's "restart, then read"; nor is it counted, so a
            # restart from the saved error ends the same way
            w.say("EXIT closed")
            return EXIT_CLOSED
        now = timer()
        if failed:
            errors += 1
            failing += now - last
        elif failed is not None:
            errors = 0
            failing = 0.0
        last = now
        if until_change and not saved:
            # the change lines, if any, and the save's ERROR came before. Unsaved, every
            # restart would print the same changes again: a loop of EXIT change.
            w.say("EXIT error")
            return EXIT_ERROR
        if until_change and changes:
            w.say("EXIT change")
            return EXIT_CHANGE
        if until_change and (errors >= max_errors if error_seconds is None
                             else failing >= error_seconds):
            # the error line came before: a failed round's, shown when it first came
            w.say("EXIT error")
            return EXIT_ERROR
        if max_minutes is not None and timer() - start >= max_minutes * 60:
            w.say("EXIT quiet %d min" % max_minutes)
            return EXIT_QUIET
    return 0


def _locked(w, state):
    """The lock on the saved snapshot, or None after the ERROR line if another watcher has
    it."""
    try:
        lk = take_lock(state)
    except OSError as e:
        raise fsops.error(e, state + ".lock")
    if lk is None:
        # create, join, leave and close hold this lock while they run (DESIGN, "Create, join,
        # leave, close"). The older text stays the line's start: the exit-12 line is matched on it
        w.say("ERROR another watcher is running on this mailbox (%s.lock), or a create, join, "
              "leave or close of this member" % state)
    return lk


def watch_dir(root, me, every, out=say, sleep=time.sleep, rounds=None, clock=time.time,
              timer=time.monotonic, fresh=False, until_change=False, max_minutes=None,
              max_errors=10, folder_limits=None, updated=None, orphaned=None):
    """Server mode: a server member's channel as it is on disk; root is the channel's
    folder, as main passes it. rounds: stop after that many (tests). folder_limits: (max
    bytes, max files) of each other member's folder; one over them is held as it was, with a
    WARN line. updated, orphaned: _loop's. Returns the exit code. Refused (VCharonError) unless
    root/me/ holds MEMBER.md; root gone (a closed channel) is the ERROR and fix lines and EXIT
    closed, before any lock or snapshot."""
    gone = gone_fix(root, me)
    try:
        os.scandir(root).close()
    except FileNotFoundError as e:
        # as a round that finds it gone: the OS's own (localized) text
        w = _Watch(root, me, out, clock, gone=gone)
        w.status(*w._error(e))
        w.say("EXIT closed")
        return EXIT_CLOSED
    except OSError:
        # there, but not a folder or not readable: server_leader says what's wrong
        pass
    leader = server_leader(root, me)
    state = snapshot_path(root, me)
    w = _Watch(root, me, out, clock, state=state, check=warnings, leader=leader, gone=gone,
               folder_limits=folder_limits)
    lk = _locked(w, state)
    if lk is None:
        return EXIT_LOCKED
    try:
        at_once = w.start(fresh)

        def step():
            error, fix, changes = w.round()
            changes += w.status(error, fix)
            # a failed scan saves too: the error shown must survive a restart
            saved = w.save(scanned=error is None)
            return changes, None if w.told() else error is not None, saved

        return _loop(w, every, sleep, step, timer, at_once, until_change, max_minutes,
                     max_errors, rounds, updated=updated, orphaned=orphaned)
    finally:
        lk.release()


def sync_argv(sync_args, repeat=None):
    """The child's argv: vcharon sync <sync_args>, started as this vcharon is (self_argv),
    never through PATH (DESIGN, "Launch rules"); with repeat, --repeat <repeat>. sync_args: the
    channel, and the --project and --role that find this membership."""
    argv = platform.self_argv() + ["sync"] + list(sync_args)
    if repeat is not None:
        argv += ["--repeat", str(repeat)]
    return argv


def parse_failure(code, lines, job):
    """(exit code, its error line, that line's fix) of a sync's output lines: the first
    line that starts with ERROR, else the first that isn't blank; the fix is the text of that
    ERROR line's "  fix: " line, with its "  log: " path after it, since a fix can point
    at lines the watcher doesn't show (ssh's other messages); a log with no fix gives one
    that names the log; else None. No line at all: the exit code, with silent_fix's logs.
    Both are None for code 0. One parser for a sync's stderr and a streamed round's lines."""
    if code == 0:
        return 0, None, None
    for i, line in enumerate(lines):
        if line.startswith("ERROR"):
            fix = log = None
            # the error's own lines are indented: the connection's last words ("  | ") and
            # the done line can come between it and its fix (cli.py's show_error)
            for after in lines[i + 1:]:
                if not after.startswith("  "):
                    break
                if after.startswith(FIX) and fix is None:
                    fix = after[len(FIX):].strip() or None
                elif after.startswith(LOG) and log is None:
                    log = after[len(LOG):].strip() or None
            if log is not None:
                fix = fix + LOG_SEP + log if fix else LOG_ONLY_FIX + log
            return code, line.strip(), fix
    for line in lines:
        if line.strip():
            return code, line.strip(), None
    return code, "ERROR vcharon sync of %s exited with %d" % (job, code), silent_fix(job)


def silent_fix(job):
    """The fix for a sync child that exited without a word (killed from outside: on Windows a
    process ended that way exits 1): the logs it and its jobs write, which hold what it did
    up to then."""
    logs = platform.log_dir()
    return ("look at %s, %s and %s; if it happens again, tell your user"
            % tuple(os.path.join(logs, name) for name in
                    [job + suffix + ".log" for suffix in config.MAILBOX_JOBS] + ["vcharon.log"]))


def run_sync(job, sync_args):
    """(exit code, its error line, that line's fix) of one sync of the section job, from its
    stderr (parse_failure). All but the code are None on success."""
    try:
        ran = subprocess.run(sync_argv(sync_args), stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=RUN_TIMEOUT,
                             env=platform.child_env(), check=False)
    except subprocess.TimeoutExpired:
        return 1, "ERROR vcharon sync of %s didn't finish within %d s" % (job, RUN_TIMEOUT), None
    except OSError as e:
        return 1, "ERROR couldn't start vcharon: %s" % (e.strerror or e), None
    return parse_failure(ran.returncode, ran.stderr.decode("utf-8", "replace").splitlines(),
                         job)


def _spawn(argv, env):
    """The streaming child: stdin, stdout and stderr as pipes, bytes (decoded by the
    readers)."""
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)


class _ErrTail:
    """One child's stderr: its last 20 lines, and how many it has had. Each child has its own,
    so a slow reader of an old child can't leak lines into the next one's."""

    def __init__(self):
        self.lines = collections.deque(maxlen=20)
        self.count = 0

    def since(self, mark):
        """The lines after the first mark of them (as many as are kept)."""
        n = self.count - mark
        return list(self.lines)[-n:] if n > 0 else []


# the sync's codes for the errors that break its connection (cli.py's _still_usable): after
# a round that ended with one, vcharon sync --repeat exits by design
_BROKE = re.compile(r"\AERROR (?:[A-Za-z0-9][A-Za-z0-9._-]*: )?(connect|timeout|lost|protocol): ")


class Stream:
    """A streaming watch's child: one long-lived `vcharon sync C --repeat
    <every>`. A reader thread puts its stdout's lines in a queue, another keeps stderr's last
    20. Each ROUND <code> line ends one round; when the child exits, it's started again after
    BACKOFF's wait. spawn, sleep and timer are the tests' to replace."""

    def __init__(self, job, sync_args, every, spawn=_spawn, sleep=time.sleep,
                 timer=time.monotonic, stop_wait=STOP_WAIT):
        self.job = job
        self.argv = sync_argv(sync_args, repeat=every)
        self.every = every
        self.spawn = spawn
        self.sleep = sleep
        self.timer = timer
        self.stop_wait = stop_wait
        self.proc = None
        # the child's exits since the last round that worked: picks the wait before the next
        self.exits = 0
        self._lines = None
        self._threads = []
        self._err = _ErrTail()
        # the current child's stderr lines that came before its last round
        self._err_mark = 0
        # the mark of the round before the last, None before the second round
        self._prev_mark = None
        # the current child's ROUND lines, and whether its last one ended with an error that
        # breaks the connection, after which it exits by design
        self._rounds = 0
        self._broke = False

    def _start(self):
        # vcharon's own prints in UTF-8, on Windows too (the gbk lesson); a binary's child
        # unpacks its own copy (platform.child_env)
        env = platform.child_env(dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
        proc = self.spawn(self.argv, env)
        self.proc = proc
        self._lines = queue.Queue()
        self._err = _ErrTail()
        self._err_mark = 0
        self._prev_mark = None
        self._rounds = 0
        self._broke = False
        self._threads = [threading.Thread(target=self._read_out, args=(proc.stdout, self._lines),
                                          name="watch-stdout", daemon=True),
                         threading.Thread(target=self._read_err, args=(proc.stderr, self._err),
                                          name="watch-stderr", daemon=True)]
        for t in self._threads:
            t.start()

    @staticmethod
    def _read_out(stream, lines):
        try:
            for raw in iter(stream.readline, b""):
                lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    @staticmethod
    def _read_err(stream, tail):
        try:
            for raw in iter(stream.readline, b""):
                tail.lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
                tail.count += 1
        except (OSError, ValueError):
            pass

    def next_round(self, deadline=None):
        """(code, error line, fix) of the child's next round, as run_sync gives them, after
        starting the child if none runs; None when deadline (by timer, --max-minutes) passes
        before the round: during the wait before a start, or before its ROUND line, when the
        child is stopped. A child that exits after a ROUND line whose error breaks the
        connection, with nothing more on stdout, is started again without a round of its own:
        the round said why, and what it says on stderr then is only its way out. An exit with
        EXIT_UPDATED is UPDATED: vcharon was replaced. Any other exit (a round cut off, an exit
        before its first round, a crash after a round that didn't break) is one round, with
        its error line from stdout or stderr."""
        pending = []
        while True:
            if self.proc is None:
                if self.exits:
                    wait = BACKOFF[min(self.exits, len(BACKOFF)) - 1]
                    left = None if deadline is None else deadline - self.timer()
                    if left is not None and left < wait:
                        # --max-minutes ends the watch during the wait
                        if left > 0:
                            self.sleep(left)
                        return None
                    self.sleep(wait)
                try:
                    self._start()
                except OSError as e:
                    self.exits += 1
                    return 1, "ERROR couldn't start vcharon: %s" % (e.strerror or e), None
            line = self._next_line(deadline)
            if line is _LATE:
                # --max-minutes: on time, whatever the round under way does (a dying link
                # can take 45 s, a hung helper idle_timeout)
                self.stop()
                return None
            if line is _STUCK:
                # no round for too long: the child is stuck; vcharon's own timeouts didn't end it
                self.stop()
                self.exits += 1
                return 1, ("ERROR vcharon sync of %s didn't finish within %d s"
                           % (self.job, RUN_TIMEOUT)), None
            if line is None:
                err = self._err
                code = self._reap()
                self.exits += 1
                if code == EXIT_UPDATED:
                    # vcharon sync --repeat saw vcharon replaced under it
                    return UPDATED
                if not self._rounds:
                    # an exit before the first round: a config error, say, on stderr
                    return parse_failure(code, pending + err.since(0), self.job)
                if pending or not self._broke:
                    # stderr and stdout are two pipes: lines written after the last ROUND may
                    # have been read before it was, so with none after its mark, those after
                    # the round before's; never what came before the first round (a note on
                    # a skipped channels.d/ file), else the generic line
                    late = err.since(self._err_mark)
                    if not late and self._prev_mark is not None:
                        late = err.since(self._prev_mark)
                    return parse_failure(code, pending + late, self.job)
                continue
            m = _ROUND.match(line)
            if m is None:
                pending.append(line)
                continue
            code = int(m.group(1))
            self._prev_mark = self._err_mark if self._rounds else None
            self._rounds += 1
            self._err_mark = self._err.count
            got = parse_failure(code, pending, self.job)
            # any of the round's jobs: up's content error can come before down's lost
            self._broke = any(_BROKE.match(p) for p in pending)
            if code == 0:
                self.exits = 0
            return got

    def _next_line(self, deadline=None):
        """The child's next stdout line; None at its end; _LATE once deadline passes, _STUCK
        after RUN_TIMEOUT and a wait with no line (both by timer). Waits in short steps, so a
        Ctrl-C gets through on Windows too."""
        since = self.timer()
        while True:
            try:
                return self._lines.get(timeout=0.5)
            except queue.Empty:
                now = self.timer()
                if deadline is not None and now >= deadline:
                    return _LATE
                if now - since >= RUN_TIMEOUT + self.every:
                    return _STUCK

    def _reap(self):
        """The ended child's exit code, once its readers have caught up."""
        proc, self.proc = self.proc, None
        try:
            code = proc.wait(self.stop_wait)
        except subprocess.TimeoutExpired:
            # stdout ended but the child didn't: a leftover holds nothing we need
            proc.kill()
            code = proc.wait()
        self._close(proc)
        return code

    def _close(self, proc):
        for t in self._threads:
            t.join(2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def stop(self):
        """Ends the child, on every way out of the watch: its stdin closed, so it ends after
        its round under way, then up to stop_wait seconds, then a kill (whose round's held
        log lines are lost; vcharon's session lines are in the job's log already). A watcher
        that is killed closes the pipe the same way; the child then ends at its next wait."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(self.stop_wait)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self._close(proc)


# Stream._next_line's answers besides a line and None
_LATE = object()
_STUCK = object()
# Stream.next_round's answer for a child that exited because vcharon was updated
UPDATED = object()


def mailbox_of(job):
    """(local tree, me, leader) of the channel section job, read through vcharon's own config
    code."""
    cfg = config.load()
    if job not in cfg.mailboxes:
        skip = cfg.skipped_for(job)
        if skip is not None:
            # a broken channels.d/ file: its own error, as the sync's
            raise skip.error
        raise VCharonError("config", "%s has no channel section [%s]"
                           % (config.channels_dir(cfg.path), job),
                           hint="join the channel again, with the --project and --role you "
                           "joined with: it writes the section")
    box = cfg.jobs[cfg.mailboxes[job][0]].mailbox
    return plugin.Ctx("local").resolve(box.local, "mailbox.local"), box.me, box.leader


def server_leader(root, me):
    """The leader of a local member's channel: its record's (vcharon join --local wrote it),
    else its MEMBER.md's. Refused if root/me/ holds no MEMBER.md: a local member's watch reads
    a channel member's folder only."""
    member = os.path.join(root, me, entries.MEMBER_FILE)
    channel = os.path.basename(root)
    if not entries.own_folder(member, top=os.path.join(root, me)):
        # a root that's gone never gets here: watch_dir ends with EXIT closed
        raise VCharonError("channel", "%s isn't there: your folder in the channel holds it, once "
                           "vcharon join --local has written it" % member,
                           "ask the user: your folder in %s lost its %s"
                           % (channel, entries.MEMBER_FILE))
    record = None if pathrules.writer_problem(channel) else channel_cmd.read_record(channel, me)
    if record is not None:
        return record["leader"]
    try:
        found = entries.parse_file(member)
    except OSError as e:
        raise fsops.error(e, member)
    for key, value in (found[0].header if found else []):
        if key == "leader" and pathrules.writer_problem(value) is None:
            return value
    raise VCharonError("channel", "%s names no leader, and there's no record of %s in %s"
                       % (member, me, channel), "ask the user")


def gone_fix(root, me):
    """A local member's fix line for a channel folder that's gone: the sync's text for a
    closed channel, with the leave command's flags from me's record (a placeholder without
    one: name_flags never raises), and the command as this box runs vcharon."""
    channel = os.path.basename(root)
    return platform.runnable(channel_cmd.CHANNEL_GONE_HINT
                             % (channel, channel_cmd.name_flags(channel, me)))


def is_gone(fix):
    """Whether a fix line is the one for a channel that's gone: CHANNEL_GONE_HINT's,
    told by its start, which neither platform.runnable nor run_sync's log part changes.
    The sync swaps that hint in only for a channel job's not_found on up's sink root or
    down's source root; a local member's scan gives it only for FileNotFoundError on
    the channel's folder."""
    if not fix:
        return False
    return fix.startswith(channel_cmd.CHANNEL_GONE_PREFIX)


def watch_job(job, sync_args, every, out=say, sleep=time.sleep, run=run_sync, rounds=None,
              clock=time.time, timer=time.monotonic, fresh=False, until_change=False,
              max_minutes=None, max_errors=10, stream=False, spawn=_spawn, stop_wait=STOP_WAIT,
              updated=None, orphaned=None):
    """A remote member: sync the channel section job, then compare the local tree with the
    round before. Returns the exit code. sync_args: what follows `vcharon sync` for this
    membership. stream: the rounds are those of one long-lived `vcharon sync C --repeat
    <every>` (Stream; spawn starts it, sleep waits before a restart), in place of a
    run(job, sync_args) every `every` seconds; the wake rules then count time by timer.
    updated: _loop's; a streaming child's exit with EXIT_UPDATED ends the watch the same
    way. orphaned: _loop's."""
    local, me, leader = mailbox_of(job)
    state = snapshot_path(local, me, job)
    # the members the pull left out for their size, as the sync saved them after its down:
    # a WARN each while it lasts, in both modes (a streaming sync prints no notes)
    w = _Watch(local, me, out, clock, fold=FOLDS, state=state, leader=leader, timer=timer,
               hold=STREAM_HOLD if stream else None,
               suffix=", streaming every %d s" % every if stream else "",
               check=lambda root, me: charter.left_out_notes(job))
    lk = _locked(w, state)
    if lk is None:
        return EXIT_LOCKED
    child = None
    try:
        # before the first run, so what it brings shows as new
        w.start(fresh)
        error_seconds = None
        # one value for the loop's limits and the child's deadline: the loop can't go round
        # again at the deadline
        start = timer()
        if stream:
            child = Stream(job, sync_args, every, spawn=spawn, sleep=sleep, timer=timer,
                           stop_wait=stop_wait)
            deadline = None if max_minutes is None else start + max_minutes * 60
            error_seconds = max_errors * STREAM_ERROR_ROUND

            def run(job, sync_args):
                return child.next_round(deadline)

            # the child waits between rounds itself
            def sleep(seconds):
                return None

        def step():
            got = run(job, sync_args)
            if got is None:
                # --max-minutes passed before the child's round: no round
                return 0, None, True
            if got is UPDATED:
                w.updated = True
                return [], None, True
            code, line, fix = got
            # a failed run may still have brought files, so the scan comes either way; a
            # failed run's error line is the one shown, not the scan's
            error, scan_fix, changes = w.round()
            if code == BUSY:
                # the agent's own run holds the job's lock: a skipped round, not an error,
                # unless the scan failed too
                if error is not None:
                    changes += w.status(error, scan_fix)
                failed = None if error is None else True
            else:
                changes += w.status(*((line, fix) if code != 0 else (error, scan_fix)))
                failed = code != 0 or error is not None
            if failed and w.told():
                # the agent was woken for this error: not one for --max-errors
                failed = None
            # a failed scan saves too: the error shown must survive a restart
            saved = w.save(scanned=error is None)
            return changes, failed, saved

        # the client's first round runs at once, as it always did
        return _loop(w, every, sleep, step, timer, True, until_change, max_minutes,
                     max_errors, rounds, error_seconds, start=start, updated=updated,
                     orphaned=orphaned)
    finally:
        # every way out: any EXIT, Ctrl-C, a crash; the lock even if the stop fails
        try:
            if child is not None:
                child.stop()
        finally:
            lk.release()


def utf8_output():
    """Every line in UTF-8, whatever the console's code page (a Windows client's may be 936):
    a name the code page can't hold must not stop the watch."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass
