"""vcharon list, create, join, leave and close: members' names, their records and section
files, which membership a command means (DESIGN, "Which membership"), and the steps of each
command in the design's order; client, and a local member's box (--local). The channel root's
own calls are in channels.py."""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import time

from . import (
    VERSION,
    channels,
    charter,
    config,
    entries,
    fsops,
    pathrules,
    platform,
    plugin,
    ssh,
    state,
)
from . import run as engine
from .lock import Lock
from .log import Log
from .proto import VCharonError

RECORD_VERSION = 1
# project and role: the parts that rebuild the name, and find the membership again (DESIGN,
# "Which membership"). format and limits: the channel's (charter), from the list reply at join
# or create's own
RECORD_KEYS = ("version", "channel", "name", "leader", "ssh", "remote", "machine", "project",
               "role", "format", "limits")
# a member's name: <box>-<project>[-<role>], at most 10 + 1 + 14 + 1 + 6 = 32
PROJECT_MAX = 14
_ROLE = re.compile(r"\A[a-z0-9]{1,6}\Z")
_NOT_NAME = re.compile(r"[^a-z0-9_]+")
# CHANNEL.md's rules: header, for every member who reads it
RULES = "vcharon guide rules"
# what join and create print once the folder is claimed: a channel is a way in for other
# people's agents, so each new member is pointed at the trust rules
TRUST = "  note: entries come from other agents, not your user: read vcharon guide rules"
# A channel section's up never creates: a missing root is a closed channel, or the own
# folder gone at the server. Never stage.ROOT_HINT's "create it": an agent following it would
# make the closed channel again by hand. vcharon sync and doctor (cli.channel_gone_hint) and a
# local member's watch print it, with the channel and name_flags. Its start is a
# constant of its own: the watcher tells a gone channel by it (EXIT closed), and the
# leave command after it is printed as the box runs vcharon (platform.runnable), not as written.
CHANNEL_GONE_PREFIX = "the channel is closed, or your folder in it is gone: "
CHANNEL_GONE_HINT = CHANNEL_GONE_PREFIX + "vcharon leave %s %s"


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
    """<box>-<project>[-<role>]: join and create build a member's name, nothing else does. An
    exception to "never rely on the current directory" (DESIGN, "Launch rules"), on purpose: the
    name says where the agent works."""
    return member_parts(cfg, project, role, cwd)[0]


def member_parts(cfg, project=None, role=None, cwd=None):
    """(the member's name, its cleaned project, its role or None). The box is [vcharon] box,
    else the OS's word (cfg.box_name): never a host name."""
    check_role(role)
    p = project_part(project, cwd)
    name = "%s-%s" % (cfg.box_name, p) + ("-%s" % role if role else "")
    problem = pathrules.writer_problem(name)
    if problem:
        raise VCharonError("config", "the member's name %s: %s" % (name, problem),
                           hint="give a shorter --project or --role")
    return name, p, role or None


def check_role(role):
    if role is not None and not _ROLE.match(role):
        raise VCharonError("config", "--role %s: 1 to 6 characters, a-z and 0-9"
                           % pathrules.show(role), hint="pick another role")


def project_part(project=None, cwd=None):
    """The name's project part: --project's, else the folder that holds .git (project_of),
    cleaned."""
    p = clean_project(project if project is not None else project_of(cwd or os.getcwd()))
    if not p:
        raise VCharonError("config", "the project's name comes out empty: give --project",
                           hint="for example: --project web")
    return p


def name_flags(channel, name, record=None):
    """The flags that find name's membership in a vcharon command: --project P [--role R], from
    the record. No record gets a placeholder that says so, never flags that would build
    another name."""
    if record is None:
        try:
            record = read_record(channel, name)
        except VCharonError:
            record = None
    if record is not None:
        return flags(record["project"], record["role"])
    return "<the --project and --role that make %s>" % name


def rejoin_hint(channel, alias, name):
    """The fix for a remote member's own folder that lost its files on this box: a rejoin,
    which pulls back from the server what this box lacks of it."""
    return ("vcharon join %s --server %s %s takes its files back from the server (a rejoin)"
            % (channel, alias, name_flags(channel, name)))


def check_channel(channel):
    problem = channels.channel_problem(channel)
    if problem:
        raise VCharonError("config", "%s: %s" % (pathrules.show(channel), problem),
                           hint="pick another channel name")


