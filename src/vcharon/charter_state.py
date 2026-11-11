"""Client-only channel state: skipped folders, last-watched ages and version notes."""

from __future__ import annotations

import json
import os
import re
import tempfile

from . import channels, entries, pathrules, platform
from .charter import limit_text, skipped_note

# skipped_note's start, and the member it names
_LEFT_OUT = re.compile(r"\Aleft out ([a-z0-9][a-z0-9_-]{0,31})/: ")


def left_out_member(note):
    """The member a plan's note says the pull left out (skipped_note's), or None."""
    found = _LEFT_OUT.match(note)
    return found.group(1) if found else None


# --- what a remote member's last pull left out ---

def left_out_path(section):
    """The file where a channel section's down keeps the members its last pull left out:
    {"limit": the limits' text, "members": [names]}. The watcher and read take it from there
    (a streaming sync prints no notes). Names only: it changes when a member goes over or
    comes back, not each round while a folder grows."""
    return os.path.join(platform.state_dir(), section + ".down.left-out.json")


def save_left_out(section, notes, max_bytes, max_files):
    """Keeps the members a down plan's notes say it left out; written only when that changed,
    and removed when none is left out. Returns the names, sorted."""
    members = sorted({name for name in map(left_out_member, notes) if name is not None})
    path = left_out_path(section)
    if not members:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return members
    doc = {"limit": limit_text(max_bytes, max_files), "members": members}
    if load_left_out(section) == doc:
        return members
    data = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
    try:
        with open(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return members


def load_left_out(section):
    """save_left_out's document, or None: none saved, or one that can't be read or has
    another shape (it only adds notes, so nothing is refused for it)."""
    try:
        with open(left_out_path(section), "rb") as f:
            doc = json.loads(f.read(1 << 20).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(doc, dict) or not isinstance(doc.get("limit"), str)
            or not isinstance(doc.get("members"), list)
            or not all(isinstance(k, str) and pathrules.writer_problem(k) is None
                       for k in doc["members"])):
        return None
    return doc


def left_out_notes(section):
    """The texts of the WARN lines (a remote member's watcher) and of read's notes for the
    members the last pull left out: one per member, naming the limit and never the folder's
    size, so it stays one warning while the folder grows (as a local member's over_limit)."""
    doc = load_left_out(section)
    if doc is None:
        return []
    return [skipped_note(name, "over the channel's limit of %s" % doc["limit"])
            for name in sorted(doc["members"])]


# --- the members' last-watched ages, as a remote member's last pull brought them ---

# the cache is written at most this often while only its times move: a streaming watcher pulls
# every 2 s, and the stamps it reads move every 30 s at most (channels.SEEN_EVERY)
SEEN_SAVE_EVERY = 30
# a time that moved by this much or less is the same: each pull works it out again from an
# age in whole seconds
SEEN_SLACK = 2


def seen_path(section):
    """Where a channel section's down keeps the members' last-watched stamps its last pull
    brought: {"members": {name: [time on this box, pace]}}, the time being this box's clock
    less the age the server worked out, so no two machines' clocks are compared."""
    return os.path.join(platform.state_dir(), section + ".down.seen.json")


def save_seen(section, seen, now):
    """Keeps a down plan's seen ({name: [age, pace]}, checked here: a reply in another shape
    is refused with TypeError or ValueError, and nothing is written). Written when a member or
    a pace changed, or a time moved, but while only times move at most once every
    SEEN_SAVE_EVERY seconds by the file's own mtime. Returns whether it wrote."""
    if not isinstance(seen, dict):
        raise TypeError("not an object")
    members = {}
    for name, value in seen.items():
        if (pathrules.writer_problem(name) is not None or not isinstance(value, list)
                or len(value) != 2 or not isinstance(value[0], int)
                or isinstance(value[0], bool) or value[0] < 0
                or channels.parse_pace(value[1]) is None):
            raise ValueError("a malformed member: %s" % json.dumps([name, value])[:200])
        members[name] = [int(now - value[0]), value[1]]
    path = seen_path(section)
    old = _load_seen_doc(path)
    if old is not None:
        same_keys = ({k: v[1] for k, v in old.items()}
                     == {k: v[1] for k, v in members.items()})
        moved = any(abs(old[k][0] - members[k][0]) > SEEN_SLACK for k in members) \
            if same_keys else True
        if not moved:
            return False
        if same_keys:
            try:
                if now - os.stat(path).st_mtime < SEEN_SAVE_EVERY:
                    return False
            except OSError:
                pass
    data = json.dumps({"members": members}, sort_keys=True).encode("utf-8")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
    try:
        with open(fd, "wb") as f:
            f.write(data)
        entries._replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return True


def seen_due(section, now):
    """Whether save_seen would write the copy now, given times that moved: none kept, or its
    file is SEEN_SAVE_EVERY old by its mtime."""
    try:
        return now - os.stat(seen_path(section)).st_mtime >= SEEN_SAVE_EVERY
    except OSError:
        return True


def _load_seen_doc(path):
    """{name: [time, pace]} of the cache file path, or None: none, or one that can't be read
    or has another shape."""
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read(1 << 20).decode("utf-8"))
    except (OSError, ValueError):
        return None
    members = doc.get("members") if isinstance(doc, dict) else None
    if not isinstance(members, dict):
        return None
    for name, value in members.items():
        if (pathrules.writer_problem(name) is not None or not isinstance(value, list)
                or len(value) != 2 or not isinstance(value[0], int)
                or isinstance(value[0], bool) or channels.parse_pace(value[1]) is None):
            return None
    return members


def load_seen(section):
    """{name: (time on this box, --every seconds)} of what save_seen kept; {} for none, or
    one that can't be read (it only adds to whoami and read --json, so nothing is refused for
    it)."""
    members = _load_seen_doc(seen_path(section)) or {}
    return {name: (t, channels.parse_pace(pace)[1]) for name, (t, pace) in members.items()}


# --- the version note read showed last ---

def version_note_path(section):
    """Where read keeps the members' versions note it printed last for a membership (its
    channel section): {"note": the text}. Gone while the versions agree."""
    return os.path.join(platform.state_dir(), section + ".version-note.json")


def version_note_new(section, note):
    """Whether read prints the version note: once per text, so it shows the first time and
    again when a member's version changes, not on every read. note None (the versions agree)
    removes the kept one, so a later difference shows again. A file that can't be read is no
    text kept, and one that can't be written keeps the note printing each time: it only adds
    a note, so nothing is refused for it."""
    path = version_note_path(section)
    if note is None:
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read(1 << 20).decode("utf-8"))
    except (OSError, ValueError):
        doc = None
    if isinstance(doc, dict) and doc.get("note") == note:
        return False
    data = json.dumps({"note": note}, ensure_ascii=False).encode("utf-8")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
        try:
            with open(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass
    return True
