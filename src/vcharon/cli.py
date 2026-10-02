"""The command line (DESIGN §13): its verbs, their output and exit codes, and the sync engine's
runner behind vcharon sync."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import posixpath
import string
import sys
import threading
import time
import traceback

from . import (VERSION, channel_cmd, config, doctor, entries, fsops, keys, pathrules,
               platform, plugin, proto, remote, ssh, stage, state)
# `run` is the name of _Run objects here.
from . import run as engine
from .log import HeldLog, Log
from .mailbox import post as post_mod
from .mailbox import read as read_mod
from .mailbox import watch as watch_mod
from .plugins import path as path_plugin
from .proto import VCharonError

# A dry run's listing shows this many paths, then "… and N more".
LIST_MAX = 50

EXIT_CODES = """exit codes: 0 ok, 1 refused or failed, 2 busy (a lock is held), 3 usage or config,
  4 couldn't connect or start the helper, 130 Ctrl-C.
  watch: 0 a change, 10 quiet (--max-minutes), 11 error, 12 another watcher runs,
  13 the channel is closed (14 is kept for "updated").
Every refusal ends with a fix: line, a command to run or one line of text."""

DESCRIPTION = """File-based channels for AI agents, on one machine or across machines over plain
SSH. C is a channel's name. Your member name comes from this box, the project (the folder that
holds .git, or --project) and --role; after a join, from your join record."""


class _Parser(argparse.ArgumentParser):
    # argparse exits with 2 on a usage error, and 2 means busy; ours is a usage error (3).
    def error(self, message):
        raise VCharonError("config", message, hint="vcharon %s --help"
                           % self.prog.partition(" ")[2] if " " in self.prog else
                           "vcharon --help")


# --repeat's limit: the watcher's --every for a streaming watch is 1 to 300 too (M15).
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
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    commands.required = True

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
                         "the folder that holds .git, from the current directory")
        one.add_argument("--role", metavar="R", help="your name's last part, 1 to 6 of a-z0-9: "
                         "a second session in the same project on this box passes one")

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
    key = verb("key", "unlock an ssh key into this OS's agent or keychain, so runs stop failing "
               "after a reboot", "vcharon key dev")
    key.add_argument("dest", metavar="ALIAS", nargs="?",
                     help="the server whose key to unlock, and test")
    key.add_argument("--key", metavar="FILE", dest="key_file",
                     help="the private key file to unlock, instead of the one ssh finds")
    doc = verb("doctor", "check this machine, the servers of your channels and their syncs; "
               "changes nothing", "vcharon doctor --server dev")
    doc.add_argument("--server", metavar="ALIAS", help="check this server only")
    as_json(doc)
    ping = verb("ping", "connect, echo 1 MiB, check the server's Python", "vcharon ping dev")
    ping.add_argument("dest", metavar="ALIAS", help="an ~/.ssh/config alias, user@host, or "
                      "ssh://user@host:port")

    one = verb("list", "list a server's channels, their leaders and members",
               "vcharon list --server dev")
    where(one)
    as_json(one)
    def agent(one):
        one.add_argument("--agent", choices=platform.AGENTS, help="the agent you are, for "
                         "MEMBER.md and vcharon list (default: found from the environment, "
                         "else other)")

    one = verb("create", "create a channel; you lead it", "vcharon create game --server dev")
    channel(one)
    where(one)
    ident(one)
    agent(one)
    one = verb("join", "join a channel as a member", "vcharon join game --server dev")
    channel(one)
    where(one)
    ident(one)
    agent(one)
    one.add_argument("--rejoin", action="store_true", help="take an existing folder of your "
                     "name without a record here; only when the user says it's yours")
    one.add_argument("--takeover", action="store_true", help="with --rejoin: take the folder "
                     "though another machine's id holds it; only when the user confirms this "
                     "is that machine")
    one = verb("leave", "leave a channel (a member; the leader closes it)", "vcharon leave game")
    channel(one)
    ident(one)
    one = verb("close", "close and delete a channel (its leader only)", "vcharon close game")
    channel(one)
    ident(one)
    one = verb("whoami", "your name, folder, local or remote, server; without C, every "
               "channel you joined from this project", "vcharon whoami game --json")
    one.add_argument("channel", metavar="C", nargs="?", help="the channel's name")
    ident(one)
    as_json(one)

    one = verb("post", "post an entry to a .md file in your own folder, RESULTS.md unless "
               "--file names another",
               "vcharon post game --to @mac-web --title \"step 2 done\" --body \"tests pass\"")
    channel(one)
    one.add_argument("--to", nargs="+", required=True, metavar="@NAME",
                     help="@<name> ..., or @all (the leader only); one argument or several")
    one.add_argument("--title", required=True, help="the entry's title, one line")
    one.add_argument("--re", metavar="NAME#N", help="the entry this answers")
    one.add_argument("--body", metavar="TEXT", help="the body; without it, stdin is the body. "
                     "A body line that starts like a Markdown heading gets '> ' in front")
    one.add_argument("--file", metavar="NAME.md", help="the .md file in your own folder, a "
                     "subfolder's with a / (default: RESULTS.md)")
    one.add_argument("--steps", action="store_true", help="the leader's plan: --file STEPS.md, "
                     "the leader only")
    ident(one)
    one = verb("read", "every member's entries, in one order", "vcharon read game --last 20")
    channel(one)
    one.add_argument("--last", type=_number(1, 1000000, "entries"), metavar="N",
                     help="only the newest N entries")
    one.add_argument("--full", action="store_true", help="each entry's other header lines and "
                     "its body too")
    ident(one)
    as_json(one)
    one = verb("watch", "print what reaches you, one line per entry",
               "vcharon watch game --until-change")
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
    one.add_argument("--no-stream", action="store_true", help="a remote member: a sync each "
                     "round, in place of one long-lived vcharon sync --repeat")
    one.add_argument("--max-errors", type=_number(1, 1000, "rounds"), metavar="N",
                     help="with --until-change: exit after this many failed rounds in a row "
                     "(default 10); a streaming watch counts %d s of failing as one"
                     % watch_mod.STREAM_ERROR_ROUND)
    ident(one)
    one = verb("sync", "a remote member: push your folder, pull the others'",
               "vcharon sync game --full")
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
    return parser


def _utf8_console():
    """Launch rules (DESIGN §13): on Windows, stdout and stderr write UTF-8. A stream that a
    test or another tool swapped in may not have reconfigure(). Elsewhere Python 3.9+ writes
    UTF-8 already."""
    if platform.os_name() != "windows":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    _utf8_console()
    run = _Run()
    return _main(argv, run)


def _main(argv, run):
    def command():
        try:
            args = _parser().parse_args(argv)
        except SystemExit as e:
            # --help, --version
            return e.code if isinstance(e.code, int) else 0
        return COMMANDS[args.command](args, run)

    return _guarded(command, run)


def _guarded(fn, run):
    """fn()'s exit code; an error it raises is shown, as every command shows one, and gives
    its exit code."""
    try:
        return fn()
    except VCharonError as e:
        run.show_error(e)
        return e.exit_code
    except KeyboardInterrupt:
        # The session has killed ssh on its way out.
        run.log_line("error", "interrupted")
        sys.stderr.write("vcharon: interrupted\n")
        return 130
    except Exception as e:
        run.log_line("error", traceback.format_exc(), create=True)
        run.show_error(VCharonError("internal", "%s: %s" % (type(e).__name__, e)),
                       logged=True)
        return 1


def _usage(message, hint):
    return VCharonError("config", message, hint=hint)


# --- a membership's commands: whoami, post, read, watch, sync ---

def _membership(args, cfg):
    """(the record of the membership args mean, the flags that find it again): DESIGN
    §7.2."""
    record = channel_cmd.membership(cfg, args.channel, args.project, args.role)
    project = record.get("project") or channel_cmd.project_part(args.project)
    role = record.get("role") if "project" in record else args.role
    return record, ["--project", project] + (["--role", role] if role else [])


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
    return {"channel": record["channel"], "name": name, "project": record.get("project"),
            "role": record.get("role"), "leader": record["leader"],
            "leads": record["leader"] == name,
            "mode": "local" if record["ssh"] is None else "remote", "server": record["ssh"],
            "folder": os.path.join(tree, name), "tree": tree}


def _whoami(args, run):
    """vcharon whoami [C] [--json]. With C, one object: {"channel", "name", "project", "role",
    "leader", "leads", "mode", "server", "folder", "tree", "box", "box_source"}: "mode" is
    "local" or "remote", "server" the alias (null for a local member), "folder" your own
    folder on this box and "tree" the channel's (a remote member's copy); "role" is null
    without one. "box" is this machine's box now (a membership keeps the name it joined
    with), "box_source" "config" ([vcharon] box) or "os" (the default: mac, win, linux).
    Without C: {"box", "box_source", "project", "role", "name", "channels"}: "name" is the
    name a join from here would take, "channels" every membership of this project on this box
    (of this role too, with --role), each an object as with C, without "box" and
    "box_source"."""
    cfg = load_config()
    box = {"box": cfg.box_name, "box_source": cfg.box_source}
    if args.channel is not None:
        record, _ = _membership(args, cfg)
        doc = _member_doc(cfg, record)
        if args.json:
            return _print_json(dict(doc, **box))
        _say("vcharon: whoami %s" % args.channel)
        _whoami_lines(doc)
        _say("  box      %s" % cfg.box_text())
        return 0
    channel_cmd.check_role(args.role)
    project = channel_cmd.project_part(args.project)
    name = channel_cmd.member_name(cfg, project, args.role)
    mine = [r for r in channel_cmd.records()
            if r.get("project") == project and (args.role is None
                                                or r.get("role") == args.role)]
    doc = dict(box, project=project, role=args.role, name=name,
               channels=[_member_doc(cfg, r) for r in mine])
    if args.json:
        return _print_json(doc)
    _say("vcharon: whoami")
    _say("  box      %s" % cfg.box_text())
    _say("  project  %s%s" % (project, "  role %s" % args.role if args.role else ""))
    _say("  name     %s (a join from here)" % name)
    if not mine:
        _say("  no channels joined from this project")
    for one in doc["channels"]:
        _say("")
        _whoami_lines(one)
    return 0


def _whoami_lines(doc):
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
    """vcharon post C: an entry into a .md file of your own folder: RESULTS.md, --file's, or
    the leader's STEPS.md (--steps). post() checks the file (mailbox/post.py)."""
    watch_mod.utf8_output()
    to, title = post_mod.check_args(args.to, args.title, args.re, args.body)
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
    record, flags = _membership(args, cfg)
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
    post_mod.check_own(own, tree if synced else None)
    # a missing own folder is post()'s to refuse, with the rejoin as its fix
    path = (post_mod.file_below(own, parts) if os.path.isdir(own)
            else os.path.join(own, *parts))
    body = args.body if args.body is not None else post_mod.body_from(_stdin_bytes())
    id_, when = post_mod.post(path, name, to, title, args.re, body)
    print("posted %s — %s to %s at %s" % (id_, title, "/".join([name] + parts), when))
    return 0


