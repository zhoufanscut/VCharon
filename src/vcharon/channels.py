"""The channel root: list, claim, release and remove channel and member
folders, directly below the root; on both ends (the helper's channel.* calls, and vcharon's
--local in-process). Beside the root, seen/ holds each member's last-watched stamp."""

from __future__ import annotations

import errno
import os
import re
import stat
import tempfile
import time

from . import VERSION, charter, entries, fsops, pathrules, platform
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
# A member's folder removed as idle (prune) is renamed to this at the root's top, then deleted;
# a leftover is noted by list, as a closed channel's is
REMOVED_PREFIX = ".vcharon-removed-"
# The most files and folders (each folder itself included) one list --remove-idle run removes
# from a work channel, one folder or several together: every remote member's next down deletes
# their copies in one plan, which the dir sink refuses over its max_deletes default of 500
# (plugins/dir.py); 50 under it, so the plan's other deletes (a member's own, a moved file)
# still fit. A folder over it is never removed; a lobby's down has no such limit (config.py,
# MAILBOX_KIND)
PRUNE_MAX_ITEMS = 450
# list --remove-idle's --days: its default, and the most the command line takes
IDLE_DAYS = 14
IDLE_DAYS_MAX = 3650
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


def no_channel(channel):
    return VCharonError("not_found", "there is no channel %s" % channel,
                        "check the name against vcharon list's output")


def _close_all(*handles):
    for h in handles:
        if h is not None:
            h.close()


# --- channel.list ---

def list_channels(root, tick=_no_tick):
    """{"channels": [{"name", "members", "fields", "leaders", "strays", "newest", "bytes",
    "created", "made", "format", "kind", "limits"}], "others": [{"name", "why"}]}, in name
    order. fields: for each member, {"box", "os", "agent", "project"} from its MEMBER.md (None
    each when it's missing); leaders: the members whose folder holds CHANNEL.md; newest: the
    newest file's mtime, or None; bytes: the size of every file below the channel's folder;
    created: the one leader's CHANNEL.md created: time text (charter.created), or None; made:
    the channel folder's mtime, which list shows when created is None; format, kind (a lobby's
    only) and limits: from that CHANNEL.md (charter.parse), unchecked; idle: for each member,
    member_idle's seconds (None when unknown), and items its folder's count of files and
    folders. A missing root holds no channel."""
    now = server_now()
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
        if name.startswith(REMOVED_PREFIX):
            others.append({"name": name, "why": "a member's folder removed as idle that wasn't "
                           "deleted"})
            continue
        if fsops.kind(st) != fsops.DIR or channel_problem(name):
            others.append({"name": name, "why": "not a channel's folder"})
            continue
        try:
            channels.append(_channel_info(os.path.join(root, name), name, tick, st.st_mtime,
                                          now))
        except OSError as e:
            others.append({"name": name, "why": "can't be read: %s" % (e.strerror or e)})
    return {"channels": channels, "others": others}


def newest_file(path, tick=_no_tick, stages=False):
    """The newest mtime of a regular file below path, or None for none. os.walk never follows
    a symlinked folder. stages: also the files of vcharon's stage dirs, half-written files of
    a sync in progress (list counts them: they are activity in the channel)."""
    return _files_below(path, tick, stages)[0]


def _files_below(path, tick, stages):
    """(newest_file's mtime, the total size) of the regular files below path, in one walk; a
    symlink is neither followed nor counted."""
    newest, total = None, 0
    for dirpath, dirnames, filenames in os.walk(path):
        tick()
        if not stages:
            dirnames[:] = [d for d in dirnames
                           if not d.casefold().startswith(pathrules.STAGE_PREFIX)]
        for f in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
                if newest is None or st.st_mtime > newest:
                    newest = st.st_mtime
    return newest, total


def _channel_info(path, name, tick, made, now):
    members, leaders, strays = [], [], []
    fields, idle, items = {}, {}, {}
    for entry in sorted(os.scandir(path), key=lambda e: e.name):
        if entry.is_dir(follow_symlinks=False) and pathrules.writer_problem(entry.name) is None:
            members.append(entry.name)
            found = read_member(entry.path, entry.name)
            # the version too: a leader's offer of the lead is refused to a member whose
            # vcharon has none (vcharon list shows LIST_FIELDS only)
            fields[entry.name] = {k: found.get(k) for k in LIST_FIELDS + ("vcharon",)}
            try:
                st = os.lstat(os.path.join(entry.path, CHANNEL_FILE))
            except OSError:
                st = None
            if st is not None and stat.S_ISREG(st.st_mode):
                leaders.append(entry.name)
            idle[entry.name], items[entry.name] = member_idle(
                os.path.dirname(path), name, entry.name, now, tick)
        else:
            strays.append(entry.name)
    newest, total = _files_below(path, tick, True)
    chain = charter.chain(path)
    info = {"name": name, "members": members, "fields": fields, "leaders": leaders,
            "strays": strays, "newest": newest, "bytes": total, "created": None, "made": made,
            "idle": idle, "items": items,
            "leader": chain.leader, "term": chain.term if chain.charters else None,
            "tied": chain.tied}
    if chain.leader is not None:
        text = charter.read_text(os.path.join(path, chain.leader))
        info.update(charter.parse(text, chain.leader))
        info["created"] = charter.created(text, chain.leader)
    else:
        info.update(charter.parse("", ""))
    return info


