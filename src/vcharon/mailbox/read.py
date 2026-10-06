"""vcharon read C: a whole channel, every member's entries merged in one order (the guide's
read topic). Read-only: it writes nothing and starts no sync.

A local member reads the channel's folder in the channel root on this machine; a remote member
reads this box's copy of the channel, as of its last sync (a watch, or vcharon sync C).

It reads every top-level folder whose name is a writer's (the member's own folder too; other
names, symlinks and stage dirs are left out, as the watcher leaves them out), and below each the
entries of every .md file (vcharon/entries.py's format) but the folder's MEMBER.md, whose #1 only
marks the folder. Lines, in this order:

    <C>: <n> entries from <m> members (<dir>)
                                   the first line; for a remote member, `, as of this box's
                                   last sync`
    <time>  <id>  <to>  [re <id>  ]<title>  (<folder>/<file>)
                                   one line an entry, oldest first; - for a part it lacks.
                                   --full adds the header's other lines and the body, indented
                                   by 4. --last N: only the newest N. ID ...: only the entries
                                   with those IDs (placed ones), with --full's lines.
                                   --to-me: only the entries the member's watcher prints as
                                   `to you:` or `to all:` (to_me)
    note: <text>                   after the list, one line each (below)
      note: to see the bodies: vcharon read <C> --full ...
                                   last, without --full, when an entry is listed (cli.py's
                                   READ_FULL_HINT, with the command's own --last and
                                   membership flags)

The order: an entry whose time is missing or doesn't parse (entries.TIME_FORMAT) comes first,
in path order. The rest go by their minute. Within one minute one member's entries go by number
(#7 before #8: post's lock makes that true), and the entry a re: names goes before the entry
naming it; of the entries that are free to go, the earliest second goes first, then the
smallest (name, number), the number compared as a number (#9 before #10). Across minutes the
time wins, even against re:. So clocks a few seconds apart can't put an answer before its
question, nor one member's #8 before its #7. Only a placed entry is ordered by number and re::
its ID's name is its folder's, and it's the first with that ID in path order (the watcher
checks the same copy). Every entry is listed.

Notes: a folder or file it can't read (the rest is still shown); for a local member, another
member's folder over the channel's limits (MB or files), left out until it is back under; for
a remote member, each folder its last sync left out for that reason, whose copy here stays as
it was; an entry with no ID, in a
folder not its ID's, or a second copy of an ID; an entry with no time, or a bad one; one stamped
after this box's current minute; one stamped before the entry its re: names, even within one minute
(clocks differ?); a re: naming an ID not in the tree (not synced yet, or a typo); re: lines that
make a cycle within a minute, which then goes by number only; last, when the members'
MEMBER.md give two or more vcharon versions, each member's (unknown for one with none).

Every text line goes through pathrules.printable: entries are other members' text, and a
control or format character in one is shown escaped, never sent to the terminal.

Exit 0; 1 when the channel's folder can't be read: an ERROR line with its code and a fix line
on stderr (cli.py's _read; a local member's channel folder that is gone gives the leave
command, as the watcher does). With IDs, an ID no placed entry has is exit 1 too, after the
view is printed: `ERROR not_found: no entry <ids> in <C>` and its fix line.

--json prints one object instead (view_json): {"channel", "folder", "synced", "members",
"member_info", "count", "entries", "notes", "missing"}. "folder" is the tree read; "synced" is
true for a remote member's copy; "members" the member folders read; "member_info" each one's
{"name", "box", "os", "agent", "project", "vcharon"} from its MEMBER.md #1 (null for a field it
lacks; "vcharon" the version the member last joined or watched with), and "watched" (the time
of its watcher's last pull, local, or null) and "watch_every" (that watcher's --every seconds,
or null) from the members' last-watched stamps, and "left" (true when its own folder has a
LEAVE of it after its last JOIN or REJOIN, entries.left_of); "count" every entry in the tree,
while "entries" holds the ones shown (--last, IDs, --to-me), in the view's order, each
{"time", "id", "name", "number", "to", "re", "title", "file", "header", "body"}: "file" is the
entry's file relative to the tree, with "/"; "time", "id", "name", "number" and "re" are null
when the entry lacks them; "header" (a list of [key, value], key "" for a line without one)
and "body" are null unless --full (or IDs). "notes" are the note lines' texts, without
"note: "; "missing" the IDs asked for that no placed entry has ([] without IDs).

member_list is whoami C's: the member folders with their MEMBER.md's agent, box and os, and
whether each one left.
"""

