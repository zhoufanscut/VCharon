"""The two kinds of channel, work channel and lobby, as objects the commands ask, so a call
site holds no test of the channel's name or kind (DESIGN, "The lobby"). A plain test stays only
where the flow itself differs: join making the lobby, the refusals of create lobby and close,
join's print, and whoami's presence view. On both ends: the helper's claim writes a lobby's
CHANNEL.md.

A lobby keeps 30 days of day files, chat-YYYY-MM-DD.md, in each member's folder: the post that
makes today's file deletes the poster's own day files from before that (cleanup)."""

from __future__ import annotations

import datetime
import os
import re
import stat

from . import charter

# the one lobby of a channel root: "join the lobby in devbox" is then a whole instruction
LOBBY_NAME = "lobby"
DAY_PREFIX = "chat-"
_DAY_FILE = re.compile(r"\Achat-([0-9]{4})-([0-9]{2})-([0-9]{2})\.md\Z")
# day files kept, today's included
KEEP_DAYS = 30


class Work:
    """A channel made by create: a leader, a plan, a close."""

    name = "work"
    format = charter.WORK_FORMAT
    can_close = True
    has_plan = True
    # the watcher's next: line after the leader's CLOSED to @all
    closing_line = True
    # the watcher keeps every heading hash (a work channel ends)
    prune_heads = False

    def may_post_all(self, folder, leader):
        """Whether an @all from folder is to all: the leader's only, the record's leader, never
        whoever holds CHANNEL.md."""
        return folder == leader

    def day_file(self, when):
        """The file a post with no --file goes into, from its heading's time; None for
        RESULTS.md."""
        return

    def announce_to(self, name, leader):
        """The to: of name's JOIN, REJOIN, LEAVE and MEMBER.md #1."""
        return "@" + leader

    def silent(self, entry, folder):
        """Whether another member's watcher tells nothing of entry, read in folder."""
        return False

    def cleanup_for(self, own, path, created):
        """The paths a post into path deletes after it (cleanup): none in a work channel."""
        return []


class Lobby(Work):
    """The root's lobby: no leader, no plan, no end; day files, any member's @all."""

    name = "lobby"
    format = charter.LOBBY_FORMAT
    can_close = False
    has_plan = False
    closing_line = False
    prune_heads = True

    def may_post_all(self, folder, leader):
        return True

    def day_file(self, when):
        return "%s%s.md" % (DAY_PREFIX, when[:10])

    def announce_to(self, name, leader):
        # to the member itself: every agent rejoins at each session, and a wake is a model
        # turn for every member that watches
        return "@" + name

    def silent(self, entry, folder):
        return tuple(entry.to) == ("@" + folder,)

    def cleanup_for(self, own, path, created):
        """The own folder's day files a post that created today's day file path deletes: those
        dated more than KEEP_DAYS - 1 days before its date. None for any other post."""
        day = day_of(os.path.basename(path))
        if not created or day is None or os.path.dirname(os.path.abspath(path)) != \
                os.path.abspath(own):
            return []
        return [os.path.join(own, n) for n in old_day_files(own, day)]


WORK = Work()
LOBBY = Lobby()


def of_name(text):
    """The kind of a record's, a listing's or CHANNEL.md's kind value: "lobby", or a work
    channel for None (a channel or record written without one)."""
    return LOBBY if text == charter.LOBBY_KIND else WORK


def of(record):
    """The kind of a join record (one without the key is a work channel's)."""
    return of_name(record.get("kind"))


def day_of(name):
    """The date of a day file's name (chat-YYYY-MM-DD.md, a real date), or None."""
    m = _DAY_FILE.match(name)
    if m is None:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def old_day_files(own, today):
    """The names, sorted, of the regular files (by lstat: a link is never followed) at the top
    of own that are day files dated more than KEEP_DAYS - 1 days before today (a date)."""
    cutoff = today - datetime.timedelta(days=KEEP_DAYS - 1)
    try:
        names = os.listdir(own)
    except OSError:
        return []
    out = []
    for n in sorted(names):
        day = day_of(n)
        if day is None or day >= cutoff:
            continue
        try:
            if stat.S_ISREG(os.lstat(os.path.join(own, n)).st_mode):
                out.append(n)
        except OSError:
            continue
    return out


def cleanup(paths):
    """Deletes paths (cleanup_for's); (the names removed, [(name, the OS's message)] of those
    that couldn't be)."""
    removed, failed = [], []
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as e:
            failed.append((os.path.basename(path), e.strerror or str(e)))
            continue
        removed.append(os.path.basename(path))
    return removed, failed