def _changed(st):
    """When the entry of st last changed, by this machine's clock: its ctime, which the kernel
    sets (a sync's rename or utime moves it, while a file's mtime is copied from the writer's
    machine). On Windows st_ctime is the creation time, so its mtime: a Windows root holds
    local members only, whose writes are this machine's."""
    return st.st_mtime if fsops.WINDOWS else st.st_ctime


def _stamp_time(root, channel, name):
    """The mtime of name's last-watched stamp in seen/<channel>/ when it holds a pace (the
    watcher's own clock on this machine), else None. Never through a link."""
    top = seen_root(root)
    folder = os.path.join(top, channel)
    if not _is_real_dir(top) or not _is_real_dir(folder):
        return None
    path = os.path.join(folder, name)
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or parse_pace(_read_pace(path)) is None:
        return None
    return st.st_mtime


def server_now():
    """This machine's time, as idle times count it; VCHARON_TEST_CLOCK_SHIFT (seconds, tests
    only) moves it, as it moves the hello's clock (helper.hello): a folder's change times can't
    be set back."""
    return time.time() + float(os.environ.get("VCHARON_TEST_CLOCK_SHIFT") or 0)


def member_idle(root, channel, name, now, tick=_no_tick):
    """(how many whole seconds <root>/<channel>/<name> has been idle at now, how many files and
    folders it holds, itself included). Idle: since the newest of its last-watched stamp
    (_stamp_time) and the change time (_changed) of the folder and of every entry below it,
    stage dirs too, by an lstat walk that never follows a link. (None, 0) when the folder
    isn't a real folder. Every time is this machine's clock (DESIGN, "Clocks").
    last_active, lead's gone check, reads less (folders' mtimes, any stamp file), and is kept:
    it only asks whether a leader wrote in the last 30 min, where a folder's rename moves its
    mtime. A removal deletes a folder, so this takes the newest trace of any kind, and no
    stamp that isn't a watcher's."""
    top = os.path.join(root, channel, name)
    try:
        st = os.lstat(top)
    except OSError:
        return None, 0
    if fsops.kind(st) != fsops.DIR:
        return None, 0
    newest, count = _changed(st), 1
    for dirpath, dirnames, filenames in os.walk(top):
        tick()
        for n in dirnames + filenames:
            try:
                one = os.lstat(os.path.join(dirpath, n))
            except OSError:
                continue
            count += 1
            newest = max(newest, _changed(one))
    seen = _stamp_time(root, channel, name)
    if seen is not None:
        newest = max(newest, seen)
    return max(0, int(now - newest)), count


def _charter(path):
    """{"format", "limits"}, and "kind" for a lobby, from the leader's CHANNEL.md
    (charter.parse; the leader by the charters' terms); no format when the channel has no
    leader, or two at the top term."""
    leader = charter.chain(path).leader
    if leader is None:
        return charter.parse("", "")
    return charter.read(os.path.join(path, leader), leader)


# --- channel.claim, channel.release ---

def claim(root, channel, name, create, tick=_no_tick, lobby=False, claimer=None):
    """mkdir <root>/<channel>/<name>, and with create <root>/<channel> first (and the root's
    missing parents): one mkdir each, so of two creators or two joiners with one name only one
    wins. With create and lobby (a lobby's first join), then <name>/CHANNEL.md with the
    lobby's header and limits, in this one call: a CHANNEL.md sent only by the founder's first
    sync would leave the lobby without one until then, and every other join would be refused
    (DESIGN, "The lobby"); a write that fails removes <name>/ and then <channel>/. Returns
    {"existed": the member folder was there already, "machine", "root": the root as a
    section's mailbox.remote spells it, "claimer": the claimer: of the existing folder's
    MEMBER.md, or None (a new folder, no MEMBER.md, none in it), "format", "limits" (and
    "kind" for a lobby): the channel's, as list_channels gives them}, and for a lobby made
    "charter": the CHANNEL.md's text, which a remote founder writes into its local tree as it
    is. claimer: the joining machine's; an existing folder whose MEMBER.md names it loses its
    last-watched stamp too, since the join holds its state dir's watcher lock, so that
    watcher doesn't run: a stamp from before a leave would otherwise read "here" after the
    rejoin. Another state dir on the same machine has the same claimer, and a watcher there
    stamps again at its next round. Never a host name: the reply's values go into the
    channel's files."""
    _check_names(channel, name)
    if create:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as e:
            raise fsops.error(e, root)
    top = _open_root(root)
    if top is None:
        raise no_channel(channel)
    ch = None
    try:
        if create:
            # markers left by an earlier channel of this name would skip its terms
            _sweep_lead_of(root, channel)
            try:
                top.mkdir(channel)
            except FileExistsError:
                raise refused("the channel %s already exists" % channel,
                              "join it, or pick another name")
            except OSError as e:
                raise fsops.error(e, os.path.join(root, channel))
        if _real_dir(top, channel, "the channel %s" % channel) is None:
            raise no_channel(channel)
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
        owner = _claimer_of(ch, name) if existed else None
    finally:
        _close_all(ch, top)
    made = create and lobby and _write_charter(root, channel, name)
    if not existed or (owner is not None and owner == claimer):
        # a stamp left by an earlier member of this name isn't this member's, and this
        # state dir's own is from a watcher the join's lock keeps stopped
        drop_seen(root, channel, name)
    sweep_seen(root)
    reply = {"existed": existed, "machine": platform.machine_id(), "root": root_text(),
             "claimer": owner}
    if made:
        reply["charter"] = made
    # a new work channel's CHANNEL.md isn't written yet: create writes it after the claim
    path = os.path.join(root, channel)
    try:
        reply.update(_charter(path))
    except OSError:
        reply.update(charter.parse("", ""))
    return reply


