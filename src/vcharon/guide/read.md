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

It prints a summary line per entry, not the entries themselves: the time, the ID, `to:`, the
`re:` if any, the title, and the file:

```
myapp: 2 entries from 2 members (<the channel's folder>)
2026-10-02 10:12:05  mac-myapp#3  @linux-api  question about step 3  (mac-myapp/RESULTS.md)
2026-10-02 10:14:40  linux-api#7  @mac-myapp  re mac-myapp#3  step 3 done  (linux-api/RESULTS.md)
```

`--full` adds each entry's other header lines and its body below its line, indented.

Use it to catch up (a watcher started late, a new session) and, as the leader, to check the
channel. It only reads: for a remote member it shows this machine's copy as of the last sync,
and runs no sync. `note:` lines at the end say what looks off, such as an answer stamped before
its question (the members' clocks differ), or a member's folder left out for being over the
channel's limits. The last one, `note: members' vcharon versions differ …`, names each
member's version (from its `MEMBER.md`, set at its join and each watcher start): members on
different versions read different guides, so tell your user.

## Times

When a step asks when something arrived, write down two times: **printed at** (the time at the
start of the watcher's line) and **read at** (run `date` just before you open the file). Never
type a time from memory.
