"""vcharon doctor: checks of this machine, each server and each channel section's jobs, one line
each (DESIGN, "Command line"); client. It only reads, apart from a temp file in the state and log
dirs, and its log. It never prompts.

--json prints one object instead: {"version", "protocol", "format", "python", "executable", "os",
"command", "install", "helper_bundle", "box", "box_source", "claimer_source", "dirs", "servers",
"ok", "failed", "warnings", "checks"}. "dirs" is {"state", "logs", "joined", "channels"}: the
folders vcharon writes here (joined: a remote member's copies of its channels; channels: the
channel root of local members). "format" is the newest channel format this vcharon reads;
"servers" one object for each server whose session opened and echoed: {"server", "python", "os",
"distro", "distro_id", "distro_version", "tested"}, the distro fields from its /etc/os-release
(PRETTY_NAME, ID, VERSION_ID; null each when missing) and "tested" true for Debian 13 or later.
"command" is how this box runs vcharon (the fix lines' spelling); "install" {"kind", "path"}: how
it was installed ("binary", "pipx", "uv", "pip" or "source") and the file vcharon --update
replaces (a binary) or the checkout (source), else null; "helper_bundle" {"modules",
"has_helper", "bytes"}: the bundle sent to a server, built here as a session builds it: its
module count, whether vcharon.helper is in it, and its size ({"modules": 0, "has_helper": false,
"bytes": null} when it couldn't be built); "box" this machine's part of member names and
"box_source" "config" ([vcharon] box) or "os" (the default), both null when the config can't be
read; "claimer_source" what the id this machine claims member folders with comes from: "client-id
file (from the machine id)" or "client-id file (random)"; before the first join has made that
file, the source it will be made from; null when the file can't be read, or can't be made (a Mac
or Windows box whose id couldn't be read); "ok" is true when no check failed; "failed" and
"warnings" count the checks of those levels; each of "checks" is {"level", "subject", "text",
"fix", "note"}, in the order the lines would print: "level" is "ok", "warn" or "FAIL", "subject"
what was checked (python, config, a server's alias, a job's name, …), "fix" the fix line's text
(as this box runs vcharon) and "note" an ok line's note, each null when there is none."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

from . import (
    PROTOCOL,
    VERSION,
    bundle,
    channels,
    charter,
    config,
    fsops,
    install,
    keys,
    platform,
    plugin,
    skill,
    ssh,
    state,
)
from .log import Log
from .proto import VCharonError

# the doctor's echo: all 256 byte values, then random bytes, 4 MiB in all
ECHO_BYTES = 4 << 20
# subjects are padded to the longest one, but to no more than this
SUBJECT_MAX = 16
CLIENT_SUBJECTS = ("vcharon", "install", "path", "skill", "bundle", "python", "config", "box",
                   "ssh", "agent", "dirs", "machine", "claimer")
NO_JOBS_NOTE = "no channels joined over ssh; to check a server: vcharon doctor --server ALIAS"
# A chosen line, not a derived one: across minutes a heading's time wins, so any gap can
# reorder entries posted near a minute's end; from 30 s it will do so often.
CLOCK_WARN = 30
CLOCK_HINT = "sync both clocks (NTP); channel entries are ordered by each box's own clock"
# a Linux box without /etc/machine-id
LINUX_ID_HINT = "as root, run systemd-machine-id-setup: it writes /etc/machine-id"
# a bundle without the helper: the package's .py files are missing from this install
BUNDLE_HINT = "this install is broken: install vcharon again"
ZONE_HINT = "give both the same time zone; entry headings carry local time with no zone"
# Other vcharon installs on PATH. Each fix asks the user: an agent follows a fix line, and
# removing software or changing PATH is the user's to decide.
LATER_HINT = ("ask your user to uninstall the vcharon they don't use, or to take its folder off "
              "PATH: a shell with another PATH order runs the other one")
FIRST_HINT = ("ask your user to uninstall that vcharon, or to put this one's folder before it "
              "on PATH: until then a vcharon typed as a command runs the other one")
# a checkout is often run by hand, as <venv python> -m vcharon: there is no folder of its own to
# put on PATH
CHECKOUT_HINT = ("ask your user which vcharon agents should run: a vcharon typed as a command "
                 "runs the one first on PATH, not this checkout")


def _counted(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


class Report:
    """The check lines, and how many failed or warned."""

    def __init__(self, say, subjects):
        self.say = say
        self.width = min(SUBJECT_MAX, max(len(s) for s in subjects))
        self.failed = 0
        self.warnings = 0
        # every check, as --json gives them
        self.checks = []

    def check(self, level, subject, text, hint=None, note=None):
        head = "  %s  %s  " % (level.ljust(4), subject.ljust(self.width))
        self.say(head + text)
        if level == "FAIL":
            self.failed += 1
        elif level == "warn":
            self.warnings += 1
        # as this box runs vcharon
        fix = platform.runnable(hint) if level != "ok" and hint else None
        note = note if level == "ok" and note else None
        self.checks.append({"level": level, "subject": subject, "text": text, "fix": fix,
                            "note": note})
        # the fix or the note starts under the text
        if fix:
            self.say(" " * len(head) + "fix: " + fix)
        if note:
            self.say(" " * len(head) + "note: " + note)

    def last_line(self, seconds):
        warned = ", %s" % _counted(self.warnings, "warning") if self.warnings else ""
        if self.failed:
            return "FAIL  %d failed%s  (%.1f s)" % (self.failed, warned, seconds)
        return "OK  nothing failed%s  (%.1f s)" % (warned, seconds)


class _Dest:
    """One destination in scope: its settings, and its jobs in file order."""

    def __init__(self, dest, settings):
        self.dest = dest
        self.settings = settings
        self.jobs = []


def _scope(target, cfg):
    """[_Dest] in the order they're checked: the server target
    (--server) alone, or every channel section's. A target must be a good destination: a bad
    one is a usage error, before any line."""
    if target is not None:
        ssh.check_dest(target)
        settings = cfg.settings if cfg is not None else config.Settings()
        if cfg is not None:
            # the settings of the first job that names it, as in all mode
            for job in cfg.jobs.values():
                if job.ssh == target:
                    settings = job.settings
                    break
        return [_Dest(target, settings)]
    if cfg is None:
        return []
    dests = {}
    for job in cfg.jobs.values():
        if job.ssh not in dests:
            dests[job.ssh] = _Dest(job.ssh, job.settings)
        dests[job.ssh].jobs.append(job)
    return list(dests.values())


# --- the client ---

def _vcharon(rep):
    """This vcharon's version, and how it's run here: the spelling of every fix line."""
    rep.check("ok", "vcharon", "%s, protocol %d, reads channel formats up to %d; runs as %s"
              % (VERSION, PROTOCOL, charter.FORMAT, platform.self_command()))


def _install(rep):
    """How this vcharon was installed, so a user knows what vcharon --update touches: a
    binary's file, or the other tool that updates it. No network call. Returns --json's
    "install"."""
    inst = install.detect()
    if inst.kind == "binary":
        text = "a standalone binary, %s: vcharon --update replaces this file" % inst.path
    elif inst.kind == "source":
        text = ("a checkout, %s: vcharon --update prints the git commands that update it"
                % inst.path)
    else:
        text = "with %s: vcharon --update prints the command that updates it" % inst.kind
    rep.check("ok", "install", text)
    return {"kind": inst.kind, "path": inst.path}


# the PATH scan lives in platform, beside self_command, which uses it on Windows too; doctor's
# own name for it is what its tests replace
vcharons_on_path = platform.vcharons_on_path


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def path_check(rep, kind, which=shutil.which):
    """Other vcharon installs on PATH (DESIGN, "Fix lines"). What a typed vcharon runs is
    platform.typed_vcharon's answer, as self_command's, so this row and the fix lines'
    spelling agree. One after this install is a warning:
    a shell or tool with another PATH order runs it. One that a typed vcharon runs instead, or
    this install not on PATH at all, is a failure for an install of its own (the vcharon an
    agent types, and the guide it reads, aren't this one), and a warning for a checkout (kind
    "source"), which is normally run by hand."""
    first = platform.typed_vcharon(which)
    first_mine = first is not None and platform.runs_this(first)
    others = [one for one in vcharons_on_path() if not platform.runs_this(one)]
    if first is not None and not first_mine and not any(_same(first, o) for o in others):
        others.insert(0, first)
    if not others:
        return
    if first_mine:
        rep.check("warn", "path", "another vcharon on PATH, after this one: %s"
                  % ", ".join(others), LATER_HINT)
        return
    shown = first if first is not None else others[0]
    rest = ", ".join(o for o in others if not _same(o, shown))
    checkout = kind == "source"
    rep.check("warn" if checkout else "FAIL", "path",
              "the vcharon first on PATH is another install, %s%s; this %s runs as %s"
              % (shown, " (and on PATH: %s)" % rest if rest else "",
                 "checkout" if checkout else "one", platform.self_command()),
              CHECKOUT_HINT if checkout else FIRST_HINT)


def skill_check(rep):
    """The skill copies vcharon skill install wrote (told by its marker line) against this
    version's text: an update replaces the program, never them, and an agent reads the skill
    before the guide. No line when none is installed; a file without the marker is the
    user's own, and not vcharon's to judge."""
    found = skill.installed()
    if not found:
        return
    stale = [(agent, target) for agent, target, current in found if not current]
    if not stale:
        rep.check("ok", "skill", "%s: this version's" % ", ".join(t for _, t, _ in found))
        return
    rep.check("warn", "skill", "%s: written by another version of vcharon"
              % ", ".join(t for _, t in stale),
              skill.FIX % " ".join("--" + a for a, _ in stale))


def _bundle(rep):
    """The bundle a session sends to the server, built as a session builds it: its module count
    and that vcharon.helper is in it. In a binary that proves the spec collected the package's
    .py files, which nothing else run here reads. Returns --json's "helper_bundle"."""
    try:
        found = bundle.summary()
    except Exception as e:  # noqa: BLE001
        # a file that won't read or decode, or a bundle that won't read back: a check line,
        # never a crash of the doctor
        rep.check("FAIL", "bundle", "couldn't build the helper's bundle: %s: %s"
                  % (type(e).__name__, e), BUNDLE_HINT)
        return {"modules": 0, "has_helper": False, "bytes": None}
    if not found["has_helper"]:
        rep.check("FAIL", "bundle", "the helper's bundle has %s and no vcharon.helper: no server "
                  "can run it" % _counted(found["modules"], "module"), BUNDLE_HINT)
    else:
        rep.check("ok", "bundle", "the helper's bundle: %s, %.0f kB, vcharon.helper in it"
                  % (_counted(found["modules"], "module"), found["bytes"] / 1000))
    return found