def _write_charter(root, channel, name):
    """A lobby's CHANNEL.md in the just-made <root>/<channel>/<name>/, as create writes a
    work channel's (entries.post); its text. On a failure, the file, the member's folder and
    the channel's folder go again, as a failed member mkdir removes the channel's."""
    own = os.path.join(root, channel, name)
    path = os.path.join(own, CHANNEL_FILE)
    title, to, header = charter.charter_entry(channel, name, VERSION, charter.LOBBY_LIMITS,
                                              time.time(), charter.LOBBY_KIND)
    try:
        entries.post(path, own, name, title, to, header=header, number=2)
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except BaseException as e:
        for undo in (lambda: os.remove(path), lambda: os.rmdir(own),
                     lambda: os.rmdir(os.path.join(root, channel))):
            try:
                undo()
            except OSError:
                pass
        if isinstance(e, OSError):
            raise fsops.error(e, path) from None
        raise


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


def release(root, channel, name, tick=_no_tick, keep_charter=False):
    """Removes <root>/<channel>/<name> while it's empty or holds only MEMBER.md and
    CHANNEL.md (what a failed join or create put there), then <root>/<channel> if that left it
    empty. Returns {"removed": bool}. keep_charter (a lobby's): a folder holding CHANNEL.md in
    a channel with anything else in it keeps CHANNEL.md, and so itself; only its MEMBER.md
    goes, and the reply adds "kept": True. Other members may have joined the lobby since the
    claim, and a lobby without CHANNEL.md is refused at every join."""
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
        if keep_charter and CHANNEL_FILE in names and set(ch.listdir()) != {name}:
            if MEMBER_FILE in names:
                member.unlink(MEMBER_FILE)
            return {"removed": False, "kept": True}
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
    # undoing a failed create removes the channel with no close
    sweep_seen(root)
    return {"removed": True}


# --- channel.remove ---

def remove(root, channel, name, tick=_no_tick, lobby=False):
    """Closes a channel for its leader name: refused unless <channel>/<name>/CHANNEL.md exists
    and holds the top term's charter alone (charter.chain: a former leader's CHANNEL.md stays),
    no takeover's marker is under way (it takes the next term's marker first), and every entry
    at the channel's top is a member's folder. lobby (close lobby --yes, the user's word): the
    channel must be a lobby by its CHANNEL.md, and name any member with a folder in it; a lobby
    has no terms, so no marker is taken.
    Then one rename to .vcharon-closed-<channel>-<stamp>, so every member's next run sees the
    channel gone at once, and the no-link walk deletes that (DESIGN, "Create, join, leave,
    close"). Returns {"closed", "deleted"}."""
    _check_names(channel, name)
    top = _open_root(root)
    if top is None:
        raise no_channel(channel)
    ch = None
    try:
        if _real_dir(top, channel, "the channel %s" % channel) is None:
            raise no_channel(channel)
        ch = top.enter(channel)
        leaders, strays, members = [], [], []
        for n, st in sorted(ch.scan()):
            tick()
            if fsops.kind(st) != fsops.DIR or pathrules.writer_problem(n) is not None:
                strays.append(pathrules.show(n) + ("/" if fsops.kind(st) == fsops.DIR
                                                   else ""))
                continue
            members.append(n)
            member = ch.enter(n, owner_rule=False)
            try:
                if _is_file(member, CHANNEL_FILE):
                    leaders.append(n)
            finally:
                member.close()
        chain = charter.chain(os.path.join(root, channel))
        if lobby:
            _check_lobby(root, channel, name, chain, members)
        elif name not in leaders:
            raise refused("%s/%s has no %s: only the channel's leader closes it"
                          % (channel, name, CHANNEL_FILE), "ask the user")
        else:
            # the leader by terms: a former leader's CHANNEL.md stays in its folder
            _check_leader(channel, name, chain)
        if strays:
            raise refused("%s holds %s at its top, not a member's folder"
                          % (channel, ", ".join(strays)), "ask the user, and remove it first")
        ch.close()
        ch = None
        # the next term's marker, as a takeover takes it: of a close and a takeover at once,
        # one mkdir wins
        marker = None
        if not lobby:
            now = time.time()
            term, mine = _next_term(root, channel, chain, name, now)
            marker = _marker_path(root, channel, term) if mine else _take_marker(
                root, channel, term, name, chain)
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
            _drop_marker(marker)
            if getattr(e, "winerror", None) not in fsops.HELD_CODES:
                raise
            raise _held(e, os.path.join(root, channel))
        except BaseException:
            _drop_marker(marker)
            raise
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
    sweep_seen(root)
    return {"closed": closed, "deleted": deleted}


def _check_lobby(root, channel, name, chain, members):
    """Refused unless channel is a lobby by its one CHANNEL.md and name has a folder in it."""
    kind = None
    if chain.leader is not None:
        kind = charter.read(os.path.join(root, channel, chain.leader), chain.leader).get("kind")
    if kind != charter.LOBBY_KIND:
        raise refused("%s isn't a lobby by its %s" % (channel, CHANNEL_FILE), "ask the user")
    if name not in members:
        raise refused("you have no folder in %s" % channel, "ask the user")


def _check_leader(channel, name, chain):
    """Refused unless name holds the top term's charter of channel (chain): a former leader's
    CHANNEL.md stays, so holding one isn't leading."""
    if chain.leader == name:
        return
    if chain.leader is None:
        raise refused(charter.tied_text(channel, chain.tied, chain.term), "ask the user")
    raise refused("%s leads %s since term %d; %s led it before: only the leader closes it"
                  % (chain.leader, channel, chain.term, name), "ask the user")


