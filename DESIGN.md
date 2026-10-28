# VCharon — design

VCharon gives AI agents file-based channels: on one machine, or across machines over plain ssh,
with nothing installed on the server. Each agent owns one folder in a channel, writes only that
folder, and reads everyone else's. A remote agent syncs over ssh: its own folder up, the others'
down, in one run.

This file is how VCharon works now: each rule, with the reason for it. It is edited in place
when a rule changes, in the same commit as the code. It holds no history: what shipped in which
version is in [CHANGELOG.md](CHANGELOG.md), why one change was made is in its commit message.
Setup and use are in [README.md](README.md); what an agent needs is the guide
(`vcharon guide`, [docs/GUIDE.md](docs/GUIDE.md)).

Code comments cite this file by section name: `(DESIGN, "Which membership")`. A test checks that
every such name is a heading here.

## Contents

- [Scope](#scope), [Decisions](#decisions), [Terms](#terms)
- [How a sync works](#how-a-sync-works), [Files](#files)
- [Session](#session), [Wire protocol](#wire-protocol), [Plans](#plans), [Plugins](#plugins)
- [Applying a plan](#applying-a-plan), [State and locks](#state-and-locks), [Config](#config)
- [Channels](#channels): the shared root, names, the claimer, records, which membership, the
  lobby, entries, formats, limits, the watcher, reading, clocks
- [Command line](#command-line), [Stable](#stable)
- [Tests](#tests), [Rules for the code](#rules-for-the-code),
  [Not in this version](#not-in-this-version)

## Scope

| role | OS | Python |
|---|---|---|
| client (where an agent runs) | Linux, macOS, Windows 10 or 11 | the binary's own; 3.13 or later for pipx, uvx or a source checkout |
| server for remote members | Linux. Debian 13 or later is the supported one; `doctor` and `ping` warn on any other distro and go on | its `python3`, 3.13 or later |
| channel root for local members only | any of the three | – |

- The client always opens the connection. The server can't reach the client (NAT, firewalls, no
  ssh server on a laptop), so the server never starts anything.
- The server needs `python3` and nothing else.
- Each member folder has one writer. A remote member's sync is two one-way moves, its own folder
  up and the other folders down: never a two-way sync, which would need merging.
- VCharon trusts a server only as far as a sink's root: a pull applies the plan the server
  sends, but only inside the local tree of that channel, and deletes only up to `max_deletes`
  (a lobby's down has no such limit: [The lobby](#the-lobby)).

Not in scope: two-way sync, the server connecting back (reverse forwards, listeners), a
background service, VCharon's own ssh code, user-defined jobs, macOS or Windows as the server
for remote members, a GUI.

## Decisions

| decision | why |
|---|---|
| Python, standard library only, on both ends | Every server has `python3`. Nothing to install there, and the helper can be sent as source code |
| Python 3.13 or later everywhere | One language level for both ends. The binaries bundle 3.13, the server VCharon targets (Debian 13) ships 3.13, and a client running the binary never uses its own Python; one floor means one CI row fewer and no code for older versions |
| The system OpenSSH client, never an ssh library | It reuses your keys, agent, `~/.ssh/config`, `known_hosts` and jump hosts |
| One ssh connection per sync; up and down share it | Windows' OpenSSH can't share a connection between runs (no ControlMaster), so fewer connections is the only way to save handshakes there |
| Nothing installed on the server | On every connection the client sends the loader and the sha256 of the helper's code, and the code itself only when the server has no copy that matches it; the loader checks a kept copy each time, so both ends always run the same version; the copies are a cache anyone can delete |
| INI config, read with `configparser` | Windows paths need no quoting or escaping |
| Sources and sinks are plugins; two are built in, `path` and `dir` | A channel's sync is two ordinary one-way jobs; the engine knows nothing about channels |
| Flat verbs, the channel first: `vcharon post C …` | Channels are the only thing VCharon has; an explicit channel name is easy for an agent to get right and to read back in a log |
| The tool works out who you are: no `--me`, `--job`, `--dir` or `--config` | An agent can't pass the wrong name if it never passes one |
| Member names come from the machine and the checkout, never from the agent's choice | A new session in the same checkout gets the same name back with no memory ([Member names](#member-names)) |
| No host name, path, user name or raw machine id in any file VCharon writes to a channel; no raw machine id in what it prints or logs either | A channel's files are shared; a host name can name an employer or a network. machine-id(5) asks that the id be kept private, and agents paste VCharon's lines into entries |
| Never prompt, except `vcharon key` (it runs `ssh-add` on your terminal) | Agents and background commands have no terminal; a prompt would hang them |
| One binary per platform, plus pipx and uvx from GitHub | Nothing to set up on a client; the guide inside the binary always matches the binary |

## Terms

| term | meaning |
|---|---|
| client | the machine an agent runs `vcharon` on |
| server | the Linux machine a remote member's ssh connects to; it holds the channel root |
| channel root | the folder that holds the channels on one machine, `~/.local/state/vcharon/channels` |
| channel | one folder in the root: one folder per member below it |
| member | an agent with its own folder in a channel. A **local member** (`--local`) runs on the machine that holds the root and writes there directly; a **remote member** (`--server ALIAS`) runs on another machine and syncs over ssh |
| leader | the member that leads a work channel: its creator, until it hands the lead to a member ([Leader handoff](#leader-handoff)); it writes `STEPS.md`, alone posts to `@all`, and closes the channel |
| charter, term | a `CHANNEL.md` entry that says who leads: create's #2 is term 1, and each takeover writes one of the next term into the taker's own `CHANNEL.md`; the leader holds the highest term ([Leader handoff](#leader-handoff)) |
| work channel, lobby | the two kinds of channel: one made by `create`, with a leader and a plan; and the one `lobby` of a channel root, made by its first `join`, where any member posts to `@all` and nothing closes it ([The lobby](#the-lobby)) |
| founder | the lobby member whose folder holds its `CHANNEL.md`: its first joiner, with no power the others lack |
| member name | `<box>-<project>[-<role>]`, also its folder's name |
| box, project, role | the parts of a name: this machine's name in VCharon, the checkout's folder name, and a short tag that tells two sessions in one checkout apart |
| record | this machine's note of one membership, `<state>/channels/<C>.<name>.json` |
| channel section | a remote member's two sync jobs, in `channels.d/<C>.<name>.ini` |
| local tree | a remote member's copy of a channel, `<joined>/<C>.<name>/` |
| entry | one message: a block in a member's `.md` file, headed `## <time> — <name>#<n> — <title>` |
| watcher | `vcharon watch`: prints the entries that reach a member |
| `fix:` line | the last line of every refusal: what to run, or what to do |
| controller | the `vcharon` process on the client |
| helper | VCharon's code running on the server for one connection; it arrives over ssh stdin and exits with the connection |
| hello | the helper's first message: protocol, version, OS, distro, machine id, clock |
| job | a source on one end and a sink on the other. A channel section makes two, `<C>.<name>.up` and `<C>.<name>.down` |
| sync | one `vcharon sync C`: the section's two jobs on one connection |
| plan | what a source produces: puts and deletes, plus optional identity and state ([Plans](#plans)) |
| source, sink | the plugin that builds a plan and serves file bytes; the plugin that checks a plan and applies it under its root |
| state | what a job saved after its last good run ([State and locks](#state-and-locks)) |
| claimer | a hash that says which machine made a member folder ([The claimer](#the-claimer)) |
| format | the version of a channel's file layout, in `CHANNEL.md` ([Formats](#formats)) |

## How a sync works

```
vcharon sync C
 1  controller  find the membership, load the config, take both jobs' locks, load their state
 2  controller  start  ssh <options> <dest> "python3 -I -c '<bootstrap>'"
 3  controller  send the loader and VCharon's own code over stdin
 4  helper      print the ready marker, then send hello (with the server's machine id)
    then for up (your folder to the server), and then for down (the others to you):
 5  source      build the plan: what changed since the last good run   (on the source's end)
                controller: the source's identity must match the saved state
 6  sink        check the plan                                          (on the sink's end)
                controller: the sink's machine and root must match the saved state
                --dry-run prints the plan and stops here
 7  transfer    the planned file bytes the sink doesn't already hold stream to the sink's
                end in one go; the sink stages them; its root isn't touched yet
 8  sink        commit: deletes first, then move staged files into place
 9  controller  save state, release the lock, print the result
 10 controller  say bye
```

| job | the source runs in | the sink runs in |
|---|---|---|
| up (push) | the controller | the helper |
| down (pull) | the helper | the controller |

Steps 5 to 8 are one piece of code for both directions, talking to a source object and a sink
object; each is local (a direct call) or remote (a proxy that sends calls over the connection).
Push and pull differ only in which of the two is a proxy.

- Both jobs are checked and both locks taken before ssh starts: one bad or busy job and neither
  runs, since a half sync would leave the agent guessing which half ran.
- The helper's `job.reset` comes between the two jobs, so down starts from a clean helper.
- After up fails, down still runs on the same connection, unless the connection broke (lost, a
  timeout, a protocol error): a blocked up must not also stop the member from reading.
- A sync costs a fixed number of round trips, however many files it moves: a pull is plan plus
  send, a push is check plus receive plus commit. A plan with no entries skips the transfer and
  the commit. Plugins never make one call per file: over a slow link that would crawl.

### Repeated syncs

`vcharon sync C --repeat S` (S 1 to 300) keeps one session and syncs again and again, a round
every S seconds counted from the end of the last, until its stdin ends. The watcher of a remote
member runs it as one long-lived child ([The watcher in a channel](#the-watcher-in-a-channel)).

- A round runs up and down once each, taking each job's lock only for its turn, never between
  rounds: a by-hand `vcharon sync C --full` gets in between. A busy job sits that round out.
- Output: everything per round goes to stdout at the round's end: each failed job's error block
  (its `ERROR <job>: …` line names the job), then `ROUND <code>`, the code a single sync would
  have exited with. A good job prints nothing. stderr holds only what comes before the first
  round, and a crash. Why: the watcher reads rounds from one ordered stream.
- End of stdin ends it after the round under way, with exit 0. stdin's end is read from the raw
  file descriptor by a thread: a thread blocked in Python's buffered reader holds its lock and
  makes the interpreter abort at exit.
- A round that leaves its session unusable ends it with that round's code: an error that breaks
  the connection (`connect`, `protocol`, `timeout`, `lost`), or any other that does (an
  `internal` error inside the session). It never reconnects by itself. Any other failed round is
  followed by the next one on the same connection. The watcher tells this exit by its code, the
  last `ROUND`'s, with nothing more on stdout or stderr, not by the error's text: a list of codes
  there would drift from the child's own rule.
- Quiet on disk: a job's state is written only when it changed, and a round with nothing to do
  logs nothing; the log gets one line every 10 minutes (`repeat: <n> rounds since <time>,
  nothing to do in <m>`). Else a quiet watch would fsync two files and grow the log every two
  seconds. The exceptions are a watcher's last-watched stamp at the server and the client's
  copy of the ages, each at most one small write every 30 s, not fsynced: without them nothing
  shows that a watcher still runs ([The watcher in a channel](#the-watcher-in-a-channel)).
- Quiet on the wire: a round with nothing new sends its down state by reference and gets it
  back the same way, and the members' ages only on a change or about every 30 s
  ([Calls](#calls)), so its down's `source.plan` carries a few hundred bytes each way, whatever
  the channel's size. Key order counts in "changed": a state the source gives back reordered (a
  member folder over the limits has its entries moved to the end) is written once, so later
  rounds go by reference again.
- `run_timeout` counts per round. Refused with `--full`, `--dry-run` or `--reset`.

## Files

```
src/vcharon/
  __init__.py      VERSION, PROTOCOL, FLOOR = (3, 13)
  __main__.py      python -m vcharon
  cli.py           the command line: verbs, output, exit codes; the sync runner       client
  channel_cmd.py   list, create, join, leave, close: names, records, sections         client
  channels.py      the channel root: list, claim, release, remove                    both
  charter.py       a channel's format and limits, from CHANNEL.md                    both
  entries.py       entry text, parsing, numbers, the locked append                    both
  config.py        vcharon.ini and channels.d/                                        client
  doctor.py        vcharon doctor                                                     client
  keys.py          vcharon key                                                        client
  state.py         state files                                                        client
  lock.py          file locks for jobs, stage dirs, posts and watchers                both
  ssh.py           the ssh command, start-up, reader threads, watchdog                client
  bundle.py        the bootstrap line, the loader, the bundle                         client
  remote.py        proxies: RemoteSource, RemoteSink                                  client
  run.py           the engine: steps 5 to 8 for either direction                      client
  helper.py        the helper's main loop                                             server
  proto.py         frames, messages, file streams, error codes                        both
  plan.py          plan types, JSON form, shape checks                                both
  pathrules.py     path rules for each receiving OS                                   both
  fsops.py         file kinds, OS errors, directory handles, tree walks, other programs both
  stage.py         staging and commit                                                 both
  plugin.py        plugin base classes, registry, options                             both
  platform.py      OS name, folders, machine and client id, how this install runs     both
  log.py           log files                                                          client
  plugins/         path.py (source), dir.py (sink)                                    both
  mailbox/         post.py, read.py, watch.py: the verbs behind post, read, watch     client
  guide/           the agent guide, one .md file per topic                            client
  skill/           SKILL.md, which vcharon skill install writes                       client
tests/             unittest, standard library only
docs/GUIDE.md      made from src/vcharon/guide/*.md; a test fails when they differ
```

The bundle sent to the server holds the "both" and "server" modules and the package's
`__init__.py`, the ones `bundle.SERVER` names, and nothing else: the client's code never travels.
`tests/test_bundle.py` walks the helper's imports, function-level ones too, from `helper.py` and
each plugin (`plugin.py` loads plugins by name, which no import shows), and fails when the helper
can reach a module the list leaves out, or when the list names one it can't reach: a module the
helper needs is never missing, and the list can't grow stale. A module on the list whose source
file is missing (a binary built without it) stops the session on the client, before ssh starts
(`internal`, naming the module; the fix line says to install vcharon again), and fails
`doctor`'s `bundle` line, rather than showing up as an `ImportError` on the server.

## Session

### The ssh command

The controller starts ssh with an argument list, never a shell string:

```
<ssh> -T -e none
      -o BatchMode=yes
      -o ConnectTimeout=<connect_timeout>
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3
      -o ControlMaster=no -o ControlPath=none      (on Windows, and in every doctor or key probe)
      -C                                           (only when compress = yes)
      <dest>
      <remote_python> -I -c 'import sys,base64;exec(base64.b64decode(sys.stdin.buffer.readline()))'
```

The last line is one argument, and the only text that ever goes through the server's shell.

| option | why |
|---|---|
| `-T` | No terminal on the server, so bytes pass through unchanged |
| `-e none` | No escape character; a second guard beside `-T` |
| `BatchMode=yes` | Never prompt: an agent's command has no terminal, and a prompt would hang it |
| `ConnectTimeout` | An unreachable host fails in seconds, not after a long TCP stall. It covers the connect and key exchange; `handshake_timeout` covers the rest |
| `ServerAlive*` | A dead link fails within about 45 s instead of hanging |
| `ControlMaster=no`, `ControlPath=none` | Windows' OpenSSH can't share connections and fails if a shared `ssh_config` asks it to. Probes use them everywhere, so a live shared connection can't make a broken key look fine |
| `-I` | Python's isolated mode: the current folder isn't on `sys.path`, and `PYTHON*` variables and user site-packages are ignored, so nothing in the server's home can replace VCharon's code |

- `<ssh>`: config `ssh_path`. Default on Windows: `%SystemRoot%\System32\OpenSSH\ssh.exe`
  (`Sysnative` from a 32-bit Python, which Windows would redirect), never Git for Windows' ssh,
  which can't use the Windows ssh-agent service. macOS and Linux: `/usr/bin/ssh`.
- `<dest>`: anything ssh accepts: an `~/.ssh/config` alias (best), `user@host`, or
  `ssh://user@host:port`. VCharon never parses it, but refuses one that is empty, starts with
  `-` (ssh would read an option) or holds whitespace or control characters.
- `<remote_python>`: config `remote_python`, default `python3`. It goes into the server's shell
  command as it is, so it may hold no quotes or backslashes and can't start with `-`.
- On macOS and Linux, ssh starts in a new session: with no controlling terminal it can't prompt
  even where BatchMode doesn't reach. Ctrl-C still reaches VCharon, which kills ssh.

### Start-up

1. The controller writes one line to ssh's stdin: the loader's source, base64, then `\n`.
   Then a second line, `<nonce> <sha256>\n`: the session's random nonce, and the sha256 of the
   bundle, which is UTF-8 JSON `{"packages", "modules": {name: source}}`, compact and with its
   keys sorted, so one install always hashes the same. The modules are those `bundle.SERVER`
   names, which a test keeps equal to what the helper can import ([Files](#files)). The
   controller builds and hashes the JSON on every connection: a hash fixed at build time would
   go stale in an editable install, and the server would run an old helper under an unchanged
   version. The nonce stays out of the JSON, or no two sessions would hash the same.
2. The loader, on the server, exits with code 90 and one line if Python is older than `FLOOR`.
   It parses on any Python 3 from 3.6 on, so an old server gets the version message, not a
   syntax error. It then looks for `cache/helper-<sha256>` beside the channel root (by default
   `~/.local/state/vcharon/cache/`; it follows `VCHARON_CHANNELS_ROOT` as the root does, and
   has its own copy of `channels.root_path()`'s rule, since it runs before any of VCharon's
   modules; a test keeps the two equal). A copy whose sha256 matches is used and its mtime
   touched. It is hashed on every use: nothing on the server is trusted, only reused.
3. Without a matching copy the loader writes `VCHARON-NEED <nonce>\n` to stdout. The controller
   finds it the way it finds the marker (step 6), then sends the bundle: an 8-byte big-endian
   length N, then N bytes of the JSON zlib-compressed. It compresses only then: zlib is most of
   the cost of building it. The loader exits with 91 if the bundle is malformed or its sha256
   isn't the one on the second line (64 MiB caps the compressed bundle, the JSON and a cached
   copy), then keeps it: a temp file in `cache/`, then `os.replace`; `cache/` is made 0700. A
   failed write (a read-only home, a quota) is one stderr line, and the session goes on from
   memory. After a write it keeps the 8 newest `helper-*` by mtime (about one per client
   version that connects: the client reads the sources with LF line ends whatever its OS, so
   every OS sends the same bundle) and removes temp files older than an hour, left by a loader
   that was killed. It opens a copy non-blocking and reads it only if it is a regular file: a
   FIFO there would block forever.
4. It puts an import finder serving the bundle from memory at the front of `sys.meta_path`, so
   nothing on disk shadows it, then calls `vcharon.helper.main(nonce)`.
5. The helper moves the protocol output off fd 1 (`os.dup(1)`, then `os.dup2(2, 1)`), so
   neither a stray `print` nor a child process can write into the protocol. It reads stdin only
   through `sys.stdin.buffer`, which may already hold the next bytes; children never get fd 0.
   It writes `VCHARON-READY <nonce>\n`, then a `hello`.
6. The controller drops every byte before the marker (and before `VCHARON-NEED`) and logs up to
   4 KiB of it: shell startup files on the server may print, and the marker keeps that out of
   the protocol. If ssh exits before the marker, the connection fails at once ([Failures before
   the helper runs](#failures-before-the-helper-runs)). `handshake_timeout` runs to the marker,
   so it covers a miss's extra round trip. The session records whether it sent the bundle:
   `ping`'s `connect` line and `doctor`'s line for the server end in `bundle sent` or `bundle
   cached`.

### Threads, timeouts, shutdown

- The helper is single-threaded.
- The controller's reader thread reads ssh's stdout. It logs `log` and `tick` messages itself
  and queues every other frame (up to about 16 MiB): the helper never blocks on a full pipe, a
  slow receiver slows the sender instead of filling memory, and helper logs can't fill the
  queue.
- Another thread reads ssh's stderr into the log, keeping the last 20 lines for error messages,
  so a full stderr pipe can't stall ssh.
- A watchdog kills ssh when the ready marker hasn't come `handshake_timeout` (30 s) after start;
  when nothing has moved for `idle_timeout` (300 s) while the controller waits on the helper
  (local work doesn't count); or when the whole run passes `run_timeout` (0, no limit). The run
  fails with `timeout`, or `connect` before the marker.
- Long loops in the helper call `ctx.tick()`, which sends a `tick` at most every 10 s, so a busy
  helper never looks idle.
- If a write to ssh fails because the helper exited, the controller reads stdout to the end to
  pick up the helper's `err`, then fails.
- At the end the controller sends `bye`, closes stdin, waits up to 5 s, then kills ssh.
- The helper removes its stage dir whenever it exits without a commit.

### Failures before the helper runs

| what happened | how the controller knows | `fix:` |
|---|---|---|
| host key unknown or changed | ssh exit 255, `Host key verification failed` | run `ssh <dest>` once in a terminal |
| no usable key, or key refused | ssh exit 255, `Permission denied` | add your key to the server, or run `vcharon key <dest>` |
| key accepted but locked by a passphrase | the `-v` probe of `doctor` and `key` (ControlMaster off) names a key file here, then ssh exits 255 with `Permission denied` | `vcharon key <dest>` |
| network | exit 255 with `Connection refused`, `timed out`, `Could not resolve`, … | check the host, port, VPN; for `Could not resolve`, first that the alias matches a `Host` line in `~/.ssh/config` exactly (its patterns are case-sensitive, so `devbox` doesn't match `Host DevBox`) |
| no Python on the server | exit 127 | install `python3`, or set `remote_python` |
| `remote_python` isn't runnable | exit 126 | check `remote_python` |
| Python too old | exit 90 | install 3.13 or later |
| VCharon's code didn't load | exit 91 | a bug; see the log |
| ssh exited before the marker in any other way | exit 255 otherwise | try again, else run `ssh <dest>` in a terminal; ssh's last stderr line goes into the error itself, `(ssh: <line>)`, since a watcher shows only the error and its fix |
| the bootstrap exited before the marker | any other code | a shell startup file may have read stdin; see the log |
| no marker within `handshake_timeout` | the watchdog | authentication or a jump host may be stuck |
| the ssh client can't be started | starting `ssh_path` fails | install the OpenSSH client, or set `ssh_path` |

All of these exit 4. They apply only before the marker: after it, any unexpected exit is
`lost` (exit 1), since a helper killed by a signal also makes ssh exit 255 and its traceback may
hold "Permission denied".

### Keys without prompts

BatchMode means ssh can't ask for a passphrase. A key with one works only while an agent holds it
unlocked, and most agents forget keys at logout or reboot, so a sync that worked yesterday fails
today. `vcharon key [<dest>] [--key FILE]` makes the unlock last, with each OS's own store:

| client | where the unlocked key lives | survives a reboot |
|---|---|---|
| macOS | ssh-agent, the passphrase in the Keychain (`ssh-add --apple-use-keychain`) | yes, with `UseKeychain yes` |
| Windows | the OpenSSH Authentication Agent service | yes |
| Linux | the agent of your login session, or one your ssh login forwards | no; once per login |

- It needs a terminal (stdin and stdout); it's the only verb that may prompt, and the prompt is
  `ssh-add`'s: VCharon never sees, stores or logs a passphrase.
- It picks the key from `--key`, or from a `-v` probe: the session's ssh command with `-v`
  (BatchMode, ControlMaster off) and stdin an empty pipe closed at once, so the bootstrap reads
  nothing and runs nothing. Not the null device: Win32-OpenSSH never ends the remote side's input
  when its own stdin is the null device. It reads only `debug1: Server accepts key:` lines, up to
  `Authenticated to`, decoding ssh's escapes.
- It unlocks a key file the server accepts and the agent doesn't hold; does nothing for a key
  the agent alone holds (a forwarded agent: nothing to unlock); and stops before `ssh-add` when
  no agent answers: with the admin commands when Windows' agent service is off, with how to start
  an agent when Linux has none, and on macOS with "open a new Terminal window" (macOS starts an
  agent for each login session; a stale `SSH_AUTH_SOCK`, as in a tmux server from an earlier
  login, names none), since `ssh-add` would only fail and a passphrase hint wouldn't help.
- macOS: after `ssh-add --apple-use-keychain` it prints the lines to put at the top of
  `~/.ssh/config` (`IgnoreUnknown UseKeychain`, then a `Host` block with `IdentityFile`,
  `UseKeychain yes`, `AddKeysToAgent yes`) and never edits the file itself.
- With a destination it ends with a BatchMode login test and prints `ok` or `FAIL`.
- `vcharon doctor` only reads: for a locked key it points at `vcharon key`.

Unattended use with nobody logged in needs a key without a passphrase, kept on that client
only. A `command=` line in `authorized_keys` can't restrict it: the forced command would be the
bootstrap line, and the helper runs whatever code the client sends.

## Wire protocol

### Frames

After the ready marker, both directions carry frames:

```
frame = kind (1 byte) + length (4 bytes, big-endian, unsigned) + payload

J (0x4A)  one JSON object, UTF-8, at most 64 MiB
D (0x44)  file bytes, 1 to 262144 bytes
E (0x45)  end of one file's bytes: empty = complete, else a JSON error object
```

An unknown kind, or a length over its limit, is a protocol error and ends the connection.

### Messages and file streams

```
controller → helper   {"t": "call", "id": 7, "fn": "source.plan", "args": {...}}
helper → controller   {"t": "ok",   "id": 7, "result": {...}}
                      {"t": "err",  "id": 7, "error": {"code", "message", "hint", "detail"}}
                      {"t": "log",  "level": "info", "msg": "..."}
                      {"t": "tick"}
                      {"t": "hello", ...}       once, right after the ready marker
```

- One call at a time; the controller reads until the reply with the same `id`.
- `log` and `tick` may come anywhere, even inside a file stream.
- `detail` holds a traceback for `internal` errors: the log gets it, never the screen.
- A file stream: for each file `J {"t": "file", "index": i}`, D frames, then E; after the last,
  `J {"t": "end"}` (with an `error` if it couldn't go on). Upload (`sink.receive`): the
  controller sends it right after the call. Download (`source.send`): the helper sends `ok`,
  then the stream.
- A sender that fails to read one file ends it with an error E frame and the stream with `end`
  plus that error. A receiver that fails keeps reading to `end` before it reports, so both
  sides stay in step. Either way nothing is committed.
- After a protocol error the helper sends `err` (`"id": null` if it can't tell which call) and
  exits.

### hello

```json
{"t": "hello", "protocol": 4, "version": "0.1.0", "python": "3.13.5", "os": "linux",
 "distro": "Debian GNU/Linux 13 (trixie)", "distro_id": "debian", "distro_version": "13",
 "machine": "<32 hex>", "user": "me", "home": "/home/me", "shared_root": true,
 "utc_offset": 3600, "time": 1790000000.25}
```

- The controller requires `protocol` and `version` to equal its own: both ends run the same
  bundled code, so a mismatch is a bundling bug. Members of one channel may run different
  versions: each client sends its own helper.
- `machine`: `/etc/machine-id` (or `/var/lib/dbus/machine-id`). It ties a job's state and a
  record to the actual server. A server with none can't take remote members. Stored and
  compared as it is; shown only hashed ([Rules for the code](#rules-for-the-code)).
- `os` must be `linux` for a remote member: a Mac or Windows root takes local members only.
- `shared_root`: whether the helper can hold a shared root ([The shared root](#the-shared-root)),
  by its own test, the one that refuses `--shared`. `list` asks for the shared group only when
  it is true. Why: `os` says `linux` for any OS that isn't macOS or Windows, so a gate on it
  could ask a helper that refuses.
- `distro`, `distro_id`, `distro_version`: `PRETTY_NAME`, `ID` and `VERSION_ID` from
  `/etc/os-release`. `ID=debian` with `VERSION_ID` 13 or later is the supported server
  (`doctor --json`'s `"tested"`); anything else gets a warning from `doctor` and `ping`, never a
  refusal.
- `time` (set last, so it leaves out the time the other fields took) and `utc_offset` give
  `doctor`'s clock and time-zone lines ([Clocks](#clocks)).

### Calls

| fn | args | result | stream |
|---|---|---|---|
| `echo` | – | `{}` | upload one file (at most 16 MiB), then the same bytes back |
| `plugin.doctor` | `plugin`, `role`, `options` | `{"checks": [[level, message, hint], …]}` | – |
| `source.plan` | `plugin`, `options`, `state` or `state_ref`, `full`, optional `watch`, optional `want_seen` | a plan, with `state_ref` in place of an unchanged `state`; a channel's down also `seen` or `seen_error` | – |
| `source.send` | `indexes` | `{}` | download of those file puts |
| `source.state_after` | `written`, `deleted` | `{"state": …}`, or `{"state": null}` | – |
| `sink.check` | `plugin`, `options`, `plan` | `{"root", "notes", "deletes", "have"}` | – |
| `sink.receive` | `indexes` | `{"staged": n}` | upload of those file puts |
| `sink.commit` | – | `{"written", "deleted", "deletes_done", "notes"}`; an `err` also carries `"done"` | – |
| `sink.abort` | – | `{}` | – |
| `job.reset` | – | `{}` | – |
| `channel.list` | optional `shared` | `{"channels": [{"name", "members", "fields", "leaders", "leader", "term", "tied", "strays", "newest", "idle", "items", "format", "limits"}], "others": [{"name", "why"}], "missing"}` | – |
| `channel.claim` | `channel`, `name`, `create`, optional `lobby`, optional `claimer`, optional `shared` | `{"existed", "machine", "root", "claimer", "format", "limits"}` | – |
| `channel.release` | `channel`, `name`, optional `shared` | `{"removed"}` | – |
| `channel.remove` | `channel`, `name`, optional `lobby`, optional `shared` | `{"closed", "deleted"}` | – |
| `channel.prune` | `channel`, `name`, `days`, `budget`, optional `shared` | `{"kept", "idle", "items", "removed", "deleted"}` | – |
| `channel.lead` | `channel`, `name`, `number`, `mode` (`offer` or `gone`), `time`, optional `shared` | `{"term", "after", "from", "charter", "steps", "steps_cut"}` | – |
| `bye` | – | `{}` | – |

- The helper holds at most one source and one sink per job. `job.reset` drops them (a sink
  that didn't commit drops its stage dir) so the next job can plan and check again.
- `plugin.doctor` sets neither: doctor only reads.
- File streams name puts by their index in the plan, so only planned entries move.
- `have` lists the hashed file puts whose bytes the target already holds ([Full
  syncs](#full-syncs)); the controller refuses a `have` naming anything else.
- `state` in `source.plan` is the job's saved state, or `{}`. The plan `sink.check` gets has no
  state: the sink never reads it, and it can be large.
- States by reference: the helper keeps, for its session, the last 4 states `source.plan` got
  or gave back, each by its `state_ref`, the sha256 of the state as a call's JSON encodes it
  (key order counts). The client counts them the same way, and sends `state_ref` in place of
  `state` only for a state its helper holds: never at the session's first plan of it, so not
  after a reconnect (a new session) or a `--reset`, and never with `--full`. A plan whose state
  came back unchanged gives `state_ref` in place of `state`, and the client keeps the state it
  sent, so the state file is byte for byte what the whole state would give; a changed one comes
  back whole. A reference the helper doesn't hold is `state_unknown`, which keeps the session:
  the client sends the state whole in a second `source.plan` of the same round. Never a guess.
  Why: a streaming watcher's idle round otherwise sent the whole channel's listing both ways
  every round, a cost that grew with the channel. A `--no-stream` watcher opens a session each
  round, so its state still goes whole.
- `deletes_done` lists the deletes a commit got through; `source.state_after` uses it after a
  commit that failed partway.
- `watch` in `source.plan` is a watcher's pace: the helper stamps the member's last-watched time
  after the plan. A down of a channel in the root or the shared root (`mailbox_me` set) gets
  `seen`, `{member: [age seconds, pace]}`, beside the plan's fields, or `seen_error` with why
  not; the client takes both out before reading the plan ([The watcher in a
  channel](#the-watcher-in-a-channel)).
  `seen` comes at the session's first plan of the channel, when a member or a pace changed since
  the last one the session sent, and when the call has `want_seen`, which the client sends at
  its first plan of the channel in the session, then once 30 s have passed both since the last
  `seen` came and since its copy was written, so the ages still reach it about every 30 s.
  Why: the ages of every member, in every round, were the reply's other cost that grew with
  the channel.
- The `channel.*` calls work on the channel root: the fixed `~/.local/state/vcharon/channels`,
  or `VCHARON_CHANNELS_ROOT` in the helper's environment (tests only); with `"shared": true`,
  the fixed shared root `/var/tmp/vcharon/channels`, after its checks ([The shared
  root](#the-shared-root)). Never an argument: every member must reach the same folder, so the
  call says only which of the two fixed roots, yes or no. Each call checks the names again,
  works only directly below the root, and never follows a symlink. `--local` calls the same
  functions in-process.
  `claim`, `release` (undoing a failed claim), `remove` and `prune` also sweep `seen/` beside
  the root: each `seen/<X>/` (and `lead/<X>/`: [Leader handoff](#leader-handoff)) whose channel
  folder is gone is removed. A new member's claim drops an earlier member's stamp of its name,
  and a join's claim, which sends the joining machine's `claimer`, drops the stamp of an
  existing folder whose `MEMBER.md` names that claimer: the join holds the watcher lock, so the
  stamp is from a watcher that isn't running (`whoami` shows `watched -` until the next round).
  Matched by claimer, not dropped by every claim: another machine's join, refused after the
  claim, must not blank a live member. Why there: those calls end or reuse channels, and the
  stamps of a channel that is gone are nobody's.
- `channel.claim`'s `root` is the root as a section's `mailbox.remote` spells it (`~/…`, the
  shared root's absolute);
  `claimer` is the `claimer:` of an existing folder's `MEMBER.md`, or null; `format` and
  `limits` are the channel's, read from its leader's `CHANNEL.md`. No reply carries a host name.
- `channel.list`'s `leader` is the folder holding the top term's charter (null for none, or a
  tie: `tied` names the folders then), `term` that term (null with no charter); `fields` holds
  each member's `vcharon` version too, for `lead --to`. `channel.lead`: [Leader
  handoff](#leader-handoff).
- `channel.list`'s `idle` is each member's idle seconds by the server's clock (null when
  unknown), `items` how many files and folders its folder holds; `channel.prune` removes one
  member's idle folder: [Removing idle members](#removing-idle-members). `channel.remove`'s
  `lobby`, sent only by `close lobby --yes`: [The lobby](#the-lobby).
- `channel.list`'s `missing` is true when the root's folder doesn't exist (it then lists
  nothing), so `doctor` tells a shared root not made yet from an empty one ([The shared
  root](#the-shared-root)).

### Error codes

Every error the user sees has one of these codes; the last column is the exit code.

| code | meaning | exit |
|---|---|---|
| `config` | a usage or config error | 3 |
| `bad_options` | a plugin option is missing or invalid | 3 |
| `missing_capability` | the plugin needs something this client lacks | 3 |
| `state_mismatch` | a state belongs to another config, server or root, or can't be read; or the server has no machine id; or it isn't Linux | 3 |
| `busy` | another run holds the lock | 2 |
| `connect` | couldn't connect, or couldn't start the helper | 4 |
| `timeout` | the watchdog stopped the run | 1 |
| `lost` | the connection closed unexpectedly after the helper started | 1 |
| `protocol` | a bad frame or message, or an unknown `fn` | 1 |
| `not_found` | a source path or a sink root doesn't exist, or the root, or the nearest existing folder above a missing root, isn't a directory; no channel of the name; a local member's channel folder gone; a member's own folder gone; a remote member's own folder here without its `MEMBER.md`; a file gone during a run | 1 |
| `unsafe_path` | a path breaks the receiver's rules; a source holds a symlink, a special file or a name that isn't UTF-8 | 1 |
| `unsafe_dir` | a folder under the root isn't yours, or others can write to it | 1 |
| `collision` | two paths become one on the receiver | 1 |
| `kind_change` | a put would replace a folder with a file, or the reverse | 1 |
| `too_many_deletes` | the deletes would remove more than `max_deletes` | 1 |
| `empty_source` | `prune` refused: the source is empty, but earlier runs sent files | 1 |
| `channel` | a refused channel check: a name taken, a channel missing or already there, no or several leaders, not the leader, a lock held, a record for another server or of an earlier channel of the name, a format or limit | 1 |
| `refused` | a file VCharon won't overwrite (`skill install`) | 1 |
| `vanished` | a planned file was gone, or no longer a regular file, when read | 1 |
| `in_use` | Windows: another program has the file open | 1 |
| `permission` | the OS refused access | 1 |
| `no_space` | the disk is full | 1 |
| `aborted` | the other side ended a file or a stream with an error | 1 |
| `io` | an OS error with no code of its own | 1 |
| `too_big` | a message would pass the 64 MiB limit of a J frame; a post over the channel's entry or folder limits; an up whose own folder is over the folder limits ([Limits](#limits)) | 1 |
| `internal` | a bug; `detail` has the traceback | 1 |
| `state_unknown` | `source.plan` named a state by a reference its helper doesn't hold (the two ends counted the states differently, as after a call that failed once the helper had taken the state); the client sends the state whole in the same round, so users don't see it | 1 |
| `update` | `vcharon --update` failed or was refused; `--json`'s `error` says why ([Self-update](#self-update)) | 1 |

## Plans

```json
{"entries": [
   {"op": "put",    "path": "docs",       "kind": "dir"},
   {"op": "put",    "path": "docs/a.md",  "kind": "file", "size": 1234, "mtime": 1790000000.25,
    "exec": false},
   {"op": "delete", "path": "old.md",     "why": "pruned"}],
 "identity": null, "state": {}, "notes": []}
```

- `path` is relative to the sink's root, uses `/`, and has no empty, `.` or `..` part, no
  leading `/`, no NUL, and is valid Unicode. No part starts with `.vcharon-stage-` in any case:
  those names belong to stage dirs.
- Two entries never share a path, except a delete plus a put of it (a replacement). Nothing is
  put below a path put as a file, nor deleted below a `tree` delete.
- A plan that breaks these is refused with `unsafe_path` or `collision`; one with the wrong JSON
  shape (an `mtime` that isn't finite or is more than 10^11 s from 1970 included) is `protocol`.
- A delete removes a file, or an empty folder; a folder that still has entries is kept with a
  note, unless `"tree": true`, which removes it with everything in it. `prune` never sets `tree`.
- `exec` is the owner's execute bit from a POSIX source; a Windows source leaves it out ("leave
  the bits as they are").
- `sha256`, in a `--full` sync only: each file put's hash ([Full syncs](#full-syncs)).
- `identity`: what the source is; saved and compared on the next run. `state`: what the source
  wants kept after a good run. `notes`: lines shown to the user.
- The sink applies deletes, then folder puts, then file puts, whatever the order in the plan.

## Plugins

### Plugin interface

A plugin is one module in `vcharon/plugins/`, listed statically in `plugins/__init__.py`: no
file-system scan, since in the helper every module comes from memory.

```python
NAME = "path"                # from = local:path
KINDS = ("source",)          # "source", "sink", or both
ENDS = ("local", "remote")   # where it may run
NEEDS = ()                   # commands that must exist on its end
CAPS = ()                    # client capabilities it needs
OPTIONS = {"path": Option(str, required=True), ...}

class Source(plugin.Source):
    def plan(self, state, full=False): ...    # a Plan
    def open(self, index): ...                # a binary reader for one file put
    def state_after(self, written, deleted):  # optional: the state after a failed commit
    def close(self): ...

class Sink(plugin.Sink):
    def check(self, plan): ...                # a stage.Checked: root, notes, deletes, have
    def stage(self, index, reader): ...       # stores bytes; the root isn't touched
    def commit(self): ...                     # a stage.Done
    def abort(self): ...
    def close(self): ...                      # aborts unless it committed
```

Each may define `doctor(self)`, which only reads. Each object gets `self.ctx`: `end`, `os`,
`caps`, `home`, `fs`, `log()`, `tick()` and `resolve(path, where)`.

- A doctor check that can't be made by reading says so in its row instead of guessing: on
  Windows `os.access` ignores ACLs, so it is never the proof that a path can be read or written.
  A file source is opened to show it can be read; on Windows the dir sink's write access isn't
  checked.
- A plugin does all its work on its own end and never calls the other end.
- A sink writes only through `stage.Stager` ([Applying a plan](#applying-a-plan)), which
  enforces "only inside the root, only planned deletes".
- A plugin runs other programs only through `fsops.run`.
- Option values are strings, converted by type on each end (bool `yes/no/true/false/1/0`, list
  comma-separated, int digits only); unknown options are refused, so a typo fails loudly. The
  controller checks both sides' options before it starts ssh.
- `~` means the home of the end the plugin runs on; a relative remote path is relative to the
  server's home (`ctx.home`), never the process's current folder. Local paths in config must be
  absolute or start with `~`: a command's current folder is unpredictable.
- `ctx.resolve` follows the end's path rules (`ntpath` for a Windows end, else `posixpath`), so
  the tests run every OS's rules on any OS. A Windows path needs a drive or a share. `~user` names
  another user's home only on this machine's own end.

### The path source

Source, either end. Options:

| option | default | meaning |
|---|---|---|
| `path` | required | a file or a folder |
| `keep_name` | no | put a folder's entries under its own name |
| `prune` | no | also delete what earlier runs sent and the source no longer has |
| `allow_empty` | no | with `prune`, allow an empty source folder although earlier runs sent files |
| `mailbox_me` | – | a channel section's down only: plan only other members' folders at the top ([Channel sections](#channel-sections)) |
| `max_bytes`, `max_files` | – | a channel section's only: the folder limits ([Limits](#limits)) |

- `path` itself is resolved once (a symlink there is followed); nothing below it ever is. A
  symlink below it fails the plan with `unsafe_path`; sockets, devices and FIFOs, and on Windows
  junctions and other name-surrogate reparse points, count as symlinks. There is no option to
  skip them or to exclude paths: no channel section could set one.
- The order is depth-first, names sorted by code point, each folder right before what it holds.
- It walks through directory handles, as the sink does: on POSIX each folder is entered with
  `O_NOFOLLOW` through its parent's fd, on Windows the parents are checked again before each
  listing or open. So a folder swapped for a link during the run is never followed.
- Any error listing a folder or reading metadata fails the plan; only an entry that vanishes
  between listing and stat is left out. It never skips a subtree silently: to `prune`, a skipped
  subtree would look deleted.
- A name that isn't valid UTF-8 fails the plan with `unsafe_path`.
- To send a file it opens it again (POSIX: `O_NOFOLLOW | O_NONBLOCK | O_NOCTTY`, then `fstat`;
  Windows: `lstat` first, then `fstat`); anything but a regular file fails with `vanished`. So a
  symlink swapped in after the plan is never followed, and a FIFO can't hang the run.
- It always skips `.vcharon-stage-*` entries.
- Identity: `{"end", "machine" (remote only), "path", "kind"}`.

State, and what a run sends:

- The state is `{"sent": {"<path>": "d" or [size, mtime, exec]}}`, every path sent so far.
- A run plans only what changed since: a file not in `sent`, or whose size, mtime or `exec`
  differs; a folder not in `sent`. A run with nothing changed moves no bytes.
- The target is trusted between runs: a file changed there isn't noticed until it changes at
  the source, or until a `--full` sync.
- A run that plans anything also plans every path whose name differs from a planned one only by
  case or Unicode normalization, so a macOS or Windows sink sees both spellings and refuses the
  pair with `collision` instead of taking one for the other.
- The new `sent` is the old one, less what the run deleted, plus every path it planned.
- A saved state of any other shape is `state_mismatch`: VCharon never guesses.

`prune`:

- A sent path gone from the source becomes a delete without `tree`. Files others added at the
  target are never deleted.
- A path under a name that `mailbox_me` leaves out at the top is never deleted and drops out of
  `sent`.
- A kind change (folder to file or back) is a delete plus a put; if the target folder still
  holds files VCharon didn't send, the plan fails with `kind_change`.
- An empty source with a non-empty `sent` fails with `empty_source` unless `allow_empty`: an
  unmounted disk looks empty.
- After a commit that failed partway, `state_after` saves the old `sent`, less the deletes done
  and the puts not written, plus the puts written. So `sent` never lists a path the commit
  removed.

### The dir sink

Sink, either end.

| option | default | meaning |
|---|---|---|
| `path` | required | the root |
| `create` | no | create the root (and its parents) if missing; else a missing root is `not_found` |
| `max_deletes` | 500 | refuse a plan whose deletes would remove more files and folders than this (0: no limit) |

- `doctor` runs the root checks on an empty plan, then checks with `os.access` that the root (or
  the folder that would hold it) is writable. On Windows it says `a directory (write access isn't
  checked on Windows)`: `os.access` ignores ACLs there, and a test write would break "only
  reads".

## Applying a plan

### Path rules

`pathrules.py` holds pure functions, so every OS's rules are tested on any OS. The receiver runs
them on every plan before it stages anything, and never trusts the sender's checks.

Every receiver:

- the plan rules of [Plans](#plans); the joined path stays inside the root;
- each path part is at most 255 bytes of UTF-8 on Linux, 255 UTF-16 code units on Windows and
  macOS;
- collisions: all put paths and their parent folders, as one set, stay distinct under the
  receiver's folding (below);
- no existing folder between the root and a path is a symlink or, on Windows, a name-surrogate
  reparse point, even if the plan deletes it (`unsafe_path`; remove it by hand);
- the root may be a symlink, resolved once at the start. On POSIX every symlink on the way must
  be owned by you or root (`unsafe_dir`), else another user could plant one into your files.
  VCharon resolves it itself: `realpath` reads links in user space, where the kernel's
  `protected_symlinks` never sees them. It resolves to what `realpath` gives: a `..` after a
  link goes up from the link's target, as the kernel does, not back to the link's folder;
- a folder the plan needs must not exist as a file (`kind_change`) unless the plan deletes it;
- no existing folder under the root is on another file system: a move there would fail halfway
  through a commit;
- on POSIX, the root and every folder on the way are owned by you and not writable by others
  (`unsafe_dir`), except group write for your private group and a sticky folder such as `/tmp`:
  else another user could swap a folder for a symlink between check and commit. Not in the
  shared root, whose folders are every account's ([The shared root](#the-shared-root));
- the root itself is never deleted or replaced.

Windows receiver, per part: none of `< > : " | ? * \` or control characters; no trailing `.` or
space; not a reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `CONIN$`, `CONOUT$`, `COM1`-`COM9`,
`COM¹`-`COM³`, `LPT1`-`LPT9`, `LPT¹`-`LPT³`, the text before the first `.` with trailing spaces
removed, any case); folding is `casefold()`, slightly stricter than NTFS (refusing is safe,
overwriting isn't). Every absolute path handed to Windows starts with `\\?\` (or `\\?\UNC\`),
so the 260-character limit never applies.

macOS receiver: folding is `NFD(casefold(NFD(s)))`, since APFS ignores case and normalization by
default. Linux receiver: no folding.

### Files already at the target

- A file put overwrites an existing file, which keeps its permission bits; any other non-folder
  (a symlink, a FIFO) is replaced by a rename, so nothing is followed.
- On Windows and macOS a replaced file can keep its old spelling (APFS keeps `README.md` when
  `readme.md` replaces it); the commit renames it to the plan's spelling.
- A put that would turn a folder into a file, or the reverse, is `kind_change` unless the plan
  also deletes that path.
- A sink never deletes anything that isn't a delete entry.

### Staging and commit

- The sink opens the root once, at the check, and keeps the handle for the whole run.
- The check only reads, so a dry run changes nothing. The first stage or the commit creates the
  root if `create` asks, then the stage dir in it: `.vcharon-stage-` plus 16 random hex digits,
  mode 0700, with a `lock` file it holds for the whole run.
- It removes other stage dirs in the root that you own, that are at least 60 s old, and whose
  lock it can take (or that have none): a live run holds its lock, the OS drops a crashed run's.
- `stage(index, reader)` creates `<stage>/<index>` with `O_EXCL` (the umask applies), writes the
  bytes, sets the mtime and bits. Nothing in the root changes yet: a connection dropped
  mid-transfer leaves the root as it was.
- Commit: deletes, deepest first (an already-gone path is fine); folder puts, shallowest first;
  file puts, moved from the stage dir, then the spelling fixed; the stage dir removed.
- On POSIX the commit never follows a path from the root again: it walks one part at a time with
  `O_NOFOLLOW | O_DIRECTORY` through the parent's fd and uses the `dir_fd` forms of rename,
  unlink, rmdir and mkdir. A folder swapped for a symlink after the check makes that step fail.
- On Windows, which has no `dir_fd`, every parent is checked for reparse points right before each
  step: it narrows the race without closing it. A read-only file is made writable before it is
  replaced or deleted. A sharing violation and access denied are both retried 3 times, 0.2 s
  apart, then are `in_use` and `permission`: virus scanners briefly hold new files, and a file
  another program has open without delete sharing (as Python opens files) is expected to give
  access denied when it is replaced (a folder held open was seen to give it).
- A failed commit stops, removes the stage dir and reports what it did; nothing is undone.
  Running again is safe: puts overwrite, deleting a missing path does nothing, and the state never
  records more than is at the target.
- No fsync for target files: a re-run repairs a power cut. State files are fsynced.

### What is copied

File bytes, unchanged (no line-ending or encoding conversion), and the mtime. A new file gets
`0o666` less the umask, plus execute bits where it has read bits when `exec` is true. A replaced
file keeps its bits, except that `exec` sets or clears its execute bits. New folders get `0o777`
less the umask. In the shared root every new folder gets `0o777` and every file `0o666` or more
(a replaced one too), whatever the umask ([The shared root](#the-shared-root)). Not copied:
owner, ACLs, extended attributes, resource forks, folder mtimes.

### Full syncs

`vcharon sync C --full` compares both ends by content:

- The source plans every file and folder, each file put with its `sha256`.
- The sink's check hashes each target file a hashed put would replace, if it is a regular file
  of the same size with one link, the plan deletes neither it nor a folder above it, and (POSIX)
  it is yours or (Windows) it isn't read-only. Matching files go in `have`: their bytes aren't
  sent.
- The commit gives each `have` file the plan's mtime and execute bits, after checking it is still
  the file it hashed (device, inode, size, mtime, one link), else `vanished`.
- So a member whose copy differs from the server's gets back in step moving only the files that
  differ. It reads every file on both ends.
- `OK <n> written` (and join's `synced:` counts) include the `have` files, which the commit
  touches: only the `put` line's `, <n> already there` tells them from files sent.

## State and locks

### Where files live

| | Windows | macOS | Linux |
|---|---|---|---|
| config | `%APPDATA%\vcharon\vcharon.ini` | `~/.config/vcharon/vcharon.ini` | `$XDG_CONFIG_HOME/vcharon/vcharon.ini` |
| channel sections | `channels.d\` next to the config | the same | the same |
| state, locks, records, `client-id` | `%LOCALAPPDATA%\vcharon\state\` | `~/Library/Application Support/vcharon/state/` | `$XDG_STATE_HOME/vcharon/state/` |
| logs | `%LOCALAPPDATA%\vcharon\logs\` | `~/Library/Logs/vcharon/` | `$XDG_STATE_HOME/vcharon/logs/` |
| local trees (`joined`) | `%LOCALAPPDATA%\vcharon\joined\` | `~/.local/state/vcharon/joined/` | `~/.local/state/vcharon/joined/` |
| channel root | `~/.local/state/vcharon/channels/` (under `%USERPROFILE%`) | `~/.local/state/vcharon/channels/` | `~/.local/state/vcharon/channels/` |
| last-watched stamps (`seen`) | beside the channel root | the same | the same |
| a server's copies of the helper's bundle (`cache`) | beside the channel root | the same | the same |
| the shared root (`--shared`), with `seen` and `lead` beside it | – | – | `/var/tmp/vcharon/channels/` |

Defaults: `$XDG_CONFIG_HOME` is `~/.config`, `$XDG_STATE_HOME` `~/.local/state`.

- `VCHARON_HOME=<dir>` puts config, state, logs and `joined` under one folder (tests, the
  release smoke test). The channel root doesn't follow it: the server never sees a client's
  setting, and a machine's local and remote members must share one root.
  `VCHARON_CHANNELS_ROOT` moves the root, for tests only, and `VCHARON_SHARED_ROOT` the shared
  root's folder (`/var/tmp/vcharon`), for tests only: the suite's sandbox points it into its
  temp folder.
- `VCHARON_HOME` must be absolute after `~` is expanded, else every command that reads it
  fails with `config`: a relative folder would follow the current directory, putting records
  in whatever checkout a command ran from, and a channel section's `mailbox.local` is refused
  unless absolute.
- `joined` is set per OS, not taken from the state dir: macOS's state dir has a space, and the
  tree's path is written into a section.
- The channel root is the same text on every OS (`~/.local/state/vcharon/channels`): one text
  for a local member's root and for what a remote member's section names at the server.
- Never `$TMPDIR` on macOS: different launchers can see different ones.
- `cache/` holds `helper-<sha256>` files, the bundles connections sent ([Start-up](#start-up)).
  Anything in it can be deleted at any time: the next connection sends the bundle again.

### State file

`<state>/<job>.json`: `{"schema": 1, "job", "fingerprint", "identity", "sink", "source",
"saved"}`.

- `fingerprint`: the sha256 of the job's `[ssh, from, to, from.path, to.path, me]` as the
  section gives them. Compared before ssh starts.
- `identity`: the source's; compared right after the plan. `sink`: its end, the server's machine
  id for a remote sink, and the resolved root; compared right after the check.
- Any mismatch refuses the job with `state_mismatch` (exit 3) and the `fix:` `vcharon sync C
  --reset up` (or `down`), then `vcharon sync C --full`. So an alias pointed at another server,
  or a moved root, can't make `prune` delete files on the wrong machine.
- A state file that can't be read, or of any other shape, is `state_mismatch` too.
- Written after a good run and through `state_after` after a commit that failed partway: a temp
  file, fsync, `os.replace`. A dry run never writes state.
- On Windows that replace is retried after a sharing violation or access denied, 3 times 0.2 s
  apart, as the commit's are ([Staging and commit](#staging-and-commit)); so are the watcher's
  snapshot, `vcharon.ini`'s and a join record's: a program with the old file open is expected
  to give access denied.

### Lock

- `<state>/<job>.lock` is held for the whole run, from before the state is read:
  `fcntl.flock(LOCK_EX | LOCK_NB)` on macOS and Linux, `msvcrt.locking` on Windows. Held:
  `busy`, exit 2. The OS drops it when the process dies, so a crash never leaves it stuck.
- `vcharon sync C --reset` takes the same lock.
- The same lock kind guards the post lock, the watcher's snapshot, and stage dirs.
- The watcher's snapshot lock names its holder: whoever takes it (a watcher; `create`, `join`,
  `leave` and `close`) writes `<pid>\n` into the file from byte 1 on (`msvcrt` locks byte 0,
  which no other process can read on Windows while it is held), plus `<ns>\n`, its
  `/proc/self/ns/pid` text, on Linux outside the initial PID namespace; it cuts the file to its
  first byte before it lets go, and writes nothing but that cut when its namespace link can't be
  read. A refused watcher, `leave` or `close` ends its line with ` (PID <n>)` only when the file
  holds exactly one of these forms and, on Linux, the reader's own namespace is the same (a bare
  `<pid>\n` counting as the initial one's). Why: the user (or agent) can find the process that
  holds the lock, while a PID from another namespace (a Codex sandbox, a container) names
  another process. The PID can be stale (a holder killed before it lets go, a number reused), so
  the guide says to check it is a `vcharon watch` before stopping it.

## Config

Read with `configparser`:

- Interpolation off and no inline comments: a value is everything after `=`, so `ssh_path =
  C:\Tools #2\OpenSSH\ssh.exe` keeps its `#2`. A line starting with `#` or `;` is a comment.
- A `[DEFAULT]` section is refused (configparser would copy its keys into every section), and so
  is a value that continues on an indented line.
- UTF-8, with an optional BOM (Windows Notepad adds one).

`vcharon.ini` holds one section, `[vcharon]`. All its keys are optional:

| key | default | meaning |
|---|---|---|
| `box` | the OS: `mac`, `win`, `linux` | this machine's part of every member name; a writer's name of at most 10 characters ([Member names](#member-names)) |
| `ssh_path` | per OS ([The ssh command](#the-ssh-command)) | the ssh client, an absolute path (`~` allowed) |
| `remote_python` | `python3` | the server's Python command; no quotes, backslashes or leading `-` |
| `connect_timeout` | 10 | seconds, 1 to 86400 |
| `handshake_timeout` | 30 | seconds from starting ssh to the ready marker, 1 to 86400 |
| `idle_timeout` | 300 | seconds with nothing moving while the controller waits; 30 (three times the helper's tick) to 86400 |
| `run_timeout` | 0 | one sync (one round with `--repeat`), in seconds, 0 to 86400; 0 is no limit |
| `compress` | no | pass `-C` to ssh |

- Every timeout is at most 86400 s (a day): past 2147483 s a wait's milliseconds overflow, and
  ssh refuses a `ConnectTimeout` of 2^31 s or more with an error that doesn't name the key.
- Unknown keys are errors, so a typo fails loudly. Any other section in `vcharon.ini` is refused:
  channel sections live in `channels.d/`.
- `vcharon setup` writes the file when it's missing, with a commented `# box = <os>` line;
  `setup --box NAME` edits `[vcharon]`'s box line in place, line by line, never through
  configparser, which would drop the comments. Every other line, the BOM, CRLF line ends, the
  file's mode and a symlinked file (the link stays, its target is written) are kept.

### Channel sections

A remote member's channel section lives in `channels.d/` next to the config file, one file per
section, `<C>.<name>.ini`, written by `join` and `create`:

```ini
[myapp.mac-web]
ssh               = devbox
mailbox.me        = mac-web
mailbox.leader    = linux-api
mailbox.local     = ~/.local/state/vcharon/joined/myapp.mac-web
mailbox.remote    = ~/.local/state/vcharon/channels/myapp
mailbox.max_mb    = 50
mailbox.max_files = 1000
```

| key | meaning |
|---|---|
| `ssh` | the server (required) |
| `mailbox.me` | this member's name (required) |
| `mailbox.leader` | the channel's leader (required) |
| `mailbox.local` | the local tree (required), absolute or `~` |
| `mailbox.remote` | the channel's folder on the server (required); never the server's home or `/`, since down prunes the whole tree |
| `mailbox.max_mb`, `mailbox.max_files` | the channel's folder limits, from the join record ([Limits](#limits)); default 50 and 1000 |
| `mailbox.kind` | `lobby` for a lobby member's section, written by `join`; absent for a work channel. Its down job gets `max_deletes = 0` ([The lobby](#the-lobby)) |
| `idle_timeout`, `run_timeout`, `compress`, `remote_python` | override `[vcharon]`'s |

Why one file each: agents joining at once never edit one shared file.

- `channels.d/` is read after `vcharon.ini`: each file whose name ends in exactly `.ini`
  (compared case-sensitively, from `os.listdir`; a glob ignores case on Windows) and doesn't
  start with `.`, in name order. Each holds exactly one section, named as the file.
- A file that is broken, or whose name clashes (case ignored) with an earlier one, is
  **skipped**: a command that needs it fails with its error; `doctor` lists it; every other
  command logs it and goes on. Never on stderr: the watcher takes a sync's first stderr line as
  the round's error. One agent's broken file can't stop the user's other channels.
- Two sections whose `mailbox.local` is one folder are both skipped: each would pull the other's
  own folder over its own.
- A section `[S]` becomes two jobs:

  | | `S.up` | `S.down` |
  |---|---|---|
  | from | `local:path`, `path = <local>/<me>`, `prune`, `allow_empty`, the limits | `remote:path`, `path = <remote>`, `mailbox_me = <me>`, `prune`, `allow_empty`, the limits |
  | to | `remote:dir`, `path = <remote>/<me>`, `create = no` | `local:dir`, `path = <local>`, `create = yes` |

- Up never creates: the claim made `<remote>/<me>`. With `create`, a member's up would make a
  closed channel again as soon as it had something to send. So once the channel is closed, up
  and down fail with `not_found`, and their `fix:` line is the channel-gone fix: `the channel is
  closed, or your folder in it is gone: …`, asking the user first in a work channel with no
  `CLOSED` (the folder may have been removed by hand or as idle), then `vcharon leave C …`;
  never "create it".
- Down plans, at the top of the tree, only folders whose name is a valid member name other than
  `<me>`; everything else there (`<me>/` in any case, a file, a symlink, `Mac/`, `con/`) is left
  out (dropped from `sent`, never deleted) and named in down's log. Valid names are
  lowercase ASCII, so no two planned top-level names fold together on any client. Below the top,
  a case twin in one member's folder still refuses macOS and Windows clients with `collision`:
  dropping one of the two would decide for the writer which file a client gets.
- Before either job, the controller creates `<local>` and `<local>/<me>` while up has sent
  nothing. Once up has sent files, a missing own folder, or one without `MEMBER.md`, stops both
  jobs with `not_found` and the rejoin as the fix: made again empty, up's `prune` would empty
  the server's copy.
- A channel job's `fix:` for a name its source can't send or list says what a member can do
  (`remove or rename it in your own folder (<me>/)` for up; `the writer of each folder named
  above removes or renames it; your up still runs` for down), in place of the general hints,
  which speak to the owner of the whole source.
- The fingerprint includes `mailbox.me`: down's paths don't hold the member's name, so without
  it a renamed member would keep a state that isn't its own.

## Channels

### Layout

```
server or this machine: ~/.local/state/vcharon/channels/      the channel root
  myapp/                                     a channel
    linux-api/                               the leader's folder
      CHANNEL.md  MEMBER.md  STEPS.md  RESULTS.md
    mac-web/                                 a member's folder
      MEMBER.md  RESULTS.md  mac-web-1.patch
  lobby/                                     the root's lobby
    linux-api/                               its founder's folder
      CHANNEL.md  MEMBER.md  chat-2026-10-07.md
server or this machine, beside the root: ~/.local/state/vcharon/
  seen/myapp/mac-web                         a member's last-watched stamp
  lead/myapp/2/mac-web                       the term 2 marker of a takeover, named after its taker
a Linux server or machine, the shared root (--shared): /var/tmp/vcharon/
  channels/  seen/  lead/                    the same, for every account
a remote member's machine:
  <joined>/myapp.mac-web/                    its local tree: mac-web/, linux-api/, …
  <config dir>/channels.d/myapp.mac-web.ini  its channel section
  <state>/channels/myapp.mac-web.json        its record (a local member has a record too)
```

- A channel's name follows the member-name rule but is at most 24 characters, so
  `<channel>.<name>` fits a job name with `.down` after it.
- One local tree per (channel, member), never shared: two members on one machine in one channel
  would each pull the other's folder over its own.
- `joined/` isn't the channel root, so a machine that is also a server keeps the two apart.

### Trust and access

- Anyone who can log in to the server as the channel root's user can read and write every
  channel on it; in the shared root, any account on the server ([The shared
  root](#the-shared-root)). VCharon adds no login or encryption of its own; it relies on ssh.
- A member's folder is written only by that member: on a remote member's machine the other
  folders are copies, and a copy is never sent back.
- Entries are input, not orders: `join` and `create` print one line pointing at `vcharon guide
  rules`, which says an entry never overrides the user. Readers enforce what they can: `@all`
  from anyone but the leader is ignored (in a work channel; [The lobby](#the-lobby) has no
  leader), and an ID in the wrong folder is flagged. Who leads is read from the charters'
  terms in the members' folders ([Leader handoff](#leader-handoff)): a member that writes a
  `CHANNEL.md` with a high `term:` into its own folder by hand is the leader for every reader.
  The race marker at the server stops two honest takers, not a forged charter.
- **What deletes others' folders is the user's.** `close lobby --yes` and `list --remove-idle
  --yes` delete other members' folders; the guide's rules tell agents never to run them on
  their own, only on their user's word. The code can't tell an agent from its user, so the
  guard is `--yes` and the guide, with no refusal by environment. Nothing about a removal is
  posted into a channel: the members see their folder, or the lobby, gone (`EXIT closed`).

### The shared root

For people on separate accounts of one Linux server. The default is unchanged: the root in the
login user's home. Trust stays whoever can log in to the server.

- **One fixed folder**, `/var/tmp/vcharon/`, holding `channels/` (the shared root), `seen/` and
  `lead/`, as `~/.local/state/vcharon/` holds them for the home's root. Not configurable: every
  member, of every account, must reach the same folder. `/var/tmp` because every Linux server
  has it, writable by every account, and keeps it across reboots (FHS). The helper's `cache/`
  stays in each account's home: a copy of the bundle is only the sending account's to trust.
- **Chosen by `--shared`** on `create` and `join` (`--server` or `--local`). The join record
  keeps it (`"shared": true`, [Join records](#join-records)), so every later command and a
  rejoin find it there, with no flag. A remote member's section names the shared channel
  folder in `mailbox.remote`; over ssh each `channel.*` call gets `"shared": true`, never a
  path ([Calls](#calls)). A join or create of a name whose record is of the other root is
  refused: a record, a section and a local tree are one per (C, name), so a membership never
  moves between the roots.
- **Modes**: everything vcharon makes there gets mode 0777 (folders) or 0666 (files; a file a
  sync replaces gets 0666 added to the bits it keeps), whatever the umask, set right after the
  create through an fd opened without following a link, so a folder swapped for a link never
  passes 0777 on. Why: a close or `list --remove-idle` by any member deletes what other
  accounts made, and every member's down reads every file. No sticky bit: with it only an
  entry's owner could remove it. The umask itself is never changed: the same process writes
  records and logs, which stay private. A folder or file a member's agent makes by hand keeps
  the umask's modes; its owner fixes it.
- **No owner rule there**: the receiver's rule that every folder on the way be yours and not
  writable by others ([Path rules](#path-rules)) can't hold where every folder is 0777 and
  another account's. Its other rules stay (no link followed, no other file system).
- **Found folders are checked, never repaired**: the first `create --shared` (or a lobby's first
  join) makes `/var/tmp/vcharon/` and `channels/`; `seen/` and `lead/` come at their first use.
  Each `channel.*` call on the shared root and each watcher's stamp there first checks the
  shared folder and each of its three folders that exists: a real folder (not a link) of mode
  exactly 0777, reached with no link above it, else `unsafe_dir`, with a fix that asks the
  user to have its owner fix or remove it. Why never repaired: a mode found there is another
  account's choice, and only its owner can change it. Why no link above: handles open at the
  real path, which the shared-root test of a path wouldn't recognize, so new files would keep
  the umask's modes. A first use racing another account's can be refused once; a watcher's
  stamp is then skipped.
- **Linux only**: `--shared` is refused on macOS and Windows (`config`, exit 3), with `--local`
  too. With `--server`, the helper decides.
- **`list`** shows the shared root's channels with no flag, as a group of its own after the home
  root's, only when it holds any; `--json`'s `shared` holds them (null on a server that isn't
  Linux, by the hello's `shared_root`: [hello](#hello)). A shared folder that fails the checks
  doesn't stop `list`: the home root is listed, then a `note:` with the error and its fix, and
  `--json`'s `shared` is `{"error": …}`. Why: a user who never uses `--shared` must still
  list, and the folder is another account's to fix. Only that check's `unsafe_dir` is a note;
  every command that uses the shared root still refuses. `whoami` says when a membership is in
  the shared root.
- **`doctor`** has a `shared` row for this machine on Linux and for each server whose hello says
  `shared_root`. It reads through `list`'s listing, so it never makes or chmods anything: ok
  with a count, ok `not made yet`, or a `warn` with the check's error. A warn, not a FAIL: only
  a user of `--shared` needs it, and the folder is another account's to fix.
- **One name from two accounts**: the claim's checks refuse it as on any root ([The
  claimer](#the-claimer)).
- **Aging**: a system whose tmpfiles config ages `/var/tmp` may delete a long-running channel's
  old files; vcharon doesn't guard against it, and the README warns.

### Member names

A name is `<box>-<project>[-<role>]`, for example `mac-myapp` or `linux-api-b`. It is also the
member's folder name.

- `<box>`: `[vcharon] box`, else the OS's word (`mac`, `win`, `linux`), so a first run needs no
  setup. Never a host name, not as a default nor as a fallback: a host name can name an
  employer or a network, and a silent fallback would give one machine a second name.
  `whoami`, `doctor` and `setup` print where it came from.
- `<project>`: the name of the nearest folder, walking up from the current one, that holds
  `.git` (a folder, or a worktree's `.git` file), a `.svn` folder or a `.hg` folder, found in
  Python, not by running `git`, `svn` or `hg`; else the current folder's own name. Lowercased,
  each run of characters other than `a-z0-9_` made one `-`, `-` stripped at both ends, cut to
  14, `-` stripped again. `--project P` overrides it. Why `.svn` and `.hg` too: a member must
  keep its name in any folder of its checkout, whatever the version control (an SVN working
  copy older than 1.7, with a `.svn` in every folder, needs `--project`).
  - Without `--project`, `join` and `create` refuse a new name whose project folder is the home
    folder (exit 3, `give --project`): its name is the OS user name, which would land in the
    member's name and every ID. A membership with a record here keeps its name.
  - Without `--project`, `join` and `create` print a `note:` saying where the project came from
    (the checkout's path and mark, or "this folder's name"). For a new name the note also gives
    the undo, a `leave` (for `create`, a `close`) and then the command again with `--project P`:
    by then the name is taken, so a second join with `--project` alone would make a second
    membership. A join that finds its record or takes back its folder gets no undo, and a join
    refused before its claim prints no note. Why: in a folder of checkouts the name changes
    with the folder the agent starts in, silently. Only on stdout: no channel file holds the
    path.
- `-<role>`: only with `--role R`, 1 to 6 of `a-z0-9`.
- The whole name: lowercase `a-z0-9-_`, starting with a letter or digit, at most 32 (10 + 1 +
  14 + 1 + 6), not a name Windows reserves: a Windows client must be able to hold the folder,
  and lowercase means no two names differ by case on a macOS or Windows client.

Why this scheme, not a name the agent picks:

1. No memory needed: a new session in the same checkout builds the same name and finds its
   folder again.
2. Unique across machines: the same repo on two machines gives two names (with two boxes).
3. It says where the agent runs, so a leader can write "@win-myapp: run the Windows test".
4. It is tied to the place, not the tool: the same checkout can run another agent tomorrow and
   keep its folder. What kind of agent it is goes in `MEMBER.md`'s `agent:` field.

Using the current folder breaks "never rely on the current directory" ([Launch
rules](#launch-rules)) on purpose: the name says where the agent works.

`MEMBER.md`'s entry #1 holds `channel:`, `name:`, `leader:`, then `box:`, `os:` (`mac`, `win`,
`linux`), `agent:` (`claude`, `codex`, `opencode`, `other`), `project:`, `claimer:` and
`vcharon:` (the version the member last ran). No host name, path, user name or raw machine id.
`vcharon list` shows box, os, agent and project; `vcharon read` notes differing versions
([Mixed versions](#mixed-versions)).

- `agent:` comes from `--agent`, else from the environment the agent's CLI sets
  (`CLAUDECODE`, `CODEX_THREAD_ID`, `OPENCODE`), else `other`; more than one found gives
  `other`. A rejoin updates it only when one was found or given: a rejoin from a plain terminal
  leaves it.
- `vcharon:` is written at `join` and `create`, and set in place to the running version (added
  at the header's end when missing) by every rejoin, which prints a line saying it updated the
  file and that nothing is left to do, and by every start of the member's watcher, silently.
  Why the watcher: an update needs no rejoin (a leader never rejoins), but every agent restarts
  its watcher. The watcher does it before its first round, trying the post lock once (held, or
  any failure: the line stays and the watch starts). No other watcher wakes: they leave `.md`
  files out of their file lines and hash only an entry's heading. So the line can lag until the
  member's next watcher start that gets the lock.
- A reader takes the fields it knows and leaves every other header line alone, so a newer
  vcharon may add one. A `MEMBER.md` over 64 KiB isn't read at all and counts as missing for
  the claimer (so `--takeover` too), the fields `list`, `read` and `whoami` show, and `lead`:
  vcharon's is a few hundred bytes, so a bigger one isn't its, and the read stays bounded. A
  rejoin's and a watcher start's `agent:`/`vcharon:` update, and the watcher start's `leader:`
  refresh, read it whole.

### The claimer

A member folder that already exists may be this machine's own (a rejoin) or another machine's
that happens to build the same name (two laptops both left at box `mac`). The claimer tells them
apart without putting a machine id in a shared file.

- **Client id.** `<state>/client-id`, made once: the first 32 hex digits of
  `sha256("vcharon:" + machine id)`, or random on a Linux machine with no machine id (some
  containers). After that the file alone counts, so a machine id that turns up later, or a read
  that fails once, changes nothing. On macOS (`ioreg`'s `IOPlatformUUID`, hashed) and Windows
  (the registry's `MachineGuid`, hashed) a failed id read is refused, never made random: a random
  id made then would be kept for good. `doctor` says which source it uses.
- **Claimer.** `sha256("<C>:" + client id)`, first 16 hex digits, written as `claimer:` in
  `MEMBER.md`'s entry #1. Hashed with the channel's name, so no raw id reaches a shared file and
  one machine looks different in different channels. Not a separate marker file at the server:
  the own folder is pushed with `prune`, which would delete it.
- **Read** by `channel.claim`, which returns the existing folder's `claimer:` (or none).
- **At `join`, when the folder exists**: a different claimer is refused, `another machine holds
  <name> in C`, with the fix `vcharon setup --box NAME` on this machine; `--rejoin` alone doesn't
  pass it. A same claimer is treated like none: a record here, or `--rejoin` on the user's word,
  is still needed, since cloned VMs, containers and two OS users on one machine share a machine
  id. No claimer (a remote member whose first push failed has no `MEMBER.md` at the server yet):
  the record, or `--rejoin`.
- **`--takeover`** (with `--rejoin`) is the only way past a different claimer: for a machine whose
  state was wiped or whose id changed, on the user's word. It rewrites entry #1's `claimer:` in
  place after the rejoin's pull, under the folder's post lock, and the sync sends it.
- At `create` the folder can't exist: `channel.claim` with `create` refuses an existing channel.

### Join records

`<state>/channels/<C>.<name>.json`, one per membership on this machine, in a folder of its own
so a record can't share a name with a job's state file:

`{"version": 1, "channel", "name", "leader", "ssh" (null for a local member), "remote",
"machine", "project", "role", "format", "limits"}`

- `remote`: the section's `mailbox.remote` text (remote member), or the channel folder's
  absolute path (local member). `machine`: the server's (or this machine's) id from the claim.
- `project` and `role` rebuild the name for `fix:` lines.
- `format` and `limits` are the channel's, stored at `join`/`create` ([Formats](#formats)).
- `"shared": true` only for a membership in the server's shared root ([The shared
  root](#the-shared-root)); a record without it is in the root in the home.
- Written through a temp file and `os.replace`; a write the file system refuses is its error
  (`permission`, `no_space`, …) with its fix line, as any other write's. A record that can't be
  read is an error, never a guess: VCharon never guesses which server a membership is on.
- `leave` and `close` take the server from the record, never from flags.

### Which membership

`post`, `read`, `watch`, `sync`, `leave`, `close` and `whoami C` find the member from the channel
name and this machine's records. The lookup key is **channel + project + role**, not the full
name, so a `setup --box` after a join doesn't orphan the record: the record keeps the name it
joined with.

- project: `--project`, else the current folder's (as for names);
- role: `--role`; none given means the record with no role.

In order:

1. A record for (C, project, role) exists: use its name.
2. No `--role`, no role-less record, but records with roles exist for (C, project): refused,
   `you are in C from <P> only with a role`, with a `fix:` naming each `--role`.
3. Nothing: refused, `you aren't in C as --project P (no join record on this box)`, with a
   `fix:` that names the flags of the memberships this box has, or `vcharon join C …`.

Two records for one (C, project, role) are refused: ask the user.

`join` and `create` run step 1 only, before they build a new name: a `join C` after a `setup
--box` rejoins under the name it had, not a second membership under the new box. Steps 2 and 3
would block a legitimate role-less join, so instead, at every join, `join` and `create` print a
`note:` naming each other membership of C this project holds on this machine, with its flags,
and saying both stand. It never calls the other one the reader's: two agents can work in one
folder (a leader and a member), and the other membership may be the other agent's. `whoami C`
lists the same, as `also` lines.

**Another agent's folder.** A join that takes back a folder with no `--rejoin` (step 1's record,
or a lobby's folder after a `leave`) whose `MEMBER.md` said another `agent:` than this run's
prints a `note:`: if that membership is another agent's, join as a member of your own with
`--role R`. None when the two agree, with `--rejoin` (the user's word), or for a `MEMBER.md`
with no `agent:`. The note changes nothing: the rejoin updates `agent:` as [Member
names](#member-names) says, so the first agent's next session gets the note too. Why: a second
agent started in another's folder with no `--role` builds the same name and takes the first
one's membership with no word, and the agent is the only sign vcharon has; it can't be sure, so
it is a note. The fix has no `leave`: that would end the first agent's membership.

### Create, join, leave, close

- **`create C`**: one `channel.claim` with `create`: `mkdir <root>/C` (fails if it exists, so of
  two creators one wins), then `mkdir <root>/C/<name>`; if that fails, the empty channel folder
  is removed. A join record of the name already here for this server while the channel isn't
  there is left from a channel that ended without a `leave`: refused before the claim, fix
  `vcharon leave C`. Then the record, `MEMBER.md` and `CHANNEL.md`; a remote member also gets
  its local tree and section, then a `--full` sync. A failure before the sync undoes what it
  wrote and releases the claim. Right after the claim it takes this machine's watcher lock for
  the name, as `join` does, and holds it to the end. Then (after the sync, even a failed one,
  but not one stopped by Ctrl-C) it saves the leader's watcher snapshot with nothing of any
  member's folder marked seen. Why: every member folder is newer than the claim, so the
  leader's first watcher start must print every member's `JOIN`.
- **`join C`**, in this order:
  1. `channel.list`: the channel exists with a leader by the charters' terms ([Leader
     handoff](#leader-handoff); none, or two at the top term, refuses), and its format and
     limits pass; checked before the claim, so a refused join leaves nothing behind.
  2. This machine's watcher lock for the name, taken without waiting. Held means a live session
     already is that member: refused, `a live session holds <name> in C`. With a record of the
     name here, the line says whose membership it is (the leader's or a member's, with its
     flags), and the fix names the cases without choosing (your own earlier watcher: keep it or
     stop it with `watch --stop`; another agent's: join with `--role R`; unsure: ask your user);
     ` (PID <n>)` ends it when the holder wrote one ([Lock](#lock)). Why: an agent in the
     leader's folder builds the leader's name, so the holder may not be the reader's. Taken, the
     lock is held until the join returns; a join that fails with no record left deletes the lock
     file it made.
     A record of this name for another server is refused, and so is one whose folder isn't in
     the listed channel, or whose leader holds no `CHANNEL.md` there: it is of an earlier channel
     of the name, fix `vcharon leave C`. Why: reused, the new channel would get the old
     `MEMBER.md`'s `leader:` and old entries, and the full down would delete this machine's copy
     of the old channel.
  3. `channel.claim`, one `mkdir`; the claim's own reading of format and limits must equal the
     listing's.
  4. An existing folder: the claimer rules ([The claimer](#the-claimer)); with no record and no
     `--rejoin`, `the name <name> is taken in C`.
  5. The record, then `MEMBER.md` (a new member only), a remote member's section. A failure
     before the record releases a new claim.
  6. A remote member's rejoin first pulls its own folder from the server when this machine
     lost it (below).
  7. A `JOIN` (or `REJOIN`) entry to the leader in `RESULTS.md` (a lobby's differ: [The
     lobby](#the-lobby)), then the sync, which sends it with `MEMBER.md`: the leader sees the
     `JOIN` when the folder appears.
  8. Unless the sync was stopped by Ctrl-C, the watcher's snapshot, under the lock the join
     holds. A first join, or a rejoin with no usable snapshot, saves the one a watcher's start
     with none would save (one constructor builds both, so they agree: a difference isn't
     caught when it loads); a first join, and a `--rejoin` with no record here, replace one
     left there, which can only be of an earlier membership. A rejoin with a usable snapshot
     keeps it, so its watcher still prints what came while none ran. Why: a start with no
     snapshot is a baseline, so an entry posted between the join and the watcher's first start
     would never be printed. A save that fails doesn't fail the join: a `note:` says so and
     points at `read --to-me`.
  9. The entries already addressed to the member or to all are printed, escaped as `read`
     escapes them: with a snapshot just saved, only those it takes as seen, so each is printed
     once; with a kept snapshot, all of them. A wrong-folder ID is a `WARN` line, as the watcher
     prints it; an entry with no ID is left out; of an ID in two files, the first in path order.
- **The next step.** A successful `join` or `create` prints, before its `OK` line (which stays
  last), a `next:` line with the watcher command to run (`watch C --until-change` with the
  record's flags, spelled as this machine runs vcharon, so it works from any folder) and the
  guide topic to read (`member`, or `lobby`); `create` adds a template of the plan's `post`
  (a placeholder title that refuses to parse, since a plan posted as printed would reach
  everyone). Notes after it, each for one case:
  - a first `join` of a work channel: if the user only asked to join, ask them whether to work
    on the leader's steps (a bare "join" is no task);
  - a first `join` or `create` whose agent isn't `claude`: try the watcher once with
    `--max-minutes 1` to see how it ends (only a run shows whether the agent's tool lets it end
    on its own). A note, not part of the watcher line, which would then keep every watcher at
    one minute;
  - any `join` or `create` by Codex: while idle, don't end the turn, poll the watcher again
    (Codex sees a background command's exit only while its turn runs).

  Every guide topic a line on the normal path names is one read before joining
  (`tests/test_guide.py` checks each string in the code). Why: an agent that skips the guide
  still sees what to run next. A join whose sync failed prints none of these: its sync is the
  next step.
- **The stale-skill note.** Just before the `next:` line, `join` and `create` print a `note:`
  when a skill copy that `vcharon skill install` wrote (its marker line) holds another text
  than this version's SKILL.md, with the `skill install` command for those agents. The check is
  doctor's `skill` row. A copy without the marker is the user's own; a check that fails gives no
  note and never changes the exit code. Why: an agent reads the skill before the guide, and only
  `skill install` (or a later `--update`) rewrites the copies.
- **The rejoin's pull** is decided from up's saved state, never from how the folder looks (a
  stray `.DS_Store` would pass for a tree): it pulls when up has no usable state, has sent
  nothing, or has sent `MEMBER.md` and `MEMBER.md` is missing here. It pulls into a temp folder
  next to the local tree, then moves in only the files this machine lacks; every part of each
  path is `lstat`'ed first and a symlink refused. Why: down never plans the own folder, and an
  up from a lost tree would delete the server's files and restart numbering at #1. A file
  deleted on purpose (with `MEMBER.md` still here) is not pulled back.
- **`leave C`**:
  - Refused while this machine's watcher lock or a sync lock of the membership is held; the
    watcher lock is taken, not just checked, and held to the end.
  - It checks the server's machine id against the record, then lists. A gone channel, or a
    listed one without the member's folder or with another leader than the record's, skips to
    the removal with a `note`: a `LEAVE` would reach no one. Only then is a work channel's
    leader by the charters' terms refused (`you lead C: hand the lead over, or close it`; a
    former leader leaves as a member, and a lobby's founder as any member). The record's leader
    lists before the lock checks, so it isn't told to stop the watcher of a channel it still
    leads; any other member's leave takes the locks first.
  - A remote member whose own folder here lacks `MEMBER.md` is refused, `not_found`, with the
    rejoin as its fix (a sync would send the emptied folder over the server's copy); a local
    member's gets a note, and no `LEAVE`.
  - Else it posts `LEAVE` to the listed leader (in a lobby, to itself: [The lobby](#the-lobby),
    "Silent entries"), once, not again after a failed try, and syncs; a failed sync stops before
    removing anything.
  - Then it removes everything of the membership on this machine: the local tree (only when it
    is exactly the computed `joined/<C>.<name>`, with the no-link walk), the jobs' state, the
    kept ages and version note, logs, locks not held, the watcher's snapshot and a stale stop
    request, the post lock, the record, the section file, and last the watcher's lock file, so
    a watcher started then finds no membership. One `removed <path>` line each.
  - The member's folder in the channel stays: it is its history, and its name stays taken (a
    lobby gives it back to this machine: [The lobby](#the-lobby)).
  - When the channel or the member's folder was gone, it ends with a note that nothing of that
    membership is left here, naming other memberships of C that stay; `close` prints the same.
    Why: an agent told nothing would ask its user what to delete. Nothing is removed on its own
    when a watcher sees `EXIT closed`: that also means a folder removed as idle or by hand, and
    after a close this machine's copy is the last of the channel's text.
- **`close C`**: the leader only, the one the listing names by the charters' terms. It checks
  the machine id and lists first, so a member or a former leader is refused before any lock
  check. Then the same lock checks as `leave`, then `channel.remove`, which refuses unless the
  caller holds the top term's charter and every top-level entry is a folder with a valid member
  name, and takes the next term's marker first, as a takeover does ([Leader
  handoff](#leader-handoff)), so a close and a takeover exclude each other; the marker is
  dropped again if the rename fails. It renames
  `<root>/C` to `.vcharon-closed-<C>-<stamp>` in one step, so every member's next sync sees the
  channel gone at once (a channel name can't start with `.`), then deletes it with the no-link
  walk. A failed delete leaves the renamed folder, which `list` notes. On Windows a rename
  refused because something holds a file or folder inside the channel fails cleanly, with a
  `fix:` saying so. Then its own files as `leave` removes them. When the leader posted no
  `CLOSED` to `@all`, a note after the close says the members weren't told and points at the
  guide's end: a note, not a refusal, since by then the close is done.
- **Why the lock is held, not checked once**: a watcher started between the check and the
  command's own sync ran alongside it, skipping rounds as busy, and once made the join's own
  sync fail `busy`. Held, a watcher started meanwhile exits 12 at once. The
  refusal of `leave` and `close` names the watcher first, since a running watcher is what an
  agent forgets to stop, with ` (PID <n>)` when known, its fix `watch C --stop` and then the
  command again.

### Removing idle members

`vcharon list --remove-idle (--server ALIAS | --local) [C …] [--days N] [--yes] [--home |
--shared] [--json]` is the user's way to remove the folders of members that don't come back:
`leave` keeps a member's folder (its history), and so does a session that ends without one. It
acts on no membership, as `list` does.

- **Idle**: the server's time now less the newest of the member's last-watched stamp ([The
  watcher in a channel](#the-watcher-in-a-channel)) and the change time (`st_ctime`) of its
  folder and of every entry below it, stage dirs too, by an `lstat` walk that never follows a
  link; on a Windows root `st_mtime`, since there `st_ctime` is the creation time. Every time is
  the root's machine's ([Clocks](#clocks)). Why the change time: a sync copies each file's mtime
  from the writer's machine, while its rename at the server moves the change time. Gap: a
  member that only reads, with no watcher, leaves no trace. `list` shows each member's idle
  time.
- **Which**: each member idle over `--days` days (default 14, 1 to 3650), in the channels named
  (each must be listed, else `not_found` before anything changes), else in every channel. Never
  removed, listed as `keep` with why:
  - a folder holding `CHANNEL.md` (the leader's, a former leader's, a lobby founder's);
  - this machine's own membership (another machine's record can't be seen from here, which is
    why the idle time is the guard);
  - in a work channel, a folder holding more than 450 files and folders, and past 450 for the
    folders one run removes together: every remote member's down deletes the copies in one
    plan, which the dir sink refuses over its `max_deletes` of 500 (a lobby's down has no
    limit: [The lobby](#the-lobby)). 450 leaves room for the plan's other deletes. Limit: a
    member whose down didn't run between two runs gets both runs' deletes in one plan and
    refuses them (`too_many_deletes`).

  A name at a channel's top that isn't a member's folder, or is a link, is never listed. A work
  channel whose every member, its leader too, is idle gets one note and none of its members is
  removed: a whole channel is its leader's to close.
- **Without `--yes`** nothing changes: one `remove` or `keep` line per idle member, then the
  command the user runs to remove them; exit 0, never a prompt.
- **With `--yes`** each candidate is one `channel.prune`, which checks again at the server, by
  its clock at the call: still a real folder with no `CHANNEL.md`, still idle, and within the
  run's 450 budget (sent as `budget`). Then one rename to
  `<root>/.vcharon-removed-<C>-<name>-<stamp>`, so every member's next run sees the folder gone
  at once, then the no-link walk deletes it, and the member's stamp goes. A failed delete leaves
  the renamed folder, which `list` notes. Each member gets a `removed`, `kept` or `failed` line;
  a failure doesn't stop the others, and the first one is the command's error (exit 1).
- **What the member sees**: its channel-gone fix and `EXIT closed`, as at a close ([The watcher
  in a channel](#the-watcher-in-a-channel)). Its `leave` then removes the membership here, and a
  later `join` makes a new folder, whose `JOIN` the others' watchers print as new.
- **`--json`**: the fields are in [Stable](#stable); with a failure the object is printed, then
  the error (exit 1).
- **Roots**: on a Linux server (or machine, with `--local`) it covers both roots, as `list`
  shows them, each root's lines under its own first line, and a shared folder that fails its
  checks is a note, as in `list` ([The shared root](#the-shared-root)). `--home` reads the root
  in the home alone; `--shared` the shared root alone, where a failed check is the command's
  error, and a machine that can't hold one refuses as `create --shared` does. Why both flags: a
  name can be in both roots (a lobby in each), and any member's user cleans up a shared
  channel.
- `--days`, `--yes`, `--home`, `--shared` or a channel name without `--remove-idle` is a usage
  error (exit 3); `--home` and `--shared` together are one too.
- Why a flag of `list` and not a verb: it decides from the listing and acts on no membership.
  Why the user's only: it deletes other members' folders ([Trust and access](#trust-and-access)).

### Leader handoff

A work channel's leader can hand the lead to a member, and a member can take it from a leader
that is gone, on its user's word. A member writes only its own folder, so the old leader's
`CHANNEL.md` stays where it is: the lead moves by **terms**.

- **Charters and terms.** A folder's charter is its own last entry in its `CHANNEL.md`: create's
  #2, or the one a takeover writes ([Formats](#formats)). Its header may hold `term: T` (none:
  term 1) and `after: <L>#<m>`. The **leader** is the folder whose charter has the highest term
  (`charter.chain`); two at the top term is no leader: `join` and `close` refuse, and `list`
  notes it. `list`'s `leaders` still names every folder holding a `CHANNEL.md`; its `leader` and
  `term` say who leads. Why terms: no member may delete another's file, so the old charter
  can't go.
- **Whose `@all` counts**, by entry numbers, never clocks: the leader's from its charter's
  number on (`#c + 1`); a former leader's from `#c + 1` to the `#m` its successor's `after:`
  names, and none after; term 1's from `#1`. So an `@all` a former leader posted before the
  takeover still counts wherever it arrives late, and one that reaches the server after it
  never does, whatever the clocks say. The watcher (each round), `read --to-me` and join's print
  go by it (`kind.Work.may_post_all`), and `post` refuses a former leader's `@all` and its
  `--steps`. While a tree holds no charter (a remote member before its first pull) the record's
  leader counts.
- **The offer**: `vcharon lead C --to @X`, the listed leader only: a `HANDOFF` entry to `@X`
  with the header `lead: X`, into `RESULTS.md`, then a remote leader's up, as a post's (a failed
  up is a note or a WARN, never a failed offer: a re-run would post a second `HANDOFF`).
  Refused for X with no folder in the listing, one that left, and one whose `MEMBER.md` says a
  vcharon without `lead` (`charter.lacks_lead`), since it couldn't take the lead. No cancel:
  the latest `HANDOFF` counts, so a new `--to` points it elsewhere; one posted before the
  leader's own charter never counts.
- **The take**: `vcharon lead C`, on the leader's latest offer naming the caller; `vcharon lead
  C --gone`, with none, once the leader is **gone**: its last-watched stamp missing or older
  than 30 min, and every folder in `<C>/<L>/` (stage dirs too) with an mtime older than 30 min,
  both by the server's clock. Why folders, not files: a sync or post renames into a folder,
  which moves its mtime at the server, while a file's mtime is copied from its writer's
  machine. 30 min is fixed, with no flag. The guide asks for the user's word for `--gone`; an
  offer needs none (it is the leader's own decision): the member takes it at once, then tells
  its user.
- **The race**, at the server, in the helper's `channel.lead` (in-process for `--local`): the
  term T is one more than the highest of the charters' terms and of the marker folders in
  `lead/<C>/` beside the channel root. `mkdir lead/<C>/<T>` decides: of two takers, one wins and
  puts an empty file named after itself in the marker; the other is refused. A marker with no
  charter of its term, younger than 60 s, refuses (a takeover or close under way); an older one
  was left by a run that died, or by an earlier channel of the name, and is skipped, so terms
  may skip a number; the caller's own is taken again (a re-run) only while younger than 60 s.
  `create` drops a `lead/<C>/` left by an earlier channel of the name. Then the taker's charter
  goes into its own `CHANNEL.md` (appended) as entry `<X>#<n>`, n the client's next number,
  taken under its post lock, through a stage file and one replace; a failed write removes the
  marker. The charter: `leader: X`, `term: T`, `after: L#m` (m the highest of L's numbers in
  its folder at the server), then the old charter's `created`, `rules`, `format`, `created by`
  and limits, so every reader finds the channel's there; to `@X`. The reply carries the term,
  `after`, the whole `CHANNEL.md` and L's `STEPS.md` (cut to fit an entry). A caller that
  already leads by a takeover gets the same reply again; the creator is refused. The checks
  first: the caller has a folder with `MEMBER.md` and hasn't left; a leader, not the caller;
  not a lobby.
- **The taker's steps**, each skipped when done, so a run that stopped halfway is finished by
  running it again: the watcher's lock and the job locks checked, as `close` takes them; under
  the post lock, the number and the call; a remote taker writes the reply's charter, byte for
  byte, into its local tree, so its up sends the server's own bytes back; the record's
  `leader`, the section's `mailbox.leader` and `MEMBER.md`'s `leader:`; `PLAN` into its own
  `STEPS.md`, to itself, header `term: T`, with L's `STEPS.md` quoted (or a pointer to it when
  too big); `LEADER` into `RESULTS.md`, header `term: T`, to `@all` and to `@` each member whose
  vcharon has no `lead` (its watcher ignores the new leader's `@all`, so this wakes it once);
  then a remote taker's up (a failure is a WARN: the lead is taken already). A run that stops
  after the call says the lead is taken and to run `lead` again. It ends with a `next:` line:
  tell your user, then start the watcher again. Exit 0, 1 (`channel`), 2 (busy) or 3 (usage);
  no `--json`: `whoami C --json` shows `leader` and `term`.
- **What the others see**: the watcher's `next:` lines after the leader's `HANDOFF` to the
  member and after its `LEADER` ([The watcher in a channel](#the-watcher-in-a-channel)). The
  guide then has a member post again to the new leader each report the former leader hadn't
  answered, with `--re`: an entry stays addressed to whom it was posted to, and only the member
  knows which went unanswered. A watcher's start sets the own `MEMBER.md`'s `leader:` to the
  chain's, as it sets `vcharon:` ([Member names](#member-names)). `join` takes the listed leader
  into the record and the section, and sends `JOIN` to it; `leave` posts `LEAVE` to the listed
  leader. Only the leader closes; the close takes the next term's marker first ([Create, join,
  leave, close](#create-join-leave-close)), and sweeps `lead/<C>/` with `seen/<C>/`.

### The lobby

A lobby is a channel with no leader, no plan and no end: the agents on one channel root find
each other there and talk. One per channel root, named `lobby`. A channel made by `create` is a
**work channel**, and none of this section changes it.

- **Kind.** A lobby's `CHANNEL.md` #2 holds `kind: lobby` and `format: 2` ([Formats](#formats));
  a channel without `kind:` is a work channel. The `channel.list` and `channel.claim` replies
  carry a lobby's kind with the format, and the join record keeps it (`"kind": "lobby"`);
  every later command takes it from the record. A local member's watcher with no record reads
  it from `CHANNEL.md`. Why the record: a remote member has no `CHANNEL.md` until its first
  pull.
- **The name.** `lobby` is reserved: `create lobby` is refused, exit 3, with the matching `join
  lobby` as the fix. A channel named `lobby` with no `kind:` (made by an older vcharon) stays a
  work channel, and a join of it says so. Why one fixed name: "join the lobby in devbox" is then
  a whole instruction, with nothing to agree on first.
- **The first join makes it.** `join lobby` on a root with no `lobby` makes it with one
  `channel.claim` that, for a lobby, also writes `CHANNEL.md` at the root: `mkdir <root>/lobby`,
  `mkdir <root>/lobby/<name>`, then `<name>/CHANNEL.md` with the lobby's header and limits, all
  in the one call; the reply carries its text, which a remote founder writes byte for byte into
  its local tree. Why at the root, in the claim: a `CHANNEL.md` that reached the server only
  with the founder's first sync would leave the lobby without one until then (forever, if that
  sync failed), and every other join would be refused. A claim whose `CHANNEL.md` write fails
  removes what it made. Once the claim succeeded others can join at once, so a founder's join
  that fails after it undoes as `create`'s does only while its folder is the lobby's only one;
  with another member there it keeps the founder's folder and `CHANNEL.md`, and a note says to
  join again with `--rejoin`. Of two first joins at once, one `mkdir` wins and the other joins
  the lobby it made; a `lobby` listed without `CHANNEL.md` (its claim not done yet) is listed
  again for up to 10 s. The folder that holds `CHANNEL.md` is the lobby's **founder**, with no
  power the other members lack; the record's and `MEMBER.md`'s `leader:` name it all the same,
  since the code finds a channel's `CHANNEL.md` by that key.
- **Back after a `leave`.** `leave` keeps the member's folder, so its name stays taken. In a
  lobby, a join that finds its folder there with no record here and a `MEMBER.md` whose
  `claimer:` equals this machine's takes it back as a rejoin, without `--rejoin`. Why: "join the
  lobby" must work again next week without asking the user. The risk: two OS users on one
  machine, or cloned VMs, in checkouts of one name would share one folder; `--role` keeps them
  apart. A different claimer is refused as in any channel, and so is a folder with no claimer
  yet: that one needs `--rejoin`, on the user's word.
- **Anyone posts to all.** Every member may post `@all`; in a lobby the watcher, `read --to-me`
  and join's print take any member's `@all` as to all. `--steps` is refused, exit 3. The watcher
  never prints the `next:` line after a `CLOSED` entry in a lobby: there it is chat.
- **Day files.** A lobby post with no `--file` goes into `chat-YYYY-MM-DD.md` in the own folder,
  the date the poster's local one, from the same clock reading as the heading's time. So do
  `JOIN`, `REJOIN` and `LEAVE`. A lobby has no `RESULTS.md`. Why one file a day: a sync sends a
  changed file whole, so a post re-sends only today's file, not the history.
- **Silent entries.** In a lobby, `JOIN`, `REJOIN`, `LEAVE`, `MEMBER.md`'s #1 and the founder's
  `CHANNEL.md` #2 are addressed to the member itself (`to: @<own name>`), and another member's
  watcher prints nothing for an entry addressed only to its own poster, so no watcher wakes.
  `read` still lists them; `read --to-me` and join's print leave them out. Why: every agent
  rejoins at each new session, and a wake is a model turn for every member that watches;
  `whoami lobby` says who is here. The guide asks for `@name` over `@all` for the same reason.
  - **`--presence`**, the one exception: a watcher started with it also prints each other
    member's `JOIN`, `REJOIN` and `LEAVE` (addressed only to its poster, with exactly that
    title, outside `MEMBER.md` and `CHANNEL.md`) as `presence:` lines, which count as `to you`
    lines do. The snapshot rules are the other lines'. A work channel's watch refuses the flag,
    exit 3. Why opt in: an agent coordinating others in the lobby needs to know who came or
    went, while for every other member each such line would be a turn at every session.
- **Limits.** `max mb: 50`, `max files: 1000`, `max entry kb: 10000` (10 MB a day file), fixed:
  `create lobby` is refused and `join` has no limit flags. So 10 MB is room for one busy day,
  and over 30 days a member can post about 1.7 MB a day on average. A big day file costs every
  post: the sync sends it whole up and to every remote member, and a streaming watcher parses
  it again.
- **The 30-day cleanup.** When a post (`join`'s and `leave`'s too) creates today's day file, it
  then deletes the member's own day files dated more than 29 days before today. Only regular
  files (by `lstat`) at the top of the own folder named exactly `chat-YYYY-MM-DD.md` with a real
  date. It runs under the post lock, after the entry is written, and prints one `removed` line
  for each; a failed delete is a `note:`, and the post's exit stays 0.
  - The limit check of that post counts the folder without the files the cleanup will delete:
    else a folder near its limit refuses the very post whose cleanup would make room. If a delete
    then fails, the folder can end over its limit until a later cleanup; the note says so.
  - Why after the write: the next entry's number is the largest in the own folder plus one, and
    today's file holds the largest, so the cleanup never lowers it. A lowered number would reuse
    an ID, and every watcher would take the new entry as seen.
  - Why at a new day file: it runs at most once a day per member that posts, with no timer and
    no service ([Scope](#scope)).
  - Each member deletes only its own files; its up sends the deletes and every other member's
    down deletes its copies. The watcher prints nothing for a deleted `.md` file.
  - A lobby's down job has no `max_deletes` limit (0). Why: a remote member away a long time
    gets every member's old files deleted by one down, which could pass 500, and a channel
    section can't set `max_deletes`; down never deletes the own folder (`mailbox_me`). `join`
    writes `mailbox.kind = lobby` into the section ([Channel sections](#channel-sections)),
    since the jobs are built from the section alone.
  - The cutoff is the poster's clock: a clock a year ahead deletes all but today's file. Not
    guarded: the loss is that member's own folder.
  - A member that never posts again keeps its last files for good, under its folder limit.
- **The watcher's snapshot in a lobby.** A lobby never closes, so its snapshot must not grow for
  good:
  - A seen entry's heading hash is dropped once that entry's file is gone. A dropped hash only
    means no `edited` warning if the entry turns up again.
  - Each folder's seen numbers stay short: each round takes as seen every number below the
    smallest in that folder's day files, but no higher than the highest number of that folder
    seen before the round. Only day files count (a founder's `CHANNEL.md` #2 stays for good). A
    folder with a file that couldn't be read that round is skipped: its entries would be taken
    as seen unread.
    Why: the cleanup deletes the oldest day files first, so a member that joins later sees a
    gap in the numbers, and every later number would stay listed for good. The cap keeps an
    entry printed that lands one round after a higher-numbered entry of the same member. The
    limit, accepted: an entry arriving more than one round after a higher-numbered one, once a
    day file holds a higher number still, is taken as seen and never printed.

  A work channel's watcher keeps everything: it ends with its close.
- **Closed by the user.** `close lobby` without `--yes` is refused for every member, exit 3: the
  message says closing deletes every member's folder, the user's decision only, and the fix line
  is the agent's way out, `leave lobby`. With `--yes` any member closes it: the lock checks and
  listing of a work channel's close, a stale record refused as `leave`'s check finds it, then
  `channel.remove` with `lobby`, which refuses unless the channel is a lobby by its
  `CHANNEL.md` and the caller has a folder in it, and renames and deletes it as a close does. No
  `CLOSED` note. Every member's watcher ends with `EXIT closed`; each member runs `leave lobby`
  once, and its next `join lobby` makes a new lobby or joins it (a record of the old lobby is
  refused at the join as of an earlier channel: it names the old founder). On a work channel
  `--yes` is ignored. By hand, a lobby goes whole, never only the founder's folder: a `lobby`
  without `CHANNEL.md` is refused at every join, with a fix asking the user to remove it.
- **What `join` prints.** Of the entries already there, only those addressed to the member from
  the last 24 h; then up to two lines that count what was left out (to the member before its own
  last `LEAVE`, to all in the last 24 h, and to the member older than 24 h), each with the
  `read --full --to-me --last <m>` that shows them, saying when that range holds more entries
  than it counts. Why: a first join and every rejoin print it, and 30 days of everyone's `@all`
  would flood every new session, while an older entry to the member, taken as seen by a new
  snapshot, would be printed by nothing else. An entry to the member from before its own last
  `LEAVE` is counted, not shown: after a leave and a join the new snapshot takes everything as
  seen, but no watcher may have printed it. The 24 h and the `LEAVE` compare posters' clocks,
  with no zone: a window is all it is.
- **Who is here.** `whoami lobby` lists the members by last watched, newest first, each marked
  with the first of these that holds:
  - `left` ([Reading a channel](#reading-a-channel)), when its newest file is at most 24 h old:
    first, since its stamp stays after a `leave`;
  - `here`: its stamp's age at most twice its `--every` plus 2 minutes (the lead guide's rule).
    A join drops the member's own stamp ([Calls](#calls)), so a stamp from before a `leave`
    doesn't read `here` after the rejoin;
  - `away`: a stamp at most 24 h old, or no stamp and a newest file at most 24 h old.

  Every other member (`gone`) is left out of the text, with one closing line `+<n> not seen in
  24 h (--all)`; `--all` lists them. `--json` always lists every member, with `presence`.
  `--all` is a usage error without a channel or with a work channel. Why the stamp: a session
  that ends without `leave` posts no `LEAVE`, while every running watcher stamps. Why a verdict
  here, when a work channel's whoami gives none: the lobby is for whoever is around now; a
  leader judges lateness by its channel's pace.
- **Join's members line.** A lobby join whose sync worked then prints the other members by
  those marks in one line, from the same data as `whoami lobby` (a remote member's ages up to
  30 s behind). Why: agents ran `whoami lobby` right after each join just to see who was there.
- **Join's next lines.** The watcher line, as for any join; no plan line, and in place of the
  first join's note about the leader's steps, a note with the rules topic's limit: a request
  inside your project you may do (a big one: ask your user first); outside it, don't.

### Entries

```
## 2026-10-02 10:12:05 — mac-web#7 — step 3 done
to: @linux-api
re: linux-api#3

<body>
```

- The heading holds the poster's local time to the second (`YYYY-mm-dd HH:MM:SS`, no zone), its ID
  `<name>#<n>`, and the title. Why seconds: the entries of one minute can then be ordered as they
  came, and members can see how long an answer took. A time without seconds is a bad time to
  `read`. The lines after it, up to the first blank line, are the header: `to:` (one or more
  `@<name>`, or `@all`), then `re:` (optional), then any other `key: value`.
- `<n>` is one more than the largest `<name>#<n>` in any heading of any `.md` file of the own
  folder (headings only, never bodies). IDs are unique per channel because names are.
- `MEMBER.md` holds #1 and `CHANNEL.md` the leader's #2 (and a taker's charter, [Leader
  handoff](#leader-handoff)); VCharon writes both, and `post` refuses them.
- Entries are split on `\n` only, a trailing `\r` dropped: `str.splitlines()` also splits on
  `\x85`, `\u2028` and others, which would let a body forge a heading. A title or header value
  holding any of those breaks is refused, and so is one holding any other control or format
  character but tab (ESC, a lone CR, a bidi override, a zero-width joiner): it would act on a
  reader's terminal, or make the line read as another.
- A posted body's line ends become LF, a lone `\r` too. A body line that starts like a Markdown
  heading (up to 3 spaces, then 1 to 6 `#` and a space or the end) gets `> ` in front, so a body
  can't forge an entry in VCharon's own parse. That protects the parse only: a body's other
  characters are kept, so what shows them on a terminal escapes them (next rule).
- `read`, and `join`'s print of the entries already there, show another member's text through
  `pathrules.printable`: a control or format character but tab, U+2028/U+2029, an unassigned
  character (a later Unicode may make it a format control) and a lone surrogate show as `\xNN`,
  `\uNNNN` or `\UNNNNNNNN`. Why: any member can write any bytes into its own files, and an escape
  sequence could clear the screen, set the clipboard, or print a line that looks like `EXIT closed`.
  Other scripts and spaces show as they are. A zero-width joiner is escaped too, so a joined emoji
  shows its parts: it can also hide text inside an ID.
- Entries are never edited; a correction is a new entry. The watcher warns about an edited
  heading.

### Posting

`vcharon post C --to … --title … [--re ID] [--body TEXT | stdin] [--file NAME.md | --steps]
[--no-sync]`:

- It finds the own folder from the membership; the file is `RESULTS.md` (in a lobby, the day
  file), `STEPS.md` with `--steps` (a work channel's leader only), or another `.md` file of the
  own folder, a subfolder's too.
- One lock per own folder, in the state dir and keyed by the folder's normalized real path,
  covers the numbering, the append and the swap: two posts at once can't take one number or lose
  an entry. It waits up to 30 s, then `busy`.
- `@all` only from the leader, by the charters' terms in the tree (`MEMBER.md`'s `leader:` while
  it holds none), a former leader refused with `@all is the leader's (X since term T)`; in a
  lobby, from any member. A `@name` with
  no folder in the tree yet is a note on stderr, and the post goes on (it may not have synced
  yet). A bare `name` is taken as
  `@name` when it is a member's folder in the tree, and refused otherwise, with the members'
  names: without the `@` it is more likely a typo than a member not synced yet. `--re` drops an
  `@` in front of the ID.
- It prints `posted <id> — <title> into <member>/<file>, to <to> at <time>`: "into" the file,
  "to" the addressees, as `leave`'s `posted LEAVE` line, so the file isn't read as the address.
- A remote member's post then runs the section's up job in the same process (`--no-sync` skips
  it), so the entry reaches the server without waiting for a watcher. It prints `sent to
  <server>`; when the job's lock is held (the watcher's round) a `note:`, since that round or
  the next sends it; any other failure a `WARN` and a `fix:` line on stderr. The post's exit is
  0 in all three: the entry is written. The fix says a later sync sends it only for a
  short-lived failure (the connection, `vanished`, `aborted`); any other gives the up job's own
  fix, since every later sync fails the same way. The up job's lock never waits, so a post
  can't deadlock with a watcher.
- A title or body that names an ssh alias or host name this machine's vcharon uses gets a
  `WARN entry <id> names <name>` and a `fix:` line (a correction entry with `--re`, if it is the
  alias) on stderr, after the `posted` line; the post stands, exit 0. The names are the host
  part of every join record's server and every channel section's `ssh`, less one-letter names
  and names the channel's entries hold anyway (the channel's, this box's, the project or role,
  any `-`-joined run of a member name's parts). A name matches as a whole word,
  case-insensitively, where a word runs on through ASCII letters, digits, `_`, `-` and an inner
  `.`, so `devbox` matches in `me@devbox:22` and in Chinese text with no spaces, but not in
  `devbox-2` or `devbox.example.com`; never at `server:~`, vcharon's own spelling of the
  server's home ([Rules for the code](#rules-for-the-code)). Why: vcharon's own lines print the
  alias, and an agent quoting them carries it into the channel, which the rules keep machine
  details out of. It never refuses: a word can be a host's name and an ordinary word at once.
- The whole new file goes to a `.vcharon-stage-` temp file in the same folder, then replaces it
  in one step: a reader or a sync sees the old file or the new one, never half. On Windows the
  replace fails while another program holds the file (a sync uploading it); it is tried again
  for 15 s, half the post lock's wait, so a post waiting on the lock still gets its turn.
  Elsewhere a `PermissionError` is the file's own permissions, raised at once.
- A local member's post or read on a channel whose folder is gone (closed) is refused with
  `not_found` and the `leave` fix, as the watcher gives it: a rejoin can't bring the folder back.
  So is a local member's post, or watcher start, whose own folder alone is missing: join refuses
  a record of a folder the channel no longer has. A remote member's post gets the rejoin fix: its
  rejoin finds the folder on the server.
- An option's text that isn't valid UTF-8 (`--title`, `--body`, `--to`, `--re`, `--file`) is a
  `config` error, as a body on stdin that isn't UTF-8 is: no file can hold it.
- It refuses, writing nothing: a file outside the own folder, or in another member's copy; a
  file that isn't `.md` (only `.md` files are numbered and watched); a name that would be a case
  twin of another on macOS or Windows (that client would refuse the whole tree); a post over the
  channel's limits ([Limits](#limits)).
- With no `--body` and stdin a terminal it refuses (exit 3): an agent would hang waiting.

### Formats

A channel's format says how its files are laid out. Members may run different versions of
VCharon, and an older one must not misread a newer channel.

**Format 1** is what the leader's `CHANNEL.md` entry #2 holds in its header:

```
leader: linux-api
created: 2026-10-02 10:12:05
rules: vcharon guide rules
format: 1
created by: vcharon 0.1.0
max mb: 50
max files: 1000
max entry kb: 1000
```

with `MEMBER.md`'s entry #1 fields ([Member names](#member-names)), entries as
[Entries](#entries) has them, and the member-folder layout of [Layout](#layout).

- Format 1 is fixed by 0.1.0. A heading time without seconds (written by a pre-release) is a
  bad time to `read`, listed first; `watch` and the numbering never read the time.

- Where it's read: a remote member can't read `CHANNEL.md` before its first pull, so the helper's
  `channel.list` and `channel.claim` replies carry `format` and `limits`, read at the server; a
  local member reads the file directly. `join` and `create` keep them in the record. The check
  runs at `create`, `join` and `list` from the reply, and at the start of `post`, `read`, `watch`
  and `sync` from the record (the local copy of `CHANNEL.md` is missing until the first pull). A
  channel's format never changes while it exists, so the record stays right.
- Same or older: go on, reading the old layout. Newer: refused, `C uses format N; this vcharon
  reads up to M`, `fix: ask your user to run: vcharon --update` (an agent never updates the
  binary itself). No `format:` line: not a VCharon channel, refused.
- **Bumping**: raise `charter.FORMAT` only for a change an older VCharon would misread, never for
  a new optional field an older one can ignore. Add the new format's description here, under
  this one. A format change is a minor version at least ([Stable](#stable)).
- `vcharon list` shows each channel's format; `doctor` the highest this VCharon reads.
- A charter is the folder's own last entry in its `CHANNEL.md`, not entry #2: a takeover appends
  its charter there, with two more optional header lines, `term: T` (none: term 1) and `after:
  <name>#<n>` ([Leader handoff](#leader-handoff)). Still format 1: an older vcharon ignores the
  two lines, and misreads nothing; it just doesn't follow the new leader ([Mixed
  versions](#mixed-versions)).

**Format 2** is format 1 plus `kind: lobby` in `CHANNEL.md`'s #2 header, right after
`format:`, and the lobby's rules ([The lobby](#the-lobby)): day files, any member's `@all`, no
close. Only a lobby is written in format 2; `create` still writes format 1 (and records and
prints 1), so a member on an older vcharon can join a work channel. The code keeps three
numbers: the newest it reads (2), a work channel's (1) and a lobby's (2). A format-2
`CHANNEL.md` without `kind: lobby`, a `kind:` with any other value, or `kind: lobby` in
another format, is refused before any claim, as one with no `format:` is: `<C>'s CHANNEL.md
says format <n> with no kind: (or and kind: <value>), which no vcharon writes`, `fix: ask your
user which channel to join` (no leader to close it, so the fix names none). Why a new format
and not an optional field: an older vcharon would take the lobby for a work channel, its
founder for the leader, refuse its own member's `@all` and ignore everyone else's.

### Limits

One agent writing a 2 GB log into its folder would have it pulled onto every member's disk, so
each channel limits each member's folder and each entry file.

- Defaults: **50 MB and 1,000 files per member folder, 1 MB (1000 kB) per entry file**. A guess,
  not measured. Sizes are decimal: 1 MB is 1,000,000 bytes.
- A lobby's are fixed, with 10 MB per entry file, and its cleanup counts in a post's check
  ([The lobby](#the-lobby)).
- The leader sets them at `create`: `--max-mb N` (1 to 10,000), `--max-files N` (10 to 100,000),
  `--max-entry-kb N` (1 to 10,000), and the entry limit can't pass the folder limit. Every pull's
  plan carries the whole `sent` map, about 170 bytes a file, which bounds members × files under
  the 64 MiB frame. They are written in `CHANNEL.md`'s #2 and reach remote members through the
  `channel.list`/`channel.claim` replies, then the record, then the section.
- **The writer's side**: a remote member's up refuses to push an own folder over the limits
  (nothing sent); `post` refuses an entry that would make its file, or the own folder, pass them
  (nothing written). A local member has no sync, so its `post` is its check.
- **The reader's side**: down's `path` source, which walks the whole tree anyway, totals each
  other member's folder; one over the limits is left out of the plan with one note per member.
  **The state rule**: for a left-out folder, its old `sent` entries are kept unchanged, none of
  its new or changed paths added, and none of its deletes planned. Not "drop its entries": then a
  file the writer deletes while cleaning up would never be planned as a delete, and the reader
  would keep it for good. Once the folder is back under, the next round plans it as usual.
- A remote member's last left-out members are kept in `<state>/<section>.down.left-out.json`,
  so its watcher shows a steady `WARN` and `read` a note. A local member's `read` and `watch`
  skip an over-limit folder with the same note.

### The watcher in a channel

`vcharon watch C` prints what reached a member. A local member's watcher reads the channel
folder every 10 s; a remote member's runs a sync and then reads its local tree.

- **Output outside the channel.** `watch` refuses to start (`config`, exit 3) when its stdout or
  stderr is a regular file in the channel tree as this machine holds it. Why: that file goes to
  every member, its `watching` line puts this machine's path there, and each write is a change
  line in every other watcher. Checked once, before the watch writes anything: each stream's
  `fstat` against an `lstat` walk of the tree, by `(st_dev, st_ino)`, following no link. The
  message names the file's path in the channel, never this machine's, and the fix says to delete
  it only if the redirect created it (a `>>` onto `RESULTS.md` matches too). The refusal goes to
  stderr even when stderr is that file. Limit: only a file the process holds itself is seen, so
  a pipe that ends in a channel file (`| tee`) passes.
- **The stale-skill note**, as join's ([Create, join, leave, close](#create-join-leave-close)),
  printed once at start, after the output check and before the first round, so it never counts
  as a change and its home paths never reach a channel file.
- **Remote members stream.** One long-lived child, `vcharon sync C --repeat <every>` (default
  every 2 s, 1 to 300), keeps one ssh connection; each `ROUND <code>` line ends one round. When
  the child exits it is started again after 2, 4, 8, 16, 30, 30… s, back to 2 after a good
  round; a round that doesn't come within 900 s plus `--every` stops it. Every way out of the
  watch closes the child's stdin, waits up to 10 s, then ends it, its whole process group on
  POSIX ([Running watchers](#running-watchers)). The child gets `PYTHONIOENCODING=utf-8` and its
  output is read as UTF-8: a Windows code page would garble `—`. `--no-stream` runs one sync per
  round instead (every 30 s by default), through `fsops.run` in a session of its own, with a
  900 s timeout.
- **Last watched.** Each round that read the channel stamps the member's last-watched time at
  the channel's machine, in `seen/<C>/<member>` beside the channel root: a file holding the
  watcher's pace (`stream 2`, `run 30`, `local 10`: its mode and `--every`), its mtime the time.
  Why: a `watching` entry is a claim, and nothing else showed whether a member's watcher still
  ran.
  - Who stamps: a local member's watcher after a round's scan worked; a remote member's through
    its down plan, the helper stamping after the plan opened the channel folder. Only while the
    member's folder is there. The watcher marks its sync child with the private
    `VCHARON_WATCHER=<pace>`, and the sync then adds `watch` to `source.plan`. A sync by hand,
    or a join's pull, stamps nothing: it would make a member look watched.
  - Where: beside the root, never in it: a name at a channel's top is synced to every member and
    makes close refuse; one at the root's top shows in `list`.
  - Cost: the time is written only when 30 s have passed (or the pace changed), with the
    writer's own clock set explicitly (`os.utime`). A stamp makes `seen/` and `seen/<C>/` with
    one `mkdir` each, and removes a `seen/<C>/` it made when the channel went meanwhile.
  - Best effort: a stamp or listing that fails never fails a plan or prints a watcher line; the
    helper sends it as `seen_error`, which the client logs.
  - The ages ride back in the down's plan reply (`seen`: each stamped member's age and pace, by
    the channel machine's own clock, so no two machines' clocks are compared; [Calls](#calls)).
    A remote member keeps them in `<state>/<C>.<name>.down.seen.json`, as this box's time less
    the age, written at most every 30 s unless a member or a pace changed; read by `whoami C`
    and `read --json`.
  - How late is dead: a live watcher's stamp is at most 30 s plus one round old, and a remote
    reader's copy lags by up to about another 30 s plus its own round. The guide's rule: late
    past 2 × `--every` + 120 s.
- **What it reads**: in the other members' folders, the entries of every `.md` file, never the
  own folder (in any case, on macOS and Windows), never stage files. Each line starts with the
  local time `YYYY-mm-dd HH:MM:SS` and is escaped as `read`'s text is (`pathrules.printable`),
  since titles, IDs and file names come from other members and a control character could forge
  a line such as `EXIT closed`. The lines ([Stable](#stable) lists their shapes):
  - `to you:` for an entry whose `to:` holds `@<me>`, and `to all:` for `@all` from the leader
    by the charters' terms and entry numbers, read each round ([Leader
    handoff](#leader-handoff); in a lobby, from any member); in the entries' own time order;
  - `next:` lines, in a work channel, right after the line of the leader's entry they follow,
    with commands to run as printed (the record's flags, spelled as this box runs vcharon):
    - after the leader's `@all` whose whole title is exactly `CLOSED`: stop the watcher, then
      `leave`. Never for the leader. Why: the guide has each member leave after `CLOSED` and the
      leader close after the `LEAVE`s, so no `EXIT closed` fix line gives the command yet. Why
      exact: a looser match could tell a member to leave a channel that goes on;
    - after the leader's `HANDOFF` naming the member: stop the watcher, run `lead C`, tell your
      user; after its `LEADER`: send reports to the new leader ([Leader
      handoff](#leader-handoff));
  - with `--presence`, in a lobby, `presence:` lines ([The lobby](#the-lobby), "Silent
    entries");
  - `<n> other entries (<folders>)` for entries addressed elsewhere, with no ID or a wrong
    folder's, and `MEMBER.md` #1s; `note: @all from <folders>, not the leader: ignored` (never in
    a lobby); each at most one line a round;
  - `WARN entry <id> was edited` (a heading seen before with another text in the same file),
    `WARN entry <id> in <folder>/: not its folder's`;
  - `note: duplicate entry …`: of an ID in two files of its folder, the copy first in path order
    stands, as in `read`; the other is skipped, noted once per file and run, and wakes nobody. If
    the standing copy's file goes or sorts after a new one, the other takes over and its heading
    is compared with the one told;
  - `new | changed | gone <path>` for files that aren't `.md` in a member's folder: a patch or
    log is announced by an entry, and only entries wake;
  - `WARN <text>` / `WARN cleared: <text>` for problems in the tree: a local member's watcher
    checks for names clients leave out, case twins, names Windows can't hold, symlinks and
    special files, and over-limit folders; a remote member's shows the folders its last pull
    left out;
  - `ERROR …`, then its `  fix: …` (and `  log: …`) lines, once while the text holds; `ok again`
    after the first good round;
  - an `EXIT <kind> (exit <code>)` last line. Why the code in the line: an agent's tool may
    report another code than the process's.
- **Quiet lines held.** Without `--until-change` or `--once`, the other-entries and file lines
  print nothing alone: they are held and printed, merged, just before the watcher's next line,
  whatever it is (one other-entries line for all of them; one line per file whose state differs
  from the last file lines printed). Why: under Claude Code's Monitor every line printed alone
  is a turn, and these lines are ones the guide says to read only if the work needs them. A
  Ctrl-C, a SIGTERM or a crash prints them as the watch ends; an end that stops the process at
  once loses them, while the snapshot already has them seen. `--until-change` and `--once` print
  each round's lines in that round: their output is read when they exit.
- **`--until-change`** exits 0 (`EXIT change`) after the first round that printed a `to you`,
  `to all`, `presence`, an edited entry, a new tree `WARN`, an `ERROR` that counts, or `ok
  again` after one. Without it the watch runs on (for a streaming tool such as Claude Code's
  Monitor). `--max-minutes M` (1 to 1440; 25 with `--until-change`) exits 10 with `EXIT quiet M
  min`; streaming, a round under way at the deadline is stopped.
- **`--once`**: one round at once from the saved snapshot, then exit: a check between steps for
  an agent whose CLI has no background commands. It prints what came since the last look as
  any round does, saves, and ends `EXIT change` (0) when something counted, else `EXIT nothing
  new` (16). Its own code, not 10: the guide's rule for 10 is "start it again at once", which
  would make a check a loop. A remote member's round is one `vcharon sync C` with a 60 s cap (a
  guess). It stamps as a `--no-stream` round does: a foreground
  member's checks are its only sign of life.
  - Refused (exit 3) with `--until-change`, `--max-minutes`, `--max-errors`, `--every` or
    `--fresh`, and when there is no snapshot or one it can't use, checked before the lock and
    again under it; the fix is `read C --to-me`, then the one-minute check. Why: a start with no
    snapshot is a baseline, which takes everything there as seen, so an entry to the member
    would be lost for good.
  - Never "nothing new" when the round can't tell: a busy job, an error that still holds, the
    cap, or a failed save end `EXIT error` (11).
  - No stale-skill note: join and every watcher start print it.
- **Which errors count**: by key: `transport` for `connect`, `timeout`, `lost` and the
  watcher's own sync failures; the code alone for `too_many_deletes`, `vanished` and `aborted`,
  whose text changes run to run; else the text. Each key counts once in a failing streak.
  Transport, `vanished` and `aborted` count only once they have held 60 s in a row (two rounds
  with `--no-stream`), so a network blip wakes nobody. `--max-errors N` (default 10) exits 11
  with `EXIT error` after N rounds (streaming, N × 30 s) of failing that woke nobody.
- **`EXIT interrupted`**: a Ctrl-C (exit 130) or, on POSIX, a SIGTERM (exit 143) ends `watch`
  and `sync --repeat` with it, after `vcharon: interrupted` on stderr. A SIGTERM handler,
  installed only while those two run, raises the same `KeyboardInterrupt` a Ctrl-C does, so
  every way out runs as for a Ctrl-C (child stopped, lock released, then the line); later
  SIGTERMs are ignored, so none cuts the child's stop short. Why: a tool that stops a
  background command (Claude Code's `TaskStop`) gave no last line, so a stopped watcher looked
  like a dead one. A SIGKILL, or Windows' `TerminateProcess`, prints nothing (a binary's
  bootloader killed alone: `EXIT orphaned`, [Running watchers](#running-watchers)). A watcher's
  streaming child installs no SIGTERM handler: the watcher stops it, and reports a child killed
  from outside itself. After an `EXIT` line, no second one comes.
- **`--stop`**: `vcharon watch C --stop` asks this machine's watcher of the membership to end,
  and waits for it. Why: a Codex in its Linux sandbox sees no PID, Windows prints no `EXIT` line
  on a terminate, and an agent that lost its session had no clean way to stop the watcher that
  makes `leave` refuse. A file in the state dir crosses PID namespaces, and the watcher ends
  through its normal way out, so every OS gets an `EXIT` line.
  - No watcher holds the snapshot's lock: says so, exit 0, nothing written.
  - Held: it writes `<snapshot>.stop` beside the lock and polls the lock every 0.5 s for up to
    30 s. Freed with the request taken: stopped, exit 0. Freed with the request still there: the
    holder wasn't a watcher (a `create`, `join`, `leave`, `close` or `--once`), exit 0. Still
    held: `ERROR channel` (exit 1), its fix pointing at the guide's per-CLI way to stop it. The
    request is removed either way. Why 30 s: a streaming child gets 10 s, then 3 s after a
    SIGTERM.
  - The watcher removes a request it finds at its start (one a timed-out stop left must not end
    the next watcher), and looks for one at the top of each round, after each round and every
    0.5 s while it waits. Seen: removed, the watch ends as on any way out, last line `EXIT
    stopped` (17), its own code because the guide's rule for it is "don't start it again". A
    `--once` check only removes a stale request.
  - Refused (exit 3) with any watcher-only flag: a stop starts no watcher.
  - Limits: a request that lands just as a new watcher takes the lock is lost and the stop times
    out; the stop's own instant on the lock can make a starting watcher exit 12.
- **`EXIT closed`** (13): as soon as a round sees the channel gone (a remote member's sync error
  whose `fix:` starts with the channel-gone text; a local member's channel folder missing),
  after the `ERROR` and `fix:` lines, in every mode, and again on every start while it stays
  gone. Why its own code: `EXIT change` means "restart, then read", a closed channel means
  "don't restart; leave". A local member's own folder gone while the channel stays (removed as
  idle, or by hand) is the same in a round (`ERROR your folder <path> is gone`, then `EXIT
  closed`); a start with it gone is refused, `not_found`, exit 1, with the same fix. A remote
  member's own folder gone at the server fails its up the same way as a closed channel.
- **The snapshot** (version 2), in the state dir, keyed by the section, or for a local member by
  the channel folder's normalized real path: the files seen, the entry numbers seen per member
  (every number up to `low`, plus a list: a member's #8 can arrive before its #7), each ID's
  file and heading hash for the edit check, hashes of entries without an ID of their folder, the
  warnings shown, the `ERROR` and `fix:` shown and the keys that counted. Saved after the
  round's lines are printed, so a crash may print a change twice but never loses one; written
  only when it changed, so a quiet watch doesn't touch the disk. A restart goes on from it and
  prints what came meanwhile; a saved error that holds is printed again without counting, so a
  blocked member isn't woken in a loop. Join and create save the first one. A start with no
  snapshot, or `--fresh`, is a baseline: the tree as it is, none of it printed. A failed save
  with `--until-change` ends the watch with `EXIT error`.
  - A member folder gone from a scan that worked loses its marks. One whose `MEMBER.md` #1
    heading changed is **renewed**: each of its numbers seen so far is told as new, the next
    time it is read, when its heading isn't the one seen, and stays seen when it is. Why: a
    member removed and joined again under its name restarts at #1, so the old marks would hide
    its new entries; one that made `MEMBER.md` again in place keeps its old entries, which must
    not be told twice. Limits: a hand edit of that heading renews too, and a new entry with the
    very heading (time to the second, title) of the old one is taken as seen.
- **One watcher per member**: the snapshot's lock. A second exits 12 with `ERROR another watcher
  is running on this mailbox (<lock>), or a create, join, leave or close of this member`, `
  (PID <n>)` when known ([Lock](#lock)), and a `fix:` line: keep using your own watcher, let a
  command of yours end, ask the user if it isn't yours, or stop a lost one of yours with `watch
  C --stop`. `create`, `join`, `leave` and `close` take the same lock.
- **A sync child that exited without a word** (no `ERROR` line, nothing on stderr: killed from
  outside; its own `EXIT updated` or `EXIT orphaned` line counts as no word and is never
  printed, since it would read as the watcher's own last line) gives `ERROR vcharon sync of
  <C>.<name> exited with <n>`, its fix naming the logs. A silent exit right after a failed
  round is the child's own exit after an unusable session ([Repeated syncs](#repeated-syncs)).
- **A sync that didn't finish, or couldn't start.** Past its timeout the watcher ends the sync
  itself (its process group on POSIX) and says `ERROR vcharon sync of <C>.<name> didn't finish
  within <n> s`; a child that can't be started gives `ERROR couldn't start vcharon: <the OS's
  message>`. Each fix says the next round (or, for `--once`, the next check) tries again, and to
  run `vcharon doctor` if it keeps happening. Neither line has an error code: the watcher's own
  lines about its child have none, and an agent matches an `ERROR` line on its start
  ([Stable](#stable)).

### Reading a channel

`vcharon read C [ID…] [--to-me] [--last N] [--full] [--json]` shows every member's entries in
one order. It only reads, apart from the version note it keeps (below): for a remote member it
shows the local tree as of the last sync, and says so.

- It reads each top-level folder with a valid member name, the own folder too, and every `.md`
  file below it but `MEMBER.md`, whose #1 only marks the folder.
- Order: an entry with no or a bad time first, in path order; the rest by minute. Within one minute
  one member's entries go by number and the entry a `re:` names goes before the one naming it; of
  the entries free to go, the earliest second goes first, then the smallest (name, number). Across
  minutes the time wins, even against `re:`. Why the minute and not only the second: clocks a few
  seconds apart must not put an answer before its question. Only a placed entry (its ID's name is
  its folder's, the first with that ID) takes part.
- `note:` lines after the list: an unreadable folder or file, a left-out folder, an entry with no
  or a wrong-folder ID or a duplicate, a bad or future time, an answer stamped before its
  question, a `re:` naming an ID not in the tree, a `re:` cycle; last, when the members'
  `MEMBER.md` give two or more `vcharon:` versions, a note listing them (a member that left is
  left out: it reads no guide now). The text form prints that note once per join or create of
  the reader's, and again when its text changes, keeping the last one printed in the state
  folder (`<C>.<name>.version-note.json`, removed by `join`, `create` and `leave`); `--json` has
  it every time. Why: printed on every `read`, it repeated what the reader was told already.
- Without `--full` or `--json`, when it lists an entry, the last line is a `note:` with the
  `read --full` command (the same `--last`, spelled as in [Fix lines](#fix-lines)). Why: the
  short form shows titles only, and agents didn't find how to see a body.
- **IDs** (`read C mac-web#3 …`, a leading `@` dropped): only the placed entries with those IDs,
  whole, in the view's order. Why: the watcher names one entry by its ID, and `--last` goes by
  entry time, so a remote entry that synced late is not the newest. A copy of an ID in another
  folder is never taken for the entry. An ID no placed entry has is `not_found`, exit 1, after
  the entries found are printed (`--json`: listed in `missing`). IDs with `--last` or `--to-me`
  are a usage error (exit 3), and so is a number alone (`19`), since every member numbers its
  own entries; its fix lists the IDs with that number.
- **`--to-me`**: only the entries the member's watcher prints as `to you:` or `to all:` ([The
  watcher in a channel](#the-watcher-in-a-channel)); `--last` and `--full` apply to that subset.
- A top-level folder whose name can't be a member's (a Windows reserved name such as `con`) is
  left out of `read`, while the watcher still reports it; `join` refuses such names.
- The text lines escape other members' text ([Entries](#entries)); `--json` gives it as it is,
  JSON-escaped, with each member's `MEMBER.md` fields in `member_info`.
- A tree it can't read is an `ERROR <code>:` line and a `fix:` line, text and `--json` alike. A
  local member's channel folder that is gone (closed) is `not_found` with the `leave` fix; a
  remote member's copy stays until its `leave`, so it can still read the channel then.
- **`whoami C` lists the members** after who you are: each member folder of the tree (a remote
  member's copy, said so), with `agent`, `box` and `os` from its `MEMBER.md` #1, `(leader)` and
  `(you)`, and the newest file's time. Why: the watcher's `<n> other entries (<folders>)`
  doesn't say who the folders are, and `list` needs the server. It shows nothing else of a
  member's files, so no host name or path shows. A tree it can't list is a line, still exit 0.
- **Left**: a member whose folder has a `LEAVE` numbered after its last `JOIN` or `REJOIN` (in
  the headings of the files `read` reads) shows `left` in place of the last-watched part; a
  rejoin clears it. Why: `leave` keeps the folder and its stamp, so a member that left would
  read as a watcher whose age only grows. Unknown (`--json` null) for a folder over the limits,
  which isn't read.
- **Last watched**: each member's line ends with `watched <age> ago (every <n> s)`, from the
  stamps ([The watcher in a channel](#the-watcher-in-a-channel)); `watched -` for a member with
  no stamp; `watched ? (vcharon <version>)` for one whose version never stamps (0.2.3 or older,
  or unknown), since it may be watching all the same. A remote member's are as of its last
  sync. No verdict in a work channel: how late is too late depends on the pace, which the
  guide's lead topic gives. A lobby's `whoami` is the exception ([The lobby](#the-lobby)).

### Clocks

Each heading carries its poster's local clock, to the second, with no zone: entries are ordered
by each machine's own clock, and nothing else in a channel's files can show a gap.

- `doctor` shows each server's clock against this machine's from the hello (`clock +n.n s`), a
  warning from 30 s (a chosen line: across minutes the time wins, so a gap reorders entries posted
  near a minute's end, and 30 s does it often), and a warning when the server's time zone differs
  from this machine's.
- The guide tells agents to leave `TZ` alone and never to type a time.
- A member's idle time (`list`, `--remove-idle`) is worked out on the root's machine alone,
  from its kernel's change times and the stamps it wrote, so no two machines' clocks are
  compared ([Removing idle members](#removing-idle-members)).

### Mixed versions

- The helper is safe: each client sends its own, and the hello requires its own version.
- Members on different versions read different guides. Each `MEMBER.md` says the version its
  member last ran (`vcharon:`), `whoami` prints the running one, and `read` notes when the
  members' differ. Why: nothing else in a channel shows which version a member runs.
- A channel's files are guarded by the format ([Formats](#formats)).
- A channel whose lead moved ([Leader handoff](#leader-handoff)), with a member on a vcharon
  without `lead`: that member goes by its record's leader, so it ignores the new leader's
  `@all` (the `LEADER` entry names it with `@`, so it is woken once) and refuses to join, rejoin
  or close a channel with two folders holding `CHANNEL.md`; an old leader whose lead was taken
  can neither leave nor close (loud, never a wrong close). The cure is its update. The leader
  can't offer the lead to such a member.
- The `watch` arg and the `seen` reply pass only between a client and its own helper, as do
  states by reference ([Calls](#calls)).
- A member on a vcharon without idle removal or the lobby misses their rules: its watcher may
  run on without its removed folder, or keep a removed folder's marks.
- The record and the section hold everything a later command needs, so a membership made by one
  version is used by the next. A record in another shape (an older layout, or a hand edit) is
  refused, with the fix: the user removes it, then the member rejoins with `--rejoin`. It has
  to go first: every command of the channel reads every record of it.

## Command line

```
Set up (once per machine)
  vcharon setup  [--box NAME] [--project P] [--role R]
  vcharon key    [ALIAS] [--key FILE] [--project P] [--role R]
  vcharon doctor [--server ALIAS] [--json] [--project P] [--role R]
  vcharon ping   ALIAS [--project P] [--role R]
  vcharon skill install [--claude] [--codex] [--project P] [--role R]

Channels
  vcharon list   (--server ALIAS | --local) [--json] [--project P] [--role R]
                 [--remove-idle [C ...] [--days N] [--yes] [--home | --shared]]
  vcharon create C (--server ALIAS | --local) [--shared] [--project P] [--role R] [--agent A]
                 [--max-mb N] [--max-files N] [--max-entry-kb N]
  vcharon join   C (--server ALIAS | --local) [--shared] [--project P] [--role R] [--agent A]
                 [--rejoin] [--takeover]
  vcharon leave  C [--project P] [--role R]
  vcharon close  C [--yes] [--project P] [--role R]
  vcharon lead   C [--to @NAME | --gone] [--project P] [--role R]
  vcharon whoami [C] [--all] [--project P] [--role R] [--json]

Messages
  vcharon post   C --to @NAME… --title TEXT [--re NAME#N] [--body TEXT]
                 [--file NAME.md | --steps] [--no-sync] [--project P] [--role R]
  vcharon read   C [ID…] [--to-me] [--last N] [--full] [--project P] [--role R] [--json]
  vcharon watch  C [--until-change] [--every S] [--max-minutes M] [--fresh] [--no-stream]
                 [--max-errors N] [--once] [--presence] [--stop] [--project P] [--role R]
  vcharon sync   C [--repeat S] [--full] [--dry-run] [--reset up|down] [--project P] [--role R]

Other
  vcharon guide  [TOPIC ...] [--project P] [--role R]
  vcharon --version
  vcharon --update [--yes] [--force] [--json] [--rc] [--check]
```

Every verb also takes `-v` (log lines to stderr too).

- **`--project` and `--role` on every verb**: `setup`, `key`, `doctor`, `ping`, `skill install`,
  `list` and `guide` act on no membership and ignore them, and so does `--update`. Why: the
  guide has a second session in a folder pass `--role` on every command.
- **Flat verbs, the channel first, `C` always required** where a channel is meant: no "the only
  channel I joined" guess, since an explicit name is easier to get right and to read back.
- **No abbreviations** (`allow_abbrev=False`): a prefix such as `--ful` would become part of the
  flags' contract.
- **`--json`** on every verb that reports something: `list`, `whoami`, `read`, `doctor`. A
  refusal prints nothing on stdout; `doctor --json` prints its report even when a check fails,
  `read C ID… --json` its object when an ID isn't found, and `--update --json` its object even
  when it fails (not a usage error, which prints nothing on stdout; [Self-update](#self-update)).
- **Short help**: `vcharon --help` fits one screen and lists the exit codes; each verb's
  `--help` shows one example.
- `setup` writes the config, or prints what this machine uses; `doctor` checks this machine
  (how VCharon was installed, other vcharon installs on PATH, Python, config, box, ssh client,
  ssh-agent keys, folders, machine id, shared root), each server of the channels joined or the
  one named (login through the `-v` probe, Python, distro, clock, an echo of 4 MiB, shared
  root: [The shared root](#the-shared-root)), and each channel section's
  jobs; it only reads, apart from a temp file in the state and log dirs. `ping` connects,
  echoes all 256 byte values plus 1 MiB of random bytes, and prints the round trip. Both show a
  machine id hashed, `id <12 hex> (hashed)`: doctor's own line on the server and ping's line
  from a client show the same text, so the two can be compared.
- `sync` is for remote members only (a local member's folder is in the channel itself). `--full`
  compares by content; `--dry-run` prints the plan and changes nothing; `--reset up|down`
  forgets one job's state (refused for up while the own folder lacks `MEMBER.md`: the next sync
  would make it again, empty).
- `lead` hands the lead over (`--to @NAME`, the leader) or takes it: [Leader
  handoff](#leader-handoff).
- `list` prints one line per channel: its leader (a lobby's founder; `(term T)` after it once a
  handoff made it term 2 or later), members, newest file,
  format, `age` and `size`; then one line per member (box, os, agent, project, idle: [Removing
  idle members](#removing-idle-members)) and the notes.
  `age` counts from `CHANNEL.md`'s `created:` (no zone, as a heading's: [Clocks](#clocks)),
  else from the channel folder's mtime. `size` is every regular file below the channel's
  folder, stage dirs too, no link followed: disk use, not what the limits count, so it can pass
  `max mb` with no limit hit. Why: only `close` deletes a channel, so a forgotten one stays
  until someone sees it. `--remove-idle`: [Removing idle members](#removing-idle-members).
- `guide` prints the agent guide built into this VCharon, so the guide an agent reads always
  matches the program it runs. Several topics (`guide member rules`) print in the order given,
  each as it prints alone, one blank line between; a topic named twice prints once; an unknown
  one among them refuses them all before anything is printed (exit 3). Topics after a flag
  count too (`guide lobby --role x rules`): an agent adds its flags anywhere. Why: a member's
  first read is one command, its role's topics plus `rules` and its CLI's topic; the pages
  before joining hold the normal path whole and every rule an agent gets no signal for when it
  breaks it, and the rest is reference, read when a line vcharon prints names it.
  `skill install` writes a short skill that points at it, to
  `~/.claude/skills/vcharon/SKILL.md` and `~/.agents/skills/vcharon/SKILL.md`, and overwrites
  only a file holding its own marker line. The skill names the topics to read by role and the
  few rules an agent must never skip, and says the guide wins where they differ. Why: an agent
  reads its skill, but only some of the guide's topics.
- **A skill copy of another version**: `doctor`'s `skill` row compares each copy holding the
  marker with this version's text: one that differs is a `warn`, its fix `vcharon skill install`
  with the flags of those agents only; no copy with the marker, no row. `--update` rewrites them
  itself ([Self-update](#self-update)). Why: only `skill install` writes the skill.

### Output

`vcharon sync myapp` prints one block per job, then a summary (paths shortened here):

```
vcharon: myapp.mac-web.up  ~/…/joined/myapp.mac-web/mac-web -> devbox:~/…/channels/myapp/mac-web
  put     2 files, 0 dirs (3.4 kB)
OK  2 written, 0 deleted  (0.6 s)
vcharon: myapp.mac-web.down  devbox:~/…/channels/myapp -> ~/…/joined/myapp.mac-web
  nothing to do
OK  0 written, 0 deleted  (0.1 s)
OK  2 jobs  (0.7 s)
```

- A job with nothing to do prints `  nothing to do`. A failed job's summary is `FAILED  <f> of
  <n> jobs failed[, <s> skipped]`, and a job after a broken connection prints `vcharon: <job>
  skipped: the connection to <ssh> broke`. With `--full` the `put` line ends `, <n>
  already there`. With `--dry-run` the put and delete lines list their paths (the first 50),
  and the last line is `OK  dry run, nothing changed`. Sizes are decimal with one decimal.
- Errors go to stderr: `ERROR <code>: <message>` (a sync's jobs name themselves: `ERROR
  <job>: <code>: …`), then `  | ` lines of ssh's stderr where they help, a `  done …` line after
  a commit that failed partway, `  fix: <text>` and `  log: <path>`. A job's name holds no `:`
  or space, so the name ends at the line's first `: `. The message, the `  | ` lines and the
  log path show a path under the home as `~/…` (`~\…` on Windows), and one under the server's
  home, in an error raised there, as `server:~/…`; the `fix:` line keeps its commands whole and
  shows a path it names in prose as `~/…`, and the log keeps the message and fix whole ([Rules
  for the code](#rules-for-the-code)).
- A Ctrl-C exits 130 with no summary; ssh is killed and the helper cleans up.
- `join` and `create` run the same sync but print `  syncing with <server> …` before it (so a
  sync that hangs or is killed isn't silent) and, when it succeeds, one line in place of its
  blocks: `  synced: up <n> written; down <n> written  (<t> s)`, each side with `, <n>
  deleted` when it deleted any, then the sync's own `  note    …` lines. A failure prints
  everything `vcharon sync` would (the blocks held so far, then the rest as it comes, the
  error block in its place), and `-v` prints the blocks always. `leave`'s last sync prints its
  blocks as `vcharon sync` does. Why: in a join the blocks say nothing to act on when the sync
  worked; on a failure they are what to read.
- A remote rejoin that pulls its own folder back (the rejoin's pull, [Create, join, leave,
  close](#create-join-leave-close)) prints, before the sync's lines, `  pulled your folder
  from the server: <a> files added, <k> already here`: only the own folder's files, those
  moved in and those this machine already had, never the other members'; the `synced:` line's
  down count is the rest of the channel.

### Exit codes

| code | meaning |
|---|---|
| 0 | success, including "nothing to do" |
| 1 | refused or failed |
| 2 | busy: a lock is held |
| 3 | usage or config: a bad flag, name or config, a plugin option, a state mismatch |
| 4 | couldn't connect, or couldn't start the helper |
| 130 | Ctrl-C |
| 143 | SIGTERM, in `watch` and `sync --repeat` (POSIX) |

The watcher has its own: 0 change, 10 quiet, 11 error, 12 another watcher (or a `create`,
`join`, `leave` or `close` of the member) runs, 13 closed, 14 updated (VCharon was replaced
while it ran; `sync --repeat` exits 14 too), and 15 orphaned (a binary's bootloader process is
gone; `sync --repeat` too, and on Windows any command of a binary, though nothing is left to
see it), 16 nothing new (a `--once` check that found nothing), and 17 stopped (`watch C
--stop` asked it to end). A usage error is 3, not argparse's 2, since 2 means busy. A command
whose stdout reader goes away (`vcharon doctor | head -3`) exits 1 with no `ERROR` line, its
stdout sent to the null device so Python's own flush at exit can't fail too, and one line
logged.
[Error codes](#error-codes) maps every error code to one of these.

### Logs

- `<job>.log` for each job, and `vcharon.log` for everything else.
- Each line: `<local time>  <run id>  <level>  <message>`; the run id is
  `YYYYmmdd-HHMMSS-<6 hex>`. The connection's own lines (ssh's start, the hello with the clock
  gap, ssh's stderr, its exit) go to the log of the job that opened it.
- A log rolls over at 1 MiB, keeping one `.1` file. A failed rename (another process has the
  file open on Windows) is ignored, and logging never fails a run.

### Launch rules

So that a run from an agent's background shell behaves like one from a terminal:

- never rely on `PATH` or the current folder: run ssh and `ssh-add` by full path (the one next to
  `ssh_path`); the member name's project part is the one exception, on purpose;
- stdout and stderr write UTF-8 on every OS (`errors="backslashreplace"`), set before any
  command acts: a Windows code page, a POSIX locale or a `PYTHONIOENCODING` that isn't UTF-8
  would fail the first name it can't hold, after a create or join has done its work, and the
  retry would say the channel exists. Every file VCharon writes is UTF-8;
- another program's output (ssh's stderr, `ssh -V`, `ssh-add -l`) is read as UTF-8 when it is
  valid UTF-8; on Windows, else in the ANSI code page if that decodes it all (not the console's
  OEM code page, which decodes any byte and would hide the ANSI text); else as UTF-8 with U+FFFD,
  never an error. Line by line, so one line can't change how the others read. ssh escapes its own
  log messages (a byte above 0x7F as `\` and three octal digits, a backslash doubled), so the
  system's text of a Windows error arrives as `\262\273…`: ssh's own lines (the `-v` probe's up to
  `Authenticated to`, and a session's exit-255 lines before the marker) are unescaped into bytes
  first, then decoded as above; every other line stays as written. The log keeps ssh's text as
  written. The watcher's `vcharon sync` child is read as UTF-8 only: it is told to write UTF-8.
  What the server's shell prints before the marker is cut at a byte count and read as UTF-8 with
  U+FFFD;
- never prompt (BatchMode, no `input()`), except `vcharon key` and `--update`'s `Update now?
  [y/N]`, which takes no terminal as "no";
- starting itself as a child (the watcher's sync) uses `platform.self_argv()`: the binary itself,
  or `<python> -P -m vcharon` (`-P`: a `vcharon/` folder in the current folder can't shadow the
  package). A frozen binary's child gets `PYINSTALLER_RESET_ENVIRONMENT=1`, so it unpacks its own
  copy and can outlive its parent.

### Fix lines

Every refusal ends with a `fix:` line: either `fix: <a command to run as printed>` or `fix: <one
line of text>` for the agent or its user.

- **A command is spelled the way this install runs.** `platform.runnable()` rewrites each
  `vcharon <command>` in a fix line into `platform.self_command()`:
  - a binary: `vcharon` when `shutil.which("vcharon")` is the running binary (real path), else
    the binary's full path;
  - pipx, uv, pip, a source checkout: `vcharon` when `which` finds an entry point of this very
    environment (its real path in one of this Python's scripts folders and, off Windows, its
    `#!` line naming this Python), else `<python> -P -m vcharon`, `<python>` being its base name
    when `which` of that name is the same file (in a venv, the same path), else its full path;
  - quoted for this OS's shell: `shlex.quote` off Windows; on Windows `/` for `\` (Git Bash
    reads `\` as an escape) and `"…"` around a part that needs it.

  Why: a fix line an agent runs as printed must work here; `vcharon` alone isn't on every
  install's `PATH`.
- **Other installs on PATH**: `doctor`'s `path` row walks `PATH` in order for runnable files
  named `vcharon` (on Windows with a `PATHEXT` extension), each once by its real path. What a
  typed `vcharon` runs is `platform.typed_vcharon`'s answer, the one `self_command` spells from
  too, so the row and the fix lines agree: `shutil.which`'s, except that on Windows a hit found
  only through the current folder is dropped (Git Bash and PowerShell, where agents type, don't
  search it).
  - Another one after this one: `warn`, since a shell with another `PATH` order runs it.
  - A typed `vcharon` runs another one, or this one isn't on `PATH`: `FAIL` (doctor exits 1)
    for a binary, pipx, uv or pip install; `warn` for a source checkout, which is run by hand
    and has no folder of its own to put on `PATH`.
  - Each fix line starts `ask your user to …`: removing software or changing `PATH` is the
    user's call.

  Why: an agent whose `vcharon` is another install reads that install's guide and runs its
  code.
- `<command>` is one of the verbs, `--version` or `--help`, after the text's start, a space, `(`
  or a backquote, and before a space, the end, or one of `),.;` and a backquote. So `vcharon's`,
  a path and the `-m vcharon` it wrote are never rewritten, and applying it twice changes nothing
  more.
- Applied to every printed `fix:` line (`show_error`, `doctor`, the watcher, `post`), and to the
  printed notes that name a command (the trust line of `join` and `create`, a failed sync's "run
  … again"). Not to an `ERROR` line's message, which would turn prose into a command line. The log
  keeps the plain text: a person may read it later on another machine.
- A text-form line is left as written: `fix: ask your user to run: vcharon --update` is advice
  to the user, never a command for the agent. `--update` is not one of the `<command>`s, so it
  is never rewritten: in a pipx, uv, pip or checkout install, `<python> -P -m vcharon --update`
  would only print the other tool's command, and the user knows how they installed VCharon.
  `--update`'s own line for a binary, `… --update --yes`, is spelled with `self_command()`.
- `permission` from `EROFS` or `EPERM` gets `fix: if your CLI's sandbox blocked it, ask your
  user to allow vcharon's folders (vcharon doctor); else check the owner and permissions of
  <path>`; `EACCES` keeps only the last part, and so does the helper on a server
  (`fsops.SERVER`), where no agent's sandbox runs. The error code stays `permission`. A failed
  `dirs` check of `doctor` gives the same fix for `EROFS` or `EPERM` (any other error keeps `fix
  its permissions, or set VCHARON_HOME …`), and a `note:` under it still names all four
  folders. Why: an agent CLI's sandbox refuses with these on a folder that is fine (Codex's
  Linux sandbox gives `Read-only file system`), allowing the folders is the user's step, and the
  fix points at the list they need.
- A server's fix line that points at `list` is the client's to word: the helper never knows the
  alias it was reached by, so it sends a plain text (`channels.NO_CHANNEL_FIX` and the two
  beside it), and the client puts `vcharon list --server ALIAS` or `vcharon list --local` in
  its place (`channel_cmd.LIST_FIXES`). Why: `vcharon list` alone doesn't run.
- The tests parse every command-form fix line back with the command line's own parser.

### Running watchers

A watcher and `sync --repeat` run for a long time while the program under them may be replaced
(a `pipx install --force`, a binary swapped by an update). A one-file binary reads its code
archive from its own file, by path, at each first-time import, and holds no descriptor on it:
after a swap, an old process importing a module for the first time reads the new file at the
old offsets and fails (with PyInstaller, a `zlib` error).

- So the long-running commands import every module they can need at start, in every install
  mode: `cli.py` imports every module of the package at its top, the plugins too; the few
  imports inside functions, there to break an import cycle, only look up a module already
  loaded. Standard-library modules loaded at first use are loaded at start too: the codecs
  `pathrules` and `config` use, on Windows `winreg` and the ANSI code page's codec, and the
  sysconfig data that spelling `vcharon` as fix lines do reads. `update.py` is the one module
  imported later, by `--update` alone, which imports nothing after its swap. Tests run a watcher
  and `sync --repeat` in a child and check that no module was imported after the start.
- At the top of each round, before any other work, `watch` and `sync --repeat` compare the
  code's file with the one they started with: a binary's own file, else the package's
  `__init__.py` (pipx and pip rewrite it), by size, mtime and file id. Changed or gone: `EXIT
  updated`, exit 14. `sync --repeat` does it on its own, never relying on its watcher.
- A failed read of the code can show up as any exception, so in a binary an unexpected
  exception in a long-running command checks the file first: changed, `EXIT updated` and 14;
  unchanged, the usual `internal` error.
- The fix for 14 is to start the watcher again: that runs the new VCharon.
- **Orphans.** A one-file binary runs as two processes: the bootloader, and the Python child it
  starts. A signal the bootloader can't catch (SIGKILL; `TerminateProcess`) ends the bootloader
  alone, and on POSIX the child runs on, holding the watcher's lock. So in a binary, `watch` and
  `sync --repeat` note their parent at start and check it at the top of each round, after the
  update check: gone (another parent pid, not only 1, since a subreaper may take the child),
  `EXIT orphaned`, exit 15, through the normal path, which releases the lock. A new code, not
  11: nothing failed, and a restart is right only if the user didn't mean to stop it. Not in a
  Python install, where the command is the process that was signalled. The unpack folder (about
  20 MB) stays: only the bootloader removes it.
- **A Windows binary ends with its bootloader, at once.** Every command of a Windows binary
  opens a `SYNCHRONIZE` handle on its parent at start and waits on it in a daemon thread.
  Signalled, the thread logs, prints `EXIT orphaned` in `watch` and `sync --repeat`, and exits
  15 with `os._exit`, after at most 2 s whatever the printing does; the OS drops the locks.
  While the thread waits, the round's check leaves the exit to it. Why: `TerminateProcess` ends
  the bootloader alone, whoever sends it, Windows has no process group to kill with it, and a
  plain `sync` has no round check. Not in a Python install: a venv or pipx launcher already runs
  `python.exe` in a job that ends with it. Without the handle the round's check stays. The ssh
  under such a process ends at its stdin's end, or after ssh's keepalive gives up. No test runs
  a real Windows binary.
- **Ending a sync child.** A watcher ending its sync child must not kill only the bootloader. On
  POSIX the streaming child runs in a session of its own; one that hasn't ended 10 s after its
  stdin closed gets SIGTERM, which the bootloader passes on, so both end the usual way and the
  unpack folder goes; after 3 s, what is left of its process group gets SIGKILL, sent before the
  child is reaped, so the group's id can't belong to another process yet. `fsops.run`'s
  timeout or Ctrl-C does the same for `--no-stream`'s sync and `--update`'s runs; its other
  callers kill at once. Why: the Python process holds the pipes and the job's locks, so a
  watcher waiting on it would never end and every later round would be busy. Windows has no
  tree kill: `TerminateProcess` ends the bootloader, and the Python process then ends itself as
  the bullet above says.

### Self-update

`vcharon --update [--yes] [--force] [--json] [--rc] [--check]` replaces a standalone binary with
the latest GitHub release. It is the only network call VCharon makes besides ssh, and only when
it is run: no version check at start-up.

- **A flag, not a verb**: the verbs are what agents run, and an agent never updates (the guide
  says to ask the user: it replaces the program every member on the machine runs). Refused, as a
  usage error (3), with a verb or any flag but `--yes`, `--force`, `--json`, `--rc` and
  `--check`; those five are refused without it. `--project P` and `--role R` are taken and
  ignored: an agent passes them on every command.
- **`--check`**: reads the latest as `--update` does, prints the `current` and `latest` lines,
  then whether it is newer (with the command to install it: this binary's `--update [--rc]`, or
  another install kind's own command), then `OK`. It never asks and never installs: exit 0 once
  the release is read, 1 when the read fails. Refused (3) with `--yes` or `--force`. Why: the
  guide forbids agents `--update`; `--check` is the one an agent may run without its user's
  word, and the guide says so.
- **It asks first**: it reads `releases/latest` (GitHub leaves out drafts and pre-releases),
  prints the current and the latest version, and with nothing newer and no `--force` says so and
  exits 0. Else `Update now? [y/N]`. No terminal, `--json`, Ctrl-C or anything but `y`/`yes` is
  "no": it reports and installs nothing, exit 0, so `vcharon --update --json` is the scripted
  check. `--yes` is the one yes; `--force` installs the latest even when it is this version, or
  an older one.
- **Pre-releases**: with `--rc`, or when this build is itself a pre-release, it reads the first
  30 releases GitHub lists, skips drafts and tags that don't parse, and takes the highest
  version, pre-releases included: by version, not list order, which GitHub doesn't document.
  Why the pre-release build: a user testing `0.5.0rc1` follows rc2 and then the final with a
  plain `--update`, and once on the final reads full releases only again, with no state kept.
- **Versions**: numbers, then an optional pre-release label (`dev` < `a` < `b` < `rc`) and its
  number, after a leading `v`; a pre-release sorts below its final, and a post-release (`post`)
  above it. A tag that doesn't parse is never newer.
- **Checked before the question**: how VCharon was installed, that a binary is published for
  this platform (`linux-x64`, `darwin-arm64`, `win-x64`; others get the pipx command), that the
  binary's folder is writable (by making and removing a temp folder in it: on Windows
  `os.access` ignores ACLs), and that the release has the archive. A problem found after a yes
  would be a question that should never have been asked.
- **Install kinds**: a PyInstaller binary (both `sys.frozen` and `sys._MEIPASS`) is replaced in
  place. pipx, uv, pip and a source checkout are refused with the exact command, pinned to the
  release's tag, as the fix line; nothing changes.
- **The swap**, in this order: download the archive into a temp folder inside the binary's own
  folder (one filesystem, so the last rename can't hit `EXDEV`); check the release's
  `<archive>.sha256`, whose first word counts (a mismatch, or a first word that isn't a sha256,
  fails; none published warns and goes on); extract only the binary, never everything (the
  `.tar.gz`'s top-level `vcharon`, or the `.zip`'s entry named exactly `vcharon.exe`, at most
  200 MB, streamed); run the new binary's `--version` and require the release's version; then
  replace. POSIX: `os.replace` over the running binary, which keeps running from its own inode.
  Windows, where a running `.exe` can be renamed but not replaced: rename it to a unique
  `vcharon.exe.old-<unix time>`, move the new one in, and rename the old one back if that fails;
  each rename is retried for about two seconds, since antivirus often holds a new file. Any way
  out of the swap, a Ctrl-C too, renames the old one back once it was moved and nothing is at
  the binary's path; if even that fails, the error's fix names both paths and the temp folder
  holding the new one is kept. Every start of a Windows binary deletes the `.old-*` copies next
  to it that it can. Any other failure leaves the old binary as it was; temp folders a killed
  run left are removed by a later run once an hour old.
- **Network**: https only, also after a redirect; `GITHUB_TOKEN` (or `GH_TOKEN`) is sent to
  `api.github.com` only, and dropped on a redirect to another host; timeouts per socket
  operation (15 s for the API, 120 s for a download). A binary whose OpenSSL can't find its
  build machine's CA file uses the system's bundle. An HTTP 401 to a call that carried a token
  is tried once more without it; if that hits the rate limit, the error is `bad_token`, whose
  fix says to unset or renew the variable. Why: a stale token fails every later try the same
  way, and the release is public.
- **The skill**, after a swap: the agents whose skill copy holds the marker (read before the
  swap) get it rewritten by the new binary, run as `<binary> skill install --claude|--codex`.
  Not by this process: it is the old version, with the old skill text. A failure there doesn't
  fail the update: a `note:` with the command to run, exit 0. Why: an agent reads the skill
  before the guide, and an old skill can name what the new version changed.
- **Running the new binary** (`--version`, then `skill install`): through `fsops.run`, at most
  90 s each, on POSIX in a session of its own with the 3 s grace a `--no-stream` sync gets
  ([Running watchers](#running-watchers)), so a timeout or Ctrl-C ends the bootloader and its
  Python process both and the unpack folder (about 20 MB) goes. Stdin is an empty pipe, and
  neither command prompts.
- **Exit codes**: 0 done, nothing newer, or "no"; 1 every failure, with the `update` error
  code; 3 a usage error.
- **`--json`**: one object on stdout, a failure's too: `current`, `install` (the kind), `path`,
  `rc` (`"flag"` for `--rc`, which wins, `"current"` for a pre-release build, else `false`),
  `update_available` (null until the release is read; then true or false); once the release is
  read `latest`, `tag`, `prerelease`, `url`, `changed`, `confirmed`; with `--check` and another
  install kind whose release is newer, its `command`; `ok`; a failure's `error`
  (`not_self_updatable`, `unsupported_platform`, `not_writable`, `missing_asset`, `no_release`,
  `not_found`, `network`, `rate_limited`, `bad_token`, `checksum_mismatch`, `smoke_failed`,
  `version_mismatch`, `bad_asset`, `install_failed`, …), `message` and `fix`; another install
  kind's `command`; an install's `previous`, `installed`, `verified`, and `skills` (`{"paths",
  "ok", "fix"}`).
- `not_writable`'s fix runs the installer again. A new binary that won't run here
  (`smoke_failed`; on Linux most often a glibc older than the build machine's) and a broken
  release point to pipx; only the failures a later try can fix say to try again. `doctor`
  prints the install kind and a binary's path, so a user knows what `--update` touches.

### Packaging

VCharon ships as a wheel and as a standalone binary per platform, built by PyInstaller from
`vcharon.spec` (the `build` extra pins its version).

- **One file**, console, named `vcharon` (`vcharon.exe` on Windows). A one-file binary unpacks
  itself into a temp folder at each start (a few tenths of a second), so a `--no-stream`
  watcher, which starts a child every round, pays that each round; the streaming child starts
  once.
- **Stripped on Linux**: the spec sets `strip` on Linux, so PyInstaller strips every shared
  library it packs: the Python CI builds with ships them with debug info, which more than
  doubled the binary. A missing `strip` would only be a warning in PyInstaller, so the spec
  stops the build instead. Not on macOS (small already, stripping untested) or Windows.
- **The package's files on disk**: the spec collects every file of the package as data, the
  `.py` files too, under the binary's unpack folder (`sys._MEIPASS/vcharon/`). The PYZ holds
  only bytecode, and the bundle sent to a server is source: `bundle._sources()` reads it from
  `vcharon.__file__`'s folder, which is that folder in a binary. The guide and `SKILL.md` are
  read with `importlib.resources`, which finds them there too.
- **Every module named**: the spec's `hiddenimports` lists every module of the package (but
  the `__main__` ones), since `plugin.py` loads plugins by name and `update.py` is imported only
  by `--update`; the analysis follows neither. The codecs looked up by name at start
  (`utf-16-le`, `utf-8-sig`) come with all of `encodings`, in `base_library.zip`.
- **The bundled Python, said so**: in a binary, `doctor`'s `python` line reads `<version>,
  bundled in <binary>, on <os>`, not `<version> (<executable>)`: the executable is the binary
  itself, and the line must not read as if a Python were installed.
- **Proof in every build**: `doctor` builds the bundle the way a session does and reads it back
  (its `bundle` line; `--json`'s `helper_bundle`: module count, whether `vcharon.helper` is in
  it, size). `tests/smoke.sh DIST WORK` runs a built binary through `--version`, `doctor
  --json` (the bundle has the helper; the install kind is `binary`), `setup --box ci`, `create
  smoke --local`, `post`, `read --json`, `guide`, `skill install` and `close`, everything in
  WORK; `SMOKE_PING=<dest>` adds `ping`, which runs the helper on a real server.
- **Installers**: `install.sh` (Linux, macOS) and `install.ps1` (Windows) read the latest release,
  download the archive and its `.sha256` (a mismatch fails, a file without a sha256 fails, none
  published warns), take only the binary out (`vcharon` from the `.tar.gz`, the entry named exactly
  `vcharon.exe` from the `.zip`, at most 200 MB), run its `--version` and require the release's
  version, then move it in with a rename in the target folder: `~/.local/bin/vcharon`, or
  `%LOCALAPPDATA%\Programs\vcharon\vcharon.exe` (a running one is renamed to `vcharon.exe.old-<unix
  time>` first). `install.sh` prints the line that puts `~/.local/bin` on the PATH when it isn't;
  `install.ps1` adds its folder to the user's PATH. An Intel Mac, Linux arm64 and Windows on ARM
  get the pipx command. https only (redirects too); only their own tests point them at a local
  fake release over plain http to `127.0.0.1` or `localhost` (`VCHARON_INSTALL_API_URL`,
  `VCHARON_INSTALL_DOWNLOAD_URL`, and two variables that shorten the waits). Only a 404 for the
  `.sha256` warns; any other failure to get it fails. `install.sh` writes only the archive
  member's bytes (`tar -O`), enforces the 200 MB cap, fails when it finds no sha256 tool (a
  published checksum is never skipped), gives the version check 60 s, and cleans up on INT,
  TERM and HUP. `install.ps1` stops a binary that overruns the 60 s, retries its two moves, puts
  the old binary back after a stop between them, and keeps its work folder while no binary is in
  place. Both run against a fake release in each release build ([Releases](#releases)).

### Releases

A tag `v<version>` runs `.github/workflows/release.yml`. Pushing the tag publishes, so the
maintainer asks before tagging.

- **The tag must match both version strings**, `VERSION` in `src/vcharon/__init__.py` and
  `version` in `pyproject.toml` (a leading `v` stripped), and CHANGELOG.md must have the
  version's dated heading, `## <version> — YYYY-MM-DD`; anything else stops the release before a
  build, so no release goes out with an undated or missing entry.
- **One build per platform**, each on its own runner, all steps in bash (Git Bash on Windows):
  the unit tests, `pyinstaller vcharon.spec`, `tests/smoke.sh` with the tag's version, then
  packing and the installer check. The Linux row also pings the runner's own sshd from the binary
  and runs `tests/ssh_flow.sh` with it, so the helper bundled in the binary runs on a real server.
  The Linux row's runner is pinned (`ubuntu-24.04`), not `ubuntu-latest`: the binary needs the
  glibc of the image it was built on, so a newer image can raise that floor for every user
  unannounced; moving it is a change of its own, with a CHANGELOG line. CI's jobs stay on
  `ubuntu-latest`, so a new Ubuntu breaks a test there before it reaches a build.
- **Assets**, nine per release, the names `--update`, `install.sh` and `install.ps1` pick. These
  agree on them and change together: `release.yml` and `tests/pack.py`, `install.sh` and
  `install.ps1` (and `tests/install_check.sh`), and `src/vcharon/update.py`:

  | platform | binary | archive (holds the binary and `LICENSE`, at its top) | checksum |
  |---|---|---|---|
  | Linux x64 | `vcharon-linux-x64` | `vcharon-linux-x64.tar.gz`, member `vcharon` | `.tar.gz.sha256` |
  | macOS arm64 | `vcharon-darwin-arm64` | `vcharon-darwin-arm64.tar.gz`, member `vcharon` | `.tar.gz.sha256` |
  | Windows x64 | `vcharon-win-x64.exe` | `vcharon-win-x64.zip`, entry `vcharon.exe` | `.zip.sha256` |

  The checksum covers the archive, which is what the installers and `--update` download. Each
  `.sha256` is one line, `<hex>  <archive name>` with an LF, as `sha256sum` prints it: they read
  its first word.
- **`tests/pack.py`** packs with Python's `tarfile` and `zipfile`, not `tar` and `zip`: the same
  on all three runners (Git Bash may have no `zip`; macOS's `tar` adds `._` files for extended
  attributes), no build machine's owner in the archive, and the zip entry a regular file in its
  Unix mode (`--update` refuses any other type). It then reads the archive back: exactly the
  binary and `LICENSE`, the binary's bytes, executable. The workflow checks the `.sha256` again
  with the system's own `sha256sum` (macOS: `shasum -a 256`).
- **`tests/install_check.sh`** serves the packed archive as a fake release on `127.0.0.1` through
  the installers' test-only URL overrides and runs this OS's installer (`install.sh`, or
  `install.ps1` under `pwsh` on Windows): with a wrong `.sha256`, which must fail and install
  nothing; with the real one, whose installed binary must print the tag's version; then over
  that install, whose binary it first overwrites with a marker, so only a real replace prints
  the version, and after which the folder must hold the binary alone (on Windows: no
  `vcharon.exe.old-*` left). On Windows the install also runs under Windows PowerShell 5.1,
  what `irm | iex` runs in by default. A file left outside the install folder must survive the
  replace runs. `install.ps1` adds its folder to the user's PATH, so on Windows the check runs
  only where `CI` is set.
- **The build's inputs are pinned**, so a release is built from what was checked, not from what
  was newest that day. The actions are at commit SHAs (a tag can be moved; the publish job holds
  a write token), with the version in a comment, which Dependabot bumps weekly as pull requests
  (`.github/dependabot.yml`). Every Python package the build installs is in
  `build-requirements.txt` at an exact version, with the sha256 of each file PyPI has for that
  release: PyInstaller and its dependencies, the per-OS ones under `sys_platform` markers, pip,
  and hatchling with `editables`. The build installs it with `--require-hashes`, then the
  package with `--no-deps --no-build-isolation`, so nothing comes from the index unpinned. One
  lock serves all three runners: hashes cover every file of a release, so each OS's pip finds
  its own wheel. The `build` extra pins PyInstaller and its hooks package to the same versions
  (`tests/test_build_lock.py`, which checks the lock's shape only), so a hand build matches. No
  ruff: the release runs no lint.
- **One job publishes**, after all three builds: it takes their assets, checks there are exactly
  the nine and that each checksum holds, and runs `gh release create`. The release notes are
  the version's CHANGELOG entry without its heading, then a line pointing at the README's install
  section and the full CHANGELOG: the notes say what changed and what was checked, and the
  entry is already written and checked by the tag test above. A version with a
  pre-release label (`rc`, `a`, `b`, `dev`, by `--update`'s own `parse_version`) is published as
  a GitHub pre-release: `releases/latest` leaves those out, so the installers and a plain
  `--update` on a final build never offer a release candidate; `--update --rc` does.
- `*.sh` files are LF on every checkout (`.gitattributes`), so a script is the same bytes on
  every OS: a Windows checkout turns text to CRLF, and a shell other than Git Bash refuses a CR.

## Stable

Agents parse VCharon's output and scripts call its flags, so these are a contract:

- **Verbs and flags**: the command line above, with each flag's meaning.
- **Exit codes**: 0, 1, 2, 3, 4, 130 and 143 ([Exit codes](#exit-codes)); the watcher's 0 (change),
  10 (quiet), 11 (error), 12 (another watcher, or a create, join, leave or close of the
  member, runs), 13 (closed), 14 (updated), 15 (orphaned), 16 (nothing new) and 17
  (stopped).
- **The watcher's lines**, each after a `YYYY-mm-dd HH:MM:SS ` time (a `<dir>` or `<path>`
  that is absolute shows a path under the home as `~/…`, `~\…` on Windows: expand a leading `~`
  where you read one out):
  - `watching <dir>, <n> files in other folders[, since <time> | , fresh start][, syncing
    every <n> s]`
  - `to you: <id> — <title>  (<path>)`, `to all: <id> — <title>  (<path>)`, and after the
    leader's `CLOSED` to `@all`, its `HANDOFF` to the member, or its `LEADER`, `  next: <text>`,
    its commands to run as printed
  - with `--presence`, in a lobby: `presence: <id> — <JOIN|REJOIN|LEAVE>  (<path>)`
  - `<n> other entries (<folders>)` (`1 other entry (<folder>)` for one), `note: @all from
    <folders>, not the leader: ignored`, `note: ignoring the saved snapshot <path>: <why>`,
    `note: duplicate entry <id> in <path>: the one in <file> stands`, `note: your vcharon
    skill at <path> is from another version: <fix>` (`skills at <path> and <path> are` for
    two; at start only)
  - `new <path>`, `changed <path>`, `gone <path>`
  - without `--until-change` or `--once`, the other entries and file lines come just before the
    next line, merged over the rounds they were held, with that line's time: their shapes are as
    above
  - `WARN <text>`, `WARN cleared: <text>`, `WARN entry <id> was edited`, `WARN entry <id> in
    <folder>/: not its folder's`
  - `ERROR <text>`, `  fix: <text>`, `  log: <path>`, `ok again`
  - `EXIT change`, `EXIT quiet <n> min`, `EXIT error`, `EXIT closed`, `EXIT updated`, `EXIT
    orphaned`, `EXIT nothing new`, `EXIT stopped`, `EXIT interrupted`, each followed by
    ` (exit <code>)` (match
    on the `EXIT <kind>` start); `ERROR another watcher is running on this mailbox (<lock>), or a
    create, join, leave or close of this member` with exit 12, followed by ` (PID <n>)` when the
    holder wrote its PID (`<lock>` shows a path under the home as `~/…`, `~\…` on Windows: match
    on the start up to `mailbox (`, and allow or expand a leading `~` where you read the path).
    The `  fix:` line after it is not stable.
    The text after an `ERROR` line's colon is the OS's message and may be translated: match on
    the prefix.
- **`--json` fields**:
  - `list`: `{"server", "channels", "others", "shared"}` (`shared`: the shared root's
    `{"channels", "others"}`, null on a server that isn't Linux, `{"error": {"code",
    "message", "fix"}}` when its folder fails the checks); each channel `{"name",
    "kind", "leader", "term", "leaders", "founder", "members", "member_info", "newest",
    "created", "bytes",
    "strays", "format", "limits"}` (`leader` and `term` by the charters' terms, null for none
    or a tie; `leaders` every folder holding a `CHANNEL.md`, a former leader's too), `member_info` each `{"name", "box", "os", "agent", "project", "idle_seconds"}`,
    `limits` `{"max_mb", "max_files", "max_entry_kb"}`; each of `others` `{"name", "why"}`.
  - `list --remove-idle`: `{"server", "days", "yes", "root", "members", "idle_channels",
    "shared"}` (`root`: `home` or `shared` for that flag, `both` for neither; `shared`: the shared root's `{"members",
    "idle_channels"}`, null, or `{"error"}` as in `list`; with `--shared`, `members` and
    `idle_channels` are null, with `--home`, `shared` is), each member
    `{"channel", "name", "idle_seconds", "action", "why", "deleted"}`: `action` `remove` or
    `keep` without `--yes`, `removed`, `kept` or `failed` with it; `why` the reason's key
    (`charter`, `own`, `items`, `budget`, `active`, `gone`, `not_member`), a failure's
    `<code>: <message>`, or null; `deleted` the count deleted, else null. `idle_channels` names
    the whole idle work channels ([Removing idle members](#removing-idle-members)).
  - `whoami C`: `{"channel", "name", "project", "role", "leader", "term", "founder", "leads",
    "mode", "server", "shared", "folder", "tree", "box", "box_source", "members", "kind"}`
    (`shared` true for a channel in the server's shared root; `leader`,
    `term` and `leads` by the charters' terms in the tree on this box), each member
    `{"name", "agent", "box", "os", "leader", "newest", "watched", "watch_every", "left"}`,
    plus `"presence"` in a lobby. `founder` is in `list`'s channels too: in a lobby the
    founder's name, with the top-level `leader` null (`list`'s `leaders` empty), `leads` false
    and each member's `leader` false; in a work channel null. `kind` is `"work"` or `"lobby"`,
    in `list` too (null there for a channel whose format, kind or limits this vcharon refuses).
    `whoami` without C: `{"box", "box_source", "project", "role", "name", "channels"}`, each
    channel as with C without `box`, `box_source` and `members`.
  - `read`: `{"channel", "leader", "term", "folder", "synced", "members", "member_info",
    "count", "entries", "notes", "missing"}` (`leader` and `term` null in a lobby); each of `member_info` `{"name", "box", "os", "agent", "project",
    "vcharon", "watched", "watch_every", "left"}`; each entry `{"time", "id", "name",
    "number", "to", "re", "title", "file", "header", "body"}`; `count` counts every entry in the
    channel, the same as the text's first line; IDs, `--last` and `--to-me` narrow only
    `entries`.
  - `doctor`: `{"version", "protocol", "format", "python", "executable", "os", "command", "install",
    "helper_bundle", "box", "box_source", "claimer_source", "dirs", "servers", "ok", "failed",
    "warnings", "checks"}`; `install` `{"kind", "path"}`; `helper_bundle` `{"modules", "has_helper",
    "bytes"}` (`{"modules": 0, "has_helper": false, "bytes": null}` when it couldn't be built);
    `dirs` `{"state", "logs", "joined", "channels"}`; each server `{"server", "python", "os",
    "distro", "distro_id", "distro_version", "tested"}`; each check `{"level", "subject", "text",
    "fix", "note"}`.
  - `--update --json`: the fields in [Self-update](#self-update).

  A field may be added in a minor version; none is removed or changes meaning without a
  CHANGELOG line.
- **The entry header**: `## <time> — <name>#<n> — <title>`, `<time>` as `YYYY-mm-dd HH:MM:SS`
  (a time without seconds is a bad time to `read`), then `to:`, `re:`, and other `key: value`
  lines up to the first blank line ([Entries](#entries)).
- **The channel files**: the layout ([Layout](#layout)); `MEMBER.md`'s #1 fields, a charter's
  fields, `term:` and `after:` included ([Formats](#formats)); `STEPS.md` (the leader's) and `RESULTS.md`; a lobby's
  `chat-YYYY-MM-DD.md` day files; any `.md` file of a member's folder holds entries.
- **A lobby's `whoami` lines**: `here`, `away`, `left` or `gone` on each member's line, the
  channel line `channel lobby (a lobby, founded by <founder>)`, and `+<n> not seen in 24 h
  (--all)`.
- **The `fix:` line form**: `fix: <runnable command>` or `fix: <one line of text>`, the last line
  of every refusal.
- **`vcharon --version`** prints the bare version and nothing else: the installers, `--update`
  and the smoke test require exactly the release's version from a new binary.
- **The config keys** ([Config](#config), [Channel sections](#channel-sections)).

Not stable: other human-facing lines (a sync's summary, `doctor`'s text, `list`'s layout), the
log format, the state, snapshot and record files, and the wire protocol (both ends always run
one version). A record change still gets a CHANGELOG line on how to adapt.

Versioning is semver. Before 1.0, a minor version may break one of these, and the CHANGELOG says
which and how to adapt. A channel format change is always a minor version at least.

## Tests

- From the repo root: `python -m unittest discover -s tests -t .` (standard library only; `-t .`
  so `tests/__init__.py`'s sandbox loads for every module).
- `tests/__init__.py` points HOME and the OS's config and state folders into a temp folder for the
  whole suite, in `os.environ` itself, so no test can undo it, and every child inherits it; the
  functions that name VCharon's folders fail on a path under the real home. Why: a test that
  loses its own environment must not reach the user's real config and channels. So a test never
  calls `mock.patch.stopall()`. It drops the shell's `VCHARON_HOME` and `VCHARON_CHANNELS_ROOT`
  too (a developer exports them for hand runs). Nothing turns the sandbox off; the real-ssh
  tests don't need it off, since OpenSSH reads `~/.ssh` from the account's home, not `$HOME`.
- The controller takes the ssh command as an argument list, so tests replace `[<ssh_path>]` with
  `[sys.executable, "tests/fake_ssh.py"]` on every OS. The fake ssh runs the bootstrap with
  `sys.executable -I -c`, relays stdin and stdout as ssh does, sets HOME and the test machine
  id, and can print junk, read stdin, exit with a chosen code or stall, from `FAKE_SSH_*`
  variables. Everything after it is the real code. `tests/fake_ssh_add.py` does the same for
  `ssh-add`.
- Test seams in the helper's environment: `VCHARON_TEST_MACHINE_ID`, `VCHARON_TEST_OS`,
  `VCHARON_TEST_OS_RELEASE`, `VCHARON_TEST_CLOCK_SHIFT`, `VCHARON_TEST_UTC_OFFSET`, and
  `VCHARON_CHANNELS_ROOT` (`VCHARON_TEST_CLOCK_SHIFT` moves the idle times' clock too: a
  folder's change times can't be set back). The machine id is the fake server's: a test of
  another server sets it, and a test of another client machine patches `platform.client_id`,
  since this box's client id is kept in a file once made.
- Real ssh, optional: with `VCHARON_TEST_SSH=<dest>` set, the session tests run against a real
  server, in a temp folder under its home. The helper's cache still goes to that user's real
  `~/.local/state/vcharon/cache/` (ssh passes no test variable), at most 8 files, all safe to
  delete.
- Path rules are pure functions: every OS's rules run on every OS. Windows-only and POSIX-only
  tests are skipped elsewhere.
- A test that declares another OS (patches `platform.os_name`) gets that OS's path rules
  (`platform._path`) but keeps the host's temp folders: where what it checks depends on such a
  path, it patches the OS only around the call it is about, or gives that OS's own paths. Why: a
  host path read by another OS's rules isn't absolute there (`C:\…` under `"linux"`) and joins
  with the wrong separator.
- A test fakes what each OS's code path uses (`install.Parent`, not only `os.getppid`): a fake
  the other OS never calls leaves its path running for real. A fake sleep that waits for a faked
  event raises after a few rounds, so such a miss fails the test instead of hanging the job.
- A fake stream is an `io.StringIO`, or a mock whose `fileno` raises `io.UnsupportedOperation`
  and whose `isatty` returns False: Python 3.14's argparse calls `os.isatty(stream.fileno())`.
- A check that no path is shown from the home looks for `~` and a separator at a path's start,
  never for any `~`. Why: a Windows temp path can hold an 8.3 short name such as `RUNNER~1`.
- The sandbox's `VCHARON_SHARED_ROOT` is one folder for the whole run: a test that reads the
  shared root's state points it at a temp folder of its own (`SharedCase`, `DoctorCase`).
- A test that expects a path the code builds from the current folder takes `os.getcwd()` after
  its `chdir`, never the path it changed to. Why: macOS's getcwd gives `/private/var/…` for a
  temp folder made as `/var/…`.
- Guards on the repo itself: the guide's commands and flags parse with the command line's own
  parser; `docs/GUIDE.md` matches what `guide/*.md` makes; no milestone or review tag in the
  code, tests, README or DESIGN; the old tool's name nowhere but the CHANGELOG line that says
  where VCharon came from; every reference by name to a section of DESIGN.md or README.md names
  a heading that exists, and none goes by a section number; every `vcharon <verb>` string
  constant in `src` is listed in `tests/test_commands.py`'s `HINTS`.
- CI runs the suite on Linux, macOS and Windows, each with Python 3.13 and 3.14, and `ruff check src
  tests` on one row.
- A push or pull request that changes only docs (top-level `*.md`, `docs/`, `LICENSE`, the issue
  templates) skips CI and runs `docs.yml` instead: `tests.test_guide` and `tests.test_old_name`
  on one Linux row, the guards that read those files. Its paths and `ci.yml`'s `paths-ignore`
  are the same list. A release tag still runs the whole suite.
- CI's `ssh` job (Linux) makes the runner its own ssh server (`tests/ci_sshd.sh`, with
  setup-python's 3.13 as the login's `python3`, since the runner's own is below the floor) and
  runs a real channel over it: `tests/ssh_flow.sh localhost WORK` has two remote members from
  two `VCHARON_HOME`s create, join, post, watch, read, leave and close; then the real-ssh unit
  tests (`VCHARON_TEST_SSH=localhost`). The channel is made in the ssh user's real channel root,
  since the server side reads no test variable over ssh, so `ssh_flow.sh` runs only where `CI`
  is set (or `SSH_FLOW_REAL_HOME=1`) and refuses a root that already holds its channel, and
  `ci_sshd.sh` only on GitHub Actions.
- `install.sh` runs under `sh` against a fake release on a local HTTP server, with `uname`
  faked for each platform (POSIX only). A built binary is checked by `tests/smoke.sh`
  ([Packaging](#packaging)), and its packed assets by `tests/pack.py` and
  `tests/install_check.sh` ([Releases](#releases)).

## Rules for the code

- Standard library only, Python 3.13 or later. `ruff check src tests` clean, with the ignores in
  `pyproject.toml`, each with its reason there.
- Never build a shell command string on the client: argument lists only; the bootstrap line is
  the only shell text.
- Run other programs only through `fsops.run`: an argument list, never `shell=True`, stdin an
  empty pipe closed at once (not the null device), output captured, a timeout. Data goes in
  arguments or environment variables, never into script text. The exceptions: `vcharon key`'s
  `ssh-add`, which runs on your terminal (`fsops.run_terminal`) so it can ask for the
  passphrase; the session's own ssh; and the streaming watcher's `vcharon sync --repeat` child
  (`watch._spawn`), whose rounds are read line by line while it runs, which `fsops.run` (output
  only once the program ends) can't give. It still gets a pipe as stdin, an argument list, a
  new session on POSIX and a bounded end ([Running watchers](#running-watchers)).
- In the helper, read stdin only through `sys.stdin.buffer` and write frames only to the private
  protocol fd. Never use `__file__` in a module that runs on the server; list plugins statically.
- Resolve relative remote paths against `ctx.home`, never the process's current folder.
- Never write inside a sink's root except through `stage.Stager`; on POSIX touch the target tree
  only through directory file descriptors. The exceptions are the channels': `channels.py`'s
  calls on the channel root (claim, release, `close`'s rename and no-link delete), and
  `entries.py`'s locked writes into a member's own folder (`MEMBER.md`, `CHANNEL.md`, posts).
- Never read a passphrase or password in VCharon's code.
- Stream file bytes in chunks; never read a whole file into memory (an entry file, bounded by its
  limit, is the exception).
- Every error the user sees has a code ([Error codes](#error-codes)) and a `fix:` line.
- No host name, path, user name or raw machine id in any file written to a channel.
- A machine id reaches a line VCharon prints or logs only through `platform.shown_machine` (a
  hello, a reply, a saved identity or target through `platform.shown_doc`, at any depth): the
  first 12 hex digits of `sha256("vcharon shown machine:" + id)`. Why: machine-id(5) asks
  that the id be kept private, and agents paste these lines into entries; one hash everywhere
  keeps two outputs of one
  machine comparable. Its prefix is neither the client id's `vcharon:` nor a claimer's `<C>:`
  (a channel's name holds no space), so a shown id can't be matched against a `claimer:`.
  What is stored and compared (records, state, the hello) keeps the id itself.
- Every human-readable line prints a path under the home folder with the home as `~` (`~/…`,
  `~\…` on Windows): agents quote these lines into channel entries, and the full path names
  the user. Display only:
  - A path a line names goes through `platform.tilde`; text made from paths in many places
    through `platform.tilde_text` where it is printed (an error's message and `  | ` lines,
    `doctor`'s check lines). A `fix:` line that names a path in prose is made with
    `platform.prose_hint` (`proto.Hint`), so only the printers show the `~`: never `tilde_text`
    on a whole fix line, which would reach its commands.
  - Kept whole: a command in any line (it runs as printed, with its paths); `doctor`'s `config`
    file, `dirs` lines and folders note, which a user pastes into a sandbox allow-list where `~`
    may not expand; a `--json` field; a line only logged; what a program reads back (state,
    records, `MEMBER.md`, sections); a quoted path whose quoting changed the home's text; a
    server's `<ssh>:<path>`.
  - A path under the server's home shows that home as `server:~` (the word, never the host name
    or alias), because a bare `~` reads as the client's home: the helper applies
    `platform.server_tilde_text` to the message of every error it sends, and `doctor` to its
    checks from the server, before the client's own tilde. The error's hint goes as it is, so
    a server's fix line names its paths whole: the wire doesn't mark prose paths apart from a
    command's arguments.
- Long-running commands import every module at start ([Running watchers](#running-watchers)).
- Comment the reason for anything that isn't obvious: why the code does this, never which piece
  of work or which review it came from.
- When a design decision changes, update this file in the same commit, in place.

### Line endings

- Every file VCharon makes is LF: a new entry file, `MEMBER.md`, `CHANNEL.md`, a record, a
  section, a new `vcharon.ini`. An appended entry is LF too, its body included: a posted body's
  `\r\n` and lone `\r` become `\n`.
- An in-place edit keeps the file's own line ends: `setup --box` keeps a CRLF `vcharon.ini`
  CRLF, and the `claimer:` or `agent:` rewrite of `MEMBER.md`'s #1 ends its line as the heading
  line ends.
- Readers take both: entries split on `\n` and drop a trailing `\r`. Synced files are copied
  byte for byte, never converted.
- The helper's sources are bundled with LF line ends (`\r\n` becomes `\n`; a lone `\r` stays):
  a Windows checkout or build has CRLF, and this way every OS's client sends the same bundle,
  so a server keeps one cached copy per version ([Start-up](#start-up)).
- Test fixtures that need exact bytes are written with `newline=""`: Windows' text mode turns
  `\n` into `\r\n`.

## Not in this version

Later:

- A shared root closed to some accounts (a Unix group's): the shared root is one folder per
  Linux machine, open to every account on it ([The shared root](#the-shared-root)).
- Many streaming watchers on one server: each holds an ssh connection and a helper of about
  16 to 25 MB of the server's memory, and many started at once get connections reset at start
  (each retries); the measurements are in the CHANGELOG.

Not planned: Intel Mac and Linux arm64 binaries, a Homebrew tap or a winget package (pipx or
uv covers them); support for server distros other than Debian (doctor warns and goes on);
a shared root on macOS or Windows (a server is Linux already: [Scope](#scope)); two-way merge;
the server connecting back; a background service; VCharon's own ssh code; a
`command=`-restricted key, which can't restrict anything here ([Keys without
prompts](#keys-without-prompts)).
