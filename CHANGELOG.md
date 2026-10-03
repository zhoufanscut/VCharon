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
- **Entry headings carry seconds**: `## 2026-10-02 10:12:05 — mac-web#7 — title` (was
  `10:12`), and so do `read`'s time column, its `--json` `"time"` and `CHANNEL.md`'s `created:`.
  `read` orders the entries of one minute by their second, then by name and number; one
  member's numbers and `re:` still go first. Headings without seconds are still read. The
  channel format stays 1. How to adapt: update every member of a channel. A 0.1.0rc1 member's
  `read` lists the new entries first with a `bad time` note, so its `read --last N` leaves them
  out of its N (measured against 0.1.0rc1's code); its `watch` and `post` are unaffected
  (inferred from the code). A script that parses the time takes both forms.
- **A remote member's `post` sends the entry at once** (its up job) and prints `sent to
  <server>`; `--no-sync` leaves it to the watcher. If a watcher's sync is running it prints a
  `note:`; if the send fails, `WARN not sent to <server>: …` and a `fix:` line. The post exits 0
  in every case: the entry is saved.
- **`post --to name`** without the `@` works when `name` is a member of the channel; any other
  bare name is refused, listing the members. `--re` takes an `@` in front of the ID.
- **ssh failing before VCharon starts** (exit 255, no known cause): the error now ends with
  ssh's last stderr line, `(ssh: kex_exchange_identification: read: Connection reset by peer)`
  say, and the fix says to try again or run `ssh <server>` in a terminal. It said "see ssh's
  messages above", which a watcher never showed.
- **`leave` of a closed channel, and `close`,** end with `note    nothing of <C> as <name> is
  left on this machine`, naming any other membership of the channel on this machine that stays
  (`still here: …`); the guide's end topic lists what `leave` removes and says not to delete
  anything by hand.
- **`doctor` on the standalone binary**: the `python` line reads `3.13.x, bundled in <binary>,
  on <os>`.
- Docs: the README says what Windows Defender's false positive on `vcharon.exe` looks like (only
  `[PYI-…:ERROR] Could not load PyInstaller's embedded PKG archive …`) and how the user restores
  and allows it, and how to install from a release's assets by hand (the macOS quarantine flag
  only if present). The guide: `setup` writes the config on its first run; Claude Code reports a
  watcher's `EXIT quiet` (exit 10) as "failed with exit code 10", which only means restart it.
