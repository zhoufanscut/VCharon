# Security

## Reporting a vulnerability

Report it privately through GitHub: the repository's **Security** tab, then **Report a
vulnerability**. Please don't open a public issue for it. Say what you found, how to reproduce
it, and which version (`vcharon --version`) and OS it was on; leave out your own host names,
user names and keys.

Fixes go into the next release, and its CHANGELOG entry says what was fixed.

## What is in scope

- The `vcharon` client: the command line, the channel files it reads and writes, the watcher.
- The helper VCharon sends over ssh and runs with the server's `python3` for each connection:
  what it reads from the client, and what it does in the channel root.
- The installers, `install.sh` and `install.ps1`, and `vcharon --update`: what they download,
  how they check it, and where they write.
- The standalone binaries the release workflow publishes.

Out of scope: ssh itself, the server's account security, and what an agent does with an entry it
reads (VCharon treats entries as input, not orders; the guide's rules topic tells agents so).

## The trust model, in short

Anyone who can log in to the server as the user that holds the channel root can read and write
every channel on it, and on one machine the same holds for its OS user: VCharon adds no login or
encryption of its own and relies on ssh for both. Entries come from other agents and are never
run. The details, each rule with its reason, are in [DESIGN.md](DESIGN.md), "Trust and access",
and the README's "Security model" and "Trust".
