"""vcharon key: unlock a passphrase key into the OS's own agent or keychain (DESIGN, "Keys without
prompts"); client.

vcharon never reads, stores or logs a passphrase: ssh-add asks for it on the terminal. It never
writes in ~/.ssh either: on macOS it prints the config lines to add.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time

from . import fsops, platform, ssh
from .proto import VCharonError

KEY_EXAMPLE = "for example: vcharon key devbox"
ADMIN_HINT = ("once, in an admin PowerShell: Get-Service ssh-agent | Set-Service -StartupType "
              "Automatic; Start-Service ssh-agent")
START_AGENT = 'eval "$(ssh-agent -s)"'
APPLE_SSH = "/usr/bin/ssh"


def terminal():
    """True when stdin and stdout are a terminal: only then may vcharon key prompt (DESIGN,
    "Launch rules"). Tests patch it."""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def agent_state(settings):
    """(state, n): ("keys", n) when the agent answers with n keys, ("empty", 0) when it holds
    none, ("none", 0) when no agent answers (ssh-add -l exits 2, or another code, or times
    out), and ("error", <why>) when ssh-add can't be started."""
    argv = ssh.ssh_add_prefix(settings) + ["-l"]
    try:
        ran = fsops.run(argv, timeout=10)
    except OSError as e:
        return "error", "couldn't start %s: %s" % (argv[0], e.strerror or e)
    if ran.rc == 0:
        lines = [line for line in ran.out.decode("utf-8", "replace").splitlines()
                 if line.strip()]
        return "keys", len(lines)
    if ran.rc == 1:
        return "empty", 0
    return "none", 0


def no_agent_why():
    """Why no agent answers, as the agent line shows it."""
    if platform.os_name() == "windows":
        return "the ssh-agent service isn't running"
    sock = os.environ.get("SSH_AUTH_SOCK")
    if not sock:
        return "SSH_AUTH_SOCK is unset"
    return "nothing answers at %s" % sock


def agent_text(state, n):
    if state == "error":
        # n is the reason
        return n
    if state == "keys":
        return "holds %d key%s" % (n, "" if n == 1 else "s")
    if state == "empty":
        return "holds no keys"
    return "none: %s" % no_agent_why()


def no_key_hint(dest, state):
    """The hint for "accepts none of your keys": a forwarded
    agent that ended with its login looks just like a missing key."""
    sock = os.environ.get("SSH_AUTH_SOCK")
    if platform.os_name() != "windows" and sock and state == "none":
        return "no agent answers at %s: a forwarded agent ends with its ssh login; log in again" \
            % sock
    return "add your public key to ~/.ssh/authorized_keys on %s" % dest


def no_key_error(dest, state):
    return VCharonError("connect", "the server %s accepts none of your keys (Permission denied)"
                        % dest, hint=no_key_hint(dest, state))


def host_of(dest):
    """The host of a destination, for a Host line of ~/.ssh/config: no ssh://, no user@, no
    brackets around an address, no :port of an ssh:// destination."""
    url = dest.startswith("ssh://")
    host = dest[len("ssh://"):] if url else dest
    host = host.rpartition("@")[2]
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end >= 0 else host[1:]
    if url:
        name, colon, port = host.rpartition(":")
        if colon and port.isdigit():
            return name
    return host


def show_argv(argv):
    """An argument list as the run line shows it, quoted the way this OS's shell reads it."""
    argv = [ssh.shown(arg) for arg in argv]
    if platform.os_name() == "windows":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def key_text(key):
    ident = ssh.shown(key.ident)
    return "%s (%s)" % (ident, key.type) if key.type else ident


def _config_lines(dest, key_file):
    """The lines to add at the top of ~/.ssh/config on macOS; printed, never written. After a
    reboot the agent is empty, and ssh must know which file to unlock through the
    Keychain."""
    # printed, never written: the display form
    path = ssh.shown(key_file)
    path = '"%s"' % path if " " in path else path
    return ["IgnoreUnknown UseKeychain",
            "Host %s" % (host_of(dest) if dest else "*"),
            "    IdentityFile %s" % path,
            "    UseKeychain yes",
            "    AddKeysToAgent yes"]


def needs_terminal():
    """vcharon key's refusal when stdin and stdout aren't a terminal."""
    return VCharonError("config", "vcharon key needs a terminal: ssh-add asks for your passphrase "
                        "there", hint="run it in a terminal window")


