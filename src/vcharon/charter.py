"""What a channel's CHANNEL.md says about it: the format its files are in, and the limits on
each member's folder and entry files; on both ends (the helper reads them for channel.list and
channel.claim, the client checks them and keeps them in the join record).

The leader's CHANNEL.md entry #2, written by create, carries them in its header:

    format: 1
    created by: vcharon 0.1.0
    max mb: 50
    max files: 1000
    max entry kb: 1000

A lobby's (format 2) holds `kind: lobby` right after format:, and the lobby's fixed limits
(DESIGN, "The lobby"). Sizes are decimal, as the sync's lines print them: 1 MB is 1,000,000
bytes, 1 kB 1,000.

A member that takes the lead writes a CHANNEL.md of its own (its folder is its own to write),
whose entry carries `term: T` and `after: <L>#<m>`; a charter without term: is term 1. The
leader is the folder whose charter has the highest term (chain, DESIGN, "Leader handoff"). A
charter's header is that of the folder's own last entry in its CHANNEL.md: a member that leads
a second time appends one there."""

from __future__ import annotations

import os
import re
import stat

from . import VERSION, entries, pathrules, platform
from .proto import VCharonError

# The newest channel format this vcharon reads. Raised only for a change an older vcharon
# would misread, never for a new field an older one can ignore.
FORMAT = 2
# what each kind of channel is written in: create still writes 1, so a member on an older
# vcharon can join a work channel; only a lobby is 2, which an older one would misread
WORK_FORMAT = 1
LOBBY_FORMAT = 2
# CHANNEL.md's kind: value for a lobby; a channel without one is a work channel
LOBBY_KIND = "lobby"
# The limits' defaults: a guess, not measured; to tune once real channels have run.
DEFAULT_MAX_MB = 50
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_ENTRY_KB = 1000
# what create's flags take: (lowest, highest). Every pull's plan carries the whole sent map
# (about 170 bytes a file), so members × max_files is bounded by the plan size
# (proto.MAX_JSON: some 400,000 files in the whole channel); post and the watcher read an
# entry file whole, which bounds max_entry_kb.
BOUNDS = {"max_mb": (1, 10000), "max_files": (10, 100000), "max_entry_kb": (1, 10000)}
LIMIT_KEYS = ("max_mb", "max_files", "max_entry_kb")
# a lobby's, fixed: nothing sets them (create lobby is refused, join has no limit flags). An
# entry file is a day file there, so it holds a whole day
LOBBY_LIMITS = {"max_mb": 50, "max_files": 1000, "max_entry_kb": 10000}
# each limit's key in CHANNEL.md's header
HEADER_KEYS = {"max_mb": "max mb", "max_files": "max files", "max_entry_kb": "max entry kb"}
MB = 1000 * 1000
KB = 1000
# CHANNEL.md is read only this far: its entry #2 is at its start
READ_MAX = 64 << 10
UPDATE_HINT = "ask your user to run: vcharon --update"
NO_FORMAT_HINT = ("ask your user which channel to join; to use this one, its leader closes it "
                  "and creates it again")
# a kind no vcharon writes: a lobby has no leader to close it, so the hint names none
NO_KIND_HINT = "ask your user which channel to join"


# CHANNEL.md's rules: header, for every member who reads it
RULES = "vcharon guide rules"


def charter_entry(channel, name, version, limits, now, kind=None):
    """(title, to, header) of CHANNEL.md's #2 by name: create's, to @all, and a lobby's
    claim's (kind LOBBY_KIND), to the founder itself: in a lobby an entry to its own poster
    wakes no one, and every new member would be told of it. No host name of either end: a
    channel's files are shared."""
    to = ["@" + name] if kind == LOBBY_KIND else [entries.ALL]
    return ("channel %s created" % channel, to,
            [("leader", name), ("created", entries.stamp(now)), ("rules", RULES)]
            + header(version, limits, kind))


def default_limits():
    return {"max_mb": DEFAULT_MAX_MB, "max_files": DEFAULT_MAX_FILES,
            "max_entry_kb": DEFAULT_MAX_ENTRY_KB}


def header(version, limits, kind=None):
    """The header lines create (and a lobby's claim) writes into CHANNEL.md's #2, after
    leader: and created:; kind LOBBY_KIND for a lobby's, None for a work channel's."""
    if kind == LOBBY_KIND:
        lines = [("format", str(LOBBY_FORMAT)), ("kind", LOBBY_KIND)]
    else:
        lines = [("format", str(WORK_FORMAT))]
    return lines + [("created by", "vcharon %s" % version)] + [
        (HEADER_KEYS[k], str(limits[k])) for k in LIMIT_KEYS]