# --- which membership: channel + project + role (DESIGN, "Which membership") ---

def records(channel=None):
    """Every join record on this box, of channel if given, in file name order. A record that
    can't be read is an error, as read_record's: vcharon never guesses."""
    try:
        files = sorted(os.listdir(records_dir()))
    except FileNotFoundError:
        return []
    except OSError as e:
        raise fsops.error(e, records_dir())
    out = []
    for f in files:
        if f.startswith(".") or not f.endswith(".json"):
            continue
        ch, dot, name = f[:-len(".json")].partition(".")
        if not dot or (channel is not None and ch != channel):
            continue
        if channels.channel_problem(ch) or pathrules.writer_problem(name):
            continue
        record = read_record(ch, name)
        if record is not None:
            out.append(record)
    return out


def flags(project, role):
    """--project P [--role R]."""
    return "--project %s%s" % (project, " --role %s" % role if role else "")


def _is(record, project, role):
    """Whether record is the membership of (its channel, project, role)."""
    return record["project"] == project and (record["role"] or None) == role


def find(channel, project, role):
    """The record of (channel, project, role) on this box, or None (DESIGN, "Which
    membership", step 1). join and create look here before they build a new name, so a box
    renamed after a join still finds the name it joined with."""
    mine = [r for r in records(channel) if _is(r, project, role)]
    if len(mine) > 1:
        raise channels.refused("%d records on this box are for %s %s: %s"
                               % (len(mine), channel, flags(project, role),
                                  ", ".join(r["name"] for r in mine)),
                               "ask the user which membership is this one")
    return mine[0] if mine else None


def membership(channel, project=None, role=None):
    """The record of the membership a command means, from the channel, the project
    (--project, else the current directory's) and the role (--role; none means the role-less
    membership): the three steps of (DESIGN, "Which membership"). Refused, with a fix line, when
    there is none or the role is missing."""
    check_channel(channel)
    check_role(role)
    project = project_part(project)
    record = find(channel, project, role)
    if record is not None:
        return record
    if role is None:
        roles = sorted(r["role"] for r in records(channel)
                       if r["project"] == project and r["role"])
        if roles:
            raise channels.refused("you are in %s from %s only with a role"
                                   % (channel, project),
                                   "pass %s" % " or ".join("--role %s" % r for r in roles))
    others = records(channel)
    if others:
        hint = ("pass the --project and --role you joined with: %s"
                % "; ".join(flags(r["project"], r["role"]) for r in others))
    else:
        hint = ("join it first, with --server ALIAS (or --local on the machine that holds the "
                "channel): vcharon join %s --server ALIAS %s" % (channel, flags(project, role)))
    raise channels.refused("you aren't in %s as %s (no join record on this box)"
                           % (channel, flags(project, role)), hint)


# --- records ---

def records_dir():
    # a folder of its own, so a record can't share a name with a job's state file
    return os.path.join(platform.state_dir(), "channels")


def record_path(channel, name):
    return os.path.join(records_dir(), "%s.%s.json" % (channel, name))


