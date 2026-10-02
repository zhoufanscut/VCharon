"""ferry doctor: checks of the client, each server and each job, one line each (DESIGN §13);
client. It only reads, apart from a temp file in the state and log dirs, and its log."""

from __future__ import annotations

import os
import sys
import tempfile
import time

from . import VERSION, config, fsops, keys, platform, plugin, ssh, state
from .log import Log
from .proto import VCharonError

# the doctor's echo: all 256 byte values, then random bytes, 4 MiB in all
ECHO_BYTES = 4 << 20
# subjects are padded to the longest one, but to no more than this
SUBJECT_MAX = 16
CLIENT_SUBJECTS = ("python", "config", "ssh", "agent", "dirs", "machine")
NO_JOBS_NOTE = "no jobs; to check a server: ferry doctor <dest>"
# A chosen line, not a derived one (M12b): headings carry the minute, so any gap can reorder
# entries posted near a minute's end; from 30 s it will do so often.
CLOCK_WARN = 30
CLOCK_HINT = "sync both clocks (NTP); channel entries are ordered by each box's own clock"
ZONE_HINT = "give both the same time zone; entry headings carry local time with no zone"


def python_tuple():
    """(major, minor) of this Python; tests patch it."""
    return tuple(sys.version_info[:2])


def _counted(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


class Report:
    """The check lines (decision 17 of the M5 plan), and how many failed or warned."""

    def __init__(self, say, subjects):
        self.say = say
        self.width = min(SUBJECT_MAX, max(len(s) for s in subjects))
        self.failed = 0
        self.warnings = 0

    def check(self, level, subject, text, hint=None, note=None):
        head = "  %s  %s  " % (level.ljust(4), subject.ljust(self.width))
        self.say(head + text)
        if level == "FAIL":
            self.failed += 1
        elif level == "warn":
            self.warnings += 1
        # the fix or the note starts under the text
        if level != "ok" and hint:
            # as this box runs ferry (M14a)
            self.say(" " * len(head) + "fix: " + platform.runnable(hint))
        if level == "ok" and note:
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
    """[_Dest] in the order they're checked (decision 16 of the M5 plan). A target that is
    no job must be a good destination: a bad one is a usage error, before any line."""
    jobs = cfg.named(target) if target is not None and cfg is not None else None
    if jobs is not None:
        # a job, or a mailbox section's two, which share a destination and settings
        d = _Dest(jobs[0].ssh, jobs[0].settings)
        d.jobs.extend(jobs)
        return [d]
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


# --- the client (decision 18) ---

def _python(rep):
    osn = platform.os_name()
    rep.check("ok", "python", "%s (%s) on %s" % (platform.python_version(), sys.executable, osn))
    if osn != "windows":
        return
    if platform.is_wow64():
        rep.check("warn", "python", "32-bit Python on 64-bit Windows",
                  "install a 64-bit Python; until then ferry runs Sysnative\\OpenSSH\\ssh.exe")
    if python_tuple() < (3, 11):
        rep.check("warn", "python", "ferry is tested with Python 3.11 or later on Windows",
                  "install a newer Python")


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
    # a broken or clashing channels.d/ file, a retired [mailbox]: the other jobs still run
    for skip in cfg.skipped:
        rep.check("warn", "config", skip.line, skip.error.hint)


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
        # ssh-add itself is missing: no agent question can be answered (review W3)
        rep.check("warn", "agent", text, ssh.START_HINT)
        return
    dest = dests[0].dest if len(dests) == 1 else "<dest>"
    if state_ == "empty":
        rep.check("warn", "agent", text, "a key with a passphrase works only once it's in the "
                  "agent: run ferry key %s" % dest)
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


def _dirs(rep):
    folders = (platform.state_dir(), platform.log_dir())
    good = True
    for folder in folders:
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".ferry-doctor-", suffix=".tmp")
            os.close(fd)
            os.remove(tmp)
        except OSError as e:
            good = False
            rep.check("FAIL", "dirs", "can't write in %s: %s" % (folder, e.strerror or e),
                      "fix its permissions, or set FERRY_HOME")
    if good:
        rep.check("ok", "dirs", "state %s, logs %s" % folders)


def _machine(rep):
    """This machine's id (M11d), so an agent checking it needs no python3 -c. Only a warn
    without one: a client of remote jobs needs none."""
    mid = platform.machine_id()
    if mid:
        rep.check("ok", "machine", mid)
        return
    hint = platform.no_machine_hint() or "give it one: systemd-machine-id-setup, as root"
    rep.check("warn", "machine", "no machine id: this machine can't hold a channel (--local), "
              "nor keep jobs' state as a server", hint)


# --- a destination (decision 19) ---