def _number(value):
    if value is None or not value.isascii() or not value.isdigit() or len(value) > 9:
        return None
    return int(value)


def own_entry(text, name):
    """The last entry of name in a CHANNEL.md's text: its charter (create's #2, or the entry a
    takeover wrote); None when it holds none."""
    found = [e for e in entries.parse(text) if e.name == name]
    return found[-1] if found else None


def header_of(text, name):
    """The other header lines of name's charter in CHANNEL.md's text (own_entry), as a dict,
    the first value of each key; {} when it holds none."""
    entry = own_entry(text, name)
    out = {}
    for key, value in entry.header if entry is not None else ():
        if key and key not in out:
            out[key] = value
    return out


def parse(text, leader):
    """{"format", "limits"} from CHANNEL.md's text: the header of the leader's charter
    (own_entry); "format" an int or None, "limits" {max_mb, max_files, max_entry_kb}, each an
    int or None; and "kind", the kind: line's text, only when it has one (a work channel's has
    none). Not checked here: the client does that (check)."""
    found = header_of(text, leader)
    out = {"format": _number(found.get("format")),
           "limits": {k: _number(found.get(HEADER_KEYS[k])) for k in LIMIT_KEYS}}
    if found.get("kind") is not None:
        out["kind"] = found["kind"]
    return out


def read(member_folder, leader):
    """parse() of member_folder/CHANNEL.md; a missing file, a link (never read through), or
    one that can't be read has no format."""
    return parse(read_text(member_folder), leader)


def read_text(member_folder):
    """member_folder/CHANNEL.md's start (READ_MAX); "" for a missing file, a link (never read
    through), or one that can't be read."""
    path = os.path.join(member_folder, entries.CHANNEL_FILE)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return ""
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return ""
            data = os.read(fd, READ_MAX)
        finally:
            os.close(fd)
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def created(text, leader):
    """The created: time of the leader's charter in CHANNEL.md's text as written, a heading's
    local time with no zone; None when it has none in that shape."""
    value = header_of(text, leader).get("created")
    return value if entries.parse_time(value) is not None else None


# --- leadership by terms (DESIGN, "Leader handoff") ---

# a charter without term: is term 1: create's, and every one an older vcharon wrote
FIRST_TERM = 1
# the most a term: line may say; one bigger isn't vcharon's
TERM_MAX = 999999999
# the first release with vcharon lead: a member whose MEMBER.md says an older one ignores a new
# leader's @all, so the LEADER entry names it too, and an offer to it is refused
LEAD_SINCE = (0, 8, 6)
_RELEASE = re.compile(r"\A(\d+)\.(\d+)\.(\d+)")


def _release(version):
    m = _RELEASE.match(version) if isinstance(version, str) else None
    return None if m is None else tuple(int(n) for n in m.groups())


def lacks_lead(version):
    """Whether a member whose MEMBER.md says version (None: none) runs a vcharon without
    vcharon lead: older than LEAD_SINCE, and older than this build (a build that has it but
    still carries an older number writes that number too)."""
    numbers, this = _release(version), _release(VERSION)
    return numbers is None or (numbers < LEAD_SINCE and (this is None or numbers < this))


class Charter:
    """One charter: folder's entry #number in its CHANNEL.md, of term term; after: the
    (name, number) its after: names, or None (term 1's)."""

    def __init__(self, folder, number, term, after):
        self.folder = folder
        self.number = number
        self.term = term
        self.after = after

    def __repr__(self):
        return "Charter(%r, %r, %r, %r)" % (self.folder, self.number, self.term, self.after)


def charters_of(text, folder):
    """The charters in folder's CHANNEL.md text: each entry of folder's own, in file order. A
    file with none (a hand edit) still makes folder a holder: one term 1 charter numbered 0."""
    out = []
    for e in entries.parse(text):
        if e.name != folder:
            continue
        head = {}
        for key, value in e.header:
            head.setdefault(key, value)
        term = _number(head.get("term"))
        after = entries.parse_id(head.get("after") or "")
        out.append(Charter(folder, e.number, term if term and term <= TERM_MAX
                           else FIRST_TERM, after))
    return out or [Charter(folder, 0, FIRST_TERM, None)]


