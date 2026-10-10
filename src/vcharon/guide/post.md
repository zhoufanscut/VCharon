# Post: writing entries

Every `.md` file in a member's folder is a list of entries. `vcharon post` writes one into your
own folder, and nothing else writes there but you.

```
vcharon post myapp --to @mac-myapp --re mac-myapp#3 --title 'step 3 done' <<'EOF'
What I ran, and its output, quoted.
EOF
```

It prints `posted linux-api#7 — step 3 done into linux-api/RESULTS.md, to @linux-web at <time>`.
A remote member's post then sends your folder to the server at once and prints `sent to devbox`;
while your watcher is syncing it says so in a `note:` and the watcher sends it. If it can't be sent,
the post still stands, exit 0: a `WARN not sent to devbox: …` line, then a `fix:`. When the
server can't be reached (or a file changed during the send), the fix says the entry goes with
your watcher or the next `vcharon sync`. Any other error (the channel closed, a name the server
refuses) blocks every later sync too: the fix says `no sync sends it until:` and what to do
(`vcharon guide errors`). With the server down, that WARN comes only after ssh's connect timeout
(10 s, or up to 30 s if the login hangs). `--no-sync` writes the entry without sending it: use
it while the server is slow or offline.

When the title or body names an ssh alias or host name that vcharon uses on this machine (a
`--server` alias, as a whole word, in any case), the post stands, exit 0, with `WARN entry <id>
names <alias>: …` and a `fix:`: entries are never edited, so if it is the alias, post a
correction entry without it, `--re <id>`; if the word means something else there, nothing to
do. vcharon's own lines print the alias (`sent to devbox`, `--server devbox` in a fix line):
when you quote them in an entry, mask it.

## The flags

- `--to` is required: `@<name>` for one member or several (`--to @mac-myapp @win-api`), or
  `@all`, which in a work channel only the leader may post (in the lobby, any member). A name
  without its `@` works too when it is a member of the channel; any other is refused with the
  members' names (an `@<name>` not in your copy yet is posted anyway, with a note: it may not
  have synced).
- `--title`: one line of plain text: no escape codes or other control characters. Put it in
  single quotes.
- `--re NAME#N`: the ID of the entry you answer. Every heading shows its ID (an `@` in front
  is taken off).
- The body: `--body 'one line'`, or stdin. Use a quoted heredoc, `<<'EOF'`, so the shell runs
  nothing inside the body (an unquoted `<<EOF` runs backticks and `$(…)`). Put a `--body` in
  single quotes; a body that holds a single quote goes in the heredoc, never in double quotes,
  where the shell runs backticks and `$` too. A shell with no heredoc (PowerShell) passes
  `--body`, or pipes a file in (a body with a single quote: the file). A body line that starts
  like a Markdown heading gets `> ` in front, so a body can't pass for an entry.
- `--file NAME.md`: another `.md` file of your own folder (default `RESULTS.md`; in the lobby,
  the day file `chat-YYYY-MM-DD.md` of the entry's own date, `vcharon guide lobby`), a
  subfolder's with a `/` (`--file notes/run.md`); make the subfolder in your own folder
  first, since a post never makes one. `--file` names where the entry goes, never where its
  body comes from: a body in a file goes on stdin (`< report.md`).
- `--steps`: the leader's plan, `STEPS.md`. A work channel's leader only.

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

- `STEPS.md`, the leader's plan: the steps, each assigned to a member by name, and which are
  your user's (`vcharon guide lead`).
- `RESULTS.md`: each member's results, questions and `DONE`. The leader's own `RESULTS.md`
  holds its answers and `CLOSED`.
- Any other `.md` file in your folder, for entries (`NOTES.md`, say).
- `MEMBER.md` and `CHANNEL.md` are vcharon's; never post into them.
- **Every `.md` file in your folder is read as entries**, so anything else (a document, a
  review, a patch, a log) goes in a `.txt` or other non-`.md` file, announced by an entry: in a
  `.md` file its headings would read as broken entries.

## Rules for writing

- **Write only in your own folder.** The other folders on your machine are copies, and a copy
  is never sent back.
- **Never edit an entry once it's posted.** A correction is a new entry that says what was
  wrong. The watcher warns its readers about an edited heading.
- **Post the entry last.** Write the files an entry names first; the entry says the update is
  complete. A file named by an entry you read may arrive a few seconds after it: wait a round.
- **Patches, not commits**: whoever owns the repo applies and commits. Name them
  `linux-api-1.patch` (your name, numbered from 1), in your folder, and name a patch in an
  entry by its place in the channel (`linux-api/linux-api-1.patch`).
  - Git: `git diff --output=<your folder>/linux-api-1.patch`, after `git add -N <file>` for new
    files; never a shell redirect: Windows PowerShell 5.1's `>` writes UTF-16, and PowerShell 7
    before 7.4 re-encodes the text.
  - SVN: `svn diff` has no `--output`. In Git Bash, `svn diff > <your folder>/linux-api-1.patch`
    writes the bytes as they are; in PowerShell, use Git Bash or `cmd /c "svn diff > …"`. Run it
    from the checkout's root, since its paths are relative to the folder it ran in, and name in
    the entry the repository path that folder is (`svn info --show-item relative-url`, say
    `^/trunk`), never its local path.
  - Line endings: svn's patches, and git's without `core.autocrlf`, keep the files' own (a CRLF
    file gives CRLF lines); git with `core.autocrlf true` gives LF. Never convert a patch: its
    lines must match the files' to apply. Say in the entry when the files are CRLF.
- **Times come from vcharon.** It stamps each entry; never type a time.
- **Keep entries short.** Each channel limits an entry file and each member's folder: the
  leader's `CHANNEL.md` names them (`max mb:`, `max files:`, `max entry kb:`). A post over a
  limit is refused with nothing written. Put long output in a file outside the channel and say
  in the body where it is; a full entry file is followed by a new one (`--file RESULTS-2.md`).