def _held(e, path, again="run close again"):
    """close's (or prune's) rename refused because something holds the folder
    (fsops.HELD_CODES): fsops.error's line, with a fix that names what can hold it, then again.
    Only for these renames: elsewhere the generic fix stays."""
    err = fsops.error(e, path)
    return VCharonError(err.code, err.message,
                        platform.prose_hint("something has a file or its current folder inside "
                                            "%s (a shell, an editor, Explorer, or a program a "
                                            "shell started): close it or leave that folder, "
                                            "then " + again, path))


def _not_gone(e, path):
    """An error inside remove as the client must read it: not_found means "no such channel"
    there, and close then removes the leader's records. So only no_channel's checks give
    not_found; any other ENOENT (a member folder or file gone during the checks, a race with
    the rename) is vanished, which close reports and stops at."""
    err = fsops.error(e, path) if isinstance(e, OSError) else e
    if err.code == "not_found":
        return VCharonError("vanished", "%s went away while the channel was checked or closed"
                            % path, "check it in vcharon list's output, then close again")
    return err


# --- channel.prune: a member's folder removed as idle (list --remove-idle --yes) ---

# why prune kept a folder: the keys of its reply's "kept" (the client words them)
KEPT_GONE = "gone"
KEPT_NOT_MEMBER = "not_member"
KEPT_CHARTER = "charter"
KEPT_ACTIVE = "active"
KEPT_ITEMS = "items"
KEPT_BUDGET = "budget"


def prune(root, channel, name, days, budget=PRUNE_MAX_ITEMS, tick=_no_tick, now=None):
    """Removes <root>/<channel>/<name>, the folder of a member idle over days (member_idle, by
    this machine's clock at the call: a member that came back since the listing is kept).
    Kept, never removed: a folder that isn't a real folder with a member's name (a link, a
    file), one holding CHANNEL.md (the leader's, a former leader's after a handoff, a lobby
    founder's), and in a work channel one holding more than PRUNE_MAX_ITEMS, or more than
    budget: what this run's removals in the channel left of PRUNE_MAX_ITEMS, since each remote
    member's next down deletes every removed folder's copy in one plan. The join records
    are on the members' machines: the client keeps its own before the call. Then one rename to
    <root>/.vcharon-removed-<channel>-<name>-<stamp>, so every member's next run sees the folder
    gone at once (a channel's name can't start with "."), the no-link walk deletes that, and the
    member's last-watched stamp goes. Returns {"kept": None or a KEPT_* key, "idle": the
    seconds, "items": how many files and folders the folder held, itself included (what a
    member's down deletes of it), "removed": the renamed folder's name or None, "deleted": how
    many files and folders went}. A failed delete leaves the renamed folder, which list notes."""
    _check_names(channel, name)
    if (not isinstance(days, int) or isinstance(days, bool)
            or not 1 <= days <= IDLE_DAYS_MAX):
        raise VCharonError("protocol", "a malformed prune call: %s days"
                           % pathrules.show(str(days)))
    if (not isinstance(budget, int) or isinstance(budget, bool)
            or not 0 <= budget <= PRUNE_MAX_ITEMS):
        raise VCharonError("protocol", "a malformed prune call: a budget of %s"
                           % pathrules.show(str(budget)))
    t = server_now() if now is None else now
    reply = {"kept": None, "idle": None, "items": None, "removed": None, "deleted": 0}
    path = os.path.join(root, channel)
    if not _is_real_dir(path):
        raise no_channel(channel)
    try:
        st = os.lstat(os.path.join(path, name))
    except FileNotFoundError:
        return dict(reply, kept=KEPT_GONE)
    except OSError as e:
        raise fsops.error(e, os.path.join(path, name)) from None
    if fsops.kind(st) != fsops.DIR:
        return dict(reply, kept=KEPT_NOT_MEMBER)
    if os.path.lexists(os.path.join(path, name, CHANNEL_FILE)):
        return dict(reply, kept=KEPT_CHARTER)
    idle, items = member_idle(root, channel, name, t, tick)
    reply.update(idle=idle, items=items)
    if idle is None or idle <= days * 86400:
        return dict(reply, kept=KEPT_ACTIVE)
    if _charter(path).get("kind") != charter.LOBBY_KIND:
        if items > PRUNE_MAX_ITEMS:
            return dict(reply, kept=KEPT_ITEMS)
        if items > budget:
            return dict(reply, kept=KEPT_BUDGET)
    removed = "%s%s-%s-%s-%s" % (REMOVED_PREFIX, channel, name, time.strftime("%Y%m%d-%H%M%S"),
                                 os.urandom(3).hex())
    top = _open_root(root)
    if top is None:
        raise no_channel(channel)
    ch = None
    try:
        ch = top.enter(channel)
        # as close's rename: on Windows something holding a file or the current folder
        # inside it refuses the rename for a moment, or for as long as it holds it
        try:
            top.move_in(ch, name, removed)
        except OSError as e:
            if getattr(e, "winerror", None) in fsops.HELD_CODES:
                raise _held(e, os.path.join(path, name), "run the removal again") from None
            raise
        ch.close()
        ch = None
        drop_seen(root, channel, name)
        try:
            reply["deleted"] = fsops.remove_tree(top, removed, tick)
        except (OSError, VCharonError) as e:
            err = fsops.error(e, os.path.join(root, removed)) if isinstance(e, OSError) else e
            raise VCharonError(err.code, "%s/%s is removed, but deleting %s failed: %s"
                               % (channel, name, removed, err.message),
                               "delete it by hand; vcharon list's output notes it") from None
    except OSError as e:
        raise fsops.error(e, os.path.join(path, name)) from None
    finally:
        _close_all(ch, top)
        sweep_seen(root)
    reply["removed"] = removed
    return reply


