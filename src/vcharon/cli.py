"""The command line (DESIGN, "Command line"): its verbs, their output and exit codes,
and dispatch to the sync runner."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import string
import sys
import time
import traceback

from . import (
    VERSION,
    channel_cmd,
    channels,
    charter,
    charter_state,
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
    skill,
    ssh,
    sync,
)

# `run` is the name of _Run objects here.
from . import kind as kinds
from .command import _guarded, _Run, _say, load_config
from .log import Log
from .mailbox import post as post_mod
from .mailbox import read as read_mod
from .mailbox import watch as watch_mod
from .plugins import dir as dir_plugin  # noqa: F401  (loaded at start: see below)
from .proto import VCharonError

# Importing this module loads the package, the plugins too though plugin.py loads them by
# name, and update.py alone is left for vcharon --update: a long-running command imports nothing
# after its start (DESIGN, "Running watchers").

EXIT_CODES = """exit codes: 0 ok, 1 refused or failed, 2 busy (a lock is held), 3 usage or config,
  4 couldn't connect or start the helper, 130 Ctrl-C, 143 SIGTERM (watch, sync --repeat).
  watch: 0 a change, 10 quiet (--max-minutes), 11 error, 12 another watcher (or a create,
  join, leave or close of the member) runs, 13 closed, 14 vcharon was updated (start it again),
  15 a binary's bootloader process was killed, 16 nothing new (--once), 17 stopped (--stop).
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
    # one line each, like --check's: the top help is one screen
    parser.add_argument("--update", action="store_true", help="update this binary from "
                        "GitHub releases, asking first")
    parser.add_argument("--yes", dest="update_yes", action="store_true",
                        help="with --update: install without asking")
    parser.add_argument("--force", dest="update_force", action="store_true",
                        help="with --update: install the latest even if it isn't newer")
    parser.add_argument("--json", dest="update_json", action="store_true",
                        help="with --update: print one JSON object; installs only with --yes")
    parser.add_argument("--rc", dest="update_rc", action="store_true",
                        help="with --update: count pre-releases too")
    parser.add_argument("--check", dest="update_check", action="store_true",
                        help="with --update: only say if a newer one is out")
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

    def shared(one):
        one.add_argument("--shared", action="store_true", help="the server's shared root "
                         "(/var/tmp/vcharon, Linux only), for people on separate accounts: any "
                         "account there can read, change or delete its channels; later "
                         "commands take it from the join record")

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
    one.add_argument("channels", metavar="C", nargs="*", help="with --remove-idle: only these "
                     "channels")
    where(one)
    as_json(one)
    one.add_argument("--remove-idle", action="store_true", help="your user's only, never an "
                     "agent's: list the folders of members idle over --days, and with --yes "
                     "remove them (never a leader's, nor this machine's own membership)")
    one.add_argument("--days", metavar="N", type=_number(1, channels.IDLE_DAYS_MAX, "days"),
                     help="with --remove-idle: idle over N days (default %d); 1 to %d"
                     % (channels.IDLE_DAYS, channels.IDLE_DAYS_MAX))
    one.add_argument("--yes", action="store_true", help="with --remove-idle: remove them; "
                     "without it nothing changes")
    root = one.add_mutually_exclusive_group()
    root.add_argument("--home", action="store_true", help="with --remove-idle: only the "
                      "channel root in the home folder (default: both roots)")
    root.add_argument("--shared", action="store_true", help="with --remove-idle: only the "
                      "server's shared root (/var/tmp/vcharon, Linux only)")
    ignored(one)
    def agent(one):
        one.add_argument("--agent", choices=platform.AGENTS, help="the agent you are, for "
                         "MEMBER.md and vcharon list (default: found from the environment, "
                         "else other)")

    one = verb("create", "create a channel; you lead it", "vcharon create myapp --server devbox")
    channel(one)
    where(one)
    shared(one)
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
    shared(one)
    ident(one)
    agent(one)
    one.add_argument("--rejoin", action="store_true", help="take an existing folder of your "
                     "name without a record here; only when the user says it's yours")
    one.add_argument("--takeover", action="store_true", help="with --rejoin: take the folder "
                     "though another machine's id holds it; only when the user confirms this "
                     "is that machine")
    one = verb("leave", "leave a channel (a member; a work channel's leader closes it)",
               "vcharon leave myapp")
    channel(one)
    ident(one)
    one = verb("close", "close and delete a work channel (its leader only)",
               "vcharon close myapp")
    channel(one)
    one.add_argument("--yes", action="store_true", help="the lobby only: close it, deleting "
                     "every member's folder; only on your user's word (ignored in a work "
                     "channel)")
    ident(one)
    one = verb("lead", "take the lead of a work channel, or (the leader) offer it",
               "vcharon lead myapp")
    channel(one)
    one.add_argument("--to", metavar="@NAME", help="the leader only: offer the lead to this "
                     "member, with a HANDOFF entry to it; it takes it with vcharon lead C")
    one.add_argument("--gone", action="store_true", help="take it though no offer names you: "
                     "only on your user's word, and only once the leader has been inactive %d "
                     "min" % (channels.GONE_AFTER // 60))
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
    one.add_argument("--presence", action="store_true", help="the lobby: also print the other "
                     "members' JOIN, REJOIN and LEAVE, each a change (for an agent "
                     "coordinating others)")
    one.add_argument("--max-errors", type=_number(1, 1000, "rounds"), metavar="N",
                     help="with --until-change: exit after this many failed rounds in a row "
                     "(default 10); a streaming watch counts %d s of failing as one"
                     % watch_mod.STREAM_ERROR_ROUND)
    one.add_argument("--stop", action="store_true", help="ask this membership's watcher on "
                     "this machine to end (EXIT stopped, 17), and wait up to %d s for it"
                     % watch_mod.STOP_TIMEOUT)
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

    one = verb("guide", "the guide topic index, or the topics named",
               "vcharon guide member rules")
    one.add_argument("topics", metavar="TOPIC", nargs="*", help="any of, printed in the order "
                     "given: %s" % ", ".join(guide.TOPICS))
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
UPDATE_FLAGS = ("--update", "--yes", "--force", "--json", "--rc", "--check")
# taken with --update and ignored, as by the verbs that act on no membership
UPDATE_IGNORED = ("--project", "--role")
# the verbs whose list of positionals may go on after a flag, and the list's dest
LISTS_AROUND_FLAGS = {"read": "ids", "guide": "topics", "list": "channels"}


def _main(argv, run):
    def command():
        words = sys.argv[1:] if argv is None else list(argv)
        words = _update_alone(words)
        parser = _parser()
        try:
            args, extra = parser.parse_known_args(words)
        except SystemExit as e:
            # --help, --version
            return e.code if isinstance(e.code, int) else 0
        if extra and args.command in LISTS_AROUND_FLAGS:
            # IDs, topics or channels on both sides of a flag (read C a#1 --full b#2, guide
            # lobby --role x rules): argparse takes only the first run of them into the list
            dest = LISTS_AROUND_FLAGS[args.command]
            setattr(args, dest, getattr(args, dest) + [w for w in extra if not w.startswith("-")])
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
    the verb's parser refuses it as unknown. With --update, returns the words without
    --project P and --role R, which it ignores: the guide has an agent pass them on every
    command; without it, the words as given."""
    def ignored(i):
        """How many words from i on are an ignored --project P or --role R: 0, 1 or 2."""
        word = words[i]
        if "=" in word and word.split("=", 1)[0] in UPDATE_IGNORED:
            return 1
        if word in UPDATE_IGNORED and i + 1 < len(words) and not words[i + 1].startswith("-"):
            return 2
        return 0

    stripped = []
    i = 0
    while i < len(words):
        skip = ignored(i)
        if not skip:
            stripped.append(words[i])
        i += skip or 1
    lead = []
    for word in stripped:
        if not word.startswith("-"):
            break
        lead.append(word)
    if "--update" not in lead:
        return words
    # The top parser has no --project or --role: left in, the value would read as the verb.
    if "-h" in lead or "--help" in lead:
        return stripped
    others = [w for w in stripped if w not in UPDATE_FLAGS]
    if others:
        raise _usage("--update runs on its own, with only --yes, --force, --json, --rc and "
                     "--check (--project P and --role R are ignored): not with %s"
                     % " ".join(others),
                     "run vcharon --update by itself; then the rest")
    return stripped


def _update_flags(args):
    """--yes, --force, --json, --rc and --check before a verb are --update's: without it,
    refused rather than ignored. --check, which never installs, refuses --yes and --force."""
    if args.update:
        if args.update_check:
            for flag, on in (("--yes", args.update_yes), ("--force", args.update_force)):
                if on:
                    raise _usage("--check never installs, so it doesn't go with %s" % flag,
                                 "leave out %s, or --check" % flag)
        return
    for flag, on in (("--yes", args.update_yes), ("--force", args.update_force),
                     ("--json", args.update_json), ("--rc", args.update_rc),
                     ("--check", args.update_check)):
        if not on:
            continue
        if flag == "--json" and args.command is not None:
            raise _usage("--json goes after the verb: vcharon %s ... --json" % args.command,
                         "move --json after %s" % args.command)
        raise _usage("%s goes only with --update" % flag, "leave out %s" % flag)


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
    # a work channel's by the charters' terms in the tree (the record's while it holds none):
    # a handoff moves the lead, and only the taker's record follows it
    chain = None if lobby else _chain(record, tree)
    return {"channel": record["channel"], "name": name, "project": record["project"],
            "role": record["role"], "leader": None if lobby else chain.leader,
            "term": None if lobby else chain.term_of(chain.leader),
            "founder": record["leader"] if lobby else None,
            "leads": not lobby and chain.leader == name,
            "mode": "local" if record["ssh"] is None else "remote", "server": record["ssh"],
            "shared": channel_cmd._is_shared(record),
            "folder": os.path.join(tree, name), "tree": tree, "kind": kind_.name}


def _chain(record, tree):
    """The work channel's charter.Chain of the tree on this box, the record's leader while it
    holds no charter (a remote member before its first pull)."""
    return charter.chain(tree, record["leader"])


def _whoami(args, run):
    """vcharon whoami [C] [--all] [--json]. With C, one object: {"channel", "name", "project",
    "role", "leader", "term", "founder", "leads", "mode", "server", "folder", "tree", "kind",
    "box", "box_source", "members"}: "kind" is "work" or "lobby"; a work channel's "leader" is
    the member that leads it by the charters' terms in the tree on this box, "term" its term
    (both null when none or two lead at the top term); a lobby's "leader" and "term" are null
    and "founder" the member whose folder holds its CHANNEL.md, who "leads" nothing; a work
    channel's "founder" is null;
    "mode" is "local" or "remote", "server" the alias (null for a local member), "shared" whether
    the channel is in the server's shared root (true) or the root in its home, "folder" your own
    folder on this box and "tree" the channel's (a remote member's copy); "role" is null
    without one. "box" is this machine's box now (a membership keeps the name it joined
    with), "box_source" "config" ([vcharon] box) or "os" (the default: mac, win, linux).
    With C only, "members": read_mod.member_list's of the tree but its "vcharon", null when
    it can't be read; in a lobby each with "presence" too (_presence), every "leader" false.
    The text of a lobby's lists only the members seen in 24 h, unless --all (_lobby_lines);
    the text with C names this project's other memberships of C on this box ("also").
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
        members, unread = _members_of(doc["tree"], None if lobby else doc["leader"], seen,
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
        # two agents can work in one folder (one with --role): a plain whoami shows the
        # role-less membership, so the others are named here, each with the flags that find it
        for other in channel_cmd.records(args.channel, skip_unreadable=True):
            if (other["project"] == record["project"]
                    and (other["role"] or None) != (record["role"] or None)):
                _say("  also     %s  (%s): this project's other membership of %s here"
                     % (other["name"], channel_cmd.record_flags(other), args.channel))
        _say("  box      %s" % platform.tilde_text(cfg.box_text()))
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
    _say("  box      %s" % platform.tilde_text(cfg.box_text()))
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
    member's as its last pull brought them (charter_state.load_seen), a local member's from the
    stamps beside the channel root; {} for none, or when they can't be read (they only add
    to whoami and read --json)."""
    if record["ssh"] is not None:
        return charter_state.load_seen(_section(record))
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
        _say("  members  can't read %s: %s" % (platform.tilde(doc["tree"]), unread))
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
    out. A remote member's from the copy and the ages its pulls keep (charter_state.save_seen: the
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
        _say("  channel  %s, led by %s%s%s" % (
            doc["channel"], doc["leader"] or "no one (two at the top term: ask your user)",
            " (you)" if doc["leads"] else "",
            ", term %d" % doc["term"] if doc["term"] and doc["term"] > 1 else ""))
    _say("  name     %s  (--project %s%s)" % (doc["name"], doc["project"] or "?",
                                              " --role %s" % doc["role"] if doc["role"] else ""))
    _say("  mode     %s%s" % ("local, on this machine" if doc["mode"] == "local"
                              else "remote, server %s" % doc["server"],
                              ", its shared root" if doc["shared"] else ""))
    # the home as ~ in the line, as agents quote it; --json's folder stays whole
    _say("  folder   %s" % platform.tilde(doc["folder"]))


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
    tree, synced = _tree(cfg, record)
    leader = _chain(record, tree).leader if steps else None
    if steps and leader != name:
        # --steps, or --file STEPS.md at the top of the own folder: the leader's by the
        # charters' terms, so a former leader's plan stays as it was
        raise channel_cmd.channels.refused(
            "%s is the leader's (%s): its plan" % (post_mod.STEPS_FILE, leader),
            "leave out %s: your entries go into %s"
            % ("--steps" if args.steps else "--file %s" % file, post_mod.RESULTS_FILE))
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
    its own, whose own entry in hosts matches it); never at a server:~ (platform.SERVER_HOME),
    vcharon's own word for the server's home."""
    found = set()
    for host in hosts:
        pattern = r"(?<![\w-])(?<![\w-]\.)%s(?![\w-])(?!\.[\w-])" % re.escape(host)
        # ASCII: a name written next to Chinese text (no space between) is still a word
        for m in re.finditer(pattern, text, re.IGNORECASE | re.ASCII):
            # vcharon's own server:~ (a path under the server's home, quoted from its lines)
            # names no host, even for an alias spelled "server"
            if not text.startswith(platform.SERVER_HOME, m.start()):
                found.add(host)
                break
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
        code = _guarded(lambda: sync.run_jobs([job], run=run), run)
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