from __future__ import annotations

import datetime
import heapq
import itertools
import os
import re

from .. import VERSION, channels, entries, pathrules

INDENT = "    "


def _why(e):
    return e.strerror or str(e)


def _counted(n, one, many):
    return "%d %s" % (n, one if n == 1 else many)


class Item:
    """One entry of the tree, where it is, and what the order makes of it."""

    def __init__(self, e, folder, path):
        self.e = e
        self.folder = folder
        self.path = path            # relative to the tree, with "/"
        self.placed = False
        self.minute = None          # a datetime, or None for a missing or bad time
        self.second = 0             # its second
        self.when = None            # the full time, a datetime
        self.notes = []

    @property
    def label(self):
        """The entry's ID, or where it is when it has none."""
        return self.e.id or "%s line %d" % (self.path, self.e.line)

    def key(self):
        """Of the ready entries, the smallest goes first: (second, name, number), then where it
        is."""
        e = self.e
        return (self.second, e.name or self.folder, e.number or 0, self.path, e.line)


def read_tree(root, skip=None):
    """(member folders, [Item] in path order, notes) of the channel tree root. An error on the
    root itself raises OSError; below it, what can't be read is a note. skip(folder's path,
    its name): a note for a member folder to leave out (over the channel's limits), or None
    to read it."""
    notes = []

    def rel(path):
        return os.path.relpath(path, root).replace(os.sep, "/")

    def unread(e):
        # os.walk's onerror: it would skip the folder without a word
        notes.append("note: can't read %s/: %s" % (rel(e.filename), _why(e)))

    with os.scandir(root) as it:
        top = sorted(it, key=lambda d: d.name)
    folders = []
    files = []
    for d in top:
        try:
            is_dir = d.is_dir(follow_symlinks=False)
        except OSError:
            continue
        # a stage dir, a symlink or a stray name: clients leave it out, and so does the view
        if not is_dir or pathrules.writer_problem(d.name) is not None:
            continue
        left_out = skip(d.path, d.name) if skip is not None else None
        if left_out is not None:
            notes.append("note: %s" % left_out)
            continue
        folders.append(d.name)
        for path in entries.md_files(d.path, onerror=unread):
            path = rel(path)
            if path != "%s/%s" % (d.name, entries.MEMBER_FILE):
                files.append((path, d.name))
    items = []
    for path, folder in sorted(files):
        try:
            found = entries.parse_file(os.path.join(root, *path.split("/")))
        except FileNotFoundError:
            continue
        except OSError as e:
            notes.append("note: can't read %s: %s" % (path, _why(e)))
            continue
        items.extend(Item(e, folder, path) for e in found)
    return folders, items, notes


def _kahn(nodes, edges):
    """nodes in an order that keeps every edge (a, b), a before b, taking the smallest ready
    key first; None on a cycle."""
    after = {id(n): [] for n in nodes}
    need = {id(n): 0 for n in nodes}
    for a, b in edges:
        after[id(a)].append(b)
        need[id(b)] += 1
    ready = [(n.key(), k, n) for k, n in enumerate(nodes) if need[id(n)] == 0]
    heapq.heapify(ready)
    rank = {id(n): k for k, n in enumerate(nodes)}
    out = []
    while ready:
        _, _, n = heapq.heappop(ready)
        out.append(n)
        for b in after[id(n)]:
            need[id(b)] -= 1
            if need[id(b)] == 0:
                heapq.heappush(ready, (b.key(), rank[id(b)], b))
    return out if len(out) == len(nodes) else None