def _python(rep):
    osn = platform.os_name()
    if platform.is_frozen():
        # the binary's own Python: nothing is installed, and the line must not read as if it were
        rep.check("ok", "python", "%s, bundled in %s, on %s"
                  % (platform.python_version(), sys.executable, osn))
    else:
        rep.check("ok", "python", "%s (%s) on %s"
                  % (platform.python_version(), sys.executable, osn))
    if osn != "windows":
        return
    if platform.is_wow64():
        rep.check("warn", "python", "32-bit Python on 64-bit Windows",
                  "install a 64-bit Python; until then vcharon runs Sysnative\\OpenSSH\\ssh.exe")


def _config(rep, cfg, err, no_scope):
    if err is not None:
        rep.check("FAIL", "config", err.message, err.hint)
        return
    note = NO_JOBS_NOTE if no_scope else None
    if not cfg.exists:
        rep.check("ok", "config", "%s doesn't exist; using the defaults" % cfg.path, note=note)
    else:
        rep.check("ok", "config", "%s: %s" % (cfg.path, _counted(len(cfg.jobs), "job")),
                  note=note)
    # a broken or clashing channels.d/ file: the other jobs still run
    for skip in cfg.skipped:
        rep.check("warn", "config", skip.line, skip.error.hint)


def _box(rep, cfg):
    if cfg is not None:
        rep.check("ok", "box", cfg.box_text())


