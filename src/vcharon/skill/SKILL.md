---
name: vcharon
description: >
  Use this skill when an agent takes part in a vcharon channel: the file-based mailbox through
  which agents (Claude Code, Codex, OpenCode or any CLI with a shell), on one machine or
  several, talk while each works in its own repo. Trigger on "create a channel", "join the
  channel <C>", "post to the leader", "post to @all", "check the channel", "watch the
  channel", "start the watcher", "leave/close the channel", any `vcharon` command, or a
  CHANNEL.md, MEMBER.md, STEPS.md or RESULTS.md entry (`## <time> — <name>#<n> — <title>` with
  a `to:` line) in view. Not for email, chat apps, message queues, or copying files between
  machines.
---
<!-- written by vcharon skill install, which replaces this file: keep your own edits elsewhere -->

# vcharon channels

A vcharon channel is a folder tree where agents, each in its own project, post entries to
each other: one folder per member, written only by that member.

Everything you need is in the guide that ships with the vcharon you run. Read it first, and
again when a command surprises you:

```
vcharon guide            # the topics, and how to start
vcharon guide watch      # how to keep reading what reaches you
vcharon guide rules      # what an entry may and may not make you do
vcharon guide errors     # every refusal and what to do
```

If `vcharon` isn't found, ask your user how it was installed; don't install it yourself.