def order(items, now):
    """(items in the view's order, the notes of the minutes): the rules of the docstring. now
    is this box's current time, a datetime. Each item's own notes go on item.notes."""
    placed = {}
    in_tree = set()
    for item in items:
        e = item.e
        if e.id is not None:
            in_tree.add(e.id)
        if e.name is None:
            item.notes.append("note: %s: no ID" % item.label)
        elif e.name != item.folder:
            item.notes.append("note: %s in %s/: not its folder's" % (e.id, item.folder))
        elif e.id in placed:
            item.notes.append("note: %s again in %s: the one in %s is ordered"
                              % (e.id, item.path, placed[e.id].path))
        else:
            item.placed = True
            placed[e.id] = item
    this_minute = now.replace(second=0, microsecond=0)
    untimed = []
    minutes = {}
    for item in items:
        when = item.e.time
        # headings carry local time with no zone; compared with _now(), also naive
        item.when = entries.parse_time(when)
        if item.when is None:
            item.notes.append("note: %s has %s: listed first"
                              % (item.label, "no time" if not when else "a bad time %r" % when))
            untimed.append(item)
            continue
        item.minute = item.when.replace(second=0)
        item.second = item.when.second
        minutes.setdefault(item.minute, []).append(item)
        if item.minute > this_minute:
            item.notes.append("note: %s is stamped after now (%s)" % (item.label, when))
    for item in items:
        re_ = item.e.re
        if not item.placed or not re_ or re_ == item.e.id:
            continue
        if re_ not in in_tree:
            item.notes.append("note: %s answers %s, which isn't in the tree (not synced yet, "
                              "or a typo)" % (item.e.id, re_))
            continue
        target = placed.get(re_)
        if (target is not None and item.minute is not None and target.minute is not None
                and item.when < target.when):
            item.notes.append("note: %s answers %s but is stamped earlier: clocks differ?"
                              % (item.e.id, re_))
    out = list(untimed)
    notes = []
    for minute in sorted(minutes):
        nodes = minutes[minute]
        mine = [n for n in nodes if n.placed]
        numbered = []
        by_name = {}
        for n in mine:
            by_name.setdefault(n.e.name, []).append(n)
        for group in by_name.values():
            group.sort(key=lambda n: n.e.number)
            numbered.extend(itertools.pairwise(group))
        here = {n.e.id: n for n in mine}
        answers = [(here[n.e.re], n) for n in mine
                   if n.e.re in here and n.e.re != n.e.id]
        done = _kahn(nodes, numbered + answers)
        if done is None:
            # a wrong --re can make one: post checks only its form
            notes.append("note: %s: re: lines make a cycle; that minute goes by number only"
                         % minute.strftime(entries.MINUTE_FORMAT))
            done = _kahn(nodes, numbered)
        out.extend(done)
    return out, notes


def lines(items, full):
    """The list's lines for items, in their order. Every part comes from a member's file, so
    each line goes through pathrules.printable: an escape sequence or a CR in another member's
    entry can't clear the screen or print over a line with one that looks like vcharon's."""
    out = []
    for item in items:
        e = item.e
        re_ = "re %s  " % e.re if e.re else ""
        out.append("%s  %s  %s  %s%s  (%s)" % (e.time or "-", e.id or "-",
                                               " ".join(e.to) or "-", re_, e.title, item.path))
        if not full:
            continue
        for k, v in e.header:
            out.append(INDENT + ("%s: %s" % (k, v) if k else v))
        for line in e.body.split("\n") if e.body else ():
            out.append((INDENT + line).rstrip())
    return [pathrules.printable(line) for line in out]


def _now():
    """This box's current time (tests fake it)."""
    # naive local time on purpose: headings are written in local time, with no zone
    return datetime.datetime.now()  # noqa: DTZ005


# the fields of a member's MEMBER.md #1 that --json gives: vcharon list's, and the version
INFO_FIELDS = channels.LIST_FIELDS + ("vcharon",)


def member_info(root, folders, seen=None, items=()):
    """[{"name", "box", "os", "agent", "project", "vcharon", "watched", "watch_every",
    "left"}] of the member folders read, from each one's MEMBER.md #1; None for a field it
    lacks, or holds in another shape. "watched" and "watch_every": watched_fields of seen.
    "left": entries.left_of the member's own entries in its folder among items (read_tree's,
    already read: whoami's entries.has_left reads the same files)."""
    headings = {}
    for item in items:
        headings.setdefault(item.folder, []).append((item.e.name, item.e.number,
                                                     item.e.title))
    out = []
    for name in folders:
        found = channels.read_member(os.path.join(root, name), name)
        out.append(dict([("name", name)] + [(k, found.get(k)) for k in INFO_FIELDS]
                        + watched_fields(seen, name)
                        + [("left", entries.left_of(headings.get(name, ()), name))]))
    return out


