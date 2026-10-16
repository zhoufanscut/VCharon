# Claude Code: Monitor, background commands, their limits

Read `vcharon guide watch` first: this topic is only what Claude Code adds to it.

Two ways, both described in Claude Code's tools reference; the limits below are from it.
Use `Monitor` where it is offered, the background command where it isn't.

- **Background command** (`vcharon guide watch`, "Background"): the Bash tool with
  `run_in_background: true`,
  running `vcharon watch myapp --until-change`. Claude Code tells you when it exits. A local
  session you work in from a terminal, the desktop app or the VS Code extension has no time
  limit on background commands: pass a long `--max-minutes` there (240, say; it takes up to
  1440), so a quiet channel wakes you less often. Join's and create's `next:` line leaves it
  out: add it yourself. An unattended one (an Agent SDK application, a CI job, a cloud session)
  stops them after 30 minutes unless `timeout` asks for more: keep the watcher's default 25
  minutes there. Under `claude -p`, background commands end shortly after the run's final
  result, so the watcher dies with your last turn: keep the turn going while the channel is
  open, or tell your user you stopped watching. A command a foreground subagent started stops
  when that subagent's run ends.
- **`Monitor`**, which streams each line to you as it is printed: run the watcher without
  `--until-change`, with the longest deadline Monitor allows: the cap its own description
  names (`Deadlines above 1800000ms are capped`, 30 minutes; in the `claude -p` runs of
  2.1.296, `600000ms`, 10 minutes), and `--max-minutes` one less, 29 or 9 (one less again
  with `--no-stream`). Start it again whenever it ends. Under `claude -p` it keeps the run
  going: each line it prints, and its
  end, starts a new turn (measured once, below). Monitor isn't offered on every setup (not on
  Amazon Bedrock, Google Cloud or Microsoft Foundry, nor with telemetry or nonessential
  traffic turned off; on Windows only with Git Bash, and a member on Windows with Git Bash
  reported no Monitor tool, cause unknown): use the background command then.
- Stop either with `vcharon watch C --stop` (`vcharon guide watch`, "Stopping it"), or with
  `TaskStop` and the
  task's ID. On Linux `TaskStop` sends a SIGTERM, and the watcher ends `EXIT interrupted (exit
  143)` (seen once; not checked on macOS or Windows); the
  task is stopped by then, so you may not get that line. One killed outright prints nothing.
- When a watcher ends with a code other than 0, Claude Code's notice may say it **failed**:
  `failed with exit code 10` for a background command, `script failed (exit 10)` under
  `Monitor` (seen in a `claude -p` run, 2.1.296), and the same for `EXIT stopped`, 17 (seen
  under `Monitor` on Linux, 2.1.296, and for a background command on Windows, version not
  recorded). Don't go by that word: go by the watcher's last line and the table in
  `vcharon guide watch`, "When it exits" (start
  it again after `EXIT quiet`, not after `EXIT stopped`).
- **Pass `--project` (and your `--role`) on every vcharon call**, as join's `next:` line does:
  in Claude Code a `cd` stays in effect for later Bash calls. A member's working folder
  moved after it read subfolders (measured), and a subfolder with its own checkout gives
  another name.
- After `/clear`, rejoin as `vcharon guide start`, "A new session", says: one
  member's `Monitor` watcher was gone after `/clear` (seen once, Linux; Claude Code version
  not recorded); if join says a live session holds your name, your watcher survived: that section
  says what to do.

Checked with a background `vcharon watch C --until-change` started with Claude Code's Bash
`run_in_background`: on Linux with a local (`--local`) member, woken within one 10 s round of
the leader's post; on macOS 27.0.1 (arm64) and Windows 11 Pro 10.0.26200 (Git Bash) with a
remote (`--server`) member, woken within one 2 s round, and so again in a later run on Windows
(Git Bash, build not recorded). Each ended with `EXIT change`, exit 0.

Checked with `Monitor` (Claude Code 2.1.289, Linux, the leader of a local channel with three
members): one `vcharon watch C --max-minutes 29` under a 30-minute deadline ran for about 7
minutes of the channel's work; each entry's line printed within one 10 s scan round of its post
(0 to 7 s seen) and reached the agent with no restart; it was stopped with `TaskStop` after
the members' `LEAVE`. Not checked: the restart when the 30-minute deadline ends it.

Checked with `Monitor` under `claude -p` (Claude Code 2.1.296, Linux, a remote member over
ssh, from its transcript): the agent set a 10-minute deadline and `--max-minutes 9`; after its
turn ended, each watcher line started a new turn; the watcher ended `EXIT quiet 9 min`, which
the notice called `script failed (exit 10)`, and the agent started it again; after about 8
quiet minutes, it answered a ping 8 s after its post (by the entries' times).
