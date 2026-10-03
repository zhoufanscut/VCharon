"""Stands in for ssh in tests: runs the bootstrap line with this Python and relays its bytes."""

# -V as the only argument prints a version line to stderr and exits 0, as ssh -V does.
# Otherwise it ignores every argument except the last one, which must match
# (\S+) -I -c '([^']*)'; if not, it prints an error and exits 2. Environment knobs, in the
# order it applies them:
#
# | variable              | effect                                                         |
# |-----------------------|----------------------------------------------------------------|
# | FAKE_SSH_ARGV_FILE    | write sys.argv[1:] there as JSON                               |
# | FAKE_SSH_ARGV_LOG     | append sys.argv[1:] there as one JSON line per run             |
# | FAKE_SSH_UNLOCK_FILE  | when set and that file exists, skip STALL, STDERR, EAT_STDIN   |
# |                       | and EXIT: the key is unlocked now (fake_ssh_add.py makes it)   |
# | FAKE_SSH_STALL        | sleep this many seconds, reading nothing                       |
# | FAKE_SSH_JUNK_B64     | write these bytes to stdout (a noisy shell startup file)       |
# | FAKE_SSH_STDERR       | write this text to stderr                                      |
# | FAKE_SSH_EAT_STDIN    | read and drop this many stdin bytes (a startup file that reads |
# |                       | stdin)                                                         |
# | FAKE_SSH_EXIT         | exit with this code now, before starting Python                |
# | FAKE_SSH_HOME         | HOME, USERPROFILE and the current directory for the helper;    |
# |                       | default: a new temp dir, removed at exit                       |
# | VCHARON_TEST_MACHINE_ID | passed on; default 0123456789abcdef0123456789abcdef            |
# | VCHARON_TEST_OS         | passed on; default linux, the os the helper's hello names      |
#
# The rest of the environment reaches the helper as it is, VCHARON_TEST_CLOCK_SHIFT and
# VCHARON_TEST_UTC_OFFSET among it (the hello's clock and zone, M12b), and
# VCHARON_TEST_OS_RELEASE (the file the hello reads in place of /etc/os-release).
#
# It then runs [sys.executable, "-I", "-c", code] with its own stdin and stdout pipes and relays
# bytes both ways, the way ssh does: a thread copies fd 0 to the child and closes the child's
# stdin at end of file; the main thread copies the child's stdout to fd 1. The child's stderr is
# ours. It exits with the child's exit code. Killing it closes the controller's pipes at once,
# and the child then sees end of file on stdin and exits. It also sets PYTHONPATH=<home> for the
# child, which -I must ignore.

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

TEST_MACHINE_ID = "0123456789abcdef0123456789abcdef"


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def relay_stdin(child_stdin):
    # Like ssh: copy our stdin to the remote command, and close its stdin at end of file.
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            write_all(child_stdin.fileno(), data)
    except OSError:
        pass
    finally:
        try:
            child_stdin.close()
        except OSError:
            pass


def main():
    argv = sys.argv[1:]
    if argv == ["-V"]:
        sys.stderr.write("OpenSSH_fake 1.0, for vcharon's tests\n")
        return 0
    m = re.fullmatch(r"(\S+) -I -c '([^']*)'", argv[-1]) if argv else None
    if not m:
        sys.stderr.write("fake_ssh: the last argument isn't vcharon's remote command: %r\n"
                         % argv[-1:])
        return 2
    code = m.group(2)
    env = os.environ
    if env.get("FAKE_SSH_ARGV_FILE"):
        with open(env["FAKE_SSH_ARGV_FILE"], "w", encoding="utf-8") as f:
            json.dump(argv, f)
    if env.get("FAKE_SSH_ARGV_LOG"):
        with open(env["FAKE_SSH_ARGV_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\n")
    unlocked = bool(env.get("FAKE_SSH_UNLOCK_FILE")) and os.path.exists(env["FAKE_SSH_UNLOCK_FILE"])
    if env.get("FAKE_SSH_STALL") and not unlocked:
        time.sleep(float(env["FAKE_SSH_STALL"]))
    if env.get("FAKE_SSH_JUNK_B64"):
        write_all(1, base64.b64decode(env["FAKE_SSH_JUNK_B64"]))
    if env.get("FAKE_SSH_STDERR") and not unlocked:
        text = env["FAKE_SSH_STDERR"]
        sys.stderr.write(text if text.endswith("\n") else text + "\n")
        sys.stderr.flush()
    if env.get("FAKE_SSH_EAT_STDIN") and not unlocked:
        left = int(env["FAKE_SSH_EAT_STDIN"])
        while left > 0:
            data = os.read(0, min(left, 65536))
            if not data:
                break
            left -= len(data)
    if env.get("FAKE_SSH_EXIT") and not unlocked:
        return int(env["FAKE_SSH_EXIT"])
    home = env.get("FAKE_SSH_HOME")
    made_home = not home
    if made_home:
        home = tempfile.mkdtemp(prefix="fake-ssh-home-")
    try:
        # PYTHONPATH points at the home, where tests plant modules; -I must ignore it.
        child_env = dict(env, HOME=home, USERPROFILE=home, PYTHONPATH=home,
                         VCHARON_TEST_MACHINE_ID=env.get("VCHARON_TEST_MACHINE_ID",
                                                         TEST_MACHINE_ID),
                         VCHARON_TEST_OS=env.get("VCHARON_TEST_OS", "linux"))
        # Relay rather than hand our pipes down, the way ssh does: killing us then closes the
        # controller's pipes at once, and the child sees end of file on stdin and exits.
        child = subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, cwd=home, env=child_env, bufsize=0)
        threading.Thread(target=relay_stdin, args=(child.stdin,), daemon=True).start()
        out = child.stdout.fileno()
        try:
            while True:
                data = os.read(out, 65536)
                if not data:
                    break
                write_all(1, data)
        except OSError:
            pass
        # Nobody may read it now; don't let the child block on a full pipe.
        child.stdout.close()
        return child.wait()
    finally:
        if made_home:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
