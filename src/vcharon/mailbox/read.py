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
                                   by 4. --last N: only the newest N
    note: <text>                   after the list, one line each (below)

The order: an entry whose time is missing or doesn't parse (entries.TIME_FORMAT) comes first,
in path order. The rest go by their minute. Within one minute one member's entries go by number
(#7 before #8: post's lock makes that true), and the entry a re: names goes before the entry
naming it; of the entries that are free to go, the earliest second goes first, then the
smallest (name, number), the number compared as a number (#9 before #10). Across minutes the
time wins, even against re:. So clocks a few seconds apart can't put an answer before its
question, nor one member's #8 before its #7. Only a placed entry is ordered by number and re::
its ID's name is its folder's, and it's the first with that ID in path order (the watcher's
rule). Every entry is listed.

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
command, as the watcher does).

--json prints one object instead (view_json): {"channel", "folder", "synced", "members",
"member_info", "count", "entries", "notes"}. "folder" is the tree read; "synced" is true for a
remote member's copy; "members" the member folders read; "member_info" each one's {"name",
"box", "os", "agent", "project", "vcharon"} from its MEMBER.md #1 (null for a field it lacks;
"vcharon" the version the member last joined or watched with); "count" every entry in the
tree, while "entries" holds the ones shown (--last), in the view's order, each {"time", "id",
"name", "number", "to", "re", "title", "file", "header", "body"}: "file" is the entry's file
relative to the tree, with "/"; "time", "id", "name", "number" and "re" are null when the
entry lacks them; "header" (a list of [key, value], key "" for a line without one) and "body"
are null unless --full. "notes" are the note lines' texts, without "note: ".
"""

from __future__ import annotations

import datetime
import heapq
import itertools
import os

from .. import channels, entries, pathrules

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


def member_info(root, folders):
    """[{"name", "box", "os", "agent", "project", "vcharon"}] of the member folders read, from
    each one's MEMBER.md #1; None for a field it lacks, or holds in another shape."""
    out = []
    for name in folders:
        found = channels.read_member(os.path.join(root, name), name)
        out.append(dict([("name", name)] + [(k, found.get(k)) for k in INFO_FIELDS]))
    return out


def version_note(info):
    """The note when the members' MEMBER.md give two or more vcharon versions, each member
    with its version (unknown for one with none); None otherwise. Why: members on different
    versions read different guides, and nothing else in a channel shows it."""
    if len({one["vcharon"] for one in info if one["vcharon"]}) < 2:
        return None
    return ("note: members' vcharon versions differ (from their MEMBER.md): %s; their guides "
            "may differ" % ", ".join("%s %s" % (one["name"], one["vcharon"] or "unknown")
                                     for one in info))


def _collect(root, now, skip=None, notes=()):
    """(member folders, items in the view's order, notes, member_info) of the tree root;
    OSError when the root can't be read. notes: more notes' texts, first; the version note,
    if any, last."""
    folders, items, read_notes = read_tree(root, skip)
    read_notes = ["note: %s" % n for n in notes] + read_notes
    if now is None:
        now = _now()
    ordered, minute_notes = order(items, now)
    notes = read_notes + [n for item in ordered for n in item.notes] + minute_notes
    info = member_info(root, folders)
    versions = version_note(info)
    if versions is not None:
        notes.append(versions)
    return folders, ordered, notes, info


def view(root, channel, synced=False, full=False, last=None, now=None, skip=None, notes=()):
    """The view's lines of the channel tree root, to print; OSError when the root can't be
    read (the caller names it with a code and a fix). Nothing is printed here, so an error
    writing stdout is never taken for one reading the tree. skip: read_tree's; notes: more
    notes (a remote member's: the members its last pull left out)."""
    folders, ordered, notes, _info = _collect(root, now, skip, notes)
    out = ["%s: %s from %s (%s)%s" % (channel, _counted(len(ordered), "entry", "entries"),
                                      _counted(len(folders), "member", "members"), root,
                                      ", as of this box's last sync" if synced else "")]
    shown = ordered[-last:] if last else ordered
    out += lines(shown, full)
    # a note holds IDs, times and paths from members' files too
    out += [pathrules.printable(note) for note in notes]
    return out


def view_json(root, channel, synced=False, full=False, last=None, now=None, skip=None,
              notes=()):
    """The view as one JSON object (the module's docstring has its fields); OSError when the
    root can't be read. skip and notes: view's."""
    folders, ordered, notes, info = _collect(root, now, skip, notes)
    shown = ordered[-last:] if last else ordered
    items = []
    for item in shown:
        e = item.e
        items.append({"time": e.time or None, "id": e.id, "name": e.name, "number": e.number,
                      "to": list(e.to), "re": e.re or None, "title": e.title, "file": item.path,
                      "header": [[k or "", v] for k, v in e.header] if full else None,
                      "body": e.body if full else None})
    return {"channel": channel, "folder": root, "synced": synced, "members": folders,
            "member_info": info, "count": len(ordered), "entries": items,
            "notes": [n.removeprefix("note: ") for n in notes]}
