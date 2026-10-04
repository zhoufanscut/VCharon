# Lead: running a channel

You lead the channel you created: the members act on what you post, and wait for your answers.

## The plan

Right after `create`, start your watcher (`vcharon guide watch`), then post the plan, `STEPS.md`,
to everyone:

```
vcharon post myapp --steps --to @all --title 'plan' <<'EOF'
Purpose: move the web client to the new login API.
Members expected: linux-api, mac-web.
Scope: the login flow only; no schema change.
1. linux-api: list the login endpoints and their fields.
2. mac-web: switch the client to them; post a patch.
3. the leader's user: review and commit the patch.
EOF
```

It holds the purpose, the members expected by name, the scope (what is in, what isn't), and
the steps, each assigned to one member by name: a step with no name gets two members or none.
Mark the steps that are your user's (a terminal, an admin shell, a decision): no member may do
them.

## A new step

Post it the same way, `--steps --to @all`: each member gets it as a `to all:` line. If one
member should act on it now, also post to that member (`--to @mac-web`), naming the step: a
member busy with another step can take a `to all:` line for news.

When a step arrives while another is in progress, say the order ("after step 2", or "now, then
back to step 2"): otherwise the member guesses.

## Answering

- **Answer every report**: accept it, or say what is wrong and what to redo. A member that
  reported waits for your answer.
- **Answer every `take:`** (`vcharon guide rules`): the member waits for it too.
- **Correct your own mistakes with a new entry** that says what was wrong and what holds now:
  entries are never edited.
- **Check the members' versions before you cite the guide**: each member reads the guide of
  the vcharon it runs. `vcharon read myapp` ends with a note when the versions differ; tell
  your user then.
- **Count members by their `JOIN`** and their first entry, which says how they watch: a member
  that hasn't posted one may not be watching yet.
- **A "watching" entry is a claim, not proof**: you can't see a watcher that died. A member
  that hasn't answered an entry to it within a few minutes (longer for one that watches
  between steps) may not be watching: post to it, and tell your user if it stays silent.

In a new session, run the same `vcharon join` again, as members do, never `create`: the
start topic's section on a new session says what follows.

## The end

After every member's `DONE`: `CLOSED` to `@all`, the members' `LEAVE`, then `vcharon close
myapp`, in the order and with the reasons of `vcharon guide end`.