# a number alone (19, #19): every member numbers its own entries, so it names no entry
# (capped: a longer one is no entry's number, and gets the usual fix line)
_BARE_NUMBER = re.compile(r"\A#?([1-9][0-9]{0,17})\Z")


def _read_ids(args):
    """The IDs read was given, each once, in order, a leading @ dropped (post's --re takes it
    too, by habit); a usage error for one that isn't an ID, or with --last or --to-me."""
    ids = []
    for arg in args.ids:
        one = arg.removeprefix("@")
        if entries.parse_id(one) is None:
            hint = "give each ID as <name>#<n>"
            number = _BARE_NUMBER.match(one)
            same = _same_number(args, int(number.group(1))) if number else None
            if same:
                hint += "; with that number: %s" % ", ".join(same)
            else:
                hint += ", as the watcher's line prints it"
                if same is not None:
                    # a number read found in no entry: without this the fix line reads as if
                    # only the spelling were wrong
                    hint = ("no entry read would show has number %d; " % int(number.group(1))
                            + hint)
            raise _usage("%s isn't an entry's ID (<name>#<n>)" % pathrules.show(arg), hint)
        if one not in ids:
            ids.append(one)
    if ids and (args.last or args.to_me):
        flag = "--last" if args.last else "--to-me"
        raise _usage("%s goes with the whole list, not with IDs" % flag,
                     "leave out %s, or the IDs" % flag)
    return ids


