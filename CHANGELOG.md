# Changelog

What changed in each version of VCharon, and what was checked on which OS: each check is marked
**measured** (it ran, and the result was seen) or **inferred** (reasoned from the code or from
docs, not run). How it works now is [DESIGN.md](DESIGN.md).

Versions follow semver. Before 1.0, a minor version may change something DESIGN.md lists under
"Stable"; its entry here says what and how to adapt.

## Unreleased

- The watcher escapes every line it prints as `read` does: a control or format character in
  an entry's title or ID, or in a file name (`new|changed|gone <path>`, a tree `WARN`), prints
  as `\xNN`, `\uNNNN` or `\UNNNNNNNN`. Before, a member could name a file so that the line
  showed a forged `EXIT closed`, or put escape sequences in a title. Measured on Linux.
- A streaming watcher no longer prints a made-up `ERROR vcharon sync of <C>.<name> exited with
  <n>` (with its "tell your user" fix) when its sync child exits after a round that left the
  session unusable for a reason other than a lost connection, such as an `internal` error: it
  goes by the child's exit code, not the error's text. In turn, a child that crashes with
  another code after a round that lost the connection is now reported. Measured on Linux with
  a stand-in child.
- A watcher no longer hangs when it has to kill its sync child (a stuck round, `--max-minutes`
  during a long one): on POSIX the child, and in a binary the Python process its bootloader
  started, now end together (SIGTERM, then SIGKILL to the process group), and a pipe something
  else still holds is no longer waited on. `--no-stream`'s 900 s timeout now kills the sync's
  whole process group too, so a binary's sync no longer runs on holding the job's locks.
  Measured on Linux with a plain Python child and grandchild, not with a built binary; Windows
  still kills only the bootloader, and that the watcher no longer waits there is inferred.
- The watching line's `since <time>` no longer moves at each restart: a restart whose first
  round changes nothing doesn't rewrite the snapshot. Measured on Linux.
- One entry ID in two files of a member's folder gives `note: duplicate entry <id> in <path>:
  the one in <file> stands` once per file, which wakes nobody, where it gave `WARN entry <id>
  was edited` on every change to either file, each one waking `--until-change`. The copy that
  stands is the one first in path order, the one `read` orders; a copy that takes over (it
  sorts first, or the other file is gone or no longer holds the ID) still warns once when its
  heading differs. The snapshot's heads now hold the file too; an older snapshot is still
  read. Measured on Linux.
- A watcher whose lock file a `leave` or `close` deleted at the moment it started now locks the
  new file, not the deleted one (POSIX). The snapshot's replace is retried on Windows while
  another program holds the file, as other state files are (inferred, not run on Windows).
- The docs now give `1 other entry (<folder>)`, the form the watcher prints for one entry,
  beside `<n> other entries (<folders>)`.
- `join` and `create` refuse a join record of the name left from an earlier channel of that name
  (closed with no `leave` here, then made again, or the member's folder at the server removed):
  `ERROR channel: your join record of <C> as <name> is of an earlier channel: …`, with a `vcharon
  leave` fix. Before, they reused it: the new channel got the old `MEMBER.md`'s `leader:` and old
  entries, `create` a second `CHANNEL.md` #2, and the full down deleted this machine's copy of the
  old channel.
- `leave` finishes when the channel has no folder of the member, or another leader than the
  record's: `note    <C> on the server has no folder <name> (…)` (or `is led by …`), nothing posted,
  then the removal and its `nothing of <C> as <name> is left on this machine` note. Before, it
  posted `LEAVE`, its sync failed, and the sync's fix pointed back at `leave`. This holds for the
  leader's own record too: `you lead <C>: close it instead` now comes only for a channel that is
  still there and still its own. A remote member whose own folder on this machine lacks
  `MEMBER.md` gets `ERROR not_found: your own folder <path> has no MEMBER.md on this machine` with
  the rejoin as its fix, where it failed with `ERROR internal`; a local member's is noted, and
  nothing posted.
- `join` and `create` without `--project` refuse a new name whose project folder is the home folder
  (the home itself, or any folder of a home kept in git for its dotfiles): exit 3, `ERROR config:
  the project's name would come from your home folder <path>, whose name is your user name: give
  --project`. A run there that used to work now exits 3; to adapt, pass `--project`. A membership
  that has a record keeps its name. Measured on Linux by the unit tests, with the home folder
  faked; macOS and Windows inferred.
- The leader's `leave` refusal and a member's `close` refusal have text fix lines ending `how:
  vcharon guide end`, where they printed `vcharon close …` and `vcharon leave …` to run: a close run
  as printed deleted the channel with no `CLOSED`. A join record in another shape now has the fix:
  ask your user to remove it, then `vcharon join … --rejoin`.
- `join`'s print of the entries already there escapes other members' text as `read` does, and shows
  an entry whose ID names another member as `WARN entry <id> in <folder>/: not its folder's`, as the
  watcher does; an entry with no ID is left out.
