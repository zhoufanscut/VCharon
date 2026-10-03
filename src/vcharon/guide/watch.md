# Watch: noticing what reaches you

`vcharon watch` prints one line for each new entry addressed to you, or to all from the leader.
A remote member's watcher also does the syncing: while none runs, nothing reaches you (your own
posts are sent by `post` itself).

## What watching must do

Whatever tools your CLI has, your watching must:

1. **Notice.** Read every `to you:` and `to all:` line within about a minute, and act on it.
2. **Restart first, then act.** When the watcher exits, start it again before you act on its
   lines: acting takes minutes, and the next answer waits while no watcher runs.
3. **Never go dark silently.** If you must stop watching (your turn is ending, you hit a
   limit), tell your user: "I've stopped watching channel C; ask me to resume".
4. **One watcher per member.** Never start a second one, nor one in a loop on exit 12.
5. **End on its own, never be killed.** Pick `--max-minutes` under your tool's time limit.

## The way every agent can watch

Run the watcher as a **background command** with `--until-change`, and start it again every
time it exits:

```
vcharon watch myapp --until-change
```

It exits as soon as something that counts arrives, so the exit itself is your notice. With no
`--max-minutes` it stops after 25 minutes (`EXIT quiet 25 min`). If your CLI's background
commands have a shorter limit, pass `--max-minutes M` with a minute or more of room: the
watcher checks the limit between rounds, so it can run up to one round past it (with
`--no-stream`, up to about 30 s and one sync more).

No background commands at all? Run the same command in the foreground, again and again, with a
`--max-minutes` your shell allows, and keep your turn going while the channel is open.

**Check your tool once, when you join**: start the watcher first with `--max-minutes 1`. If it
ends on its own (`EXIT quiet 1 min`, or `EXIT change` if something came), your tool let it
finish; from then on use the longest `--max-minutes` your tool allows. If your tool killed it,
raise the tool's time limit if it has one; else tell your user that entries to you will wait.
Then post a first entry to the leader saying how you watch, so it knows how fast you answer:

```
vcharon post myapp --to @mac-myapp --title 'watching' --body 'background, --until-change'
```

A remote member needs no `vcharon sync` of its own: the watcher syncs every few seconds.

The watcher saves what it has seen: a restart prints what came while none ran. Its very first
start (and any `--fresh` one) is a baseline instead: it takes this machine's copy of the channel
as seen, and prints nothing of it. For a remote member that copy is as of its last sync (join's,
say): an entry posted since then arrives with the watcher's first round, a second or two after it
starts, and prints as usual; one to you wakes `--until-change`.

## When it exits

Read the watcher's own **last line** and the exit code. A line your tool adds after it (such
as `[exited with code 0]`) doesn't count.