def _same_number(args, number):
    """The IDs numbered number in the channel's tree on this box, as read would read it, for
    the fix line of a number given alone; None when the membership or the tree can't be read
    (then nothing is known about that number)."""
    try:
        cfg = load_config()
        record, _ = _membership(args)
        tree, synced = _tree(cfg, record)
        skip = (None if synced
                else _over_limit(channel_cmd.channel_limits(record), record["name"]))
        return read_mod.ids_numbered(tree, number, skip)
    except (VCharonError, OSError):
        return None


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
    notes = charter_state.left_out_notes(_section(record)) if synced else []
    lobby = kinds.of(record) is kinds.LOBBY
    chain = _chain(record, tree)
    mine = (record["name"], chain, kinds.of(record)) if args.to_me else None
    # only the reading is in the try: a closed stdout is _guarded's to handle
    try:
        if args.json:
            doc = read_mod.view_json(tree, args.channel, synced=synced, full=args.full,
                                     last=args.last, skip=skip, notes=notes, ids=ids,
                                     mine=mine, seen=_watched(record, tree),
                                     lead=(None, None) if lobby
                                     else (chain.leader, chain.term_of(chain.leader)))
            missing = doc["missing"]
        else:
            last = ["--last", str(args.last)] if args.last else []
            to_me = ["--to-me"] if args.to_me else []
            bodies = platform.runnable(READ_FULL_HINT % (args.channel,
                                                         " ".join(to_me + last + flags)))
            # the versions note once per join, and again when it changes: on every read it
            # would repeat what the reader was told. --json has it each time
            section = _section(record)
            shown, missing = read_mod.view(
                tree, args.channel, synced=synced, full=args.full, last=args.last, skip=skip,
                notes=notes, bodies=bodies, ids=ids, mine=mine,
                versions=lambda note: charter_state.version_note_new(section, note))
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
    its synced copy; the watcher's own exit codes. --stop: _stop_watcher."""
    if args.stop:
        return _stop_watcher(args)
    # every line the watch prints goes through out, so a later Ctrl-C knows whether an EXIT
    # line came already
    out = run.noting_exit(watch_mod.say, len(watch_mod.stamp(time.time())) + 1)

    def say(line):
        out("%s %s" % (watch_mod.stamp(time.time()), line))

    with run.stoppable(say):
        return _watch_member(args, run, say, out)