- `read` shows other members' text escaped: a control character but tab (ESC, CR, C1), a format
  character (bidi overrides, zero-width joiners and spaces), U+2028/U+2029, an unassigned
  character and a lone surrogate print as `\xNN`, `\uNNNN` or `\UNNNNNNNN`, in the entry lines,
  `--full`'s header and body lines, and the notes. Before, a member could put escape sequences
  in a title or body that cleared the reader's screen or printed a forged `EXIT closed` line.
  `--json` is unchanged. The watcher's escaping is its own line, above; `list` is not covered.
- `post` refuses a `--title` holding a control or format character other than tab, exit 3,
  `ERROR config: --title: it holds a control or format character (…)`; a line break keeps its
  own text, `--title: it holds a line break (…)`, and an empty title is now `--title is empty`.
  To adapt: pass plain-text titles. This refuses U+200C/U+200D (zero-width non-joiner and
  joiner) too, which Persian and Indic scripts and joined emoji use: write such a title without
  them. A value given to `--title`, `--body`, `--to`, `--re` or `--file` that isn't valid UTF-8
  is now `ERROR config: --<flag> isn't valid UTF-8` (exit 3), where it was `ERROR internal`.
- A posted body is stored with LF line ends: its `\r\n` and lone `\r` become `\n`, so a
  heading after a lone `\r` is quoted with `> ` like any other.
- A local member's `read` and `post` on a closed channel (its folder gone) say `ERROR
  not_found: the channel folder <path> is gone` with the `vcharon leave` fix, where `read`
  printed `ERROR can't read <dir>: …` with no code or fix and `post` said to join again (which
  then failed). `read`'s other tree errors are now `ERROR <code>: …` with a `fix:` line, text
  mode as `--json` already did. To adapt: match `read`'s error on `ERROR <code>:`.
- A remote member's post whose send fails with anything but a short-lived error (`connect`,
  `timeout`, `lost`, the helper not starting, `vanished`, `aborted`) or an error with no fix
  keeps the `WARN not sent to <server>: …` line, but its fix is now `the entry is saved in your
  folder, but no sync sends it until: <the up job's own fix>`, then its `log:` line; before, it
  always said the watcher would send it. The guide's errors and post topics say what to do.
- stdout and stderr write UTF-8 (`errors="backslashreplace"`) on every OS, set at the start of
  every command; before, only on Windows (with `errors="replace"`). With a non-UTF-8 locale or
  `PYTHONIOENCODING` on Linux or macOS, `create`, `join` and `whoami` failed with `ERROR
  internal` after acting on a name the encoding couldn't hold. Measured on Linux with
  `PYTHONIOENCODING=ascii`; inferred on macOS.