def _read(args, run):
    """vcharon read C [--json]: read_mod's view of the channel's tree on this box."""
    watch_mod.utf8_output()
    cfg = load_config()
    record, _ = _membership(args, cfg)
    tree, synced = _tree(cfg, record)
    if args.json:
        try:
            doc = read_mod.view_json(tree, args.channel, synced=synced, full=args.full,
                                     last=args.last)
        except OSError as e:
            raise fsops.error(e, tree)
        return _print_json(doc)
    return read_mod.view(tree, args.channel, synced=synced, full=args.full, last=args.last)


def _watch(args, run):
    """vcharon watch C: a local member's watch of the channel folder, or a remote member's of
    its synced copy; the watcher's own exit codes."""
    watch_mod.utf8_output()
    if args.max_errors is not None and not args.until_change:
        raise _usage("--max-errors needs --until-change", "add --until-change, or leave out "
                     "--max-errors")
    cfg = load_config()
    record, flags = _membership(args, cfg)
    limits = {"fresh": args.fresh, "until_change": args.until_change,
              "max_minutes": args.max_minutes or (25 if args.until_change else None),
              "max_errors": args.max_errors or 10}
    if record["ssh"] is None:
        if args.no_stream:
            raise _usage("--no-stream is for a remote member; you are a local member of %s"
                         % args.channel, "leave out --no-stream")
        return watch_mod.watch_dir(os.path.abspath(os.path.expanduser(record["remote"])),
                                   record["name"], args.every or watch_mod.DIR_EVERY, **limits)
    stream = not args.no_stream
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
    record, flags = _membership(args, cfg)
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
    if _missing(os.path.join(own, entries.MEMBER_FILE)):
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
}