def _ssh(rep, settings):
    hint = ssh.START_HINT
    argv = ssh.ssh_prefix(settings) + ["-V"]
    try:
        ran = fsops.run(argv, timeout=10)
    except OSError as e:
        rep.check("FAIL", "ssh", "couldn't start %s: %s" % (argv[0], e.strerror or e), hint)
        return
    if ran.rc != 0:
        rep.check("FAIL", "ssh", "%s -V didn't work (exit %s)" % (settings.ssh_path, ran.rc),
                  hint)
        return
    text = ""
    for data in (ran.err, ran.out):
        lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines()
                 if line.strip()]
        if lines:
            text = lines[0]
            break
    rep.check("ok", "ssh", "%s (%s)" % (text, settings.ssh_path))


def _agent(rep, agent, dests):
    state_, n = agent
    text = keys.agent_text(state_, n)
    if state_ == "keys":
        rep.check("ok", "agent", text)
        return
    if state_ == "error":
        # ssh-add itself is missing: no agent question can be answered
        rep.check("warn", "agent", text, ssh.START_HINT)
        return
    dest = dests[0].dest if len(dests) == 1 else "ALIAS"
    if state_ == "empty":
        rep.check("warn", "agent", text, "a key with a passphrase works only once it's in the "
                  "agent: run vcharon key %s" % dest)
        return
    if platform.os_name() == "windows":
        hint = keys.ADMIN_HINT
    elif not os.environ.get("SSH_AUTH_SOCK"):
        hint = ("only a key without a passphrase works without an agent; start one: %s"
                % keys.START_AGENT)
    else:
        hint = "a forwarded agent ends with its ssh login; log in again"
    # at most a warn: a key without a passphrase needs no agent
    rep.check("warn", "agent", text, hint)


