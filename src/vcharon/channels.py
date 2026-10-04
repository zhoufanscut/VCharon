"""The channel root: list, claim, release and remove channel and member
folders, directly below the root; on both ends (the helper's channel.* calls, and vcharon's
--local in-process)."""

from __future__ import annotations

import os
import re
import stat
import time

from . import charter, fsops, pathrules, platform
from .entries import CHANNEL_FILE, MEMBER_FILE, header_of
from .proto import VCharonError

# The fixed root; VCHARON_CHANNELS_ROOT replaces it (tests only, through the fake ssh). Never a
# flag: every member of a channel must reach the same folder.
DEFAULT_ROOT = "~/.local/state/vcharon/channels"
ROOT_ENV = "VCHARON_CHANNELS_ROOT"
# so the section name <channel>.<member> (24 + 1 + 32) fits config.MAILBOX_NAME_MAX
CHANNEL_MAX = 24
# A closed channel is renamed to this, then deleted. A channel's name can't start with ".", so
# nothing takes a leftover for a channel.
CLOSED_PREFIX = ".vcharon-closed-"
# a member's folder that only these files are in may be released (a failed join or create)
RELEASABLE = frozenset([MEMBER_FILE, CHANNEL_FILE])

_DIR = fsops.PathDir if fsops.WINDOWS else fsops.FdDir
# a MEMBER.md bigger than this isn't read at all: vcharon's is a few hundred bytes
MEMBER_READ_MAX = 64 << 10
# the fields of MEMBER.md's #1 that vcharon list shows, and their shape: a value of another
# shape (a hand edit, an older or newer writer) is shown as missing, never printed
LIST_FIELDS = ("box", "os", "agent", "project")
_FIELD = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,31}\Z")
CLAIMER_LEN = 16
_CLAIMER = re.compile(r"\A[0-9a-f]{%d}\Z" % CLAIMER_LEN)
# MEMBER.md's vcharon: line, the version the member last joined or watched with: 0.1.0,
# 0.2.0rc1 (a version string, not a name, so dots are fine)
_VERSION = re.compile(r"\A[0-9][0-9a-z.+-]{0,31}\Z")


def _no_tick():
    pass


def member_fields(data, name):
    """MEMBER.md's bytes: {"box", "os", "agent", "project", "claimer", "vcharon"} from the
    header of its entry name#1, each None when missing or not in the shape vcharon writes.
    Other header lines are left alone: a newer vcharon may write more."""
    header = header_of(data.decode("utf-8", "replace"), name, 1)
    out = {}
    for key in LIST_FIELDS:
        value = header.get(key)
        out[key] = value if value is not None and _FIELD.match(value) else None
    for key, shape in (("claimer", _CLAIMER), ("vcharon", _VERSION)):
        value = header.get(key)
        out[key] = value if value is not None and shape.match(value) else None
    return out


def read_member(folder, name):
    """member_fields of the member folder's MEMBER.md (read as _member_file reads it); {}
    when there is none to read."""
    data = _member_file(folder)
    return member_fields(data, name) if data is not None else {}


def _read_head(reader):
    """reader's bytes, or None when there are more than MEMBER_READ_MAX (vcharon writes
    MEMBER.md far smaller: a bigger one isn't its); reader is closed."""
    with reader as f:
        data = b""
        while len(data) <= MEMBER_READ_MAX:
            got = f.read(MEMBER_READ_MAX + 1 - len(data))
            if not got:
                break
            data += got
    return data if len(data) <= MEMBER_READ_MAX else None


def _member_file(folder):
    """folder's MEMBER.md, or None: missing, not a regular file (never read through a link),
    bigger than MEMBER_READ_MAX, or unreadable."""
    path = os.path.join(folder, MEMBER_FILE)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return None
            # _read_head closes it
            reader = open(fd, "rb", buffering=0)  # noqa: SIM115
        except BaseException:
            os.close(fd)
            raise
        return _read_head(reader)
    except OSError:
        return None


def refused(text, hint):
    """A refused channel check: what's wrong, and its fix line."""
    return VCharonError("channel", text, hint)


def channel_problem(channel):
    """Why channel can't name a channel, or None: a writer's name, at most CHANNEL_MAX long."""
    if not isinstance(channel, str):
        return "a channel's name must be text"
    problem = pathrules.writer_problem(channel)
    if problem:
        return problem.replace("a writer's name", "a channel's name").replace(
            "32 characters", "%d characters" % CHANNEL_MAX)
    if len(channel) > CHANNEL_MAX:
        return "a channel's name is at most %d characters long" % CHANNEL_MAX
    return None


