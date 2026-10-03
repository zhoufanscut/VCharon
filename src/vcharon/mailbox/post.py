"""vcharon post C: post an entry to your own folder in a channel, atomically (the guide's
post topic).

    vcharon post game --to @mac-ui --title "step 3 done" < body.md
    vcharon post game --to @all --re mac-ui#3 --title "steps" --steps --body "one line"
    vcharon post game --to @mac-ui --title "log" --file logs/run.md < run.md

A member posts into RESULTS.md in its own folder: results, questions, DONE, and the leader's
answers too. The leader's plan goes into STEPS.md (--steps, the leader only); any other .md
file of the own folder, a subfolder's too, with --file (NOTES.md, logs/run.md). vcharon writes
MEMBER.md and CHANNEL.md itself. The command line finds the own folder from your membership
(DESIGN, "Which membership"); post() below takes the file's path, and checks it. It appends

    ## <local time YYYY-mm-dd HH:MM:SS> — <name>#<n> — <title>
    to: @<name> ...
    re: <id>

    <body>

to the file, creating it with a `# <file name without extension>` line if it's missing. <n> is
one more than the largest <name>#<n> in any entry heading of any .md file of the own folder
(headings only, never bodies); a lock in vcharon's state dir, one per own folder, covers the
read, the append and the swap, so two posts at once can't take one number or lose an entry.
The time is the clock's when it writes: an agent never types a time.

--to is required: @<name> separated by spaces, or @all, which only the leader posts (the
leader MEMBER.md names). An @<name> that isn't a folder in the local tree is posted anyway, with
a note on stderr: it may not have synced yet. A bare <name> (no @) is taken as @<name> when it
is a member's folder in the tree; any other is refused with the members' names, since a typo
there would go to nobody. --re is optional, an ID (<name>#<n>, an @ in front taken off),
checked for its form only. The body comes from --body or from stdin, in UTF-8, and is never
interpreted: give it a file or a quoted heredoc (<<'EOF'), and the shell can't run any of it
either. It goes after the header's blank line, and a body line that starts like a Markdown
heading (`# `, `## ` … `###### `) gets `> ` in front, so a body can't forge a header or a
heading.

It refuses, writing nothing: a body bigger than the channel's entry limit (1 MB unless its
leader set another); an entry file that would grow past it, and an own folder that would hold
more than the channel's folder limits (50 MB and 1,000 files unless set), with this entry; a
file outside a member's folder, or in another member's folder;
MEMBER.md and CHANNEL.md; a file that isn't a .md file (the watcher and the numbering read only
those); a file whose name is the same on macOS or Windows as another name next to it
(ANSWERS.md next to answers.md: a client there would refuse the whole tree), or one in a folder
that has such a twin (Mac/ next to mac/). Appending to the exact name is fine. A folder with a
lowercase name next to twins that aren't valid writers' names (debian/ next to Debian/) gets a
note on stderr, and the post goes on: it makes no new twin. At the top of the tree clients leave
such a stray out; below it, the twin blocks macOS and Windows clients.

The whole new content goes to a temp file in the file's folder whose name starts with
.vcharon-stage- (vcharon and the watcher skip those names), then replaces the file in one
step: a reader, or a sync, sees the old file or the new one, never half of it. On Windows the
swap fails while another program has the file open (a sync reading it); it's tried a few
times before giving up.
"""

from __future__ import annotations

import os
import sys
import time

from .. import charter, entries, fsops, pathrules, platform
from ..proto import VCharonError

