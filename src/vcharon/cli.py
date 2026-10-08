"""The command line (DESIGN, "Command line"): its verbs, their output and exit codes, and the sync
engine's runner behind vcharon sync."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import posixpath
import re
import string
import sys
import threading
import time
import traceback

from . import (
    VERSION,
    channel_cmd,
    channels,
    charter,
    config,
    doctor,
    entries,
    fsops,
    guide,
    install,
    keys,
    pathrules,
    platform,
    plugin,
    proto,
    remote,
    skill,
    ssh,
    stage,
    state,
)

# `run` is the name of _Run objects here.
from . import kind as kinds
from . import run as engine
from .log import HeldLog, Log
from .mailbox import post as post_mod
from .mailbox import read as read_mod
from .mailbox import watch as watch_mod
from .plugins import dir as dir_plugin  # noqa: F401  (loaded at start: see below)
from .plugins import path as path_plugin
from .proto import VCharonError

# Every module of the package is imported above, the plugins too though plugin.py loads them by
# name, and update.py alone is left for vcharon --update: a long-running command imports nothing
# after its start (DESIGN, "Running watchers").

# A dry run's listing shows this many paths, then "… and N more".
LIST_MAX = 50

EXIT_CODES = """exit codes: 0 ok, 1 refused or failed, 2 busy (a lock is held), 3 usage or config,
  4 couldn't connect or start the helper, 130 Ctrl-C.
  watch: 0 a change, 10 quiet (--max-minutes), 11 error, 12 another watcher (or a create,
  join, leave or close of the member) runs, 13 the channel is closed,
  14 vcharon was updated (start it again), 15 a binary's bootloader process was killed,
  16 nothing new (--once).
Every refusal ends with a fix: line, a command to run or one line of text."""

DESCRIPTION = """File-based channels for AI agents, on one machine or across machines over plain
SSH. C is a channel's name. Your member name comes from this box, the project (the folder that
holds .git, .svn or .hg, or --project) and --role; after a join, from your join record."""


TOP_HELP = "vcharon --help"


class _Parser(argparse.ArgumentParser):
    # argparse exits with 2 on a usage error, and 2 means busy; ours is a usage error (3).
    def error(self, message):
        raise VCharonError("config", message, hint="vcharon %s --help"
                           % self.prog.partition(" ")[2] if " " in self.prog else TOP_HELP)


# --repeat's limit: the watcher's --every for a streaming watch is 1 to 300 too.
REPEAT_MAX = 300


def _number(low, high, what):
    """An argparse type: a whole number of what, low to high, in ASCII digits (str.isdigit()
    also takes "²", which int() refuses)."""
    def parse(text):
        if (not text or any(c not in string.digits for c in text) or len(text) > 9
                or not low <= int(text) <= high):
            raise argparse.ArgumentTypeError("must be a whole number of %s, %d to %d: %r"
                                             % (what, low, high, text))
        return int(text)
    return parse


_repeat_seconds = _number(1, REPEAT_MAX, "seconds")


def _parser():
    # no abbreviations: a prefix (sync --ful) would become part of the flags' contract
    parser = _Parser(prog="vcharon", description=DESCRIPTION, epilog=EXIT_CODES,
                     formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=VERSION,
                        help="print the version and exit")
    # --update is a flag, not a verb: the verbs are what agents run, and an agent never
    # updates (DESIGN, "Self-update"). Its options have their own dests, so a verb's --json
    # can't be taken for its.
    parser.add_argument("--update", action="store_true", help="update this binary from "
                        "GitHub releases, asking first; alone or with the four below")
    parser.add_argument("--yes", dest="update_yes", action="store_true",
                        help="with --update: install without asking")
    parser.add_argument("--force", dest="update_force", action="store_true",
                        help="with --update: install the latest even if it isn't newer")
    parser.add_argument("--json", dest="update_json", action="store_true",
                        help="with --update: print one JSON object; installs only with --yes")
    parser.add_argument("--rc", dest="update_rc", action="store_true",
                        help="with --update: count pre-releases too")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    def verb(name, text, example):
        one = commands.add_parser(name, help=text, description=text[0].upper() + text[1:] + ".",
                                  epilog="example: %s" % example,
                                  formatter_class=argparse.RawDescriptionHelpFormatter,
                                  allow_abbrev=False)
        one.add_argument("-v", "--verbose", action="store_true",
                         help="also print log lines to stderr")
        return one

    def channel(one):
        one.add_argument("channel", metavar="C", help="the channel's name")

    def ident(one):
        one.add_argument("--project", metavar="P", help="your name's project part; default: "
                         "the nearest folder that holds .git, .svn or .hg, from the current "
                         "directory up")
        one.add_argument("--role", metavar="R", help="your name's last part, 1 to 6 of a-z0-9: "
                         "a second session in the same project on this box passes one")

    def ignored(one):
        # The guide has a second session in a folder pass --role on every command: a verb that
        # acts on no membership takes both and ignores them, so the rule has no exception.
        for flag, metavar in (("--project", "P"), ("--role", "R")):
            one.add_argument(flag, metavar=metavar, help="ignored: this verb acts on no "
                             "membership")

    def where(one):
        group = one.add_mutually_exclusive_group(required=True)
        group.add_argument("--server", metavar="ALIAS", help="the server, over ssh (an "
                           "~/.ssh/config alias, user@host or ssh://user@host:port): you are a "
                           "remote member, with a local copy that vcharon syncs")
        group.add_argument("--local", action="store_true", help="this machine holds the "
                           "channel root: you write your folder in it directly")

    def as_json(one):
        one.add_argument("--json", action="store_true", help="print one JSON object instead")

    one = verb("setup", "write the config; without --box, print what this machine uses",
               "vcharon setup --box laptop")
    one.add_argument("--box", metavar="NAME", help="this machine's part of every member name, "
                     "at most %d of a-z0-9_- (default: the OS: mac, win, linux)" % config.BOX_MAX)
    ignored(one)
    key = verb("key", "unlock an ssh key into this OS's agent or keychain, so runs stop failing "
               "after a reboot", "vcharon key devbox")
    key.add_argument("dest", metavar="ALIAS", nargs="?",
                     help="the server whose key to unlock, and test")
    key.add_argument("--key", metavar="FILE", dest="key_file",
                     help="the private key file to unlock, instead of the one ssh finds")
    ignored(key)
    doc = verb("doctor", "check this machine, the servers of your channels and their syncs; "
               "changes nothing", "vcharon doctor --server devbox")
    doc.add_argument("--server", metavar="ALIAS", help="check this server only")
    as_json(doc)
    ignored(doc)
    ping = verb("ping", "connect, echo 1 MiB, check the server's Python", "vcharon ping devbox")
    ping.add_argument("dest", metavar="ALIAS", help="an ~/.ssh/config alias, user@host, or "
                      "ssh://user@host:port")
    ignored(ping)

    one = verb("list", "list a server's channels, their leaders and members",
               "vcharon list --server devbox")
    where(one)
    as_json(one)
    ignored(one)
    def agent(one):
        one.add_argument("--agent", choices=platform.AGENTS, help="the agent you are, for "
                         "MEMBER.md and vcharon list (default: found from the environment, "
                         "else other)")

    one = verb("create", "create a channel; you lead it", "vcharon create myapp --server devbox")
    channel(one)
    where(one)
    ident(one)
    agent(one)
    for flag, key, what, default in (
            ("--max-mb", "max_mb", "MB", "the most each member's folder may hold, in MB "
             "(default %d)" % charter.DEFAULT_MAX_MB),
            ("--max-files", "max_files", "files", "the most files each member's folder may "
             "hold (default %d)" % charter.DEFAULT_MAX_FILES),
            ("--max-entry-kb", "max_entry_kb", "kB", "the most an entry file may hold, in kB "
             "(default %d)" % charter.DEFAULT_MAX_ENTRY_KB)):
        low, high = charter.BOUNDS[key]
        one.add_argument(flag, dest=key, metavar="N", type=_number(low, high, what),
                         help="%s; %d to %d" % (default, low, high))
    one = verb("join", "join a channel as a member", "vcharon join myapp --server devbox")
    channel(one)
    where(one)
    ident(one)
    agent(one)
    one.add_argument("--rejoin", action="store_true", help="take an existing folder of your "
                     "name without a record here; only when the user says it's yours")
    one.add_argument("--takeover", action="store_true", help="with --rejoin: take the folder "
                     "though another machine's id holds it; only when the user confirms this "
                     "is that machine")
    one = verb("leave", "leave a channel (a member; in a work channel the leader closes it)",
               "vcharon leave myapp")
    channel(one)
    ident(one)
    one = verb("close", "close and delete a work channel (its leader only)",
               "vcharon close myapp")
    channel(one)
    ident(one)
    one = verb("whoami", "your name, folder, local or remote, server; without C, every "
               "channel you joined from this project", "vcharon whoami myapp --json")
    one.add_argument("channel", metavar="C", nargs="?", help="the channel's name")
    one.add_argument("--all", action="store_true", help="in a lobby: also the members not "
                     "seen in 24 h")
    ident(one)
    as_json(one)

    one = verb("post", "post an entry to a .md file in your own folder: the one --file names, "
               "else RESULTS.md (in a lobby, the day's chat-YYYY-MM-DD.md)",
               "vcharon post myapp --to @mac-myapp --title \"step 2 done\" --body \"tests pass\"")
    channel(one)
    one.add_argument("--to", nargs="+", required=True, metavar="@NAME",
                     help="@<name> ..., or @all (in a work channel, the leader only); one "
                     "argument or several")
    one.add_argument("--title", required=True, help="the entry's title, one line")
    one.add_argument("--re", metavar="NAME#N", help="the entry this answers")
    one.add_argument("--body", metavar="TEXT", help="the body; without it, stdin is the body. "
                     "A body line that starts like a Markdown heading gets '> ' in front")
    one.add_argument("--file", metavar="NAME.md", help="the .md file in your own folder, a "
                     "subfolder's with a / (default: RESULTS.md; in a lobby, the day's "
                     "chat-YYYY-MM-DD.md)")
    one.add_argument("--steps", action="store_true", help="the leader's plan: --file STEPS.md, "
                     "the leader of a work channel only")
    one.add_argument("--no-sync", action="store_true", help="a remote member: don't send the "
                     "entry to the server now; your watcher or the next sync sends it")
    ident(one)
    one = verb("read", "every member's entries, in one order; with IDs, just those entries, "
               "whole", "vcharon read myapp mac-myapp#3")
    channel(one)
    one.add_argument("ids", metavar="ID", nargs="*", help="only these entries (<name>#<n>, "
                     "as the watcher prints them), each with its header and body")
    one.add_argument("--to-me", action="store_true", help="only the entries your watcher "
                     "prints as to you: or to all:")
    one.add_argument("--last", type=_number(1, 1000000, "entries"), metavar="N",
                     help="only the newest N entries")
    one.add_argument("--full", action="store_true", help="each entry's other header lines and "
                     "its body too")
    ident(one)
    as_json(one)
    one = verb("watch", "print what reaches you, one line per entry",
               "vcharon watch myapp --until-change")
    channel(one)
    one.add_argument("--until-change", action="store_true", help="exit after the first round "
                     "that printed a change or an error that counts (for a background command)")
    one.add_argument("--every", type=_number(1, 86400, "seconds"), metavar="S",
                     help="seconds between rounds (a local member %d; a remote member %d, 1 to "
                     "%d; with --no-stream %d)" % (watch_mod.DIR_EVERY, watch_mod.STREAM_EVERY,
                                                   watch_mod.STREAM_EVERY_MAX,
                                                   watch_mod.RUN_EVERY))
    one.add_argument("--max-minutes", type=_number(1, 1440, "minutes"), metavar="M",
                     help="exit between rounds after this many minutes (default: none; 25 with "
                     "--until-change)")
    one.add_argument("--fresh", action="store_true", help="ignore the saved snapshot")
    one.add_argument("--once", action="store_true", help="one round at once, then exit: 0 a "
                     "change, 16 nothing new (a check between steps, for an agent with no "
                     "background commands)")
    one.add_argument("--no-stream", action="store_true", help="a remote member: a sync each "
                     "round, in place of one long-lived vcharon sync --repeat")
    one.add_argument("--max-errors", type=_number(1, 1000, "rounds"), metavar="N",
                     help="with --until-change: exit after this many failed rounds in a row "
                     "(default 10); a streaming watch counts %d s of failing as one"
                     % watch_mod.STREAM_ERROR_ROUND)
    ident(one)
    one = verb("sync", "a remote member: push your folder, pull the others'",
               "vcharon sync myapp --full")
    channel(one)
    one.add_argument("--repeat", type=_repeat_seconds, metavar="S",
                     help="keep one connection open and sync again and again, S (1 to %d) "
                     "seconds after each round ends, until stdin ends; each round prints its "
                     "errors and a ROUND <exit code> line (what watch starts)" % REPEAT_MAX)
    one.add_argument("--full", action="store_true", help="compare both ends by content, and "
                     "send only what the other end doesn't hold; reads every file on both ends")
    one.add_argument("--dry-run", action="store_true", help="build and check the plan, print "
                     "it, change nothing and save nothing")
    one.add_argument("--reset", choices=("up", "down"), help="forget what this box has "
                     "synced: up (your folder, sent) or down (the others', pulled); the next "
                     "sync --full compares by content")
    ident(one)

    one = verb("guide", "the agent guide for this vcharon, or one of its topics",
               "vcharon guide watch")
    one.add_argument("topic", metavar="TOPIC", nargs="?", help="one of: %s"
                     % ", ".join(guide.TOPICS))
    ignored(one)
    one = verb("skill", "write the skill that points agents at vcharon guide",
               "vcharon skill install --claude")
    actions = one.add_subparsers(dest="action", metavar="<action>")
    actions.required = True
    one = actions.add_parser("install", help="write it for Claude Code and Codex (no flag: "
                             "both)", description="Write the skill where each agent looks for "
                             "user skills; a file that vcharon didn't write is refused.",
                             epilog="example: vcharon skill install --codex",
                             formatter_class=argparse.RawDescriptionHelpFormatter,
                             allow_abbrev=False)
    for agent, parts in skill.AGENTS:
        one.add_argument("--" + agent, action="store_true", help="~/%s/%s/SKILL.md"
                         % ("/".join(parts), skill.NAME))
    ignored(one)
    return parser


def main(argv=None):
    # (DESIGN, "Launch rules"): UTF-8 on every OS, before any command acts. A Windows console's
    # code page, or a POSIX locale or PYTHONIOENCODING that isn't UTF-8, would otherwise fail
    # the first name it can't hold, after a create or join has already done its work.
    watch_mod.utf8_output()
    run = _Run()
    if platform.is_frozen() and platform.os_name() == "windows":
        # the copies earlier updates renamed the running binary to (DESIGN, "Self-update")
        install.sweep_old()
        # a kill of the bootloader alone must end this process too (DESIGN, "Running
        # watchers")
        run.parent = install.exit_with_parent(run.parent_gone)
    return _main(argv, run)


# what may come with --update
UPDATE_FLAGS = ("--update", "--yes", "--force", "--json", "--rc")


def _main(argv, run):
    def command():
        words = sys.argv[1:] if argv is None else list(argv)
        _update_alone(words)
        parser = _parser()
        try:
            args, extra = parser.parse_known_args(words)
        except SystemExit as e:
            # --help, --version
            return e.code if isinstance(e.code, int) else 0
        if extra and args.command == "read":
            # IDs on both sides of a flag (read C a#1 --full b#2): argparse takes only the
            # first run of them into the list
            args.ids += [w for w in extra if not w.startswith("-")]
            extra = [w for w in extra if w.startswith("-")]
        if extra:
            # parse_args would say the same, but from the top parser, whose fix is the top
            # help: a flag unknown after a verb is the verb's to explain
            raise _usage("unrecognized arguments: %s" % " ".join(extra),
                         _help_hint(args, words, extra))
        _update_flags(args)
        if args.update:
            return _update(args, run)
        if args.command is None:
            parser.error("the following arguments are required: <command>")
        return COMMANDS[args.command](args, run)

    return _guarded(command, run)


def _help_hint(args, words, extra):
    """The help command for words the parse left over: the verb's (skill install's) when
    they come after it, else the top help."""
    command = args.command
    if command is None or words.index(command) > words.index(extra[0]):
        return TOP_HELP
    action = getattr(args, "action", None) if command == "skill" else None
    return "vcharon %s --help" % " ".join([command] + ([action] if action else []))


def _update_alone(words):
    """--update's refusal of a verb or another flag, before the parse (whose refusal of a
    verb's missing argument would hide it). Only --update before any verb counts: after one,
    the verb's parser refuses it as unknown."""
    lead = []
    for word in words:
        if not word.startswith("-"):
            break
        lead.append(word)
    if "--update" not in lead or "-h" in lead or "--help" in lead:
        return
    others = [w for w in words if w not in UPDATE_FLAGS]
    if others:
        raise _usage("--update runs on its own, with only --yes, --force, --json and --rc: not "
                     "with %s" % " ".join(others), "run vcharon --update by itself; then the rest")


