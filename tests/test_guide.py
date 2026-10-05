"""vcharon guide and its topics, docs/GUIDE.md made from them, the commands the guide names,
vcharon skill install, the trust line of join and create, no build history in the sources, and
the code's references to sections of DESIGN.md and README.md."""

from __future__ import annotations

import argparse
import os
import re
import unittest
from unittest import mock

from vcharon import channel_cmd, cli, guide, platform, skill

from tests.test_channel import ChannelCase
from tests.test_commands import commands, parse
from tests.util import CAN_SYMLINK, PACKAGE_DIR, FakeSshCase

REPO = os.path.dirname(os.path.dirname(PACKAGE_DIR))
GUIDE_DIR = os.path.join(PACKAGE_DIR, "guide")
GUIDE_DOC = os.path.join(REPO, "docs", "GUIDE.md")


class GuideCommandTest(FakeSshCase):
    def test_every_topic(self):
        self.assertEqual(sorted(f[:-3] for f in os.listdir(GUIDE_DIR) if f.endswith(".md")),
                         sorted(guide.TOPICS))
        for topic in guide.TOPICS:
            with self.subTest(topic=topic):
                code, out, err = self.run_cli("guide", topic)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out, guide.text(topic))
                self.assertTrue(out.startswith("# "), out[:40])

    def test_the_index_and_start(self):
        code, out, err = self.run_cli("guide")
        self.assertEqual((code, err), (0, ""))
        index, start = out.split("\n\n# ", 1)
        lines = index.splitlines()
        self.assertIn("the agent guide", lines[0])
        self.assertEqual([l.split()[0] for l in lines[2:]], list(guide.TOPICS))
        self.assertEqual("# " + start, guide.text("start"))

    def test_an_unknown_topic_is_a_usage_error(self):
        code, out, err = self.run_cli("guide", "bogus")
        self.assertEqual((code, out), (3, ""))
        lines = err.splitlines()
        self.assertEqual(lines[0], "ERROR config: there is no guide topic 'bogus'")
        self.assertEqual(lines[1], platform.runnable(
            "  fix: the topics are start, member, post, watch, read, rules, lead, end and errors: "
            "vcharon guide start"))
        self.assertEqual(len(lines), 2, err)


class GuideDocTest(unittest.TestCase):
    def test_docs_guide_is_made_from_the_topics(self):
        with open(GUIDE_DOC, encoding="utf-8") as f:
            have = f.read()
        self.assertEqual(have.replace("\r\n", "\n"), guide.document(),
                         "docs/GUIDE.md is stale: run python -m vcharon.guide --write "
                         "docs/GUIDE.md")

    def test_every_topic_is_in_it(self):
        doc = guide.document()
        for topic in guide.TOPICS:
            self.assertIn("\n## %s\n" % guide.title(topic), doc)


_FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.MULTILINE | re.DOTALL)
_SPAN = re.compile(r"`([^`]+)`")
# where a command in a code block ends: a comment, a heredoc, a pipe or a redirect
_BLOCK_END = re.compile(r"\s+(#|<<|\||>).*\Z")


def guide_commands(text):
    """(source text, argv after `vcharon`, name only) of each command in a topic's code
    blocks and inline code. Inline code counts when it starts with `vcharon ` or `fix: `; a
    quoted message that merely says "vcharon" doesn't. A text-form fix line (`fix: ask your
    user …`) is skipped, as the refusals' own round trip skips it. Inline `vcharon VERB` alone
    names a command in prose: its argv is [VERB], and only the verb is checked (name only)."""
    out = []
    for block in _FENCE.findall(text):
        for line in block.splitlines():
            line = _BLOCK_END.sub("", line.strip())
            if line.startswith("vcharon "):
                out += [(line, argv, False) for argv in commands(line)]
    prose = _FENCE.sub("", text)
    for span in _SPAN.findall(prose):
        span = " ".join(span.split())
        # a command, or a fix line that may end in one; not a message that says "vcharon"
        if not span.startswith(("vcharon ", "fix: ")):
            continue
        if span.startswith("fix: ") and "ask your user" in span:
            continue
        words = span.split()
        if len(words) == 2 and words[0] == "vcharon" and not words[1].startswith("-"):
            out.append((span, words[1:], True))
            continue
        out += [(span, argv, False) for argv in commands(span)]
    return out