def utc_text(offset):
    """Seconds east of UTC as UTC+hh:mm."""
    sign = "-" if offset < 0 else "+"
    minutes = abs(int(offset)) // 60
    return "UTC%s%02d:%02d" % (sign, minutes // 60, minutes % 60)


def clock_checks(hello, received, local_offset):
    """[(level, text, hint)]: the server's clock and time zone against this machine's (M12b),
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
        # DESIGN §6.4's third row
        rep.check("FAIL", dest, "ssh can't use your key %s: the server accepts it, but it's "
                  "locked by a passphrase" % ssh.shown(key.ident), "run: ferry key %s" % dest)
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
    for level, text, hint in clock_checks(hello, session.hello_received,
                                          time.localtime().tm_gmtoff):
        rep.check(level, dest, text, hint)
    if session.junk_bytes:
        rep.check("warn", dest, "the server's shell printed %d bytes before ferry started"
                  % session.junk_bytes, "make its startup files print nothing when the shell "
                  "isn't interactive; the log has the text")
    if hello.get("os") != "linux":
        # M11a: a Mac or Windows box has a machine id now; it still isn't a server
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


# --- a job (decisions 20, 21) ---

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
                # the closed channel's hint, as ferry run shows it (M10)
                from . import cli
                fix = cli.channel_gone_hint(job, "not_found", fix) or fix
            elif job.mailbox is not None and role == "source":
                level, message, fix = _made_by_the_run(job, level, message, fix)
            rep.check(level, name, message, fix)


def _made_by_the_run(job, level, message, fix):
    """A channel section's own folder on the client may not exist before its first run: the
    run makes it while up has sent nothing (DESIGN §12). The server's tree is never made by a
    run since M10: its absence is a closed channel."""
    if not (level == "FAIL" and message.startswith("from.path ")
            and message.endswith(" doesn't exist")) or job.source.end == "remote":
        return level, message, fix
    section = job.mailbox.section
    try:
        saved = state.load(section + ".up")
    except VCharonError:
        saved = None
    if saved is not None and isinstance(saved.source, dict) and saved.source.get("sent"):
        return (level, "%s, but %s.up has sent files from it" % (message, section),
                "restore the folder; if it's meant to be gone: ferry state reset %s.up"
                % section)
    return "ok", "%s yet; ferry run %s makes it" % (message, section), None


# --- the command ---

def main(args, run):
    """ferry doctor [<target>]: exit 0 when no check failed, else 1 (decisions 16-22 of the
    M5 plan)."""
    started = time.monotonic()
    log = run.log = Log(os.path.join(platform.log_dir(), "ferry.log"), console=args.verbose)
    target = args.target
    try:
        cfg, cfg_err = config.load(args.config), None
    except VCharonError as e:
        # a FAIL line, not exit 3; a target is then a destination with the default settings
        cfg, cfg_err = None, e
    skip = None
    if target is not None and cfg is not None and cfg.named(target) is None:
        skip = cfg.skipped_for(target)
    # a skipped section's own config error is a FAIL line, and nothing is in scope
    dests = _scope(target, cfg) if skip is None else []
    base = cfg.settings if cfg is not None else config.Settings()
    log.info("ferry %s doctor%s; Python %s (%s) on %s; config %s"
             % (VERSION, " " + target if target is not None else "",
                platform.python_version(), sys.executable, platform.os_name(),
                cfg.path if cfg is not None else "broken"))

    def say(line):
        print(line)
        sys.stdout.flush()
        log.info(line)

    subjects = list(CLIENT_SUBJECTS) + [d.dest for d in dests] + [job.name for d in dests
                                                                 for job in d.jobs]
    rep = Report(say, subjects)
    say("ferry: doctor%s" % (" " + target if target is not None else ""))
    _python(rep)
    _config(rep, cfg, cfg_err, not dests and skip is None)
    if skip is not None:
        rep.check("FAIL", "config", skip.error.message, skip.error.hint)
    _ssh(rep, base)
    agent = keys.agent_state(base)
    _agent(rep, agent, dests)
    _dirs(rep)
    _machine(rep)
    locked = []
    for d in dests:
        logged_in, is_locked = _login(rep, d, agent, log)
        if is_locked:
            locked.append(d)
        # ControlMaster off, as in the probe (DESIGN §6.1)
        with ssh.Session(d.settings, d.dest, log, probe=True) as session:
            hello = _server(rep, d, session) if logged_in else None
            for job in d.jobs:
                _job(rep, job, session if hello is not None else None, hello, log)
    say(rep.last_line(time.monotonic() - started))
    code = 1 if rep.failed else 0
    _offer(locked, log, say, run)
    return code


def _offer(locked, log, say, run):
    """In a terminal, offers to run ferry key for each destination whose key is locked
    (DESIGN §6.5); the default is no. The doctor's exit code stays its own."""
    for d in locked:
        if not keys.terminal():
            return
        try:
            answer = input("run ferry key %s now? [y/N] " % d.dest)
        except EOFError:
            continue
        yes = answer.strip().lower() in ("y", "yes")
        # never the answer's text: it's typed on the terminal
        log.info("offer to run ferry key %s: %s" % (d.dest, "yes" if yes else "no"))
        if not yes:
            continue
        try:
            keys.unlock(d.settings, d.dest, None, log, say)
        except VCharonError as e:
            run.show_error(e)