class Chain:
    """Who leads a work channel, and whose @all counts, from the charters of its tree: the
    leader holds the highest term; a tie there is no leader. fallback: the leader to go by
    while the tree holds no charter (a remote member before its first pull: its record's)."""

    def __init__(self, charters, fallback=None):
        self.charters = sorted(charters, key=lambda c: (c.term, c.folder, c.number))
        self.fallback = fallback
        if not self.charters:
            self.term = FIRST_TERM if fallback is not None else None
            self.leader = fallback
            self.tied = []
            return
        self.term = self.charters[-1].term
        top = sorted({c.folder for c in self.charters if c.term == self.term})
        self.leader = top[0] if len(top) == 1 else None
        self.tied = top if len(top) > 1 else []

    def holders(self):
        """Every folder that holds a charter, in name order."""
        return sorted({c.folder for c in self.charters})

    def top(self):
        """The leader's charter of the top term, or None."""
        for c in reversed(self.charters):
            if c.term == self.term and c.folder == self.leader:
                return c
        return None

    def former(self, folder):
        """Whether folder held an earlier term and leads no more."""
        return folder != self.leader and any(c.folder == folder for c in self.charters)

    def term_of(self, folder):
        """The term folder leads in, or None."""
        return self.term if folder is not None and folder == self.leader else None

    def counts_all(self, folder, number):
        """Whether folder's entry #number to @all is to all, by entry numbers, never clocks:
        the top term's holder (charter #c) from #c + 1 on; the holder of an earlier term from
        #c + 1 to the #m its successor's after: names, and none after. Term 1 counts from #1,
        as before any handoff. number None: a new entry, which only the leader's counts. A tie
        at the top counts nobody's new @all."""
        if not self.charters:
            return folder is not None and folder == self.fallback
        for c in self.charters:
            if c.folder != folder:
                continue
            low = 0 if c.term == FIRST_TERM else c.number
            later = [d.term for d in self.charters if d.term > c.term]
            if not later:
                if folder == self.leader and (number is None or number > low):
                    return True
                continue
            ends = [d.after[1] for d in self.charters if d.term == min(later)
                    and d.after is not None and d.after[0] == folder]
            if number is not None and ends and low < number <= max(ends):
                return True
        return False


def tied_text(channel, tied, term):
    """The refusal's text for a channel two or more lead at its top term."""
    return ("%s has %d leaders at term %d (%s hold its charter)"
            % (channel, len(tied), term, ", ".join(f + "/" for f in tied)))


def chain(tree, fallback=None):
    """The Chain of the channel tree (a channel folder, or a remote member's copy): the
    charters in each member folder's CHANNEL.md, its own folder's too. A tree that can't be
    listed holds none."""
    found = []
    try:
        names = sorted(os.listdir(tree))
    except OSError:
        names = []
    for name in names:
        if pathrules.writer_problem(name) is not None:
            continue
        folder = os.path.join(tree, name)
        try:
            if not stat.S_ISDIR(os.lstat(folder).st_mode):
                continue
            if not stat.S_ISREG(os.lstat(os.path.join(folder, entries.CHANNEL_FILE)).st_mode):
                continue
        except OSError:
            continue
        found += charters_of(read_text(folder), name)
    return Chain(found, fallback)


def takeover_entry(channel, name, term, after, old):
    """(title, to, header) of the charter a taker writes into its own CHANNEL.md: leader:,
    term: and after:, then the fields of the old leader's charter (old: its header_of): its
    created:, rules:, format:, created by: and limits, so every reader finds the channel's.
    To the taker itself: its LEADER entry to @all is the one that tells."""
    keys = ["created", "rules", "format", "created by"] + [HEADER_KEYS[k] for k in LIMIT_KEYS]
    return ("lead of %s taken (term %d)" % (channel, term), ["@" + name],
            [("leader", name), ("term", str(term)), ("after", "%s#%d" % after)]
            + [(k, old[k]) for k in keys if old.get(k) is not None])


def limits_problem(limits):
    """Why limits ({max_mb, max_files, max_entry_kb}) can't be a channel's, or None."""
    if not isinstance(limits, dict):
        return "no limits"
    for k in LIMIT_KEYS:
        value = limits.get(k)
        low, high = BOUNDS[k]
        if (not isinstance(value, int) or isinstance(value, bool)
                or not low <= value <= high):
            return "%s is %s, not %d to %d" % (HEADER_KEYS[k], value if value is not None
                                                else "missing", low, high)
    if limits["max_entry_kb"] * KB > limits["max_mb"] * MB:
        return "max entry kb is more than max mb"
    return None