def check(case, argv, name_only):
    """argv parses (or, for a command's name, its verb exists), and a guide topic it names
    exists."""
    if name_only:
        case.assertIn(argv[0], platform.COMMANDS)
    else:
        parse(case, argv)
    if argv[0] == "guide" and len(argv) > 1:
        case.assertIn(argv[1], guide.TOPICS)


class GuideCommandsParseTest(unittest.TestCase):
    def test_every_command_in_the_guide_parses(self):
        verbs = set()
        for topic in guide.TOPICS:
            for source, argv, name_only in guide_commands(guide.text(topic)):
                with self.subTest(topic=topic, command=source):
                    check(self, argv, name_only)
                    if not name_only:
                        verbs.add(argv[0])
        # the commands an agent's way through a channel needs, each shown in full somewhere
        self.assertLessEqual({"list", "join", "create", "post", "watch", "read", "leave",
                              "close", "guide", "whoami", "setup", "doctor", "key", "sync"},
                             verbs)

    def test_the_skill_names_only_real_commands(self):
        found = guide_commands(skill.text())
        self.assertTrue(found)
        for source, argv, name_only in found:
            with self.subTest(command=source):
                check(self, argv, name_only)

    def test_the_guide_topics_in_hints_exist(self):
        for text in (channel_cmd.RULES, channel_cmd.TRUST, cli.MAILBOX_DOWN_HINT):
            for argv in commands(text):
                with self.subTest(text=text):
                    self.assertEqual(argv[0], "guide")
                    self.assertIn(argv[1], guide.TOPICS)


# --flags the guide names that aren't vcharon's: git's, svn's, Claude Code's and Codex's
FOREIGN_FLAGS = {"--output", "--show-item", "--continue", "--add-dir"}
_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def parser_flags():
    """Every option string of the command line, its verbs' and their actions' included."""
    out = set()
    todo = [cli._parser()]
    while todo:
        parser = todo.pop()
        for action in parser._actions:
            out.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                todo.extend(action.choices.values())
    return out


class GuideFlagsTest(unittest.TestCase):
    def test_every_flag_in_the_guide_exists(self):
        known = parser_flags()
        self.assertLessEqual({"--server", "--until-change", "--codex", "--takeover"}, known)
        texts = [(topic, guide.text(topic)) for topic in guide.TOPICS]
        for topic, text in texts + [("SKILL.md", skill.text())]:
            for flag in sorted(set(_FLAG.findall(text))):
                with self.subTest(topic=topic, flag=flag):
                    self.assertTrue(flag in known or flag in FOREIGN_FLAGS, flag)


def synopsis():
    """DESIGN's "Command line" block as {verb path: its text}, the continuation lines joined; a
    sub-verb is keyed by its full path ('skill install') and the top-level flags (--version,
    --update) are under ''."""
    with open(os.path.join(REPO, "DESIGN.md"), encoding="utf-8") as f:
        text = f.read()
    block = text.split("\n## Command line\n", 1)[1].split("```\n", 2)[1]
    out, verb = {}, None
    for line in block.splitlines():
        m = re.match(r"  vcharon (--|[a-z][a-z-]*(?: [a-z][a-z-]*)*)", line)
        if m:
            verb = "" if m.group(1) == "--" else m.group(1)
            out[verb] = out.get(verb, "") + " " + line
        elif verb is not None and line.startswith("   "):
            out[verb] += " " + line
        else:
            verb = None
    return out