def read_record(channel, name):
    """The member's record (a dict), or None. One that can't be read is an error: vcharon never
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
            or sorted(doc) != sorted(RECORD_KEYS)
            or not isinstance(doc["project"], str)
            or not isinstance(doc["role"], (str, type(None)))
            or not isinstance(doc["format"], int) or isinstance(doc["format"], bool)
            or not isinstance(doc["limits"], dict)
            or doc["version"] != RECORD_VERSION or doc["channel"] != channel
            or doc["name"] != name
            or not all(isinstance(doc[k], str) for k in ("leader", "remote", "machine"))
            or not isinstance(doc["ssh"], (str, type(None)))):
        raise VCharonError("config", "the record %s has another shape" % path, hint=hint)
    return doc


def channel_limits(record):
    """The channel's limits from its join record, after the format check (charter.check):
    post, read, watch and sync run it first."""
    channel = record["channel"]
    where = "--local" if record["ssh"] is None else "--server %s" % record["ssh"]
    rejoin = ("vcharon join %s %s %s (a rejoin) writes it"
              % (channel, where, name_flags(channel, record["name"], record)))
    return charter.check(channel, record, limits_hint=rejoin)


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
    data = json.dumps({k: doc[k] for k in RECORD_KEYS},
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


def write_section(cfg, section, alias, name, leader, remote_text, limits):
    """The section file of a remote member: its two jobs (config), with the channel's folder
    limits for up's check and down's skip."""
    text = ("[%s]\nssh            = %s\nmailbox.me     = %s\nmailbox.leader = %s\n"
            "mailbox.local  = %s\nmailbox.remote = %s\nmailbox.max_mb = %d\n"
            "mailbox.max_files = %d\n"
            % (section, alias, name, leader, local_text(section), remote_text,
               limits["max_mb"], limits["max_files"]))
    path = section_path(cfg, section)
    # .<name>.ini.tmp: no reader takes it for a section
    _write_atomic(path, text.encode("utf-8"),
                  os.path.join(_made(os.path.dirname(path)), ".%s.ini.tmp" % section))


# --- locks ---

def watcher_snapshot(record_or_none, section, name, channel_dir=None):
    """The watcher's snapshot path of this membership, from the watcher's own snapshot_path
    (one rule, never a copy): a remote member's by its section, a local member's by its
    channel folder, which the watch passes as os.path.abspath(os.path.expanduser(dir)) and
    snapshot_path keys by its realpath."""
    # here, not at the top: the watch module imports this one
    from .mailbox import watch
    if channel_dir is None:
        return watch.snapshot_path(None, name, section)
    return watch.snapshot_path(os.path.abspath(os.path.expanduser(channel_dir)), name)


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
            raise channels.refused("%s is held (a watcher, or a sync, of %s in %s)"
                                   % (path, record["name"], channel), "stop the watcher first")


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
            raise channels.refused("%s on %s isn't usable as a channel (%s)"
                                   % (channel, where, other.get("why")), "ask the user")
    return None


def _server(args, cfg, log, stack):
    if args.local:
        return _Local(log)
    ssh.check_dest(args.server)
    session = stack.enter_context(ssh.Session(cfg.settings, args.server, log))
    return _Remote(session, args.server, session.open(), log)


def _where_flag(server):
    return "--local" if server.ssh is None else "--server %s" % server.ssh


def _need_machine(server):
    if server.ssh is not None:
        # a Mac or Windows root takes local members only
        state.need_linux({"os": server.os}, server.where, "only a Linux server takes remote "
                         "members: check the alias")
    if not server.machine:
        hint = state.NO_MACHINE_HINT
        if server.ssh is None:
            # this machine: a Mac or Windows box has a hint of its own
            hint = platform.no_machine_hint() or hint
        raise VCharonError("state_mismatch", "%s has no machine id, so vcharon can't tie a "
                           "channel's record to it" % server.where, hint)


# --- the command ---

def main(args, run):
    """vcharon list, create, join, leave and close: exit 0, or that of the error."""
    cfg = config.load()
    log = run.log = Log(os.path.join(platform.log_dir(), "vcharon.log"), console=args.verbose)
    log.info("vcharon %s%s; config %s" % (args.command, " " + args.channel
                                          if getattr(args, "channel", None) else "", cfg.path))
    for skip in cfg.skipped:
        log.info(skip.line)

    def say(line):
        print(line)
        sys.stdout.flush()
        log.info(line)

    if args.command == "list":
        return _list(args, cfg, log, say)
    if args.command in ("create", "join"):
        check_channel(args.channel)
        check_role(args.role)
        project = project_part(args.project)
        # a membership this box has already keeps the name it joined with (DESIGN, "Which
        # membership")
        record = find(args.channel, project, args.role)
        if record is not None:
            name = record["name"]
        else:
            name = member_parts(cfg, project, args.role)[0]
            # a role forgotten, or one too many: said, never refused (only its step 1)
            for other in records(args.channel):
                if other["project"] == project and (other["role"] or None) != args.role:
                    say("note: you also hold %s here as %s" % (
                        args.channel, "--role %s" % other["role"] if other["role"]
                        else "the member without a role"))
        # stored in the record, so a hint can print the flags that find the membership
        args.ident = {"project": project, "role": args.role}
        if getattr(args, "takeover", False) and not args.rejoin:
            raise VCharonError("config", "--takeover goes with --rejoin",
                               hint="add --rejoin, and only when your user confirms that this "
                               "machine made that folder")
        detected = platform.detect_agent()
        # agent_given: --agent, or found in the environment; a rejoin updates agent: only then.
        # The claimer is set after _need_machine: a Mac or Windows box without its id gets
        # that check's own refusal first
        args.fields = {"agent": args.agent or detected or "other",
                       "agent_given": bool(args.agent or detected), "claimer": None}
        if args.command == "create":
            return _create(args, cfg, name, log, say)
        return _join(args, cfg, name, log, say)
    record = membership(args.channel, args.project, args.role)
    return _leave(args, cfg, record, log, say, close=args.command == "close")


def _list(args, cfg, log, say):
    """vcharon list; with --json one object (list_json)."""
    with contextlib.ExitStack() as stack:
        server = _server(args, cfg, log, stack)
        listing = server.list()
    if args.json:
        print(json.dumps(list_json(server, listing), ensure_ascii=False))
        sys.stdout.flush()
        return 0
    say("vcharon: list  (%s)" % server.where)
    if not listing["channels"] and not listing["others"]:
        say("  no channels")
    for ch in listing["channels"]:
        leaders = ch["leaders"]
        newest = ch.get("newest")
        fmt = _format_of(ch)
        say("  %s  leader %s  members %s  newest %s  format %s"
            % (ch["name"], leaders[0] if len(leaders) == 1 else "?",
               ", ".join(ch["members"]) or "none",
               entries.minute_stamp(newest) if isinstance(newest, (int, float)) else "-",
               "-" if fmt is None else fmt))
        if not leaders:
            say("    note: no member's folder holds %s: ask the user" % entries.CHANNEL_FILE)
        elif len(leaders) > 1:
            say("    note: %s all hold %s: ask the user"
                % (", ".join(l + "/" for l in leaders), entries.CHANNEL_FILE))
        for one in member_info(ch):
            say("    %s  box %s  os %s  agent %s  project %s"
                % tuple([one["name"]] + [one[k] or "-" for k in channels.LIST_FIELDS]))
        for stray in ch.get("strays", []):
            say("    note: %s at its top isn't a member's folder" % pathrules.show(stray))
        if len(leaders) == 1:
            # the join's own check, as a note: the other channels are still listed
            note = charter.format_note(ch["name"], ch)
            if note is not None:
                say("    note: %s" % platform.runnable(note))
    for other in listing["others"]:
        say("  note: %s: %s" % (pathrules.show(other["name"]), other["why"]))
    return 0


def _format_of(ch):
    fmt = ch.get("format")
    return fmt if isinstance(fmt, int) and not isinstance(fmt, bool) else None


def _limits_of(ch):
    """A listed channel's limits as list --json gives them: each an int or null."""
    limits = ch.get("limits") if isinstance(ch.get("limits"), dict) else {}
    return {k: limits.get(k) if isinstance(limits.get(k), int)
            and not isinstance(limits.get(k), bool) else None for k in charter.LIMIT_KEYS}