def check(channel, info, limits_hint=None):
    """The channel's limits (a dict) from info ({"format", "kind", "limits"}: a channel.list or
    channel.claim reply's, or a join record's, where a missing kind is a work channel's), or
    refused: no format (not a VCharon channel), a newer format than this vcharon reads, a kind
    no vcharon writes (format 2 without kind: lobby, or any other kind), or limits that aren't
    a channel's.
    limits_hint: the fix for those limits (a join record's: the rejoin); by default, the
    leader's CHANNEL.md."""
    fmt = info.get("format") if isinstance(info, dict) else None
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt < 1:
        raise VCharonError("channel", "%s has no format: line in its %s, so it isn't a "
                           "channel this vcharon made" % (channel, entries.CHANNEL_FILE),
                           NO_FORMAT_HINT)
    if fmt > FORMAT:
        raise VCharonError("channel", "%s uses format %d; this vcharon reads up to %d"
                           % (channel, fmt, FORMAT), UPDATE_HINT)
    kind = info.get("kind")
    if (kind not in (None, LOBBY_KIND)
            or (kind == LOBBY_KIND) != (fmt == LOBBY_FORMAT)):
        raise VCharonError("channel", "%s's %s says format %d%s, which no vcharon writes"
                           % (channel, entries.CHANNEL_FILE, fmt,
                              " with no kind:" if kind is None
                              else " and kind: %s" % pathrules.show(str(kind))),
                           NO_KIND_HINT)
    limits = info.get("limits")
    problem = limits_problem(limits)
    if problem:
        if limits_hint is not None:
            raise VCharonError("channel", "the limits in your join record of %s: %s"
                               % (channel, problem), limits_hint)
        raise VCharonError("channel", "%s's %s: %s" % (channel, entries.CHANNEL_FILE, problem),
                           "ask the leader to check the header of %s's #2 in %s"
                           % (entries.CHANNEL_FILE, channel))
    return {k: limits[k] for k in LIMIT_KEYS}


def format_note(channel, info):
    """vcharon list's note for a channel this vcharon can't use (check's refusal, then its fix
    line), or None."""
    try:
        check(channel, info)
    except VCharonError as e:
        return "%s; %s" % (e.message, e.hint)
    return None


# --- sizes ---

def size_text(n):
    """Decimal units with one decimal: 0 B, 999 B, 1.0 kB, 3.4 MB, 1.2 GB."""
    if n < 1000:
        return "%d B" % n
    for unit, scale in (("kB", 1e3), ("MB", 1e6), ("GB", 1e9)):
        # decided after rounding, so 999,999 bytes is 1.0 MB, not 1000.0 kB
        if round(n / scale, 1) < 1000:
            return "%.1f %s" % (n / scale, unit)
    return "%.1f TB" % (n / 1e12)


def _counted(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def limit_text(max_bytes, max_files):
    return "%s and %s" % (size_text(max_bytes), _counted(max_files, "file"))


def folder_hint(folder, max_bytes, max_files):
    """The fix for the writer of an own folder over the limits: up's refusal and post's."""
    return platform.prose_hint("move what isn't an entry (logs, builds, data) out of %s, or "
                               "delete it, until it holds at most %s; keep MEMBER.md and your "
                               ".md files with entries", folder,
                               limit_text(max_bytes, max_files))


def over(size, files, max_bytes, max_files):
    """None while a folder of size bytes in files files is within the limits; else what it
    holds and by how much it is over, as one clause."""
    parts = []
    if size > max_bytes:
        parts.append("%s" % size_text(size - max_bytes))
    if files > max_files:
        parts.append(_counted(files - max_files, "file"))
    if not parts:
        return None
    return ("%s in %s, %s over the limit of %s"
            % (size_text(size), _counted(files, "file"), " and ".join(parts),
               limit_text(max_bytes, max_files)))


def folder_total(folder):
    """(bytes, files) of the regular files under folder: links are neither followed nor
    counted, nor are vcharon's stage dirs and files. A missing folder is (0, 0)."""
    size = files = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames
                       if not d.casefold().startswith(pathrules.STAGE_PREFIX)]
        for f in filenames:
            if f.casefold().startswith(pathrules.STAGE_PREFIX):
                continue
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                size += st.st_size
                files += 1
    return size, files


def skipped_note(member, text, pulled=True):
    """The note for another member's folder left out for its size: by the pull (pulled), or
    by a local member's read and watch."""
    if pulled:
        return ("left out %s/: %s; this box's copy of it stays as it was until it is back under"
                % (member, text))
    return "left out %s/: %s; it is read again once it is back under" % (member, text)