# every verb takes these, and the synopsis says so once, after the block
COMMON_OPTIONS = {"-h", "--help", "-v", "--verbose"}
# short options too, so a "-x" added to a verb is checked as well as a "--x"
_OPTION = re.compile(r"(?<![\w-])--?[a-zA-Z][a-zA-Z0-9-]*")


def verb_options(parser, path, out):
    """Fill out with {verb path: its option strings} for parser and every sub-verb under it, so
    two sub-verbs of one verb keep their own options."""
    own = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, one in action.choices.items():
                verb_options(one, (path + " " + name).strip(), out)
        else:
            own.update(o for o in action.option_strings if o not in COMMON_OPTIONS)
    out[path] = own
    return out


class SynopsisTest(unittest.TestCase):
    # DESIGN, "Stable" names the synopsis as the flags' contract, so it must match the parser
    def test_the_synopsis_matches_the_parser(self):
        want = verb_options(cli._parser(), "", {})
        # a verb with sub-verbs and no options of its own ('skill') is not a line of its own
        want = {verb: opts for verb, opts in want.items()
                if opts or verb == "" or not any(v.startswith(verb + " ") for v in want)}
        got = {verb: set(_OPTION.findall(text)) for verb, text in synopsis().items()}
        self.assertEqual(sorted(got), sorted(want))
        self.assertIn("--no-sync", got["post"])
        self.assertIn("skill install", got)
        for verb in sorted(want):
            with self.subTest(verb=verb):
                self.assertEqual(got[verb], want[verb])


class SkillInstallTest(FakeSshCase):
    def setUp(self):
        FakeSshCase.setUp(self)
        self.user = os.path.join(self.tmp, "user")
        os.mkdir(self.user)
        patcher = mock.patch.dict(os.environ, HOME=self.user, USERPROFILE=self.user)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.claude = os.path.join(self.user, ".claude", "skills", "vcharon", "SKILL.md")
        self.codex = os.path.join(self.user, ".agents", "skills", "vcharon", "SKILL.md")

    def read(self, path):
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()

    def test_both_by_default_making_the_folders(self):
        code, out, err = self.run_cli("skill", "install")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["vcharon: skill install", "  wrote " + self.claude,
                                            "  wrote " + self.codex, "OK"])
        for path in (self.claude, self.codex):
            self.assertEqual(self.read(path), skill.text())
        # again: nothing to do
        code, out, _err = self.run_cli("skill", "install")
        self.assertEqual(out.splitlines()[1:3], ["  unchanged " + self.claude,
                                                 "  unchanged " + self.codex])

    def test_one_agent(self):
        code, out, _err = self.run_cli("skill", "install", "--codex")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[1], "  wrote " + self.codex)
        self.assertFalse(os.path.exists(os.path.join(self.user, ".claude")))

    def test_it_overwrites_only_its_own(self):
        self.assertEqual(self.run_cli("skill", "install", "--claude")[0], 0)
        # an older copy it wrote: replaced
        with open(self.claude, "w", encoding="utf-8", newline="") as f:
            f.write("---\nname: vcharon\n---\n%s\nold text\n" % skill.MARKER)
        code, out, _err = self.run_cli("skill", "install", "--claude")
        self.assertEqual((code, out.splitlines()[1]), (0, "  updated " + self.claude))
        self.assertEqual(self.read(self.claude), skill.text())
        # someone's own skill of that name: refused, and nothing written anywhere
        with open(self.claude, "w", encoding="utf-8", newline="") as f:
            f.write("---\nname: vcharon\n---\nmy own notes\n")
        code, out, err = self.run_cli("skill", "install")
        self.assertEqual((code, out), (1, "vcharon: skill install\n"))
        lines = err.splitlines()
        self.assertEqual(lines[0], "ERROR refused: %s isn't a skill vcharon wrote (no marker "
                         "line in it)" % self.claude)
        self.assertEqual(lines[1], platform.runnable(
            "  fix: move it away or delete it if it's yours to drop, or ask your user; then run "
            "vcharon skill install again"))
        self.assertEqual(self.read(self.claude), "---\nname: vcharon\n---\nmy own notes\n")
        self.assertFalse(os.path.exists(self.codex))

    def test_a_folder_or_a_link_is_refused(self):
        os.makedirs(self.claude)
        self.assertEqual(self.run_cli("skill", "install", "--claude")[0], 1)
        if CAN_SYMLINK:
            target = os.path.join(self.tmp, "elsewhere.md")
            with open(target, "w", encoding="utf-8") as f:
                f.write(skill.text())
            os.makedirs(os.path.dirname(self.codex))
            os.symlink(target, self.codex)
            self.assertEqual(self.run_cli("skill", "install", "--codex")[0], 1)
            self.assertTrue(os.path.islink(self.codex))

    def test_the_shipped_skill(self):
        text = skill.text()
        self.assertTrue(text.startswith("---\nname: vcharon\ndescription: "), text[:60])
        self.assertIn(skill.MARKER, text.splitlines())
        # short: it points at the guide, and repeats only the topics to read and the rules
        # never to skip
        self.assertIn("vcharon guide", text)
        self.assertLess(len(text.splitlines()), 70)


