"""Channel entries (DESIGN §14 M10): their text, parsing them, their numbers, and appending one
under a lock; client and server member (vcharon join, post and watch).

An entry is a heading with the poster's ID, a header up to the first blank line, and a body:

    ## 2026-10-02 10:12 — mac-web#7 — step 3 done
    to: @laptop-ui
    re: laptop-ui#3

    <body>
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import stat
import tempfile
import time

from . import pathrules, platform
from .lock import Lock
from .proto import VCharonError

# the heading's time: local, to the minute (post's since M7b)
TIME_FORMAT = "%Y-%m-%d %H:%M"
MEMBER_FILE = "MEMBER.md"
CHANNEL_FILE = "CHANNEL.md"
# between the heading's parts
DASH = " — "
# a body line that would read as a Markdown heading, an entry's own included (Markdown allows
# up to 3 spaces before the #): it gets "> " in front
HEADING = re.compile(r"^(?= {0,3}#{1,6}(?:[ \t\r]|$))", re.M)
# <name>#<n>: a writer's name, then a number from 1
_ID = re.compile(r"\A([a-z0-9][a-z0-9_-]{0,31})#([1-9][0-9]{0,8})\Z")
# a to: token: @all, or @ and a writer's name
_TO = re.compile(r"\A@[a-z0-9][a-z0-9_-]{0,31}\Z")
ALL = "@all"
# what str.splitlines() takes for a line break. Entries split on "\n" only (a trailing "\r"
# dropped), so a body holding one of the others can't make a line that starts a heading; a
# heading or header value holding any of them is refused, so it can't look like two lines in
# another reader either.
LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
# os.replace on Windows: tries, and the pause between them, while another program has the
# file open (a sync reading it)
REPLACE_TRIES = 5
REPLACE_PAUSE = 0.2
# how long a post waits for another post into the same folder
LOCK_WAIT = 30.0
LOCK_POLL = 0.05


@dataclasses.dataclass
class Entry:
    """One entry of a file. name and number are None for a heading without an ID."""

    heading: str          # the heading line, without the "## "
    time: str             # the heading's time text, or None
    name: str
    number: int
    title: str
    to: tuple             # the to: tokens, as written ("@all", "@mac-web")
    re: str               # re:'s value, or None
    header: list          # [(key, value)] of the other header lines, in order
    body: str
    line: int             # the heading's line number, from 1

    @property
    def id(self):
        return None if self.name is None else "%s#%d" % (self.name, self.number)


def parse_id(text):
    """(name, number) of <name>#<n>, or None."""
    m = _ID.match(text)
    return None if m is None else (m.group(1), int(m.group(2)))


def to_problem(token):
    """Why token can't be a to: address, or None."""
    if not _TO.match(token):
        return "%s isn't @all or @ and a member's name" % pathrules.show(token)
    return None


def parse_heading(text):
    """(time, name, number, title) of a heading's text after "## "; name and number None for a
    heading without an ID, time None for one without a " — "."""
    parts = text.split(DASH, 2)
    if len(parts) == 3:
        found = parse_id(parts[1])
        if found is not None:
            return parts[0], found[0], found[1], parts[2]
    if len(parts) >= 2:
        return parts[0], None, None, text.split(DASH, 1)[1]
    return None, None, None, text


def split_lines(text):
    """text's lines, split on "\n" only, each without a trailing "\r": never splitlines(),
    which also splits on \x85, \u2028 and the like (quote_body's ^ doesn't)."""
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def one_line_problem(value):
    """Why value can't be a heading or header value, or None."""
    if any(c in LINE_BREAKS for c in value):
        return "it holds a line break (%r)" % value
    return None


def parse(text):
    """The entries of a file's text, in file order. A line that starts with "## " is a
    heading: a posted body never has one (its heading-like lines get "> ")."""
    lines = split_lines(text)
    entries = []
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        heading = lines[start][3:]
        when, name, number, title = parse_heading(heading)
        to, re_, header = (), None, []
        i = start + 1
        while i < end and lines[i].strip():
            key, colon, value = lines[i].partition(":")
            value = value.strip()
            if colon and key == "to":
                to = tuple(value.split())
            elif colon and key == "re":
                re_ = value
            else:
                header.append((key.strip() if colon else "", value if colon else lines[i]))
            i += 1
        body = "\n".join(lines[i + 1:end] if i < end else []).strip("\n")
        entries.append(Entry(heading, when, name, number, title, to, re_, header, body,
                             start + 1))
    return entries