def folders():
    """{"state", "logs", "joined", "channels"}: the folders vcharon writes on this machine. A
    remote member's copies of its channels are under joined (its own folder and the other
    members' alike); a local member's channels under channels, the channel root here."""
    return {"state": platform.state_dir(), "logs": platform.log_dir(),
            "joined": os.path.expanduser(platform.joined_dir()),
            "channels": channels.root_path()}


def _dirs(rep):
    where = folders()
    # only the state and log dirs are made and tried here: the others may not exist yet, and
    # doctor makes nothing else
    tried = (where["state"], where["logs"])
    good = True
    for folder in tried:
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".vcharon-doctor-", suffix=".tmp")
            os.close(fd)
            os.remove(tmp)
        except OSError as e:
            good = False
            rep.check("FAIL", "dirs", "can't write in %s: %s" % (folder, e.strerror or e),
                      "fix its permissions, or set VCHARON_HOME to an absolute folder")
    if good:
        rep.check("ok", "dirs", "state %s, logs %s, joined %s, channels %s"
                  % (where["state"], where["logs"], where["joined"], where["channels"]))


def _machine(rep):
    """This machine's id, so an agent checking it needs no python3 -c, and the
    client-id file the claimer id in MEMBER.md comes from. Only a warn without a machine id:
    a client of remote jobs needs none. Returns the claimer's source, as --json gives it, or
    None when the file can't be read."""
    mid = platform.machine_id()
    if mid:
        rep.check("ok", "machine", mid)
    else:
        hint = platform.no_machine_hint() or LINUX_ID_HINT
        rep.check("warn", "machine", "no machine id: this machine can't hold a channel "
                  "(--local), nor keep jobs' state as a server", hint)
    path = platform.client_id_path()
    try:
        cid, origin = platform.client_id(make=False)
    except VCharonError as e:
        rep.check("FAIL", "claimer", e.message, e.hint)
        return None
    rep.check("ok", "claimer", "member folders are claimed with the client-id file %s (%s%s)"
              % (path, "" if cid else "made at the first join, ", origin))
    return "client-id file (%s)" % origin


