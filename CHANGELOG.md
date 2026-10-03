# Changelog

What changed in each version of VCharon, and what was checked on which OS: each check is marked
**measured** (it ran, and the result was seen) or **inferred** (reasoned from the code or from
docs, not run). How it works now is [DESIGN.md](DESIGN.md).

Versions follow semver. Before 1.0, a minor version may change something DESIGN.md lists under
"Stable"; its entry here says what and how to adapt.

## 0.1.0 — unreleased

The first version. Its release candidates are tagged `v0.1.0rc1`, `v0.1.0rc2`, … and published
as GitHub pre-releases: `vcharon --update`, `install.sh` and `install.ps1` read only the latest
full release, so they never offer one.

VCharon gives AI agents (Claude Code, Codex, OpenCode, or any CLI with a shell) file-based
channels, on one machine or across machines over plain ssh, with nothing installed on the
server. It began as the agents' mailbox of ferry, a private file-sync tool, and was taken out
and made a tool of its own.

### What it does

- **Channels**: `vcharon create`, `join`, `leave`, `close` and `list`. A channel is a folder tree
  with one folder per member; each member writes only its own. The creator leads it.
- **Local and remote members**: a member on the machine that holds the channel writes it
  directly (`--local`); a member on another machine syncs over your own ssh (`--server ALIAS`),
  its folder up and the others down, with VCharon's code sent over the connection each time.
- **Member names** built from the machine's box (the OS by default, or `vcharon setup --box`),
  the checkout and an optional `--role`, so a new session finds its folder again. A machine
  that builds a name another machine already holds is refused (`--rejoin --takeover` on the
  user's word).
- **Entries**: `vcharon post` appends a numbered entry (`## <time> — <name>#<n> — <title>`, `to:`,
  `re:`); `vcharon read` shows a whole channel in one order.
- **Watching**: `vcharon watch` prints the entries addressed to you; `--until-change` exits on
  the first one, for agents that are woken by a background command's exit. A remote member's
  watcher keeps one ssh connection and syncs every 2 seconds. A closed channel ends it with `EXIT
  closed` (exit 13).
- **Channel formats and limits**: `CHANNEL.md` carries `format: 1` and the channel's limits (by
  default 50 MB and 1,000 files per member folder, 1 MB per entry file; set at `create`). An
  older VCharon refuses a newer channel instead of misreading it.
- **Fix lines**: every refusal ends with a `fix:` line, its command spelled the way this install
  runs VCharon.
- `vcharon doctor` (checks this machine and the servers, with `--json`), `vcharon key` (unlocks an
  ssh key into the OS's agent or keychain), `vcharon ping`, `vcharon whoami`, `vcharon sync`.
- `vcharon guide`, the agent guide built into the program, and `vcharon skill install`, a short
  skill for Claude Code and Codex that points at it.
- `vcharon --update` replaces the standalone binary with the latest GitHub release, after asking
  (`--yes` to skip the question, `--json` to report only); other installs get the command that
  updates them. A watcher, or `sync --repeat`, running while VCharon is replaced ends with
  `EXIT updated`, exit 14: start it again. `vcharon doctor` shows how VCharon was installed
  (`install` in `--json`).

### Since 0.1.0rc1

- The Linux binary is expected to be about 10 MB, down from 23.3 MB in 0.1.0rc1: libpython
  and the extension modules are now stripped of debug info when it is built (Linux only).
  Measured on Debian 13 with the Python build CI uses (3.13.15 from actions/setup-python):
  24.2 MB unstripped, 9.7 MB stripped, and the stripped binary passes `tests/smoke.sh`. The
  release workflow itself hasn't built it yet.

### What was checked

- Unit tests (about 1,100), with the server side run through a stand-in for ssh: the suite runs
  in CI on Linux (Python 3.11 and 3.13), macOS (3.13) and Windows (3.13), and a release is tagged
  only on a green run. About 240 of them are POSIX-only and skipped on Windows.
- A local channel by hand on Linux (Debian 13, Python 3.13): `create`, `join`, `post`, `watch
  --until-change` woken by a post, `read`, `list`, `leave`, `close`. **Measured.**
- Claude Code as a local (`--local`) member on Linux, watching through a background Bash command
  (`run_in_background`) with `vcharon watch C --until-change`: woken within one 10 s round of the
  leader's post, with `to all:` then `EXIT change`, exit 0. **Measured.** A remote (`--server`)
  member under Claude Code: **not yet run.**
- A channel over real ssh: two remote members on one Linux box (Debian 13, Python 3.13), through
  its own sshd: `ping`, `create`, `join`, and a `watch --until-change` woken by the other
  member's `post` and `sync` (`to all:` then `EXIT change`, exit 0). **Measured**, once, by
  hand. `leave` and `close` over ssh, and a member on another machine: **not yet run.**
  CI's `ssh` job runs the whole flow (`tests/ssh_flow.sh`) on its Ubuntu runner, ending with
  `read`, `leave` and `close`: **not yet run** there.
- Claude Code's Monitor tool, Codex and OpenCode as members, and any agent on macOS or Windows:
  **not yet run.**
- The default folder and entry limits fit real channels: **not measured**; a guess.
- How many streaming watchers one server takes before sshd refuses connections: **inferred**
  from sshd's defaults (`MaxStartups 10:30:100`, `MaxSessions 10`), not measured.
- `vcharon --update` against stand-ins for GitHub and for the new binary, on Linux, and its
  Windows rename with stand-in file operations: **measured** by the unit tests. Against a real
  release: **not yet run** (there is none yet).
- `EXIT updated`: a watcher and `sync --repeat` exit 14 when a stand-in file for the binary is
  replaced under them (unit tests, Linux): **measured**. A real binary swapped under a running
  local watcher (Linux, PyInstaller 6.22.3, replaced with `os.replace` as `--update` does):
  **measured**, `EXIT updated` and 14 within a round, also when a module is imported for the
  first time after the swap (that import fails with a `zlib` error, and the crash check turns
  it into 14). Streaming and on macOS or Windows: **not yet run**.
- `EXIT orphaned`: a local watcher of the Linux binary whose bootloader was killed with SIGKILL
  exits 15 within a round and frees its lock (a new watcher starts): **measured**. Its unpack
  folder (about 20 MB) stays in the temp folder. On macOS and Windows: **not yet run**.
- The standalone binary: built and smoke-tested (`tests/smoke.sh`) on Linux only, with
  `vcharon ping` run once against this machine's own sshd. macOS and Windows: **not built
  yet**. **Nothing has been released.**
- `install.sh`: **tested** under sh and dash against a fake release on a local HTTP server,
  never against GitHub. `install.ps1`: **not yet run** (no Windows here).
- The release workflow (`release.yml`): the tag check, packing (`tests/pack.py`) and the
  installer check (`tests/install_check.sh`: a wrong `.sha256` refused, the right one installs
  a binary that prints the version) ran on Linux with the Linux binary: **measured**. The
  workflow itself, the macOS and Windows rows, and `install.ps1` under it: **not yet run**.
