"""ferry channel list, create, join, leave and close (DESIGN §14 M10): members' names, their
records and section files, and the steps of each command in the design's order; client, and a
server member's box (--local). The channel root's own calls are in channels.py."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import time

from . import channels, config, entries, fsops, pathrules, platform, plugin, ssh, state
from . import run as engine
from .lock import Lock
from .log import Log
from .proto import VCharonError

RECORD_VERSION = 1
RECORD_KEYS = ("version", "channel", "name", "leader", "ssh", "remote", "machine")
# the parts that rebuild the name, for the hints that tell a member to run ferry channel; a
# record written before the M10 re-review has neither, and is read as before
RECORD_OPTIONAL = ("project", "role")
# a member's name: <box>-<project>[-<role>], at most 10 + 1 + 14 + 1 + 6 = 32
PROJECT_MAX = 14
_ROLE = re.compile(r"\A[a-z0-9]{1,6}\Z")
_NOT_NAME = re.compile(r"[^a-z0-9_]+")
BOX_HINT = "set box in [ferry] of ferry.ini (the user picks it: mac, win, linux, laptop)"
RULES = "MAILBOX.md in the ferry folder (the ferry-mailbox skill)"
# A channel section's up never creates (M10): a missing root is a closed channel, or the own
# folder gone at the server. Never stage.ROOT_HINT's "create it": an agent following it would
# make the closed channel again by hand. ferry run and doctor (cli.channel_gone_hint) and
# mailbox_watch.py's --dir (M13) print it, with the channel and name_flags. Its start is a
# constant of its own: the watcher tells a gone channel by it (EXIT closed, M14b), and the
# leave command after it is printed as the box runs ferry (platform.runnable), not as written.
CHANNEL_GONE_PREFIX = "the channel is closed, or your folder in it is gone: "
CHANNEL_GONE_HINT = CHANNEL_GONE_PREFIX + "ferry channel leave %s %s"


# --- names ---

def project_of(cwd):
    """The name of the folder that holds .git (a directory, or a worktree's .git file),
    walking up from cwd; cwd's own name when there's none. In Python, never by running git."""
    folder = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(folder, ".git")) or os.path.isfile(
                os.path.join(folder, ".git")):
            return os.path.basename(folder)
        up = os.path.dirname(folder)
        if up == folder:
            return os.path.basename(os.path.abspath(cwd))
        folder = up


def clean_project(text):
    """Lowercased, every run of characters other than a-z, 0-9 and _ one "-", "-" stripped at
    both ends, cut to PROJECT_MAX, "-" stripped at the end again."""
    text = _NOT_NAME.sub("-", text.lower()).strip("-")
    return text[:PROJECT_MAX].rstrip("-")


def member_name(cfg, project=None, role=None, cwd=None):
    """<box>-<project>[-<role>]: ferry channel builds a member's name, nothing else does. An
    exception to "never rely on the current directory" (DESIGN §13), on purpose: the name says
    where the agent works."""
    return member_parts(cfg, project, role, cwd)[0]


def member_parts(cfg, project=None, role=None, cwd=None):
    """(the member's name, its cleaned project, its role or None)."""
    if not cfg.box:
        raise VCharonError("config", BOX_HINT, hint="fix %s" % cfg.path)
    if role is not None and not _ROLE.match(role):
        raise VCharonError("config", "--role %s: 1 to 6 characters, a-z and 0-9"
                           % pathrules.show(role), hint="pick another role")
    p = clean_project(project if project is not None else project_of(cwd or os.getcwd()))
    if not p:
        raise VCharonError("config", "the project's name comes out empty: give --project",
                           hint="for example: --project web")
    name = "%s-%s" % (cfg.box, p) + ("-%s" % role if role else "")
    problem = pathrules.writer_problem(name)
    if problem:
        raise VCharonError("config", "the member's name %s: %s" % (name, problem),
                           hint="give a shorter --project or --role")
    return name, p, role or None


def name_flags(channel, name, record=None):
    """The flags that rebuild name in a ferry channel command: --project P [--role R], from
    the record. A record without them (written before the M10 re-review), or none at all,
    gets a placeholder that says so, never flags that would build another name."""
    if record is None:
        try:
            record = read_record(channel, name)
        except VCharonError:
            record = None
    if record is not None and isinstance(record.get("project"), str):
        role = record.get("role")
        return "--project %s%s" % (record["project"], " --role %s" % role if role else "")
    return "<the --project and --role that make %s>" % name


def check_channel(channel):
    problem = channels.channel_problem(channel)
    if problem:
        raise VCharonError("config", "%s: %s" % (pathrules.show(channel), problem),
                           hint="pick another channel name")


# --- records ---

def records_dir():
    # a folder of its own, so a record can't share a name with a job's state file
    return os.path.join(platform.state_dir(), "channels")


def record_path(channel, name):
    return os.path.join(records_dir(), "%s.%s.json" % (channel, name))


def read_record(channel, name):
    """The member's record (a dict), or None. One that can't be read is an error: ferry never
    guesses which server a membership is on."""
    path = record_path(channel, name)
    hint = "check it; if the membership is gone, delete it"
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise VCharonError("config", "the record %s can't be read: %s" % (path, e), hint=hint)
    if (not isinstance(doc, dict)
            or sorted(k for k in doc if k not in RECORD_OPTIONAL) != sorted(RECORD_KEYS)
            or not isinstance(doc.get("project", ""), str)
            or not isinstance(doc.get("role"), (str, type(None)))
            or doc["version"] != RECORD_VERSION or doc["channel"] != channel
            or doc["name"] != name
            or not all(isinstance(doc[k], str) for k in ("leader", "remote", "machine"))
            or not isinstance(doc["ssh"], (str, type(None)))):
        raise VCharonError("config", "the record %s has another shape" % path, hint=hint)
    return doc


def _write_atomic(path, data, temp):
    """data to path through the temp file temp in the same folder, flushed, then os.replace:
    a crash leaves the old file or the new one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(temp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if fsops.WINDOWS:
            fsops.retry_in_use(os.replace, temp, path)
        else:
            os.replace(temp, path)
    except BaseException:
        _remove(temp)
        raise


def write_record(doc):
    path = record_path(doc["channel"], doc["name"])
    data = json.dumps({k: doc[k] for k in RECORD_KEYS + RECORD_OPTIONAL if k in doc},
                      ensure_ascii=False,
                      indent=1).encode("utf-8") + b"\n"
    fd, temp = tempfile.mkstemp(dir=_made(records_dir()), prefix=".", suffix=".tmp")
    os.close(fd)
    _write_atomic(path, data, temp)


def _made(folder):
    os.makedirs(folder, exist_ok=True)
    return folder


def _remove(path):
    """Removes a file; True if it was there."""
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    return True


# --- a remote member's section ---

def section_path(cfg, section):
    return os.path.join(config.channels_dir(cfg.path), section + ".ini")


def local_text(section):
    """A remote member's local tree, as its section's mailbox.local spells it: one tree per
    (channel, member), never shared."""
    return os.path.join(platform.joined_dir(), section)


def write_section(cfg, section, alias, name, leader, remote_text):
    text = ("[%s]\nssh            = %s\nmailbox.me     = %s\nmailbox.leader = %s\n"
            "mailbox.local  = %s\nmailbox.remote = %s\n"
            % (section, alias, name, leader, local_text(section), remote_text))
    path = section_path(cfg, section)
    # .<name>.ini.tmp: no reader takes it for a section (DESIGN §14 M10, Config)
    _write_atomic(path, text.encode("utf-8"),
                  os.path.join(_made(os.path.dirname(path)), ".%s.ini.tmp" % section))


# --- locks ---

def _watch_tool():
    """tools/mailbox_watch.py at the repo root (two folders above the package, src/vcharon),
    loaded by its path: join's lock check takes the lock's name from the watcher's own
    snapshot_path (one rule, never a copy)."""
    module = sys.modules.get("ferry_mailbox_watch")
    if module is None:
        package = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(os.path.dirname(package)), "tools", "mailbox_watch.py")
        spec = importlib.util.spec_from_file_location("ferry_mailbox_watch", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["ferry_mailbox_watch"] = module
    return module


def watcher_snapshot(record_or_none, section, name, channel_dir=None):
    """The watcher's snapshot path of this membership: the client mode's for a remote member,
    the server mode's for a server member, whose --dir main passes as
    os.path.abspath(os.path.expanduser(dir)) and snapshot_path keys by its realpath."""
    tool = _watch_tool()
    if channel_dir is None:
        return tool.snapshot_path(None, name, section)
    return tool.snapshot_path(os.path.abspath(os.path.expanduser(channel_dir)), name)


def held(path):
    """True if another process holds the lock file path; a missing file is no lock."""
    try:
        lk = Lock.open(path, create=False)
    except FileNotFoundError:
        return False
    except OSError as e:
        raise fsops.error(e, path)
    try:
        return not lk.try_acquire()
    finally:
        lk.release()


def _job_locks(section):
    return [os.path.join(platform.state_dir(), section + suffix + ".lock")
            for suffix in config.MAILBOX_JOBS]


def _check_locks(record, section, channel):
    """leave's and close's: refused while this box's watcher of the membership, or a run of
    one of its jobs, holds its lock."""
    if record["ssh"] is None:
        paths = [watcher_snapshot(record, section, record["name"], record["remote"]) + ".lock"]
    else:
        paths = [watcher_snapshot(record, section, record["name"]) + ".lock"]
        paths += _job_locks(section)
    for path in paths:
        if held(path):
            raise channels.refused("%s is held (a watcher, or a ferry run, of %s in %s): stop "
                                   "the watcher first" % (path, record["name"], channel))


# --- the server: over ssh, or this machine ---

class _Remote:
    """The channel root of the server at alias, over one connection."""

    def __init__(self, session, alias, hello, log):
        self.session = session
        self.ssh = alias
        self.machine = hello.get("machine")
        self.os = hello.get("os")
        self.log = log
        self.where = alias

    def list(self):
        return _listing(self.session.call("channel.list", {}))

    def claim(self, channel, name, create):
        return self.session.call("channel.claim", {"channel": channel, "name": name,
                                                   "create": create})

    def release(self, channel, name):
        return self.session.call("channel.release", {"channel": channel, "name": name})

    def remove(self, channel, name):
        return self.session.call("channel.remove", {"channel": channel, "name": name})


class _Local:
    """This machine's channel root, for a server member: the same functions, in-process."""

    ssh = None
    where = "this machine"

    def __init__(self, log):
        self.machine = platform.machine_id()
        self.root = channels.root_path()
        self.log = log

    def list(self):
        return _listing(channels.list_channels(self.root))

    def claim(self, channel, name, create):
        return channels.claim(self.root, channel, name, create)

    def release(self, channel, name):
        return channels.release(self.root, channel, name)

    def remove(self, channel, name):
        return channels.remove(self.root, channel, name)


def _listing(result):
    if (not isinstance(result, dict) or not isinstance(result.get("channels"), list)
            or not isinstance(result.get("others"), list)):
        raise VCharonError("protocol", "a malformed channel.list result")
    return result


def _find(listing, channel, where):
    """The channel's listing entry, or None when the root has no such name. A name that is
    listed but isn't usable as a channel (it can't be read, or it's a symlink or a file) is
    refused: never taken for a channel that is gone (leave would then remove the
    membership)."""
    for ch in listing["channels"]:
        if ch.get("name") == channel:
            return ch
    for other in listing["others"]:
        if other.get("name") == channel:
            raise channels.refused("%s on %s isn't usable as a channel (%s): ask the user"
                                   % (channel, where, other.get("why")))
    return None


def _server(args, cfg, log, stack):
    if args.local:
        return _Local(log)
    ssh.check_dest(args.ssh)
    session = stack.enter_context(ssh.Session(cfg.settings, args.ssh, log))
    return _Remote(session, args.ssh, session.open(), log)


def _need_machine(server):
    if server.ssh is not None:
        # a Mac or Windows root takes local members only (M11a)
        state.need_linux({"os": server.os}, server.where, "only a Linux server takes remote "
                         "members: check the alias")
    if not server.machine:
        hint = state.NO_MACHINE_HINT
        if server.ssh is None:
            # this machine: a Mac or Windows box has a hint of its own
            hint = platform.no_machine_hint() or hint
        raise VCharonError("state_mismatch", "%s has no machine id, so ferry can't tie a "
                           "channel's record to it" % server.where, hint)


# --- the command ---

def main(args, run):
    """ferry channel <action>: exit 0, or that of the error."""
    cfg = config.load(args.config)
    log = run.log = Log(os.path.join(platform.log_dir(), "ferry.log"), console=args.verbose)
    log.info("ferry channel %s%s; config %s" % (args.action, " " + args.channel
                                                if getattr(args, "channel", None) else "",
                                                cfg.path))
    for skip in cfg.skipped:
        log.info(skip.line)

    def say(line):
        print(line)
        sys.stdout.flush()
        log.info(line)

    if args.action == "list":
        return _list(args, cfg, log, say)
    check_channel(args.channel)
    name, project, role = member_parts(cfg, args.project, args.role)
    # stored in the record, so a hint can print the flags that rebuild the name
    args.ident = {"project": project, "role": role}
    if args.action == "create":
        return _create(args, cfg, name, log, say)
    if args.action == "join":
        return _join(args, cfg, name, log, say)
    return _leave(args, cfg, name, log, say, close=args.action == "close")


def _list(args, cfg, log, say):
    with contextlib.ExitStack() as stack:
        server = _server(args, cfg, log, stack)
        listing = server.list()
    say("ferry: channel list  (%s)" % server.where)
    if not listing["channels"] and not listing["others"]:
        say("  no channels")
    for ch in listing["channels"]:
        leaders = ch["leaders"]
        newest = ch.get("newest")
        say("  %s  leader %s  members %s  newest %s"
            % (ch["name"], leaders[0] if len(leaders) == 1 else "?",
               ", ".join(ch["members"]) or "none",
               entries.stamp(newest) if isinstance(newest, (int, float)) else "-"))
        if not leaders:
            say("    note: no member's folder holds %s: ask the user" % entries.CHANNEL_FILE)
        elif len(leaders) > 1:
            say("    note: %s all hold %s: ask the user"
                % (", ".join(l + "/" for l in leaders), entries.CHANNEL_FILE))
        for stray in ch.get("strays", []):
            say("    note: %s at its top isn't a member's folder" % pathrules.show(stray))
    for other in listing["others"]:
        say("  note: %s: %s" % (pathrules.show(other["name"]), other["why"]))
    return 0


def _another_server(record, server, channel):
    if record is not None and (record["machine"] != server.machine or record["ssh"] != server.ssh):
        raise channels.refused("you are in %s on another server: pass --role" % channel)


def _create(args, cfg, name, log, say):
    channel = args.channel
    section = "%s.%s" % (channel, name)
    record = read_record(channel, name)
    with contextlib.ExitStack() as stack:
        server = _server(args, cfg, log, stack)
        _need_machine(server)
        _another_server(record, server, channel)
        say("ferry: channel create %s  as %s on %s" % (channel, name, server.where))
        got = server.claim(channel, name, True)
        made = []
        try:
            own, remote_text = _write_member(cfg, server, channel, name, name, section, made,
                                             got, args.ident, create=True)
        except BaseException:
            # while the folder holds only those files: release removes nothing else
            _undo(made)
            try:
                server.release(channel, name)
            except Exception as e:
                log.warn("couldn't release %s/%s after the failure: %s" % (channel, name, e))
            raise
    say("  claimed %s/%s; you lead it" % (channel, name))
    if server.ssh is None:
        say("OK  created %s; your folder is %s" % (channel, own))
        return 0
    code = _run_section(args, section, full=True)
    if code != 0:
        say(platform.runnable("ferry: the run failed; %s is created: run ferry run %s --full "
                              "again" % (channel, section)))
        return code
    say("OK  created %s; your folder is %s" % (channel, own))
    return 0


def _write_member(cfg, server, channel, name, leader, section, made, got, ident, create=False,
                  rejoin=False):
    """The record, MEMBER.md (and CHANNEL.md for create), a remote member's section file:
    returns (the own folder, mailbox.remote's text or the channel folder). made collects what
    it wrote, for _undo."""
    remote = server.ssh is not None
    if remote:
        # the root as the helper spelled it: ~/… for the fixed one
        remote_text = posixpath.join(got["root"], channel)
    else:
        remote_text = os.path.join(server.root, channel)
    doc = {"version": RECORD_VERSION, "channel": channel, "name": name, "leader": leader,
           "ssh": server.ssh, "remote": remote_text, "machine": server.machine}
    doc.update(ident)
    if not rejoin or read_record(channel, name) is None:
        made.append(record_path(channel, name))
    write_record(doc)
    if remote:
        local = plugin.Ctx("local").resolve(local_text(section), "mailbox.local")
        own = os.path.join(local, name)
        # a rejoin's own folder comes from the pull (or after it): one made empty here and
        # left by a failed pull would let the next run's up empty the server's copy
        for folder in (local,) if rejoin else (local, own):
            if not os.path.isdir(folder):
                os.makedirs(folder)
                made.append(folder + os.sep)
    else:
        own = os.path.join(remote_text, name)
    if not rejoin:
        _member_md(own, channel, name, leader, made)
    if create:
        made.append(os.path.join(own, entries.CHANNEL_FILE))
        entries.post(os.path.join(own, entries.CHANNEL_FILE), own, name,
                     "channel %s created" % channel, [entries.ALL],
                     header=[("leader", name), ("server host", got.get("host") or "?"),
                             ("created", entries.stamp(time.time())), ("rules", RULES)],
                     number=2)
    if remote:
        if not os.path.exists(section_path(cfg, section)):
            made.append(section_path(cfg, section))
        write_section(cfg, section, server.ssh, name, leader, remote_text)
    return own, remote_text


def _member_md(own, channel, name, leader, made):
    path = os.path.join(own, entries.MEMBER_FILE)
    if os.path.exists(path):
        return
    made.append(path)
    entries.post(path, own, name, "member", ["@" + leader],
                 header=[("channel", channel), ("name", name), ("leader", leader)], number=1)


def _undo(made):
    """Removes what a failed create or join wrote, newest first; folders only when empty."""
    for path in reversed(made):
        try:
            if path.endswith(os.sep):
                os.rmdir(path)
            else:
                os.remove(path)
        except OSError:
            pass


def _join(args, cfg, name, log, say):
    channel = args.channel
    section = "%s.%s" % (channel, name)
    record = read_record(channel, name)
    with contextlib.ExitStack() as stack:
        server = _server(args, cfg, log, stack)
        _need_machine(server)
        say("ferry: channel join %s  as %s on %s" % (channel, name, server.where))
        # 1. the leader, before any claim: a refused join leaves nothing behind
        found = _find(server.list(), channel, server.where)
        if found is None:
            raise channels.refused("there is no channel %s on %s: check its name (ferry channel "
                                   "list)" % (channel, server.where))
        leaders = found["leaders"]
        if not leaders:
            raise channels.refused("%s has no leader (no member's folder holds %s): ask the "
                                   "user" % (channel, entries.CHANNEL_FILE))
        if len(leaders) > 1:
            raise channels.refused("%s has %d leaders (%s hold %s): ask the user"
                                   % (channel, len(leaders), ", ".join(l + "/" for l in leaders),
                                      entries.CHANNEL_FILE))
        leader = leaders[0]
        # 2. a live session already is <name>
        if server.ssh is None:
            lock = watcher_snapshot(None, section, name,
                                    os.path.join(server.root, channel)) + ".lock"
        else:
            lock = watcher_snapshot(None, section, name) + ".lock"
        if held(lock):
            raise channels.refused("a live session holds %s in %s: if that watcher is yours, "
                                   "keep using it; else pass --role" % (name, channel))
        _another_server(record, server, channel)
        # 3. one mkdir
        got = server.claim(channel, name, False)
        rejoin = got["existed"]
        if rejoin and record is None and not args.rejoin:
            raise channels.refused("the name %s is taken in %s: pass --role" % (name, channel))
        made = []
        try:
            # 4. the record first; before it is written, a failure releases the claim
            own, remote_text = _write_member(cfg, server, channel, name, leader, section, made,
                                             got, args.ident, rejoin=rejoin)
        except BaseException:
            if not rejoin:
                _undo(made)
                try:
                    server.release(channel, name)
                except Exception as e:
                    log.warn("couldn't release %s/%s after the failure: %s" % (channel, name, e))
            raise
        say("  %s %s/%s; the leader is %s" % ("took back" if rejoin else "claimed", channel,
                                              name, leader))
        # 5. a rejoin pulls back what this box lacks of its own folder first: up from a folder
        # missing files it sent would delete them at the server, and posts would restart at #1
        if rejoin and server.ssh is not None and needs_pull(section, own):
            _pull_own(server, remote_text, name, own, log, say)
        if rejoin:
            os.makedirs(own, exist_ok=True)
            _member_md(own, channel, name, leader, [])
    code = 0
    if server.ssh is not None:
        code = _run_section(args, section, full=True)
        if code == 130:
            # a Ctrl-C stops everything at once, as in ferry run
            return code
        if code != 0:
            say(platform.runnable("ferry: the run failed; you are in %s: run ferry run %s "
                                  "--full again" % (channel, section)))
    entries.post(os.path.join(own, "RESULTS.md"), own, name, "REJOIN" if rejoin else "JOIN",
                 ["@" + leader], body="%s %s %s." % (name, "rejoined" if rejoin else "joined",
                                                     channel))
    # 6. the member's first watcher start is a baseline and never prints these
    tree = os.path.dirname(own)
    _print_entries(tree, name, leader, channel, say)
    if code == 0:
        say("OK  in %s as %s; your folder is %s" % (channel, name, own))
    return code


def needs_pull(section, own):
    """Whether a rejoin pulls the server's copy of its own folder: decided from up's saved
    state, never from whether the folder looks empty (a stray .DS_Store would pass for a
    tree). Yes with no usable up state, or one that has sent nothing (the pull only adds
    what's missing). Else only when MEMBER.md, which up has sent, is missing here: the tree
    was lost (cli._own_folder's guard uses the same mark). With MEMBER.md here, a file up sent
    and that is gone was deleted on purpose, and the next run deletes it at the server."""
    st, why = state.read(section + ".up")
    sent = st.source.get("sent") if st is not None and isinstance(st.source, dict) else None
    if why is not None or not isinstance(sent, dict) or not sent:
        return True
    if entries.MEMBER_FILE not in sent:
        return False
    try:
        os.lstat(os.path.join(own, entries.MEMBER_FILE))
    except FileNotFoundError:
        return True
    except OSError:
        # can't tell: the pull only adds, so pulling is the safe side
        return True
    return False


LINK_HINT = "remove the link by hand (the pull never writes through one), then join again"


def _real_dir(path, make):
    """Checks that path is a real folder, never a symlink (or a Windows junction); makes it
    when missing and make. A link raises unsafe_path: the pull would write wherever it
    points."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if not make:
            raise
        os.mkdir(path)
        return
    if fsops.kind(st) == fsops.LINK:
        raise VCharonError("unsafe_path", "%s is a symlink: the pull into your own folder doesn't "
                           "go through it" % path, LINK_HINT)
    if fsops.kind(st) != fsops.DIR:
        raise VCharonError("kind_change", "%s isn't a folder, but the server's copy has one "
                           "there" % path, "rename it, then join again")


def _pull_own(server, remote_text, name, own, log, say):
    """A one-off pull of <remote>/<name>/ before the section's first run (down never plans the
    own folder, M9): into a temp folder next to the local tree, then each file this box lacks
    moves into the own folder. A file already here is kept: the pull only adds. A pull that
    fails before the move changes nothing here, so it never leaves an own folder that ferry
    made; one that fails while moving leaves the files already moved. Never through a link.
    The checks go by path, one lstat per part: a link swapped in between a check and the move
    isn't caught (the own folder is the member's own, not another user's)."""
    from . import cli
    path = posixpath.join(remote_text, name)
    local = os.path.dirname(own)
    temp = tempfile.mkdtemp(prefix=".ferry-pull-", dir=os.path.dirname(local))
    try:
        source = engine.Side("remote", "path", {"path": path, "keep_name": "yes",
                                                "symlinks": "error"})
        sink = engine.Side("local", "dir", {"path": temp})
        plugin.check_side(source.end, source.plugin, "source", source.options)
        plugin.check_side(sink.end, sink.plugin, "sink", sink.options)
        eng = engine.Engine(server.session, source, sink, log,
                            after_plan=cli.pull_guard(path))
        eng.run()
        added = kept = 0
        pulled = os.path.join(temp, name)
        # the local tree and the own folder themselves, then every folder down to each file:
        # each part is lstat'ed, so a symlink anywhere in the own folder is refused, never
        # written through (the M10 re-review measured a write outside the tree)
        _real_dir(local, make=False)
        target = own
        made_own = not os.path.lexists(own)
        try:
            _real_dir(own, make=True)
            for dirpath, dirnames, filenames in os.walk(pulled):
                dirnames.sort()
                rel = os.path.relpath(dirpath, pulled)
                target = own
                if rel != os.curdir:
                    for part in rel.split(os.sep):
                        target = os.path.join(target, part)
                        _real_dir(target, make=True)
                for f in sorted(filenames):
                    dst = os.path.join(target, f)
                    if os.path.lexists(dst):
                        kept += 1
                        continue
                    os.replace(os.path.join(dirpath, f), dst)
                    added += 1
        except BaseException as e:
            if made_own and not added:
                # nothing moved: no empty own folder left behind (folders made below it too)
                shutil.rmtree(own, ignore_errors=True)
            if isinstance(e, OSError):
                raise fsops.error(e, target)
            raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    say("  pulled your folder from the server: %d files added, %d already here"
        % (added, kept))


def _print_entries(tree, name, leader, channel, say):
    """The entries already in the other members' folders addressed to name, or to @all from
    the leader's."""
    shown = 0
    try:
        folders = sorted(os.listdir(tree))
    except OSError:
        folders = []
    for folder in folders:
        path = os.path.join(tree, folder)
        if (folder == name or pathrules.writer_problem(folder) is not None
                or os.path.islink(path) or not os.path.isdir(path)):
            continue
        for md in entries.md_files(path):
            try:
                found = entries.parse_file(md)
            except OSError:
                continue
            for e in found:
                if "@" + name not in e.to and not (entries.ALL in e.to and folder == leader):
                    continue
                if not shown:
                    say("entries for %s already in %s:" % (name, channel))
                shown += 1
                rel = os.path.relpath(md, tree).replace(os.sep, "/")
                say("")
                say("  %s" % rel)
                say("  ## %s" % e.heading)
                say("  to: %s" % " ".join(e.to))
                if e.re:
                    say("  re: %s" % e.re)
                for key, value in e.header:
                    say("  %s: %s" % (key, value) if key else "  %s" % value)
                if e.body:
                    say("")
                    for line in entries.split_lines(e.body):
                        say("  %s" % line)
    if shown:
        say("")
    else:
        say("no entries for %s in %s yet" % (name, channel))


def _run_section(args, section, full):
    """ferry run <section> [--full], as its own command would: its lines, its exit code."""
    from . import cli
    argv = ["run", section] + (["--full"] if full else [])
    if args.config:
        argv += ["--config", args.config]
    if args.verbose:
        argv.append("-v")
    return cli._main(argv, cli._Run())


def _leave(args, cfg, name, log, say, close):
    channel = args.channel
    section = "%s.%s" % (channel, name)
    record = read_record(channel, name)
    if record is None:
        raise channels.refused("you aren't in %s as %s (no record at %s)"
                               % (channel, name, record_path(channel, name)))
    leads = record["leader"] == name
    if close and not leads:
        raise channels.refused("only the leader closes %s, and that is %s"
                               % (channel, record["leader"]))
    if not close and leads:
        raise channels.refused("you lead %s: close it instead (ferry channel close %s %s)"
                               % (channel, channel, name_flags(channel, name, record)))
    # before anything on the server changes: a held lock can't leave a half-closed channel
    _check_locks(record, section, channel)
    job = cfg.named(section)
    settings = job[0].settings if job else cfg.settings
    with contextlib.ExitStack() as stack:
        if record["ssh"] is None:
            server = _Local(log)
        else:
            ssh.check_dest(record["ssh"])
            session = stack.enter_context(ssh.Session(settings, record["ssh"], log))
            server = _Remote(session, record["ssh"], session.open(), log)
        say("ferry: channel %s %s  as %s on %s" % ("close" if close else "leave", channel, name,
                                                   server.where))
        # the record's server, compared before any call: leave too, or another server's
        # missing channel would pass for a gone one
        _need_machine(server)
        if server.machine != record["machine"]:
            raise channels.refused("%s isn't the server %s is on (its machine id is %s, "
                                   "the record's %s): check the alias"
                                   % (server.where, channel, server.machine,
                                      record["machine"]))
        if close:
            try:
                done = server.remove(channel, name)
            except VCharonError as e:
                if e.code != "not_found":
                    raise
                say("  note    %s" % e.message)
            else:
                say("  closed %s: %d files and folders deleted" % (channel, done["deleted"]))
            gone = True
        else:
            gone = _find(server.list(), channel, server.where) is None
            if gone:
                # a local member's server is this machine (M11d)
                say("  note    %s is gone %s" % (channel, "on the server" if record["ssh"]
                                                 else "on this machine"))
    if not close and not gone:
        own = _own_of(cfg, record, section)
        # a leave whose run failed and is tried again posts no second LEAVE
        if own is not None and not _left_already(own, name):
            entries.post(os.path.join(own, "RESULTS.md"), own, name, "LEAVE",
                         ["@" + record["leader"]], body="%s left %s." % (name, channel))
        if record["ssh"] is not None:
            code = _run_section(args, section, full=False)
            if code != 0:
                say(platform.runnable(
                    "ferry: the run failed, so nothing was removed: run ferry channel leave %s %s "
                    "again once ferry run %s works"
                    % (channel, name_flags(channel, name, record), section)))
                return code
    _remove_membership(cfg, record, section, say)
    say("OK  %s %s" % ("closed" if close else "left", channel))
    return 0


def _left_already(own, name):
    """True if the own folder has a LEAVE of name after its last JOIN or REJOIN."""
    last = {"LEAVE": 0, "JOIN": 0}
    for path in entries.md_files(own) if os.path.isdir(own) else ():
        try:
            found = entries.parse_file(path)
        except OSError:
            continue
        for e in found:
            kind = "JOIN" if e.title == "REJOIN" else e.title
            if e.name == name and kind in last and e.number > last[kind]:
                last[kind] = e.number
    return last["LEAVE"] > last["JOIN"]


def _own_of(cfg, record, section):
    if record["ssh"] is None:
        return os.path.join(record["remote"], record["name"])
    jobs = cfg.named(section)
    if not jobs:
        return None
    return plugin.Ctx("local").resolve(jobs[0].mailbox.own_folder, "mailbox.local")


def _remove_membership(cfg, record, section, say):
    """leave's and close's removal on this box: a remote member's local tree (only when its
    mailbox.local is exactly the computed joined/ path), its jobs' state, log and lock files;
    the watcher snapshot and its lock (a server member's keyed by the channel folder, as the
    watcher's --dir), the own folder's post lock, the record, and a remote member's section
    file last. Locks only when no one holds them. A server member's folder stays on the server
    (leave) or went with the channel (close). One `removed <path>` line each."""
    name = record["name"]

    def drop(path, lock=False):
        if lock and held(path):
            return
        if _remove(path):
            say("  removed %s" % path)

    own = _own_of(cfg, record, section)
    # the post lock's key, from the own folder before it goes
    post_lock = entries.lock_path(own) if own is not None else None
    if record["ssh"] is not None:
        jobs = cfg.named(section)
        computed = local_text(section)
        if jobs and jobs[0].mailbox.local == computed:
            local = plugin.Ctx("local").resolve(computed, "mailbox.local")
            if os.path.lexists(local):
                _remove_tree(local)
                say("  removed %s" % local)
        elif jobs:
            say("  note    left %s in place: it isn't %s" % (jobs[0].mailbox.local, computed))
        for suffix in config.MAILBOX_JOBS:
            job = section + suffix
            for path in (state.path(job), os.path.join(platform.log_dir(), job + ".log"),
                         os.path.join(platform.log_dir(), job + ".log.1")):
                drop(path)
            drop(os.path.join(platform.state_dir(), job + ".lock"), lock=True)
        snapshot = watcher_snapshot(record, section, name)
    else:
        snapshot = watcher_snapshot(record, section, name, record["remote"])
    drop(snapshot)
    drop(snapshot + ".lock", lock=True)
    if post_lock is not None:
        drop(post_lock, lock=True)
    drop(record_path(record["channel"], name))
    if record["ssh"] is not None:
        drop(section_path(cfg, section))


def _remove_tree(path):
    """§10.1's walk: bottom-up, links removed, never entered."""
    parent, name = os.path.split(os.path.abspath(path))
    cls = fsops.PathDir if fsops.WINDOWS else fsops.FdDir
    try:
        handle = cls.open_root(fsops.resolve_root(parent))
        try:
            fsops.remove_tree(handle, name, lambda: None)
        finally:
            handle.close()
    except OSError as e:
        raise fsops.error(e, path)
