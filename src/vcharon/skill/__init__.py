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
# agent -> the folder under the home that holds its user skills
AGENTS = (("claude", (".claude", "skills")), ("codex", (".agents", "skills")))


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
