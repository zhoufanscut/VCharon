"""The path source: a file or a directory tree (DESIGN §9.2), on either end."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import stat

from .. import fsops, pathrules, platform, plugin, proto
from ..fsops import DIR, FILE, LINK, OTHER
from ..plan import Plan, delete, good_mtime, put_dir, put_file
from ..plugin import Option, choice
from ..proto import VCharonError, quote

NAME = "path"
KINDS = ("source",)
ENDS = ("local", "remote")
NEEDS = ()
CAPS = ()
OPTIONS = {"path": Option(str, required=True), "keep_name": Option(bool, default=False),
           "exclude": Option(list, default=[]),
           "symlinks": Option(choice("error", "skip"), default="error"),
           "prune": Option(bool, default=False), "allow_empty": Option(bool, default=False),
           # set by a mailbox section's down job only (DESIGN §12)
           "mailbox_me": Option(str, default=None)}

# options only a channel section sets: left out of the list of options
HIDDEN = ("mailbox_me",)

NAME_HINT = "rename it at the source"
LINKS_HINT = "remove them, or skip them with symlinks = skip"
# a directory below the root it can't list; a mailbox job swaps this one (cli.SOURCE_HINTS)
LIST_HINT = "fix its permissions, or exclude it"
# the root itself, which no exclude and no mailbox writer fixes
ROOT_LIST_HINT = "fix its permissions"
# a missing root; a channel section's down swaps it (channel_cmd.CHANNEL_GONE_HINT)
MISSING_HINT = "check the path"
# ferry run gives every state_mismatch its own hint, which names the job
STATE_HINT = "check the target; then reset the job's state, and run it with --full"
EMPTY_HINT = "check that its disk is mounted; if it's really empty, set from.allow_empty = yes"


def check_options(options):
    me = options["mailbox_me"]
    if me is not None:
        problem = pathrules.writer_problem(me)
        if problem:
            raise VCharonError("bad_options", "from.mailbox_me: %s" % problem,
                               "fix mailbox.me")
        if options["keep_name"]:
            raise VCharonError("bad_options", "from.mailbox_me can't go with keep_name",
                               "leave out keep_name")
    for item in options["exclude"]:
        # fnmatch reads \ as / on Windows only; one meaning on every end is safer.
        if "\\" in item:
            raise VCharonError("bad_options", "from.exclude: %s: use / in patterns, not \\"
                               % pathrules.show(item),
                               "fix from.exclude")
        # Patterns are relative to the source directory; a leading or trailing / would
        # never match anything.
        if item.startswith("/") or item.endswith("/"):
            raise VCharonError("bad_options", "from.exclude: %s can't start or end with /"
                               % pathrules.show(item),
                               "fix from.exclude; patterns are relative to the source "
                               "directory, as in docs/*.md")


def _escaped(path):
    """A path whose name isn't valid UTF-8, with its bad bytes shown as \\xNN."""
    return os.fsencode(path).decode("utf-8", "backslashreplace")


def _changed(where):
    return VCharonError("vanished", "%s changed while it was being listed" % where, "run again")


def _walk_error(e, where, hint=LIST_HINT):
    """Decision 10 of the M3 plan: listing or entering the directory at where failed. hint:
    for a directory it can't list, ROOT_LIST_HINT at the root."""
    if isinstance(e, VCharonError):
        # the handle's own refusals: the directory is now a link, or no directory at all
        if e.code in ("unsafe_path", "kind_change"):
            return _changed(where)
        return e
    if e.errno in (errno.EACCES, errno.EPERM):
        return VCharonError("permission", "can't list %s: %s" % (where, e.strerror or e), hint)
    if isinstance(e, FileNotFoundError) or e.errno in (errno.ENOTDIR, errno.ELOOP):
        return _changed(where)
    return fsops.error(e, where)


def _gone(shown):
    return VCharonError("vanished", "%s is gone, or isn't a regular file any more" % shown,
                        "it changed during the run; run again")