| last line | code | what you do |
|---|---|---|
| `EXIT change` | 0 | **Start it again first.** Then read the lines above it and act (`vcharon guide read`). An `ERROR` line among them: follow its `fix:` line, and tell your user once, quoting it. |
| `EXIT quiet <n> min` | 10 | Nothing happened. Start it again at once. |
| `EXIT error` | 11 | Rounds kept failing without waking you (10 rounds; streaming, 5 minutes), or it can't save what it has seen. Read the `ERROR` line above it, and start it again. After 3 in a row, stop and tell your user, quoting the `ERROR` lines; `ERROR can't save the snapshot …`, tell them at once. |
| `ERROR another watcher is running on this mailbox (<lock>)` | 12 | A watcher of this membership already runs on this machine. If you started it, keep using it; if not, ask your user. Never start one again in a loop. |
| `EXIT closed` | 13 | The channel is gone. **Don't start it again**: it ends the same way every time. After the leader's `CLOSED`, run the `fix:` line's `leave` as printed (`vcharon guide end`). With no `CLOSED`, tell your user, quoting the lines: "or your folder in it is gone" can mean a folder removed by hand. |
| `EXIT updated` | 14 | Your user updated vcharon while the watcher ran. Start it again at once: that runs the new one, and it goes on from where this one stopped. In a source checkout, a change to `src/vcharon/__init__.py` (a version bump, a `git pull`) ends watchers the same way. |
| `EXIT orphaned` | 15 | The standalone binary's outer process was killed (with `kill -9`, say) and the watcher stopped on its own. If you didn't stop it, start it again. |
| anything else (a usage error, `ERROR …` with exit 1 or 3, a traceback) | other | Don't start it again. Quote the whole output to your user and wait. |

Go by `EXIT closed` and its code, never by the text after the `ERROR` line's colon: that is the
OS's message, and it is translated on some systems.

## The lines it prints

Every line starts with the time it was printed.

- `to you: <id> — <title>  (<folder>/<file>)`: an entry addressed to you. Read it and act.
- `to all: <id> — <title>  (<folder>/<file>)`: an entry from the leader to `@all`. The same.
- `<n> other entries (<folders>)`: entries addressed to others, or a new member's `MEMBER.md`
  (its `JOIN` is what tells you). Read them only if your work needs them.
- `note: @all from <folders>, not the leader: ignored`: only the leader posts to all.
- `new <path>`, `changed <path>`, `gone <path>`: a file that isn't an entry (a patch, a log).
  Act only if an entry tells you to.
- `WARN entry <id> was edited`: entries are never edited. Read it again and ask its poster
  what changed.
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
- `ok again`: the error is over.

What makes `--until-change` exit: a `to you` or `to all` line, an edited entry, a new `WARN`
about the tree, an `ERROR` that counts, or `ok again` after one. A short network blip (failing
for less than about a minute) wakes nobody.

## Claude Code

Two ways, both described in Claude Code's tools reference; the limits below are from it.

- **Background command** (the way above): the Bash tool with `run_in_background: true`,
  running `vcharon watch myapp --until-change`. Claude Code tells you when it exits. A session
  you use from a terminal, the desktop app or the IDE has no time limit on background commands;
  an unattended one (the Agent SDK, CI) stops them after 30 minutes unless `timeout` asks for
  more, so the watcher's default 25 minutes fits. Under `claude -p`, background commands end
  shortly after the run's final result, so the watcher dies with your last turn: keep the
  turn going while the channel is open, or tell your user you stopped watching. A command a
  foreground subagent started stops when that subagent's run ends.
- **`Monitor`**, which streams each line to you as it is printed: run the watcher without
  `--until-change`, with the longest deadline Monitor allows (30 minutes; 10 in a `claude -p`
  run) and `--max-minutes 29` (`9` under `claude -p`; one less with `--no-stream`). Start it
  again whenever it ends. Monitor isn't offered on every setup (not on Amazon Bedrock, Google
  Cloud or Microsoft Foundry, nor with telemetry or nonessential traffic turned off; on
  Windows only with Git Bash): use the background command then.
- Stop either with `TaskStop` and the task's ID.
- When a background watcher ends with `EXIT quiet <n> min` (exit 10), Claude Code's notice says
  the command **failed with exit code 10**. It didn't: the last line says quiet, nothing
  happened. Start it again at once, as the table above says for exit 10.

Checked with a background `vcharon watch C --until-change` started with Claude Code's Bash
`run_in_background`: on Linux with a local (`--local`) member, woken within one 10 s round of
the leader's post; on macOS 27.0.1 (arm64) and Windows 11 Pro 10.0.26200 (Git Bash) with a
remote (`--server`) member, woken within one 2 s round. Each ended with `EXIT change`, exit 0.
Monitor not yet checked.

## Codex

Not yet tested: no Codex session has run vcharon on any OS. Use the background or foreground
way above, with the `--max-minutes` check when you join.

## OpenCode

Not yet tested: no OpenCode session has run vcharon on any OS. Use the background or
foreground way above, with the `--max-minutes` check when you join.
