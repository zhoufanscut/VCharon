# OpenCode: watching in the foreground

Read `vcharon guide watch` first: this topic is only what OpenCode adds to it.

The foreground way (`vcharon guide watch`, "Foreground").
OpenCode's shell tool has no background mode: every command runs until it
exits or the tool's timeout. Upstream OpenCode's source sets a default of 2 minutes, and the
tool takes a `timeout` in milliseconds, with no maximum (read in OpenCode 1.18.34's source, not
measured). Pass that timeout explicitly, with a `--max-minutes` at least a minute under it.
OpenCode has no streaming tool, nor any tool that tells you a command exited (read in the same
source; an OpenCode 1.18.31 agent said the same of its own tools). Third-party plugins say they
add one; none was checked.

Checked with an OpenCode build reporting version 1.18.31, on Linux, a local member: `vcharon
watch C --until-change --max-minutes 3` with the tool's timeout 300000 ran in full and ended
`EXIT quiet 3 min`, exit 10. A watcher started with `nohup … &`, or in a detached tmux session,
ran but never woke the agent.
