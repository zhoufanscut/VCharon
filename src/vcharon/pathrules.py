"""The pure path rules of DESIGN §8 and §10.1, for each receiving OS; no file-system access."""

from __future__ import annotations

import ntpath
import posixpath
import re
import unicodedata

from .proto import VCharonError, quote

STAGE_PREFIX = ".vcharon-stage-"

# Python 3.13's ntpath._reserved_names; ported, since 3.11 and 3.12 don't have it.
RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + ["COM%d" % i for i in range(1, 10)] + ["LPT%d" % i for i in range(1, 10)]
    + ["COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"])

_WINDOWS_BAD = frozenset('<>:"|?*\\') | frozenset(chr(c) for c in range(32))

_OS_NAMES = {"linux": "Linux", "darwin": "macOS", "windows": "Windows"}

# At most this many problems go into an error's detail (and so into the log).
MAX_LISTED = 100

# A mailbox writer's name, and so its folder's name on every client (DESIGN §12): lowercase
# only, so two writers' folders can't fold together on a Windows or macOS client.
WRITER = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,31}\Z")

UNSAFE_HINT = "rename or exclude these paths at the source"
COLLISION_HINT = "rename or exclude one of them at the source"


def show(path):
    """A path as messages print it: as it is, or quoted if it holds anything unprintable."""
    if isinstance(path, str) and path.isprintable():
        return path
    return quote(path)


def _split_problem(path):
    """(parts, None), or (None, why the text rules refuse the path)."""
    if not isinstance(path, str) or not path:
        return None, "an empty path"
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return None, "not valid Unicode"
    if "\0" in path:
        return None, "holds a NUL character"
    parts = tuple(path.split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            return None, "has an empty, \".\" or \"..\" part"
        # stage dir names are vcharon's own, in any case
        if part.casefold().startswith(STAGE_PREFIX):
            return None, "uses a name reserved for vcharon's stage dirs"
    return parts, None


def split(path):
    """The parts of a plan path, after the text rules of §8; otherwise unsafe_path."""
    parts, problem = _split_problem(path)
    if problem:
        raise VCharonError("unsafe_path", "%s: %s" % (show(path), problem), UNSAFE_HINT)
    return parts


def is_reserved_windows(part):
    """Python 3.13's ntpath rule for one name."""
    return part.partition(".")[0].rstrip(" ").upper() in RESERVED


def part_problem(part, osn):
    """Why a receiver on osn can't store this part, or None."""
    if osn == "linux":
        if len(part.encode("utf-8", "surrogatepass")) > 255:
            return "a name is longer than 255 bytes"
        return None
    # APFS and NTFS count UTF-16 code units.
    if len(part.encode("utf-16-le", "surrogatepass")) // 2 > 255:
        return "a name is longer than 255 characters"
    if osn == "windows":
        bad = sorted(set(part) & _WINDOWS_BAD)
        if bad:
            return "a name holds %s, which Windows doesn't allow" % quote("".join(bad))
        if part.endswith((".", " ")):
            return "a name ends with a dot or a space, which Windows drops"
        if is_reserved_windows(part):
            return "%s is a reserved name on Windows" % show(part)
    return None


def writer_problem(name):
    """Why name can't be a mailbox writer's name, the name of its folder at the top of the
    tree (DESIGN §12), or None. A Windows client can't hold a folder with a reserved name."""
    if not WRITER.match(name):
        return ("a writer's name has only lowercase letters, digits, '-' and '_', starts with "
                "a letter or digit, and is at most 32 characters long")
    if is_reserved_windows(name):
        return "%s is a reserved name on Windows, so a Windows client can't hold its folder" % name
    return None


def fold(text, osn):
    """The form under which the receiver's file system takes two names as the same."""
    if osn == "windows":
        return text.casefold()
    if osn == "darwin":
        # Unicode's canonical caseless match; a little stricter than NFD, then casefold.
        return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())
    return text


def refusal(code, problems, hint):
    """One error for a list of problems: the first one, and how many more are in the log."""
    message = problems[0]
    if len(problems) > 1:
        message += " (and %d more; see the log)" % (len(problems) - 1)
    return VCharonError(code, message, hint, detail="\n".join(problems[:MAX_LISTED]))