def _stop_watcher(args):
    """vcharon watch C --stop: asks the membership's watcher on this machine to end, through a
    request file beside its lock (watch.ask_stop), and waits for its lock to come free. 0 when
    none runs or it stopped; refused (exit 1) when the lock is still held after
    watch.STOP_TIMEOUT, with the request removed. Not a watcher: no EXIT line of its own."""
    # each is a watcher's own: a stop starts no watcher
    for flag, on in (("--until-change", args.until_change),
                     ("--max-minutes", args.max_minutes is not None),
                     ("--max-errors", args.max_errors is not None),
                     ("--every", args.every is not None), ("--fresh", args.fresh),
                     ("--once", args.once), ("--no-stream", args.no_stream),
                     ("--presence", args.presence)):
        if on:
            raise _usage("--stop doesn't go with %s" % flag, "leave out %s" % flag)
    record, flags = _membership(args)
    name, channel = record["name"], args.channel
    snapshot = channel_cmd.record_snapshot(record)

    def asked(pid):
        # before the wait, so a stop that takes seconds isn't silent
        print("asked the watcher of %s in %s to stop; waiting up to %d s"
              % (name, channel, watch_mod.STOP_TIMEOUT), flush=True)

    got = watch_mod.ask_stop(snapshot, asked=asked)
    if got is None:
        print("no watcher of %s in %s runs on this machine" % (name, channel))
        return 0
    pid, stopped, taken = got
    # the PID its lock named, when it wrote one (DESIGN, "Lock")
    held_by = " (PID %d)" % pid if pid else ""
    if stopped:
        # its lock is free either way; only a watcher that took the request was stopped by it
        print("the watcher of %s in %s stopped%s" % (name, channel, held_by) if taken
              else watch_mod.STOP_NOT_TAKEN + held_by)
        return 0
    raise channel_cmd.channels.refused(
        "the watcher of %s in %s didn't stop within %d s (%s.lock is still held)%s"
        % (name, channel, watch_mod.STOP_TIMEOUT, snapshot, held_by),
        watch_mod.STOP_FIX % (channel, " ".join(flags)))


