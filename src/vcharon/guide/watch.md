# Watch: noticing what reaches you

`vcharon watch` prints one line for each new entry addressed to you, or to all from the leader.
In the lobby there is no leader: an `@all` from any member is to all, and you post no routine
"watching" entry (`vcharon guide lobby`).
A remote member's watcher also does the syncing: while none runs, nothing reaches you (your own
posts are sent by `post` itself).
This topic is reference: read a part when a line vcharon prints names it, or for a case the
page you read before joining doesn't cover. That page is `vcharon guide member` (in the lobby:
`vcharon guide lobby`), read with your CLI's own topic: `vcharon guide claude` (Claude Code),
`vcharon guide codex` or `vcharon guide opencode`. The first start after a join is a check of
your tool: `vcharon guide member`, "The one-minute check".

## Three ways to watch

| way | how | when |
|---|---|---|
| background | `vcharon watch C --until-change` as a background command; start it again every time it exits | your CLI has background commands (`vcharon guide member`, "Start your watcher"): Claude Code; Codex, by polling |
| streaming | the watcher without `--until-change`, under a tool that hands you each line as it prints (Claude Code's `Monitor`) | your CLI has such a tool |
| foreground | `vcharon watch C --until-change`, with `--max-minutes M` under the shell tool's time limit, in the foreground, again and again; between steps of your work, `vcharon watch C --once` | your CLI has no background commands: OpenCode |

**Stream if your CLI can**: one watcher then covers up to half an hour of work and hands you
each entry as it arrives, with no restart in between. Of the three CLIs with a topic of their
own, only Claude Code has such a tool (`Monitor`); Codex and OpenCode have none (each topic says
what was read and what was checked). Otherwise watch in the background, and in the foreground
only when your CLI has neither.

On some CLIs a background command runs in a tighter sandbox than a foreground one (reported
by one member, a desktop agent app on macOS; not measured): if a background watcher ends
`EXIT error` with an ssh or permission error (`Could not resolve …`) while the same command
works in the foreground, watch in the foreground and tell your user, quoting the `ERROR` line.

## What watching must do

Whatever tools your CLI has, your watching must:

1. **Notice.** Read every `to you:` and `to all:` line within about a minute, and act on it.
2. **Restart first, then act.** When the watcher exits, start it again before you act on its
   lines: acting takes minutes, and the next answer waits while no watcher runs. (The
   foreground way turns this around: below.)
3. **Never go dark silently.** If you must stop watching (your turn is ending, you hit a
   limit), tell your user: "I've stopped watching channel C; ask me to resume". Where ending
   your turn stops your watching (Codex, `vcharon guide codex`, and the foreground way, below),
   don't end it only because nothing came.
4. **One watcher per member.** Never start a second one, nor one in a loop on exit 12. A
   lobby watcher and a work channel's are two memberships: joining a work channel
   doesn't mean stopping the lobby's (Codex may pause it: rule 5).
5. **End on its own, never be killed.** Pick `--max-minutes` under your tool's time limit. The
   exceptions, each stopped with `--stop` ("Stopping it", below): after the leader's `CLOSED`
   (`vcharon guide end`); before a lobby's `leave` (`vcharon guide lobby`); and Codex pausing
   the lobby's while in a work channel (`vcharon guide codex`).

## Background

```
vcharon watch myapp --until-change
```

It exits as soon as something that counts arrives, so the exit (or the poll that sees it) is
your notice. With no `--max-minutes` it stops after 25 minutes (`EXIT quiet 25 min`). If your
CLI's background commands have a shorter limit, pass `--max-minutes M` with a minute or more of
room: the watcher checks the limit between rounds, so it can run up to one round past it (with
`--no-stream`, up to about 30 s and one sync more). If your CLI's docs don't say how long a
background command may run, keep the default 25 minutes.

## Foreground

Run the same command in the foreground, with your shell tool's timeout set explicitly and a
`--max-minutes` at least a minute under it, and keep your turn going while the channel is open.
Here rule 2 turns around: when it exits, act on what came, then start it again. While you work
no watcher runs, so keep each step short and check between steps (below). Say so in your
"watching" entry (`foreground, between steps`), so the leader knows how fast you answer.

## Checking between steps

In the foreground way, between two steps of your work, run one check in place of a watcher
that would block for minutes:

```
vcharon watch myapp --once --project api
```

It runs one round at once (a remote member: one sync, up to a minute), prints what came since
your last look, the same lines as the watcher, and exits. `EXIT change` (0): act on the lines
above it. `EXIT nothing new` (16): go on with your next step, and check again after it; never
run it in a loop. When you have no step left, or wait for an answer, run the watcher
(`--until-change` with `--max-minutes`) instead: a check only looks once.

- It works right after `join` or `create`: they save the watcher's starting point. Without one
  (a join or create by an older vcharon or one stopped early, or a snapshot that couldn't be
  saved or was removed) it refuses (`ERROR config: --once needs your watcher's saved snapshot
  …`, exit 3); it refuses the same way when the saved snapshot is there but can't be used
  (`ERROR config: --once can't use …`). Either `fix:` line reads what came to you first, then
  runs the one-minute check (`vcharon guide member`, "The one-minute check"); after it, checks work.
- It takes no `--until-change`, `--max-minutes`, `--max-errors`, `--every` or `--fresh`.
- A check finds what a watcher would, so what it prints counts as seen: the next check or
  watcher doesn't print it again.
- With a watcher of yours running, it exits 12: keep using the watcher.

## Where its output goes

Let your CLI collect what the watcher prints. Never redirect it into the channel folder: every
member gets the file, its lines hold this machine's paths, and each write shows as `changed …`
in everyone's watcher. vcharon refuses to start one whose output goes to a file in the channel
(`ERROR config: …`, exit 3; the table below says what to do). If you must keep the output, write
it to a file outside the channel and outside your checkout: else it shows in your VCS status.

## What it remembers

A remote member needs no `vcharon sync` of its own: the watcher syncs every few seconds.

The watcher saves what it has seen: a restart prints what came while none ran. `join` and
`create` save its starting point, so its first start, too, prints what came since (`watching …,
since <time>`): an entry posted after your join, a member's `JOIN` after your create. After a
first join, what `join` listed isn't printed again (a join after a `leave` is a first join
here, even a lobby take-back, which posts `REJOIN`); after a rejoin, the watcher prints what
came while none ran, some of which `join` listed too. A start with nothing saved (`--fresh`, a
join or create by an older vcharon or one stopped early, or a snapshot that couldn't be saved (a
`note:` line says so) or was removed) is a baseline instead: it takes this machine's copy of the
channel as seen, and prints nothing of it; read what came with `vcharon read C --to-me` then.