def root_text():
    """The root as the client writes it into a section's mailbox.remote."""
    return os.environ.get(ROOT_ENV) or DEFAULT_ROOT


def root_path():
    """The root on this machine, absolute. A relative value is relative to the home, as every
    remote path is (DESIGN, "Rules for the code"), never to the current directory."""
    path = os.path.expanduser(root_text())
    if not os.path.isabs(path):
        path = os.path.join(platform.home(), path)
    return os.path.normpath(path)


def _check_names(channel, name):
    # The client checked them; the server never trusts that (DESIGN, "Path rules").
    problem = channel_problem(channel)
    if problem:
        raise refused("%s: %s" % (pathrules.show(channel), problem), "pick another channel name")
    problem = pathrules.writer_problem(name) if isinstance(name, str) else "not text"
    if problem:
        raise refused("%s: %s" % (pathrules.show(name), problem), "pick another member name")


def _open_root(root):
    """A handle on the root, which may itself be a symlink (resolved once: DESIGN, "Path rules");
    None if it's missing."""
    resolved = fsops.resolve_root(root)
    try:
        return _DIR.open_root(resolved, list=True)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise fsops.error(e, root)


def _real_dir(handle, name, what):
    """The lstat of handle/name, which must be a real directory: never a symlink, which the
    walk would not follow anyway. None if it's missing."""
    st = handle.lstat(name)
    if st is None:
        return None
    kind = fsops.kind(st)
    if kind == fsops.LINK:
        raise refused("%s is a symlink, and vcharon never follows one" % what,
                      "remove it by hand")
    if kind != fsops.DIR:
        raise refused("%s isn't a folder" % what, "remove it by hand")
    return st


def _is_file(handle, name):
    st = handle.lstat(name)
    return st is not None and fsops.kind(st) == fsops.FILE


def _no_channel(channel):
    return VCharonError("not_found", "there is no channel %s" % channel,
                        "check the name against vcharon list's output")


def _close_all(*handles):
    for h in handles:
        if h is not None:
            h.close()


# --- channel.list ---

def list_channels(root, tick=_no_tick):
    """{"channels": [{"name", "members", "fields", "leaders", "strays", "newest", "format",
    "limits"}], "others": [{"name", "why"}]}, in name order. fields: for each member, {"box",
    "os", "agent", "project"} from its MEMBER.md (None each when it's missing); leaders: the
    members whose folder holds CHANNEL.md; newest: the newest file's mtime, or None; format
    and limits: from the one leader's CHANNEL.md (charter.parse), unchecked. A missing root
    holds no channel."""
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return {"channels": [], "others": []}
    except OSError as e:
        raise fsops.error(e, root)
    channels, others = [], []
    for name in names:
        tick()
        try:
            st = os.lstat(os.path.join(root, name))
        except FileNotFoundError:
            continue
        except OSError as e:
            others.append({"name": name, "why": "can't be read: %s" % (e.strerror or e)})
            continue
        if name.startswith(CLOSED_PREFIX):
            others.append({"name": name, "why": "a closed channel that wasn't deleted"})
            continue
        if fsops.kind(st) != fsops.DIR or channel_problem(name):
            others.append({"name": name, "why": "not a channel's folder"})
            continue
        try:
            channels.append(_channel_info(os.path.join(root, name), name, tick))
        except OSError as e:
            others.append({"name": name, "why": "can't be read: %s" % (e.strerror or e)})
    return {"channels": channels, "others": others}


def _channel_info(path, name, tick):
    members, leaders, strays = [], [], []
    fields = {}
    newest = None
    for entry in sorted(os.scandir(path), key=lambda e: e.name):
        if entry.is_dir(follow_symlinks=False) and pathrules.writer_problem(entry.name) is None:
            members.append(entry.name)
            found = read_member(entry.path, entry.name)
            fields[entry.name] = {k: found.get(k) for k in LIST_FIELDS}
            try:
                st = os.lstat(os.path.join(entry.path, CHANNEL_FILE))
            except OSError:
                st = None
            if st is not None and stat.S_ISREG(st.st_mode):
                leaders.append(entry.name)
        else:
            strays.append(entry.name)
    # os.walk never follows a symlinked folder
    for dirpath, dirnames, filenames in os.walk(path):
        tick()
        for f in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and (newest is None or st.st_mtime > newest):
                newest = st.st_mtime
    info = {"name": name, "members": members, "fields": fields, "leaders": leaders,
            "strays": strays, "newest": newest}
    info.update(_charter(path, leaders))
    return info