class _Run:
    """What the error path needs to know about the run so far."""

    def __init__(self):
        # None until a command opens its log; a usage error writes no log.
        self.log = None
        # what a failed commit did, as the error block shows it; None if nothing
        self.done_line = None
        # the job whose error this is, in a sync of a section's two jobs: its console ERROR
        # line names it (M9c); None for one job and every other command
        self.job_name = None
        # where the error block goes: stderr, or with vcharon sync --repeat the round's lines,
        # which go to stdout (M15)
        self.out = None

    def log_line(self, level, msg, create=False):
        if self.log is None and create:
            try:
                self.log = Log(os.path.join(platform.log_dir(), "vcharon.log"))
            except Exception:
                pass
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
            # as this box runs vcharon (M14a); the log keeps the plain text, read later by a
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
    """config.load, and each skipped section's line in vcharon.log (M10): never on stderr, whose
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
    """vcharon key (DESIGN §6.5): the terminal first, then the arguments and the config, then
    keys.unlock."""
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
    if path.startswith("~/"):
        path = path[2:]
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


def size_text(n):
    """Decimal units with one decimal: 0 B, 999 B, 1.0 kB, 3.4 MB, 1.2 GB."""
    if n < 1000:
        return "%d B" % n
    for unit, scale in (("kB", 1e3), ("MB", 1e6), ("GB", 1e9)):
        # decided after rounding, so 999,999 bytes is 1.0 MB, not 1000.0 kB
        if round(n / scale, 1) < 1000:
            return "%.1f %s" % (n / scale, unit)
    return "%.1f TB" % (n / 1e12)


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
    """The jobs a name runs: a job, or a mailbox section's two (DESIGN §12)."""
    jobs = cfg.named(name)
    skip = cfg.skipped_for(name) if jobs is None else None
    if skip is not None:
        # its own file's error; every other job still runs (M10)
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
    # the round wrote its state (M15)
    real_log: Log = None
    saved_state: bool = False

    @property
    def name(self):
        return self.job.name