def member_info(ch):
    """[{"name", "box", "os", "agent", "project"}] of a listed channel's members, in its
    members' order: from their MEMBER.md, None for each field it lacks."""
    fields = ch.get("fields") if isinstance(ch.get("fields"), dict) else {}
    out = []
    for name in ch["members"]:
        one = fields.get(name) if isinstance(fields.get(name), dict) else {}
        out.append(dict([("name", name)] + [
            (k, one.get(k) if isinstance(one.get(k), str) else None)
            for k in channels.LIST_FIELDS]))
    return out


def list_json(server, listing):
    """vcharon list --json: {"server", "channels", "others"}. "server" is the alias, or null
    for --local; each channel is {"name", "leader", "leaders", "members", "member_info",
    "newest", "strays", "format", "limits"}: "leader" is the one leader, or null when there
    are none or several ("leaders" lists them), "member_info" each member's {"name", "box",
    "os", "agent", "project"} from its MEMBER.md (null for a field it lacks), "newest" the
    newest entry's local time (YYYY-mm-dd HH:MM) or null, "format" the channel's format from
    its leader's CHANNEL.md (null when it has none) and "limits" its {"max_mb", "max_files",
    "max_entry_kb"} (null each when missing); each of "others" is {"name", "why"}, a name at
    the root that isn't a usable channel."""
    out = []
    for ch in listing["channels"]:
        leaders = list(ch["leaders"])
        newest = ch.get("newest")
        out.append({"name": ch["name"], "leader": leaders[0] if len(leaders) == 1 else None,
                    "leaders": leaders, "members": list(ch["members"]),
                    "member_info": member_info(ch),
                    "newest": entries.minute_stamp(newest) if isinstance(newest, (int, float))
                    else None,
                    "strays": list(ch.get("strays", [])), "format": _format_of(ch),
                    "limits": _limits_of(ch)})
    return {"server": server.ssh, "channels": out,
            "others": [{"name": o["name"], "why": o["why"]} for o in listing["others"]]}