# --- the watchers' stamps: seen/<channel>/<member> beside the root ---

# The folder beside the channel root that holds, per channel, each member's last-watched stamp:
# a file named after the member, holding the watcher's pace (PACE), its mtime the time of the
# watcher's last pull. Beside the root, never in it: a name at the root's top would show in
# list, and one in a channel would be synced, logged and refused by close (DESIGN, "The watcher
# in a channel").
SEEN = "seen"
# A stamp's time is written at most this often, so a watcher every 2 s costs one metadata write
# every 30 s at the channel's machine; readers allow for it.
SEEN_EVERY = 30
# the stamp's text: the watcher's mode and its --every (stream, run: a remote member's watcher,
# streaming or with --no-stream; local: a local member's)
_PACE = re.compile(r"\A(stream|run|local) ([1-9][0-9]{0,4})\Z")
PACE_MAX = 86400
# a stamp file holds a few bytes; one bigger isn't vcharon's
_SEEN_READ_MAX = 64


def parse_pace(text):
    """(mode, seconds) of a stamp's pace text, or None for one not in PACE's shape."""
    m = _PACE.match(text) if isinstance(text, str) else None
    if m is None or int(m.group(2)) > PACE_MAX:
        return None
    return m.group(1), int(m.group(2))


def seen_root(root):
    """The stamps' folder of the channel root root: seen beside it. Callers pass
    root_path(), so the helper and a local member find the same folder."""
    return os.path.join(os.path.dirname(root), SEEN)


def _is_real_dir(path):
    try:
        return fsops.kind(os.lstat(path)) == fsops.DIR
    except OSError:
        return False


def _mkdir(path):
    """mkdir path; whether it made it (False: there already)."""
    try:
        os.mkdir(path)
    except FileExistsError:
        return False
    return True


def _read_pace(path):
    """The pace text of the stamp file path, or None: never through a link, at most
    _SEEN_READ_MAX bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        data = os.read(fd, _SEEN_READ_MAX + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _SEEN_READ_MAX:
        return None
    return data.decode("ascii", "replace").strip()


def stamp_seen(root, channel, name, pace, now=None):
    """A watcher's pull of channel worked: seen/<channel>/<name> holds pace, its mtime now (this
    process's clock, set explicitly, so a file server's own clock never counts). Only while
    <root>/<channel> and the member's folder in it are real folders (a folder removed as idle
    gets no stamp from a watcher still running); seen/ and seen/<channel>/ are each one mkdir
    (never makedirs), and a seen/<channel>/ made for a channel that went away meanwhile (a
    close) is removed again. The time is written only when SEEN_EVERY has passed, or the pace
    changed. Returns whether it wrote. Raises OSError or VCharonError: every caller treats a
    stamp as best effort, which never fails a pull and prints nothing."""
    _check_names(channel, name)
    if parse_pace(pace) is None:
        raise VCharonError("protocol", "a pace that isn't one: %s" % pathrules.show(pace))
    t = time.time() if now is None else now
    channel_dir = os.path.join(root, channel)
    if not _is_real_dir(channel_dir) or not _is_real_dir(os.path.join(channel_dir, name)):
        return False
    top = seen_root(root)
    _mkdir(top)
    if not _is_real_dir(top):
        # never followed (no mkdir through it), never replaced; a file there is reported
        # (the remote client logs it), as the mkdir in it would have been
        # fsops.kind, as _is_real_dir: a Windows junction is a link too, which islink misses
        try:
            if fsops.kind(os.lstat(top)) == fsops.LINK:
                return False
        except OSError:
            pass
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
    folder = os.path.join(top, channel)
    if _mkdir(folder) and not _is_real_dir(channel_dir):
        os.rmdir(folder)
        return False
    if not _is_real_dir(folder):
        return False
    path = os.path.join(folder, name)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        return False
    if st is None or _read_pace(path) != pace:
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")
        try:
            with open(fd, "wb") as f:
                f.write(pace.encode("ascii") + b"\n")
            os.utime(tmp, (t, t))
            if fsops.WINDOWS:
                # a reader holding the old file gives access denied for a moment
                fsops.retry_in_use(os.replace, tmp, path, codes=fsops.HELD_CODES)
            else:
                os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True
    # abs: a clock set back writes it again too
    if abs(t - st.st_mtime) < SEEN_EVERY:
        return False
    os.utime(path, (t, t))
    return True


def list_seen(root, channel, now=None):
    """{name: [age in whole seconds, pace]} of the stamps in seen/<channel>/ whose name has a
    member folder in <root>/<channel>: a stamp of a member gone, or of an earlier channel of
    that name, is left out. {} for none. Raises OSError when seen/<channel>/ exists but can't
    be listed."""
    t = time.time() if now is None else now
    top = seen_root(root)
    folder = os.path.join(top, channel)
    # a linked seen/ is never followed: lstat of folder alone would go through it
    if channel_problem(channel) or not _is_real_dir(top) or not _is_real_dir(folder):
        return {}
    out = {}
    for name in sorted(os.listdir(folder)):
        if (pathrules.writer_problem(name) is not None
                or not _is_real_dir(os.path.join(root, channel, name))):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        pace = _read_pace(path) if stat.S_ISREG(st.st_mode) else None
        if parse_pace(pace) is None:
            continue
        out[name] = [max(0, round(t - st.st_mtime)), pace]
    return out


def drop_seen(root, channel, name):
    """Removes seen/<channel>/<name>: a stamp left by an earlier member of that name, or by
    this member's watcher before its join (claim). Best effort: never raises."""
    if channel_problem(channel) or pathrules.writer_problem(name) is not None:
        return
    top = seen_root(root)
    folder = os.path.join(top, channel)
    # either level a link: removing through it would delete a file outside the store
    if not _is_real_dir(top) or not _is_real_dir(folder):
        return
    path = os.path.join(folder, name)
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            os.remove(path)
    except OSError:
        pass


