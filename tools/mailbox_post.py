"""Post an entry to a channel member's file, atomically (DESIGN §14 M10, and MAILBOX.md and
WATCHING.md in the vcharon folder).

    python3 mailbox_post.py FILE --me laptop-api --to @laptop-ui --title "step 3 done" < body.md
    python3 mailbox_post.py FILE --me laptop-api --to @all --re laptop-ui#3 --title "steps" \
        --body "one line"

FILE is in the poster's own folder in a channel's tree: walking up from FILE's folder, the
first folder that holds MEMBER.md is the own folder, and its name is the poster's name (a file
in a subfolder is still posted by the own folder's member). --me <name> is the poster's own
name, checked against that folder: in a channel root (a local member's, or a server's) every
member's folder is writable, so --me is required there; in a remote member's local tree (under
vcharon's joined/ base) the tree already names its member, and --me is optional. It appends

    ## <local time YYYY-mm-dd HH:MM> — <name>#<n> — <title>
    to: @<name> ...
    re: <id>

    <body>

to FILE, creating FILE with a `# <file name without extension>` line if it's missing. <n> is
one more than the largest <name>#<n> in any entry heading of any .md file of the own folder
(headings only, never bodies); a lock in vcharon's state dir, one per own folder, covers the
read, the append and the swap, so two posts at once can't take one number or lose an entry.
The time is the clock's when it writes: an agent never types a time.

--to is required: @<name> separated by spaces, or @all, which only the leader posts (the
leader MEMBER.md names). A name that isn't a folder in the local tree is posted anyway, with a
note on stderr: it may not have synced yet. --re is optional, an ID (<name>#<n>), checked for its
form only. The body comes from --body or from stdin, in UTF-8, and is never interpreted: give
it a file or a quoted heredoc (<<'EOF'), and the shell can't run any of it either. It goes
after the header's blank line, and a body line that starts like a Markdown heading (`# `, `## `
… `###### `) gets `> ` in front, so a body can't forge a header or a heading.

It refuses, writing nothing: a FILE outside a member's folder; a FILE in another member's
folder, or one in a channel root without --me; MEMBER.md and CHANNEL.md (vcharon
channel writes them); a FILE that isn't a .md file (the watcher and the numbering read only
those); a FILE whose name is the same on macOS or Windows as another name next to it
(ANSWERS.md next to answers.md: a client there would refuse the whole tree), or one in a folder
that has such a twin (Mac/ next to mac/). Appending to the exact name is fine. A folder with a
lowercase name next to twins that aren't valid writers' names (debian/ next to Debian/) gets a
note on stderr, and the post goes on: it makes no new twin. At the top of the tree clients leave
such a stray out; below it, the twin blocks macOS and Windows clients.

The whole new content goes to a temp file in FILE's folder whose name starts with
.vcharon-stage- (vcharon and mailbox_watch.py skip those names), then replaces FILE in one step:
a reader, or a vcharon run, sees the old file or the new one, never half of it. On Windows the
swap fails while another program has the file open (a vcharon run reading it); it's tried a few
times before giving up.

Standard library only, Python 3.9 or newer, Windows, macOS and Linux. It lives outside the
vcharon package, as mailbox_watch.py does, and posts through its entries module.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# the repo's src folder, which holds the vcharon package, for its rules and entries.py
VCHARON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _vcharon():
    # the vcharon package next to this tool, never one on PATH (DESIGN §13)
    if VCHARON_DIR not in sys.path:
        sys.path.insert(0, VCHARON_DIR)
    from vcharon import pathrules
    return pathrules


def _entries():
    _vcharon()
    from vcharon import entries
    return entries


_OSES = (("darwin", "macOS"), ("windows", "Windows"))


def _twins_in(folder, name, fold):
    """(whether name is in folder, the other names in it that are name on macOS or Windows,
    the OS names that fold them together). A folder that can't be listed has none: the write
    then fails or works on its own."""
    try:
        names = os.listdir(folder)
    except OSError:
        return False, [], ""
    others = []
    osns = set()
    for other in sorted(names):
        if other == name:
            continue
        hit = [osn for osn, _ in _OSES if fold(other, osn) == fold(name, osn)]
        if hit:
            others.append(other)
            osns.update(hit)
    return name in names, others, " and ".join(shown for osn, shown in _OSES if osn in osns)


def _listed(names):
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]


def twin(path):
    """("refuse", line), ("note", line) or None, for path's name and its folder's name
    against the other names next to them, as macOS or Windows compare names (vcharon's
    pathrules.fold): a client there would refuse the tree (DESIGN §10.1 collisions)."""
    rules = _vcharon()
    fold = rules.fold
    folder, name = os.path.split(os.path.abspath(path))
    there, others, on = _twins_in(folder, name, fold)
    if others:
        if there or len(others) > 1:
            # the twins exist already: pointing at one of them would leave the other in place
            return "refuse", ("%s are one name on %s: remove or rename one first"
                              % (_listed(sorted([name] + others)), on))
        return "refuse", ("%s and %s are one name on %s: post to %s"
                          % (name, others[0], on, others[0]))
    parent, dname = os.path.split(folder)
    if not dname:
        return None
    there, others, on = _twins_in(parent, dname, fold)
    if not others:
        return None
    shown = sorted([dname + "/"] + [o + "/" for o in others])
    valid = [o for o in others if rules.writer_problem(o) is None]
    if rules.writer_problem(dname) is None and not valid:
        # debian/ next to Debian/: the post makes no new twin, so it goes on. This tool can't
        # tell where the tree's top is: at the top clients leave the stray out (DESIGN §12),
        # below it the twin blocks those clients. The note says both; the twin should go.
        return "note", ("%s are one folder on %s: remove or rename %s (at the top of the tree "
                        "clients leave it out; below it, %s clients get nothing until one is "
                        "removed)" % (_listed(shown), on, _listed([o + "/" for o in others]),
                                      on))
    if rules.writer_problem(dname) is not None and len(valid) == 1 and len(others) == 1:
        return "refuse", ("%s are one folder on %s: post into %s/"
                          % (_listed(shown), on, valid[0]))
    return "refuse", "%s are one folder on %s: remove or rename one first" % (_listed(shown), on)


def _utf8_output():
    """Output in UTF-8, whatever the console's code page (a Windows client's may be 936)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def leader_of(own):
    """The leader MEMBER.md in the own folder names (its leader: line), or None."""
    entries = _entries()
    try:
        found = entries.parse_file(os.path.join(own, entries.MEMBER_FILE))
    except OSError:
        return None
    for key, value in (found[0].header if found else []):
        if key == "leader":
            return value
    return None


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def member_of_tree(own):
    """For an own folder in a remote member's local tree (<joined base>/<C>.<name>/, vcharon
    channel's), the member whose tree it is; None for any other tree (a server member's
    channel folder). This box's copy of another member's folder holds a MEMBER.md too, so
    the walk up alone would post as that member."""
    _vcharon()
    from vcharon import platform
    tree = os.path.dirname(own)
    if not _same(os.path.dirname(tree), os.path.expanduser(platform.joined_dir())):
        return None
    return os.path.basename(tree).partition(".")[2] or None