# What opening a planned file gives when something else is there now: a directory or a link
# on the way, or a socket (ENXIO on Linux, EOPNOTSUPP on macOS) or a device (ENODEV).
_GONE_ERRNOS = tuple(getattr(errno, name) for name in ("ENOTDIR", "ELOOP", "ENXIO",
                                                       "EOPNOTSUPP", "ENODEV")
                     if hasattr(errno, name))


def _read_error(e, shown):
    """What opening or reading the file shown as shown raised, as open() reports it."""
    if isinstance(e, VCharonError):
        if e.code in ("vanished", "unsafe_path", "kind_change"):
            return _gone(shown)
        return e
    if getattr(e, "winerror", None) in (32, 33):
        # another program has it open; Windows sets errno to EACCES for this too
        return fsops.error(e, shown)
    if isinstance(e, FileNotFoundError) or e.errno in _GONE_ERRNOS:
        return _gone(shown)
    if e.errno in (errno.EACCES, errno.EPERM):
        return VCharonError("permission", "%s: %s" % (shown, e.strerror or e),
                            "check its permissions at the source")
    return fsops.error(e, shown)


def _malformed(what):
    return VCharonError("state_mismatch", "the saved state is malformed: %s" % what, STATE_HINT)


def _sent_of(state):
    """The sent map of a saved state, {"sent": {path: "d" or [size, mtime, exec]}}, checked
    (decision 2 of the M4 plan); {} means nothing was sent yet."""
    if not isinstance(state, dict):
        raise _malformed("it isn't an object")
    for key in state:
        if key != "sent":
            raise _malformed("an unknown key %s" % quote(key))
    sent = state.get("sent", {})
    if not isinstance(sent, dict):
        raise _malformed("sent isn't an object")
    for path, value in sent.items():
        if not isinstance(path, str) or not path:
            raise _malformed("sent has the path %s" % quote(path))
        # With prune it becomes a delete, so it must be a plan path (DESIGN §8).
        try:
            pathrules.split(path)
        except VCharonError as e:
            raise _malformed("sent holds %s" % e.message)
        if value == "d":
            continue
        if not (isinstance(value, list) and len(value) == 3
                and isinstance(value[0], int) and not isinstance(value[0], bool)
                and value[0] >= 0 and not isinstance(value[1], bool) and good_mtime(value[1])
                and (value[2] is None or isinstance(value[2], bool))):
            raise _malformed("sent has %s for %s, not \"d\" or [size, mtime, exec]"
                             % (quote(value), pathrules.show(path)))
    return sent


def _partners(walked, puts, deletes):
    """The walk indexes to plan too, so that a receiver that folds names (Windows, macOS) sees
    what a full plan would show it: every walked path spelled differently from a planned put,
    a planned delete or a directory above one, that folds to the same under either OS's rule.
    Without them, README (sent, unchanged) and a new readme would pass its collision check and
    readme would replace README; a delete of a stale Foo.cpp would unlink the live foo.cpp."""
    osns = ("windows", "darwin")
    prefixes = set()
    for path in [walked[i].path for i in puts] + [e.path for e in deletes]:
        parts = path.split("/")
        for k in range(1, len(parts) + 1):
            prefixes.add("/".join(parts[:k]))
    # Folding goes part by part, so two paths fold alike only if their last names do. Only
    # those walked entries are folded in full: a whole big tree for every small run costs a
    # second and tens of MB.
    names = {(osn, pathrules.fold(q.rpartition("/")[2], osn)) for q in prefixes for osn in osns}
    index = {}
    for i, e in enumerate(walked):
        name = e.path.rpartition("/")[2]
        for osn in osns:
            if (osn, pathrules.fold(name, osn)) in names:
                index.setdefault((osn, pathrules.fold(e.path, osn)), []).append(i)
    planned = set(puts)
    found = set()
    for prefix in prefixes:
        for osn in osns:
            for i in index.get((osn, pathrules.fold(prefix, osn)), ()):
                if walked[i].path != prefix and i not in planned:
                    found.add(i)
    return found


