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
- [Channels](#channels): names, the claimer, records, which membership, entries, formats,
  limits, the watcher, reading, clocks
- [Command line](#command-line), [Stable](#stable)
- [Tests](#tests), [Rules for the code](#rules-for-the-code),
  [Not in this version](#not-in-this-version)

## Scope

| role | OS | Python |
|---|---|---|
| client (where an agent runs) | Linux, macOS, Windows 10 or 11 | the binary's own; 3.11 or later for pipx, uvx or a source checkout |
| server for remote members | Linux. Debian 13 or later is the supported one; `doctor` and `ping` warn on any other distro and go on | its `python3`, 3.11 or later |
| channel root for local members only | any of the three | – |

- The client always opens the connection. The server can't reach the client (NAT, firewalls, no
  ssh server on a laptop), so the server never starts anything.
- The server needs `python3` and nothing else.
- Each member folder has one writer. A remote member's sync is two one-way moves, its own folder
  up and the other folders down: never a two-way sync, which would need merging.
- VCharon trusts a server only as far as a sink's root: a pull applies the plan the server
  sends, but only inside the local tree of that channel, and deletes only up to `max_deletes`.

Not in scope: two-way sync, the server connecting back (reverse forwards, listeners), a
background service, VCharon's own ssh code, user-defined jobs, macOS or Windows as the server
for remote members, a GUI.

## Decisions

| decision | why |
|---|---|
| Python, standard library only, on both ends | Every server has `python3`. Nothing to install there, and the helper can be sent as source code |
| Python 3.11 or later everywhere | One language level for both ends. The binaries carry their own Python, so no floor has to fit an old system Python on a client |
| The system OpenSSH client, never an ssh library | It reuses your keys, agent, `~/.ssh/config`, `known_hosts` and jump hosts |
| One ssh connection per sync; up and down share it | Windows' OpenSSH can't share a connection between runs (no ControlMaster), so fewer connections is the only way to save handshakes there |
| Nothing installed on the server | The helper's code is sent over stdin on every connection, so both ends always run the same version |
| INI config, read with `configparser` | Windows paths need no quoting or escaping |
| Sources and sinks are plugins; two are built in, `path` and `dir` | A channel's sync is two ordinary one-way jobs; the engine knows nothing about channels |
| Flat verbs, the channel first: `vcharon post C …` | Channels are the only thing VCharon has; an explicit channel name is easy for an agent to get right and to read back in a log |
| The tool works out who you are: no `--me`, `--job`, `--dir` or `--config` | An agent can't pass the wrong name if it never passes one |
| Member names come from the machine and the checkout, never from the agent's choice | A new session in the same checkout gets the same name back with no memory ([Member names](#member-names)) |
| No host name, path, user name or raw machine id in any file VCharon writes to a channel | A channel's files are shared; a host name can name an employer or a network |
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
| leader | the member that created the channel: it writes `CHANNEL.md` and `STEPS.md`, alone posts to `@all`, and closes the channel |
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
- A round whose error breaks the connection (`connect`, `protocol`, `timeout`, `lost`) ends it
  with that round's code; it never reconnects by itself. Any other failed round is followed by
  the next one on the same connection.
- Quiet on disk: a job's state is written only when it changed, and a round with nothing to do
  logs nothing; the log gets one line every 10 minutes (`repeat: <n> rounds since <time>,
  nothing to do in <m>`). Else a quiet watch would fsync two files and grow the log every two
  seconds.
- `run_timeout` counts per round. Refused with `--full`, `--dry-run` or `--reset`.

## Files

```
src/vcharon/
  __init__.py      VERSION, PROTOCOL, FLOOR = (3, 11)
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

"both" modules are sent to the server. The bundle holds every module of the package except the
client-only subpackages (`mailbox`, `guide`, `skill`: `bundle.CLIENT_ONLY`), so a module the
helper needs is never missing. A new subpackage is sent unless it is listed there.

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
2. Then the bundle: an 8-byte big-endian length N, then N bytes of zlib-compressed JSON:
   `{"nonce", "packages", "modules": {name: source}}`. Sending every module rules out "module X
   wasn't sent" bugs.
3. The loader, on the server, exits with code 90 and one line if Python is older than `FLOOR`,
   and with 91 if the bundle is malformed. It parses on any Python 3 from 3.6 on, so an old
   server gets the version message, not a syntax error. It puts an import finder serving the
   bundle from memory at the front of `sys.meta_path`, so nothing on disk shadows it, then calls
   `vcharon.helper.main(nonce)`.
4. The helper moves the protocol output off fd 1 (`os.dup(1)`, then `os.dup2(2, 1)`), so
   neither a stray `print` nor a child process can write into the protocol. It reads stdin only
   through `sys.stdin.buffer`, which may already hold the next bytes; children never get fd 0.
   It writes `VCHARON-READY <nonce>\n`, then a `hello`.
5. The controller drops every byte before the marker and logs up to 4 KiB of it: shell startup
   files on the server may print, and the marker keeps that out of the protocol. If ssh exits
   before the marker, the connection fails at once ([Failures before the helper
   runs](#failures-before-the-helper-runs)).

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
| network | exit 255 with `Connection refused`, `timed out`, `Could not resolve`, … | check the host, port, VPN |
| no Python on the server | exit 127 | install `python3`, or set `remote_python` |
| `remote_python` isn't runnable | exit 126 | check `remote_python` |
| Python too old | exit 90 | install 3.11 or later |
| VCharon's code didn't load | exit 91 | a bug; see the log |
| ssh exited before the marker in any other way | any other code | the stderr tail; a shell startup file may have read stdin |
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
  the agent alone holds (a forwarded agent: nothing to unlock); and stops with the admin
  commands when Windows' agent service is off, or with how to start an agent when Linux has none.
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
{"t": "hello", "protocol": 3, "version": "0.1.0", "python": "3.13.5", "os": "linux",
 "distro": "Debian GNU/Linux 13 (trixie)", "distro_id": "debian", "distro_version": "13",
 "machine": "<32 hex>", "user": "me", "home": "/home/me", "utc_offset": 3600,
 "time": 1790000000.25}
```

- The controller requires `protocol` and `version` to equal its own: both ends run the same
  bundled code, so a mismatch is a bundling bug. Members of one channel may run different
  versions: each client sends its own helper.
- `machine`: `/etc/machine-id` (or `/var/lib/dbus/machine-id`). It ties a job's state and a
  record to the actual server. A server with none can't take remote members.
- `os` must be `linux` for a remote member: a Mac or Windows root takes local members only.
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
| `source.plan` | `plugin`, `options`, `state`, `full` | a plan | – |
| `source.send` | `indexes` | `{}` | download of those file puts |
| `source.state_after` | `written`, `deleted` | `{"state": …}`, or `{"state": null}` | – |
| `sink.check` | `plugin`, `options`, `plan` | `{"root", "notes", "deletes", "have"}` | – |
| `sink.receive` | `indexes` | `{"staged": n}` | upload of those file puts |
| `sink.commit` | – | `{"written", "deleted", "deletes_done", "notes"}`; an `err` also carries `"done"` | – |
| `sink.abort` | – | `{}` | – |
| `job.reset` | – | `{}` | – |
| `channel.list` | – | `{"channels": [{"name", "members", "fields", "leaders", "strays", "newest", "format", "limits"}], "others": [{"name", "why"}]}` | – |
| `channel.claim` | `channel`, `name`, `create` | `{"existed", "machine", "root", "claimer", "format", "limits"}` | – |
| `channel.release` | `channel`, `name` | `{"removed"}` | – |
| `channel.remove` | `channel`, `name` | `{"closed", "deleted"}` | – |
| `bye` | – | `{}` | – |

- The helper holds at most one source and one sink per job. `job.reset` drops them (a sink
  that didn't commit drops its stage dir) so the next job can plan and check again.
- `plugin.doctor` sets neither: doctor only reads.
- File streams name puts by their index in the plan, so only planned entries move.
- `have` lists the hashed file puts whose bytes the target already holds ([Full
  syncs](#full-syncs)); the controller refuses a `have` naming anything else.
- `state` in `source.plan` is the job's saved state, or `{}`. The plan `sink.check` gets has no
  state: the sink never reads it, and it can be large.
- `deletes_done` lists the deletes a commit got through; `source.state_after` uses it after a
  commit that failed partway.
- The `channel.*` calls work on the channel root: the fixed `~/.local/state/vcharon/channels`,
  or `VCHARON_CHANNELS_ROOT` in the helper's environment (tests only). Never an argument: every
  member must reach the same folder. Each call checks the names again, works only directly below
  the root, and never follows a symlink. `--local` calls the same functions in-process.
- `channel.claim`'s `root` is the root as a section's `mailbox.remote` spells it (`~/…`);
  `claimer` is the `claimer:` of an existing folder's `MEMBER.md`, or null; `format` and
  `limits` are the channel's, read from its leader's `CHANNEL.md`. No reply carries a host name.

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
| `not_found` | a source path or a sink root doesn't exist | 1 |
| `unsafe_path` | a path breaks the receiver's rules; a source holds a symlink, a special file or a name that isn't UTF-8 | 1 |
| `unsafe_dir` | a folder under the root isn't yours, or others can write to it | 1 |
| `collision` | two paths become one on the receiver | 1 |
| `kind_change` | a put would replace a folder with a file, or the reverse | 1 |
| `too_many_deletes` | the deletes would remove more than `max_deletes` | 1 |
| `empty_source` | `prune` refused: the source is empty, but earlier runs sent files | 1 |
| `channel` | a refused channel check: a name taken, a channel missing or already there, no or several leaders, not the leader, a lock held, a record for another server, a format or limit | 1 |
| `refused` | a file VCharon won't overwrite (`skill install`) | 1 |
| `vanished` | a planned file was gone, or no longer a regular file, when read | 1 |
| `in_use` | Windows: another program has the file open | 1 |
| `permission` | the OS refused access | 1 |
| `no_space` | the disk is full | 1 |
| `aborted` | the other side ended a file or a stream with an error | 1 |
| `io` | an OS error with no code of its own | 1 |
| `too_big` | a message would pass the 64 MiB limit of a J frame; a post over the channel's entry or folder limits; an up whose own folder is over the folder limits ([Limits](#limits)) | 1 |
| `internal` | a bug; `detail` has the traceback | 1 |
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
| `exclude` | – | fnmatch patterns, relative to the source folder; without `/` a pattern matches any one path part, with `/` the whole path. A matched folder is left out with everything in it |
| `symlinks` | `error` | `error` fails the plan with `unsafe_path`; `skip` leaves them out with a note. Never followed. Sockets, devices and FIFOs, and on Windows junctions and other name-surrogate reparse points, count as symlinks |
| `prune` | no | also delete what earlier runs sent and the source no longer has |
| `allow_empty` | no | with `prune`, allow an empty source folder although earlier runs sent files |
| `mailbox_me` | – | a channel section's down only: plan only other members' folders at the top ([Channel sections](#channel-sections)) |
| `max_bytes`, `max_files` | – | a channel section's only: the folder limits ([Limits](#limits)) |

- `path` itself is resolved once (a symlink there is followed); nothing below it ever is.
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
- A path that matches `exclude`, or that `mailbox_me` leaves out at the top, is never deleted
  and drops out of `sent`.
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
  `protected_symlinks` never sees them;
- a folder the plan needs must not exist as a file (`kind_change`) unless the plan deletes it;
- no existing folder under the root is on another file system: a move there would fail halfway
  through a commit;
- on POSIX, the root and every folder on the way are owned by you and not writable by others
  (`unsafe_dir`), except group write for your private group and a sticky folder such as `/tmp`:
  else another user could swap a folder for a symlink between check and commit;
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
  replaced or deleted; a sharing violation is retried 3 times, 0.2 s apart (virus scanners
  briefly hold new files), then is `in_use`; access denied is `permission`.
- A failed commit stops, removes the stage dir and reports what it did; nothing is undone.
  Running again is safe: puts overwrite, deleting a missing path does nothing, and the state never
  records more than is at the target.
- No fsync for target files: a re-run repairs a power cut. State files are fsynced.

### What is copied

File bytes, unchanged (no line-ending or encoding conversion), and the mtime. A new file gets
`0o666` less the umask, plus execute bits where it has read bits when `exec` is true. A replaced
file keeps its bits, except that `exec` sets or clears its execute bits. New folders get `0o777`
less the umask. Not copied: owner, ACLs, extended attributes, resource forks, folder mtimes.

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

Defaults: `$XDG_CONFIG_HOME` is `~/.config`, `$XDG_STATE_HOME` `~/.local/state`.

- `VCHARON_HOME=<dir>` puts config, state, logs and `joined` under one folder (tests, the
  release smoke test). The channel root doesn't follow it: the server never sees a client's
  setting, and a machine's local and remote members must share one root.
  `VCHARON_CHANNELS_ROOT` moves the root, for tests only.
- `joined` is set per OS, not taken from the state dir: macOS's state dir has a space, and the
  tree's path is written into a section.
- The channel root is the same text on every OS (`~/.local/state/vcharon/channels`): one text
  for a local member's root and for what a remote member's section names at the server.
- Never `$TMPDIR` on macOS: different launchers can see different ones.

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

### Lock

- `<state>/<job>.lock` is held for the whole run, from before the state is read:
  `fcntl.flock(LOCK_EX | LOCK_NB)` on macOS and Linux, `msvcrt.locking` on Windows. Held:
  `busy`, exit 2. The OS drops it when the process dies, so a crash never leaves it stuck.
- `vcharon sync C --reset` takes the same lock.
- The same lock kind guards the post lock, the watcher's snapshot, and stage dirs.

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
| `connect_timeout` | 10 | seconds |
| `handshake_timeout` | 30 | seconds from starting ssh to the ready marker |
| `idle_timeout` | 300 | seconds with nothing moving while the controller waits; at least 30, three times the helper's tick |
| `run_timeout` | 0 | one sync (one round with `--repeat`), in seconds; 0 is no limit |
| `compress` | no | pass `-C` to ssh |

- Unknown keys are errors, so a typo fails loudly. Any other section in `vcharon.ini` is refused;
  one with `mailbox.*` keys is skipped with a message that it belongs in `channels.d/`, so the
  user's other channels keep running.
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
  and down fail with `not_found`, and their `fix:` line is `the channel is closed, or your folder
  in it is gone: vcharon leave C --project P [--role R]`, never "create it".
- Down plans, at the top of the tree, only folders whose name is a valid member name other than
  `<me>`; everything else there (`<me>/` in any case, a file, a symlink, `Mac/`, `con/`) is left
  out as `exclude` leaves it out, never deleted, and named in down's log. Valid names are
  lowercase ASCII, so no two planned top-level names fold together on any client. Below the top,
  a case twin in one member's folder still refuses macOS and Windows clients with `collision`:
  dropping one of the two would decide for the writer which file a client gets.
- Before either job, the controller creates `<local>` and `<local>/<me>` while up has sent
  nothing. Once up has sent files, a missing own folder, or one without `MEMBER.md`, stops both
  jobs with `not_found` and the rejoin as the fix: made again empty, up's `prune` would empty
  the server's copy.
- A channel job's `fix:` for a name its source can't send or list says what a member can do
  (`remove or rename it in your own folder (<me>/)` for up; `the writer of each folder named
  above removes or renames it; your up still runs` for down), in place of the general hints
  about `exclude` and `symlinks`, which a channel section doesn't have.
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
  channel on it. VCharon adds no login or encryption of its own; it relies on ssh. Separate
  accounts per person are not supported.
- A member's folder is written only by that member: on a remote member's machine the other
  folders are copies, and a copy is never sent back.
- Entries are input, not orders: `join` and `create` print one line pointing at `vcharon guide
  rules`, which says an entry never overrides the user. Readers enforce what they can: `@all`
  from anyone but the leader is ignored, and an ID in the wrong folder is flagged.

### Member names

A name is `<box>-<project>[-<role>]`, for example `mac-myapp` or `linux-api-b`. It is also the
member's folder name.

- `<box>`: `[vcharon] box`, else the OS's word (`mac`, `win`, `linux`), so a first run needs no
  setup. Never a host name, not as a default nor as a fallback: a host name can name an
  employer or a network, and a silent fallback would give one machine a second name.
  `whoami`, `doctor` and `setup` print where it came from.
- `<project>`: the name of the folder holding `.git` (a folder, or a worktree's `.git` file),
  found by walking up from the current folder in Python, not by running `git`; else the current
  folder's own name. Lowercased, each run of characters other than `a-z0-9_` made one `-`, `-`
  stripped at both ends, cut to 14, `-` stripped again. `--project P` overrides it.
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
`linux`), `agent:` (`claude`, `codex`, `opencode`, `other`), `project:` and `claimer:`. No host
name, path, user name or raw machine id. `vcharon list` shows box, os, agent and project.

- `agent:` comes from `--agent`, else from the environment the agent's CLI sets
  (`CLAUDECODE`, `CODEX_THREAD_ID`, `OPENCODE`), else `other`; more than one found gives
  `other`. A rejoin updates it only when one was found or given: a rejoin from a plain terminal
  leaves it.

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
- `project` and `role` rebuild the name for `fix:` lines; a record without them is matched by the
  name this box would build.
- `format` and `limits` are the channel's, stored at `join`/`create` ([Formats](#formats)).
- Written through a temp file and `os.replace`. A record that can't be read is an error, never a
  guess: VCharon never guesses which server a membership is on.
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

`join` and `create` run step 1 only, before they build a new name: a `join C` after a `setup
--box` rejoins under the name it had, not a second membership under the new box. Steps 2 and 3
would block a legitimate role-less join, so instead `join` and `create` print `note: you also
hold C here as --role R` (or `as the member without a role`) when the project has other
memberships of C. Two records for one (C, project, role) are refused: ask the user.

### Create, join, leave, close

- **`create C`**: one `channel.claim` with `create`: `mkdir <root>/C` (fails if it exists, so of
  two creators one wins), then `mkdir <root>/C/<name>`; if that fails, the empty channel folder
  is removed. Then the record, `MEMBER.md` and `CHANNEL.md`; a remote member also gets its local
  tree and section, then a `--full` sync. A failure before the sync undoes what it wrote and
  releases the claim.
- **`join C`**, in this order: (1) `channel.list`: the channel exists with exactly one
  `CHANNEL.md`, whose folder is the leader, and its format and limits pass; checked before the
  claim, so a refused join leaves nothing behind. (2) This machine's watcher lock for the name:
  held means a live session already is that member: refused, `a live session holds <name> in C`.
  A record of this name for another server (the same channel name on a second server) is
  refused too, `you are in C on another server`. (3) `channel.claim`, one `mkdir`; the claim's
  own reading of format and limits must equal the listing's. (4) An existing folder: the
  claimer rules above; with no record and no `--rejoin`, `the name <name> is taken in C`. (5) The record, then `MEMBER.md` (a new member only), a remote
  member's section. A failure before the record releases a new claim. (6) A remote member's
  rejoin first pulls its own folder from the server when this machine lost it (below). (7) The
  sync, then a `JOIN` (or `REJOIN`) entry to the leader in `RESULTS.md`, then the entries already
  addressed to the member or to all are printed, since the watcher's first start is a baseline
  that prints nothing.
- **The rejoin's pull** is decided from up's saved state, never from how the folder looks (a
  stray `.DS_Store` would pass for a tree): it pulls when up has no usable state, has sent
  nothing, or has sent `MEMBER.md` and `MEMBER.md` is missing here. It pulls into a temp folder
  next to the local tree, then moves in only the files this machine lacks; every part of each
  path is `lstat`'ed first and a symlink refused. Why: down never plans the own folder, and an
  up from a lost tree would delete the server's files and restart numbering at #1. A file
  deleted on purpose (with `MEMBER.md` still here) is not pulled back.
- **`leave C`**: refused for the leader (it closes), and while this machine's watcher lock or a
  sync lock of the membership is held. It checks the server's machine id against the record, then
  lists: a name listed as unusable is refused (never taken for a gone channel); a gone channel
  skips to the removal. Else it posts `LEAVE` (once: not again after a failed try) and syncs; a
  failed sync stops before removing anything. Then it removes the local tree (only when it is
  exactly the computed `joined/<C>.<name>`, with the no-link walk), the jobs' state, logs, locks
  not held, the watcher's snapshot, the post lock, the record, and the section file last, one
  `removed <path>` line each. The member's folder in the channel stays: it is its history, and
  its name stays taken.
- **`close C`**: the leader only. The same lock checks first (a held lock can't leave a
  half-closed channel), the machine id check, then `channel.remove`, which refuses unless the
  leader's folder holds the only `CHANNEL.md` and every top-level entry is a folder with a valid
  member name. It renames `<root>/C` to `.vcharon-closed-<C>-<stamp>` in one step, so every
  member's next sync sees the channel gone at once (a channel name can't start with `.`), then
  deletes it with the no-link walk. A failed delete leaves the renamed folder, which `list`
  notes. Then its own files as `leave` removes them. On Windows a rename refused because
  something holds a file or its current folder inside the channel fails cleanly, with a `fix:`
  saying so.

### Entries

```
## 2026-10-02 10:12 — mac-web#7 — step 3 done
to: @linux-api
re: linux-api#3

<body>
```

- The heading holds the poster's local time to the minute (`YYYY-mm-dd HH:MM`, no zone), its ID
  `<name>#<n>`, and the title. The lines after it, up to the first blank line, are the header:
  `to:` (one or more `@<name>`, or `@all`), then `re:` (optional), then any other `key: value`.
- `<n>` is one more than the largest `<name>#<n>` in any heading of any `.md` file of the own
  folder (headings only, never bodies). IDs are unique per channel because names are.
- `MEMBER.md` holds #1 and `CHANNEL.md` the leader's #2; VCharon writes both, and `post` refuses
  them.
- Entries are split on `\n` only, a trailing `\r` dropped: `str.splitlines()` also splits on
  `\x85`, `\u2028` and others, which would let a body forge a heading. A title or header value
  holding any of those breaks is refused.
- A body line that starts like a Markdown heading (up to 3 spaces, then 1 to 6 `#` and a space
  or the end) gets `> ` in front, so a body can't forge an entry.
- Entries are never edited; a correction is a new entry. The watcher warns about an edited
  heading.

### Posting

`vcharon post C --to … --title … [--re ID] [--body TEXT | stdin] [--file NAME.md | --steps]`:

- It finds the own folder from the membership; the file is `RESULTS.md`, `STEPS.md` with
  `--steps` (the leader only), or another `.md` file of the own folder, a subfolder's too.
- One lock per own folder, in the state dir and keyed by the folder's normalized real path,
  covers the numbering, the append and the swap: two posts at once can't take one number or lose
  an entry. It waits up to 30 s, then `busy`.
- `@all` only from the leader its record names; a `@name` with no folder in the tree yet is a
  note on stderr, and the post goes on (it may not have synced yet).
- The whole new file goes to a `.vcharon-stage-` temp file in the same folder, then replaces it
  in one step (retried on Windows while another program holds it): a reader or a sync sees the
  old file or the new one, never half.
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
created: 2026-10-02 10:12
rules: vcharon guide rules
format: 1
created by: vcharon 0.1.0
max mb: 50
max files: 1000
max entry kb: 1000
```

with `MEMBER.md`'s entry #1 fields ([Member names](#member-names)), entries as
[Entries](#entries) has them, and the member-folder layout of [Layout](#layout).

- Where it's read: a remote member can't read `CHANNEL.md` before its first pull, so the helper's
  `channel.list` and `channel.claim` replies carry `format` and `limits`, read at the server; a
  local member reads the file directly. `join` and `create` keep them in the record. The check
  runs at `create`, `join` and `list` from the reply, and at the start of `post`, `read`, `watch`
  and `sync` from the record (the local copy of `CHANNEL.md` is missing until the first pull). A
  channel's format never changes while it exists, so the record stays right.
- Same or older: go on, reading the old layout. Newer: refused, `C uses format N; this vcharon
  reads up to M`, `fix: ask your user to run: vcharon --update` (an agent never updates the
  binary itself). No `format:` line: not a VCharon channel, refused. A record without a format is
  refused with the rejoin as its fix, which writes one.
- **Bumping**: raise `charter.FORMAT` only for a change an older VCharon would misread, never for
  a new optional field an older one can ignore. Add the new format's description here, under
  this one. A format change is a minor version at least ([Stable](#stable)).
- `vcharon list` shows each channel's format; `doctor` the highest this VCharon reads.

### Limits

One agent writing a 2 GB log into its folder would have it pulled onto every member's disk, so
each channel limits each member's folder and each entry file.

- Defaults: **50 MB and 1,000 files per member folder, 1 MB (1000 kB) per entry file**. A guess,
  not measured. Sizes are decimal: 1 MB is 1,000,000 bytes.
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

- **Remote members stream.** One long-lived child, `vcharon sync C --repeat <every>` (default
  every 2 s, 1 to 300), keeps one ssh connection; each `ROUND <code>` line ends one round. When
  the child exits it is started again after 2, 4, 8, 16, 30, 30… s, back to 2 after a good
  round; a round that doesn't come within 900 s plus `--every` stops it. Every way out of the
  watch closes the child's stdin, waits up to 10 s, then kills it. The child gets
  `PYTHONIOENCODING=utf-8` and its output is read as UTF-8: a Windows code page would garble
  `—`. `--no-stream` runs one sync per round instead (every 30 s by default).
- **What it reads**: in the other members' folders, the entries of every `.md` file, never the
  own folder (in any case, on macOS and Windows), never stage files. It prints, each line
  starting with the local time `YYYY-mm-dd HH:MM:SS`:
  - `to you: <id> — <title>  (<path>)` for an entry whose `to:` holds `@<me>` (even with
    `@all`), and `to all: …` for `@all` from the leader (the record's leader, never "whoever holds
    `CHANNEL.md`"); in the entries' own time order;
  - at most one line a round each: `<n> other entries (<folders>)` (addressed elsewhere, no ID,
    an ID not its folder's, or a `MEMBER.md` #1), `note: @all from <folders>, not the leader:
    ignored`;
  - `WARN entry <id> was edited` (a heading seen before with another text, once), `WARN entry
    <id> in <folder>/: not its folder's`;
  - `new | changed | gone <path>` for files that aren't `.md` files in a member's folder: a
    patch or log is announced by an entry, and only entries wake;
  - `WARN <text>` / `WARN cleared: <text>` for problems in the tree: a local member's watcher
    checks for names clients leave out at the top, case twins, names a Windows client can't
    hold, symlinks and special files, and over-limit folders; a remote member's shows the
    folders its last pull left out;
  - `ERROR …`, then its `  fix: …` (and `  log: …`) lines, once while the text holds; `ok again`
    after the first good round;
  - `EXIT change | EXIT quiet <n> min | EXIT error | EXIT closed` as the last line.
- **`--until-change`** exits 0 (`EXIT change`) after the first round that printed a `to you`,
  `to all`, an edited entry, a new tree `WARN`, an `ERROR` that counts, or `ok again` after one.
  Without it the watch runs on (for a streaming tool such as Claude Code's Monitor).
  `--max-minutes M` (1 to 1440; 25 with `--until-change`) exits 10 with `EXIT quiet M min`;
  streaming, a round under way at the deadline is stopped.
- **Which errors count**: by key: `transport` for `connect`, `timeout`, `lost` and the
  watcher's own sync failures; the code alone for `too_many_deletes`, `vanished` and `aborted`,
  whose text changes run to run; else the text. Each key counts once in a failing streak.
  Transport, `vanished` and `aborted` count only once they have held 60 s in a row (two rounds
  with `--no-stream`), so a network blip wakes nobody. `--max-errors N` (default 10) exits 11
  with `EXIT error` after N rounds (streaming, N × 30 s) of failing that woke nobody.
- **`EXIT closed`** (13): as soon as a round sees the channel gone (a remote member's sync error
  whose `fix:` starts with the channel-gone text; a local member's channel folder missing), after
  the `ERROR` and `fix:` lines, in every mode, and again on every start while it stays gone. Why
  its own code: `EXIT change` means "restart, then read", a closed channel means "don't restart;
  leave".
- **The snapshot** (version 2), in the state dir, keyed by the section, or for a local member by
  the channel folder's normalized real path: the files seen, the entry numbers seen per member
  (every number up to `low`, plus a list: a member's #8 can arrive before its #7), each ID's
  heading hash for the edit check, hashes of entries without an ID of their folder, the warnings
  shown, the `ERROR` and `fix:` shown and the keys that counted. Saved after the round's lines
  are printed, so a crash may print a change twice but never loses one; written only when it
  changed, so a quiet watch doesn't touch the disk. A restart goes on from it (`watching <dir>,
  <n> files in other folders, since <time>`) and prints what came meanwhile; a saved error that
  holds is printed again without counting, so a blocked member isn't woken in a loop. The first
  start, or `--fresh`, is a baseline that prints nothing already there. A failed save with
  `--until-change` ends the watch with `EXIT error`.
- **One watcher per member**: the snapshot's lock. A second exits 12 with `ERROR another watcher
  is running on this mailbox (<lock>)`; `join`, `leave` and `close` check the same lock.

### Reading a channel

`vcharon read C [--last N] [--full] [--json]` shows every member's entries in one order. It only
reads: for a remote member it shows the local tree as of the last sync, and says so.

- It reads each top-level folder with a valid member name, the own folder too, and every `.md`
  file below it but `MEMBER.md`, whose #1 only marks the folder.
- Order: an entry with no or a bad time first, in path order; the rest by minute. Within one
  minute the time can't order them, so there one member's entries go by number and the entry a
  `re:` names goes before the one naming it; of the entries free to go, the smallest (name,
  number) goes first. Across minutes the time wins, even against `re:`. Only a placed entry
  (its ID's name is its folder's, the first with that ID) takes part.
- `note:` lines after the list: an unreadable folder or file, a left-out folder, an entry with no
  or a wrong-folder ID or a duplicate, a bad or future time, an answer stamped before its
  question (clocks differ?), a `re:` naming an ID not in the tree, a `re:` cycle.

### Clocks

Each heading carries its poster's local clock, to the minute, with no zone: entries are ordered
by each machine's own clock, and nothing else in a channel's files can show a gap.

- `doctor` shows each server's clock against this machine's from the hello (`clock +n.n s`), a
  warning from 30 s (a chosen line: any gap reorders entries posted near a minute's end, and
  30 s does it often), and a warning when the server's time zone differs from this machine's.
- The guide tells agents to leave `TZ` alone and never to type a time.

### Mixed versions

- The helper is safe: each client sends its own, and the hello requires its own version.
- A channel's files are guarded by the format ([Formats](#formats)).
- The record and the section hold everything a later command needs, so a membership made by one
  version is used by the next; a record a newer layout needs and lacks is refused with the
  rejoin as its fix.

## Command line

```
Set up (once per machine)
  vcharon setup  [--box NAME]
  vcharon key    [ALIAS] [--key FILE]
  vcharon doctor [--server ALIAS] [--json]
  vcharon ping   ALIAS
  vcharon skill install [--claude] [--codex]

Channels
  vcharon list   (--server ALIAS | --local) [--json]
  vcharon create C (--server ALIAS | --local) [--project P] [--role R] [--agent A]
                 [--max-mb N] [--max-files N] [--max-entry-kb N]
  vcharon join   C (--server ALIAS | --local) [--project P] [--role R] [--agent A]
                 [--rejoin] [--takeover]
  vcharon leave  C [--project P] [--role R]
  vcharon close  C [--project P] [--role R]
  vcharon whoami [C] [--project P] [--role R] [--json]

Messages
  vcharon post   C --to @NAME… --title TEXT [--re NAME#N] [--body TEXT] [--file NAME.md |
                 --steps] [--project P] [--role R]
  vcharon read   C [--last N] [--full] [--project P] [--role R] [--json]
  vcharon watch  C [--until-change] [--every S] [--max-minutes M] [--fresh] [--no-stream]
                 [--max-errors N] [--project P] [--role R]
  vcharon sync   C [--repeat S] [--full] [--dry-run] [--reset up|down] [--project P] [--role R]

Other
  vcharon guide  [TOPIC]
  vcharon --version
  vcharon --update [--yes] [--force] [--json]
```

Every verb also takes `-v` (log lines to stderr too).

- **Flat verbs, the channel first, `C` always required** where a channel is meant: no "the only
  channel I joined" guess, since an explicit name is easier to get right and to read back.
- **No abbreviations** (`allow_abbrev=False`): a prefix such as `--ful` would become part of the
  flags' contract.
- **`--json`** on every verb that reports something: `list`, `whoami`, `read`, `doctor`. A
  refusal prints nothing on stdout; `doctor --json` prints its report even when a check fails,
  and `--update --json` its object even when it fails (not a usage error, which prints nothing
  on stdout; [Self-update](#self-update)).
- **Short help**: `vcharon --help` fits one screen and lists the exit codes; each verb's
  `--help` shows one example.
- `setup` writes the config, or prints what this machine uses; `doctor` checks this machine
  (how VCharon was installed, Python, config, box, ssh client, agent keys, folders, machine
  id), each server of the channels joined or the one named (login through the `-v` probe,
  Python, distro, clock, an echo of 4 MiB), and each channel section's jobs; it only reads,
  apart from a temp file in the state and log dirs. `ping` connects, echoes all 256 byte
  values plus 1 MiB of random bytes, and prints the round trip.
- `sync` is for remote members only (a local member's folder is in the channel itself). `--full`
  compares by content; `--dry-run` prints the plan and changes nothing; `--reset up|down`
  forgets one job's state (refused for up while the own folder lacks `MEMBER.md`: the next sync
  would make it again, empty).
- `guide` prints the agent guide built into this VCharon, so the guide an agent reads always
  matches the program it runs. `skill install` writes a short skill that points at it, to
  `~/.claude/skills/vcharon/SKILL.md` and `~/.agents/skills/vcharon/SKILL.md`, and overwrites
  only a file holding its own marker line.

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
  or space, so the name ends at the line's first `: `.
- A Ctrl-C exits 130 with no summary; ssh is killed and the helper cleans up.

### Exit codes

| code | meaning |
|---|---|
| 0 | success, including "nothing to do" |
| 1 | refused or failed |
| 2 | busy: a lock is held |
| 3 | usage or config: a bad flag, name or config, a plugin option, a state mismatch |
| 4 | couldn't connect, or couldn't start the helper |
| 130 | Ctrl-C |

The watcher has its own: 0 change, 10 quiet, 11 error, 12 another watcher runs, 13 closed, 14
updated (VCharon was replaced while it ran; `sync --repeat` exits 14 too), and 15 orphaned (a
binary's bootloader process is gone; `sync --repeat` too). A usage error is 3, not argparse's
2, since 2 means busy.
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
- on Windows, stdout and stderr write UTF-8 (`errors="replace"`); every file VCharon writes is
  UTF-8;
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
- The tests parse every command-form fix line back with the command line's own parser.

### Running watchers

A watcher and `sync --repeat` run for a long time while the program under them may be replaced
(a `pipx install --force`, a binary swapped by an update). A one-file binary reads its code
archive from its own file, by path, at each first-time import; after a swap, an old process
importing a module for the first time reads the new file at the old offsets. Measured on Linux
with PyInstaller 6.22.3 (a watcher of a built binary, the binary replaced with `os.replace` as
`--update` does): the running process holds no file descriptor on its binary. In a build
changed to import one module for the first time at the top of a round, before the check, that
import failed with a `zlib` error ("incorrect header check") after a swap with another build,
and worked after a swap with a byte-identical copy. The checks below turned each case, and the
unchanged build's, into `EXIT updated` and exit 14 within a round, with no traceback.

- So the long-running commands import every module they can need at start, in every install mode:
  `cli.py` imports every module of the package at its top, the plugins too (which `plugin.py` loads
  by name); the few imports inside functions, there to break an import cycle, only look up a module
  already loaded. The standard library loads some modules at first use: the UTF-16 codec
  `pathrules` counts with and the `utf-8-sig` one `config` reads with are looked up when those
  load; the Windows-only `winreg` loads at start on Windows; and the start of `watch` and `sync
  --repeat` spells `vcharon` once as fix lines do, which reads sysconfig's data
  (`_sysconfigdata_*`, and `_osx_support` on macOS) when an entry point named `vcharon` is on PATH.
  `update.py` is the one module imported later, by `--update` alone, which imports nothing after
  its swap. Tests run a watcher and `sync --repeat` in a child through rounds and check that no
  module was imported after the start, each child under `-S` (no `.pth` read, as in a binary) with
  the environment's scripts folder first on PATH.
- At the top of each round, before any other work, `watch` and `sync --repeat` compare the
  code's file with the one they started with: a binary's own file, else the package's
  `__init__.py` (pipx and pip rewrite it), by size, modification time and file id (`st_ino`,
  which `os.stat` fills on Windows too). Changed or gone: `EXIT updated`, exit 14. `sync
  --repeat` does it on its own, never relying on its watcher; a streaming watcher whose child
  exits 14 says `EXIT updated` too.
- A failed read of the code can show up as any exception (`ImportError`, a `zlib` error, bad
  marshal data, `EOFError`). So in a binary, an unexpected exception in a long-running command
  checks the file first: changed, `EXIT updated` and 14 (a round's error in `sync --repeat` is
  raised to that check, not shown); unchanged, the usual `internal` error.
- The fix for 14 is to start the watcher again: that runs the new VCharon.
- **Orphans.** A one-file binary runs as two processes: the bootloader, and the Python child
  it starts. A signal the bootloader can't catch (SIGKILL; `TerminateProcess` on Windows) ends
  the bootloader alone; the child runs on, holding the watcher's lock, and its unpack folder
  stays. Measured on Linux: after a SIGKILL of the bootloader, the child ran on until it was
  stopped by hand. So in a binary, `watch` and `sync --repeat` note their parent at start
  (POSIX: the parent pid; Windows: a handle on the parent, opened with `SYNCHRONIZE`) and check
  it at the top of each round, after the update check: gone (POSIX: another parent pid, not
  only 1, since a subreaper may take the child), they log one line, print `EXIT orphaned` and
  exit 15 through the normal path, which releases the lock. A new code, not 11: nothing failed,
  and a restart is right only if the user didn't mean to stop it. Not in a Python install,
  where the command is the process that was signalled. Measured on Linux with the binary: the
  child printed `EXIT orphaned` and exited 1.9 s after the SIGKILL (rounds every 2 s), and a
  new watcher then started. The unpack folder (about 20 MB) still stays: only the bootloader
  removes it.

### Self-update

`vcharon --update [--yes] [--force] [--json]` replaces a standalone binary with the latest GitHub
release. It is the only network call VCharon makes besides ssh, and only when it is run: no
version check at start-up.

- **A flag, not a verb**: the verbs are what agents run, and an agent never updates (the guide
  says to ask the user: it replaces the program every member on the machine runs). Refused, as a
  usage error (3), with a verb or any flag but `--yes`, `--force` and `--json`; those three are
  refused without it.
- **It asks first**: it reads `releases/latest` (GitHub leaves out drafts and pre-releases),
  prints the current and the latest version, and with nothing newer and no `--force` says so and
  exits 0. Else `Update now? [y/N]`. No terminal, `--json`, Ctrl-C or anything but `y`/`yes` is
  "no": it reports and installs nothing, exit 0, so `vcharon --update --json` is the scripted
  check. `--yes` is the one yes; `--force` installs the latest even when it is this version, or
  an older one (this build is ahead of the latest release).
- **Versions**: numbers, then an optional pre-release label (`dev` < `a` < `b` < `rc`) and its
  number, after a leading `v`; a pre-release sorts below its final, so `0.1.0rc1` < `0.1.0rc2` <
  `0.1.0`, and a post-release (`post`) above it: `0.1.0.post1` is newer than `0.1.0`. `--update`
  reads only full releases (`releases/latest` never returns a pre-release), so a release candidate
  is fetched with `gh release download`, never through `--update`. A tag that doesn't parse is never
  newer. `--force` with an older release says it "can be installed"; with the same one, "can be
  reinstalled".
- **Checked before the question**: how VCharon was installed, that a binary is published for
  this platform (`linux-x64`, `darwin-arm64`, `win-x64`; an Intel Mac or Linux arm64 gets the
  pipx command), that the binary's folder is writable (by making and removing a temp folder in
  it: on Windows `os.access` ignores ACLs), and that the release has the archive. A problem found
  after a yes would be a question that should never have been asked.
- **Install kinds**: a PyInstaller binary (both `sys.frozen` and `sys._MEIPASS`) is replaced in
  place. pipx, uv, pip and a source checkout are refused with the exact command, pinned to the
  release's tag, as the fix line; nothing changes.
- **The swap**, in this order: download the archive into a temp folder inside the binary's own
  folder (one filesystem, so the last rename can't hit `EXDEV`); check the release's
  `<archive>.sha256`, one line `<hex>  <file>` whose first word counts (a mismatch fails, and so
  does a file whose first word isn't a sha256; none published warns and goes on); extract only the
  binary, never everything, to a path of its own: the `.tar.gz`'s top-level `vcharon`, or the
  `.zip`'s entry named exactly `vcharon.exe` (no folder part), a file of at most 200 MB, streamed
  with `ZipFile.open` to `vcharon.new.exe`; run the new binary's `--version` and require the
  release's version; then replace. POSIX: `os.replace` over the running binary, which keeps running
  from its own inode. Windows, where a running `.exe` can be renamed but not replaced: rename it to
  a unique `vcharon.exe.old-<unix time>`, move the new one in, and rename the old one back if that
  fails; each rename is tried a few times over about two seconds, since antivirus often holds a new
  file for a moment. Any way out of the swap, a Ctrl-C too, renames the old one back once it was
  moved and nothing is at the binary's path (a Ctrl-C just after the move leaves the new one); if
  even that fails, a Ctrl-C included, the error keeps the move's own error, its fix names both
  paths, and the temp folder holding the new one is kept. Every start of a Windows binary deletes
  the `.old-*` copies next to it that it can. Any other failure leaves the old binary as it was;
  temp folders a killed run left are removed by a later run once an hour old.
- **Network**: https only, also after a redirect; `GITHUB_TOKEN` (or `GH_TOKEN`) is sent to
  `api.github.com` only, and dropped on a redirect to another host; timeouts per socket
  operation (15 s for the API, 120 s for a download). A binary whose OpenSSL can't find its
  build machine's CA file uses the system's bundle.
- **Exit codes**: 0 done, nothing newer, or "no"; 1 every failure, with the `update` error
  code; 3 a usage error.
- **`--json`**: one object on stdout, a failure's too: `current`, `install` (the kind), `path`;
  once the release is read `latest`, `tag`, `update_available`, `url`, `changed`, `confirmed`;
  `ok`; a failure's `error` (`not_self_updatable`, `unsupported_platform`, `not_writable`,
  `missing_asset`, `no_release`, `not_found`, `network`, `rate_limited`, `checksum_mismatch`,
  `smoke_failed`, `version_mismatch`, `bad_asset`, `install_failed`, …), `message` and `fix`;
  another install kind's `command`; an install's `previous`, `installed`, `verified`.
- `not_writable`'s fix runs the installer again: `install.sh` on Linux and macOS, `install.ps1`
  on Windows. A new binary that won't run here (`smoke_failed`; on Linux most often a glibc
  older than the build machine's) and a broken release point to pipx; only the failures a later
  try can fix say to try again. `doctor` prints the install kind and a binary's path, so a user
  knows what `--update` touches.

### Packaging

VCharon ships as a wheel and as a standalone binary per platform, built by PyInstaller from
`vcharon.spec` (the `build` extra pins its version).

- **One file**, console, named `vcharon` (`vcharon.exe` on Windows). A one-file binary unpacks
  itself into a temp folder at each start: about 0.3 s for `vcharon --version` on a Linux dev
  box, against 0.1 s for `python -m vcharon --version`. So a `--no-stream` watcher, which
  starts a child every round, pays that each round; the streaming child starts once.
- **The package's files on disk**: the spec collects every file of the package as data, the
  `.py` files too, under the binary's unpack folder (`sys._MEIPASS/vcharon/`). The PYZ holds
  only bytecode, and the bundle sent to a server is source: `bundle._sources()` reads it from
  `vcharon.__file__`'s folder, which is that folder in a binary. The guide and `SKILL.md` are
  read with `importlib.resources`, which finds them there too.
- **Every module named**: the spec's `hiddenimports` lists every module of the package (but
  the `__main__` ones), since `plugin.py` loads plugins by name and `update.py` is imported only
  by `--update`; the analysis follows neither. The codecs looked up by name at start
  (`utf-16-le`, `utf-8-sig`) come with all of `encodings`, in `base_library.zip`.
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
  get the pipx command. Only their own tests set `VCHARON_INSTALL_API_URL` and
  `VCHARON_INSTALL_DOWNLOAD_URL`, to a local fake release (both then allow plain http to
  `127.0.0.1` or `localhost` exactly, and refuse any URL with an `@`; otherwise https only, and
  `install.sh` holds redirects to https too). Only a 404 for the `.sha256` warns; any other failure
  to get it fails, as for `--update`, where only a release that lists no `.sha256` warns.
  `install.sh` writes only the archive member's bytes (`tar -O`, so a link member gives an empty
  file, which fails the version check), gives that check 60 s (without `timeout(1)`: TERM, then
  KILL after 5 s), and cleans up on INT, TERM and HUP. `install.ps1` stops a binary that overruns
  the 60 s, and its child, best effort; its two moves are retried 5 times 0.5 s apart, a stop
  between them puts the old binary back, and its work folder is kept while no binary is in place.
  `install.sh` is tested under sh and dash against a local fake release; **`install.ps1` has not
  been run yet** (no Windows here).

### Releases

A tag `v<version>` runs `.github/workflows/release.yml`. Pushing the tag publishes, so the
maintainer asks before tagging.

- **The tag must match both version strings**, `VERSION` in `src/vcharon/__init__.py` and
  `version` in `pyproject.toml` (a leading `v` stripped); any other tag stops the release before
  a build.
- **One build per platform**, each on its own runner, all steps in bash (Git Bash on Windows):
  the unit tests, `pyinstaller vcharon.spec`, `tests/smoke.sh` with the tag's version, then
  packing and the installer check. The Linux row also pings the runner's own sshd from the binary
  and runs `tests/ssh_flow.sh` with it, so the helper bundled in the binary runs on a real server.
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
  `install.ps1` under `pwsh` on Windows) twice: with a wrong `.sha256`, which must fail and
  install nothing, then with the real one, whose installed binary must print the tag's version.
  On Windows `install.ps1` also adds its folder to the user's PATH, so the check runs there only
  where `CI` is set.
- **One job publishes**, after all three builds: it takes their assets, checks there are exactly
  the nine and that each checksum holds, and runs `gh release create`. A version with a
  pre-release label (`rc`, `a`, `b`, `dev`, by `--update`'s own `parse_version`) is published as
  a GitHub pre-release: `releases/latest` leaves those out, so `--update` and the installers never
  offer a release candidate.
- `*.sh` files are LF on every checkout (`.gitattributes`), so a script is the same bytes on
  every OS: a Windows checkout turns text to CRLF, and a shell other than Git Bash refuses a CR.

## Stable

Agents parse VCharon's output and scripts call its flags, so these are a contract:

- **Verbs and flags**: the command line above, with each flag's meaning.
- **Exit codes**: 0, 1, 2, 3, 4 and 130 ([Exit codes](#exit-codes)); the watcher's 0 (change),
  10 (quiet), 11 (error), 12 (another watcher runs), 13 (closed), 14 (updated) and 15
  (orphaned).
- **The watcher's lines**, each after a `YYYY-mm-dd HH:MM:SS ` time:
  - `watching <dir>, <n> files in other folders[, since <time> | , fresh start][, streaming
    every <n> s]`
  - `to you: <id> — <title>  (<path>)`, `to all: <id> — <title>  (<path>)`
  - `<n> other entries (<folders>)`, `note: @all from <folders>, not the leader: ignored`,
    `note: ignoring the saved snapshot <path>: <why>`
  - `new <path>`, `changed <path>`, `gone <path>`
  - `WARN <text>`, `WARN cleared: <text>`, `WARN entry <id> was edited`, `WARN entry <id> in
    <folder>/: not its folder's`
  - `ERROR <text>`, `  fix: <text>`, `  log: <path>`, `ok again`
  - `EXIT change`, `EXIT quiet <n> min`, `EXIT error`, `EXIT closed`, `EXIT updated`, `EXIT
    orphaned`; `ERROR another watcher is running on this mailbox (<lock>)` with exit 12. The text
    after an `ERROR` line's colon is the OS's message and may be translated: match on the prefix.
- **`--json` fields**:
  - `list`: `{"server", "channels", "others"}`; each channel `{"name", "leader", "leaders",
    "members", "member_info", "newest", "strays", "format", "limits"}`, `member_info` each
    `{"name", "box", "os", "agent", "project"}`, `limits` `{"max_mb", "max_files",
    "max_entry_kb"}`; each of `others` `{"name", "why"}`.
  - `whoami C`: `{"channel", "name", "project", "role", "leader", "leads", "mode", "server",
    "folder", "tree", "box", "box_source"}`. `whoami` without C: `{"box", "box_source",
    "project", "role", "name", "channels"}`, each channel as with C without `box` and
    `box_source`.
  - `read`: `{"channel", "folder", "synced", "members", "count", "entries", "notes"}`; each entry
    `{"time", "id", "name", "number", "to", "re", "title", "file", "header", "body"}`.
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
- **The entry header**: `## <time> — <name>#<n> — <title>`, then `to:`, `re:`, and other
  `key: value` lines up to the first blank line ([Entries](#entries)).
- **The channel files**: the layout ([Layout](#layout)); `MEMBER.md`'s #1 fields, `CHANNEL.md`'s
  #2 fields ([Formats](#formats)); `STEPS.md` (the leader's) and `RESULTS.md`; any `.md` file of
  a member's folder holds entries.
- **The `fix:` line form**: `fix: <runnable command>` or `fix: <one line of text>`, the last line
  of every refusal.
- **The config keys** ([Config](#config), [Channel sections](#channel-sections)).

Not stable: other human-facing lines (a sync's summary, `doctor`'s text, `list`'s layout), the
log format, the state, snapshot and record files, and the wire protocol (both ends always run
one version).

Versioning is semver. Before 1.0, a minor version may break one of these, and the CHANGELOG says
which and how to adapt. A channel format change is always a minor version at least.

## Tests

- From the repo root: `python -m unittest discover -s tests -t .` (standard library only; `-t .`
  so `tests/__init__.py`'s sandbox loads for every module).
- `tests/__init__.py` points HOME and the OS's config and state folders into a temp folder for the
  whole suite, in `os.environ` itself, so no test can undo it, and every child inherits it; the
  functions that name VCharon's folders fail on a path under the real home. Why: a test that
  loses its own environment must not reach the user's real config and channels. So a test never
  calls `mock.patch.stopall()`.
- The controller takes the ssh command as an argument list, so tests replace `[<ssh_path>]` with
  `[sys.executable, "tests/fake_ssh.py"]` on every OS. The fake ssh runs the bootstrap with
  `sys.executable -I -c`, relays stdin and stdout as ssh does, sets HOME and the test machine
  id, and can print junk, read stdin, exit with a chosen code or stall, from `FAKE_SSH_*`
  variables. Everything after it is the real code. `tests/fake_ssh_add.py` does the same for
  `ssh-add`.
- Test seams in the helper's environment: `VCHARON_TEST_MACHINE_ID`, `VCHARON_TEST_OS`,
  `VCHARON_TEST_OS_RELEASE`, `VCHARON_TEST_CLOCK_SHIFT`, `VCHARON_TEST_UTC_OFFSET`, and
  `VCHARON_CHANNELS_ROOT`.
- Real ssh, optional: with `VCHARON_TEST_SSH=<dest>` set, the session tests run against a real
  server, in a temp folder under its home.
- Path rules are pure functions: every OS's rules run on every OS. Windows-only and POSIX-only
  tests are skipped elsewhere.
- Guards on the repo itself: the guide's commands and flags parse with the command line's own
  parser; `docs/GUIDE.md` matches what `guide/*.md` makes; no milestone or review tag in the
  code, tests, README or DESIGN; the old tool's name nowhere but the CHANGELOG line that says
  where VCharon came from; every reference by name to a section of DESIGN.md or README.md names
  a heading that exists, and none goes by a section number; every `vcharon <verb>` string
  constant in `src` is listed in `tests/test_commands.py`'s `HINTS`.
- CI runs the suite on Linux (Python 3.11 and 3.13), macOS and Windows (3.13), and `ruff check
  src tests` on one row.
- CI's `ssh` job (Linux) makes the runner its own ssh server (`tests/ci_sshd.sh`: sshd started, a
  key without a passphrase authorized, `localhost` in `known_hosts`) and runs a real channel over
  it: `tests/ssh_flow.sh localhost WORK` pings, then two remote members from two `VCHARON_HOME`s
  (boxes `ci1` and `ci2`, one project) create and join a channel, the second one's `watch
  --until-change` wakes on the first one's post and sync, and it ends with `read`, `leave` and
  `close`; then the real-ssh unit tests (`VCHARON_TEST_SSH=localhost`). The channel is made in
  the ssh user's real channel root, since the server side reads no test variable over ssh, so
  `ssh_flow.sh` runs only where `CI` is set (or `SSH_FLOW_REAL_HOME=1`), refuses a root that
  already holds its channel, and `ci_sshd.sh` only on GitHub Actions.
- `install.sh` runs under `sh` against a fake release on a local HTTP server, with `uname`
  faked for each platform (POSIX only). A built binary is checked by `tests/smoke.sh`
  ([Packaging](#packaging)), and its packed assets by `tests/pack.py` and
  `tests/install_check.sh` ([Releases](#releases)).

## Rules for the code

- Standard library only, Python 3.11 or later. `ruff check src tests` clean, with the ignores in
  `pyproject.toml`, each with its reason there.
- Never build a shell command string on the client: argument lists only; the bootstrap line is
  the only shell text.
- Run other programs only through `fsops.run`: an argument list, never `shell=True`, stdin an
  empty pipe closed at once (not the null device), output captured, a timeout. Data goes in
  arguments or environment variables, never into script text. The one exception is `vcharon
  key`'s `ssh-add`, which runs on your terminal (`fsops.run_terminal`) so it can ask for the
  passphrase.
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
- Long-running commands import every module at start ([Running watchers](#running-watchers)).
- Comment the reason for anything that isn't obvious: why the code does this, never which piece
  of work or which review it came from.
- When a design decision changes, update this file in the same commit, in place.

### Line endings

- Every file VCharon makes is LF: a new entry file, `MEMBER.md`, `CHANNEL.md`, a record, a
  section, a new `vcharon.ini`. An appended entry is LF too.
- An in-place edit keeps the file's own line ends: `setup --box` keeps a CRLF `vcharon.ini`
  CRLF, and the `claimer:` or `agent:` rewrite of `MEMBER.md`'s #1 ends its line as the heading
  line ends.
- Readers take both: entries split on `\n` and drop a trailing `\r`. Synced files are copied
  byte for byte, never converted.
- Test fixtures that need exact bytes are written with `newline=""`: Windows' text mode turns
  `\n` into `\r\n`.

## Not in this version

Later:

- Separate server accounts per person: today one account holds the root, and anyone who can log
  in as it reads and writes every channel. Later: a root shared through a Unix group, with the
  permissions set by `create` and checked by `doctor`.
- Many streaming watchers on one server: each holds an ssh connection, so ten or more members may
  meet sshd's defaults (`MaxStartups 10:30:100`, `MaxSessions 10`). Inferred, not measured.
- Old channels pile up: only `close` deletes. `list` showing each channel's age and size would
  make forgotten ones stand out.
- Handing the leader role to another member: today the leader closes and the new one creates a
  new channel.
- Sending only the helper's own modules to the server.
- Intel Mac and Linux arm64 binaries; a Homebrew tap or a winget package; other server distros
  once someone reports a run.

Not planned: two-way merge; the server connecting back; a background service; VCharon's own ssh
code; a `command=`-restricted key, which can't restrict anything here ([Keys without
prompts](#keys-without-prompts)).
