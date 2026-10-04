"""vcharon skill install [--claude] [--codex]: write the skill (SKILL.md in this folder) where
each agent looks for user skills. The skill says when to use vcharon, which guide topics to
read, and the few rules an agent must never skip; everything else is in vcharon guide, which
always matches the binary, and the skill says the guide wins where they differ.

Where each agent looks:
- Claude Code: ~/.claude/skills/<name>/SKILL.md (its skills documentation).
- Codex: ~/.agents/skills/<name>/SKILL.md, its documented user location. Codex also still reads
  ~/.codex/skills/, which its source marks deprecated, so vcharon doesn't write there.

A file is overwritten only when it holds MARKER, the line this SKILL.md carries: anything
else there is someone's own skill, and is refused with nothing written."""

from __future__ import annotations

import os
from importlib import resources

from .. import fsops, platform
from ..proto import VCharonError

NAME = "vcharon"
MARKER = ("<!-- written by vcharon skill install, which replaces this file: "
          "keep your own edits elsewhere -->")
# the fix for copies of an older version (doctor, --update): the agents' flags, so a user's own
# file of another agent doesn't refuse the run
FIX = "vcharon skill install %s"
# agent -> the folder under the home that holds its user skills
AGENTS = (("claude", (".claude", "skills")), ("codex", (".agents", "skills")))
# the note join, create and a watcher's start print for copies of another version: an
# agent reads the skill before the guide, and an update by an older updater, or any change to
# SKILL.md, leaves the copies as they were (DESIGN, "Create, join, leave, close")
STALE = "note: your vcharon skill at %s is from another version: "
STALE_MANY = "note: your vcharon skills at %s are from another version: "


def text():
    """The shipped SKILL.md."""
    return resources.files(__name__).joinpath("SKILL.md").read_text(encoding="utf-8")


def path(agent):
    """Where agent's copy goes, under this user's home (HOME, or USERPROFILE on Windows)."""
    parts = dict(AGENTS)[agent]
    return os.path.join(platform.home(), *parts, NAME, "SKILL.md")


def _ours(target):
    """'missing', 'same' or 'ours' for a file this command may write; else raises. A link, or
    anything but a file, is never ours: vcharon never makes one there."""
    old = None
    if not os.path.islink(target) and not os.path.isdir(target):
        try:
            with open(target, encoding="utf-8", errors="replace", newline="") as f:
                old = f.read()
        except FileNotFoundError:
            return "missing"
        except OSError as e:
            raise fsops.error(e, target)
    if old is None or MARKER not in old.splitlines():
        raise VCharonError("refused", "%s isn't a skill vcharon wrote (no marker line in it)"
                           % target, hint="move it away or delete it if it's yours to drop, "
                           "or ask your user; then run vcharon skill install again")
    return "same" if old == text() else "ours"


def installed():
    """[(agent, path, current)] for each copy vcharon wrote, told by MARKER: current is True when
    it is this version's text. A missing file, a link, a folder, one that can't be read and one
    without the marker (the user's own) are left out: nothing of vcharon's to refresh there."""
    found = []
    for agent, _parts in AGENTS:
        target = path(agent)
        if os.path.islink(target) or not os.path.isfile(target):
            continue
        try:
            with open(target, encoding="utf-8", errors="replace", newline="") as f:
                old = f.read()
        except OSError:
            continue
        if MARKER in old.splitlines():
            found.append((agent, target, old == text()))
    return found


def stale_note():
    """The note line for the copies vcharon wrote that aren't this version's text (installed()),
    with the fix for exactly those agents, as this box runs vcharon; None when there is none.
    Never raises: a skill that can't be read must not stop the command that asks."""
    try:
        stale = [(agent, target) for agent, target, current in installed() if not current]
    except Exception:  # noqa: BLE001
        return None
    if not stale:
        return None
    # runnable() on the fix alone: the note's own words hold "vcharon skill" too
    head = (STALE if len(stale) == 1 else STALE_MANY) % " and ".join(t for _, t in stale)
    return head + platform.runnable(FIX % " ".join("--" + a for a, _ in stale))


def install(agents, say):
    """Writes the skill for each of agents; every target is checked before any is written."""
    plan = [(agent, path(agent)) for agent in agents]
    states = [(agent, target, _ours(target)) for agent, target in plan]
    body = text()
    for _agent, target, state in states:
        if state == "same":
            say("  unchanged %s" % target)
            continue
        folder = os.path.dirname(target)
        try:
            os.makedirs(folder, exist_ok=True)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(body)
            os.replace(tmp, target)
        except OSError as e:
            raise fsops.error(e, target)
        say("  %s %s" % ("wrote" if state == "missing" else "updated", target))