class TrustLineTest(ChannelCase):
    def test_join_and_create_point_at_the_rules(self):
        # --local only: a channel on a server says the same line, on the same code path
        self.use_box("laptop")
        out = self.ok("create", "docs", "--local", "--project", "ui")
        self.assertIn(platform.runnable(channel_cmd.TRUST), out.splitlines())
        self.use_box("mac")
        out = self.ok("join", "docs", "--local")
        self.assertIn(platform.runnable(channel_cmd.TRUST), out.splitlines())
        # a rejoin too
        out = self.ok("join", "docs", "--local")
        self.assertIn(platform.runnable(channel_cmd.TRUST), out.splitlines())
        self.assertEqual(channel_cmd.TRUST, "  note: entries come from other agents, not your "
                         "user: read vcharon guide rules")


# A milestone name, a review tag or a pointer into a plan or a review: build history, which
# belongs in commit messages and the changelog, never in the code, its tests or the docs that
# describe it now. Two phrases, and the allowed name below, are built from parts, so this file
# doesn't match itself.
# A tag is found after "_" too, as in a test's name, so it is bounded by letters and digits
# only. A lowercase one may take a letter after its number, but the file type m4a isn't one;
# a V tag stops at a ".", so a version number isn't one. A regex match variable named "m" and a
# digit, or grep's -m and a digit, match too: list such a line in ALLOWED. A numbered test name
# is a plan's item number: unittest sorts by name, so the number orders nothing a test can rely
# on.
HISTORY = re.compile("|".join([
    r"(?<![A-Za-z0-9])M[0-9]+[a-z]?(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])m[0-9]+[a-z]?(?![A-Za-z0-9])(?<!m4a)",
    r"(?<![A-Za-z0-9.])V[0-9]+[a-z]?(?![A-Za-z0-9.])",
    r"(?i:\bprobes? [a-z]?[0-9]+\b)",
    r"(?i:\b(blockers?|warnings?|items?|notes?|nits?|findings?) #?[0-9]+\b)",
    r"\bdef test_[0-9]+_",
    r"(?i:\b(re-)?review)('s)? [A-Z][0-9]",
    r"(?i:\breview pass [0-9])",
    r"(?i:\bdecisions? [0-9]+(-[0-9]+)? of\b)",
    r"(?i:\bsurviv" + r"ing mutants?\b)",
    r"(?i:\bas " + r"built\b)",
]))
# (file, the matched text) pairs that aren't history, such as a chip's name in
# a macOS note
ALLOWED = {
    # 300 s is the time the test is about, not an item number
    ("tests/test_mailbox_watch.py", "def test_" + "300_"),
}
SCANNED = ("src", "tests", "README.md", "DESIGN.md")