# --- a destination ---

def utc_text(offset):
    """Seconds east of UTC as UTC+hh:mm."""
    sign = "-" if offset < 0 else "+"
    minutes = abs(int(offset)) // 60
    return "UTC%s%02d:%02d" % (sign, minutes // 60, minutes % 60)


def clock_checks(hello, received, local_offset):
    """[(level, text, hint)]: the server's clock and time zone against this machine's,
    from the hello, this machine's time.time() when it was read, and this machine's
    utc_offset. A field the hello lacks gives no line."""
    checks = []
    gap = ssh.clock_gap(hello, received)
    if gap is not None:
        text = "clock %+.1f s from this machine" % gap
        # on the shown value, so -29.96 (shown -30.0) warns as the text reads
        if abs(round(gap, 1)) < CLOCK_WARN:
            checks.append(("ok", text, None))
        else:
            checks.append(("warn", text, CLOCK_HINT))
    offset = hello.get("utc_offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset != local_offset:
        checks.append(("warn", "time zone %s, this machine %s"
                       % (utc_text(offset), utc_text(local_offset)), ZONE_HINT))
    return checks


def _login(rep, d, agent, log):
    """The -v probe's line. Returns (logged in, the login failed on a locked key)."""
    dest = d.dest
    try:
        probe = ssh.verbose_probe(d.settings, dest, log)
    except VCharonError as e:
        rep.check("FAIL", dest, e.message, e.hint)
        return False, False
    key = ssh.probe_key(probe)
    kind = ssh.key_kind(key) if key is not None else None
    if probe.rc == 0:
        if key is None:
            rep.check("ok", dest, "logs in")
            return True, False
        text = "logs in with %s" % keys.key_text(key)
        note = None
        if kind in ("file+agent", "agent"):
            text += ", from the agent"
        if kind == "agent":
            note = "only the agent holds this key: runs work while it does"
            if platform.os_name() == "linux" and os.environ.get("SSH_CONNECTION"):
                note += "; a forwarded agent, only while this ssh login is open"
        rep.check("ok", dest, text, note=note)
        return True, False
    if ssh.denied(probe) and kind == "file":
        # the third row of (DESIGN, "Failures before the helper runs")
        rep.check("FAIL", dest, "ssh can't use your key %s: the server accepts it, but it's "
                  "locked by a passphrase" % ssh.shown(key.ident), "vcharon key %s" % dest)
        return False, True
    if ssh.denied(probe) and key is None:
        err = keys.no_key_error(dest, agent[0])
        rep.check("FAIL", dest, err.message, err.hint)
        return False, False
    rep.check("FAIL", dest, probe.error.message, probe.error.hint)
    return False, False


def _server(rep, d, session):
    """The session's lines. Returns the hello, or None when the session didn't open or
    broke."""
    dest = d.dest
    try:
        hello = session.open()
    except VCharonError as e:
        rep.check("FAIL", dest, e.message, e.hint)
        return None
    rep.check("ok", dest, "Python %s on %s, user %s; handshake %.2f s"
              % (hello.get("python"), hello.get("distro") or hello.get("os"), hello.get("user"),
                 session.handshake_seconds))
    # tested on Debian 13 or later: anything else is a warning, never a failure
    warning = platform.distro_warning(hello)
    if warning is not None:
        rep.check("warn", dest, warning)
    for level, text, hint in clock_checks(hello, session.hello_received,
                                          time.localtime().tm_gmtoff):
        rep.check(level, dest, text, hint)
    if session.junk_bytes:
        rep.check("warn", dest, "the server's shell printed %d bytes before vcharon started"
                  % session.junk_bytes, "make its startup files print nothing when the shell "
                  "isn't interactive; the log has the text")
    if hello.get("os") != "linux":
        # a Mac or Windows box has a machine id, but it still isn't a server
        rep.check("FAIL", dest, state.not_linux(hello, dest), state.LINUX_HINT % dest)
    elif not hello.get("machine"):
        rep.check("warn", dest, "no machine id: jobs can't keep state there",
                  state.NO_MACHINE_HINT)
    data = bytes(range(256)) + os.urandom(ECHO_BYTES - 256)
    started = time.monotonic()
    try:
        back = session.echo(data)
    except VCharonError as e:
        rep.check("FAIL", dest, e.message, e.hint)
        return None
    if back != data:
        rep.check("FAIL", dest, "the echo came back different",
                  "see the log; the connection isn't binary-safe")
        return None
    rep.check("ok", dest, "echo 4 MiB byte-identical in %.2f s" % (time.monotonic() - started))
    return hello


def server_json(dest, hello):
    """--json's object of a server whose hello came: what it runs."""
    return {"server": dest, "python": hello.get("python"), "os": hello.get("os"),
            "distro": hello.get("distro"), "distro_id": hello.get("distro_id"),
            "distro_version": hello.get("distro_version"),
            "tested": platform.distro_warning(hello) is None}


# --- a job ---

def _sent_counts(source):
    if isinstance(source, dict) and set(source) == {"sent"} and isinstance(source["sent"], dict):
        values = list(source["sent"].values())
        files = sum(1 for v in values if isinstance(v, list))
        dirs = sum(1 for v in values if v == "d")
        return ", %s, %s sent" % (_counted(files, "file"), _counted(dirs, "dir"))
    return ""


def _job(rep, job, session, hello, log):
    name = job.name
    sides = []
    for side, role in ((job.source, "source"), (job.sink, "sink")):
        try:
            plugin.check_side(side.end, side.plugin, role, side.options)
        except VCharonError as e:
            # and that side's plugin doctor is skipped
            rep.check("FAIL", name, e.message, e.hint)
            continue
        sides.append((side, role))
    hint = state.reset_hint(name)
    try:
        saved = state.load(name)
    except VCharonError as e:
        rep.check("FAIL", name, e.message, hint)
    else:
        if saved is None:
            rep.check("ok", name, "state: none yet; the first run sends everything")
        elif saved.fingerprint != state.fingerprint(job):
            rep.check("FAIL", name, state.config_changed(job), hint)
        else:
            rep.check("ok", name, "state saved %s%s" % (saved.saved, _sent_counts(saved.source)))
    if hello is not None and hello.get("os") != "linux":
        rep.check("FAIL", name, state.not_linux(hello, job.ssh), state.LINUX_HINT % job.ssh)
    elif hello is not None and not hello.get("machine"):
        rep.check("FAIL", name, "the server has no machine id, so %s can't keep state there"
                  % name, state.NO_MACHINE_HINT)
    for side, role in sides:
        if side.end == "remote":
            # skipped, without a line, when the destination failed
            if session is None or not session.usable:
                continue
            try:
                checks = plugin.checks_from_json(session.call(
                    "plugin.doctor", {"plugin": side.plugin, "role": role,
                                      "options": side.options}))
            except VCharonError as e:
                checks = [("FAIL", e.message, e.hint)]
        else:
            prefix = role + ": "
            ctx = plugin.Ctx("local", log=lambda msg, prefix=prefix: log.info(prefix + msg))
            try:
                # through the wire form, so a local plugin's checks pass the same test
                checks = plugin.checks_from_json(plugin.checks_to_json(
                    plugin.doctor("local", side.plugin, role, side.options, ctx)))
            except VCharonError as e:
                checks = [("FAIL", e.message, e.hint)]
        for level, message, fix in checks:
            if job.mailbox is not None and side.end == "remote" and level == "FAIL":
                # the closed channel's hint, as a sync shows it
                from . import cli
                fix = cli.channel_gone_hint(job, "not_found", fix) or fix
            elif job.mailbox is not None and role == "source":
                level, message, fix = _made_by_the_run(job, level, message, fix)
            rep.check(level, name, message, fix)


def _made_by_the_run(job, level, message, fix):
    """A channel section's own folder on the client may not exist before its first run: the run
    makes it while up has sent nothing (DESIGN, "Channel sections"). The server's tree is never made
    by a run: its absence is a closed channel."""
    if not (level == "FAIL" and message.startswith("from.path ")
            and message.endswith(" doesn't exist")) or job.source.end == "remote":
        return level, message, fix
    section = job.mailbox.section
    try:
        saved = state.load(section + ".up")
    except VCharonError:
        saved = None
    from . import channel_cmd
    channel = job.mailbox.channel
    flags = channel_cmd.name_flags(channel, job.mailbox.me)
    if saved is not None and isinstance(saved.source, dict) and saved.source.get("sent"):
        return (level, "%s, but %s.up has sent files from it" % (message, section),
                channel_cmd.rejoin_hint(channel, job.ssh, job.mailbox.me))
    return "ok", "%s yet; vcharon sync %s %s makes it" % (message, channel, flags), None


# --- the command ---

def main(args, run):
    """vcharon doctor [--server ALIAS] [--json]: exit 0 when no check failed, else 1."""
    started = time.monotonic()
    log = run.log = Log(os.path.join(platform.log_dir(), "vcharon.log"), console=args.verbose)
    target = args.server
    try:
        cfg, cfg_err = config.load(), None
    except VCharonError as e:
        # a FAIL line, not exit 3; a target is then a destination with the default settings
        cfg, cfg_err = None, e
    dests = _scope(target, cfg)
    base = cfg.settings if cfg is not None else config.Settings()
    log.info("vcharon %s doctor%s; Python %s (%s) on %s; config %s"
             % (VERSION, " " + target if target is not None else "",
                platform.python_version(), sys.executable, platform.os_name(),
                cfg.path if cfg is not None else "broken"))

    def say(line):
        if not args.json:
            print(line)
            sys.stdout.flush()
        log.info(line)

    subjects = list(CLIENT_SUBJECTS) + [d.dest for d in dests] + [job.name for d in dests
                                                                 for job in d.jobs]
    rep = Report(say, subjects)
    say("vcharon: doctor%s" % (" --server " + target if target is not None else ""))
    _vcharon(rep)
    installed = _install(rep)
    path_check(rep, installed["kind"])
    skill_check(rep)
    helper_bundle = _bundle(rep)
    _python(rep)
    _config(rep, cfg, cfg_err, not dests)
    _box(rep, cfg)
    _ssh(rep, base)
    agent = keys.agent_state(base)
    _agent(rep, agent, dests)
    _dirs(rep)
    claimer_source = _machine(rep)
    servers = []
    for d in dests:
        # a locked key's FAIL line says to run vcharon key: the doctor never prompts
        logged_in, _ = _login(rep, d, agent, log)
        # ControlMaster off, as in the probe (DESIGN, "The ssh command")
        with ssh.Session(d.settings, d.dest, log, probe=True) as session:
            hello = _server(rep, d, session) if logged_in else None
            if hello is not None:
                servers.append(server_json(d.dest, hello))
            for job in d.jobs:
                _job(rep, job, session if hello is not None else None, hello, log)
    say(rep.last_line(time.monotonic() - started))
    if args.json:
        print(json.dumps({"version": VERSION, "protocol": PROTOCOL, "format": charter.FORMAT,
                          "python": platform.python_version(), "executable": sys.executable,
                          "os": platform.os_name(), "command": platform.self_command(),
                          "install": installed, "helper_bundle": helper_bundle,
                          "box": cfg.box_name if cfg is not None else None,
                          "box_source": cfg.box_source if cfg is not None else None,
                          "claimer_source": claimer_source, "dirs": folders(),
                          "servers": servers,
                          "ok": not rep.failed, "failed": rep.failed,
                          "warnings": rep.warnings, "checks": rep.checks},
                         ensure_ascii=False))
        sys.stdout.flush()
    return 1 if rep.failed else 0