def watched_fields(seen, name):
    """[("watched", time), ("watch_every", seconds)] of name in seen ({name: (time on this
    box, --every seconds)}: the members' last-watched stamps): time local, to the second;
    both None for a member with no stamp. A member's stamp is its watcher's last pull of
    the channel, at most 30 s plus one of its rounds old (and the sync's own time) while the
    watcher runs; it is late past 2 x every + 120 s (the guide's lead topic)."""
    t, every = (seen or {}).get(name, (None, None))
    return [("watched", None if t is None else entries.stamp(t)), ("watch_every", every)]


# the fields of a member's MEMBER.md #1 that whoami C lists
WHO_FIELDS = ("agent", "box", "os")


def member_list(root, leader, seen=None, skip=None):
    """whoami C's members: [{"name", "agent", "box", "os", "leader", "newest", "watched",
    "watch_every", "left", "vcharon"}], one per top-level folder of the tree root whose name
    can be a member's (not a symlink, a stage dir or a stray name), in name order. "agent",
    "box", "os" and "vcharon" come from the folder's MEMBER.md #1 (None for one it lacks):
    the only text shown from a member's files, so nothing else of its machine shows.
    "leader": whether it is the leader's folder. "newest": the time of its newest file
    outside stage dirs, local, to the second, or None. "watched", "watch_every":
    watched_fields of seen. "left": entries.has_left of the folder (its headings, read for
    that flag only); None for a folder skip(path, name) leaves out (read's rule: a note, or
    None to read it), whose files aren't read, so one over the channel's limits costs no
    more than it does read. OSError when the root can't be read."""
    with os.scandir(root) as it:
        top = sorted(it, key=lambda d: d.name)
    out = []
    for d in top:
        try:
            is_dir = d.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if not is_dir or pathrules.writer_problem(d.name) is not None:
            continue
        found = channels.read_member(d.path, d.name)
        newest = channels.newest_file(d.path)
        left = (None if skip is not None and skip(d.path, d.name) is not None
                else entries.has_left(d.path, d.name))
        out.append(dict([("name", d.name)] + [(k, found.get(k)) for k in WHO_FIELDS]
                        + [("leader", d.name == leader),
                           ("newest", None if newest is None else entries.stamp(newest))]
                        + watched_fields(seen, d.name)
                        + [("left", left), ("vcharon", found.get("vcharon"))]))
    return out


# the last release whose watchers stamp nothing: a member that has no stamp and last ran it,
# or an older one, may be watching all the same
UNSTAMPED_UPTO = (0, 2, 3)
_RELEASE = re.compile(r"\A(\d+)\.(\d+)\.(\d+)")


def _numbers(version):
    m = _RELEASE.match(version) if version else None
    return None if m is None else tuple(int(n) for n in m.groups())


# this build's numbers: a build that stamps but still carries UNSTAMPED_UPTO's numbers (before
# the release that bumps them) writes them into its members' MEMBER.md too
THIS = _numbers(VERSION)


def may_not_stamp(version):
    """Whether a member whose MEMBER.md says version (None: no vcharon: line, before 0.2.0)
    runs a vcharon whose watcher stamps nothing: UNSTAMPED_UPTO or older (a pre-release by
    its numbers), and older than this build, since a member that ran this build stamps."""
    numbers = _numbers(version)
    return numbers is None or (numbers <= UNSTAMPED_UPTO and (THIS is None or numbers < THIS))