- **Python 3.13 or later**, on both ends (was 3.11): a server whose `python3` is 3.11 or 3.12
  is now refused at connect (`the server's python3 is 3.12; vcharon needs 3.13 or later`, exit
  4), and pipx, uv and a source checkout need 3.13. The standalone binaries carry their own 3.13
  and are unaffected. How to adapt: install Python 3.13 on the server (Debian 13's is 3.13), or
  point `remote_python` in `vcharon.ini` at a 3.13 there; install VCharon from source with a 3.13.
  Why: the binaries bundle 3.13 and the server VCharon targets ships it, so only a source
  install meets the floor on a client. The floor isn't in DESIGN's "Stable" list.

### What was checked

- Unit tests (about 1,100), with the server side run through a stand-in for ssh: the suite runs
  in CI on Linux, macOS and Windows (Python 3.13; Linux also ran 3.11 until the floor rose), and
  a release is tagged only on a green run. About 240 of them are POSIX-only and skipped on Windows.
- A local channel by hand on Linux (Debian 13, Python 3.13): `create`, `join`, `post`, `watch
  --until-change` woken by a post, `read`, `list`, `leave`, `close`. **Measured.**
- Claude Code as a local (`--local`) member on Linux, watching through a background Bash command
  (`run_in_background`) with `vcharon watch C --until-change`: woken within one 10 s round of the
  leader's post, with `to all:` then `EXIT change`, exit 0. **Measured.**
- **A real channel with the 0.1.0rc1 binaries** (measured by the leader, 2026-10-03). The server:
  Debian 13.7, Python 3.13.5, default sshd settings; the channel over ssh. The members: a Linux
  (Debian 13) remote leader; a local member on the server (Claude Code); a remote member on
  macOS 27.0.1 arm64 (Claude Code); a remote member on Windows 11 Pro 10.0.26200 under Git Bash
  (Claude Code with a third-party model). `create`, three joins, posts to named members and to
  `@all` with replies, `read`, `close`; every member watched with Claude Code's background Bash
  and `watch --until-change`, and all three members' watchers ended with `EXIT closed` after the
  close (as their user reported). **Measured:**
  - from the leader's sync to a member's watcher line: about 1 s for the macOS and Windows remote
    members, about 5 s for the local member (its 10 s round); a remote join took 1–2 s;
  - Windows Defender quarantined `vcharon-win-x64.exe` (`Trojan:Win32/Bearfoos.A!ml`) until the
    user allowed it;
  - watchers on one server: 10, 20, 30 and 50 watchers started at the same moment from one
    machine had 0, 3, 7 and at least 16 ssh connections reset at start
    (`kex_exchange_identification: … Connection reset by peer`); every one retried and was
    streaming within about 4 s. With 50 watchers the server held 50 helpers at about 24.5 MB
    each (1.2 GB) on a 4-CPU, 3.7 GB machine, load 0.79. The resets' cause is **inferred**:
    sshd's `MaxStartups` (the server's log wasn't read).
  The fixes under "Since 0.1.0rc1" have **not yet run** in a real channel.
- A channel over real ssh: two remote members on one Linux box (Debian 13, Python 3.13), through
  its own sshd: `ping`, `create`, `join`, and a `watch --until-change` woken by the other
  member's `post` and `sync` (`to all:` then `EXIT change`, exit 0). **Measured**, once, by
  hand. `leave` over ssh: **not yet run** by hand.
  CI's `ssh` job runs the whole flow (`tests/ssh_flow.sh`) on its Ubuntu runner, ending with
  `read`, `leave` and `close`, then the real-ssh unit tests: **measured**, green at 0.1.0rc1
  (CI run 37113763415).
- Claude Code's Monitor tool, and Codex and OpenCode as members: **not yet run.**
- The default folder and entry limits fit real channels: **not measured**; a guess.
- How many streaming watchers one server takes: 50 at once ran (above); the limit itself is
  **not measured**.
- `vcharon --update` against stand-ins for GitHub and for the new binary, on Linux, and its
  Windows rename with stand-in file operations: **measured** by the unit tests. Against a real
  release: **not yet run** (only a pre-release exists, which `--update` never offers).
- `EXIT updated`: a watcher and `sync --repeat` exit 14 when a stand-in file for the binary is
  replaced under them (unit tests, Linux): **measured**. A real binary swapped under a running
  local watcher (Linux, PyInstaller 6.22.3, replaced with `os.replace` as `--update` does):
  **measured**, `EXIT updated` and 14 within a round, also when a module is imported for the
  first time after the swap (that import fails with a `zlib` error, and the crash check turns
  it into 14). Streaming and on macOS or Windows: **not yet run**.
- `EXIT orphaned`: a local watcher of the Linux binary whose bootloader was killed with SIGKILL
  exits 15 within a round and frees its lock (a new watcher starts): **measured**. Its unpack
  folder (about 20 MB) stays in the temp folder. On macOS and Windows: **not yet run**.
- The standalone binary: 0.1.0rc1 was built by the release workflow for Linux, macOS and
  Windows, smoke-tested (`tests/smoke.sh`) on each runner, and published as a GitHub
  pre-release; the Linux binary, fetched with `gh release download`, passed the smoke test on a
  Debian 13 machine too. **Measured.**
- `install.sh`: **tested** under sh and dash against a fake release on a local HTTP server,
  never against GitHub. `install.ps1`: against a fake release on CI's Windows runner (below),
  never against GitHub.
- The release workflow (`release.yml`): the tag check, packing (`tests/pack.py`) and the
  installer check (`tests/install_check.sh`: a wrong `.sha256` refused, the right one installs
  a binary that prints the version) ran on Linux with the Linux binary: **measured**. The
  workflow ran green for the `v0.1.0rc1` tag on all three rows (release run 37114158991), its
  installer check included: `install.sh` on Linux and macOS, `install.ps1` on Windows, each
  against a fake release on the runner: **measured** by CI.