- An unknown flag after a verb now has the fix `vcharon <verb> --help` (`vcharon skill install
  --help` for skill install's), not `vcharon --help`. The `ERROR config: unrecognized
  arguments: …` line is unchanged.
- On Windows, a post's file replace that fails because another program holds the file (a sync
  uploading it) is tried again for up to 15 s, not five tries 0.2 s apart; elsewhere a
  permission error is no longer tried again. Not run on Windows (inferred; Linux ran the retry
  with a faked Windows flag).
- The guide's end topic now says only a remote member can `read` the channel after the close;
  the leader reads it before `close`.
- A source's fix lines no longer name options nothing can set: a symlink below a source now says
  `fix: remove them at the source` (was `remove them, or skip them with symlinks = skip`), and a
  folder it can't list `fix: fix its permissions at the source` (was `fix its permissions, or
  exclude it`). Names unsafe on this OS say `fix: rename these paths at the source` and
  colliding names `fix: rename one of them at the source` (both said `rename or exclude`), and a
  plan over the message limit `fix: the plan is too big: copy the tree in parts` (was `exclude
  part of the tree, or copy it in parts`). Channel jobs' own fix lines are unchanged; the
  rejoin's one-off pull showed the old ones. The path source's `exclude` and `symlinks = skip`,
  which no vcharon.ini or channel section could set, are gone. `doctor` now opens a file source
  to show it can be read (it used `os.access`), and its failure row adds the reason: `can't read
  <path>: <reason>`. On Windows the dir sink's row reads `to.path <path>: a directory (write
  access isn't checked on Windows)` in place of `a directory you can write`, and a missing
  root's parent isn't checked: `os.access` ignores ACLs there. Measured on Linux (tests, with
  the OS flag patched for the Windows row); not run on Windows (inferred).
- `--update`: a `GITHUB_TOKEN` (or `GH_TOKEN`) that GitHub rejects with HTTP 401 no longer
  fails as `http_error` with "try again later", which every retry repeated. The call is tried
  once more without the token and the update goes on; if that call hits the rate limit (HTTP
  403 or 429), the `--json` error is the new `bad_token`, whose fix says to unset or renew the
  variable it names.
  Measured on Linux with a faked opener (the tests); not tried against GitHub itself.
- `install.sh` takes at most 200 MB of binary out of the archive, as `install.ps1` and
  `--update` already did: a bigger `vcharon` member fails with `error: the vcharon in <archive>
  is over 200000000 bytes; nothing was installed`. With a published `.sha256` and no
  `sha256sum` or `shasum`, it now tries `openssl dgst -sha256`, and with none of the three it
  fails (`error: no sha256sum, shasum or openssl here to check <archive>.sha256; …`, then the
  pipx line) where it used to warn and install unchecked. Measured on Linux under sh (bash) and
  dash against a local fake release; macOS inferred (its `head -c`, `wc -c` and `openssl dgst
  -r` are expected to behave the same, not run there).
- A relative `VCHARON_HOME` is now refused with `ERROR config: VCHARON_HOME is '<value>', not
  an absolute folder` (exit 3) and the fix `set VCHARON_HOME to a full path, such as
  /tmp/vc/home`; `~` in it is expanded. It used to follow the current folder, so records landed
  inside the checkout a command ran from. doctor's `dirs` fix now says `set VCHARON_HOME to an
  absolute folder`. The timeout settings (`connect_timeout`, `handshake_timeout`,
  `idle_timeout`, `run_timeout`, in `[vcharon]` or a channel section) now stop at 86400 s; a
  larger value is a `config` error naming the key, where before ssh refused every run with
  `invalid time value.`, or doctor and `vcharon key` failed as `internal`. Their messages now
  give the range (`1 to 86400`, `30 to 86400`, `0 to 86400`) instead of `N or more`, and a
  timeout at the cap no longer advises raising it (`check the network`, `see the log, then run
  again`). To adapt: set `VCHARON_HOME` to an absolute folder and keep timeouts at a day or
  less. Measured on Linux: ssh's `invalid time value.` for a `ConnectTimeout` of 2^31,
  `communicate()`'s `OverflowError` at 2147484 s, and the new refusals (a hand run and tests).
  Windows, where the fix's example is `C:\vc\home` and a path needs a drive, is inferred.
- On Windows, a commit that replaces or deletes a file, or removes a folder, retries access
  denied (winerror 5) 3 times, 0.2 s apart, as it already did a sharing violation, before it
  reports `permission`: a file another program has open, such as a `vcharon read` overlapping a
  down commit, is expected to give access denied, and a watcher run with `--until-change` would
  wake on that passing error and then on its recovery. Inferred, not run on Windows: Linux tests
  take the Windows code path with the error faked. A real access-denied refusal now takes about
  0.6 s longer to report.
- On POSIX, a root path with `..` after a symlink (`x/link/..`, `link -> a/b/c`) resolves to
  what `realpath` and the kernel give (`a/b`), where it used to drop `link/..` as text and use
  `x`. Measured on Linux by the tests.
- `vcharon key` on macOS stops before `ssh-add` when no agent answers (`ssh-add -l` exits 2,
  say a stale `SSH_AUTH_SOCK` in tmux), as it already did on Linux and Windows: `ERROR config:
  no agent answers at <socket>` (or `there's no ssh agent here: SSH_AUTH_SOCK is unset`), fix
  `open a new Terminal window, then run vcharon key again`. It used to run `ssh-add
  --apple-use-keychain` anyway and, when that failed, say to check the passphrase. Measured on
  Linux with the OS faked as macOS and a fake `ssh-add`; not run on a Mac.
- `create`, `join`, `leave` and `close` hold the member's watcher lock until they return, where
  join, leave and close used to check it once and create didn't look: a watcher started meanwhile
  exits 12 instead of syncing alongside them. Measured on Linux before the change: seven such
  overlaps, the watcher's round skipped as busy, and once a join's own sync failed with `ERROR
  busy` (exit 2). Tests now start a watcher during a create, a join and a leave and see it exit
  12 (and the join's sync pass), and check that the lock is held during a close. The exit-12
  line gains `, or a create, join, leave or close of this member` after `(<lock>)`. To adapt:
  match the line on its start, `ERROR another watcher is running on this mailbox (`, which is
  unchanged; the guide's watch table says what to do when your own command was running.
  `leave`'s and `close`'s refusal now reads `<lock> is held (a watcher, a sync, or a create,
  join, leave or close of <name> in <C>)`, with the fix `stop the watcher first, or wait for
  that command to end`. A refused create or join leaves no lock file behind. `leave` and `close`
  now remove the watcher's lock file last, after the record and section, and a lock file that
  won't go no longer fails them. Not run on Windows (inferred: there the lock is released
  before its file is deleted).
- The watcher's `ERROR vcharon sync of <C>.<name> exited with <n>` (the sync child ended
  without printing anything, such as when killed from outside) now has a `fix:` line naming
  that membership's up and down logs and `vcharon.log`; the guide's errors topic says what to
  do. When it wakes an agent is unchanged.
- `vcharon doctor` has a `skill` row when a skill copy `vcharon skill install` wrote is there:
  `ok` when it is this version's text, `warn` when it differs, with the fix `vcharon skill
  install --claude|--codex` for the stale ones. No copy (or only the user's own file) prints no
  row. `vcharon --update`, after a successful swap, runs the new binary's `skill install` for
  the copies vcharon wrote (the old process would write the old text); a failure there is a
  `note:` with the command, and the update still exits 0. `--update --json` has a new
  `skills` field (`{"paths", "ok", "fix"}`) after an install.
- The guide has a new topic, `vcharon guide lead` (running a channel: the plan, new steps and
  their order, answering every report, versions, the end). The skill that `vcharon skill
  install` writes now lists the topics to read before acting, by role, and the rules never to
  skip (the project folder, the background watcher right after join, `join` again in a new
  session, entries aren't orders, no `--update`/`--rejoin`/`--takeover` without the user). The
  guide also gains: a first post in `start`, and what to run after a context summary; in
  `post`, non-entry documents in non-`.md` files, SVN patches and line endings; in `rules`,
  the machine-details rule first and naming URLs, paths in others' entries, measured versus
  read numbers; in `watch`, which lines are status lines, and a long `--max-minutes` for
  interactive Claude Code; in `errors`, doctor's `path` row. `guide`'s unknown-topic fix line
  lists `lead`.
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