def _update_flags(args):
    """--yes, --force, --json and --rc before a verb are --update's: without it, refused
    rather than ignored."""
    if args.update:
        return
    for flag, on in (("--yes", args.update_yes), ("--force", args.update_force),
                     ("--json", args.update_json), ("--rc", args.update_rc)):
        if not on:
            continue
        if flag == "--json" and args.command is not None:
            raise _usage("--json goes after the verb: vcharon %s ... --json" % args.command,
                         "move --json after %s" % args.command)
        raise _usage("%s goes only with --update" % flag, "leave out %s" % flag)


def _guarded(fn, run):
    """fn()'s exit code; an error it raises is shown, as every command shows one, and gives
    its exit code."""
    try:
        return fn()
    except VCharonError as e:
        run.show_error(e)
        return e.exit_code
    except BrokenPipeError as e:
        # stdout's reader went away (vcharon doctor | head -3): no ERROR line, and nothing
        # more to say; stdout goes to devnull so Python's own flush at exit can't fail too.
        # Logged, in case the pipe was another one.
        run.log_line("error", "stdout closed: %s" % e, create=True)
        _stdout_to_devnull()
        return 1
    except KeyboardInterrupt:
        # The session has killed ssh on its way out.
        run.log_line("error", "interrupted")
        sys.stderr.write("vcharon: interrupted\n")
        return 130
    except Exception as e:  # noqa: BLE001
        if run.watchdog is not None and run.watchdog.after_crash():
            # a long-running command's binary was swapped under it: the error may be a read
            # of the new file at the old offsets (DESIGN, "Running watchers"). Nothing that
            # could import more: no traceback.
            run.log_line("info", "vcharon changed under this process (%s: %s); exiting with %d"
                         % (type(e).__name__, e, install.EXIT_UPDATED), create=True)
            run.updated_line()
            return install.EXIT_UPDATED
        run.log_line("error", traceback.format_exc(), create=True)
        run.show_error(VCharonError("internal", "%s: %s" % (type(e).__name__, e)),
                       logged=True)
        return 1