# the files post writes: a member's, and the leader's plan (--steps)
RESULTS_FILE = "RESULTS.md"
STEPS_FILE = "STEPS.md"


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
    pathrules.fold): a client there would refuse the tree (DESIGN, "Path rules" collisions)."""
    fold = pathrules.fold
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
    valid = [o for o in others if pathrules.writer_problem(o) is None]
    if pathrules.writer_problem(dname) is None and not valid:
        # debian/ next to Debian/: the post makes no new twin, so it goes on. This tool can't tell
        # where the tree's top is: at the top clients leave the stray out (DESIGN, "Channel
        # sections"), below it the twin blocks those clients. The note says both; the twin should
        # go.
        return "note", ("%s are one folder on %s: remove or rename %s (at the top of the tree "
                        "clients leave it out; below it, %s clients get nothing until one is "
                        "removed)" % (_listed(shown), on, _listed([o + "/" for o in others]),
                                      on))
    if pathrules.writer_problem(dname) is not None and len(valid) == 1 and len(others) == 1:
        return "refuse", ("%s are one folder on %s: post into %s/"
                          % (_listed(shown), on, valid[0]))
    return "refuse", "%s are one folder on %s: remove or rename one first" % (_listed(shown), on)


def leader_of(own):
    """The leader MEMBER.md in the own folder names (its leader: line), or None."""
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
    join's), the member whose tree it is; None for any other tree (a local member's channel
    folder). This box's copy of another member's folder holds a MEMBER.md too, so the walk up
    alone would post as that member."""
    tree = os.path.dirname(own)
    if not _same(os.path.dirname(tree), os.path.expanduser(platform.joined_dir())):
        return None
    return os.path.basename(tree).partition(".")[2] or None


def _refuse(text, hint):
    return VCharonError("channel", text, hint)


def check_args(to, title, re_, body):
    """The usage errors of a post's options, before anything is read: (the --to tokens, the
    stripped title, the --re ID). A config error (exit 3) each. A bare member's name stays
    bare in the tokens: post() takes it as @<name> once it has seen the tree."""
    title = title.strip()
    if not title or entries.one_line_problem(title):
        raise VCharonError("config", "--title: one line, not empty, with no line break of any "
                           "kind", hint="give a one-line --title")
    if re_ is not None and re_.startswith("@"):
        # @mac-web#3 for mac-web#3: the address's form, given by habit
        re_ = re_[1:]
    if re_ is not None and entries.parse_id(re_) is None:
        raise VCharonError("config", "--re: %s isn't an ID (<name>#<n>)" % re_,
                           hint="give --re as <name>#<n>, the ID of the entry you answer")
    tokens = [t for arg in to or () for t in arg.split()]
    if not tokens:
        raise VCharonError("config", "--to is required: @<name> ..., or @all",
                           hint="give --to @<name>, or @all as the leader")
    for token in tokens:
        if is_bare(token):
            continue
        problem = entries.to_problem(token)
        if problem:
            raise VCharonError("config", "--to: %s" % problem, hint="give --to @<name> ...")
    if body is not None and not body.strip():
        raise VCharonError("config", "the body is empty", hint="give --body TEXT, or the body "
                           "on stdin")
    return tokens, title, re_


def is_bare(token):
    """Whether a --to token is a member's name without its @."""
    return not token.startswith("@") and entries.to_problem("@" + token) is None


def members_in(tree):
    """The member folders' names in a channel tree, sorted."""
    try:
        names = os.listdir(tree)
    except OSError:
        return []
    return sorted(n for n in names if pathrules.writer_problem(n) is None
                  and os.path.isdir(os.path.join(tree, n)))


def resolve_to(to, tree, channel=None):
    """The --to tokens with each bare member's name as @<name>; a bare name that isn't a
    member's folder in the tree is refused, naming the members."""
    out = []
    for token in to:
        if not is_bare(token):
            out.append(token)
            continue
        if not os.path.isdir(os.path.join(tree, token)):
            members = members_in(tree)
            raise _refuse("--to %s: not a member of %s (members: %s)"
                          % (token, channel or "this channel", ", ".join(members) or "none"),
                          "address one of the members above, by name or as @<name>; @all is "
                          "the leader's")
        out.append("@" + token)
    return out