class _Conn:
    """The connection of one group of jobs: opened by the first job that needs it, closed
    after the group's last job, or as soon as it breaks."""

    def __init__(self):
        self.session = None
        # the job that opened it; the session's own lines go to its log
        self.opener = None
        # the server's machine id and OS, from the hello
        self.machine = None
        self.os = None
        # the error that broke it; the group's other jobs are then skipped
        self.broke = None


def session_key(job):
    """Jobs next to each other with the same key share one connection (M7a): the destination
    as written, and every [vcharon] setting, each of which the ssh command or the Session
    reads."""
    return (job.ssh, dataclasses.astuple(job.settings))


def _groups(todo):
    """The jobs in the order given, cut where the session key changes; never reordered."""
    groups = []
    for jr in todo:
        if groups and session_key(groups[-1][-1].job) == session_key(jr.job):
            groups[-1].append(jr)
        else:
            groups.append([jr])
    return groups


def _named_once(names):
    """A usage error if a job is named twice. Case is ignored: job names are unique that way
    (DESIGN §12)."""
    seen = {}
    for name in names:
        other = seen.get(name.casefold())
        if other is not None:
            what = ("%s is named twice" % pathrules.show(name) if other == name
                    else "%s and %s name the same job" % (pathrules.show(other),
                                                          pathrules.show(name)))
            raise VCharonError("config", what, hint="name each job once")
        seen[name.casefold()] = name


def _run_job(args, run):
    """The jobs of a sync (decision 16 of the M4 plan, M7a): the arguments, every job's
    config and plugins, the logs, every lock, then each job in the order given; the jobs
    next to each other with one session key share one connection. One job prints and logs
    exactly as ever; several end with a summary line."""
    started = time.monotonic()
    if args.repeat is not None:
        _repeat_flags(args, run)
    _named_once(args.job)
    cfg = load_config()
    jobs = [job for name in args.job for job in _jobs_named(cfg, name)]
    # a mailbox section and one of its jobs
    _named_once([job.name for job in jobs])
    if args.repeat is not None and len({session_key(job) for job in jobs}) > 1:
        raise VCharonError("config", "--repeat needs jobs that share one connection",
                           hint="run the jobs of each connection in a --repeat of their own")
    # Every usage, config, option and capability error fails before anything else: one bad
    # job, and none runs.
    for job in jobs:
        try:
            plugin.check_side(job.source.end, job.source.plugin, "source", job.source.options)
            plugin.check_side(job.sink.end, job.sink.plugin, "sink", job.sink.options)
        except VCharonError:
            # Its text names an option, not the job; with several jobs, say which (M9c).
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
        # With several jobs, a job's console ERROR line names it (M9c); one job's is as ever.
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
        for group in _groups(todo):
            conn = _Conn()
            with contextlib.ExitStack() as stack:
                for i, jr in enumerate(group):
                    if conn.broke is not None:
                        _skip(jr, conn)
                        continue
                    run.log = jr.log
                    jr.started = mark
                    _one_of_group(args, jr, conn, stack, last=i == len(group) - 1)
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
    """A job whose connection broke under an earlier job of its group: one line, and the
    exit code of the failure that broke it."""
    line = "vcharon: %s  skipped: the connection to %s broke" % (jr.name, jr.job.ssh)
    _say(line)
    jr.log.info(line)
    jr.status = "skipped"
    jr.code = conn.broke.exit_code if isinstance(conn.broke, VCharonError) else 1


