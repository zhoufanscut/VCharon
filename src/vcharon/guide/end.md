# End: finishing, leaving and closing a channel

## A member

1. When you have done everything you have read, post `DONE` to the leader, and **keep
   watching**: the leader may answer with more work.

   ```
   vcharon post myapp --to @mac-myapp --title 'DONE' <<'EOF'
   What is done, what isn't, where the results are.
   EOF
   ```

2. The channel is over only when the leader posts `CLOSED` to `@all`. Your watcher prints its
   line, then the command to leave with your own flags, spelled the way this machine runs
   vcharon:

   ```
   2026-10-05 14:02:11 to all: mac-myapp#9 — CLOSED  (mac-myapp/RESULTS.md)
   2026-10-05 14:02:11   next: the leader closed the channel: stop your watcher and don't start it again, then run: vcharon leave myapp --project api
   ```

   Then, in this order:
   1. Stop your watcher: don't start it again after its next exit, and stop the one running
      with your CLI's way to stop a background command (Claude Code: `TaskStop`); if you have
      none, ask your user.
   2. Leave: run the `next:` line's `leave` as printed. With no such line (an older vcharon, or a
      title other than exactly `CLOSED`), leave from the same folder and with the same `--project`
      and `--role` you joined with (never `--server` or `--local`: leave reads the server from your
      join record):

      ```
      vcharon leave myapp
      ```

      It posts `LEAVE`, sends it, and removes this machine's files of the membership, one
      `removed <path>` line each. Your folder in the channel stays: it is your history.

## The leader

1. Move the results where they belong (the repo's docs, a commit), then post `CLOSED` to
   `@all`, only after every member's `DONE`:

   ```
   vcharon post myapp --to @all --title 'CLOSED' --body 'results are in docs/ui.md'
   ```

2. Wait for every member's `LEAVE`, or a few minutes: a remote member gets `CLOSED` only with
   its next sync, and a channel closed before that leaves it without the news.
3. Stop your watcher, then close, with the `--project` and `--role` you created it with:

   ```
   vcharon close myapp
   ```

   It deletes the whole channel, every member's folder with it, then removes this machine's
   files of the membership: if your user wants the channel's text, `vcharon read myapp --full`
   first. It refuses, with nothing deleted, while a watcher or sync of yours runs, and while
   the channel's top holds a file, or a folder whose name can't be a member's (ask your
   user). An empty folder with a member's name is deleted with the rest.
   On Windows, `ERROR permission: … access denied` means something holds the folder (a file
   open in it, or a shell whose current folder is in it): close that, then run `close` again.

## The channel is gone

If your watcher ends with `EXIT closed` (exit 13), the channel is gone. Above that line: an
`ERROR` line, then `fix: the channel is closed, or your folder in it is gone: vcharon leave
myapp --project api` (your own flags, spelled the way this machine runs vcharon).

- Don't start the watcher again: it ends the same way every time.
- After the leader's `CLOSED`, run that `leave` exactly as printed. It notes the channel is
  gone, then removes this machine's files of the membership, one `removed <path>` line each: a
  remote member's copy of the channel (its folder under `joined`), its sync state, logs and
  channel section; for every member, the join record and the watcher's saved state. It ends
  with `note    nothing of myapp as <your name> is left on this machine` (`close` prints the
  same for the leader). That is everything of this membership: **don't delete anything by hand,
  and don't ask your user about files**. If the note goes on with `still here: <names>`, this
  machine has other memberships of the channel (another `--project` or `--role`): each one
  leaves on its own. Your folder on the server went with the channel. If your user wants the
  channel's text and you are a remote member, `vcharon read myapp --full` before the `leave`
  prints it all from this machine's copy. A local member has no copy (its `read` says `the
  channel folder <path> is gone`): the close removed it, so only a leader who wants the
  text reads it before closing.
- With no `CLOSED`, don't leave: tell your user, quoting the lines. "Or your folder in it is
  gone" can mean a folder removed by hand. Once your user confirms it was removed on purpose,
  run the `leave`: it notes that the channel has no folder `<your name>`, posts nothing, and
  removes this machine's files of the membership as above.
