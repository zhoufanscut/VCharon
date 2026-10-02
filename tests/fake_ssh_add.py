"""Stands in for ssh-add in tests, so no test ever reaches the real agent."""

# It never reads stdin: no test types a passphrase. Environment knobs:
#
# | variable               | effect                                                        |
# |------------------------|---------------------------------------------------------------|
# | FAKE_SSH_ADD_LIST      | -l prints this (default: nothing)                             |
# | FAKE_SSH_ADD_L_RC      | -l exits with this code (default 1: the agent has no keys)    |
# | FAKE_SSH_ADD_ARGV_LOG  | any other call is an add: append sys.argv[1:] there as one    |
# |                        | JSON line                                                     |
# | FAKE_SSH_ADD_RC        | an add exits with this code (default 0)                       |
# | FAKE_SSH_UNLOCK_FILE   | after an add that exits 0, create this file: fake_ssh.py then |
# |                        | logs in (the key is unlocked now)                             |

import json
import os
import sys


def main():
    argv = sys.argv[1:]
    env = os.environ
    if argv == ["-l"]:
        text = env.get("FAKE_SSH_ADD_LIST", "")
        if text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return int(env.get("FAKE_SSH_ADD_L_RC", "1"))
    if env.get("FAKE_SSH_ADD_ARGV_LOG"):
        with open(env["FAKE_SSH_ADD_ARGV_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\n")
    rc = int(env.get("FAKE_SSH_ADD_RC", "0"))
    if rc == 0 and env.get("FAKE_SSH_UNLOCK_FILE"):
        with open(env["FAKE_SSH_UNLOCK_FILE"], "w", encoding="utf-8") as f:
            f.write("unlocked\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