def read_text(path):
    """A file's text; bytes that aren't UTF-8 are replaced, so one bad byte hides nothing
    else."""
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


def parse_file(path):
    return parse(read_text(path))


def quote_body(body):
    """The body as posted: trailing newlines dropped, each heading-like line behind "> "."""
    return HEADING.sub("> ", body.rstrip("\r\n"))


def build(when, name, number, title, to, re_=None, header=(), body=""):
    """The text appended for one entry: a blank line, the heading, the header (to:, re:, then
    header's lines), and after a blank line the body. No line of the header can come from the
    body, so a body can't forge one."""
    for value in [title] + [v for _, v in header] + ([re_] if re_ else []):
        problem = one_line_problem(value)
        if problem:
            raise VCharonError("config", "a heading or header value is one line: %s" % problem)
    lines = ["", "## %s%s%s#%d%s%s" % (when, DASH, name, number, DASH, title),
             "to: %s" % " ".join(to)]
    if re_:
        lines.append("re: %s" % re_)
    lines += ["%s: %s" % (k, v) for k, v in header]
    text = "\n".join(lines) + "\n"
    body = quote_body(body)
    if body:
        text += "\n" + body + "\n"
    return text


def stamp(t):
    return time.strftime(TIME_FORMAT, time.localtime(t))


# --- the own folder and its numbers ---