def sweep_seen(root):
    """Removes each seen/<X>/ whose channel folder <root>/<X> is gone: after a close, the
    undo of a failed create, or a watcher's stamp that raced a close. Only what a stamp makes is
    removed: a folder whose name can be a channel's, holding only small regular files whose
    names can be a member's (and a stamp's temp files); anything else is left. Then lead/<X>/,
    the term markers, the same way (_sweep_lead). Best effort: never raises."""
    _sweep_seen(root)
    _sweep_lead(root)


def _sweep_seen(root):
    top = seen_root(root)
    if not _is_real_dir(top):
        # a link: a sweep through it would delete files outside the store
        return
    try:
        names = os.listdir(top)
    except OSError:
        return
    for x in names:
        if channel_problem(x):
            continue
        try:
            os.lstat(os.path.join(root, x))
            continue
        except FileNotFoundError:
            pass
        except OSError:
            # can't tell: left
            continue
        folder = os.path.join(top, x)
        try:
            if not _is_real_dir(folder):
                continue
            inside = os.listdir(folder)
            ok = True
            for n in inside:
                st = os.lstat(os.path.join(folder, n))
                stamp = (pathrules.writer_problem(n) is None
                         or (n.startswith(".") and n.endswith(".tmp")))
                if not stamp or not stat.S_ISREG(st.st_mode) or st.st_size > _SEEN_READ_MAX:
                    ok = False
                    break
            if not ok:
                continue
            for n in inside:
                os.remove(os.path.join(folder, n))
            os.rmdir(folder)
        except OSError:
            continue


# --- channel.lead: a member takes the lead (DESIGN, "Leader handoff") ---

# The folder beside the channel root that holds, per channel, one marker folder per term taken:
# lead/<channel>/<term>/, holding an empty file named after its taker. Its mkdir decides a race
# of two takers (or of a taker and a close): one wins. Beside the root, as seen/.
LEAD = "lead"
# a marker with no charter of its term yet is a takeover (or a close) under way for this long;
# an older one was left by a run that died, and is skipped (terms may skip a number)
LEAD_WAIT = 60
# a gone leader: no last-watched stamp and no change in its folder for this long, both by the
# server's own clock (file mtimes are copied from the writer's machine, so they don't count)
GONE_AFTER = 30 * 60
# the title of the leader's offer, and its header key naming the member offered the lead
HANDOFF_TITLE = "HANDOFF"
HANDOFF_KEY = "lead"
# how much of the old leader's STEPS.md the reply carries: the taker quotes it in its own
# STEPS.md, one entry file, so it is cut below the channel's entry limit
STEPS_MARGIN = 4000
# the most of the taker's CHANNEL.md the reply carries; vcharon's is a few kB
CHARTER_MAX = 1 << 20
# how often, and how far apart, a run that lost a marker's mkdir looks for the winner's name
MARKER_TRIES = 10
MARKER_PAUSE = 0.05
_TERM_NAME = re.compile(r"\A[1-9][0-9]{0,8}\Z")
OFFER_HINT = ("ask %s to offer it to you: the leader runs vcharon lead %s --to @%s ; if the "
              "leader is gone, ask your user")
GONE_HINT = ("ask your user; or have %s hand the lead over to you: the leader's offer reaches "
             "your watcher")


def lead_root(root):
    """The term markers' folder of the channel root root: lead beside it."""
    return os.path.join(os.path.dirname(root), LEAD)


def _marker_path(root, channel, term):
    return os.path.join(lead_root(root), channel, str(term))


def _markers(root, channel):
    """{term: (its marker's mtime, the names in it)} of lead/<channel>/; {} for none. Never
    through a link."""
    top = lead_root(root)
    folder = os.path.join(top, channel)
    if not _is_real_dir(top) or not _is_real_dir(folder):
        return {}
    out = {}
    for n in os.listdir(folder):
        if not _TERM_NAME.match(n):
            continue
        path = os.path.join(folder, n)
        try:
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):
                continue
            inside = sorted(x for x in os.listdir(path) if pathrules.writer_problem(x) is None)
        except OSError:
            continue
        out[int(n)] = (st.st_mtime, inside)
    return out


def _next_term(root, channel, chain, name, now):
    """(the term the next takeover or close of channel takes, whether its marker is name's
    own already: a run of name's that stopped between the marker and its charter). Refused
    while another's marker with no charter is younger than LEAD_WAIT: its run may still write
    one. An older marker, name's own too, was left by a run that died, or by an earlier
    channel of this name: skipped, with a new mkdir, since another taker may skip it too and
    only a mkdir decides between them."""
    marks = _markers(root, channel)
    top = chain.term or 0
    high = max([top] + list(marks))
    if high > top:
        mtime, inside = marks[high]
        young = abs(now - mtime) < LEAD_WAIT
        if young and inside == [name]:
            return high, True
        if young:
            raise refused("a takeover or close of %s is under way (by %s)"
                          % (channel, ", ".join(inside) or "a member"),
                          "wait a minute, then run this again")
    return high + 1, False


