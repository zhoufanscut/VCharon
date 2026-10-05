"""vcharon guide [TOPIC]: the agent guide, one Markdown file per topic in this folder, so the
guide an agent reads always matches the vcharon it runs. The files are read with
importlib.resources, which finds them in a checkout, a wheel or a frozen binary alike.

docs/GUIDE.md is made from the same files, for people who read the guide on the web:
`python -m vcharon.guide --write docs/GUIDE.md`; a test fails while the two differ."""

from __future__ import annotations

from importlib import resources

from .. import VERSION
from ..proto import VCharonError

# the topics, in reading order; each is <topic>.md here, and starts with a `# <title>` line
TOPICS = ("start", "member", "post", "watch", "read", "rules", "lead", "end", "errors")
DEFAULT = "start"
GENERATED = ("<!-- Made from src/vcharon/guide/*.md by `python -m vcharon.guide --write "
             "docs/GUIDE.md`: edit those files, then run it again. -->")


def text(topic):
    """The topic's Markdown, as shipped."""
    return resources.files(__name__).joinpath(topic + ".md").read_text(encoding="utf-8")


def title(topic):
    """The topic's title: its first line, without the `# `."""
    first = text(topic).split("\n", 1)[0]
    return first.removeprefix("# ")


def index():
    """The topic list that vcharon guide prints above the start topic."""
    lines = ["vcharon %s: the agent guide. Read a topic with vcharon guide TOPIC:" % VERSION, ""]
    lines += ["  %-7s %s" % (topic, title(topic)) for topic in TOPICS]
    return "\n".join(lines) + "\n"


def unknown(topic):
    return VCharonError("config", "there is no guide topic %r" % topic,
                        hint="the topics are %s and %s: vcharon guide %s"
                        % (", ".join(TOPICS[:-1]), TOPICS[-1], DEFAULT))


def page(topic=None):
    """What vcharon guide prints: the index and the start topic, or one topic."""
    if topic is None:
        return index() + "\n" + text(DEFAULT)
    if topic not in TOPICS:
        raise unknown(topic)
    return text(topic)


def _shift(markdown):
    """Every heading one level down, outside fenced code: a topic's `#` becomes `##`."""
    out = []
    fenced = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def document():
    """docs/GUIDE.md: every topic, in order, under one title."""
    parts = [GENERATED, "", "# The vcharon agent guide", "",
             "What `vcharon guide TOPIC` prints, one section per topic. An agent reads it with "
             "`vcharon guide`, which always matches the vcharon it runs.", ""]
    parts += ["- [%s](#%s): %s" % (topic, _anchor(title(topic)), title(topic))
              for topic in TOPICS]
    for topic in TOPICS:
        parts += ["", _shift(text(topic).rstrip("\n"))]
    return "\n".join(parts) + "\n"


def _anchor(heading):
    """GitHub's anchor for a heading: lowercase, spaces to -, other marks dropped."""
    out = []
    for c in heading.lower():
        if c.isalnum() or c in "-_":
            out.append(c)
        elif c == " ":
            out.append("-")
    return "".join(out)