def file_parts(file):
    """--file's name as path parts below the own folder: a name, or a subfolder's file with /
    (or \\). A usage error for an absolute path, an empty part, . or .., which would leave
    the own folder or name it oddly, and for a part a Windows or macOS member can't hold: one
    such file makes every such member's sync refuse the whole tree."""
    parts = file.replace("\\", "/").split("/")
    if (os.path.isabs(file) or file.startswith(("/", "\\")) or any(":" in p for p in parts)
            or any(p in ("", ".", "..") for p in parts)):
        raise VCharonError("config", "--file %s: a file of your own folder, as a name below "
                           "it (RESULTS.md, notes/run.md)" % file,
                           hint="give --file as a .md file's name in your own folder")
    for part in parts:
        for osn in ("windows", "darwin"):
            problem = pathrules.part_problem(part, osn)
            if problem:
                raise VCharonError("config", "--file %s: %s" % (file, problem),
                                   hint="pick a name every member's OS can hold")
    return parts


def check_own(own, tree=None):
    """The own folder itself, and tree (a remote member's local tree) above it, lstat'ed: a
    symlink (or a Windows junction) there is refused (unsafe_path), as file_below refuses one
    below it: the post would write wherever it points. A missing one is post()'s to
    refuse."""
    for path in ([tree] if tree is not None else []) + [own]:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as e:
            raise fsops.error(e, path)
        if fsops.kind(st) == fsops.LINK:
            raise VCharonError("unsafe_path", "%s is a symlink: a post never writes through "
                               "one" % path, "remove the link by hand (vcharon never makes "
                               "one there), then post again")


def file_below(own, parts):
    """The path of --file's parts below the existing own folder own, checked part by part
    with lstat: a symlink anywhere on the way, or something that isn't a folder, is refused
    (unsafe_path), since the post would write through it, outside the tree, and the numbering
    (entries.md_files) never reads through a link. A missing subfolder is refused too: make it
    first. The file itself may be missing; there, it must be a regular file."""
    path = own
    for i, part in enumerate(parts):
        path = os.path.join(path, part)
        last = i == len(parts) - 1
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if last:
                return path
            sub = "/".join(parts[:i + 1])
            raise _refuse("%s/ isn't in your own folder %s" % (sub, own),
                          "make %s/ in your own folder first, or post into %s"
                          % (sub, RESULTS_FILE))
        except OSError as e:
            raise fsops.error(e, path)
        kind = fsops.kind(st)
        if kind == fsops.LINK:
            raise VCharonError("unsafe_path", "%s is a symlink: a post never writes through "
                               "one" % path, "remove the link from your own folder, then post "
                               "again")
        if kind != (fsops.FILE if last else fsops.DIR):
            raise VCharonError("unsafe_path", "%s isn't a %s" % (path, "file" if last
                                                                  else "folder"),
                               "post into another name")
    return path


def body_from(data):
    """The body from stdin's bytes; utf-8-sig: a body saved by a Windows editor may start with
    a BOM."""
    try:
        body = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise VCharonError("config", "the body on stdin isn't UTF-8",
                           hint="give the body in UTF-8, or with --body")
    if not body.strip():
        raise VCharonError("config", "the body is empty", hint="give --body TEXT, or the body "
                           "on stdin")
    return body


def _too_big(text, hint):
    return VCharonError("too_big", text, hint)


BODY_HINT = ("shorten it: put long output in a file outside the channel, and say in the body "
             "where it is")
FILE_HINT = "post into a new file of your own folder: add --file NAME.md (RESULTS-2.md, say)"


def limit_check(own, limits):
    """entries.post's check for the channel's limits (charter): the entry file and the own
    folder, as they would be after the post. Refused (too_big) over either."""
    max_entry = limits["max_entry_kb"] * charter.KB
    max_bytes, max_files = limits["max_mb"] * charter.MB, limits["max_files"]

    def check(path, after):
        if after > max_entry:
            raise _too_big("%s would hold %s with this entry; an entry file holds at most %s "
                           "in this channel" % (path, charter.size_text(after),
                                                charter.size_text(max_entry)), FILE_HINT)
        size, files = charter.folder_total(own)
        try:
            st = os.lstat(path)
            size -= st.st_size
        except FileNotFoundError:
            files += 1
        text = charter.over(size + after, files, max_bytes, max_files)
        if text is not None:
            raise _too_big("with this entry your folder %s would hold %s" % (own, text),
                           charter.folder_hint(own, max_bytes, max_files))

    return check