def _watch_member(args, run, say, out):
    """_watch's work; say prints a line with the time in front, as every watcher line; out,
    watch_dir's and watch_job's, a line that has it already."""
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
    if args.presence and kinds.of(record) is kinds.WORK:
        # a usage error, as --steps in a lobby: the flag has no use in this kind of channel
        raise _usage("--presence is for the lobby",
                     "a work channel's JOIN and LEAVE already reach its leader; leave out "
                     "--presence")
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
        if kinds.of(record) is kinds.WORK:
            # MEMBER.md's leader: follows a handoff the same way, by the charters' terms
            channel_cmd.refresh_leader(os.path.join(tree, record["name"]), record["name"],
                                       _chain(record, tree).leader)
    dog = run.watch_code(say)
    channel_limits = channel_cmd.channel_limits(record)
    limits = {"fresh": args.fresh, "until_change": args.until_change,
              "max_minutes": args.max_minutes or (25 if args.until_change else None),
              "max_errors": args.max_errors or 10, "updated": dog.changed,
              "orphaned": lambda: run.orphaned("watch"), "once": args.once}
    if kinds.of(record) is not kinds.WORK:
        # the record's kind; a work channel's watch gets none, and reads its record
        limits["kind_"] = kinds.of(record)
    if args.presence:
        limits["presence"] = True
    if record["ssh"] is None:
        if args.no_stream:
            raise _usage("--no-stream is for a remote member; you are a local member of %s"
                         % args.channel, "leave out --no-stream")
        return watch_mod.watch_dir(channel_cmd.local_root(record),
                                   record["name"], args.every or watch_mod.DIR_EVERY, out=out,
                                   folder_limits=(channel_limits["max_mb"] * charter.MB,
                                                  channel_limits["max_files"]), **limits)
    # a --once check runs one sync, never the streaming child, at --no-stream's pace (the pace
    # its last-watched stamp keeps)
    stream = not args.no_stream and not args.once
    if stream and args.every is not None and args.every > watch_mod.STREAM_EVERY_MAX:
        raise _usage("--every: 1 to %d seconds when streaming" % watch_mod.STREAM_EVERY_MAX,
                     "give a smaller --every, or add --no-stream")
    every = args.every or (watch_mod.STREAM_EVERY if stream else watch_mod.RUN_EVERY)
    return watch_mod.watch_job(_section(record), [args.channel] + flags, every, out=out,
                               stream=stream,
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
        return sync._reset("%s.%s" % (section, args.reset), args.verbose, run)
    return sync.run_jobs([section], full=args.full, dry_run=args.dry_run, repeat=args.repeat,
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


def _doctor(args, run):
    return doctor.main(args, run)


def _setup(args, run):
    """vcharon setup [--box NAME]: the config file made when missing; --box sets [vcharon]
    box in it. Prints the box this machine uses and where the config is. Never prompts."""
    wrote = config.setup_file(args.box)
    cfg = load_config()
    _say("vcharon: setup")
    _say("  config   %s%s" % (platform.tilde(cfg.path), " (written)" if wrote else ""))
    _say("  box      %s" % platform.tilde_text(cfg.box_text()))
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
    "leave": channel_cmd.main, "close": channel_cmd.main, "lead": channel_cmd.main,
    "whoami": _whoami, "post": _post, "read": _read, "watch": _watch, "sync": _sync,
    "guide": lambda args, run: _guide(args), "skill": lambda args, run: _skill(args),
}


def _guide(args):
    """vcharon guide [TOPIC ...]: the text as it ships, on stdout."""
    text = guide.page(args.topics)
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
    """vcharon --update [--yes] [--force] [--json] [--rc] [--check] (DESIGN, "Self-update"):
    the latest release from GitHub, and this binary replaced with it once the user says yes.
    With --rc, or when this build is a pre-release, the newest pre-release counts too. A pipx,
    uv, pip or source install gets the command that updates it, and nothing changes. Exit 0
    when it's done, there is nothing newer, or the answer is no; 1 on every failure. --check
    stops once the release is read: it reports, never asks, never installs, exit 0.

    --json prints one object on stdout, a failure's too: {"current", "install", "path", "rc",
    "update_available"}, "rc" being whether pre-releases are counted and why ("flag",
    "current" or false), "update_available" null until the release is read; then {"latest",
    "tag", "prerelease", "url", "changed", "confirmed"}, "prerelease" being whether that
    release is one, and "update_available" true or false; "ok"; a failure's
    {"error", "message", "fix"}; a refusal for another install kind's "command"; an install's
    {"previous", "installed", "verified", "skills"}, "skills" being {"paths", "ok", "fix"}: the
    skill copies the new binary was run for, whether that worked, and the command to run when it
    didn't."""
    # Only here, not at the top: no other command needs the network modules, and this one is
    # short-lived; after its swap it imports nothing more.
    from . import update as update_mod

    as_json = args.update_json
    inst = install.detect()
    # pre-releases count with --rc, and on a pre-release build: rc1 goes to rc2, then to the
    # final, with a plain --update; a final build reads full releases only
    rc = "flag" if args.update_rc else "current" if update_mod.is_prerelease(VERSION) else False
    # rc is known before the read, so a failed read's object has it too
    doc = {"current": VERSION, "install": inst.kind, "path": inst.path, "rc": rc,
           "update_available": None}

    def fail(kind, message, fix):
        # --json's fix whole, the printed one as shown (proto.Hint)
        fix = proto.Hint(platform.runnable(fix), platform.runnable(proto.shown_hint(fix)))
        if as_json:
            _print_json(dict(doc, ok=False, error=kind, message=message, fix=str(fix)))
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

    release = guarded(lambda: update_mod.latest_release(pre=bool(rc)))
    available = update_mod.is_newer(release.tag, VERSION)
    doc.update(latest=release.version, tag=release.tag, prerelease=release.prerelease,
               update_available=available, url=release.url, changed=False, confirmed=False)
    if not as_json:
        _say("vcharon: update")
        _say("  current  %s (%s%s)" % (VERSION, inst.kind,
                                       ", %s" % platform.tilde(inst.path) if inst.path else ""))
        if rc == "current":
            _say("  note: this build is a pre-release, so pre-releases count too")
        _say("  latest   %s (%s%s)" % (release.version,
                                       "pre-release, " if release.prerelease else "",
                                       release.url))
    if args.update_check:
        # the read is all: an agent may run this without its user's word
        return _update_check(doc, release, inst, available, rc, as_json,
                             update_mod.compare(VERSION, release.version) == 0)
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
    _say("  updated %s -> %s: %s" % (VERSION, result.version, platform.tilde(result.path)))
    if not result.verified:
        _say("  note: the release has no checksum; the new binary passed its --version check")
    if why:
        _say("  note: the skill wasn't rewritten for %s (%s); run: %s"
             % (result.version, platform.tilde_text(why), skill_fix))
    elif skills:
        _say("  skill rewritten by %s: %s"
             % (result.version, ", ".join(platform.tilde(t) for _, t in skills)))
    _say("  note: running watchers end with EXIT updated (exit %d); start them again"
         % install.EXIT_UPDATED)
    _say("OK")
    return 0


def _update_check(doc, release, inst, available, rc, as_json, same):
    """--update --check's end, once the release is read: whether it is newer, and the command
    that would install it (another install kind's own, as "command" in --json too). same:
    whether the release is this version. Exit 0."""
    if as_json:
        if available and not inst.self_updatable:
            doc["command"] = inst.command_for(release.tag)
        return _print_json(dict(doc, ok=True))
    what = "release or pre-release" if rc else "release"
    if available:
        # a binary's spelling for a binary, as the question's line has it
        command = ("%s --update%s" % (platform.self_command(), " --rc" if rc == "flag" else "")
                   if inst.self_updatable else inst.command_for(release.tag))
        # the install is the user's to decide (the guide's rule), so the line says whose
        _say("  %s is newer; your user can install it: %s" % (release.version, command))
    elif same:
        _say("  %s is the latest %s" % (VERSION, what))
    else:
        _say("  %s is ahead of the latest %s" % (VERSION, what))
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
            # hashed, as doctor shows this machine's on the server
            _say("  machine  id %s (hashed)" % platform.shown_machine(hello["machine"]))
        else:
            _say("  machine  none (jobs that keep state won't run)")
        _say("  connect  %.2f s  (ssh start to hello, bundle %s)"
             % (session.handshake_seconds, "sent" if session.bundle_sent else "cached"))
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
