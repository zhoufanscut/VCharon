# Changelog

What changed in each version of VCharon, and what was checked on which OS: each check is marked
**measured** (it ran, and the result was seen) or **inferred** (reasoned from the code or from
docs, not run). How it works now is [DESIGN.md](DESIGN.md).

Versions follow semver. Before 1.0, a minor version may change something DESIGN.md lists under
"Stable"; its entry here says what and how to adapt.

## 0.1.0 — unreleased

The first version.

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
- A channel over real ssh (a remote member syncing with a Debian 13 server): **not yet run.**
- Claude Code's Monitor tool, Codex and OpenCode as members, and any agent on macOS or Windows:
  **not yet run.**
- The default folder and entry limits fit real channels: **not measured**; a guess.
- How many streaming watchers one server takes before sshd refuses connections: **inferred**
  from sshd's defaults (`MaxStartups 10:30:100`, `MaxSessions 10`), not measured.
- The standalone binaries, `install.sh`, `install.ps1` and `vcharon --update`: **not built yet.**