def post(path, me, to, title, re_=None, body="", clock=None, limits=None, channel=None):
    """Posts one entry as me into the .md file path, in me's own folder; to and title as
    check_args returns them. Returns (the entry's ID, its time). Refuses (VCharonError)
    as the module's docstring says, writing nothing. clock: time.time, looked up when it
    posts (tests fake it). limits: the channel's (charter); a body bigger than its entry
    limit is refused first, the entry file and the own folder as they would be after the post
    under the folder's lock."""
    if limits is not None:
        max_entry = limits["max_entry_kb"] * charter.KB
        size = len(body.encode("utf-8"))
        if size > max_entry:
            raise _too_big("the body is %s; an entry file holds at most %s in this channel"
                           % (charter.size_text(size), charter.size_text(max_entry)),
                           BODY_HINT)
    path = os.path.abspath(path)
    shown = path
    if not os.path.isdir(os.path.dirname(path)):
        raise _refuse("no folder for %s: post into your own folder" % shown,
                      "join the channel again, with the --project and --role you joined with")
    base = os.path.basename(path)
    if base.casefold() in (entries.MEMBER_FILE.casefold(), entries.CHANNEL_FILE.casefold()):
        raise _refuse("%s is vcharon's to write" % base, "post into %s" % RESULTS_FILE)
    # then a twin refusal, which points at the folder or file to use, wherever it is
    found = twin(path)
    if found is not None and found[0] == "refuse":
        raise _refuse(found[1], "remove or rename one of those names in your own folder, then "
                      "post again")
    own = entries.own_folder(path)
    if own is None:
        raise _refuse("%s: not in a channel member's folder (no %s in its folder or above)"
                      % (shown, entries.MEMBER_FILE),
                      "join the channel again, with the --project and --role you joined with: "
                      "a rejoin writes %s" % entries.MEMBER_FILE)
    name = os.path.basename(own)
    if not base.endswith(".md"):
        raise _refuse("%s: entries go in .md files (the watcher and the numbering read only "
                      "those)" % base, "post into %s" % RESULTS_FILE)
    if pathrules.writer_problem(name) is not None:
        raise _refuse("%s: the own folder's name %s isn't a member's name" % (shown, name),
                      "ask the user")
    mine = member_of_tree(own)
    if mine is not None and mine != name:
        raise _refuse("%s: that is %s's folder, not yours (a copy of the other members' folders "
                      "is never sent back)" % (shown, name), "post into your own folder, %s/"
                      % mine)
    if me != name:
        raise _refuse("%s is %s's folder, not %s's" % (shown, name, me),
                      "post only in your own folder")
    tree = os.path.dirname(own)
    to = resolve_to(to, tree, channel)
    if entries.ALL in to:
        leader = leader_of(own)
        if leader != name:
            raise _refuse("@all is the leader's (%s)" % (leader or "MEMBER.md names none"),
                          "address members by name: --to @<name> ...")
    for token in to:
        if token != entries.ALL and not os.path.isdir(os.path.join(tree, token[1:])):
            print("note: %s has no folder in %s yet: posted anyway (it may not have synced)"
                  % (token, tree), file=sys.stderr)
    if found is not None:
        print("note: %s" % found[1], file=sys.stderr)
    check = limit_check(own, limits) if limits is not None else None
    try:
        return entries.post(path, own, name, title, to, re_, body, clock=clock or time.time,
                            check=check)
    except OSError as e:
        raise fsops.error(e, path)
