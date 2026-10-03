# AGENTS.md

Guidance for coding agents (Claude Code reads it through `CLAUDE.md`) working **on VCharon's
source**.

> **Want to use VCharon in a channel?** That is another document: run `vcharon guide` (the
> source is `src/vcharon/guide/*.md`, made into `docs/GUIDE.md`).

VCharon gives AI agents file-based channels on one machine or across machines over ssh, with
nothing installed on the server. [README.md](README.md) is for people using it;
[DESIGN.md](DESIGN.md) is how it works, every rule with its reason. Read DESIGN.md's section
before you change the code it describes.

## Commands

From the repo root:

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # once: the package, editable, and ruff

.venv/bin/python -m unittest discover -s tests -t .          # the whole suite (about 5 minutes)
.venv/bin/python -m unittest tests.test_channel -v           # one module
.venv/bin/python -m unittest tests.test_channel.CreateJoinTest -v  # one class

.venv/bin/ruff check src tests                               # lint; CI runs exactly this

.venv/bin/python -m vcharon.guide --write docs/GUIDE.md      # after editing src/vcharon/guide/*.md

.venv/bin/pip install -e ".[build]" && .venv/bin/pyinstaller vcharon.spec   # the binary, in dist/
sh tests/smoke.sh dist /tmp/vc-smoke                         # its smoke test, all in /tmp/vc-smoke
```

- **`-t .` matters**: it makes `tests` a package, so `tests/__init__.py`'s sandbox loads before
  any test (below).
- Lines are at most 100 characters. ruff doesn't check that (E501 is off in its defaults), so
  check it yourself, counting characters, not bytes:

  ```sh
  git ls-files '*.py' | xargs .venv/bin/python -c "import sys; [print('%s:%d' % (p, n)) for p in sys.argv[1:] for n, l in enumerate(open(p, encoding='utf-8'), 1) if len(l.rstrip('\n')) > 100]"
  ```

- Tests are `unittest`, standard library only; no pytest.

## Never touch the user's real folders

VCharon writes config, state, logs and channels under your home. A test or a hand run must never
reach the real ones: they may hold live channels.

- **The suite** is sandboxed by `tests/__init__.py`: HOME and the OS's config and state folders
  point into a temp folder for the whole run, and the functions that name VCharon's folders fail
  on a path under the real home. So **never call `mock.patch.stopall()`** in a test: it would also
  stop the test helpers' own environment patches. Stop only your own patcher.
- **A hand run** goes in a scratch folder. `VCHARON_HOME` moves the config, state, logs and
  `joined` folders, and `VCHARON_CHANNELS_ROOT` the channel root (which doesn't follow
  `VCHARON_HOME`); both work on every OS:

  ```sh
  mkdir -p /tmp/vc/web/.git && cd /tmp/vc/web
  export VCHARON_HOME=/tmp/vc/home VCHARON_CHANNELS_ROOT=/tmp/vc/channels
  <repo>/.venv/bin/python -m vcharon create t --local
  ```

  `vcharon skill install` writes under the home folder itself (`~/.claude`, `~/.agents`): point
  HOME at scratch for it, and on Windows USERPROFILE too (Python's `expanduser` reads that one
  there; `%APPDATA%` and `%LOCALAPPDATA%` name the config and state folders when `VCHARON_HOME`
  isn't set).
- Before and after a full run, list VCharon's folders in the real home (names, sizes, times) and
  compare: they must not change.

## Code rules

The full list, each with its reason, is DESIGN.md's "Rules for the code". The ones most often
missed:

- Standard library only; Python 3.11 or later; 4-space indents.
- Never a shell command string; run programs through `fsops.run`. Never prompt (only `vcharon
  key` may).
- Every error the user sees has a code and ends with a `fix:` line. A command in a fix line is
  written `vcharon <verb> …` and printed through `platform.runnable()`, so it is spelled the way
  this install runs.
- No host name, path, user name or raw machine id in any file written to a channel.
- Comments say why the code does what it does; never which task, plan step or review it came from.
- Line endings: new files LF; an in-place edit keeps the file's own (DESIGN.md, "Line endings").
  Match each file's existing style.

Guards that fail the suite, and what to do:

| guard | test | when it fails |
|---|---|---|
| a `vcharon <verb>` string constant in `src` | `tests/test_commands.py` (`HINTS`) | list the new constant there (None for one that isn't a command) |
| a new subpackage of `src/vcharon/` | `tests/test_bundle.py` | it is sent to the server unless listed in `bundle.CLIENT_ONLY`; list it if only the client runs it |
| the guide | `tests/test_guide.py` | regenerate `docs/GUIDE.md`; every command and flag in the guide must parse |
| build history | `tests/test_guide.py` (`NoHistoryTest`) | remove milestone names, review tags, plan pointers from code, tests, README, DESIGN |
| the old tool's name | `tests/test_old_name.py` | VCharon came from another tool, whose name appears only in the CHANGELOG line that says so; `ALLOWED` counts hits per file exactly |
| a reference to a doc section | `tests/test_guide.py` (`DocReferenceTest`) | references go by heading name, as `(DESIGN, "Which membership")`; rename both together; never a section number |

## Private words

The repo must never hold the maintainer's own machine names, host names, paths or accounts: not
in code, tests, docs, file names or commit messages. There is a local word list in `.git/info/`,
and two hooks in `.git/hooks/` (`pre-commit`, `commit-msg`) refuse a commit that matches it; see
the maintainer for both. They are not in the repo, on purpose: a committed list would publish
the words it hides. Never commit with `--no-verify`. Examples use neutral names only: boxes
`mac`, `win`, `linux`, `laptop`; servers `devbox`, `server.example.com`; projects `myapp`,
`api`, `web`.

## Where each kind of text goes

| what | where | published |
|---|---|---|
| how it works now: the rules, each with a one-line reason | `DESIGN.md`, edited in place when something changes; no dates, no milestone names, no "as built" notes | yes |
| a piece of work being planned: steps, open questions, review notes | a local plan file (listed in `.git/info/exclude`), deleted when the work ships; **never committed** | no |
| what shipped in each version, and what was checked on which OS, marked measured or inferred | `CHANGELOG.md` | yes |
| why one change was made | its commit message | yes |
| a lesson from a mistake | only the rule it produced, in DESIGN.md or the guide, with a one-line reason; the story goes | – |
| what an agent needs to use VCharon | the guide, `src/vcharon/guide/*.md` | yes |

So DESIGN.md never grows a build log, and the code's comments never point into one.

- Docs, comments and commit messages are in English.
- A commit message says what changed and why. It states a cause only when a run showed it;
  otherwise it says the cause is suspected, or leaves it out.
- Update DESIGN.md in the same commit as the behaviour it describes, and the guide when an agent
  would see the change.
- Every change a user or an agent can see (a flag, an output line, a `--json` field, an exit
  code, a channel format) gets a line under the CHANGELOG's unreleased section in the same commit.
  A change to anything DESIGN.md lists under "Stable" says how to adapt.

## Releases

Not set up yet: `release.yml`, `install.sh` and `install.ps1` come before the first release;
`vcharon --update` (`src/vcharon/update.py`) is there, waiting for a release to read. The
outline, once they exist:

1. The version is in two places, which must agree: `VERSION` in `src/vcharon/__init__.py` and
   `version` in `pyproject.toml`.
2. The CHANGELOG's unreleased section gets the version and the release date, with what was
   checked on which OS, marked measured or inferred.
3. The maintainer tags `v<version>` (ask first: a tag publishes). `release.yml` refuses a tag that
   doesn't match both version strings, runs the suite, builds one binary per platform
   (`linux-x64`, `darwin-arm64`, `win-x64`), smoke-tests each, and uploads the binary, its
   archive and the archive's `.sha256`.
4. Three files agree on the release asset names and change together: `release.yml`,
   `install.sh`/`install.ps1`, and the updater.