def _is_file(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def own_folder(path, top=None):
    """The member's own folder of a file at path: walking up from its folder, the first that
    holds MEMBER.md; never above top (the tree), nor the file system's root. None if
    none."""
    folder = os.path.dirname(os.path.abspath(path))
    top = os.path.abspath(top) if top is not None else None
    while True:
        if _is_file(os.path.join(folder, MEMBER_FILE)):
            return folder
        if top is not None and os.path.normcase(folder) == os.path.normcase(top):
            return None
        up = os.path.dirname(folder)
        if up == folder:
            return None
        folder = up


def md_files(folder, onerror=None):
    """Every .md file in folder and its subfolders, sorted; symlinks and stage files left
    out. os.walk skips a folder it can't list without a word; onerror(OSError), if given, is
    told (vcharon read names it)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder, onerror=onerror):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(pathrules.STAGE_PREFIX))
        for f in sorted(filenames):
            path = os.path.join(dirpath, f)
            if f.endswith(".md") and not f.startswith(pathrules.STAGE_PREFIX) and _is_file(path):
                found.append(path)
    return found


def next_number(own, name):
    """One more than the largest <name>#<n> in any entry heading of any .md file of the own
    folder (headings only, never bodies)."""
    high = 0
    for path in md_files(own):
        try:
            text = read_text(path)
        except OSError:
            continue
        for line in split_lines(text):
            if line.startswith("## "):
                when, who, number, title = parse_heading(line[3:])
                if who == name and number > high:
                    high = number
    return high + 1


# --- appending, under the own folder's lock ---

def lock_path(own):
    """The post lock of an own folder: in the state dir, keyed by os.path.normcase of its
    realpath, so two spellings of one folder (through a symlink, with "..", and on Windows in
    another case) take one lock. macOS's normcase doesn't fold case."""
    key = os.path.normcase(os.path.realpath(own))
    digest = hashlib.sha256(os.fsencode(key)).hexdigest()[:12]
    return os.path.join(platform.state_dir(), "mailbox-post-%s.lock" % digest)


class _Held:
    def __init__(self, lk):
        self.lk = lk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.lk.release()


def lock(own, wait=LOCK_WAIT, sleep=time.sleep, clock=time.monotonic):
    """The own folder's post lock, held (a context manager). Two posts at once, by one agent
    or two sessions, then can't take one number or lose an entry. busy after wait seconds."""
    path = lock_path(own)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lk = Lock.open(path)
    except OSError as e:
        raise VCharonError("io", "can't open the post lock %s: %s" % (path, e.strerror or e),
                           "check the state dir")
    deadline = clock() + wait
    try:
        while not lk.try_acquire():
            if clock() >= deadline:
                raise VCharonError("busy", "another post into %s held %s for %d s"
                                   % (own, path, wait), "try again")
            sleep(LOCK_POLL)
    except BaseException:
        lk.release()
        raise
    return _Held(lk)


def _replace(src, dst, sleep=time.sleep):
    for left in range(REPLACE_TRIES - 1, -1, -1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not left:
                raise
            sleep(REPLACE_PAUSE)


def append(path, text):
    """Appends text to path atomically: the whole new file goes to a stage-named temp file in
    the same folder (vcharon and the watcher skip those names), then replaces path. A missing
    file starts with a "# <stem>" line. Call it under lock()."""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, "rb") as f:
            old = f.read()
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        stem = os.path.splitext(os.path.basename(path))[0]
        old = ("# %s\n" % stem).encode("utf-8")
        # what a plain open() would have made
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    if old and not old.endswith(b"\n"):
        old += b"\n"
    _swap(path, folder, old + text.encode("utf-8"), mode)


def _swap(path, folder, data, mode):
    """data as path's whole new content: a stage-named temp file in folder, then one replace;
    mode is the file's."""
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=pathrules.STAGE_PREFIX + "post-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp makes it 0600; keep the file's own mode (Windows keeps only read-only)
        os.chmod(tmp, mode)
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def header_of(text, name, number=1):
    """The other header lines (to: and re: aside) of the entry name#number in text, as a dict,
    the first value of each key; {} when text has no such entry. MEMBER.md's name#1 holds
    the member's fields: the claim, vcharon list and set_header all read that one entry."""
    for entry in parse(text):
        if entry.name == name and entry.number == number:
            out = {}
            for key, value in entry.header:
                if key and key not in out:
                    out[key] = value
            return out
    return {}


def set_header(path, own, name, number, key, value):
    """Sets key: value in the header of the entry name#number of path (MEMBER.md's #1), in
    place, under the own folder's lock: the key's line replaced, or added at the header's end
    when it has none. Every other byte stays; the new file replaces the old in one step, as
    append's. Refused when path has no such entry."""
    problem = one_line_problem(value)
    if problem:
        raise VCharonError("config", "a header value is one line: %s" % problem)
    folder = os.path.dirname(os.path.abspath(path))
    with lock(own):
        if not _is_file(path):
            raise VCharonError("unsafe_path", "%s isn't a regular file (a symlink, or gone)"
                               % path, "ask the user")
        with open(path, "rb") as f:
            raw = f.read()
        mode = os.stat(path).st_mode & 0o7777
        # each line with its own end; split after "\n" only, as split_lines
        lines = re.findall(rb"[^\n]*\n|[^\n]+\Z", raw)
        start = None
        for i, line in enumerate(lines):
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text.startswith("## "):
                when, who, n, title = parse_heading(text[3:])
                if who == name and n == number:
                    start = i
                    break
        if start is None:
            raise VCharonError("channel", "%s has no entry %s#%d" % (path, name, number),
                               "ask the user")
        end = b"\r\n" if lines[start].endswith(b"\r\n") else b"\n"
        new = ("%s: %s" % (key, value)).encode("utf-8") + end
        i = start + 1
        # the header ends at a blank line, or at the next heading (parse's rule too)
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(b"## "):
            if lines[i].decode("utf-8", "replace").partition(":")[0].strip() == key:
                lines[i] = new
                break
            i += 1
        else:
            if i == len(lines) and lines and not lines[-1].endswith(b"\n"):
                lines[-1] += end
            lines.insert(i, new)
        _swap(path, folder, b"".join(lines), mode)


def post(path, own, name, title, to, re_=None, body="", header=(), clock=time.time,
         number=None, check=None):
    """Appends one entry of name to path, in the own folder own, under its lock; the number is
    the next one (or number, for MEMBER.md's #1 and CHANNEL.md's #2). Returns (id, time).
    check(path, the file's size after the append): called under the lock before anything is
    written; it refuses by raising."""
    with lock(own):
        n = number if number is not None else next_number(own, name)
        when = stamp(clock())
        text = build(when, name, n, title, to, re_, header, body)
        if check is not None:
            check(path, size_after(path, text))
        append(path, text)
    return "%s#%d" % (name, n), when


def size_after(path, text):
    """path's size in bytes once append(path, text) has run."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            old = f.tell()
            if old:
                f.seek(-1, os.SEEK_END)
                old += f.read(1) != b"\n"
    except FileNotFoundError:
        stem = os.path.splitext(os.path.basename(path))[0]
        old = len(("# %s\n" % stem).encode("utf-8"))
    return old + len(text.encode("utf-8"))