def _tool(settings, key_file, agent, say, log):
    """Runs the OS's own tool on the terminal to unlock key_file.
    agent: agent_state()'s answer."""
    state = agent[0]
    if state == "error":
        raise VCharonError("config", agent[1], hint=ssh.START_HINT)
    osn = platform.os_name()
    add = ssh.ssh_add_prefix(settings)
    if osn == "darwin":
        if settings.ssh_path != APPLE_SSH:
            say("  warn    only Apple's ssh (%s) reads the Keychain; ssh_path is %s"
                % (APPLE_SSH, settings.ssh_path))
        argv = add + ["--apple-use-keychain", key_file]
    elif osn == "windows":
        if state == "none":
            raise VCharonError("config", "the ssh-agent service isn't running", hint=ADMIN_HINT)
        argv = add + [key_file]
    else:
        sock = os.environ.get("SSH_AUTH_SOCK")
        if not sock:
            raise VCharonError("config", "there's no ssh agent here: SSH_AUTH_SOCK is unset",
                               hint="start one in this shell: %s, then run vcharon key again"
                               % START_AGENT)
        if state == "none":
            raise VCharonError("config", "no agent answers at %s" % sock,
                               hint="a forwarded agent ends with its ssh login; log in again, or "
                               "start one: %s" % START_AGENT)
        argv = add + [key_file]
    say("  run     %s" % show_argv(argv))
    log.info("running ssh-add on the terminal; vcharon never sees the passphrase")
    try:
        rc = fsops.run_terminal(argv)
    except OSError as e:
        raise VCharonError("config", "couldn't start %s: %s" % (argv[0], e.strerror or e),
                           hint=ssh.START_HINT)
    log.info("ssh-add exited with code %s" % rc)
    if rc != 0:
        raise VCharonError("config", "ssh-add didn't add %s (exit %s)" % (ssh.shown(key_file), rc),
                           hint="check the passphrase, then run vcharon key again")


def _agent_only(key, say):
    say("  key     %s: only the agent holds it; there's no key file here to unlock"
        % key_text(key))
    say("  note    runs work while that agent holds the key")
    if platform.os_name() == "linux":
        say("  note    an agent forwarded by ssh -A or ForwardAgent lasts only while the ssh "
            "login that brought it is open; for cron, use a key without a passphrase on this "
            "machine (README.md, \"Keys\")")


def unlock(settings, dest, key_file, log, say):
    """vcharon key: finds the key, unlocks it with the OS's
    own tool when it's locked, and tests a BatchMode login to dest. Returns 0 or raises.
    dest may be None when key_file is given; key_file is absolute, or None to find the key
    with a -v probe."""
    # cli checks it too; any other caller gets the same refusal
    if not terminal():
        raise needs_terminal()
    started = time.monotonic()
    say("vcharon: key %s" % (dest or ssh.shown(key_file)))
    agent = agent_state(settings)
    state = agent[0]
    say("  agent   %s" % agent_text(*agent))
    osn = platform.os_name()
    tool = None
    if key_file is not None:
        say("  key     %s: given with --key" % ssh.shown(key_file))
        tool = key_file
    else:
        probe = ssh.verbose_probe(settings, dest, log)
        ok = probe.rc == 0
        if not ok and not ssh.denied(probe):
            # host key, network, no Python, a timeout: nothing a key can fix
            raise probe.error
        key = ssh.probe_key(probe)
        kind = ssh.key_kind(key) if key is not None else None
        if key is None:
            if not ok:
                raise no_key_error(dest, state)
            say("  key     none: %s logs in without a key" % dest)
        elif kind == "agent":
            _agent_only(key, say)
        elif kind == "file" and not ok:
            say("  key     %s: the server accepts it, but it's locked" % key_text(key))
            tool = key.ident
        elif not ok:
            raise VCharonError("connect", "ssh couldn't log in to %s with %s"
                               % (dest, ssh.shown(key.ident)), hint="see the log")
        elif kind == "file+agent":
            if osn == "darwin":
                # into the Keychain, so it survives a reboot
                say("  key     %s: in the agent; saving its passphrase in the Keychain"
                    % key_text(key))
                tool = key.ident
            elif osn == "windows":
                say("  key     %s: already in the ssh-agent service, which keeps it across "
                    "reboots" % key_text(key))
            else:
                say("  key     %s: already in the agent, for as long as that agent runs"
                    % key_text(key))
        elif kind == "file":
            say("  key     %s: it has no passphrase; nothing to unlock" % key_text(key))
        else:
            say("  key     %s: ssh logs in with it; nothing to unlock" % key_text(key))
    if tool is not None:
        _tool(settings, tool, agent, say, log)
        if osn == "darwin":
            say("  config  add these lines at the top of ~/.ssh/config:")
            for line in _config_lines(dest, tool):
                say("            %s" % line)
    if dest:
        try:
            with ssh.Session(settings, dest, log, probe=True) as session:
                session.open()
        except VCharonError:
            say("  test    FAIL")
            raise
        say("  test    ok: vcharon logs in to %s with no prompt" % dest)
    say("OK  (%.1f s)" % (time.monotonic() - started))
    return 0
