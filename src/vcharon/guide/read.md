# Read: reading entries

## What the watcher named

1. Read **every entry the watcher named**, in the order printed. Each line ends with the file,
   `(<folder>/<file>)`, relative to the channel's folder: `vcharon whoami myapp` prints it as
   `folder` (your own; the others are next to it).
2. Read the whole entry: its `to:`, its `re:` and its body. `vcharon read myapp --last 5
   --full` shows the newest ones with their bodies.
3. Read the leader's `STEPS.md` and the entries addressed to you before you start any patch.
4. An update is complete when its entry is there. If a file the entry names is missing, wait a
   round: a remote member's files arrive one at a time.

## The whole channel in one order

```
vcharon read myapp                 # one line per entry, every member's, oldest first
vcharon read myapp --last 20       # only the newest 20
vcharon read myapp --full          # with each entry's header lines and body
vcharon read myapp --json          # one JSON object: the channel, the members, the entries
```

Use it to catch up (a watcher started late, a new session) and, as the leader, to check the
channel. It only reads: for a remote member it shows this machine's copy as of the last sync,
and runs no sync. `note:` lines at the end say what looks off, such as an answer stamped before
its question (the members' clocks differ), or a member's folder left out for being over the
channel's limits.

## Times

When a step asks when something arrived, write down two times: **printed at** (the time at the
start of the watcher's line) and **read at** (run `date` just before you open the file). Never
type a time from memory.
