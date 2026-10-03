# Trigger tests for the vcharon skill

The prompts below check that an agent loads the skill (`src/vcharon/skill/SKILL.md`) when, and
only when, the task is about a vcharon channel. They are run by hand; no unittest runs them.

## Status

- Description: written for these lists; not yet run against an agent.
- Runtime trigger checks: not run.
- Body: it only says when to use vcharon and to run `vcharon guide`; the commands it names are
  checked by `tests/test_guide.py`.

## How to run

From a scratch git repo (not this one), with the skill as a project skill: copy
`src/vcharon/skill/SKILL.md` to `<scratch repo>/.claude/skills/vcharon/SKILL.md`. Don't point
`HOME` at a scratch folder to try `vcharon skill install` here: Claude Code then loses its
login, and runs only with `ANTHROPIC_API_KEY` set.

1. For each prompt, run a fresh non-interactive session, for example
   `claude -p "<prompt>" --max-turns 2 --output-format stream-json --verbose --disallowedTools
   "Bash,Write,Edit,Agent"`.
2. A pass is a `Skill` tool call with `"skill":"vcharon"` for a should-trigger prompt, and no
   such call for a should-not one.
3. Write down here the agent, its version, the model, the OS and the date, and the result of
   each prompt.

Codex and OpenCode: the same prompts, with each tool's own way of showing which skill it
loaded; not yet worked out.

## Should trigger

1. "Join the myapp-ui channel"
2. "Create a vcharon channel called api-docs and lead it"
3. "Post to the leader that step 3 is done"
4. "Tell everyone in the channel the plan changed" (as the leader: `@all`)
5. "Check the channel for anything addressed to me"
6. "Start the channel watcher"
7. "The watcher exited with EXIT change, what now?"
8. "Run vcharon list on devbox"
9. "We're done here — leave the channel"
10. "Close the channel, everyone posted DONE"
11. "There's a CHANNEL.md in the leader's folder of myapp-ui — what does it want from me?"
12. "vcharon post says @all is the leader's, how do I answer?"
13. "vcharon says the name mac-web is taken in myapp-ui"
14. "Another session here is already in the channel; join as a second member"

## Should not trigger

1. "Check my email for anything from the build bot"
2. "Send a Slack message to the team"
3. "Set up a RabbitMQ queue for the order service"
4. "Take a screenshot and send it to the server"
5. "Sync this folder to the server with rsync"
6. "Copy this log folder to devbox with scp"
7. "Write a mail merge script for these addresses"
8. "Add a mailbox icon to the game's UI"
9. "Fix the failing test in tests/test_channel.py"
10. "Explain the watcher's design in DESIGN.md"
11. "Close the channel in this Go worker pool once every job is sent" (a Go `chan`)
12. "Why does my Rust mpsc channel never close?"
13. "Post the release notes to the #releases channel in Slack"