def _charter(path, leaders):
    """{"format", "limits"} from the one leader's CHANNEL.md (charter.parse); no format when
    the channel has no leader or several."""
    if len(leaders) != 1:
        return charter.parse("", "")
    return charter.read(os.path.join(path, leaders[0]), leaders[0])


def _leaders(path):
    """The members whose folder holds CHANNEL.md as a regular file, in name order."""
    found = []
    for entry in sorted(os.scandir(path), key=lambda e: e.name):
        if entry.is_dir(follow_symlinks=False) and pathrules.writer_problem(entry.name) is None:
            try:
                st = os.lstat(os.path.join(entry.path, CHANNEL_FILE))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                found.append(entry.name)
    return found


# --- channel.claim, channel.release ---

def claim(root, channel, name, create, tick=_no_tick):
    """mkdir <root>/<channel>/<name>, and with create <root>/<channel> first (and the root's
    missing parents): one mkdir each, so of two creators or two joiners with one name only one
    wins. Returns {"existed": the member folder was there already, "machine", "root": the
    root as a section's mailbox.remote spells it, "claimer": the claimer: of the existing
    folder's MEMBER.md, or None (a new folder, no MEMBER.md, none in it), "format" and
    "limits": the channel's, as list_channels gives them}. Never a host name: the reply's
    values go into the channel's files."""
    _check_names(channel, name)
    if create:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as e:
            raise fsops.error(e, root)
    top = _open_root(root)
    if top is None:
        raise _no_channel(channel)
    ch = None
    try:
        if create:
            try:
                top.mkdir(channel)
            except FileExistsError:
                raise refused("the channel %s already exists" % channel,
                              "join it, or pick another name")
            except OSError as e:
                raise fsops.error(e, os.path.join(root, channel))
        if _real_dir(top, channel, "the channel %s" % channel) is None:
            raise _no_channel(channel)
        try:
            ch = top.enter(channel)
        except OSError as e:
            raise fsops.error(e, os.path.join(root, channel))
        existed = False
        try:
            ch.mkdir(name)
        except FileExistsError:
            existed = True
        except BaseException as e:
            if create:
                # no empty channel left behind
                try:
                    top.rmdir(channel)
                except OSError:
                    pass
            if isinstance(e, OSError):
                raise fsops.error(e, os.path.join(root, channel, name))
            raise
        _real_dir(ch, name, "%s/%s" % (channel, name))
        claimer = _claimer_of(ch, name) if existed else None
    finally:
        _close_all(ch, top)
    reply = {"existed": existed, "machine": platform.machine_id(), "root": root_text(),
             "claimer": claimer}
    # a new channel's CHANNEL.md isn't written yet: create writes it after the claim
    path = os.path.join(root, channel)
    try:
        reply.update(_charter(path, _leaders(path)))
    except OSError:
        reply.update(charter.parse("", ""))
    return reply


def _claimer_of(ch, name):
    """The claimer: of ch/name/MEMBER.md's entry name#1, or None: no such file, a link, one
    that can't be read, or no claimer: line in vcharon's shape."""
    member = None
    try:
        member = ch.enter(name, owner_rule=False)
        data = _read_head(member.open_read(MEMBER_FILE))
    except (OSError, VCharonError):
        return None
    finally:
        _close_all(member)
    return member_fields(data, name)["claimer"] if data is not None else None


def release(root, channel, name, tick=_no_tick):
    """Removes <root>/<channel>/<name> while it's empty or holds only MEMBER.md and
    CHANNEL.md (what a failed join or create put there), then <root>/<channel> if that left it
    empty. Returns {"removed": bool}."""
    _check_names(channel, name)
    top = _open_root(root)
    if top is None:
        return {"removed": False}
    ch = member = None
    try:
        if _real_dir(top, channel, "the channel %s" % channel) is None:
            return {"removed": False}
        ch = top.enter(channel)
        if _real_dir(ch, name, "%s/%s" % (channel, name)) is None:
            return {"removed": False}
        member = ch.enter(name)
        names = member.listdir()
        if set(names) - RELEASABLE or not all(_is_file(member, n) for n in names):
            return {"removed": False}
        for n in names:
            member.unlink(n)
        member.close()
        member = None
        ch.rmdir(name)
        if not ch.listdir():
            ch.close()
            ch = None
            top.rmdir(channel)
    except OSError as e:
        raise fsops.error(e, os.path.join(root, channel, name))
    finally:
        _close_all(member, ch, top)
    return {"removed": True}