def _take_marker(root, channel, term, name, chain):
    """mkdir lead/<channel>/<term> (lead/ and lead/<channel>/ one mkdir each, never through a
    link), then the empty file name in it. Of two runs, one mkdir wins; the other is
    refused, naming the winner. Returns the marker's path."""
    top = lead_root(root)
    folder = os.path.join(top, channel)
    for path in (top, folder):
        _mkdir(path)
        if not _is_real_dir(path):
            raise refused("%s isn't a folder (a link, or a file)" % path, "remove it by hand")
    marker = os.path.join(folder, str(term))
    try:
        os.mkdir(marker)
    except FileExistsError:
        who = []
        # the winner names itself in it a moment after its mkdir
        for _ in range(MARKER_TRIES):
            try:
                who = sorted(x for x in os.listdir(marker)
                             if pathrules.writer_problem(x) is None)
            except OSError:
                who = []
            if who:
                break
            time.sleep(MARKER_PAUSE)
        if who and who[0] == chain.leader:
            raise refused("%s is closing %s" % (who[0], channel), "ask your user") from None
        raise refused("%s took the lead of %s first" % (who[0] if who else "another member",
                                                        channel),
                      "check the channel's leader: vcharon whoami %s" % channel) from None
    try:
        with open(os.path.join(marker, name), "xb"):
            pass
    except OSError as e:
        _drop_marker(marker)
        raise fsops.error(e, marker) from None
    return marker


def _drop_marker(marker):
    """Removes a marker folder and the files in it. Best effort: never raises."""
    if marker is None:
        return
    try:
        for n in os.listdir(marker):
            os.remove(os.path.join(marker, n))
        os.rmdir(marker)
    except OSError:
        pass


def _sweep_lead(root):
    """Removes each lead/<X>/ whose channel folder <root>/<X> is gone (_sweep_lead_of).
    Best effort."""
    top = lead_root(root)
    if not _is_real_dir(top):
        return
    try:
        names = os.listdir(top)
    except OSError:
        return
    for x in names:
        if not channel_problem(x):
            _sweep_lead_of(root, x)


def _sweep_lead_of(root, x):
    """Removes lead/<x>/ when the channel folder <root>/<x> is gone and it holds only marker
    folders (a term's number, holding empty files named after members). Best effort."""
    try:
        os.lstat(os.path.join(root, x))
        return
    except FileNotFoundError:
        pass
    except OSError:
        return
    folder = os.path.join(lead_root(root), x)
    try:
        if not _is_real_dir(folder):
            return
        markers = os.listdir(folder)
        ok = all(_TERM_NAME.match(m) and _is_real_dir(os.path.join(folder, m))
                 for m in markers)
        for m in markers if ok else ():
            for n in os.listdir(os.path.join(folder, m)):
                st = os.lstat(os.path.join(folder, m, n))
                if (pathrules.writer_problem(n) is not None or not stat.S_ISREG(st.st_mode)
                        or st.st_size):
                    ok = False
        if not ok:
            return
        for m in markers:
            _drop_marker(os.path.join(folder, m))
        os.rmdir(folder)
    except OSError:
        return


def _check_offer(path, leader, name, chain):
    """Refused unless the leader's latest HANDOFF entry, posted since it took its term, has
    the header lead: name."""
    top = chain.top()
    low = 0 if top is None or top.term == charter.FIRST_TERM else top.number
    folder = os.path.join(path, leader)
    latest = None
    for md in entries.md_files(folder):
        if os.path.basename(md) in (MEMBER_FILE, CHANNEL_FILE):
            continue
        try:
            found = entries.parse_file(md)
        except OSError:
            continue
        for e in found:
            if (e.name == leader and e.title.strip() == HANDOFF_TITLE
                    and (latest is None or e.number > latest.number)):
                latest = e
    hint = OFFER_HINT % (leader, os.path.basename(path), name)
    if latest is None or latest.number <= low:
        raise refused("%s hasn't offered you the lead of %s" % (leader, os.path.basename(path)),
                      hint)
    to = dict(reversed(latest.header)).get(HANDOFF_KEY)
    if to != name:
        raise refused("%s's latest offer of the lead of %s (%s) is to %s, not to you"
                      % (leader, os.path.basename(path), latest.id, pathrules.show(str(to))),
                      hint)


def last_active(root, channel, leader):
    """The newest of leader's last-watched stamp and the mtime of every folder in
    <root>/<channel>/<leader>/ (the folder itself, stage dirs too), all by this machine's
    clock: a sync or a post renames into a folder, which moves its mtime here, while a file's
    mtime is the writer's own. None when there is neither."""
    times = []
    top = seen_root(root)
    if _is_real_dir(top) and _is_real_dir(os.path.join(top, channel)):
        try:
            st = os.lstat(os.path.join(top, channel, leader))
            if stat.S_ISREG(st.st_mode):
                times.append(st.st_mtime)
        except OSError:
            pass
    for dirpath, _dirnames, _files in os.walk(os.path.join(root, channel, leader)):
        try:
            times.append(os.lstat(dirpath).st_mtime)
        except OSError:
            continue
    return max(times) if times else None