def _refuse(text):
    # a vcharon command in it as this box runs vcharon (M14a): someone runs it as printed
    _vcharon()
    from vcharon import platform
    print("mailbox_post: %s" % platform.runnable(text), file=sys.stderr)
    return 1


def main(argv=None, stdin=None, clock=time.time):
    _utf8_output()
    parser = argparse.ArgumentParser(prog="mailbox_post.py", description="Post an entry to "
                                     "a channel member's file, atomically.")
    parser.add_argument("file", metavar="FILE", help="the .md file, in your own folder")
    parser.add_argument("--to", nargs="+", help="required: @<name> ..., or @all (the "
                        "leader only); one argument or several")
    parser.add_argument("--me", metavar="NAME", help="your own member name; required in a "
                        "channel root, where every member's folder is writable")
    parser.add_argument("--re", metavar="ID", help="the entry this answers: <name>#<n>")
    parser.add_argument("--title", required=True, help="the entry's title, one line")
    parser.add_argument("--body", help="the body; without it, stdin is the body. A body line "
                        "that starts like a Markdown heading ('# ' to '###### ') gets '> ' in "
                        "front, so it can't pass for an entry's heading")
    args = parser.parse_args(argv)
    # a quoted "~/…" reaches us unexpanded (Git Bash, an agent's quoting): expand it, as the
    # watcher does its --dir
    args.file = os.path.expanduser(args.file)
    entries = _entries()
    title = args.title.strip()
    if not title or entries.one_line_problem(title):
        parser.error("--title: one line, not empty, with no line break of any kind")
    if args.re is not None and entries.parse_id(args.re) is None:
        parser.error("--re: %s isn't an ID (<name>#<n>)" % args.re)
    to = [t for arg in args.to or () for t in arg.split()]
    for token in to:
        problem = entries.to_problem(token)
        if problem:
            parser.error("--to: %s" % problem)
    if args.body is not None:
        body = args.body
    else:
        data = (stdin if stdin is not None else sys.stdin.buffer).read()
        try:
            # utf-8-sig: a body saved by a Windows editor may start with a BOM
            body = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            parser.error("the body on stdin isn't UTF-8")
    if not body.strip():
        parser.error("the body is empty")
    path = os.path.abspath(args.file)
    if not os.path.isdir(os.path.dirname(path)):
        parser.error("no folder for %s: post into your own folder" % args.file)
    base = os.path.basename(path)
    if base.casefold() in (entries.MEMBER_FILE.casefold(), entries.CHANNEL_FILE.casefold()):
        return _refuse("%s is vcharon channel's to write: post into another file (RESULTS.md, "
                       "say)" % base)
    # then a twin refusal, which points at the folder or file to use, wherever it is
    found = twin(args.file)
    if found is not None and found[0] == "refuse":
        return _refuse(found[1])
    own = entries.own_folder(path)
    if own is None:
        return _refuse("%s: not in a channel member's folder (no %s in its folder or above; "
                       "vcharon channel join writes it)" % (args.file, entries.MEMBER_FILE))
    name = os.path.basename(own)
    if not base.endswith(".md"):
        return _refuse("%s: entries go in .md files (the watcher and the numbering read only "
                       "those)" % base)
    if not to:
        parser.error("--to is required: @<name> ..., or @all")
    if _vcharon().writer_problem(name) is not None:
        return _refuse("%s: the own folder's name %s isn't a member's name" % (args.file, name))
    mine = member_of_tree(own)
    if mine is not None and mine != name:
        return _refuse("%s: that is %s's folder, not yours: post into %s/ (a copy of the other "
                       "members' folders is never sent back)" % (args.file, name, mine))
    # In a channel root any member can write any folder (M11a): only --me says whose this is.
    if mine is None and args.me is None:
        return _refuse("%s: in a channel root, pass --me <your name>: every member's folder is "
                       "writable there" % args.file)
    if args.me is not None and args.me != name:
        return _refuse("%s is %s's folder, not %s's: post only in your own"
                       % (args.file, name, args.me))
    if entries.ALL in to:
        leader = leader_of(own)
        if leader != name:
            return _refuse("@all is the leader's (%s): address members by name"
                           % (leader or "MEMBER.md names none"))
    tree = os.path.dirname(own)
    for token in to:
        if token != entries.ALL and not os.path.isdir(os.path.join(tree, token[1:])):
            print("mailbox_post: note: %s has no folder in %s yet: posted anyway (it may not "
                  "have synced)" % (token, tree), file=sys.stderr)
    if found is not None:
        print("mailbox_post: note: %s" % found[1], file=sys.stderr)
    _vcharon()
    from vcharon.proto import VCharonError
    try:
        id_, when = entries.post(path, own, name, title, to, args.re, body, clock=clock)
    except VCharonError as e:
        return _refuse("%s%s" % (e.message, "; %s" % e.hint if e.hint else ""))
    except OSError as e:
        return _refuse("can't write %s: %s" % (args.file, e.strerror or e))
    print("posted %s — %s to %s at %s" % (id_, title, args.file, when))
    return 0


if __name__ == "__main__":
    sys.exit(main())