def check_plan(plan, osn):
    """Every pure rule for a receiver on osn. Returns each entry's parts, in plan order."""
    problems = []
    all_parts = []
    for e in plan.entries:
        parts, problem = _split_problem(e.path)
        if problem is None:
            for part in parts:
                problem = part_problem(part, osn)
                if problem:
                    break
        if problem:
            problems.append("%s: %s" % (show(e.path), problem))
        all_parts.append(parts)
    if problems:
        raise refusal("unsafe_path", problems, UNSAFE_HINT)
    _check_collisions(plan, all_parts, osn)
    return all_parts


def _check_collisions(plan, all_parts, osn):
    problems = []
    seen = set()

    def add(problem):
        if problem not in seen:
            seen.add(problem)
            problems.append(problem)

    on = _OS_NAMES.get(osn, osn)
    puts = set()
    deletes = {}        # folded parts -> the delete's path
    # Tries keyed by folded parts, so each check costs one step per part. A put node is
    # [children, spelling of this part, is_dir, the put that needs it as a directory]; a tree
    # node is [children, the tree delete's path or None].
    put_trie = {}
    tree_trie = {}
    for e, parts in zip(plan.entries, all_parts):
        folded = tuple(fold(p, osn) for p in parts)
        if e.op == "delete":
            other = deletes.get(folded)
            if other == e.path:
                add("%s is deleted twice" % show(e.path))
            elif other is not None:
                add("%s and %s are the same path on %s" % (show(other), show(e.path), on))
            else:
                deletes[folded] = e.path
                if e.tree:
                    level = tree_trie
                    for f in folded[:-1]:
                        level = level.setdefault(f, [{}, None])[0]
                    level.setdefault(folded[-1], [{}, None])[1] = e.path
            continue
        if e.path in puts:
            add("%s is put twice" % show(e.path))
            continue
        puts.add(e.path)
        level = put_trie
        for k, (part, f) in enumerate(zip(parts, folded)):
            is_dir = k < len(parts) - 1 or e.kind == "dir"
            node = level.get(f)
            if node is None:
                node = level[f] = [{}, part, is_dir, e.path if is_dir else None]
            elif node[1] != part:
                add("%s and %s are the same path on %s"
                    % (show("/".join(parts[:k] + (node[1],))), show("/".join(parts[:k + 1])),
                       on))
                break
            elif node[2] != is_dir:
                needs = e.path if is_dir else node[3]
                add("%s is put as a file, but %s needs it to be a directory"
                    % (show("/".join(parts[:k + 1])), show(needs)))
                break
            level = node[0]
    # the tree delete already removes it, and both would be counted
    for folded, path in deletes.items():
        level = tree_trie
        for f in folded[:-1]:
            node = level.get(f)
            if node is None:
                break
            if node[1] is not None:
                add("%s is below %s, which the plan deletes as a tree"
                    % (show(path), show(node[1])))
                break
            level = node[0]
    if problems:
        raise refusal("collision", problems, COLLISION_HINT)


def is_absolute(path, osn):
    """DESIGN §9.1: absolute by osn's rules. On Windows that takes a drive or a share: /srv/x,
    \\x and C:x depend on the current directory, which vcharon never uses, although Python before
    3.13 calls the first two absolute."""
    if osn == "windows":
        return ntpath.isabs(path) and bool(ntpath.splitdrive(path)[0])
    return posixpath.isabs(path)


def win_long_path(root, parts=()):
    """DESIGN §10.1: every absolute path vcharon hands to Windows starts with \\\\?\\ (\\\\?\\UNC\\
    for a share). parts have passed the Windows rules and are joined as they are."""
    if root.startswith("\\\\?\\"):
        path = root
    else:
        norm = ntpath.normpath(root)
        if norm.startswith("\\\\"):
            path = "\\\\?\\UNC\\" + norm[2:]
        else:
            path = "\\\\?\\" + norm
    if parts:
        if not path.endswith("\\"):
            path += "\\"
        path += "\\".join(parts)
    return path


def win_display(path):
    """The same path without the \\\\?\\ prefix, for messages."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path