def _value(e):
    """How sent records a put (DESIGN §9.2)."""
    if e.kind == "dir":
        return "d"
    return [e.size, e.mtime, e.executable]


def _counted(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


class Source(plugin.Source):
    # Which directory handles to use: None is PathDir on Windows and FdDir elsewhere. Tests
    # set "path" to run the Windows code on POSIX.
    impl = None

    def __init__(self, ctx, options):
        plugin.Source.__init__(self, ctx, options)
        self._planned = False
        self._entries = []
        # plan index -> the file's parts below the root handle
        self._files = {}
        self._root = None
        # [(name, handle)] from the root down to the directory open() used last
        self._chain = []
        # for state_after, once plan() ran with a state: sent after the exclude drop, each
        # put's path -> its sent value, and the deletes' paths
        self._base = None
        self._values = {}
        self._deletes = set()
        singles, wholes = [], []
        for pattern in options["exclude"]:
            (wholes if "/" in pattern else singles).append(pattern)
        self._singles, self._wholes = singles, wholes
        # a mailbox's down: the writer's name, the names left out at the top this run (for
        # the log), and every name the walk met at the top, kept or left out
        self._me = options.get("mailbox_me")
        self._strays = []
        self._top_names = set()
        # the patterns under each folding receiver's rule, for stale spellings in sent
        self._folded = [(osn, [pathrules.fold(x, osn) for x in singles],
                         [pathrules.fold(x, osn) for x in wholes])
                        for osn in ("windows", "darwin")]

    def _handles(self):
        impl = self.impl or ("path" if fsops.WINDOWS else "fd")
        return fsops.FdDir if impl == "fd" else fsops.PathDir

    def _exec(self, st):
        # the owner's execute bit, as git does; a source on Windows leaves it out
        if self.ctx.os == "windows":
            return None
        return bool(st.st_mode & 0o100)

    # --- plan ---

    def plan(self, state, full=False):
        """state None (a one-off pull): everything, as in M3, and no state back. A dict: only what
        changed since that state, and the new one (DESIGN §9.2). full: everything, each file
        with its sha256 (DESIGN §10.5)."""
        if self._planned:
            raise VCharonError("internal", "plan() runs once per source")
        self._planned = True
        sent = None if state is None else _sent_of(state)
        ctx = self.ctx
        given = ctx.resolve(self.options["path"], "from.path")
        abs_path = os.path.abspath(given)
        try:
            abs_path.encode("utf-8")
        except UnicodeEncodeError:
            # It would reach the identity and the plan's names.
            raise pathrules.refusal("unsafe_path", ["%s: the path isn't valid UTF-8"
                                                    % _escaped(abs_path)], NAME_HINT)
        # The name comes from the path as given (decision 4 of the M3 plan): cp of a symlink
        # named current.log sends current.log. Only the top is ever followed.
        name = os.path.basename(abs_path)
        real = os.path.realpath(abs_path)
        try:
            st = os.stat(real)
        except OSError as e:
            if isinstance(e, FileNotFoundError) or e.errno == errno.ENOTDIR:
                raise VCharonError("not_found", "%s doesn't exist" % given, MISSING_HINT)
            raise fsops.error(e, given)
        k = fsops.kind(st)
        try:
            if k == FILE:
                entries, files, notes = self._plan_file(real, name, given, abs_path, full)
                top = None
            elif k == DIR:
                entries, files, notes = self._plan_dir(real, name, abs_path, full)
                top = name if self.options["keep_name"] else None
            else:
                raise pathrules.refusal("unsafe_path", ["%s is neither a file nor a directory"
                                                        % given],
                                        "point from.path at a file or a directory")
            new_state = None
            if sent is not None:
                entries, files, new_state = self._since(sent, entries, files, k == FILE, top,
                                                        abs_path, full)
        except BaseException:
            self._close_root()
            raise
        self._entries = entries
        self._files = files
        identity = {"end": ctx.end}
        if ctx.end == "remote":
            identity["machine"] = platform.machine_id()
        identity["path"] = abs_path
        # The file sink refuses a directory, even when its plan holds one file or none.
        identity["kind"] = "file" if k == FILE else "dir"
        return Plan(entries, identity=identity, state=new_state, notes=notes)

    def _open_root(self, path, where):
        # Neither the owner rule nor the device rule applies to a source (decision 5).
        try:
            self._root = self._handles().open_root(path, same_device=False)
        except (OSError, VCharonError) as e:
            raise _walk_error(e, where, ROOT_LIST_HINT)
        return self._root

    def _plan_file(self, real, name, given, abs_path, full):
        # The root handle is the file's directory, so open() reads it without following a
        # link, as it does in a tree. It's only searched, never listed: a directory with x
        # but no r (a drop box) still lets the file be read.
        parent, base = os.path.split(real)
        try:
            root = self._root = self._handles().open_root(parent, same_device=False,
                                                          list=False)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EPERM):
                raise VCharonError("permission", "can't read %s: %s" % (given, e.strerror or e),
                                   "check the permissions of its directory")
            raise _walk_error(e, parent)
        try:
            st = root.lstat(base)
        except OSError as e:
            raise _walk_error(e, parent)
        if st is None or fsops.kind(st) != FILE:
            raise _changed(abs_path)
        digest = None
        if full:
            try:
                st, digest = self._hash(root, base, name)
            except FileNotFoundError:
                raise _changed(abs_path)
        return [put_file(name, st.st_size, st.st_mtime, self._exec(st), digest)], {0: (base,)}, []

    def _plan_dir(self, real, name, abs_path, full):
        entries = []
        prefix = ()
        if self.options["keep_name"]:
            if not name:
                raise VCharonError("bad_options", "from.path: keep_name needs a directory with a "
                                   "name, and %s is a file system root" % abs_path,
                                   "name the directory to copy")
            prefix = (name,)
            entries.append(put_dir(name))
        root = self._open_root(real, abs_path)
        return self._walk(root, prefix, abs_path, entries, full)

    def _top_left_out(self, name, is_dir):
        """A mailbox's down plans, at the top of the tree, only the folders of the other
        writers: a directory whose name is a valid writer's name, not <me>'s (DESIGN §12).
        Valid names are lowercase, so <me>'s folder in another case is left out too, and no
        two planned names can fold together on a client."""
        return not is_dir or name == self._me or pathrules.writer_problem(name) is not None

    def _excluded(self, parts):
        # A pattern without / matches one part; the parts above were checked on the way down.
        if any(fnmatch.fnmatch(parts[-1], p) for p in self._singles):
            return True
        if self._wholes:
            rel = "/".join(parts)
            return any(fnmatch.fnmatch(rel, p) for p in self._wholes)
        return False

    def _scan(self, d, where, hint=LIST_HINT):
        """d's entries, sorted by name (decision 6) and last first, for pop()."""
        try:
            items = d.scan()
        except (OSError, VCharonError) as e:
            raise _walk_error(e, where, hint)
        items.sort(key=lambda item: item[0], reverse=True)
        return items

    def _hash(self, d, name, path):
        """(fstat, sha256) of the regular file name in d, read through the handle as open()
        reads it (decision 6 of the M4 plan). The stat is of the same fd, taken before the
        bytes are read: a change while they're read then shows on the next run.
        FileNotFoundError propagates; any other failure maps as open() maps it."""
        shown = pathrules.show(path)
        try:
            reader = d.open_read(name)
        except FileNotFoundError:
            raise
        except (OSError, VCharonError) as e:
            raise _read_error(e, shown)
        tick = self.ctx.tick
        h = hashlib.sha256()
        try:
            try:
                st = os.fstat(reader.fileno())
            except OSError as e:
                raise _read_error(e, shown)
            while True:
                try:
                    data = reader.read(proto.CHUNK)
                except OSError as e:
                    raise _read_error(e, shown)
                if not data:
                    break
                h.update(data)
                tick()
        finally:
            reader.close()
        return st, h.hexdigest()

    def _walk(self, root, prefix, abs_path, entries, full):
        """The tree under root, depth first, each directory right before what it holds. One
        explicit stack, with one open handle per level. Returns (entries, {entry index: the
        file's parts below root}, notes)."""
        tick = self.ctx.tick
        skip = self.options["symlinks"] == "skip"
        files = {}
        bad_names = []
        links = []
        skipped = 0
        # (handle, entries left, its parts below the root)
        stack = [(root, self._scan(root, abs_path, ROOT_LIST_HINT), ())]
        try:
            while stack:
                d, items, here = stack[-1]
                if not items:
                    stack.pop()
                    if d is not root:
                        d.close()
                    continue
                name, st = items.pop()
                tick()
                parts = here + (name,)
                # ferry's own stage dirs, in any case, and excluded entries: never entered,
                # counted or noted
                if name.casefold().startswith(pathrules.STAGE_PREFIX):
                    continue
                if not here:
                    self._top_names.add(name)
                if (self._me is not None and not here
                        and self._top_left_out(name, fsops.kind(st) == DIR)):
                    # left out as exclude leaves out (never deleted); listed in the log
                    if name != self._me:
                        self._strays.append(_escaped(name) + ("/" if fsops.kind(st) == DIR
                                                              else ""))
                    continue
                if self._excluded(parts):
                    continue
                path = "/".join(prefix + parts)
                try:
                    name.encode("utf-8")
                except UnicodeEncodeError:
                    bad_names.append("%s: the name isn't valid UTF-8" % _escaped(path))
                    continue
                k = fsops.kind(st)
                if k in (LINK, OTHER):
                    if skip:
                        skipped += 1
                    else:
                        links.append("%s: %s" % (pathrules.show(path), "a symlink" if k == LINK
                                                 else "a special file"))
                    continue
                if k == DIR:
                    entries.append(put_dir(path))
                    where = os.path.join(abs_path, *parts)
                    try:
                        sub = d.enter(name, owner_rule=False)
                    except (OSError, VCharonError) as e:
                        raise _walk_error(e, where)
                    try:
                        stack.append((sub, self._scan(sub, where), parts))
                    except BaseException:
                        sub.close()
                        raise
                    continue
                digest = None
                # Once the plan is bound to fail on a name or a link, reading more bytes for
                # it would only waste time.
                if full and not bad_names and not links:
                    try:
                        st, digest = self._hash(d, name, path)
                    except FileNotFoundError:
                        # gone since the listing: left out, as when it goes before its stat
                        continue
                files[len(entries)] = parts
                entries.append(put_file(path, st.st_size, st.st_mtime, self._exec(st), digest))
        finally:
            for d, items, here in stack:
                if d is not root:
                    d.close()
        # Nothing is ever skipped silently (DESIGN §9.2); name problems come first.
        if bad_names:
            raise pathrules.refusal("unsafe_path", bad_names, NAME_HINT)
        if links:
            raise pathrules.refusal("unsafe_path", links, LINKS_HINT)
        if self._strays:
            # the log only: the console shows what a run did, as before (the M9 plan)
            self.ctx.log("left out at the top, not another writer's folder: %s"
                         % ", ".join(self._strays[:pathrules.MAX_LISTED])
                         + (" (and %d more)" % (len(self._strays) - pathrules.MAX_LISTED)
                            if len(self._strays) > pathrules.MAX_LISTED else ""))
        notes = []
        if skipped == 1:
            notes.append("skipped 1 symlink or special file")
        elif skipped:
            notes.append("skipped %d symlinks or special files" % skipped)
        return entries, files, notes

    # --- what changed since the saved state ---

    def _excluded_folded(self, parts):
        """_excluded with the path and the patterns under Windows' and macOS's folds."""
        for osn, singles, wholes in self._folded:
            if any(fnmatch.fnmatch(pathrules.fold(parts[-1], osn), p) for p in singles):
                return True
            if wholes:
                rel = "/".join(pathrules.fold(x, osn) for x in parts)
                if any(fnmatch.fnmatch(rel, p) for p in wholes):
                    return True
        return False

    def _drop_excluded(self, sent, top, walked):
        """sent without the paths that exclude matches now, by the walk's rule: the path or any
        of its ancestors, relative to the source directory. A sent path the walk didn't find
        is also dropped when its fold matches a pattern's fold: it may be a stale spelling of
        an excluded file, which its delete would remove on a sink that folds names. (A path
        the walk found stays: the walk's rule already let it in.) keep_name's own directory
        (top) is never matched. These are never deleted (DESIGN §9.2)."""
        if not (self._singles or self._wholes or self._me is not None):
            return dict(sent)
        # parts -> excluded, by each rule; sent paths share their ancestors
        memos = ({}, {})
        rules = (self._excluded, self._excluded_folded)
        kept = {}
        tops = {}
        for path, value in sent.items():
            parts = path.split("/")
            if self._me is not None:
                name = parts[0]
                out = tops.get(name)
                if out is None:
                    out = tops[name] = self._top_dropped(name, sent, walked)
                if out:
                    continue
            if top is not None:
                if path == top:
                    kept[path] = value
                    continue
                if parts[0] == top:
                    parts = parts[1:]
            hit = False
            for rule, memo in zip(rules[:1] if path in walked else rules, memos):
                for k in range(1, len(parts) + 1):
                    key = tuple(parts[:k])
                    excluded = memo.get(key)
                    if excluded is None:
                        excluded = memo[key] = rule(key)
                    if excluded:
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                kept[path] = value
        return kept

    def _top_dropped(self, name, sent, walked):
        """Whether a mailbox's down drops the sent paths under the top-level name (never
        deleting them). Kept by the walk: no, it's another writer's folder (one that was sent
        as a file then becomes a delete and a put, as any kind change). Met by the walk but
        left out: yes, whatever sent says, so a writer's folder the server turned into a
        symlink or a file deletes nothing here. Gone from the source: by its name, and by
        its kind as sent (a top-level file sent before M9 is dropped, not deleted)."""
        if name in walked:
            return False
        if name in self._top_names:
            return True
        return self._top_left_out(name, sent.get(name, "d") == "d")

    def _since(self, sent, walked, files, single, top, abs_path, full):
        """Decisions 3 and 4 of the M4 plan: the entries of a run with a state, their
        {index: parts} for open(), and the new state."""
        prune = self.options["prune"]
        found = {e.path: e for e in walked}
        # exclude doesn't apply to a single file, so neither does its drop
        base = sent if single else self._drop_excluded(sent, top, found)
        if prune and not single and all(e.path == top for e in walked):
            n = sum(1 for path in base if path != top)
            if n and not self.options["allow_empty"]:
                # An unmounted disk usually looks like an empty directory.
                raise VCharonError("empty_source", "%s is empty, but earlier runs sent %s from it"
                                   % (abs_path, _counted(n, "path")), EMPTY_HINT)
        puts = [i for i, e in enumerate(walked) if full or base.get(e.path) != _value(e)]
        deletes = []
        if prune:
            for path in sorted(base):
                e = found.get(path)
                if e is None:
                    deletes.append(delete(path, why="gone from the source"))
                elif (base[path] == "d") != (e.kind == "dir"):
                    deletes.append(delete(path, why="replaced by a file" if e.kind == "file"
                                          else "replaced by a directory"))
        if (puts or deletes) and len(puts) < len(walked):
            partners = _partners(walked, puts, deletes)
            if partners:
                puts = sorted(set(puts) | partners)
        entries = [walked[i] for i in puts] + deletes
        new_files = {j: files[i] for j, i in enumerate(puts) if i in files}
        values = {walked[i].path: _value(walked[i]) for i in puts}
        new = dict(base)
        for e in deletes:
            del new[e.path]
        new.update(values)
        self._base = base
        self._values = values
        self._deletes = {e.path for e in deletes}
        return entries, new_files, {"sent": new}

    def state_after(self, written, deleted):
        """After a commit that failed partway: the old sent (after the exclude drop), less the
        deletes the commit got through and the puts it didn't write, plus the written puts
        with the values the plan gave them. So the state never lists what the commit removed.
        An unwritten put goes because an earlier delete may have removed it (a fold partner's
        stale spelling, on a sink that folds names); but one whose own path is a delete the
        commit didn't reach (a kind change) keeps the old value: the old entry is still there,
        and the next run must delete it. None unless plan() had a state."""
        if self._base is None:
            return None
        done = set()
        for path in deleted:
            if path in self._deletes:
                done.add(path)
            else:
                self.ctx.log("state_after: %s isn't a delete of this plan; ignored"
                             % pathrules.show(path))
        wrote = set()
        for path in written:
            if path in self._values:
                wrote.add(path)
            else:
                self.ctx.log("state_after: %s isn't a put of this plan; ignored"
                             % pathrules.show(path))
        sent = {path: value for path, value in self._base.items() if path not in done}
        for path in self._values:
            if path not in wrote and (path not in self._deletes or path in done):
                sent.pop(path, None)
        for path in wrote:
            sent[path] = self._values[path]
        return {"sent": sent}

    # --- open ---

    def _dir(self, parts):
        """The directory at parts below the root, entered one part at a time and never
        through a link. The chain of handles stays open, so the next file shares what it can."""
        chain = self._chain
        keep = 0
        while keep < len(chain) and keep < len(parts) and chain[keep][0] == parts[keep]:
            keep += 1
        while len(chain) > keep:
            chain.pop()[1].close()
        d = chain[-1][1] if chain else self._root
        for name in parts[keep:]:
            d = d.enter(name, owner_rule=False)
            chain.append((name, d))
        return d

    def open(self, index):
        parts = self._files.get(index) if self._root is not None else None
        if parts is None:
            raise VCharonError("internal", "entry %s isn't a file put of this source's plan"
                               % quote(index))
        shown = pathrules.show(self._entries[index].path)
        try:
            return self._dir(parts[:-1]).open_read(parts[-1])
        except (OSError, VCharonError) as e:
            raise _read_error(e, shown)

    # --- doctor ---

    def doctor(self):
        """ferry doctor: the path exists, and is a directory it can list or a file it can
        read. The top is followed, as plan() follows it. Only reads."""
        given = self.options["path"]
        abs_path = os.path.abspath(self.ctx.resolve(given, "from.path"))
        try:
            st = os.stat(abs_path)
        except OSError as e:
            if isinstance(e, FileNotFoundError) or e.errno == errno.ENOTDIR:
                return [("FAIL", "from.path %s doesn't exist" % given, MISSING_HINT)]
            err = fsops.error(e, abs_path)
            return [("FAIL", err.message, err.hint)]
        perm_hint = "check its permissions"
        if stat.S_ISDIR(st.st_mode):
            try:
                with os.scandir(abs_path) as it:
                    next(it, None)
            except OSError as e:
                return [("FAIL", "can't list %s: %s" % (abs_path, e.strerror or e), perm_hint)]
            return [("ok", "from.path %s: a directory" % abs_path, None)]
        if stat.S_ISREG(st.st_mode):
            if os.access(abs_path, os.R_OK):
                return [("ok", "from.path %s: a file" % abs_path, None)]
            return [("FAIL", "can't read %s" % abs_path, perm_hint)]
        return [("FAIL", "from.path %s is neither a file nor a directory" % abs_path,
                 "point from.path at a file or a directory")]

    # --- close ---

    def _close_root(self):
        root, self._root = self._root, None
        if root is not None:
            root.close()

    def close(self):
        while self._chain:
            self._chain.pop()[1].close()
        self._close_root()