# --- channel.remove ---

def remove(root, channel, name, tick=_no_tick):
    """Closes a channel for its leader name: refused unless <channel>/<name>/CHANNEL.md exists
    and is the only CHANNEL.md, and every entry at the channel's top is a member's folder.
    Then one rename to .vcharon-closed-<channel>-<stamp>, so every member's next run sees the
    channel gone at once, and the no-link walk deletes that (DESIGN, "Create, join, leave,
    close"). Returns {"closed", "deleted"}."""
    _check_names(channel, name)
    top = _open_root(root)
    if top is None:
        raise _no_channel(channel)
    ch = None
    try:
        if _real_dir(top, channel, "the channel %s" % channel) is None:
            raise _no_channel(channel)
        ch = top.enter(channel)
        leaders, strays = [], []
        for n, st in sorted(ch.scan()):
            tick()
            if fsops.kind(st) != fsops.DIR or pathrules.writer_problem(n) is not None:
                strays.append(pathrules.show(n) + ("/" if fsops.kind(st) == fsops.DIR
                                                   else ""))
                continue
            member = ch.enter(n, owner_rule=False)
            try:
                if _is_file(member, CHANNEL_FILE):
                    leaders.append(n)
            finally:
                member.close()
        if name not in leaders:
            raise refused("%s/%s has no %s: only the channel's leader closes it"
                          % (channel, name, CHANNEL_FILE), "ask the user")
        if len(leaders) > 1:
            raise refused("%s has %d leaders (%s hold %s)"
                          % (channel, len(leaders), ", ".join(l + "/" for l in leaders),
                             CHANNEL_FILE), "ask the user")
        if strays:
            raise refused("%s holds %s at its top, not a member's folder"
                          % (channel, ", ".join(strays)), "ask the user, and remove it first")
        ch.close()
        ch = None
        closed = "%s%s-%s-%s" % (CLOSED_PREFIX, channel, time.strftime("%Y%m%d-%H%M%S"),
                                 os.urandom(3).hex())
        # On Windows a rename fails while another process holds the folder: measured
        # winerror 5 (access denied) on 2026-10-02 for an open file in it, a process whose
        # current folder is in it, and a Git Bash shell cd'd there. 32, a
        # sharing violation, is kept for open files; not seen. The retry (about 1 s) can't
        # outlast a real hold, so that refusal gets its own fix.
        try:
            fsops.retry_in_use(top.rename, channel, closed, codes=fsops.HELD_CODES)
        except OSError as e:
            if getattr(e, "winerror", None) not in fsops.HELD_CODES:
                raise
            raise _held(e, os.path.join(root, channel))
        try:
            deleted = fsops.remove_tree(top, closed, tick)
        except (OSError, VCharonError) as e:
            err = _not_gone(e, os.path.join(root, closed))
            raise VCharonError(err.code, "%s is closed, but deleting %s failed: %s"
                               % (channel, closed, err.message),
                               "delete it by hand; vcharon list's output notes it")
    except OSError as e:
        raise _not_gone(e, os.path.join(root, channel))
    finally:
        _close_all(ch, top)
    return {"closed": closed, "deleted": deleted}


def _held(e, path):
    """close's rename refused because something holds the folder (fsops.HELD_CODES):
    fsops.error's line, with a fix that names what can hold it. Only for this rename: elsewhere
    the generic fix stays."""
    err = fsops.error(e, path)
    return VCharonError(err.code, err.message,
                        "something has a file or its current folder inside %s (a shell, an "
                        "editor, Explorer, or a program a shell started): close it or leave "
                        "that folder, then run close again" % path)


def _not_gone(e, path):
    """An error inside remove as the client must read it: not_found means "no such channel"
    there, and close then removes the leader's records. So only _no_channel's checks give
    not_found; any other ENOENT (a member folder or file gone during the checks, a race with
    the rename) is vanished, which close reports and stops at."""
    err = fsops.error(e, path) if isinstance(e, OSError) else e
    if err.code == "not_found":
        return VCharonError("vanished", "%s went away while the channel was checked or closed"
                            % path, "check it in vcharon list's output, then close again")
    return err