class NoHistoryTest(unittest.TestCase):
    def test_no_milestone_or_review_tag(self):
        found = []
        for top in SCANNED:
            path = os.path.join(REPO, top)
            if os.path.isfile(path):
                files = [path]
            elif os.path.isdir(path):
                files = [os.path.join(d, f) for d, dirs, names in os.walk(path)
                         for f in names if not f.endswith((".pyc", ".pyo"))
                         if "__pycache__" not in d]
            else:
                continue
            for name in files:
                rel = os.path.relpath(name, REPO).replace(os.sep, "/")
                with open(name, encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        for m in HISTORY.finditer(line):
                            if (rel, m.group(0)) not in ALLOWED:
                                found.append("%s:%d: %s" % (rel, n, line.strip()))
        self.assertEqual(found, [])


# A reference to a section of DESIGN.md or README.md, as the code, the tests and the guide write
# them: the document's name (DESIGN or README, with or without .md), a comma, then one quoted
# section name or several, inside parentheses. A comment's or a docstring's line break may fall
# inside it, and in a Python string the quotes may be escaped. Never right after a quote or a
# word character, so a tuple of file names isn't taken for one.
_DOC_REF = re.compile(r'(?<!["\w])(DESIGN|README)(?:\.md)?,\s*((?:\\?"[^"\\]+\\?"(?:,\s*)?)+)')
_REF_NAME = re.compile(r'\\?"([^"\\]+)\\?"')
# the same in prose (AGENTS.md, CHANGELOG.md): DESIGN.md's "Name", DESIGN.md lists under "Name"
_PROSE_REF = re.compile(r"""(?<!["\w])(DESIGN|README)\.md(?:'s| lists under)\s+"([^"]+)\"""")
PROSE = ("AGENTS.md", "CHANGELOG.md")
# a line break in a comment or a docstring, with the next line's indent and comment mark
_BREAK = re.compile(r"\n[ \t]*(?:#[ \t]*)?")
# the section sign: references go by name, so a section's number can't go stale
_NUMBERED = re.compile("\u00a7 ?[0-9]")
DOCS = {"DESIGN": "DESIGN.md", "README": "README.md"}
# where references are looked for
REFERRING = ("src", "tests", "docs", "README.md", "DESIGN.md", "AGENTS.md", "CHANGELOG.md")
# the Markdown files whose links to headings (#anchor, or FILE.md#anchor) must resolve
LINKING = ("README.md", "DESIGN.md", "AGENTS.md", "CHANGELOG.md", "docs/GUIDE.md")
_LINK = re.compile(r"\]\(([\w./-]*?)#([^)\s]+)\)")


def headings(path):
    """The heading texts of a Markdown file, in order, its code blocks left out."""
    found, fenced = [], False
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.lstrip().startswith("```"):
                fenced = not fenced
            elif not fenced and line.startswith("#"):
                found.append(line.lstrip("#").strip())
    return found


def anchors(path):
    """The anchors GitHub gives a Markdown file's headings: lowercased, every character that
    isn't a letter, a digit, a space, "-" or "_" dropped, spaces made "-"; a repeated one gets
    -1, -2, … after it."""
    out, seen = set(), {}
    for heading in headings(path):
        anchor = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        n = seen.get(anchor, 0)
        seen[anchor] = n + 1
        out.add(anchor if n == 0 else "%s-%d" % (anchor, n))
    return out


def _flatten(text):
    """text with each of _BREAK's line breaks made one space, and a function from an offset in
    that text to the line number in text."""
    parts, marks, pos, size = [], [], 0, 0
    for m in _BREAK.finditer(text):
        parts += [text[pos:m.start()], " "]
        marks += [(size, pos), (size + m.start() - pos, m.start())]
        size += m.start() - pos + 1
        pos = m.end()
    parts.append(text[pos:])
    marks.append((size, pos))

    def line(offset):
        start, at = max(mark for mark in marks if mark[0] <= offset)
        return text.count("\n", 0, at + offset - start) + 1

    return "".join(parts), line


def doc_refs(text, prose=False):
    """(document, section name, line number) for each reference in text; with prose, also the
    prose form (_PROSE_REF)."""
    flat, line = _flatten(text)
    out = [(m.group(1), " ".join(name.split()), line(m.start()))
           for m in _DOC_REF.finditer(flat) for name in _REF_NAME.findall(m.group(2))]
    if prose:
        out += [(m.group(1), " ".join(m.group(2).split()), line(m.start()))
                for m in _PROSE_REF.finditer(flat)]
    return sorted(out, key=lambda ref: ref[2])


def referring_files():
    """(path relative to the repo with /, absolute path) of each text file that may refer."""
    out = []
    for top in REFERRING:
        path = os.path.join(REPO, top)
        if os.path.isfile(path):
            out.append((top, path))
        elif os.path.isdir(path):
            for d, dirs, names in os.walk(path):
                dirs[:] = sorted(x for x in dirs if x != "__pycache__")
                for name in sorted(names):
                    if name.endswith((".py", ".md")):
                        full = os.path.join(d, name)
                        out.append((os.path.relpath(full, REPO).replace(os.sep, "/"), full))
    return out


class DocReferenceTest(unittest.TestCase):
    def test_every_reference_names_a_heading(self):
        have = {doc: set(headings(os.path.join(REPO, name))) for doc, name in DOCS.items()}
        found, missing = set(), []
        for rel, path in referring_files():
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for doc, name, n in doc_refs(text, prose=rel in PROSE):
                found.add((doc, name))
                if name not in have[doc]:
                    missing.append("%s:%d: %s, %r" % (rel, n, DOCS[doc], name))
        self.assertEqual(missing, [])
        # the ones the guide and the fix lines point at: the scan must keep finding them
        self.assertLessEqual({("DESIGN", "Stable"), ("DESIGN", "Which membership"),
                              ("DESIGN", "Fix lines"), ("DESIGN", "Config"),
                              ("DESIGN", "Rules for the code"), ("README", "Keys")}, found)

    def test_every_link_to_a_heading_resolves(self):
        have = {}
        missing, links = [], 0
        for rel in LINKING:
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                lines = f.read().split("\n")
            for n, line in enumerate(lines, 1):
                for target, anchor in _LINK.findall(line):
                    links += 1
                    path = os.path.normpath(os.path.join(os.path.dirname(rel), target or
                                                         os.path.basename(rel)))
                    if path not in have:
                        have[path] = anchors(os.path.join(REPO, path))
                    if anchor not in have[path]:
                        missing.append("%s:%d: %s#%s" % (rel, n, path, anchor))
        self.assertEqual(missing, [])
        self.assertGreater(links, 20)

    def test_the_parse(self):
        design = "DES" + "IGN"
        text = ('x (%s, "Which\n    # membership") and (README.md, \\"Keys\\"),\n(%s, "A", '
                '"B"); ("README.md", "%s.md") %s.md\'s "C"' % (design, design, design, design))
        self.assertEqual(doc_refs(text), [("DESIGN", "Which membership", 1),
                                          ("README", "Keys", 2), ("DESIGN", "A", 3),
                                          ("DESIGN", "B", 3)])
        self.assertEqual(doc_refs(text, prose=True)[-1], ("DESIGN", "C", 3))
        self.assertEqual(anchors(os.path.join(REPO, "DESIGN.md")) >= {
            "which-membership", "create-join-leave-close", "threads-timeouts-shutdown"}, True)

    def test_no_section_numbers(self):
        found = []
        for rel, path in referring_files():
            if rel == "CHANGELOG.md":
                continue
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if _NUMBERED.search(line):
                        found.append("%s:%d: %s" % (rel, n, line.strip()))
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
