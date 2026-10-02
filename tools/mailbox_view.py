"""Show a whole vcharon channel, every member's entries merged in one order (DESIGN §14 M12,
MAILBOX.md in the vcharon folder). Read-only: it writes nothing and runs no vcharon.

For a local member, or any copy of a channel's tree:

    python3 mailbox_view.py --dir ~/.local/state/vcharon/channels/<C> [--full] [--last N]

For a client, its local tree of the channel section <C>.<name> (channels.d/), as of this box's
last sync; it runs no vcharon run, so vcharon run <C>.<name> first for a fresh view:

    python3 mailbox_view.py --job <C>.<name> [--config PATH] [--full] [--last N]

It reads every top-level folder whose name is a writer's (the member's own folder too; other
names, symlinks and stage dirs are left out, as the watcher leaves them out), and below each the
entries of every .md file (vcharon/entries.py's format) but the folder's MEMBER.md, whose #1 only
marks the folder. Lines, in this order:

    <C>: <n> entries from <m> members (<dir>)
                                   the first line; with --job, `, as of this box's last sync`
    <time>  <id>  <to>  [re <id>  ]<title>  (<folder>/<file>)
                                   one line an entry, oldest first; - for a part it lacks.
                                   --full adds the header's other lines and the body, indented
                                   by 4. --last N: only the newest N
    note: <text>                   after the list, one line each (below)

The order: an entry whose time is missing or doesn't parse (entries.TIME_FORMAT) comes first,
in path order. The rest go by their minute. Within one minute the time can't order them, so
there one member's entries go by number (#7 before #8: mailbox_post.py's lock makes that true),
and the entry a re: names goes before the entry naming it; of the entries that are free to go,
the smallest (name, number) goes first, the number compared as a number (#9 before #10).
Across minutes the time wins, even against re:. Only a placed entry is ordered by number and
re:: its ID's name is its folder's, and it's the first with that ID in path order (the
watcher's rule). Every entry is listed.

Notes: a folder or file it can't read (the rest is still shown); an entry with no ID, in a
folder not its ID's, or a second copy of an ID; an entry with no time, or a bad one; one stamped
after this box's current minute; one stamped in an earlier minute than the entry its re: names
(clocks differ?); a re: naming an ID not in the tree (not synced yet, or a typo); re: lines that
make a cycle within a minute, which then goes by number only.

Exit 0; 2 for a usage error (argparse's); 1 when the channel's folder can't be read (`ERROR
can't read <dir>: ...`) or --job's section can't be resolved (`mailbox_view: ...`), on stderr.
Standard library only, Python 3.9 or newer. It lives outside the vcharon package, as
mailbox_watch.py does, and reads through vcharon's entries and pathrules modules.
"""

from __future__ import annotations

import argparse
import datetime
import heapq
import os
import sys
import time

# this tool's folder, which holds mailbox_watch.py, and the repo's src folder, which holds the
# vcharon package
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
VCHARON_DIR = os.path.join(os.path.dirname(TOOLS_DIR), "src")
PROG = "mailbox_view"
INDENT = "    "


def _vcharon_import():
    # the vcharon package next to this tool, never one on PATH (DESIGN §13)
    if VCHARON_DIR not in sys.path:
        sys.path.insert(0, VCHARON_DIR)


def _watch():
    """The watcher next to this tool, for its mailbox_of and its output helpers."""
    if TOOLS_DIR not in sys.path:
        sys.path.insert(0, TOOLS_DIR)
    import mailbox_watch
    return mailbox_watch


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
        self.notes = []

    @property
    def label(self):
        """The entry's ID, or where it is when it has none."""
        return self.e.id or "%s line %d" % (self.path, self.e.line)

    def key(self):
        """Of the ready entries, the smallest goes first: (name, number), then where it is."""
        e = self.e
        return (e.name or self.folder, e.number or 0, self.path, e.line)


def read_tree(root):
    """(member folders, [Item] in path order, notes) of the channel tree root. An error on the
    root itself raises OSError; below it, what can't be read is a note."""
    _vcharon_import()
    from vcharon import entries, pathrules
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
    _vcharon_import()
    from vcharon import entries
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
        try:
            item.minute = datetime.datetime.strptime(when, entries.TIME_FORMAT)
        except (TypeError, ValueError):
            item.notes.append("note: %s has %s: listed first"
                              % (item.label, "no time" if not when else "a bad time %r" % when))
            untimed.append(item)
            continue
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
                and item.minute < target.minute):
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
            numbered.extend(zip(group, group[1:]))
        here = {n.e.id: n for n in mine}
        answers = [(here[n.e.re], n) for n in mine
                   if n.e.re in here and n.e.re != n.e.id]
        done = _kahn(nodes, numbered + answers)
        if done is None:
            # a wrong --re can make one: mailbox_post.py checks only its form
            notes.append("note: %s: re: lines make a cycle; that minute goes by number only"
                         % minute.strftime(entries.TIME_FORMAT))
            done = _kahn(nodes, numbered)
        out.extend(done)
    return out, notes


def lines(items, full):
    """The list's lines for items, in their order."""
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
    return out


def view(root, channel, synced=False, full=False, last=None, now=None, out=print):
    """Prints the view of the channel tree root; returns the exit code."""
    try:
        folders, items, read_notes = read_tree(root)
    except OSError as e:
        print("ERROR can't read %s: %s" % (root, _why(e)), file=sys.stderr)
        return 1
    if now is None:
        now = datetime.datetime.now()
    ordered, minute_notes = order(items, now)
    out("%s: %s from %s (%s)%s" % (channel, _counted(len(items), "entry", "entries"),
                                   _counted(len(folders), "member", "members"), root,
                                   ", as of this box's last sync" if synced else ""))
    shown = ordered[-last:] if last else ordered
    for line in lines(shown, full):
        out(line)
    for note in read_notes + [n for item in ordered for n in item.notes] + minute_notes:
        out(note)
    return 0


def main(argv=None, now=None):
    watch = _watch()
    watch._utf8_output()
    parser = argparse.ArgumentParser(prog="mailbox_view.py", description="Show a vcharon "
                                     "channel's entries, every member's, in one order.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dir", help="a channel's folder: in the channel root on this machine, "
                      "or any copy of its tree")
    mode.add_argument("--job", help="a client's channel section (channels.d/), <C>.<name>: "
                      "its local tree, as of this box's last sync")
    parser.add_argument("--config", help="with --job: vcharon's config file")
    parser.add_argument("--full", action="store_true", help="each entry's other header "
                        "lines and its body too")
    parser.add_argument("--last", type=watch._number(1, 1000000, "entries"), help="only the "
                        "newest N entries")
    args = parser.parse_args(argv)
    if args.dir is not None:
        if args.config:
            parser.error("--dir takes no --config")
        # a quoted "~/…" reaches us unexpanded, as the watcher's --dir does
        root = os.path.abspath(os.path.expanduser(args.dir))
        channel = os.path.basename(root)
        synced = False
    else:
        try:
            root = watch.mailbox_of(args.job, args.config, prog=PROG)[0]
        except SystemExit as e:
            print(e.code, file=sys.stderr)
            return 1
        # a section is [<channel>.<me>]
        channel = args.job.split(".", 1)[0]
        synced = True
    return view(root, channel, synced=synced, full=args.full, last=args.last, now=now)


if __name__ == "__main__":
    sys.exit(main())