def _check_gone(root, channel, leader, now):
    """Refused unless leader has been inactive GONE_AFTER (last_active)."""
    newest = last_active(root, channel, leader)
    if newest is not None and now - newest < GONE_AFTER:
        raise refused("%s was active %d min ago (its watcher, or a change in its folder); a "
                      "gone leader is one inactive for %d min"
                      % (leader, max(0, int(now - newest)) // 60, GONE_AFTER // 60),
                      GONE_HINT % leader)


def _read_bytes(path, limit):
    """path's bytes, at most limit + 1 of them; None for a file that is missing, a link, or
    can't be read."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            data = b""
            while len(data) <= limit:
                got = os.read(fd, limit + 1 - len(data))
                if not got:
                    break
                data += got
            return data
        finally:
            os.close(fd)
    except OSError:
        return None


def _steps_of(path, leader, limits):
    """(the old leader's STEPS.md text, cut at a line end to fit the channel's entry limit
    less STEPS_MARGIN; whether it was cut). "" for none."""
    limit = max(0, (limits.get("max_entry_kb") or 0) * charter.KB - STEPS_MARGIN)
    data = _read_bytes(os.path.join(path, leader, "STEPS.md"), limit)
    if data is None:
        return "", False
    cut = len(data) > limit
    if cut:
        data = data[:limit]
        data = data[:data.rfind(b"\n") + 1]
    return data.decode("utf-8", "replace"), cut


def _lead_reply(path, name, top):
    """channel.lead's reply for name's charter top: {"term", "after", "from", "charter": its
    CHANNEL.md's whole text, "steps", "steps_cut"}."""
    data = _read_bytes(os.path.join(path, name, CHANNEL_FILE), CHARTER_MAX)
    if data is None or len(data) > CHARTER_MAX:
        raise refused("%s/%s can't be read" % (name, CHANNEL_FILE), "ask the user")
    before = top.after[0]
    info = charter.read(os.path.join(path, name), name)
    steps, cut = _steps_of(path, before, info.get("limits") or {})
    return {"term": top.term, "after": "%s#%d" % top.after, "from": before,
            "charter": data.decode("utf-8"), "steps": steps, "steps_cut": cut}


def lead(root, channel, name, number, mode, when, tick=_no_tick, now=None):
    """name takes the lead of channel: mode "offer", on the leader's latest HANDOFF naming it;
    "gone", when the leader has been inactive GONE_AFTER (_check_gone). The term is one more
    than the highest charter's or marker's (_next_term); its marker's mkdir decides a race.
    Then name's CHANNEL.md gets the charter, entry name#number (the client's next number,
    taken under its post lock) at the time text when (the client's clock), with term: and
    after: <leader>#<its highest number here>, through a stage file and one replace; a failed
    write removes the marker. Returns _lead_reply's. A run of name's that wrote the charter
    already gets the same reply: a re-run of the client finishes a half-done takeover."""
    _check_names(channel, name)
    if mode not in ("offer", "gone"):
        raise VCharonError("protocol", "a lead mode that isn't one: %s"
                           % pathrules.show(str(mode)))
    if (not isinstance(number, int) or isinstance(number, bool) or not 1 <= number < 10 ** 9
            or entries.parse_time(when) is None):
        raise VCharonError("protocol", "a malformed lead call: %s#%s at %s"
                           % (name, number, pathrules.show(str(when))))
    t = time.time() if now is None else now
    path = os.path.join(root, channel)
    if not _is_real_dir(path):
        raise no_channel(channel)
    own = os.path.join(path, name)
    if not _is_real_dir(own) or _member_file(own) is None:
        raise refused("you have no folder with a %s in %s" % (MEMBER_FILE, channel),
                      "join it first")
    if entries.has_left(own, name):
        raise refused("you left %s" % channel, "join it again first")
    chain = charter.chain(path)
    if chain.leader is None:
        if chain.tied:
            _check_leader(channel, name, chain)
        raise refused("%s has no leader (no member's folder holds %s)" % (channel, CHANNEL_FILE),
                      "ask your user")
    info = charter.read(os.path.join(path, chain.leader), chain.leader)
    if info.get("kind") == charter.LOBBY_KIND:
        raise refused("a lobby has no leader", "post to the members by name")
    if chain.leader == name:
        top = chain.top()
        if top is None or top.after is None:
            raise refused("you lead %s already" % channel, "nothing to take")
        return _lead_reply(path, name, top)
    leader = chain.leader
    if mode == "offer":
        _check_offer(path, leader, name, chain)
    else:
        _check_gone(root, channel, leader, t)
    term, mine = _next_term(root, channel, chain, name, t)
    have = entries.next_number(own, name)
    if number < have:
        raise refused("your folder at the server holds %s#%d already" % (name, have - 1),
                      "sync your folder, then run this again")
    target = os.path.join(own, CHANNEL_FILE)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    except OSError as e:
        raise fsops.error(e, target) from None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise VCharonError("unsafe_path", "%s isn't a regular file" % target,
                           "remove it by hand")
    marker = _marker_path(root, channel, term) if mine else _take_marker(
        root, channel, term, name, chain)
    after = (leader, entries.next_number(os.path.join(path, leader), leader) - 1)
    old = charter.header_of(charter.read_text(os.path.join(path, leader)), leader)
    try:
        title, to, header = charter.takeover_entry(channel, name, term, after, old)
        entries.append(target, entries.build(when, name, number, title, to, header=header))
    except BaseException as e:
        _drop_marker(marker)
        if isinstance(e, OSError):
            raise fsops.error(e, target) from None
        raise
    return _lead_reply(path, name, charter.Charter(name, number, term, after))