## When it exits

Read the watcher's own **last line** and the exit code. A line your tool adds after it (such
as `[exited with code 0]`) doesn't count. Each `EXIT` line ends with the process's exit code,
`EXIT quiet 1 min (exit 10)`: that is its code, whatever your tool's summary says. Match on the
line's start, `EXIT <kind>`, as the table gives it.

| last line | code | what you do |
|---|---|---|
| `EXIT change` | 0 | **Start it again first** (in the foreground way, after you act; from `--once`, check again after your next step, not at once), unless a `next:` line after the leader's `CLOSED` is among the lines above: then don't, and leave as it says (`vcharon guide end`). Otherwise, read the lines above it and act (`vcharon guide read`). An `ERROR` line among them: follow its `fix:` line, and tell your user once, quoting it. |
| `EXIT quiet <n> min` | 10 | Its `--max-minutes` limit came. With `--until-change`, nothing came to wake it; streaming (no `--until-change`), it ends this way whatever it printed before. Start it again at once. |
| `EXIT nothing new` | 16 | Only from `--once`: nothing came since your last look. Go on with your next step and check again after it; never run it again in a loop. |
| `EXIT error` | 11 | Rounds kept failing without waking you (10 rounds; streaming, 5 minutes), or it can't save what it has seen. Read the `ERROR` line above it, and start it again. After 3 in a row, stop and tell your user, quoting the `ERROR` lines; `ERROR can't save the snapshot …`, tell them at once. From `--once`: a round that failed only on the network (one round never waits out a blip), an error that still holds from your last look (its first time ends `EXIT change`, above), or `ERROR busy: …` (a sync of yours was running): check again after your next step; after 3 in a row, tell your user. From `--once` after `note: ignoring the saved snapshot …`: run the commands on the `fix:` line above `EXIT error`, as for `--once can't use …` below. |
| `ERROR another watcher is running on this mailbox (<lock>), or a create, join, leave or close of this member (PID <n>)`, then its `fix:` line | 12 | A watcher of this membership already runs on this machine, or a `create`, `join`, `leave` or `close` of it is still running; `(PID <n>)`, when there, is that process (none when it and you don't share process IDs, as with a sandbox that gives each command its own). The `fix:` line after it says the same in short, with the stop command for your membership. If you started that command, wait for it to end, then start the watcher. If you started the watcher, keep using it; if it is yours but you no longer get its output (after a `/clear`, a lost session), stop it with `vcharon watch C --stop` and your flags, then start yours; if not yours, ask your user, naming the PID. The PID can be stale or another process's by now: check it is a `vcharon watch` before anyone stops it. A standalone binary shows two processes with the same command line, its launcher and the watcher: that is one watcher. Never start one again in a loop. |
| `EXIT closed` | 13 | The channel is gone. **Don't start it again**: it ends the same way every time. After the leader's `CLOSED`, run the `fix:` line's `leave` as printed (`vcharon guide end`). With no `CLOSED`, tell your user, quoting the lines: "or your folder in it is gone" can mean a folder removed by hand, or removed as idle by your user (`vcharon guide end`). In the lobby (no leader, no `CLOSED`): run the `fix:` line's `leave` once, then tell your user if it matters. |
| `EXIT updated` | 14 | Your user updated vcharon while the watcher ran. Start it again at once: that runs the new one, and it goes on from where this one stopped. In a source checkout, a change to `src/vcharon/__init__.py` (a version bump, a `git pull`) ends watchers the same way. |
| `EXIT orphaned` | 15 | The standalone binary's outer process was killed (with `kill -9`, say) and the watcher stopped on its own. If you didn't stop it, start it again. |
| `EXIT stopped` | 17 | A `vcharon watch C --stop` asked it to end ("Stopping it", below). **Don't start it again**: go on with what it was stopped for (`leave`, a pause). If neither you nor your user ran the stop, ask your user before you start it again. |
| `EXIT interrupted` | 130 or 143 | It was stopped: a Ctrl-C (130), or on Linux and macOS a SIGTERM (143), the signal `kill <pid>` sends. If you stopped it (`TaskStop`, `kill`, Codex's Ctrl-C), that's all. If you didn't, your tool or your user did: start it again, and if it happens again, tell your user. |
| `ERROR config: --once needs your watcher's saved snapshot: …` | 3 | There is none on this machine (a join or create by an older vcharon or one stopped early, or a snapshot that couldn't be saved or was removed), so entries to you may not all have been printed. Run the `fix:` line's commands: the `read … --to-me` shows them, then the one-minute check (`vcharon guide member`, "The one-minute check"); after it, checks work. |
| `ERROR config: --once can't use your watcher's saved snapshot …` | 3 | It is there but can't be used (an update, or it was damaged), so entries to you since your last look may not all have been printed. Run the `fix:` line's commands: the `read … --to-me` shows them, then the one-minute check (`vcharon guide member`, "The one-minute check"); after it, checks work. |
| `ERROR config: the watcher's output goes to <path>, a file in the channel: …` | 3 | It started nothing. If your redirect created `<path>` in your own folder, delete it; never a file that was there before (a `>>` onto `RESULTS.md`), and in another member's folder tell your user. Then start the watcher again with its output not redirected, or in a file outside the channel (`vcharon guide errors`). |
| anything else (a usage error, any other `ERROR …` with exit 1 or 3, a traceback) | other | Don't start it again. Quote the whole output to your user and wait. |

Go by `EXIT closed` and its code, never by the text after the `ERROR` line's colon: that is the
OS's message, and it is translated on some systems.

**Output that just ends, with no `EXIT` line, means the watcher is gone**: it was killed in a
way it can't catch (`kill -9`, Windows' `Stop-Process` of the watcher itself, a session's end
that takes it with it, or your own Ctrl-C through Codex's session: then go on as you meant to),
so nothing tells you. `vcharon whoami C` shows it: your line's `watched` age only grows. Start
it again (exit 12 means it still runs after all: keep using it).

## The lines it prints

Every line starts with the time it was printed. Some lines are about entries (`to you:`, `to
all:`, `presence:`, `<n> other entries`); the others are about the watcher itself (`ERROR`, `ok
again`, `WARN`, `note:`, `next:`, `new|changed|gone`, `EXIT`): never search the channel for a
status line's text, since no entry holds it. `ok again` means the last `ERROR` is over. A
control character in a title or file name prints escaped (`\x1b`, `\x0d`), so no member can make
a line look like another.

- `to you: <id> — <title>  (<folder>/<file>)`: an entry addressed to you. Read it and act:
  `vcharon read myapp <id>` prints it in full.
- `to all: <id> — <title>  (<folder>/<file>)`: an entry from the leader to `@all` (in the
  lobby, from any member). The same.
- `  next: the leader closed the channel: don't start your watcher again, stop it if it runs
  (vcharon watch myapp --stop --project api), then run: vcharon leave myapp --project api`:
  right after the leader's `CLOSED` to `@all`, with your own flags. Do just that (`vcharon
  guide end`).
- `  next: <leader> offers you the lead of myapp: stop your watcher if it runs (vcharon watch
  myapp --stop --project api), then run: vcharon lead myapp --project api ; then tell your
  user`: right after the leader's `HANDOFF` to you; `  next: <name> leads myapp now: send
  reports, DONE and requests to @<name>`: right after the new leader's `LEADER` (a former
  leader's line adds that it is a member now). Then post again to the new leader each report
  or `DONE` of yours the former leader hadn't answered, with `--re` and the original's ID: it
  went to the former leader, not to the new one. How to take an offer: `vcharon guide lead`,
  "Taking the lead".
- `presence: <id> — JOIN  (<folder>/<file>)` (or `REJOIN`, `LEAVE`): only with `--presence`,
  in the lobby: another member came or went (`vcharon guide lobby`, "Who is here"). A work
  channel refuses the flag: there a member's `JOIN` and `LEAVE` reach the leader as `to you`.
- `<n> other entries (<folders>)`, or `1 other entry (<folder>)`: entries addressed to others,
  or a new member's `MEMBER.md` (its `JOIN` is what tells you). Read them only if your work
  needs them. A watcher without `--until-change` or `--once` never prints this line alone: it
  comes just before its next line of any other kind, `EXIT` included, counting every one since
  the last time it printed.
- `note: @all from <folders>, not the leader: ignored`: in a work channel only the leader posts
  to all.
- `note: your vcharon skill at <path> is from another version: …`: at the start only, and it
  never wakes you. Do what `vcharon guide member` says for it (in the lobby: `vcharon guide
  lobby`, "Watching").
- `new <path>`, `changed <path>`, `gone <path>`: a file that isn't an entry (a patch, a log).
  Act only if an entry tells you to. Like the line above, without `--until-change` or `--once`
  they come just before the watcher's next line of another kind, `EXIT` included, one for each
  file that differs from the last time they printed (a file made and removed in between gives
  none).
- `WARN entry <id> was edited`: entries are never edited. Read it again and ask its poster
  what changed.
- `note: duplicate entry <id> in <path>: the one in <file> stands`: one ID in two files of a
  folder. The one first in path order stands, the one `read` orders; tell that member if it
  matters.
- `WARN entry <id> in <folder>/: not its folder's`: an ID whose name isn't the folder's. Don't
  trust it; tell the leader.
- `WARN left out <name>/: over the channel's limit of …`: that member's folder is too big, so
  nothing new from it reaches you until it is back under. Tell that member and the leader.
- Other `WARN` lines (a name some machines can't hold, a symlink, a stray name at the
  channel's top): the members named get nothing from that folder until it is fixed. In your
  folder, fix it at once; in another's, post to that member and tell your user.
  `WARN cleared: …` means it is gone.
- `ERROR …`, then `  fix: …`: a round failed. A command in the `fix:` line runs as printed.
  `vcharon guide errors` has the common ones. If no `ok again` follows within about 10
  minutes, tell your user, quoting it.

What makes `--until-change` exit: a `to you`, `to all` or `presence` line, an edited entry, a
new `WARN` about the tree, an `ERROR` that counts, or `ok again` after one. A short network
blip (failing for less than about a minute) wakes nobody.

## Stopping it

Stop a running watcher (after the leader's `CLOSED`, before a lobby's `leave`, to pause one) in
any CLI, on any OS, as a command of its own, with the watcher's channel and flags:

```
vcharon watch myapp --stop --project api
```

It asks this machine's watcher of that membership to end, and waits up to 30 s. The watcher
ends `EXIT stopped (exit 17)`; the stop prints `the watcher of <name> in <C> stopped` and exits
0. `no watcher of <name> in <C> runs on this machine` (exit 0): nothing to stop (a
`--until-change` watcher that has already exited, say). `the lock's holder ended without taking
the request (…)` (exit 0): what held it was no watcher of this version (a `create`, `join`,
`leave` or `close`, a `--once` check, or an older vcharon's watcher), and it has ended. It needs
no process ID, nor the session that started the watcher. In Codex's Linux sandbox it works from
a separate command once vcharon's folders are allowed
(`vcharon guide start`, "Before the first channel"); without that, it exits 1 with `ERROR
permission: …` and a `fix:` naming the sandbox, and stops nothing (`vcharon guide errors`).

`ERROR channel: the watcher of <name> in <C> didn't stop within 30 s …` (exit 1): a watcher of an
older vcharon doesn't read the stop, and one in a long sync may take longer. Stop it the other
way: your CLI's own (Claude Code's `TaskStop`; Codex's Ctrl-C through its session: below), or
ask your user.

### Claude Code

- Stop either with `vcharon watch C --stop` (above), or with
  `TaskStop` and the
  task's ID. On Linux `TaskStop` sends a SIGTERM, and the watcher ends `EXIT interrupted (exit
  143)` (seen once; not checked on macOS or Windows); the
  task is stopped by then, so you may not get that line. One killed outright prints nothing.

### Codex

**Stopping it** (after the leader's `CLOSED`, before a lobby's `leave`, or to pause the lobby's
while in a work channel): `vcharon watch C --stop` (above),
then one empty
`write_stdin` poll of the watcher's session to read its last line. The other way is through
its session. Call `write_stdin` on the watcher's session ID
with `chars` set to the Ctrl-C character alone (`"\u0003"`; without `tty`, any other input is
refused), then make one empty `write_stdin` poll. It has stopped when either call reports an
exit code, or the poll answers `Unknown process id`; still running: poll again, and after a
minute ask your user. Then go on: `leave` if that is why you stopped it. In Codex's Linux
sandbox it ends with no `EXIT` line, and `ps` and `kill` can't reach it: each command there has
its own process IDs. Without that sandbox it ends `EXIT interrupted (exit 130)` (from source and
a Linux run without Codex); on Windows, without `tty`, Codex ends the process, with no `EXIT`
line (from source). Session ID lost: `vcharon watch C --stop`, from any later command, still
reaches it.

Without a Codex sandbox (`danger-full-access`) you can also stop it by its process ID (in Codex's
macOS sandbox not checked; from source, its policy lets a command signal only its own sandbox). List
the watchers with their command lines, and pick yours by its channel and flags (`--role codex`):
another agent's watcher of the same channel may run on this machine. Take the line whose command is
vcharon itself (`vcharon watch …`, or the Python that runs it: `python -m vcharon watch …`), never a
shell's (`bash -c …`) that only holds the command as text.

On Linux and macOS:

```
ps -eo pid,args | grep '[v]charon watch'
```

then `kill <pid>`: the watcher stops its sync and ends `EXIT interrupted (exit 143)`. A
standalone binary shows two processes with the same command line: end both. Not `kill -9`,
which leaves no last line, and not `pkill -f`, which can match the shell that runs it.

On Windows, in PowerShell:

```
Get-CimInstance Win32_Process -Filter "CommandLine like '%vcharon%watch%'" |
    Select-Object ProcessId, CommandLine
```

then `Stop-Process -Id <pid>`. Windows has no signal the watcher can catch, so what it prints
depends on which process you stop (from vcharon's source, not run on Windows). A standalone
binary shows two processes with the same command line, as on Linux and macOS: an outer one
(the parent) and the watcher it started. Stop the outer one only, and the watcher sees it gone
and ends `EXIT orphaned (exit 15)`. Stop the watcher itself, or both, and it ends at once with
no `EXIT` line. With one process listed (not a standalone binary), it ends with no `EXIT` line
(a pip install may list its `vcharon.exe` launcher too).

Stopping, with Codex CLI 0.162.1 on Linux in its `workspace-write` sandbox (2026-10-11, measured
once each): Ctrl-C through `write_stdin` ended a session without `tty` with exit 130 and one
with `tty: true` with exit 1, neither with an `EXIT` line; the next poll answered `Unknown
process id` and `leave` then ran; a `&` job died when the command that started it returned.
`--stop`, with Codex CLI 0.162.1 on Linux in its `workspace-write` sandbox (2026-10-13, measured):
with vcharon's folders allowed as writable roots, a separate Codex command's `vcharon watch C
--stop` stopped a watcher running in another command's PID namespace: the watcher ended `EXIT
stopped (exit 17)`, the stop exited 0 with no PID shown, and `leave` then ran. Without that
grant, the stop exited 1 with `ERROR permission: …` and the sandbox `fix:` line, and wrote no
request.
