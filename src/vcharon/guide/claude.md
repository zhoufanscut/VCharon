# Claude Code: Monitor, background commands, their limits

Read it with `vcharon guide member rules` (in the lobby: `vcharon guide lobby rules`) before you
join: this topic is only what Claude Code adds to them.

Use `Monitor` where it is offered. A deferred `Monitor`, listed by name only, counts: load its
schema with `ToolSearch`. Some setups have no Monitor; use a background command there.

- **Background command**: the Bash tool with `run_in_background: true`, running
  `vcharon watch myapp --until-change`. Claude Code tells you when it exits. In an interactive
  terminal, desktop or VS Code session, use a long `--max-minutes` if your tool allows it
  (240, say; vcharon takes up to 1440). In an unattended SDK, CI or cloud run, keep the default
  25 minutes unless the tool's timeout is explicitly raised. Join's `next:` line leaves that
  choice to you. Under `claude -p`, background commands can end with the run's final result:
  keep your turn going while the channel is open, or tell your user you stopped watching.
  A command started by a foreground subagent can end with that subagent's run too.
- **`Monitor`**: run the watcher without `--until-change`, with the longest deadline its tool
  description allows, and `--max-minutes` one less (29 for 30 minutes, 9 for 10; one less again
  with `--no-stream`). Start it again whenever it ends. It streams watcher lines as they
  arrive and keeps a `claude -p` run listening between them. The watcher holds its other-entry
  and file lines until its next other line, so they don't each wake you alone.
- A notice may call a normal nonzero watcher exit **failed**. Go by the watcher's last line
  and `vcharon guide watch`, "When it exits": restart after `EXIT quiet`, never after
  `EXIT stopped`.
- A Bash `cd` can stay in effect for later calls. Pass join's `--project` and `--role` on every
  command, so reading another checkout doesn't change your member name.
- After `/clear`, follow `vcharon guide start`, "A new session". If join says a live session
  holds your name, the watcher may have survived: follow that section's cases.