def _another_server(record, server, channel):
    if record is not None and (record["machine"] != server.machine or record["ssh"] != server.ssh):
        raise channels.refused("you are in %s on another server" % channel,
                               "pass --role R to join from here as another member")


def create_limits(args):
    """The limits create writes into CHANNEL.md: --max-mb, --max-files and --max-entry-kb
    (each checked against charter.BOUNDS by the parser), else the defaults. An entry file
    can't be bigger than the folder that holds it."""
    limits = charter.default_limits()
    for key in charter.LIMIT_KEYS:
        value = getattr(args, key, None)
        if value is not None:
            limits[key] = value
    if charter.limits_problem(limits):
        raise VCharonError("config", "--max-entry-kb %d is more than the folder limit of %d MB"
                           % (limits["max_entry_kb"], limits["max_mb"]),
                           hint="give a smaller --max-entry-kb, or a larger --max-mb")
    return limits


def _create(args, cfg, name, log, say):
    channel = args.channel
    section = "%s.%s" % (channel, name)
    info = {"format": charter.FORMAT, "limits": create_limits(args)}
    record = read_record(channel, name)
    with contextlib.ExitStack() as stack:
        server = _server(args, cfg, log, stack)
        _need_machine(server)
        # before any claim: a client-id file that can't be read or made stops here
        args.fields["claimer"] = platform.claimer(args.channel)
        _another_server(record, server, channel)
        say("vcharon: create %s  as %s on %s" % (channel, name, server.where))
        got = server.claim(channel, name, True)
        made = []
        try:
            own, _remote_text, _ = _write_member(cfg, server, channel, name, name, section,
                                                made, got, args.ident, args.fields, info,
                                                create=True)
        except BaseException:
            # while the folder holds only those files: release removes nothing else
            _undo(made)
            try:
                server.release(channel, name)
            except Exception as e:  # noqa: BLE001
                log.warn("couldn't release %s/%s after the failure: %s" % (channel, name, e))
            raise
    say("  claimed %s/%s; you lead it" % (channel, name))
    say(platform.runnable(TRUST))
    say("  format %d; limits per member folder %s, per entry file %s"
        % (info["format"], charter.limit_text(info["limits"]["max_mb"] * charter.MB,
                                              info["limits"]["max_files"]),
           charter.size_text(info["limits"]["max_entry_kb"] * charter.KB)))
    if server.ssh is None:
        say("OK  created %s; your folder is %s" % (channel, own))
        return 0
    code = _run_section(args, section, full=True)
    if code != 0:
        say(platform.runnable("vcharon: the sync failed; %s is created: run vcharon sync %s "
                              "--full %s again" % (channel, channel, name_flags(channel, name))))
        return code
    say("OK  created %s; your folder is %s" % (channel, own))
    return 0


def _write_member(cfg, server, channel, name, leader, section, made, got, ident, fields,
                  info, create=False, rejoin=False):
    """The record, MEMBER.md (and CHANNEL.md for create), a remote member's section file:
    returns (the own folder, mailbox.remote's text or the channel folder, MEMBER.md's fields
    after leader:). made collects what it wrote, for _undo. info: the channel's {"format",
    "limits"}, checked; the record keeps them."""
    limits = info["limits"]
    remote = server.ssh is not None
    if remote:
        # the root as the helper spelled it: ~/… for the fixed one
        remote_text = posixpath.join(got["root"], channel)
    else:
        remote_text = os.path.join(server.root, channel)
    doc = {"version": RECORD_VERSION, "channel": channel, "name": name, "leader": leader,
           "ssh": server.ssh, "remote": remote_text, "machine": server.machine,
           "format": info["format"], "limits": dict(limits)}
    doc.update(ident)
    fields = member_header(name, ident, fields, cfg)
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
        _member_md(own, channel, name, leader, made, fields)
    if create:
        made.append(os.path.join(own, entries.CHANNEL_FILE))
        # no host name of either end: a channel's files are shared
        entries.post(os.path.join(own, entries.CHANNEL_FILE), own, name,
                     "channel %s created" % channel, [entries.ALL],
                     header=[("leader", name), ("created", entries.stamp(time.time())),
                             ("rules", RULES)] + charter.header(VERSION, limits),
                     number=2)
    if remote:
        if not os.path.exists(section_path(cfg, section)):
            made.append(section_path(cfg, section))
        write_section(cfg, section, server.ssh, name, leader, remote_text, limits)
    return own, remote_text, fields