# --- vcharon sync --repeat (DESIGN §14 M15) ---

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
    take for a new error (the M15a review)."""
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
    ended = threading.Event()
    _watch_stdin(ended)
    conn = _Conn()
    tally = _Tally()
    run.log = todo[0].real_log
    with contextlib.ExitStack() as stack:
        while True:
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
        # named, as with several jobs: the watcher's error keys read the name (M9c)
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
            # its path names the job (DESIGN §13)
            jr.errors.job_name = None
            jr.errors.show_error(e)
        return False
    jr.started = time.monotonic()
    try:
        eng, done = _run_one(args, jr, conn, stack)
    except Exception as e:
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


def _one_of_group(args, jr, conn, stack, last):
    """Runs one job, closes the connection when no later job of the group will use it, and
    only then prints how the job ended, as a one-job run always has."""
    error = trace = None
    try:
        eng, done = _run_one(args, jr, conn, stack)
    except Exception as e:
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
    """The group's session: opened by the first job that needs it, whose log gets the
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
    # job's log; what it says from here on is this job's (M9).
    conn.session.helper_log = jr.log
    return conn.session


def _missing(path):
    """True only when path is known not to exist: a folder that can't be read is the run's to
    report, with its own hint."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _own_folder(job, log, dry_run):
    """A mailbox job's folders on the client, <local> and <local>/<me>: up's path source
    never creates its own path. Only while up has sent nothing: a folder that vanished after
    that must not come back empty, or up's prune would empty the server's copy. Down checks it
    too, so that a run of down alone reports it as well; down itself never plans <me>/ (M9),
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
        # M10: a channel member's folder always holds MEMBER.md. Gone while up has sent it,
        # the folder was emptied or replaced (a failed restore, a stray .DS_Store in a new
        # one): up's prune would delete the server's copy, so neither job runs.
        if (job.mailbox.channel and isinstance(sent, dict) and entries.MEMBER_FILE in sent
                and _missing(os.path.join(path, entries.MEMBER_FILE))):
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


# The general hints for a name the source can't send or list: each tells the source's owner to
# rename, exclude or skip it. A mailbox section can't exclude or skip, and only a folder's writer
# can change it (the M9 real run); _mailbox_hint swaps them. Compared as text, since an error
# from the helper arrives with its hint as text.
SOURCE_HINTS = frozenset([pathrules.UNSAFE_HINT, pathrules.COLLISION_HINT,
                          path_plugin.LINKS_HINT, path_plugin.NAME_HINT])
MAILBOX_DOWN_HINT = ("the writer of each folder named above %s (MAILBOX.md in the vcharon "
                     "folder, §5); your up still runs")
# with the writer's folder, since up's paths are relative to it (M9c)
MAILBOX_UP_HINT = "%s in your own folder (%s/)"
# in channel_cmd since M13: a local member's watch prints it too
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
    """One job: its state, its header line, the group's session, the engine, the state
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

    # --repeat prints only a round's errors and its ROUND line (M15); the log keeps the rest
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
    # Every job has a remote end, and its state is tied to that server (DESIGN §7.3), which
    # must be Linux (M11a).
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
        # prune delete on the wrong machine (DESIGN §11.2).
        if saved is not None and binding != saved.sink:
            raise VCharonError("state_mismatch", "the state of %s was saved for another "
                               "target: %s, now %s"
                               % (name, _dumps(saved.sink), _dumps(binding)), hint)
        bound.append(binding)
        _summary(say, p, checked, args.dry_run, job=True, full=args.full)

    eng = engine.Engine(session, job.source, job.sink, log, after_check=after_check,
                        after_plan=after_plan)
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
        # two files (M15)
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
    return eng, done


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
    """Saves what a commit that failed partway wrote (DESIGN §9.2). A failure here is only
    logged: it never replaces the run's own error."""
    try:
        state.save(name, state.State(fingerprint, eng.plan.identity, binding, eng.state_after,
                                     state.now()))
    except Exception as e:
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
