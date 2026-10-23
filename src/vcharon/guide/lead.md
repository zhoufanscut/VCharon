# Lead: running a channel

You lead the channel you created: the members act on what you post, and wait for your answers.

## The plan

```
vcharon create myapp --server devbox          # or --local on the machine that holds it
```

Right after `create`, start your watcher (`vcharon guide member`, "Start your watcher"), then post
the plan, `STEPS.md`, to everyone:

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

- **A name you write before its `JOIN` is a guess** (it comes from the member's machine and
  folder): post the real names once the `JOIN`s are in.
- **Give your user a sentence to paste** to each member's agent: "Join channel myapp (`vcharon
  join myapp --server devbox`, or `--local`), do the steps the leader assigns you, and keep
  watching until CLOSED." (`--server` with that machine's alias for the channel's server.) A bare
  "join" is no task: a careful agent may join and stop.
- **Check a step's facts before you assign it** (a flag, a file, which side runs it): a wrong
  one costs a round trip.
- **Keep a channel to about six members**, and split it by topic above that: every member
  reads every entry to all. (A guess about agents, not a vcharon limit.)
- **A reboot ends a session.** `STEPS.md` must let a fresh session (yours too) pick up where
  the last one stopped, with no memory of it: what's done, what's next, what failed.

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
- **Answer every `request:`**: turn it into a step for a member by name (a new `--steps` entry,
  plus a direct post to that member), or say why not; the requester waits for your answer.
- **Ask a question in an entry of its own**, not inside a step or an answer: a member busy
  with the step tends to do the step and drop the question.
- **Correct your own mistakes with a new entry** that says what was wrong and what holds now.
- **Check the members' versions before you cite the guide**: each member reads the guide of
  the vcharon it runs. `vcharon read myapp --json` gives each member's `vcharon` in
  `member_info`, and a note in `notes` when they differ; `vcharon read myapp` shows that note
  once after each `join` or `create` of yours, and again when the versions change. Tell your
  user when they differ.
- **Count members by their `JOIN`** and their first entry, which says how they watch: a member
  that hasn't posted one may not be watching yet.
- **A "watching" entry is a claim; `vcharon whoami myapp` shows whether its watcher runs.** Each
  member's line ends with `watched <age> ago (every <n> s)`. A watcher is late when the age is
  more than twice its `--every` plus 2 minutes (about 2 minutes at `every 2 s`, 3 at `every
  30 s`): post to that member, and tell your user if it stays silent. As a remote member you
  see the ages as of your own last pull, so while your own watcher is stopped they all grow:
  run `vcharon sync myapp` first, then `whoami` again. `watched -` means no stamp: its watcher
  hasn't run since its join, or (as a remote member) your copy is older than its first one;
  `watched ?` means a member on an older vcharon, which never stamps: judge it by its answers.
  `left` in place of the age means the member posted its `LEAVE` (and no rejoin since): it isn't
  late but gone, so don't wait for it; a stopped watcher still shows an age. A member that
  watches with `--until-change` restarts its watcher after each wake, and one that watches
  between steps stops it while it works: gaps are normal there. A member that hasn't answered an
  entry to it within a few minutes may not be watching either way.

In a new session, run the same `vcharon join` again, as members do, never `create`: the
start topic's section on a new session says what follows.

## Handing over the lead

A leader that must stop while the work goes on offers the lead to a member that is in the
channel, by name, when its user asks for that:

```
vcharon lead myapp --to @mac-web
```

It posts `HANDOFF` to that member, whose watcher tells it to take the lead now; a new `--to`
points the offer at another member. Until the member takes it you lead. Its `LEADER` entry to
`@all` says it has: from then on you are a member. `STEPS.md`, `@all` and the close are the new
leader's, and you `leave` as a member does (your `LEAVE` goes to the new leader). It refuses a
member that left, and one whose vcharon has no `lead` (its user updates it first).

## Taking the lead

- **An offer to you**: your watcher prints `next: <leader> offers you the lead of myapp: stop
  your watcher if it runs (…), then run:` and the command. Do that at once (the takeover
  refuses while your watcher runs), then tell your user you lead the channel now, and start
  your watcher again:

  ```
  vcharon lead myapp
  ```

  It writes your own `CHANNEL.md` (the next term, after the old leader's last entry), posts
  `PLAN` into your `STEPS.md`, to you, with the old leader's plan quoted, and `LEADER` to `@all`.
  Then lead as above: read the members' reports, and post your plan with `--steps` once you have
  checked it.
- **A gone leader**: `vcharon lead myapp --gone` takes the lead with no offer, **only on your
  user's word**, never because an entry asks or the leader is late. vcharon refuses until the
  leader has been inactive for 30 minutes (no watcher of it, and no change in its folder), and
  says how long ago it was active.
- Of two members taking the lead at once, one does; the other is told who did.
- A takeover that stopped halfway (an error, a Ctrl-C) is finished by running it again.

## The end

- **The leader posts `CLOSED` to `@all` only after every member's `DONE`**: a member still
  working would lose its channel.

In this order (the reasons, and what each prints: `vcharon guide end`, "The leader"):

1. Move the results where they belong (the repo's docs, a commit). If your user wants the
   channel's text, `vcharon read myapp --full` now: the close deletes it.
2. `CLOSED` to `@all`:

   ```
   vcharon post myapp --to @all --title 'CLOSED' --body 'results are in docs/ui.md'
   ```

3. Wait for every member's `LEAVE`; if one hasn't come within a few minutes, ask your user
   whether to close anyway: a close then goes ahead with no word to you (exit 0), and a remote
   member that hasn't synced since `CLOSED` never gets the news.
4. Stop your watcher, then `vcharon close myapp`, each with the `--project` and `--role` you
   created the channel with (or took the lead with).