def _stdout_to_devnull():
    """Points stdout's file descriptor at devnull (the Python docs' recipe for a closed
    pipe). A stdout without one (a test's) is left as it is."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, ValueError, OSError):
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


def _usage(message, hint):
    return VCharonError("config", message, hint=hint)


# --- a membership's commands: whoami, post, read, watch, sync ---

def _membership(args):
    """(the record of the membership args mean, the flags that find it again): DESIGN,
    "Which membership"."""
    record = channel_cmd.membership(args.channel, args.project, args.role)
    role = record["role"]
    return record, ["--project", record["project"]] + (["--role", role] if role else [])


def _section(record):
    return "%s.%s" % (record["channel"], record["name"])


def _tree(cfg, record):
    """(the channel's tree on this box, whether it's a remote member's copy): a local member's
    channel folder, or a remote member's local tree."""
    if record["ssh"] is None:
        return os.path.abspath(os.path.expanduser(record["remote"])), False
    return watch_mod.mailbox_of(_section(record))[0], True


def _member_doc(cfg, record):
    """whoami's object of one membership (see _whoami)."""
    name = record["name"]
    try:
        tree, _ = _tree(cfg, record)
    except VCharonError:
        # the section is gone; where it would be
        tree = plugin.Ctx("local").resolve(channel_cmd.local_text(_section(record)),
                                           "mailbox.local")
    kind_ = kinds.of(record)
    # a lobby has no leader: the record's leader is its founder, who leads nothing
    lobby = kind_ is kinds.LOBBY
    return {"channel": record["channel"], "name": name, "project": record["project"],
            "role": record["role"], "leader": None if lobby else record["leader"],
            "founder": record["leader"] if lobby else None,
            "leads": record["leader"] == name and kind_.can_close,
            "mode": "local" if record["ssh"] is None else "remote", "server": record["ssh"],
            "folder": os.path.join(tree, name), "tree": tree, "kind": kind_.name}


def _whoami(args, run):
    """vcharon whoami [C] [--all] [--json]. With C, one object: {"channel", "name", "project",
    "role", "leader", "founder", "leads", "mode", "server", "folder", "tree", "kind", "box",
    "box_source", "members"}: "kind" is "work" or "lobby"; a lobby's "leader" is null and
    "founder" the member whose folder holds its CHANNEL.md, who "leads" nothing; a work
    channel's "founder" is null;
    "mode" is "local" or "remote", "server" the alias (null for a local member), "folder" your own
    folder on this box and "tree" the channel's (a remote member's copy); "role" is null
    without one. "box" is this machine's box now (a membership keeps the name it joined
    with), "box_source" "config" ([vcharon] box) or "os" (the default: mac, win, linux).
    With C only, "members": read_mod.member_list's of the tree but its "vcharon", null when
    it can't be read; in a lobby each with "presence" too (_presence), every "leader" false.
    The text of a lobby's lists only the members seen in 24 h, unless --all (_lobby_lines).
    Without C: {"box", "box_source", "project", "role", "name", "channels"}: "name" is the
    name a join from here would take, "channels" every membership of this project on this box
    (of this role too, with --role), each an object as with C, without "box", "box_source"
    and "members"."""
    if args.all and args.channel is None:
        raise _usage("--all goes with a lobby's name", "give the lobby's name, or leave out "
                     "--all")
    cfg = load_config()
    box = {"box": cfg.box_name, "box_source": cfg.box_source}
    if args.channel is not None:
        record, _ = _membership(args)
        lobby = kinds.of(record) is kinds.LOBBY
        if args.all and not lobby:
            raise _usage("--all is for a lobby; %s is a work channel" % args.channel,
                         "leave out --all")
        doc = _member_doc(cfg, record)
        seen = _watched(record, doc["tree"])
        members, unread = _members_of(doc["tree"], None if lobby else record["leader"], seen,
                                      _whoami_skip(record))
        now = time.time()
        if lobby and members is not None:
            for one in members:
                one["presence"] = _presence(one, seen, now)
        if args.json:
            # the version is read's (member_info): whoami's members keep their fields
            shown = None if members is None else [
                {k: v for k, v in one.items() if k != "vcharon"} for one in members]
            return _print_json(dict(doc, members=shown, **box))
        _say("vcharon: whoami %s" % args.channel)
        _whoami_lines(doc)
        _say("  box      %s" % cfg.box_text())
        # the version this box runs: vcharon read notes when the members' differ
        _say("  vcharon  %s" % VERSION)
        if lobby and members is not None:
            _lobby_lines(doc, members, seen, args.all)
        else:
            _members_lines(doc, members, unread, seen)
        return 0
    channel_cmd.check_role(args.role)
    project = channel_cmd.project_part(args.project)
    name = channel_cmd.member_name(cfg, project, args.role)
    mine = [r for r in channel_cmd.records()
            if r["project"] == project and (args.role is None or r["role"] == args.role)]
    doc = dict(box, project=project, role=args.role, name=name,
               channels=[_member_doc(cfg, r) for r in mine])
    if args.json:
        return _print_json(doc)
    _say("vcharon: whoami")
    _say("  box      %s" % cfg.box_text())
    _say("  project  %s%s" % (project, "  role %s" % args.role if args.role else ""))
    _say("  name     %s (a join from here)" % name)
    _say("  vcharon  %s" % VERSION)
    if not mine:
        _say("  no channels joined from this project")
    for one in doc["channels"]:
        _say("")
        _whoami_lines(one)
    return 0


def _members_of(tree, leader, seen, skip=None):
    """(whoami C's members of the tree, read_mod.member_list's; None and why when the tree
    can't be read). whoami says who you are first: a tree it can't list is a line, not an
    error."""
    try:
        return read_mod.member_list(tree, leader, seen, skip), None
    except OSError as e:
        return None, e.strerror or str(e)


def _whoami_skip(record):
    """member_list's skip for whoami C: the folders whose entries it doesn't read for
    "left", as read leaves them out. A remote member's copy: none (its pull left out a folder
    over the limits already). A local member: another member's folder over the channel's
    limits (_over_limit); when the record's limits can't be read, every other member's, since
    whoami never fails on the channel and has no limit to go by."""
    if record["ssh"] is not None:
        return None
    try:
        limits = channel_cmd.channel_limits(record)
    except VCharonError:
        me = record["name"]
        return lambda path, member: None if member == me else "no limits"
    return _over_limit(limits, record["name"])


def _watched(record, tree):
    """The members' last-watched stamps, {name: (time on this box, --every seconds)}: a remote
    member's as its last pull brought them (charter.load_seen), a local member's from the
    stamps beside the channel root; {} for none, or when they can't be read (they only add
    to whoami and read --json)."""
    if record["ssh"] is not None:
        return charter.load_seen(_section(record))
    now = time.time()
    try:
        got = channels.list_seen(os.path.dirname(tree), record["channel"], now)
    except OSError:
        return {}
    return {name: (now - age, channels.parse_pace(pace)[1])
            for name, (age, pace) in got.items()}


def _members_lines(doc, members, unread, seen):
    """whoami C's members, one line each: who writes in the channel (of the members' files it
    shows only MEMBER.md #1's fields, and of their entries only whether each one left), and
    when each one's watcher last pulled it (read_mod.watched_text), or "left". A remote member's are
    this box's copy, the ages as of its last sync."""
    copy = " (this box's copy, as of its last sync)" if doc["mode"] == "remote" else ""
    if members is None:
        _say("  members  can't read %s: %s" % (doc["tree"], unread))
        return
    _say("  members  %d%s" % (len(members), copy))
    now = time.time()
    for one in members:
        marks = [m for m, on in (("leader", one["leader"]), ("you", one["name"] == doc["name"]))
                 if on]
        _say(pathrules.printable("    %s%s  %s  newest file %s  %s" % (
            one["name"], " (%s)" % ", ".join(marks) if marks else "",
            ", ".join("%s %s" % (k, one[k] or "?") for k in read_mod.WHO_FIELDS),
            one["newest"] or "-",
            # its stamp stays after a leave, and would show an age that only grows
            "left" if one["left"] else
            read_mod.watched_text(seen, one["name"], one["vcharon"], now))))


# a lobby's members whoami lists without --all: those seen in this many seconds
SEEN_DAY = 24 * 3600


def _presence(one, seen, now):
    """A lobby member's mark (DESIGN, "The lobby"), the first that holds: left
    (a LEAVE after its last JOIN, and its newest file at most SEEN_DAY old; a left that is
    unknown counts as not left), here (its stamp at most 2 x its --every + 120 s old, the lead
    guide's rule), away (a stamp, or with none its newest file, at most SEEN_DAY old), else
    gone. now: this box's time."""
    t, every = (seen or {}).get(one["name"], (None, None))
    newest = entries.parse_time(one["newest"])
    fresh = newest is not None and now - newest.timestamp() <= SEEN_DAY
    if one["left"]:
        return "left" if fresh else "gone"
    if t is not None and now - t <= 2 * every + 120:
        return "here"
    if (now - t <= SEEN_DAY) if t is not None else fresh:
        return "away"
    return "gone"


def _by_last_watched(members, seen):
    """A lobby's members by last watched, newest first, those with no stamp last."""
    def last(one):
        t = (seen or {}).get(one["name"], (None, None))[0]
        return (t is None, -(t or 0), one["name"])

    return sorted(members, key=last)


def _lobby_lines(doc, members, seen, everyone):
    """whoami lobby's members: by last watched, newest first (no stamp last), each with its
    presence; a gone one only with everyone (--all), else one closing count line."""
    copy = " (this box's copy, as of its last sync)" if doc["mode"] == "remote" else ""
    _say("  members  %d%s" % (len(members), copy))
    now = time.time()
    hidden = 0
    for one in _by_last_watched(members, seen):
        if one["presence"] == "gone" and not everyone:
            hidden += 1
            continue
        _say(pathrules.printable("    %s%s  %s  %s  newest file %s  %s" % (
            one["name"], " (you)" if one["name"] == doc["name"] else "", one["presence"],
            ", ".join("%s %s" % (k, one[k] or "?") for k in read_mod.WHO_FIELDS),
            one["newest"] or "-",
            read_mod.watched_text(seen, one["name"], one["vcharon"], now))))
    if hidden:
        _say("  +%d not seen in 24 h (--all)" % hidden)


# the marks a lobby join's members line names, in its order; gone ones it leaves out
JOIN_PRESENCE = ("here", "away", "left")


def join_presence(record):
    """The line a lobby join prints after its entries: the other members by whoami lobby's
    marks (_presence), each mark's names by last watched, newest first; the gone ones left
    out. A remote member's from the copy and the ages its pulls keep (charter.save_seen: the
    join's pull may leave them up to 30 s behind). None when the tree can't be read: the join's
    work is done, and whoami says why."""
    cfg = load_config()
    doc = _member_doc(cfg, record)
    seen = _watched(record, doc["tree"])
    members, _ = _members_of(doc["tree"], None, seen, _whoami_skip(record))
    if members is None:
        return None
    now = time.time()
    by = {mark: [] for mark in JOIN_PRESENCE}
    for one in _by_last_watched(members, seen):
        mark = _presence(one, seen, now)
        if one["name"] != record["name"] and mark in by:
            by[mark].append(one["name"])
    parts = ["%s: %s" % (mark, ", ".join(by[mark])) for mark in JOIN_PRESENCE if by[mark]]
    return pathrules.printable("  members  %s" % ("; ".join(parts) if parts else
                                                  "no one else seen in 24 h"))


def _whoami_lines(doc):
    if doc["kind"] == kinds.LOBBY.name:
        _say("  channel  %s (a lobby, founded by %s)" % (doc["channel"], doc["founder"]))
    else:
        _say("  channel  %s, led by %s%s" % (doc["channel"], doc["leader"],
                                              " (you)" if doc["leads"] else ""))
    _say("  name     %s  (--project %s%s)" % (doc["name"], doc["project"] or "?",
                                              " --role %s" % doc["role"] if doc["role"] else ""))
    _say("  mode     %s" % ("local, on this machine" if doc["mode"] == "local"
                            else "remote, server %s" % doc["server"]))
    _say("  folder   %s" % doc["folder"])


def _print_json(doc):
    print(json.dumps(doc, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def _stdin_bytes():
    return sys.stdin.buffer.read()


def _stdin_is_terminal():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _post(args, run):
    """vcharon post C: an entry into a .md file of your own folder: RESULTS.md (a lobby's day
    file), --file's, or the leader's STEPS.md (--steps). post() checks the file
    (mailbox/post.py)."""
    to, title, re_ = post_mod.check_args(args.to, args.title, args.re, args.body, args.file)
    if args.steps and args.file is not None:
        raise _usage("--steps is --file %s: give one of them" % post_mod.STEPS_FILE,
                     "leave out --file, or --steps")
    file = (post_mod.STEPS_FILE if args.steps
            else post_mod.RESULTS_FILE if args.file is None else args.file)
    parts = post_mod.file_parts(file)
    if args.body is None and _stdin_is_terminal():
        # never wait for a body typed on the terminal: an agent would hang
        raise _usage("no --body, and stdin is a terminal", "give --body TEXT, or the body on "
                     "stdin: a file, or a quoted heredoc (<<'EOF')")
    cfg = load_config()
    record, flags = _membership(args)
    kind_ = kinds.of(record)
    if args.steps and not kind_.has_plan:
        # a flag the lobby has no use for: a usage error, as close and create of it are
        raise _usage("a lobby has no plan", "post without --steps")
    # a lobby's day file is named here from this clock reading, for post()'s checks; the
    # entry goes into the one of its heading's date, chosen under the post lock
    today = kind_.day_file(entries.stamp(time.time()))
    day = args.file is None and not args.steps and today is not None
    if day:
        parts = [today]
    if args.no_sync and record["ssh"] is None:
        raise _usage("--no-sync is for a remote member; you are a local member of %s"
                     % args.channel, "leave out --no-sync")
    limits = channel_cmd.channel_limits(record)
    name = record["name"]
    steps = len(parts) == 1 and parts[0].casefold() == post_mod.STEPS_FILE.casefold()
    if steps and record["leader"] != name:
        # --steps, or --file STEPS.md at the top of the own folder
        raise channel_cmd.channels.refused(
            "%s is the leader's (%s): its plan" % (post_mod.STEPS_FILE, record["leader"]),
            "leave out %s: your entries go into %s"
            % ("--steps" if args.steps else "--file %s" % file, post_mod.RESULTS_FILE))
    tree, synced = _tree(cfg, record)
    own = os.path.join(tree, name)
    _check_tree(record, tree, synced, own)
    post_mod.check_own(own, tree if synced else None)
    # a remote member's missing own folder is post()'s to refuse, with the rejoin as its fix
    path = (post_mod.file_below(own, parts, kind_) if os.path.isdir(own)
            else os.path.join(own, *parts))
    body = args.body if args.body is not None else post_mod.body_from(_stdin_bytes())
    done = {}
    id_, when = post_mod.post(path, name, to, title, re_, body, limits=limits,
                              channel=args.channel, kind_=kind_, day=day, done=done)
    if day:
        parts = [os.path.basename(done["path"])]
    # "into" the file, "to" the addressees, as leave's LEAVE line, so the file isn't read as
    # an address
    print("posted %s — %s into %s, to %s at %s" % (id_, title, "/".join([name] + parts),
                                                   " ".join(done["to"]), when))
    for removed in done["removed"]:
        print(channel_cmd.REMOVED_OLD % (removed, kinds.KEEP_DAYS))
    sys.stdout.flush()
    for failed, why in done["failed"]:
        print(channel_cmd.REMOVE_FAILED % (failed, kinds.KEEP_DAYS, why), file=sys.stderr)
    sys.stderr.flush()
    _warn_hosts(cfg, record, tree, id_, title, body)
    if record["ssh"] is not None and not args.no_sync:
        _send_post(args.channel, record, flags)
    return 0


def _ssh_hosts(cfg, records):
    """The ssh aliases and host names this machine's vcharon uses: the host part of each join
    record's server (records) and of each config job's destination (user@, ssh:// and :port
    taken off), one of each name whatever its case."""
    dests = [job.ssh for job in cfg.jobs.values()] + [r["ssh"] for r in records]
    hosts = {}
    for dest in dests:
        if not isinstance(dest, str):
            continue
        host = dest.removeprefix("ssh://").rpartition("@")[2]
        if host.startswith("["):
            host = host[1:].partition("]")[0]
        elif host.count(":") == 1:
            host = host.partition(":")[0]
        # a one-letter alias would match every "a" in a sentence
        if len(host) > 1:
            hosts.setdefault(host.casefold(), host)
    return set(hosts.values())


def _channel_words(cfg, channel, records, tree):
    """The casefolded words a channel's entries name anyway: the channel's name, this box's,
    and each '-'-joined run of parts of every member name (the tree's folders, records' names
    and leaders), so a box, project or role is in it whole, plus the records' projects and
    roles. records: this machine's records of the channel only. An alias that is one of these
    is no news in an entry."""
    names = [r[k] for r in records for k in ("name", "leader")]
    try:
        names += [n for n in os.listdir(tree) if os.path.isdir(os.path.join(tree, n))]
    except OSError:
        pass
    words = {channel, cfg.box_name}
    words.update(r[k] for r in records for k in ("project", "role") if r[k])
    for name in names:
        parts = name.split("-")
        words.update("-".join(parts[i:j]) for i in range(len(parts))
                     for j in range(i + 1, len(parts) + 1))
    return {w.casefold() for w in words}


def hosts_in(text, hosts):
    """The names among hosts that text holds as a whole word, case-insensitively, as written in
    hosts, sorted. A word goes on through ASCII letters, digits, '_' and '-', and through a '.'
    with one of those after it: "devbox" is in "sent to devbox.", "me@devbox:22" and
    "已发送到devbox。", but not in "devbox-2", "my-devbox" or "devbox.example.com" (a name of
    its own, whose own entry in hosts matches it)."""
    found = set()
    for host in hosts:
        pattern = r"(?<![\w-])(?<![\w-]\.)%s(?![\w-])(?!\.[\w-])" % re.escape(host)
        # ASCII: a name written next to Chinese text (no space between) is still a word
        if re.search(pattern, text, re.IGNORECASE | re.ASCII):
            found.add(host)
    return sorted(found)


# the rules keep machine details out of a channel (the guide's rules topic); a post's own
# command line is the one place vcharon can see one going in, so it warns there
HOST_WARN = ("WARN entry %s names %s: an ssh alias or host name this machine uses; the rules keep "
             "ssh aliases and host names out of the channel")
HOST_FIX = ("  fix: if that is the ssh alias, post a correction entry without it, with --re %s; "
            "if it names something else here, nothing to do")


def _warn_hosts(cfg, record, tree, id_, title, body):
    """A WARN on stderr when the posted entry's title or body names an ssh alias or host
    name of this machine's vcharon (_ssh_hosts), other than a word the channel names anyway
    (_channel_words): the entry stands, and a correction is a new entry. The user sees the
    name on its own terminal only. Never fails: a record that can't be read only leaves its
    names out, since the entry is written already."""
    records = channel_cmd.records(skip_unreadable=True)
    # only this channel's: a project or role of another channel's membership isn't named in
    # this one's entries, so an alias equal to it still warns
    known = _channel_words(cfg, record["channel"],
                           [r for r in records if r["channel"] == record["channel"]], tree)
    hosts = {h for h in _ssh_hosts(cfg, records) if h.casefold() not in known}
    found = hosts_in("%s\n%s" % (title, body), hosts)
    if found:
        print(HOST_WARN % (id_, ", ".join(found)), file=sys.stderr)
        print(HOST_FIX % id_, file=sys.stderr)
        sys.stderr.flush()


def _check_tree(record, tree, synced, own=None):
    """A local member's channel folder that is gone (the channel closed), or its own folder own
    in it (a post's): refused with the leave command, as the watcher refuses it. A rejoin can't
    bring either back (join refuses a record whose folder the channel no longer has), and
    reading or posting into nothing would only say "no such file". A remote member's copy stays
    until its leave."""
    if synced:
        return
    channel, name = record["channel"], record["name"]
    # the own folder is lstat'ed: a link there, dangling or not, is check_own's to refuse
    for path, what, look in [(tree, "the channel folder %s is gone", os.stat),
                             (own, "your folder %s in the channel is gone", os.lstat)]:
        if path is None:
            continue
        # only a folder that isn't there is gone, as the watcher decides: a stat refused for
        # another reason (a parent's permissions) is that error, never the leave fix
        try:
            look(path)
            continue
        except FileNotFoundError:
            pass
        except OSError as e:
            raise fsops.error(e, path) from None
        raise VCharonError("not_found", what % path,
                           channel_cmd.CHANNEL_GONE_HINT
                           % (channel, channel_cmd.name_flags(channel, name, record)))


# a failed send's fix when the server may answer later: the entry waits for the next sync
SEND_LATER_HINT = ("  fix: the entry is saved in your folder; your watcher sends it, or once the "
                   "server answers, run: vcharon sync %s")
# any other failure's: the up job's own fix, since no sync can send it until that is done
SEND_BLOCKED_HINT = "  fix: the entry is saved in your folder, but no sync sends it until: %s"


def _send_post(channel, record, flags):
    """A remote member's post goes to the server at once: its section's up job, in this
    process, its sync lines kept back. The entry is written already, so nothing here fails the
    post: a running watcher's round holds the job's lock and sends it itself (a note), and any
    other failure is a WARN with its error. A short-lived failure (watch_mod.RETRY_KEYS: the
    connection, a file changed during the run, an aborted run) or one with no fix says the
    entry waits for the next sync; any other, such as a closed channel or a name the
    server refuses, gives the job's own fix, since no later sync gets past it."""
    job = _section(record) + ".up"
    run = _Run()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = _guarded(lambda: run_jobs([job], run=run), run)
    if code == 0:
        _say("sent to %s" % record["ssh"])
        return
    if code == proto.EXIT["busy"]:
        print("note: a sync of %s is running (your watcher's): it sends the entry"
              % channel, file=sys.stderr)
        return
    lines = (err.getvalue() + out.getvalue()).splitlines()
    _code, line, fix = watch_mod.parse_failure(code, lines, job)
    print("WARN not sent to %s: %s" % (record["ssh"], line.removeprefix("ERROR ")),
          file=sys.stderr)
    log = None
    if fix is not None:
        # parse_failure puts the job's log after its fix, on a line of its own, or makes a
        # fix that only names the log when the error has none
        fix, _, log = fix.partition(watch_mod.LOG_SEP)
        if fix.startswith(watch_mod.LOG_ONLY_FIX):
            fix, log = None, fix[len(watch_mod.LOG_ONLY_FIX):]
    # the short-lived errors (the connection, a file changed during the run, an aborted run)
    # go away by themselves, so the next sync sends the entry
    if fix is None or watch_mod.error_key(line) in watch_mod.RETRY_KEYS:
        fix = SEND_LATER_HINT % " ".join([channel] + flags)
    else:
        fix = SEND_BLOCKED_HINT % fix
    print(platform.runnable(fix), file=sys.stderr)
    if log:
        print(watch_mod.LOG + log, file=sys.stderr)


# the short read's last line: it lists titles only, and agents in a channel run didn't find
# how to see an entry's body. The same --last and membership flags, so it shows those entries
READ_FULL_HINT = "  note: to see the bodies: vcharon read %s --full %s"


# an ID read can't find: the list shows every entry, and a remote member's copy may not hold
# one just posted. " ; " sets the commands apart, so that no mark sticks to a flag's value
READ_MISSING_HINT = "check the ID in the whole list: vcharon read %s %s"
READ_MISSING_SYNC_HINT = ("if it was just posted, sync, then read it again: vcharon sync %s %s "
                          "; else check the ID in the whole list: vcharon read %s %s")


def _read_ids(args):
    """The IDs read was given, each once, in order, a leading @ dropped (post's --re takes it
    too, by habit); a usage error for one that isn't an ID, or with --last or --to-me."""
    ids = []
    for arg in args.ids:
        one = arg.removeprefix("@")
        if entries.parse_id(one) is None:
            raise _usage("%s isn't an entry's ID (<name>#<n>)" % pathrules.show(arg),
                         "give each ID as <name>#<n>, as the watcher's line prints it")
        if one not in ids:
            ids.append(one)
    if ids and (args.last or args.to_me):
        flag = "--last" if args.last else "--to-me"
        raise _usage("%s goes with the whole list, not with IDs" % flag,
                     "leave out %s, or the IDs" % flag)
    return ids


def _read(args, run):
    """vcharon read C [ID ...] [--json]: read_mod's view of the channel's tree on this box."""
    ids = _read_ids(args)
    cfg = load_config()
    record, flags = _membership(args)
    limits = channel_cmd.channel_limits(record)
    tree, synced = _tree(cfg, record)
    _check_tree(record, tree, synced)
    # a local member reads the other folders where they are: one over the limit is left out,
    # as a remote member's pull leaves it out
    skip = None if synced else _over_limit(limits, record["name"])
    # a remote member: the members its last pull left out, whose copy here is as it was
    notes = charter.left_out_notes(_section(record)) if synced else []
    mine = (record["name"], record["leader"], kinds.of(record)) if args.to_me else None
    # only the reading is in the try: a closed stdout is _guarded's to handle
    try:
        if args.json:
            doc = read_mod.view_json(tree, args.channel, synced=synced, full=args.full,
                                     last=args.last, skip=skip, notes=notes, ids=ids,
                                     mine=mine, seen=_watched(record, tree))
            missing = doc["missing"]
        else:
            last = ["--last", str(args.last)] if args.last else []
            to_me = ["--to-me"] if args.to_me else []
            bodies = platform.runnable(READ_FULL_HINT % (args.channel,
                                                         " ".join(to_me + last + flags)))
            shown, missing = read_mod.view(tree, args.channel, synced=synced, full=args.full,
                                           last=args.last, skip=skip, notes=notes,
                                           bodies=bodies, ids=ids, mine=mine)
    except OSError as e:
        raise fsops.error(e, tree)
    if args.json:
        _print_json(doc)
    else:
        for line in shown:
            print(line)
        sys.stdout.flush()
    if missing:
        # after the ones found, which an agent reads anyway
        flag_text = " ".join(flags)
        hint = (READ_MISSING_SYNC_HINT % (args.channel, flag_text, args.channel, flag_text)
                if synced else READ_MISSING_HINT % (args.channel, flag_text))
        raise VCharonError("not_found", "no entry %s in %s%s"
                           % (", ".join(missing), args.channel,
                              ", as of this box's last sync" if synced else ""), hint)
    return 0


def _over_limit(limits, me):
    """skip(folder path, member name): a local member's read's note for another member's
    folder over the channel's limits, or None to read it."""
    max_bytes, max_files = limits["max_mb"] * charter.MB, limits["max_files"]

    def skip(path, member):
        if member == me:
            return None
        text = charter.over(*charter.folder_total(path), max_bytes, max_files)
        return None if text is None else charter.skipped_note(member, text, pulled=False)

    return skip


def _watch(args, run):
    """vcharon watch C: a local member's watch of the channel folder, or a remote member's of
    its synced copy; the watcher's own exit codes."""
    if args.once:
        # each would change what one round means: a deadline, a pace, an error streak, and
        # --fresh a baseline, which would take what came as seen without printing it
        for flag, on in (("--until-change", args.until_change),
                         ("--max-minutes", args.max_minutes is not None),
                         ("--max-errors", args.max_errors is not None),
                         ("--every", args.every is not None), ("--fresh", args.fresh)):
            if on:
                raise _usage("--once doesn't go with %s" % flag, "leave out %s" % flag)
    if args.max_errors is not None and not args.until_change:
        raise _usage("--max-errors needs --until-change", "add --until-change, or leave out "
                     "--max-errors")
    # a config vcharon can't read refuses the watch, as it does every other command
    cfg = load_config()
    record, flags = _membership(args)
    # every start, a restart after EXIT updated too: the own MEMBER.md's vcharon: line follows
    # an update made without a rejoin (a leader never rejoins). Before the watchdog's start, so
    # nothing it reads counts as an import after the start
    try:
        tree, _ = _tree(cfg, record)
    except VCharonError:
        # a missing section: the watch refuses it below, with its own fix
        tree = None
    # before anything the watch writes (MEMBER.md's vcharon: line, the snapshot and its lock,
    # the watching line): output into the channel goes to every member (DESIGN, "The watcher
    # in a channel"). Here, in the watcher the user started, for both kinds of member: a
    # remote member's sync child writes to pipes of this process
    inside = None if tree is None else watch_mod.output_in_tree(tree)
    if inside is not None:
        shown = pathrules.printable(inside)
        # "if your redirect created it": a >> onto an entry file or MEMBER.md matches too, and
        # an agent follows a fix line as written
        fix = ("send the watcher's output to a file outside the channel; if your redirect "
               "created %s, delete it")
        hint = fix % ("`%s`" % shown)
        if platform.runnable(hint) != hint:
            # a file name holding " vcharon <verb>" would be respelled as a command
            hint = fix % "that file"
        raise _usage("the watcher's output goes to %s, a file in the channel: every member "
                     "gets that file" % shown, hint)
    # once, at the start, as a status line: it never counts as a change, and it comes after
    # the check above, so the home paths in it stay out of the channel. Before the watchdog's
    # start: reading the skill's text may import modules. Not for a --once check, which runs
    # between every two steps: join and every watcher start say it already
    note = None if args.once else skill.stale_note()
    if note is not None:
        watch_mod.say("%s %s" % (watch_mod.stamp(time.time()), pathrules.printable(note)))
    if tree is not None:
        channel_cmd.refresh_version(os.path.join(tree, record["name"]), record["name"])
    dog = run.watch_code(lambda line: watch_mod.say("%s %s" % (watch_mod.stamp(time.time()),
                                                               line)))
    channel_limits = channel_cmd.channel_limits(record)
    limits = {"fresh": args.fresh, "until_change": args.until_change,
              "max_minutes": args.max_minutes or (25 if args.until_change else None),
              "max_errors": args.max_errors or 10, "updated": dog.changed,
              "orphaned": lambda: run.orphaned("watch"), "once": args.once}
    if kinds.of(record) is not kinds.WORK:
        # the record's kind; a work channel's watch gets none, and reads its record
        limits["kind_"] = kinds.of(record)
    if record["ssh"] is None:
        if args.no_stream:
            raise _usage("--no-stream is for a remote member; you are a local member of %s"
                         % args.channel, "leave out --no-stream")
        return watch_mod.watch_dir(channel_cmd.local_root(record),
                                   record["name"], args.every or watch_mod.DIR_EVERY,
                                   folder_limits=(channel_limits["max_mb"] * charter.MB,
                                                  channel_limits["max_files"]), **limits)
    # a --once check runs one sync, never the streaming child, at --no-stream's pace (the pace
    # its last-watched stamp keeps)
    stream = not args.no_stream and not args.once
    if stream and args.every is not None and args.every > watch_mod.STREAM_EVERY_MAX:
        raise _usage("--every: 1 to %d seconds when streaming" % watch_mod.STREAM_EVERY_MAX,
                     "give a smaller --every, or add --no-stream")
    every = args.every or (watch_mod.STREAM_EVERY if stream else watch_mod.RUN_EVERY)
    return watch_mod.watch_job(_section(record), [args.channel] + flags, every, stream=stream,
                               **limits)


def _sync(args, run):
    """vcharon sync C: a remote member's section, its up and down jobs; --reset forgets one
    job's saved state."""
    if args.reset is not None:
        for flag, on in (("--repeat", args.repeat is not None), ("--full", args.full),
                         ("--dry-run", args.dry_run)):
            if on:
                raise _usage("--reset doesn't go with %s" % flag, "run the reset on its own, "
                             "then sync")
    cfg = load_config()
    record, flags = _membership(args)
    channel_cmd.channel_limits(record)
    if record["ssh"] is None:
        raise channel_cmd.channels.refused(
            "you are a local member of %s: your folder is in the channel itself, so there is "
            "nothing to sync" % args.channel,
            "vcharon read %s %s" % (args.channel, " ".join(flags)))
    section = _section(record)
    if args.reset == "up":
        _own_folder_intact(cfg, record)
    if args.reset is not None:
        return _reset("%s.%s" % (section, args.reset), args.verbose, run)
    return run_jobs([section], full=args.full, dry_run=args.dry_run, repeat=args.repeat,
                    verbose=args.verbose, run=run)


def _own_folder_intact(cfg, record):
    """--reset up's refusal while the own folder lacks MEMBER.md: forgotten, up's state can't
    guard the server's copy any more, and the next sync makes the folder again, empty; a
    rejoin brings the files back instead."""
    jobs = cfg.named(_section(record))
    if not jobs:
        return
    own = plugin.Ctx("local").resolve(jobs[0].mailbox.own_folder, "mailbox.local")
    if fsops.missing(os.path.join(own, entries.MEMBER_FILE)):
        raise channel_cmd.channels.refused(
            "your own folder %s has no %s: forgetting what up has sent would leave it so"
            % (own, entries.MEMBER_FILE),
            channel_cmd.rejoin_hint(record["channel"], record["ssh"], record["name"]))


def run_jobs(names, full=False, dry_run=False, repeat=None, verbose=False, run=None):
    """The jobs named, as vcharon sync runs a section's two (_run_job); its exit code. Errors
    are raised, for the caller's _guarded."""
    args = argparse.Namespace(job=list(names), full=full, dry_run=dry_run, repeat=repeat,
                              verbose=verbose)
    return _run_job(args, run if run is not None else _Run())


def sync_section(section, full=False, verbose=False):
    """A sync of the channel section, in this process, as vcharon sync would show it: join's
    and create's first sync, leave's last one. Its exit code."""
    run = _Run()
    return _guarded(lambda: run_jobs([section], full=full, verbose=verbose, run=run), run)


def _doctor(args, run):
    return doctor.main(args, run)


def _setup(args, run):
    """vcharon setup [--box NAME]: the config file made when missing; --box sets [vcharon]
    box in it. Prints the box this machine uses and where the config is. Never prompts."""
    wrote = config.setup_file(args.box)
    cfg = load_config()
    _say("vcharon: setup")
    _say("  config   %s%s" % (cfg.path, " (written)" if wrote else ""))
    _say("  box      %s" % cfg.box_text())
    if args.box is None and cfg.box is None:
        _say(platform.runnable("  note: to name this machine otherwise (laptop, a name each "
                               "of your machines has its own of): vcharon setup --box NAME"))
    if args.box is not None and channel_cmd.records():
        _say("  note: your channels keep the names you joined with")
    _say("OK")
    return 0


COMMANDS = {
    "setup": _setup,
    "key": lambda args, run: _key(args, run),
    "doctor": _doctor,
    "ping": lambda args, run: _ping(args, run),
    "list": channel_cmd.main, "create": channel_cmd.main, "join": channel_cmd.main,
    "leave": channel_cmd.main, "close": channel_cmd.main,
    "whoami": _whoami, "post": _post, "read": _read, "watch": _watch, "sync": _sync,
    "guide": lambda args, run: _guide(args), "skill": lambda args, run: _skill(args),
}


def _guide(args):
    """vcharon guide [TOPIC]: the text as it ships, on stdout."""
    text = guide.page(args.topic)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def _skill(args):
    """vcharon skill install [--claude] [--codex]: no flag means every agent."""
    agents = [a for a, _ in skill.AGENTS if getattr(args, a)] or [a for a, _ in skill.AGENTS]
    _say("vcharon: skill install")
    skill.install(agents, _say)
    _say("OK")
    return 0


def _update(args, run):
    """vcharon --update [--yes] [--force] [--json] [--rc] (DESIGN, "Self-update"): the
    latest release from GitHub, and this binary replaced with it once the user says yes. With
    --rc, or when this build is a pre-release, the newest pre-release counts too. A pipx,
    uv, pip or source install gets the command that updates it, and nothing changes. Exit 0
    when it's done, there is nothing newer, or the answer is no; 1 on every failure.

    --json prints one object on stdout, a failure's too: {"current", "install", "path"},
    then, once the release is read, {"latest", "tag", "prerelease", "rc", "update_available",
    "url", "changed", "confirmed"}, "prerelease" being whether that release is one and "rc"
    whether pre-releases were counted and why ("flag", "current" or false); "ok"; a failure's
    {"error", "message", "fix"}; a refusal for another install kind's "command"; an install's
    {"previous", "installed", "verified", "skills"}, "skills" being {"paths", "ok", "fix"}: the
    skill copies the new binary was run for, whether that worked, and the command to run when it
    didn't."""
    # Only here, not at the top: no other command needs the network modules, and this one is
    # short-lived; after its swap it imports nothing more.
    from . import update as update_mod

    as_json = args.update_json
    inst = install.detect()
    doc = {"current": VERSION, "install": inst.kind, "path": inst.path}

    def fail(kind, message, fix):
        fix = platform.runnable(fix)
        if as_json:
            _print_json(dict(doc, ok=False, error=kind, message=message, fix=fix))
        raise VCharonError("update", message, hint=fix)

    def guarded(fn):
        try:
            return fn()
        except update_mod.UpdateError as e:
            return fail(e.kind, e.message, e.fix)
        except VCharonError:
            raise
        except Exception as e:  # noqa: BLE001
            # --json's one object, even for a bug; the traceback goes to the log
            run.log_line("error", traceback.format_exc(), create=True)
            return fail("failed", "%s: %s" % (type(e).__name__, e),
                        "this is a bug in vcharon; see the log")

    # pre-releases count with --rc, and on a pre-release build: rc1 goes to rc2, then to the
    # final, with a plain --update; a final build reads full releases only
    rc = "flag" if args.update_rc else "current" if update_mod.is_prerelease(VERSION) else False
    release = guarded(lambda: update_mod.latest_release(pre=bool(rc)))
    available = update_mod.is_newer(release.tag, VERSION)
    doc.update(latest=release.version, tag=release.tag, prerelease=release.prerelease, rc=rc,
               update_available=available, url=release.url, changed=False, confirmed=False)
    if not as_json:
        _say("vcharon: update")
        _say("  current  %s (%s%s)" % (VERSION, inst.kind,
                                       ", %s" % inst.path if inst.path else ""))
        if rc == "current":
            _say("  note: this build is a pre-release, so pre-releases count too")
        _say("  latest   %s (%s%s)" % (release.version,
                                       "pre-release, " if release.prerelease else "",
                                       release.url))
    if not available and not args.update_force:
        same = update_mod.compare(VERSION, release.version) == 0
        if as_json:
            return _print_json(dict(doc, ok=True))
        # "ahead" when they differ: a build newer than the release isn't "the latest"
        what = "release or pre-release" if rc else "release"
        _say("  %s is the latest %s" % (VERSION, what) if same
             else "  %s is ahead of the latest %s" % (VERSION, what))
        _say("OK")
        return 0
    if not inst.self_updatable:
        doc["command"] = inst.command_for(release.tag)
        fail("not_self_updatable", "this vcharon was installed with %s, so vcharon --update "
             "can't replace it (only the standalone binary)" % inst.kind, doc["command"])
    # everything that makes it impossible, before the question
    guarded(lambda: update_mod.preflight(inst, release))
    older = update_mod.compare(release.version, VERSION) == -1
    if not _confirm_update(release, args.update_yes, as_json, args.update_force, available,
                           older, args.update_rc):
        if as_json:
            return _print_json(dict(doc, ok=True))
        return 0
    doc["confirmed"] = True
    # before the swap: what skill.installed reads (this build's skill text among it) is this
    # build's, and after the swap nothing is read or imported from it
    skills = [(agent, target) for agent, target, _ in skill.installed()]
    result = guarded(lambda: update_mod.apply_update(
        release, inst, on_step=None if as_json else lambda line: _say("  " + line)))
    doc.update(changed=True, previous=VERSION, installed=result.version, path=result.path,
               verified=result.verified)
    # the skill copies vcharon wrote, rewritten by the new binary: an agent reads the skill
    # first, and an old one may name what the new version changed
    why = update_mod.refresh_skills(result.path, [a for a, _ in skills]) if skills else None
    skill_fix = None
    if why:
        skill_fix = platform.runnable(skill.FIX % " ".join("--" + a for a, _ in skills))
    doc["skills"] = {"paths": [t for _, t in skills], "ok": why is None, "fix": skill_fix}
    if as_json:
        return _print_json(dict(doc, ok=True))
    _say("  updated %s -> %s: %s" % (VERSION, result.version, result.path))
    if not result.verified:
        _say("  note: the release has no checksum; the new binary passed its --version check")
    if why:
        _say("  note: the skill wasn't rewritten for %s (%s); run: %s"
             % (result.version, why, skill_fix))
    elif skills:
        _say("  skill rewritten by %s: %s" % (result.version, ", ".join(t for _, t in skills)))
    _say("  note: running watchers end with EXIT updated (exit %d); start them again"
         % install.EXIT_UPDATED)
    _say("OK")
    return 0


def _confirm_update(release, yes, as_json, force=False, available=True, older=False,
                    rc=False):
    """Whether to install: --yes is the one yes. --json and no terminal count as no (the
    release is reported, nothing installed), so an agent can't install by accident; so do
    Ctrl-C and Ctrl-D at the question. The only question vcharon asks besides vcharon key.
    force, available, older, rc: the line that says how keeps a --force and an --rc given, and
    a release that isn't newer (only --force gets here with one) is one to reinstall, or, older
    than this build, one that can be installed."""
    if yes:
        return True
    if as_json or not _stdin_is_terminal():
        if not as_json:
            # a binary's spelling: only a binary gets here
            command = "%s --update%s%s --yes" % (platform.self_command(),
                                                 " --rc" if rc else "",
                                                 " --force" if force else "")
            if available:
                _say("  %s is available; to install it: %s" % (release.version, command))
            elif older:
                _say("  %s can be installed: %s" % (release.version, command))
            else:
                _say("  %s can be reinstalled: %s" % (release.version, command))
        return False
    try:
        answer = input("Update now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if answer in ("y", "yes"):
        return True
    _say("  cancelled; nothing was changed")
    return False


class _Run:
    """What the error path needs to know about the run so far."""

    def __init__(self):
        # None until a command opens its log; a usage error writes no log.
        self.log = None
        # what a failed commit did, as the error block shows it; None if nothing
        self.done_line = None
        # the job whose error this is, in a sync of a section's two jobs: its console ERROR
        # line names it; None for one job and every other command
        self.job_name = None
        # where the error block goes: stderr, or with vcharon sync --repeat the round's lines,
        # which go to stdout
        self.out = None
        # a long-running command's check that its code didn't change (install.Watchdog), and
        # how it says EXIT updated; None for the others
        self.watchdog = None
        self.updated_line = None
        self.orphaned_line = None
        # a Windows binary's bootloader (install.exit_with_parent); None elsewhere
        self.parent = None

    def watch_code(self, say):
        """Starts the watchdog of a long-running command (watch, sync --repeat); say prints
        its EXIT updated and EXIT orphaned lines. Returns the watchdog."""
        # Before the start the watchdog marks: how fix lines spell vcharon (platform.runnable)
        # reads sysconfig's data when an entry point named vcharon is on PATH, and Python
        # imports that data at its first use (_sysconfigdata_*; _osx_support on macOS). A
        # long-running command imports nothing after its start (DESIGN, "Running watchers");
        # sysconfig keeps what it read, so later fix lines import nothing.
        platform.self_command()
        self.watchdog = install.Watchdog(parent=self.parent)
        self.updated_line = lambda: say(watch_mod.exit_line("updated", install.EXIT_UPDATED))
        self.orphaned_line = lambda: say(watch_mod.exit_line("orphaned", install.EXIT_ORPHANED))
        return self.watchdog

    def orphaned(self, what):
        """Whether the watchdog's binary lost its bootloader parent; logs one line when it
        did. what: the command, for the log."""
        if self.watchdog is None or not self.watchdog.orphaned():
            return False
        self.log_line("info", "%s: the process that started this one (pid %d) is gone; "
                      "exiting with %d" % (what, self.watchdog.parent.pid,
                                           install.EXIT_ORPHANED), create=True)
        return True

    def parent_gone(self, parent):
        """What a Windows binary does when its bootloader (parent, an install.Parent) is
        gone, from the thread that waits on it (install.exit_with_parent), before it exits 15:
        the log line, and in watch and sync --repeat their EXIT orphaned."""
        self.log_line("info", "the process that started this one (pid %d) is gone; exiting "
                      "with %d" % (parent.pid, install.EXIT_ORPHANED), create=True)
        if self.orphaned_line is not None:
            self.orphaned_line()

    def log_line(self, level, msg, create=False):
        if self.log is None and create:
            with contextlib.suppress(Exception):
                self.log = Log(os.path.join(platform.log_dir(), "vcharon.log"))
        if self.log is not None:
            self.log.write(level, msg)

    def show_error(self, err, logged=False):
        hint = err.hint
        if not hint and err.code == "internal":
            hint = "this is a bug in vcharon; see the log"
        if not logged:
            self.log_line("error", "%s: %s" % (err.code, err.message))
            if hint:
                self.log_line("error", "fix: %s" % hint)
            if err.detail:
                self.log_line("error", err.detail)
            if self.done_line:
                self.log_line("error", self.done_line.strip())
        # The log is the job's own, so only the console line names the job.
        lines = ["ERROR %s%s: %s" % ("%s: " % self.job_name if self.job_name else "", err.code,
                                     err.message)]
        lines += ["  | %s" % line for line in err.tail]
        if self.done_line:
            lines.append(self.done_line)
        if hint:
            # as this box runs vcharon; the log keeps the plain text, read later by a
            # person, maybe on another box
            lines.append("  fix: %s" % platform.runnable(hint))
        if self.log is not None:
            lines.append("  log: %s" % self.log.path)
        if self.out is not None:
            self.out.extend(lines)
            return
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()


def _say(line):
    print(line)
    # Show progress before the next slow step, even when stdout is a pipe.
    sys.stdout.flush()


def load_config():
    """config.load, and each skipped section's line in vcharon.log: never on stderr, whose
    first line a remote member's watcher takes for the round's error."""
    cfg = config.load()
    if cfg.skipped:
        log = Log(os.path.join(platform.log_dir(), "vcharon.log"))
        for skip in cfg.skipped:
            log.info(skip.line)
    return cfg


def _ping(args, run):
    started = time.monotonic()
    cfg = load_config()
    ssh.check_dest(args.dest)
    log = run.log = Log(os.path.join(platform.log_dir(), "vcharon.log"), console=args.verbose)
    log.info("vcharon %s ping %s; Python %s (%s) on %s; config %s%s"
             % (VERSION, args.dest, platform.python_version(), sys.executable,
                platform.os_name(), cfg.path, "" if cfg.exists else " (missing: defaults)"))
    _say("vcharon: ping %s" % args.dest)
    with ssh.Session(cfg.settings, args.dest, log) as session:
        hello = session.open()
        _say("  helper   vcharon %s, protocol %s, Python %s"
             % (hello.get("version"), hello.get("protocol"), hello.get("python")))
        _say("  server   %s, user %s, home %s"
             % (hello.get("distro") or hello.get("os"), hello.get("user"), hello.get("home")))
        warning = platform.distro_warning(hello)
        if warning is not None:
            # a warning, never a failure: other servers may work
            _say("  warn     %s" % warning)
        if hello.get("machine"):
            _say("  machine  %s" % hello["machine"])
        else:
            _say("  machine  none (jobs that keep state won't run)")
        _say("  connect  %.2f s  (ssh start to hello)" % session.handshake_seconds)
        if session.junk_bytes:
            _say("  warn     the server's shell printed %d bytes before vcharon started; see the "
                 "log" % session.junk_bytes)
        data = bytes(range(256)) + os.urandom(1 << 20)
        echo_started = time.monotonic()
        back = session.echo(data)
        seconds = time.monotonic() - echo_started
        if back != data:
            raise VCharonError("protocol", "the echo came back different: %d bytes sent, %d "
                               "received, first difference at byte %d"
                               % (len(data), len(back), _first_difference(data, back)),
                               hint="see the log; the connection isn't binary-safe")
        _say("  echo     %d bytes, byte-identical, %.2f s round trip" % (len(data), seconds))
    total = time.monotonic() - started
    log.info("ping OK in %.1f s" % total)
    _say("OK  (%.1f s)" % total)
    return 0


def _key(args, run):
    """vcharon key (DESIGN, "Keys without prompts"): the terminal first, then the arguments and the
    config, then keys.unlock."""
    if not keys.terminal():
        raise keys.needs_terminal()
    if args.dest is None and args.key_file is None:
        raise VCharonError("config", "give a destination, or --key FILE", hint=keys.KEY_EXAMPLE)
    if args.dest is not None:
        ssh.check_dest(args.dest)
    key_file = None
    if args.key_file is not None:
        key_file = os.path.abspath(os.path.expanduser(args.key_file))
        if not os.path.isfile(key_file):
            raise VCharonError("config", "%s isn't a file" % ssh.shown(key_file),
                               hint="check the path")
    cfg = load_config()
    log = run.log = Log(os.path.join(platform.log_dir(), "vcharon.log"), console=args.verbose)
    log.info("vcharon %s key %s%s; Python %s (%s) on %s; config %s%s"
             % (VERSION, args.dest or "", " --key %s" % ssh.shown(key_file) if key_file else "",
                platform.python_version(), sys.executable, platform.os_name(), cfg.path,
                "" if cfg.exists else " (missing: defaults)"))

    def say(line):
        _say(line)
        log.info(line)

    return keys.unlock(cfg.settings, args.dest, key_file, log, say)


def _first_difference(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


PULL_HINT = ("the vcharon on the server may be broken, or the server compromised; nothing was "
             "changed")


def pull_name(path):
    """The name that a pull of the remote path gives its top entry, from the text alone; None
    when the text can't tell (empty, ~, ~user, or a name that comes out "", "." or "..")."""
    if not path or (path.startswith("~") and "/" not in path):
        return None
    path = path.removeprefix("~/")
    name = posixpath.basename(posixpath.normpath(path))
    return None if name in ("", ".", "..") else name


def pull_guard(path):
    """An Engine after_plan for a one-off pull of the remote path (a channel's own folder, at
    join). The server's plan may only put what the pull asked for, under that one name, and
    never delete: a broken or hostile server can't make a pull write elsewhere in the local
    folder (scp's CVE-2019-6111). It runs before anything is checked or staged."""
    name = pull_name(path)

    def refuse(what):
        raise VCharonError("unsafe_path", "the server's plan %s" % what, PULL_HINT)

    def guard(p):
        entries = p.entries
        if not entries:
            return
        for e in entries:
            if e.op != "put":
                refuse("deletes %s, and a pull never deletes" % pathrules.show(e.path))
        top = entries[0].path
        if not top or "/" in top:
            refuse("starts with %s, not with one name" % proto.quote(top))
        if len(entries) > 1:
            if entries[0].kind != "dir":
                refuse("puts %s as a file, and more after it" % pathrules.show(top))
            for e in entries[1:]:
                if not e.path.startswith(top + "/"):
                    refuse("puts %s, outside %s" % (pathrules.show(e.path),
                                                    pathrules.show(top)))
        if name is not None and top != name:
            refuse("is for %s, but the pull asked for %s" % (pathrules.show(top),
                                                             pathrules.show(name)))
        if name is None and top.startswith("."):
            refuse("is for %s, a dot name that the pull's path doesn't spell out"
                   % pathrules.show(top))

    return guard


# the sync's sizes, and the limits' (charter)
size_text = charter.size_text


def _counted(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _listing(say, items):
    for item in items[:LIST_MAX]:
        say("          %s" % item)
    if len(items) > LIST_MAX:
        say("          … and %d more" % (len(items) - LIST_MAX))


def _summary(say, p, checked, dry_run, job=False, full=False):
    """The lines after the check, before any bytes move. job: a sync's form, which says
    "nothing to do" for an empty plan, and with full how many files the target holds."""
    puts = [e for e in p.entries if e.op == "put"]
    files = [e for e in puts if e.kind == "file"]
    deletes = [e for e in p.entries if e.op == "delete"]
    if job and not p.entries:
        say("  nothing to do")
    else:
        line = "  put     %s, %s (%s)" % (_counted(len(files), "file"),
                                          _counted(len(puts) - len(files), "dir"),
                                          size_text(sum(e.size for e in files)))
        if full:
            line += ", %d already there" % len(checked.have)
        say(line)
        if dry_run:
            _listing(say, [pathrules.show(e.path) + ("/" if e.kind == "dir" else "")
                           for e in puts])
        if deletes:
            say("  delete  %d" % checked.deletes)
            if dry_run:
                _listing(say, [pathrules.show(e.path) + ("/ (tree)" if e.tree else "")
                               for e in deletes])
    for note in list(p.notes) + list(checked.notes):
        say("  note    %s" % note)


def _commit_notes(say, eng, done):
    """The commit's notes, less those the check already showed: a directory the check found
    not empty is kept at the commit too, and would otherwise be noted twice."""
    shown = set(eng.checked.notes) if eng.checked is not None else set()
    for note in done.notes:
        if note not in shown:
            say("  note    %s" % note)


def _done_line(eng):
    """The error block's line for a failed commit that did something, or None."""
    if eng.done is None or eng.plan is None:
        return None
    written = engine.written_files(eng.plan, eng.done)
    if not written and not eng.done.deleted:
        return None
    return "  done    %d written, %d deleted before the failure" % (written, eng.done.deleted)


# --- the sync's jobs ---

# a membership whose record is here but whose section isn't: join writes the section
NO_SECTION_HINT = ("join the channel again, with the --project and --role you joined with: it "
                   "writes the section")
SAVE_HINT = "the files were written; fix that, then run again"


def _dumps(obj):
    """A saved or current identity or target, as a state_mismatch message shows it."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def side_text(job, side, role):
    """One side of a job as a sync shows it: <ssh>:<path> on the remote end, <path> on the
    local one, with the plugin unless it's path for a source or dir for a sink. A side with
    no path is its plugin's name alone."""
    if "path" not in side.options:
        return side.plugin
    path = side.options["path"]
    text = "%s:%s" % (job.ssh, path) if side.end == "remote" else path
    if side.plugin != ("path" if role == "source" else "dir"):
        text += " (%s)" % side.plugin
    return text


def _jobs_named(cfg, name):
    """The jobs a name runs: a job, or a mailbox section's two (DESIGN, "Channel sections")."""
    jobs = cfg.named(name)
    skip = cfg.skipped_for(name) if jobs is None else None
    if skip is not None:
        # its own file's error; every other job still runs
        raise skip.error
    if jobs is None:
        raise VCharonError("config", "no channel section %s in %s"
                           % (pathrules.show(name), config.channels_dir(cfg.path)),
                           hint=NO_SECTION_HINT)
    return jobs


@dataclasses.dataclass
class _JobRun:
    """One job of a sync, and how it ended."""

    job: config.Job
    log: Log
    shown: tuple          # the source's and the sink's side_text
    # its error block's side of _Run: its log, its done line, its first error line
    errors: _Run = dataclasses.field(default_factory=_Run)
    started: float = 0.0
    # "ok", "failed" or "skipped" once it has ended, with its exit code
    status: str = None
    code: int = 0
    ok_line: str = None
    # vcharon sync --repeat: the job's own log, while log is the round's HeldLog; and whether
    # the round wrote its state
    real_log: Log = None
    saved_state: bool = False

    @property
    def name(self):
        return self.job.name


class _Conn:
    """The connection a sync's jobs share: opened by the first job that needs it, closed
    after the last job, or as soon as it breaks."""

    def __init__(self):
        self.session = None
        # the job that opened it; the session's own lines go to its log
        self.opener = None
        # the server's machine id and OS, from the hello
        self.machine = None
        self.os = None
        # the error that broke it; the later jobs are then skipped
        self.broke = None
        # sync --repeat's install.Watchdog
        self.watchdog = None


def _run_job(args, run):
    """The jobs of a sync: the arguments, every job's config and plugins, the logs, every
    lock, then each job in the order given, all on one connection (a sync runs one section,
    whose jobs share a destination). One job prints and logs exactly as ever; several end
    with a summary line."""
    started = time.monotonic()
    if args.repeat is not None:
        _repeat_flags(args, run)
    cfg = load_config()
    jobs = [job for name in args.job for job in _jobs_named(cfg, name)]
    # Every usage, config, option and capability error fails before anything else: one bad
    # job, and none runs.
    for job in jobs:
        try:
            plugin.check_side(job.source.end, job.source.plugin, "source", job.source.options)
            plugin.check_side(job.sink.end, job.sink.plugin, "sink", job.sink.options)
        except VCharonError:
            # Its text names an option, not the job; with several jobs, say which.
            if len(jobs) > 1:
                run.job_name = job.name
            raise
    flags = ((" --full" if args.full else "") + (" --dry-run" if args.dry_run else "")
             + (" --repeat %d" % args.repeat if args.repeat is not None else ""))
    names = " ".join(job.name for job in jobs)
    todo = []
    for job in jobs:
        log = Log(os.path.join(platform.log_dir(), job.name + ".log"), console=args.verbose)
        log.info("vcharon %s sync %s%s; Python %s (%s) on %s; config %s"
                 % (VERSION, names, flags, platform.python_version(), sys.executable,
                    platform.os_name(), cfg.path))
        shown = (side_text(job, job.source, "source"), side_text(job, job.sink, "sink"))
        log.info("job %s: from %s %s, to %s %s"
                 % (job.name, job.from_text, shown[0], job.to_text, shown[1]))
        errors = _Run()
        errors.log = log
        # With several jobs, a job's console ERROR line names it; one job's is as ever.
        if len(jobs) > 1:
            errors.job_name = job.name
        todo.append(_JobRun(job, log, shown, errors, real_log=log))
    if args.repeat is not None:
        return _repeat(args, run, todo)
    locks = []
    try:
        # Every lock before the first connection, in the order given. They never block, so
        # the order can't deadlock; one busy job and none runs.
        for jr in todo:
            run.log = jr.log
            try:
                locks.append(state.lock(jr.name))
            except VCharonError as e:
                if e.code == "busy":
                    for other in todo:
                        if other is not jr:
                            other.log.info("not run: %s" % e.message)
                raise
        mark = started
        conn = _Conn()
        with contextlib.ExitStack() as stack:
            for i, jr in enumerate(todo):
                if conn.broke is not None:
                    _skip(jr, conn)
                    continue
                run.log = jr.log
                jr.started = mark
                _one_job(args, jr, conn, stack, last=i == len(todo) - 1)
                mark = time.monotonic()
    finally:
        for lock in locks:
            lock.release()
    code = next((jr.code for jr in todo if jr.code), 0)
    if len(todo) == 1:
        return code
    failed = sum(1 for jr in todo if jr.status == "failed")
    skipped = sum(1 for jr in todo if jr.status == "skipped")
    total = time.monotonic() - started
    if not failed and not skipped:
        line = "OK  %d jobs  (%.1f s)" % (len(todo), total)
    else:
        line = ("FAILED  %d of %d jobs failed%s  (%.1f s)"
                % (failed, len(todo), ", %d skipped" % skipped if skipped else "", total))
    _say(line)
    return code


def _skip(jr, conn):
    """A job whose connection broke under an earlier job: one line, and the exit code of the
    failure that broke it."""
    line = "vcharon: %s  skipped: the connection to %s broke" % (jr.name, jr.job.ssh)
    _say(line)
    jr.log.info(line)
    jr.status = "skipped"
    jr.code = conn.broke.exit_code if isinstance(conn.broke, VCharonError) else 1


# --- vcharon sync --repeat ---

# The opener's log gets one line this often, in seconds, while rounds have nothing to do.
REPEAT_SUMMARY = 600


def _repeat_flags(args, run):
    """--repeat's refusals of the flags it can't go with, before the config loads."""
    for flag, on in (("--full", args.full), ("--dry-run", args.dry_run)):
        if on:
            raise VCharonError("config", "--repeat doesn't go with %s" % flag,
                               hint="leave one of them out")


def _watch_stdin(ended):
    """Sets ended at end of file on stdin, on every OS: a thread that only reads, and drops
    what it reads. The watcher stops its child by closing the child's stdin. It reads the raw
    fd, never sys.stdin.buffer: a thread blocked in a BufferedReader holds its lock, and an
    exit while stdin is still open (a broken connection, a Ctrl-C) then aborts at interpreter
    shutdown ("Fatal Python error: _enter_buffered_busy"), on stderr, which the watcher would
    take for a new error."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        # no stdin at all (pythonw), or one without a descriptor: as good as its end
        ended.set()
        return

    def read():
        try:
            while os.read(fd, 65536):
                pass
        except OSError:
            # closed: as good as its end
            pass
        ended.set()

    threading.Thread(target=read, name="vcharon-stdin", daemon=True).start()


def _wait_or_end(ended, seconds):
    """Waits seconds, or until stdin ends; True if it ended. Short waits, so a Ctrl-C gets
    through on Windows too."""
    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if ended.is_set():
            return True
        if left <= 0:
            return False
        ended.wait(min(left, 0.5))


class _Tally:
    """The rounds since the opener's last summary line, and how many had nothing to do."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.since = time.monotonic()
        self.since_text = time.strftime("%Y-%m-%d %H:%M:%S")
        self.rounds = self.idle = 0


def _repeat(args, run, todo):
    """vcharon sync C --repeat SECONDS: one session, opened by the first round, kept for
    every round; rounds until stdin ends (exit 0, after the round under way) or a round's
    error breaks the connection (exit with that round's code). Between rounds it waits
    SECONDS from the end of a round."""
    dog = run.watchdog or run.watch_code(_say)
    ended = threading.Event()
    _watch_stdin(ended)
    conn = _Conn()
    conn.watchdog = dog
    tally = _Tally()
    run.log = todo[0].real_log
    with contextlib.ExitStack() as stack:
        while True:
            if dog.changed():
                # before any other work: the code that would do it may be another now
                run.log.info("repeat: vcharon changed under it; exiting with %d"
                             % install.EXIT_UPDATED)
                run.updated_line()
                return install.EXIT_UPDATED
            if run.orphaned("repeat"):
                run.orphaned_line()
                return install.EXIT_ORPHANED
            if conn.session is not None:
                conn.session.start_round()
            code, idle = _round(args, todo, conn, stack)
            opener = conn.opener.real_log if conn.opener is not None else todo[0].real_log
            if conn.session is not None:
                conn.session.end_round()
                # the round's logs are flushed or dropped: what the helper says between rounds
                # goes to the opener's own log
                conn.session.helper_log = opener
            run.log = opener
            tally.rounds += 1
            tally.idle += 1 if idle else 0
            if time.monotonic() - tally.since >= REPEAT_SUMMARY:
                opener.info("repeat: %d rounds since %s, nothing to do in %d"
                            % (tally.rounds, tally.since_text, tally.idle))
                tally.reset()
            if conn.broke is not None:
                # It never connects again by itself: the watcher starts it again.
                opener.info("repeat: the connection broke; exiting with %d" % code)
                return code
            if _wait_or_end(ended, args.repeat):
                opener.info("repeat: stdin ended; stopping after %d rounds since %s"
                            % (tally.rounds, tally.since_text))
                # the stack says bye and stops ssh
                return 0


def _round(args, todo, conn, stack):
    """One round of --repeat: each job once, in the order given, each under its lock taken
    for this job and round only (a busy job sits this round out). The round's lines go to
    stdout at its end: each failed job's error block, then ROUND <code>, the code a sync
    of the jobs would exit with (2 for a busy job when nothing failed). A job's log gets its
    lines only for a round that planned something, saved its state, failed or was busy.
    Returns (the code, whether every job ran and had nothing to do)."""
    out = []
    idle = True
    for jr in todo:
        jr.log = HeldLog(jr.real_log)
        jr.errors = _Run()
        jr.errors.log = jr.log
        jr.errors.out = out
        # named, as with several jobs: the watcher's error keys read the name
        jr.errors.job_name = jr.name
        jr.status, jr.code, jr.saved_state = None, 0, False
        try:
            planned = _repeat_one(args, jr, conn, stack)
        except BaseException:
            # a Ctrl-C: what the job logged so far stays
            jr.log.flush()
            raise
        if jr.status != "ok" or planned or jr.saved_state:
            jr.log.flush()
            idle = False
    busy = any(jr.status == "busy" for jr in todo)
    code = next((jr.code for jr in todo if jr.code and jr.status != "busy"), 2 if busy else 0)
    out.append("ROUND %d" % code)
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()
    return code, idle


def _repeat_one(args, jr, conn, stack):
    """One job of a --repeat round: its lock, then _run_one. Prints nothing for a good job;
    a failed one's error block goes to the round's lines. Returns whether its plan had
    entries."""
    if conn.broke is not None:
        line = "vcharon: %s  skipped: the connection to %s broke" % (jr.name, jr.job.ssh)
        jr.errors.out.append(line)
        jr.log.info(line)
        jr.status = "skipped"
        jr.code = conn.broke.exit_code if isinstance(conn.broke, VCharonError) else 1
        return False
    try:
        lock = state.lock(jr.name)
    except VCharonError as e:
        jr.code = e.exit_code
        if e.code == "busy":
            # the agent's own run of the job holds it: this round only
            jr.status = "busy"
            jr.log.info("not run this round: %s" % e.message)
        else:
            jr.status = "failed"
            # its path names the job (DESIGN, "Command line")
            jr.errors.job_name = None
            jr.errors.show_error(e)
        return False
    jr.started = time.monotonic()
    try:
        eng, done = _run_one(args, jr, conn, stack)
    except BrokenPipeError:
        # stdout's reader is gone: no job's error, the whole command's (_guarded)
        raise
    except Exception as e:
        if (not isinstance(e, VCharonError) and conn.watchdog is not None
                and conn.watchdog.after_crash()):
            # maybe a read of the swapped binary: _guarded's EXIT updated, not a round's error
            raise
        if conn.session is not None and not _still_usable(conn.session, e):
            conn.broke = e
        jr.status = "failed"
        if isinstance(e, VCharonError):
            jr.errors.show_error(e)
            jr.code = e.exit_code
        else:
            jr.errors.log_line("error", traceback.format_exc())
            jr.errors.show_error(VCharonError("internal", "%s: %s" % (type(e).__name__, e)),
                                 logged=True)
            jr.code = 1
        return True
    finally:
        lock.release()
    jr.status = "ok"
    _commit_notes(jr.log.info, eng, done)
    jr.log.info("OK  %d written, %d deleted  (%.1f s)"
                % (engine.written_files(eng.plan, done), done.deleted,
                   time.monotonic() - jr.started))
    return bool(eng.plan.entries)


def _still_usable(session, err):
    """Whether the next job may use the session after err: not after a protocol error, a
    kill or a lost connection (the helper may be out of step, or gone)."""
    if isinstance(err, VCharonError) and err.code in ("protocol", "timeout", "lost"):
        return False
    return session.usable


def _one_job(args, jr, conn, stack, last):
    """Runs one job, closes the connection when no later job will use it, and
    only then prints how the job ended, as a one-job run always has."""
    error = trace = None
    try:
        eng, done = _run_one(args, jr, conn, stack)
    except BrokenPipeError:
        # stdout's reader is gone: no job's error, the whole command's (_guarded)
        raise
    except Exception as e:  # noqa: BLE001
        error, trace = e, traceback.format_exc()
        if conn.session is not None and not _still_usable(conn.session, e):
            conn.broke = e
    if last or conn.broke is not None:
        # Says bye (when the helper is in step) and stops ssh.
        stack.close()
    if error is not None:
        jr.status = "failed"
        if isinstance(error, VCharonError):
            jr.errors.show_error(error)
            jr.code = error.exit_code
        else:
            jr.errors.log_line("error", trace)
            jr.errors.show_error(VCharonError("internal", "%s: %s" % (type(error).__name__, error)),
                                 logged=True)
            jr.code = 1
        return

    def say(line):
        _say(line)
        jr.log.info(line)

    jr.status = "ok"
    total = time.monotonic() - jr.started
    if done is None:
        jr.ok_line = "OK  dry run, nothing changed  (%.1f s)" % total
        say(jr.ok_line)
        return
    _commit_notes(say, eng, done)
    jr.ok_line = ("OK  %d written, %d deleted  (%.1f s)"
                  % (engine.written_files(eng.plan, done), done.deleted, total))
    say(jr.ok_line)


def _session(conn, stack, jr):
    """The sync's session: opened by the first job that needs it, whose log gets the
    session's own lines. A later job logs where they are, and starts with job.reset: the
    helper then holds nothing of the job before it."""
    if conn.session is None:
        # the job's own log, not a --repeat round's HeldLog: the session outlives the round
        conn.session = stack.enter_context(ssh.Session(jr.job.settings, jr.job.ssh,
                                                       jr.real_log or jr.log))
        conn.opener = jr
        conn.session.helper_log = jr.log
        # once per connection
        hello = conn.session.open()
        conn.machine = hello.get("machine")
        conn.os = hello.get("os")
        return conn.session
    jr.log.info("shares the connection of %s (run %s)"
                % (conn.opener.name, conn.opener.log.run_id))
    remote.reset(conn.session)
    # After the reset: what the helper says while it closes the job before stays in that
    # job's log; what it says from here on is this job's.
    conn.session.helper_log = jr.log
    return conn.session


def _own_folder(job, log, dry_run):
    """A mailbox job's folders on the client, <local> and <local>/<me>: up's path source
    never creates its own path. Only while up has sent nothing: a folder that vanished after
    that must not come back empty, or up's prune would empty the server's copy. Down checks it
    too, so that a run of down alone reports it as well; down itself never plans <me>/,
    so it wouldn't make the folder again. Nothing is made in a dry run."""
    path = plugin.Ctx("local").resolve(job.mailbox.own_folder, "mailbox.local")
    up = job.mailbox.section + ".up"
    try:
        saved = state.load(up)
    except VCharonError:
        # up's own run reports that
        saved = None
    sent = saved.source.get("sent") if saved is not None and isinstance(saved.source,
                                                                          dict) else None
    if os.path.isdir(path):
        # A channel member's folder always holds MEMBER.md. Gone while up has sent it,
        # the folder was emptied or replaced (a failed restore, a stray .DS_Store in a new
        # one): up's prune would delete the server's copy, so neither job runs.
        if (job.mailbox.channel and isinstance(sent, dict) and entries.MEMBER_FILE in sent
                and fsops.missing(os.path.join(path, entries.MEMBER_FILE))):
            raise VCharonError("not_found", "the own folder %s has no %s, which %s has sent: it "
                               "was emptied or replaced" % (path, entries.MEMBER_FILE, up),
                               channel_cmd.rejoin_hint(job.mailbox.channel, job.ssh,
                                                       job.mailbox.me)
                               + "; MEMBER.md is vcharon's: to drop other files, delete them "
                               "one by one and keep it")
        return
    if sent:
        # never a reset of up: its next sync would make the folder again, empty, and a rejoin
        # is what brings the files back
        raise VCharonError("not_found", "the mailbox's own folder %s is gone, but %s has sent "
                           "files from it" % (path, up),
                           channel_cmd.rejoin_hint(job.mailbox.channel, job.ssh,
                                                   job.mailbox.me))
    if dry_run:
        log.info("a real run would create %s, the mailbox's own folder" % path)
        return
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise fsops.error(e, path)
    log.info("created %s, the mailbox's own folder" % path)


# The general hints for a name the source can't send or list speak to the owner of the whole
# source; only a folder's writer can change a mailbox folder, so _mailbox_hint swaps them.
# Compared as text, since an error from the helper arrives with its hint as text.
SOURCE_HINTS = frozenset([pathrules.UNSAFE_HINT, pathrules.COLLISION_HINT,
                          path_plugin.LINKS_HINT, path_plugin.NAME_HINT])
MAILBOX_DOWN_HINT = ("the writer of each folder named above %s; your up still runs; more: "
                     "vcharon guide errors")
# with the writer's folder, since up's paths are relative to it
MAILBOX_UP_HINT = "%s in your own folder (%s/)"
# kept in channel_cmd: a local member's watch prints it too
CHANNEL_GONE_HINT = channel_cmd.CHANNEL_GONE_HINT


def channel_gone_hint(job, code, hint):
    """CHANNEL_GONE_HINT for a channel job's not_found on up's sink root or down's source
    root, else None; vcharon sync and vcharon doctor both swap it in."""
    if job.mailbox is None or code != "not_found":
        return None
    up = job.name == job.mailbox.section + ".up"
    if hint == (stage.ROOT_HINT if up else path_plugin.MISSING_HINT):
        return CHANNEL_GONE_HINT % (job.mailbox.channel,
                                    channel_cmd.name_flags(job.mailbox.channel, job.mailbox.me))
    return None


def _mailbox_hint(e, job):
    """A mailbox job's fix line for a name its source can't send or list: what a mailbox
    writer can do, in place of the general hint. Other jobs, and other errors, keep theirs."""
    if job.mailbox is None or not isinstance(e, VCharonError):
        return
    gone = channel_gone_hint(job, e.code, e.hint)
    if gone is not None:
        e.hint = gone
        return
    up = job.name == job.mailbox.section + ".up"
    if e.code in ("unsafe_path", "collision") and e.hint in SOURCE_HINTS:
        which = "one of them" if e.code == "collision" else "it"
        what = ("remove or rename %s" if up else "removes or renames %s") % which
    elif e.code == "permission" and e.hint == path_plugin.LIST_HINT:
        what = "fix its permissions" if up else "fixes its permissions"
    else:
        return
    e.hint = MAILBOX_UP_HINT % (what, job.mailbox.me) if up else MAILBOX_DOWN_HINT % what


def _mismatch(e, name):
    """The sync's hint for every state_mismatch that names no hint of its own."""
    if isinstance(e, VCharonError) and e.code == "state_mismatch":
        e.hint = state.reset_hint(name)


def _run_one(args, jr, conn, stack):
    """One job: its state, its header line, the sync's session, the engine, the state
    saved. Returns (engine, done); done is None for a dry run."""
    job, log, shown = jr.job, jr.log, jr.shown
    name = job.name
    hint = state.reset_hint(name)
    if job.mailbox is not None:
        _own_folder(job, log, args.dry_run)
    try:
        saved = state.load(name)
    except VCharonError as e:
        _mismatch(e, name)
        raise
    fingerprint = state.fingerprint(job)
    if saved is not None and saved.fingerprint != fingerprint:
        raise VCharonError("state_mismatch", state.config_changed(job), hint)

    # --repeat prints only a round's errors and its ROUND line; the log keeps the rest
    quiet = args.repeat is not None

    def say(line):
        if not quiet:
            _say(line)
        log.info(line)

    mode = [word for word, on in (("full", args.full), ("dry run", args.dry_run)) if on]
    say("vcharon: %s  %s -> %s%s" % (name, shown[0], shown[1],
                                     "  (%s)" % ", ".join(mode) if mode else ""))
    # the sink binding, once the check has passed
    bound = []
    session = _session(conn, stack, jr)
    machine = conn.machine
    # Every job has a remote end, and its state is tied to that server (DESIGN, "hello"), which
    # must be Linux.
    state.need_linux({"os": conn.os}, job.ssh, state.LINUX_HINT % job.ssh)
    if not machine:
        raise VCharonError("state_mismatch", "the server %s has no machine id, so vcharon can't "
                           "tie the state of %s to it" % (job.ssh, name),
                           state.NO_MACHINE_HINT)

    def after_plan(p):
        if saved is not None and p.identity != saved.identity:
            raise VCharonError("state_mismatch", "the state of %s was saved for another "
                               "source: %s, now %s"
                               % (name, _dumps(saved.identity), _dumps(p.identity)), hint)

    def after_check(p, checked):
        binding = {"end": job.sink.end, "root": checked.root}
        if job.sink.end == "remote":
            binding["machine"] = machine
        # So the devbox alias pointed at another server, or a moved root, can't make
        # prune delete on the wrong machine (DESIGN, "State file").
        if saved is not None and binding != saved.sink:
            raise VCharonError("state_mismatch", "the state of %s was saved for another "
                               "target: %s, now %s"
                               % (name, _dumps(saved.sink), _dumps(binding)), hint)
        bound.append(binding)
        _summary(say, p, checked, args.dry_run, job=True, full=args.full)

    down = job.mailbox is not None and name == job.mailbox.section + ".down"
    eng = engine.Engine(session, job.source, job.sink, log, after_check=after_check,
                        after_plan=after_plan, plan_args=_watch_args(down))
    # a first run, or a source that saved nothing: {}
    source_state = saved.source if saved is not None and saved.source is not None else {}
    try:
        done = eng.run(dry_run=args.dry_run, state=source_state, full=args.full)
    except Exception as e:
        _mismatch(e, name)
        _mailbox_hint(e, job)
        jr.errors.done_line = _done_line(eng)
        if bound and isinstance(eng.state_after, dict):
            _save_after_failure(name, fingerprint, eng, bound[0], log)
        raise
    new = (state.State(fingerprint, eng.plan.identity, bound[0], eng.plan.state, state.now())
           if done is not None else None)
    if new is not None and quiet and _same_state(saved, new):
        # --repeat: a round that changed nothing writes nothing, or every round would fsync
        # two files
        new = None
    if new is not None:
        try:
            state.save(name, new)
        except (OSError, VCharonError) as e:
            err = fsops.error(e, state.path(name)) if isinstance(e, OSError) else e
            raise VCharonError(err.code, "couldn't save the state of %s: %s"
                               % (name, err.message), SAVE_HINT)
        log.info("saved the state of %s" % name)
        jr.saved_state = True
    if done is not None and down:
        # the members this pull left out for their size: the watcher's WARN and read's note
        # come from there (with --repeat the notes reach only the log); after the state,
        # which holds those members' entries as they were
        try:
            charter.save_left_out(job.mailbox.section, eng.plan.notes,
                                  int(job.source.options["max_bytes"]),
                                  int(job.source.options["max_files"]))
        except OSError as e:
            log.warn("couldn't save the members left out: %s" % e)
    if down:
        _save_seen(job.mailbox.section, eng.plan_reply, log)
    return eng, done


def _watch_args(down):
    """A channel down's more source.plan args: {"watch": the pace} when a watcher started this
    sync (watch_mod.WATCH_ENV in its environment), so the server stamps the member's last
    watched time; else None. Only a watcher's pull counts: a by-hand sync or a join's pull
    would make a member look watched."""
    pace = os.environ.get(watch_mod.WATCH_ENV) if down else None
    if pace is None or channels.parse_pace(pace) is None:
        return None
    return {"watch": pace}


def _save_seen(section, reply, log):
    """Keeps the members' last-watched ages a down plan's reply brought, for whoami and
    read --json. Best effort: a failure, or the server's own (seen_error), is only logged, never
    printed: a watcher's rounds print nothing for it."""
    if "seen_error" in reply:
        log.warn("the server couldn't give the members' last-watched times: %s"
                 % pathrules.printable(str(reply["seen_error"])[:300]))
    if "seen" not in reply:
        return
    try:
        charter.save_seen(section, reply["seen"], time.time())
    except (OSError, TypeError, ValueError) as e:
        log.warn("couldn't keep the members' last-watched times: %s" % e)


def _same_state(saved, new):
    """Whether new holds what saved does, its saved stamp aside. Compared as JSON, as the
    file holds them: a tuple in a plan's state and the list it's read back as are the same."""
    if saved is None:
        return False

    def text(st):
        return json.dumps([st.fingerprint, st.identity, st.sink, st.source], sort_keys=True,
                          ensure_ascii=False)

    return text(saved) == text(new)


def _save_after_failure(name, fingerprint, eng, binding, log):
    """Saves what a commit that failed partway wrote (DESIGN, "The path source"). A failure here is
    only logged: it never replaces the run's own error."""
    try:
        state.save(name, state.State(fingerprint, eng.plan.identity, binding, eng.state_after,
                                     state.now()))
    except Exception as e:  # noqa: BLE001
        log.warn("couldn't save the state of %s after the failure: %s" % (name, e))
        return
    log.info("saved the state of %s: what the failed commit wrote" % name)


def _show_state(name, st, why=None):
    """What a reset prints first: a summary, since a mirror's state can hold 100,000 paths."""
    if why is not None:
        print("vcharon: the state of %s can't be read (%s)" % (name, why))
        return
    if st is None:
        print("vcharon: no state for %s" % name)
        return
    print("vcharon: state of %s  (%s)" % (name, state.path(name)))
    print("  saved     %s" % st.saved)
    print("  source    %s" % ("none" if st.identity is None else _dumps(st.identity)))
    print("  target    %s" % _dumps(st.sink))
    source = st.source
    if isinstance(source, dict) and set(source) == {"sent"} and isinstance(source["sent"], dict):
        values = list(source["sent"].values())
        files = sum(1 for v in values if isinstance(v, list))
        dirs = sum(1 for v in values if v == "d")
        print("  sent      %s, %s" % (_counted(files, "file"), _counted(dirs, "dir")))


def _reset(name, verbose, run):
    """vcharon sync C --reset up|down: forgets the job's saved state, under its lock, so its
    next sync plans from nothing (with --full, by content)."""
    log = run.log = Log(os.path.join(platform.log_dir(), name + ".log"), console=verbose)
    lock = state.lock(name)
    try:
        st, why = state.read(name)
        _show_state(name, st, why)
        p = state.path(name)
        if state.remove(name):
            print("removed %s" % p)
            log.info("state reset: removed %s" % p)
        else:
            log.info("state reset: there was no state at %s" % p)
    finally:
        lock.release()
    return 0