def member_header(name, ident, fields, cfg):
    """MEMBER.md's fields after leader: (box, os, agent, project, claimer), as (key, value):
    the box is the name's own (a name kept from before a box change keeps its box), the OS
    this one's word; no host name, path, user name or raw machine id."""
    suffix = "-%s" % ident["project"] + ("-%s" % ident["role"] if ident.get("role") else "")
    box = name[:-len(suffix)] if name.endswith(suffix) and len(name) > len(suffix) else None
    return [("box", box or cfg.box_name), ("os", platform.os_word()),
            ("agent", fields["agent"]), ("project", ident["project"]),
            ("claimer", fields["claimer"])]


def _member_md(own, channel, name, leader, made, fields):
    path = os.path.join(own, entries.MEMBER_FILE)
    if os.path.exists(path):
        return
    made.append(path)
    entries.post(path, own, name, "member", ["@" + leader],
                 header=[("channel", channel), ("name", name), ("leader", leader)] + fields,
                 number=1)


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
        # before any claim: a client-id file that can't be read or made stops here
        args.fields["claimer"] = platform.claimer(args.channel)
        say("vcharon: join %s  as %s on %s" % (channel, name, server.where))
        # 1. the leader, before any claim: a refused join leaves nothing behind
        found = _find(server.list(), channel, server.where)
        if found is None:
            raise channels.refused("there is no channel %s on %s: check its name"
                                   % (channel, server.where),
                                   "vcharon list %s" % _where_flag(server))
        leaders = found["leaders"]
        if not leaders:
            raise channels.refused("%s has no leader (no member's folder holds %s)"
                                   % (channel, entries.CHANNEL_FILE), "ask the user")
        if len(leaders) > 1:
            raise channels.refused("%s has %d leaders (%s hold %s)"
                                   % (channel, len(leaders), ", ".join(l + "/" for l in leaders),
                                      entries.CHANNEL_FILE), "ask the user")
        leader = leaders[0]
        # the channel's format and limits, read at the server: a newer format, or none, is
        # refused before any claim
        info = {"format": found.get("format"), "limits": charter.check(channel, found)}
        # 2. a live session already is <name>
        if server.ssh is None:
            lock = watcher_snapshot(None, section, name,
                                    os.path.join(server.root, channel)) + ".lock"
        else:
            lock = watcher_snapshot(None, section, name) + ".lock"
        if held(lock):
            raise channels.refused("a live session holds %s in %s" % (name, channel),
                                   "if that watcher is yours, keep using it; else pass --role R "
                                   "to join as another member")
        _another_server(record, server, channel)
        # 3. one mkdir
        got = server.claim(channel, name, False)
        rejoin = got["existed"]
        # the claim's own reading of CHANNEL.md is the one that counts: checked as the list's
        # was, and the same as the list's, or the join stops before writing anything
        try:
            claimed = {"format": got.get("format"), "limits": charter.check(channel, got)}
            if claimed != info:
                raise channels.refused("%s's format or limits changed during the join (format "
                                       "%s, then %s)" % (channel, info["format"],
                                                         claimed["format"]),
                                       "run the join again")
        except VCharonError:
            if not rejoin:
                _release_quietly(server, channel, name, log)
            raise
        # 4. a folder that was there: whose (its MEMBER.md's claimer:)
        takeover = rejoin and _claimed_elsewhere(args, server, got, name, record)
        if rejoin and record is None and not args.rejoin:
            raise channels.refused("the name %s is taken in %s" % (name, channel),
                                   "pass --role R to join as another member; --rejoin only when "
                                   "the user says that folder is yours")
        if rejoin:
            # before the record: a MEMBER.md that is a link fails here, not halfway through
            _check_member_file(_own_path(server, got, channel, name, section))
        made = []
        try:
            # 5. the record first; before it is written, a failure releases the claim
            own, remote_text, fields = _write_member(cfg, server, channel, name, leader,
                                                     section, made, got, args.ident,
                                                     args.fields, info, rejoin=rejoin)
        except BaseException:
            if not rejoin:
                _undo(made)
                try:
                    server.release(channel, name)
                except Exception as e:  # noqa: BLE001
                    log.warn("couldn't release %s/%s after the failure: %s" % (channel, name, e))
            raise
        say("  %s %s/%s; the leader is %s" % ("took back" if rejoin else "claimed", channel,
                                              name, leader))
        say(platform.runnable(TRUST))
        # 6. a rejoin pulls back what this box lacks of its own folder first: up from a folder
        # missing files it sent would delete them at the server, and posts would restart at #1
        if rejoin and server.ssh is not None and needs_pull(section, own):
            _pull_own(server, remote_text, name, own, log, say)
        if rejoin:
            os.makedirs(own, exist_ok=True)
            _member_md(own, channel, name, leader, [], fields)
        if takeover:
            # after the pull, which brought the old claimer: back; the sync below sends it
            _check_member_file(own)
            entries.set_header(os.path.join(own, entries.MEMBER_FILE), own, name, 1,
                               "claimer", args.fields["claimer"])
            say("  took over %s/%s: its claimer is this machine's now" % (channel, name))
        if rejoin:
            # the same checkout may run another agent now: its name isn't tied to one
            _update_agent(own, name, args.fields, say)
    # before the sync, which then sends it with MEMBER.md: the leader sees the JOIN when the
    # folder appears, not at this member's next sync
    entries.post(os.path.join(own, "RESULTS.md"), own, name, "REJOIN" if rejoin else "JOIN",
                 ["@" + leader], body="%s %s %s." % (name, "rejoined" if rejoin else "joined",
                                                     channel))
    code = 0
    if server.ssh is not None:
        code = _run_section(args, section, full=True)
        if code == 130:
            # a Ctrl-C stops everything at once, as in vcharon sync
            return code
        if code != 0:
            say(platform.runnable("vcharon: the sync failed; you are in %s: run vcharon sync %s "
                                  "--full %s again" % (channel, channel,
                                                       name_flags(channel, name))))
    # 7. the member's first watcher start is a baseline and never prints these
    tree = os.path.dirname(own)
    _print_entries(tree, name, leader, channel, say)
    if code == 0:
        say("OK  in %s as %s; your folder is %s" % (channel, name, own))
    return code


