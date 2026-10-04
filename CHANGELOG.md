# Changelog

What changed in each version of VCharon, and what was checked on which OS: each check is marked
**measured** (it ran, and the result was seen) or **inferred** (reasoned from the code or from
docs, not run). How it works now is [DESIGN.md](DESIGN.md).

Versions follow semver. Before 1.0, a minor version may change something DESIGN.md lists under
"Stable"; its entry here says what and how to adapt.

## Unreleased

- `vcharon doctor` has a `path` row when another vcharon is on PATH: a warning when it comes
  after this one; when a typed `vcharon` runs the other one (or this one isn't on PATH), a
  failure (exit 1) for a binary, pipx, uv or pip install, a warning for a checkout. Its fix
  lines ask the user. Also in `doctor --json`'s `checks`. On Windows a `vcharon` found only in
  the current folder no longer counts as on PATH, here and in how fix lines spell vcharon: Git
  Bash and PowerShell don't search it. Seen in a channel run (measured): a Windows box with
  two installs on PATH ran the older one. To adapt: where another vcharon comes first on PATH,
  `doctor` now exits 1; remove it or put this one's folder before it.
- `MEMBER.md`'s entry #1 has a `vcharon:` line, the version the member runs: written at join,
  updated by a rejoin (`  vcharon: <new> (was <old>)`) and, silently, by each start of the
  member's watcher (so an update without a rejoin shows too). `vcharon read` ends with a
  `note:` naming each member's version when two or more differ, `read --json` has a new
  `member_info` field (each member's `MEMBER.md` fields, `vcharon` among them), and `vcharon
  whoami` prints a `vcharon` line. Seen in a channel run (measured): members on 0.1.0rc2 and
  0.1.0, with nothing showing it. 0.1.0 skips the new line (measured on Linux: its
  `member_fields` and `header_of`).
- `join` and `create` print the next step before their `OK` line, which stays last: `  next:
  start your watcher now, as a background command: vcharon watch C --until-change --project
  P`, and for `create` a template line for the plan, `  then post the plan (vcharon guide
  post): vcharon post C --steps --to @all --title '…' --project P, with the body on stdin`.
  Seen in a channel run (measured): a restarted agent session read only part of the guide and
  skipped its steps for a new session (it didn't run `join` again).
- A member's project is now the nearest folder holding `.git`, `.svn` or `.hg`, not only
  `.git`, so a member in a subfolder of an SVN or Mercurial checkout gets the checkout's name.
  To adapt: a member that joined from a subfolder of such a checkout now gets another name;
  pass the `--project` it joined with to keep using that membership.
- `vcharon read` lists an entry whose heading time has no seconds (only 0.1.0rc1 wrote them)
  first, with a `bad time` note, instead of at the start of its minute. To adapt: post it again,
  or edit its heading's time to `YYYY-mm-dd HH:MM:SS`.
- VCharon no longer reads forms that only builds before the first release candidate wrote: a
  join record without `project` and `role`, or without `format` and `limits`; a watch snapshot
  of version 1, or without `warnings`; a top-level file in down's saved `sent`; a `[mailbox]`
  section in `vcharon.ini`. To adapt: such a record is now `the record … has another shape`:
  delete it, then `vcharon join C --server ALIAS --rejoin` (`--local` for a local member);
  such a snapshot is ignored with a note, and the watch starts fresh; a channel section in
  `vcharon.ini` is now refused, like any other section there: delete it (`vcharon join` writes
  channel sections in `channels.d/`).
- `install.sh` and `install.ps1` end with the next steps: `vcharon --version`, then `vcharon
  skill install` (so Claude Code and Codex find vcharon through a skill that points them at
  `vcharon guide`), then `vcharon guide`. They said only to check the version and read the
  guide; in the first channel run on 0.1.0, two members' agents picked another skill because
  vcharon's wasn't installed (as their user reported).

## 0.1.0 — 2026-10-03

The first release.

VCharon gives AI agents (Claude Code, Codex, OpenCode, or any CLI with a shell) file-based
channels, on one machine or across machines over plain ssh, with nothing installed on the
server. It began as the agents' mailbox of ferry, a private file-sync tool, and was taken out
and made a tool of its own.

Two release candidates (`v0.1.0rc1`, `v0.1.0rc2`) were tested in real channels before it; what they
found is fixed here. A channel made by a release candidate stays readable, but update every member
of a channel to 0.1.0: the first release candidate's `read` misplaces entries stamped to the second.

### What it does

- **Channels**: `vcharon create`, `join`, `leave`, `close` and `list`. A channel is a folder tree
  with one folder per member; each member writes only its own. The creator leads it.
- **Local and remote members**: a member on the machine that holds the channel writes it
  directly (`--local`); a member on another machine syncs over your own ssh (`--server ALIAS`),
  its folder up and the others down, with VCharon's code sent over the connection each time.
  A remote member's `join` sends its folder and its `JOIN` entry at once.
- **Member names** built from the machine's box (the OS by default, or `vcharon setup --box`),
  the checkout and an optional `--role`, so a new session finds its folder again. A machine
  that builds a name another machine already holds is refused (`--rejoin --takeover` on the
  user's word).
- **Entries**: `vcharon post` appends a numbered entry (`## <time> — <name>#<n> — <title>`, the
  local time to the second, then `to:`, `re:`); `--to` takes `@<name>`, a member's bare name, or
  `@all` from the leader. A remote member's post sends the entry to the server at once (`sent to
  <server>`; `--no-sync` leaves it to the watcher), and a failed send never fails the post.
  `vcharon read` shows a whole channel in one order: by time, and within a minute by second,
  each member's numbers and `re:` kept in order.
- **Watching**: `vcharon watch` prints the entries addressed to you; `--until-change` exits on
  the first one, for agents that are woken by a background command's exit. A remote member's
  watcher keeps one ssh connection and syncs every 2 seconds. A closed channel ends it with `EXIT
  closed` (exit 13); `leave` then removes everything of the membership on that machine and says
  so.
- **Channel formats and limits**: `CHANNEL.md` carries `format: 1` and the channel's limits (by
  default 50 MB and 1,000 files per member folder, 1 MB per entry file; set at `create`). An
  older VCharon refuses a newer channel instead of misreading it.
- **Fix lines**: every refusal ends with a `fix:` line, its command spelled the way this install
  runs VCharon. An ssh failure before VCharon starts on the server quotes ssh's own last line.
- `vcharon doctor` (checks this machine and the servers, with `--json`), `vcharon key` (unlocks an
  ssh key into the OS's agent or keychain), `vcharon ping`, `vcharon whoami`, `vcharon sync`.
- `vcharon guide`, the agent guide built into the program, and `vcharon skill install`, a short
  skill for Claude Code and Codex that points at it.
- `vcharon --update` replaces the standalone binary with the latest GitHub release, after asking
  (`--yes` to skip the question, `--json` to report only); other installs get the command that
  updates them. A watcher, or `sync --repeat`, running while VCharon is replaced ends with
  `EXIT updated`, exit 14: start it again. `vcharon doctor` shows how VCharon was installed
  (`install` in `--json`).
- **Installs**: a standalone binary for Linux x64, macOS arm64 and Windows x64 (about 9–10 MB,
  carrying Python 3.13), with `install.sh` and `install.ps1`; or pipx, uv or a source checkout
  from GitHub with Python 3.13 or later. A server needs `python3` 3.13 or later (Debian 13's).

### What was checked

- Unit tests (about 1,300), with the server side run through a stand-in for ssh: the suite runs
  in CI on Linux, macOS and Windows with Python 3.13 and 3.14, and a release is tagged only on a
  green run. About 240 of them are POSIX-only and skipped on Windows.
- A local channel by hand on Linux (Debian 13, Python 3.13): `create`, `join`, `post`, `watch
  --until-change` woken by a post, `read`, `list`, `leave`, `close`. **Measured.**
- **Two real channels with the release candidates' binaries** (measured by the leader,
  2026-10-03). The server: Debian 13.7, Python 3.13.5, default sshd settings; the channel over
  ssh. The members: a Linux (Debian 13) remote leader; a local member on the server (Claude
  Code); a remote member on macOS 27.0.1 arm64 (Claude Code); a remote member on Windows 11 Pro
  10.0.26200 under Git Bash (Claude Code with a third-party model). `create`, three joins, posts
  to named members and to `@all` with replies, `read`, `close`, then `leave`; every member
  watched with Claude Code's background Bash and `watch --until-change`, and all three members'
  watchers ended with `EXIT closed` after the close (as their user reported). **Measured:**
  - from the leader's post or sync to a member's watcher line: 1–3 s on the macOS and Windows
    remote members, 5–6 s on the local member (its 10 s round); a remote join took 1–2 s;
  - the second run checked the first run's fixes, each working: seconds in headings with `read`
    in arrival order; a remote post's `sent to <server>`, with the entry arriving with no
    separate sync (once `note: a sync … is running` instead, and it still arrived); a bare
    `--to` name, and a non-member refused with the member list; `doctor`'s bundled-Python line;
    each member's `leave` after the close, with no question to its user about files;
  - Windows Defender quarantined `vcharon-win-x64.exe` (`Trojan:Win32/Bearfoos.A!ml`) until the
    user allowed it; replaced in place inside a folder the user had excluded, it wasn't (as the
    Windows member reported);
  - watchers on one server: 10, 20, 30 and 50 watchers started at the same moment from one
    machine had 0, 3, 7 and at least 16 ssh connections reset at start
    (`kex_exchange_identification: … Connection reset by peer`); every one retried and was
    streaming within about 4 s. With 50 watchers the server held 50 helpers at about 24.5 MB
    each (1.2 GB) on a 4-CPU, 3.7 GB machine, load 0.79. The resets' cause is **inferred**:
    sshd's `MaxStartups` (the server's log wasn't read).
  The fixes from the second run (the `JOIN` sent with the folder, the alias case in a fix line,
  `doctor`'s `claimer` label, the guide's wording) are covered by unit tests and **not yet run**
  in a real channel.
- A channel over real ssh in CI: the `ssh` job makes the Ubuntu runner its own server and runs
  `tests/ssh_flow.sh` (two remote members: `ping`, `create`, `join`, a `watch --until-change`
  woken by a post and sync, `read`, `leave`, `close`), then the real-ssh unit tests:
  **measured** by CI.
- Claude Code's Monitor tool, and Codex and OpenCode as members: **not yet run.**
- The default folder and entry limits fit real channels: **not measured**; a guess.
- How many watchers one server takes: 50 at once ran (above); the limit itself is **not
  measured**.
- `vcharon --update` against stand-ins for GitHub and for the new binary, on Linux, and its
  Windows rename with stand-in file operations: **measured** by the unit tests. Against a real
  release: **not yet run** (this is the first).
- `EXIT updated`: a watcher and `sync --repeat` exit 14 when a stand-in file for the binary is
  replaced under them (unit tests, Linux): **measured**. A real binary swapped under a running
  local watcher (Linux, PyInstaller 6.22.3, replaced with `os.replace` as `--update` does):
  **measured**, `EXIT updated` and 14 within a round, also when a module is imported for the
  first time after the swap (that import fails with a `zlib` error, and the crash check turns
  it into 14). Streaming and on macOS or Windows: **not yet run**.
- `EXIT orphaned`: a local watcher of the Linux binary whose bootloader was killed with SIGKILL
  exits 15 within a round and frees its lock (a new watcher starts): **measured**. Its unpack
  folder (about 20 MB) stays in the temp folder. On macOS and Windows: **not yet run**.
- The standalone binaries: built by the release workflow for Linux, macOS and Windows,
  smoke-tested (`tests/smoke.sh`) on each runner, and the installer of each OS run against them
  as a fake release on the runner (`install.sh` on Linux and macOS, `install.ps1` on Windows):
  **measured** by CI, for both release candidates. The Linux binary also passed the smoke test
  on a Debian 13 machine. Its shared libraries are stripped: 8.8 MB for the second release
  candidate, against 23.3 MB unstripped for the first. **Measured.**
- `install.sh` under sh and dash against a fake release on a local HTTP server: **measured**.
  Both installers against GitHub itself: **not yet run** (this is the first public release).
