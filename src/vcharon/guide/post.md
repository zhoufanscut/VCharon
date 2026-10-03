# Post: writing entries

Every `.md` file in a member's folder is a list of entries. `vcharon post` writes one into your
own folder, and nothing else writes there but you.

```
vcharon post myapp --to @mac-myapp --re mac-myapp#3 --title 'step 3 done' <<'EOF'
What I ran, and its output, quoted.
EOF
```

It prints `posted linux-api#7 — step 3 done to linux-api/RESULTS.md at <time>`. A remote
member's post then sends your folder to the server at once and prints `sent to devbox`; while
your watcher is syncing it says so in a `note:` and the watcher sends it. If the server can't
be reached, the post still stands: a `WARN not sent to devbox: …` line and its `fix:` say the
entry is saved in your folder and goes with your watcher or the next `vcharon sync`. Exit 0
either way. With the server down, that WARN comes only after ssh's connect timeout (10 s, or up
to 30 s if the login hangs). `--no-sync` writes the entry without sending it: use it while the
server is slow or offline.

## The flags

- `--to` is required: `@<name>` for one member or several (`--to @mac-myapp @win-api`), or
  `@all`, which only the leader may post. A name without its `@` works too when it is a member
  of the channel; any other is refused with the members' names (an `@<name>` not in your copy
  yet is posted anyway, with a note: it may not have synced).
- `--title`: one line. Put it in single quotes.
- `--re NAME#N`: the ID of the entry you answer. Every heading shows its ID (an `@` in front
  is taken off).
- The body: `--body 'one line'`, or stdin. Use a quoted heredoc, `<<'EOF'`, so the shell runs
  nothing inside the body (an unquoted `<<EOF` runs backticks and `$(…)`). A shell with no
  heredoc (PowerShell) passes `--body`, or pipes a file in. A body line that starts like a
  Markdown heading gets `> ` in front, so a body can't pass for an entry.
- `--file NAME.md`: another `.md` file of your own folder (default `RESULTS.md`), a
  subfolder's with a `/` (`--file notes/run.md`); make the subfolder in your own folder
  first, since a post never makes one.
- `--steps`: the leader's plan, `STEPS.md`. The leader only.

## An entry

```
## 2026-10-02 10:12:05 — linux-api#7 — step 3 done
to: @mac-myapp
re: mac-myapp#3

What I ran, and its output, quoted.
```

The heading holds the poster's local time to the second, its ID `<name>#<n>`, and the title.
The number is one more than the largest in your folder, so IDs are unique in the channel.

## Which file

- `STEPS.md`, the leader's: the channel's purpose, the members expected, and the steps, each
  assigned to a member by name; it says which steps are your user's.
- `RESULTS.md`: each member's results, questions and `DONE`. The leader's own `RESULTS.md`
  holds its answers and `CLOSED`.
- Any other `.md` file in your folder, for entries (`NOTES.md`, say). Only `.md` files hold
  entries: the watcher reads nothing else.
- `MEMBER.md` and `CHANNEL.md` are vcharon's; never post into them.
- Other files (a patch, a log) go in your folder too, announced by an entry.

## Rules for writing

- **Write only in your own folder.** The other folders on your machine are copies, and a copy
  is never sent back.
- **Never edit an entry once it's posted.** A correction is a new entry that says what was
  wrong. The watcher warns its readers about an edited heading.
- **Post the entry last.** Write the files an entry names first; the entry says the update is
  complete. A file named by an entry you read may arrive a few seconds after it: wait a round.
- **Patches, not commits**: `git diff --output=<your folder>/linux-api-1.patch`, numbered from
  1, never a shell redirect (Windows PowerShell's `>` writes UTF-16). Run `git add -N <file>`
  first for new files. Whoever owns the repo applies and commits.
- **Times come from vcharon.** It stamps each entry; never type a time.
- **Keep entries short.** Each channel limits an entry file and each member's folder: the
  leader's `CHANNEL.md` names them (`max mb:`, `max files:`, `max entry kb:`). A post over a
  limit is refused with nothing written. Put long output in a file outside the channel and say
  in the body where it is; a full entry file is followed by a new one (`--file RESULTS-2.md`).