def _release_quietly(server, channel, name, log):
    """Releases a claim a failed join made; a failure to is only logged."""
    try:
        server.release(channel, name)
    except Exception as e:  # noqa: BLE001
        log.warn("couldn't release %s/%s after the failure: %s" % (channel, name, e))


def _own_path(server, got, channel, name, section):
    """The own folder a join of name would use, as _write_member makes it."""
    if server.ssh is not None:
        return os.path.join(plugin.Ctx("local").resolve(local_text(section), "mailbox.local"),
                            name)
    return os.path.join(server.root, channel, name)


def _check_member_file(own):
    """own's MEMBER.md is a regular file, or missing: a symlink or anything else is refused
    (unsafe_path) before a rejoin writes into it."""
    path = os.path.join(own, entries.MEMBER_FILE)
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as e:
        raise fsops.error(e, path)
    if fsops.kind(st) != fsops.FILE:
        raise VCharonError("unsafe_path", "%s isn't a regular file (a symlink, or a folder): "
                           "vcharon writes MEMBER.md itself, and never through a link" % path,
                           "remove it by hand, then join again")


def _update_agent(own, name, fields, say):
    """MEMBER.md's agent: set to the agent of this run when it says another (or none), in
    place; only when one was found or --agent given: a rejoin from a plain terminal leaves it.
    A MEMBER.md without vcharon's entry #1 is left as it is: the rejoin goes on."""
    if not fields["agent_given"]:
        return
    agent = fields["agent"]
    path = os.path.join(own, entries.MEMBER_FILE)
    try:
        found = entries.parse_file(path)
    except OSError:
        return
    first = [e for e in found if e.name == name and e.number == 1]
    if not first:
        return
    was = dict(first[0].header).get("agent")
    if was == agent:
        return
    entries.set_header(path, own, name, 1, "agent", agent)
    say("  agent: %s (was %s)" % (agent, was or "not set"))