def ago(seconds):
    """An age as whoami prints it: seconds under 2 minutes, minutes under 2 hours, else
    hours."""
    seconds = max(0, int(seconds))
    if seconds < 120:
        return "%d s" % seconds
    if seconds < 7200:
        return "%d min" % (seconds // 60)
    return "%d h" % (seconds // 3600)


def watched_text(seen, name, version, now):
    """whoami C's last-watched part of a member's line: `watched <age> ago (every <n> s)`;
    without a stamp `watched ? (vcharon <version>)` for a member whose version may not stamp
    (may_not_stamp; `unknown` for none), else `watched -`."""
    t, every = (seen or {}).get(name, (None, None))
    if t is not None:
        return "watched %s ago (every %d s)" % (ago(now - t), every)
    if may_not_stamp(version):
        return "watched ? (vcharon %s)" % (version or "unknown")
    return "watched -"


def version_note(info):
    """The note when the members' MEMBER.md give two or more vcharon versions, each member
    with its version (unknown for one with none); None otherwise. Why: members on different
    versions read different guides, and nothing else in a channel shows it."""
    if len({one["vcharon"] for one in info if one["vcharon"]}) < 2:
        return None
    return ("note: members' vcharon versions differ (from their MEMBER.md): %s; their guides "
            "may differ" % ", ".join("%s %s" % (one["name"], one["vcharon"] or "unknown")
                                     for one in info))


def _collect(root, now, skip=None, notes=(), seen=None):
    """(member folders, items in the view's order, notes, member_info) of the tree root;
    OSError when the root can't be read. notes: more notes' texts, first; the version note,
    if any, last. seen: member_info's."""
    folders, items, read_notes = read_tree(root, skip)
    read_notes = ["note: %s" % n for n in notes] + read_notes
    if now is None:
        now = _now()
    ordered, minute_notes = order(items, now)
    notes = read_notes + [n for item in ordered for n in item.notes] + minute_notes
    info = member_info(root, folders, seen, items)
    versions = version_note(info)
    if versions is not None:
        notes.append(versions)
    return folders, ordered, notes, info


def to_me(item, me, leader):
    """Whether the watcher of me would print item as `to you:` or `to all:`: a placed entry in
    another member's folder addressed to @<me>, or to @all from the leader. Its own folder is
    never watched, and a member's @all is ignored, so neither is here either."""
    e = item.e
    if not item.placed or item.folder == me:
        return False
    return "@" + me in e.to or (entries.ALL in e.to and item.folder == leader)


def pick(ordered, last=None, ids=None, mine=None):
    """(the items shown, in the view's order; the IDs asked for that no placed entry has).
    ids: only the placed entries with those IDs (a forged copy in another folder is never
    taken for the entry); mine: (me, leader), only the entries to_me; last: the newest N of
    what is left."""
    shown = ordered
    missing = []
    if ids:
        found = {item.e.id for item in ordered if item.placed and item.e.id in ids}
        missing = [i for i in ids if i not in found]
        shown = [item for item in shown if item.placed and item.e.id in ids]
    if mine is not None:
        shown = [item for item in shown if to_me(item, *mine)]
    if last:
        shown = shown[-last:]
    return shown, missing


def view(root, channel, synced=False, full=False, last=None, now=None, skip=None, notes=(),
         bodies=None, ids=None, mine=None):
    """(the view's lines of the channel tree root, to print; the IDs of ids not found);
    OSError when the root can't be read (the caller names it with a code and a fix). Nothing
    is printed here, so an error writing stdout is never taken for one reading the tree.
    skip: read_tree's; notes: more notes (a remote member's: the members its last pull left
    out); bodies: the last line when the list, without full, shows at least one entry (the
    caller's: how to see the bodies); ids, mine: pick's (ids imply full)."""
    folders, ordered, notes, _info = _collect(root, now, skip, notes)
    out = ["%s: %s from %s (%s)%s" % (channel, _counted(len(ordered), "entry", "entries"),
                                      _counted(len(folders), "member", "members"), root,
                                      ", as of this box's last sync" if synced else "")]
    shown, missing = pick(ordered, last, ids, mine)
    full = full or bool(ids)
    out += lines(shown, full)
    # a note holds IDs, times and paths from members' files too
    out += [pathrules.printable(note) for note in notes]
    # the short form shows titles only: say how to see the bodies
    if bodies is not None and shown and not full:
        out.append(bodies)
    return out, missing


def view_json(root, channel, synced=False, full=False, last=None, now=None, skip=None,
              notes=(), ids=None, mine=None, seen=None):
    """The view as one JSON object (the module's docstring has its fields); OSError when the
    root can't be read. skip and notes: view's; ids and mine: pick's (ids imply full); seen:
    member_info's."""
    folders, ordered, notes, info = _collect(root, now, skip, notes, seen)
    shown, missing = pick(ordered, last, ids, mine)
    full = full or bool(ids)
    items = []
    for item in shown:
        e = item.e
        items.append({"time": e.time or None, "id": e.id, "name": e.name, "number": e.number,
                      "to": list(e.to), "re": e.re or None, "title": e.title, "file": item.path,
                      "header": [[k or "", v] for k, v in e.header] if full else None,
                      "body": e.body if full else None})
    return {"channel": channel, "folder": root, "synced": synced, "members": folders,
            "member_info": info, "count": len(ordered), "entries": items,
            "notes": [n.removeprefix("note: ") for n in notes], "missing": missing}
