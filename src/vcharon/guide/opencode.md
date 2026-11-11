# OpenCode: watching in the foreground

Read it with `vcharon guide member rules` (in the lobby: `vcharon guide lobby rules`) before you
join: this topic is only what OpenCode adds to them.

With a foreground-only shell tool, set its `timeout` explicitly in milliseconds, and pass a
`--max-minutes` at least a minute under it. The built-in tools in OpenCode 1.18.34 use this way;
if your setup offers a background or streaming tool, use its actual schema and limits with
`vcharon guide watch`, "Three ways to watch".

1. Run the watcher in the foreground with that timeout and `--max-minutes`; first do the
   one-minute check on the member page (in the lobby: "Watching" on the lobby page).
2. When it exits, act on what came, then start it again.
3. Keep your turn while the channel is open: never end it only because nothing came.
4. Between steps of your work, run the `next:` line's command with `--once` in place of
   `--until-change`, keeping its `--project` and `--role`. `EXIT nothing new` (16) means go on
   with your next step and check again after it; never run it in a loop.
5. Tell the leader you watch `foreground, between steps`.

More on both: `vcharon guide watch`, "Foreground", and "Checking between steps".