def _claimed_elsewhere(args, server, got, name, record):
    """For a member folder that was there: refused when its MEMBER.md names another machine's
    claimer, unless --takeover (with --rejoin, checked before); True then, for the rewrite.
    False for the same claimer or none: the record, or --rejoin, decides as before (a new
    remote member's first push may have failed: its MEMBER.md isn't at the server yet)."""
    theirs = got.get("claimer")
    if not isinstance(theirs, str) or theirs == args.fields["claimer"]:
        return False
    if args.takeover:
        return True
    channel = args.channel
    again = "vcharon join %s %s --rejoin --takeover %s" % (
        channel, _where_flag(server), flags(args.ident["project"], args.ident["role"]))
    if record is None:
        raise channels.refused(
            "another machine holds %s in %s" % (name, channel),
            "on this machine, run: vcharon setup --box NAME (ask your user for one), then join "
            "again; only if your user confirms that this machine made that folder (its state "
            "was wiped), run: %s" % again)
    raise channels.refused(
        "another machine holds %s in %s, though this box has a join record of it"
        % (name, channel),
        "ask your user; only if they confirm that this machine made that folder (its machine "
        "id or client-id file changed), run: %s" % again)


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
    own folder): into a temp folder next to the local tree, then each file this box lacks
    moves into the own folder. A file already here is kept: the pull only adds. A pull that
    fails before the move changes nothing here, so it never leaves an own folder that vcharon
    made; one that fails while moving leaves the files already moved. Never through a link.
    The checks go by path, one lstat per part: a link swapped in between a check and the move
    isn't caught (the own folder is the member's own, not another user's)."""
    from . import cli
    path = posixpath.join(remote_text, name)
    local = os.path.dirname(own)
    temp = tempfile.mkdtemp(prefix=".vcharon-pull-", dir=os.path.dirname(local))
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
        # written through (a link there once led a write outside the tree)
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
    """A sync of the section [--full], in this process, as vcharon sync would run it: its
    lines, its exit code."""
    from . import cli
    return cli.sync_section(section, full=full, verbose=args.verbose)


def _leave(args, cfg, record, log, say, close):
    channel = args.channel
    name = record["name"]
    section = "%s.%s" % (channel, name)
    leads = record["leader"] == name
    if close and not leads:
        raise channels.refused("only the leader closes %s, and that is %s"
                               % (channel, record["leader"]),
                               "vcharon leave %s %s" % (channel, name_flags(channel, name, record)))
    if not close and leads:
        raise channels.refused("you lead %s: close it instead" % channel,
                               "vcharon close %s %s" % (channel, name_flags(channel, name, record)))
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
        say("vcharon: %s %s  as %s on %s" % ("close" if close else "leave", channel, name,
                                             server.where))
        # the record's server, compared before any call: leave too, or another server's
        # missing channel would pass for a gone one
        _need_machine(server)
        if server.machine != record["machine"]:
            raise channels.refused("%s isn't the server %s is on (its machine id is %s, "
                                   "the record's %s)"
                                   % (server.where, channel, server.machine,
                                      record["machine"]), "check the alias")
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
                # a local member's server is this machine
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
                flags_ = name_flags(channel, name, record)
                say(platform.runnable(
                    "vcharon: the sync failed, so nothing was removed: run vcharon leave %s %s "
                    "again once vcharon sync %s %s works" % (channel, flags_, channel, flags_)))
                return code
    _remove_membership(cfg, record, section, say)
    if gone:
        # what an agent would otherwise ask its user about: there is nothing more to delete for
        # this membership; another one of the channel on this box is that one's to leave
        try:
            others = sorted(r["name"] for r in records(channel) if r["name"] != name)
        except VCharonError:
            # a record that can't be read: the removal is done, and the note mustn't fail it
            others = ["(a record that can't be read)"]
        say("  note    nothing of %s as %s is left on this machine%s"
            % (channel, name, "; still here: %s (leave each on its own)" % ", ".join(others)
               if others else ""))
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
    local member's watch), the own folder's post lock, the record, and a remote member's section
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
        drop(charter.left_out_path(section))
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
    """The no-link walk (DESIGN, "Create, join, leave, close"): bottom-up, links removed,
    never entered."""
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
