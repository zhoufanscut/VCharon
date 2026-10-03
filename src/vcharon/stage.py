"""Staging and commit (DESIGN, "Applying a plan"): the only way anything is written inside a root;
both ends."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import os
import secrets
import stat
import time
import traceback

from . import fsops, pathrules, platform, proto
from .fsops import DIR, FILE, LINK, WINDOWS
from .proto import VCharonError


@dataclasses.dataclass
class Checked:
    """sink.check's result (DESIGN, "Calls"). have: the indexes of the file puts whose bytes the
    target already holds (DESIGN, "Full syncs"), sorted."""

    root: str
    notes: list
    deletes: int
    have: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Done:
    """sink.commit's result; after a failed commit, what it did before the failure.
    deletes_done: the paths of the delete entries it got through, in order (removed, already
    gone, or kept because not empty; a tree delete once its whole removal is done)."""

    written: list
    deleted: int
    notes: list
    deletes_done: list = dataclasses.field(default_factory=list)


# The JSON forms of the calls (DESIGN, "Calls"), for both ends.

def checked_to_json(c):
    return {"root": c.root, "notes": list(c.notes), "deletes": c.deletes, "have": list(c.have)}


def done_to_json(d):
    return {"written": list(d.written), "deleted": d.deleted, "notes": list(d.notes),
            "deletes_done": list(d.deletes_done)}


def _strings(value):
    return isinstance(value, list) and all(isinstance(s, str) for s in value)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _indexes(value):
    # file indexes, each at most once
    return (isinstance(value, list) and all(_count(i) for i in value)
            and len(set(value)) == len(value))


def _malformed(fn, obj):
    return VCharonError("protocol", "a malformed %s result: %s" % (fn, proto.quote(obj)))


def checked_from_json(obj):
    if (not isinstance(obj, dict) or set(obj) != {"root", "notes", "deletes", "have"}
            or not isinstance(obj["root"], str) or not _strings(obj["notes"])
            or not _count(obj["deletes"]) or not _indexes(obj["have"])):
        raise _malformed("sink.check", obj)
    return Checked(root=obj["root"], notes=list(obj["notes"]), deletes=obj["deletes"],
                   have=list(obj["have"]))


def done_from_json(obj):
    if (not isinstance(obj, dict) or set(obj) != {"written", "deleted", "notes", "deletes_done"}
            or not _strings(obj["written"]) or not _count(obj["deleted"])
            or not _strings(obj["notes"]) or not _strings(obj["deletes_done"])):
        raise _malformed("sink.commit", obj)
    return Done(written=list(obj["written"]), deleted=obj["deleted"], notes=list(obj["notes"]),
                deletes_done=list(obj["deletes_done"]))


ROOT_HINT = "create it first (in a job: to.create = yes)"
KIND_HINT = fsops.KIND_HINT
DELETES_HINT = "check the source; if the deletes are right, raise max_deletes"

# A stage dir younger than this (by its mtime, in seconds) is never cleaned: its run may have
# made it but not locked it yet.
STALE_AGE = 60


def _no_tick():
    pass


def _no_log(msg):
    pass


def _needs_dir(path):
    return VCharonError("kind_change", "%s is a file at the target, but the plan needs a "
                        "directory there" % pathrules.show(path), KIND_HINT)


def _puts_file(path):
    return VCharonError("kind_change", "%s is a directory at the target, but the plan puts a "
                        "file there" % pathrules.show(path), KIND_HINT)


def _link(path):
    return VCharonError("unsafe_path", "%s is a symlink or junction; vcharon never goes through one"
                        % pathrules.show(path), fsops.LINK_HINT)


def _why(e):
    if isinstance(e, VCharonError):
        return e.message
    if isinstance(e, OSError):
        return e.strerror or str(e)
    return "%s: %s" % (type(e).__name__, e)


class Stager:
    """Serves one plan: check(), stage() each file put, then commit() or abort()."""

    def __init__(self, root, create=False, max_deletes=0, tick=None, log=None, impl=None):
        # root: absolute; the plugin expands ~ and resolves relative remote paths first.
        if not isinstance(root, str) or not os.path.isabs(root):
            raise VCharonError("internal", "the root %s isn't an absolute path"
                               % proto.quote(root))
        if impl is None:
            impl = "path" if WINDOWS else "fd"
        if impl not in ("fd", "path") or (impl == "fd" and WINDOWS):
            raise VCharonError("internal", "no directory handles of type %s here"
                               % proto.quote(impl))
        self.root = root
        self.create = create
        self.max_deletes = max_deletes
        self._tick = tick or _no_tick
        self._log = log or _no_log
        self._cls = fsops.FdDir if impl == "fd" else fsops.PathDir
        self.osn = platform.os_name()
        self.done = Done([], 0, [])
        # new -> checked -> finished; "failed" after a check that raised
        self._phase = "new"
        self._committed = False
        self._begun = False
        self._current = None
        self._resolved = None
        self._os_root = None
        self._display = None
        self._root = None
        self._stage = None
        self._stage_name = None
        self._lock = None
        # (parts, handle) of the directory _dir_at() opened last
        self._cache = None
        self._entries = []
        self._parts = []
        self._files = set()
        self._staged = set()
        # file put index -> (st_dev, st_ino, st_size, st_mtime_ns) of the target file that holds
        # its bytes, as it was when the check hashed it
        self._have = {}
        self._old_mode = {}
        self._old_spelling = {}
        self._read_only = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- check ---

    def check(self, plan):
        if self._phase != "new":
            raise VCharonError("internal", "check() runs once per Stager")
        self._phase = "failed"
        try:
            checked = self._check(plan)
        except OSError as e:
            raise fsops.error(e, self._where())
        self._phase = "checked"
        return checked

    def _where(self):
        return pathrules.show(self._current) if self._current else self.root

    def _fold(self, parts):
        return tuple(pathrules.fold(p, self.osn) for p in parts)

    def _check(self, plan):
        parts = pathrules.check_plan(plan, self.osn)
        self._entries = list(plan.entries)
        self._parts = parts
        self._files = {i for i, e in enumerate(self._entries)
                       if e.op == "put" and e.kind == "file"}
        # The root itself may be a symlink: resolved once, here.
        self._resolved = fsops.resolve_root(self.root)
        if WINDOWS:
            self._os_root = pathrules.win_long_path(self._resolved)
            self._display = pathrules.win_display(self._os_root)
        else:
            self._os_root = self._display = self._resolved
        notes = []
        deletes = 0
        if self._open_root():
            deletes = self._check_target(notes)
        # else: a missing root that commit will create, so every entry is new
        return Checked(root=self._display, notes=notes, deletes=deletes, have=sorted(self._have))

    def _open_root(self):
        """Opens the root and checks it. False if it's missing and may be created."""
        try:
            st = os.stat(self._os_root)
        except OSError as e:
            if e.errno not in (errno.ENOENT, errno.ENOTDIR):
                raise fsops.error(e, self.root)
            if not self.create:
                raise VCharonError("not_found", "the root %s doesn't exist" % self.root,
                                   ROOT_HINT)
            self._check_ancestor()
            return False
        if not stat.S_ISDIR(st.st_mode):
            raise VCharonError("not_found", "%s isn't a directory" % self.root,
                               "pick a directory as the root")
        try:
            self._root = self._cls.open_root(self._resolved)
        except OSError as e:
            raise fsops.error(e, self.root)
        problem = fsops.dir_problem(self._root.st, self._root.st.st_dev)
        if problem:
            raise fsops.unsafe_dir("the root %s" % self.root, problem, self._display)
        return True

    def _check_ancestor(self):
        # The nearest existing ancestor of a missing root must be a directory.
        path = self._resolved
        while True:
            up = os.path.dirname(path)
            if up == path:
                return
            path = up
            try:
                st = os.stat(pathrules.win_long_path(path) if WINDOWS else path)
            except OSError as e:
                if e.errno in (errno.ENOENT, errno.ENOTDIR):
                    continue
                raise fsops.error(e, path)
            if not stat.S_ISDIR(st.st_mode):
                raise VCharonError("not_found", "can't create %s: %s isn't a directory"
                                   % (self.root, path), "pick another root")
            return

    def _check_target(self, notes):
        entries, parts = self._entries, self._parts
        folded = [self._fold(p) for p in parts]
        deletes = {f: i for i, f in enumerate(folded) if entries[i].op == "delete"}
        # Hashed file puts (DESIGN, "Full syncs") whose target may already hold their bytes. A put
        # whose path, or a parent of it, the plan deletes first is a new file.
        candidates = {i for i in self._files if entries[i].sha256 is not None
                      and not any(folded[i][:k] in deletes for k in range(1, len(folded[i]) + 1))}
        hashed = [0, 0]         # files, bytes
        facts, tree_counts, child_names, spelling = self._gather(candidates, hashed)
        if candidates:
            self._log("check: hashed %d files (%d bytes), %d already there"
                      % (hashed[0], hashed[1], len(self._have)))

        # Will the plan's deletes remove this path? Deepest first, so a directory's children
        # are answered before it.
        removed = {}
        for f in sorted(deletes, key=len, reverse=True):
            e = entries[deletes[f]]
            st = facts.get(f)
            if st is None or fsops.kind(st) != DIR or e.tree:
                removed[f] = True
            else:
                removed[f] = all(removed.get(f + (pathrules.fold(n, self.osn),), False)
                                 for n in child_names.get(f, ()))

        # The kind rules, in plan order.
        for i, e in enumerate(entries):
            if e.op != "put":
                continue
            p, f = parts[i], folded[i]
            for k in range(1, len(p) + 1):
                key = f[:k]
                st = facts.get(key)
                # missing, or gone once the deletes are done: so is everything below
                if st is None or removed.get(key, False):
                    break
                is_dir = fsops.kind(st) == DIR
                if k < len(p) or e.kind == "dir":
                    if not is_dir:
                        raise _needs_dir("/".join(p[:k]))
                elif is_dir:
                    raise _puts_file(e.path)

        count = 0
        for f, i in deletes.items():
            e = entries[i]
            st = facts.get(f)
            if st is None:
                # missing, or below a non-directory
                continue
            if fsops.kind(st) != DIR:
                count += 1
            elif e.tree:
                count += tree_counts[f]
            elif removed[f]:
                count += 1
            else:
                notes.append("kept %s: it isn't empty" % pathrules.show(e.path))
        if self.max_deletes > 0 and count > self.max_deletes:
            raise VCharonError("too_many_deletes", "the plan deletes %d files and directories, "
                               "more than max_deletes (%d)" % (count, self.max_deletes),
                               DELETES_HINT)

        for i in self._files:
            f = folded[i]
            # a file put whose path, or a parent of it, the plan deletes first is a new file
            if any(f[:k] in deletes for k in range(1, len(f) + 1)):
                continue
            st = facts.get(f)
            if st is not None and fsops.kind(st) == FILE:
                self._old_mode[i] = st.st_mode & 0o777
                attrs = getattr(st, "st_file_attributes", 0)
                self._read_only[i] = bool(attrs & stat.FILE_ATTRIBUTE_READONLY)
            if i in spelling:
                self._old_spelling[i] = spelling[i]
        return count

    def _gather(self, candidates, hashed):
        """Facts about the target: one depth-first walk over a trie of every entry's parts.
        Returns ({folded parts: lstat}, {folded: count_tree} for tree deletes of directories,
        {folded: child names} for plain deletes of directories, {file put: old spelling}).
        It hashes the target file of each candidate that may hold its bytes, filling
        self._have; hashed counts the files and bytes it read."""
        trie = [{}, []]         # [children by name, indexes of the entries that end here]
        for i, p in enumerate(self._parts):
            node = trie
            for name in p:
                node = node[0].setdefault(name, [{}, []])
            node[1].append(i)
        facts, tree_counts, child_names, spelling = {}, {}, {}, {}
        spell = self.osn in ("darwin", "windows")
        root = self._root
        # [handle, names left (popped from the end), trie node, parts, its listing or None,
        # its spelling index or None]; only the directories on the current path are open.
        stack = [[root, sorted(trie[0], reverse=True), trie, (), None, None]]
        try:
            while stack:
                frame = stack[-1]
                d, names, node, here = frame[:4]
                if not names:
                    stack.pop()
                    if d is not root:
                        d.close()
                    continue
                name = names.pop()
                child = node[0][name]
                p = here + (name,)
                f = self._fold(p)
                self._current = "/".join(p)
                st = d.lstat(name)
                self._tick()
                k = None
                if st is not None:
                    facts[f] = st
                    k = fsops.kind(st)
                sub = None
                listing = None
                if child[0] and k == LINK:
                    # even when the plan deletes it
                    raise _link(self._current)
                if child[0] and k == DIR:
                    sub = d.enter(name)
                try:
                    for i in child[1]:
                        e = self._entries[i]
                        if e.op == "delete" and k == DIR:
                            if e.tree:
                                tree_counts[f] = fsops.count_tree(d, name, self._tick)
                            elif sub is not None:
                                listing = sub.listdir()
                                child_names[f] = listing
                            else:
                                with d.enter(name, owner_rule=False) as one:
                                    child_names[f] = one.listdir()
                        elif e.op == "put" and e.kind == "file" and st is not None:
                            if spell:
                                if frame[4] is None:
                                    frame[4] = d.listdir()
                                if frame[5] is None:
                                    frame[5] = self._spelling_index(frame[4])
                                old = self._spelling(frame[5], name)
                                if old is not None:
                                    spelling[i] = old
                            if (i in candidates and k == FILE and st.st_size == e.size
                                    and self._touchable(st)):
                                self._hash_target(d, name, i, hashed)
                except BaseException:
                    if sub is not None:
                        sub.close()
                    raise
                if sub is not None:
                    stack.append([sub, sorted(child[0], reverse=True), child, p, listing, None])
        finally:
            for frame in stack:
                if frame[0] is not root:
                    frame[0].close()
        self._current = None
        return facts, tree_counts, child_names, spelling

    @staticmethod
    def _touchable(st):
        """Whether the commit may set this target file's mtime and mode in place. Not with
        another hard link, which may lead outside the root; on POSIX, only your own file (it
        takes the owner); on Windows, not a read-only one, which is left to a replace."""
        if st.st_nlink != 1:
            return False
        if WINDOWS:
            return not getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY
        return st.st_uid == os.geteuid()

    def _hash_target(self, d, name, i, hashed):
        """Hashes the target file name in d through the handle; on a match, file put i is in
        self._have. A file that can't be read is simply sent."""
        e = self._entries[i]
        h = hashlib.sha256()
        try:
            reader = d.open_read(name)
        except (OSError, VCharonError) as err:
            self._log("check: couldn't hash %s, so it's sent: %s"
                      % (pathrules.show(e.path), _why(err)))
            return
        try:
            try:
                st = os.fstat(reader.fileno())
            except OSError as err:
                self._log("check: couldn't hash %s, so it's sent: %s"
                          % (pathrules.show(e.path), _why(err)))
                return
            # Taken from the bytes' own fd before they're read: an edit while they're hashed,
            # or before the commit, then shows at touch().
            ident = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            while True:
                try:
                    data = reader.read(proto.CHUNK)
                except OSError as err:
                    self._log("check: couldn't hash %s, so it's sent: %s"
                              % (pathrules.show(e.path), _why(err)))
                    return
                if not data:
                    break
                h.update(data)
                hashed[1] += len(data)
                # outside the except: a lost connection must stop the check
                self._tick()
        finally:
            reader.close()
        hashed[0] += 1
        if h.hexdigest() == e.sha256:
            self._have[i] = ident

    def _spelling_index(self, listing):
        """A directory's names, and its names by their folded form; built once per directory."""
        by_fold = {}
        for n in listing:
            by_fold.setdefault(pathrules.fold(n, self.osn), []).append(n)
        return set(listing), by_fold

    def _spelling(self, index, name):
        """The existing entry's name, if it's spelled differently from the plan's."""
        names, by_fold = index
        if name in names:
            return None
        found = by_fold.get(pathrules.fold(name, self.osn), ())
        return found[0] if len(found) == 1 else None

    # --- stage ---

    def _need_checked(self, what):
        if self._phase != "checked":
            raise VCharonError("internal", "%s() needs a Stager with a passed check and no "
                               "commit or abort" % what)

    def _stageable(self, index):
        if (not isinstance(index, int) or isinstance(index, bool)
                or index not in self._files):
            raise VCharonError("protocol", "entry %s isn't a file put" % proto.quote(index))
        if index in self._have:
            raise VCharonError("protocol", "entry %d is already at the target" % index)
        if index in self._staged:
            raise VCharonError("protocol", "file %d was staged twice" % index)

    def stage(self, index, reader):
        """Stores file put index's bytes in the stage dir. The file then gets its mode, mtime
        and Windows read-only state, as if it had been streamed; nothing is read back from a
        staged file, whose mode may be the replaced target's (0o200, say)."""
        self._need_checked("stage")
        self._stageable(index)
        indexes = [index]
        self._begin()
        # index -> the fd of its staged file, until _finish() closes it
        fds = {}
        created = []
        current = index
        try:
            for i in indexes:
                current = i
                fds[i] = self._stage.create_file(str(i))
                created.append(i)
            current = index
            self._copy([fds[i] for i in indexes], reader)
            for i in indexes:
                current = i
                self._finish(i, fds.pop(i))
        except BaseException as err:
            for fd in fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            for i in created:
                with contextlib.suppress(Exception):
                    self._stage.unlink(str(i))
            if isinstance(err, OSError):
                raise fsops.error(err, pathrules.show(self._entries[current].path))
            raise
        self._staged.update(indexes)

    def _finish(self, index, fd):
        """Gives the staged file of index its mtime and permission bits (DESIGN, "What is copied"),
        and closes fd, whatever happens."""
        e = self._entries[index]
        mtime_ns = round(e.mtime * 1e9)
        if WINDOWS:
            # os.utime takes no fd on Windows.
            os.close(fd)
            path = self._stage.join(str(index))
            self._stage.recheck()
            os.utime(path, ns=(time.time_ns(), mtime_ns))
            if self._read_only.get(index):
                os.chmod(path, stat.S_IREAD)
            return
        try:
            mode = self._old_mode.get(index)
            if mode is None:
                # 0o666 minus the umask
                mode = os.fstat(fd).st_mode & 0o777
            if e.executable is True:
                mode |= (mode & 0o444) >> 2
            elif e.executable is False:
                mode &= ~0o111
            os.fchmod(fd, mode)
            os.utime(fd, ns=(time.time_ns(), mtime_ns))
        finally:
            os.close(fd)

    @staticmethod
    def _copy(fds, reader):
        while True:
            data = reader.read(proto.CHUNK)
            if not data:
                return
            for fd in fds:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]

    def _begin(self):
        """Makes the root (with create) and the stage dir, and clears stale stage dirs; once."""
        if self._begun:
            return
        try:
            if self._root is None:
                # Resolved again before and after: a symlink planted where the missing root
                # should be would make makedirs create it somewhere else.
                self._same_root()
                try:
                    os.makedirs(self._os_root, exist_ok=True)
                except OSError as e:
                    raise fsops.error(e, self.root)
                self._same_root()
                if not self._open_root():
                    raise VCharonError("not_found", "the root %s doesn't exist" % self.root,
                                       ROOT_HINT)
            name = pathrules.STAGE_PREFIX + secrets.token_hex(8)
            self._root.mkdir(name, 0o700)
            self._stage_name = name
            self._stage = self._root.enter(name, owner_rule=False)
            self._lock = self._stage.open_lock(create=True, exclusive=True)
            if not self._lock.try_acquire():
                raise VCharonError("io", "another sync took the new stage dir; run again")
        except BaseException as e:
            self._drop_stage()
            if isinstance(e, OSError):
                raise fsops.error(e, self.root)
            raise
        self._begun = True
        self._clean_stale()

    def _same_root(self):
        if fsops.resolve_root(self.root) != self._resolved:
            raise VCharonError("unsafe_path", "the root %s changed during the run" % self.root,
                               "run again")

    def _note(self, note):
        self.done.notes.append(note)
        self._log(note)

    def _clean_stale(self):
        # Stage dirs of runs that died: ones you own whose lock nobody holds (DESIGN, "Staging and
        # commit").
        root = self._root
        try:
            names = root.listdir()
        except OSError as e:
            self._note("couldn't look for old stage dirs: %s" % _why(e))
            return
        for name in names:
            if name == self._stage_name or not name.startswith(pathrules.STAGE_PREFIX):
                continue
            try:
                st = root.lstat(name)
                if st is None or fsops.kind(st) != DIR:
                    continue
                if not WINDOWS and st.st_uid != os.geteuid():
                    continue
                # Checked before anything is opened: a young dir may belong to a run that
                # hasn't locked it yet, and taking its fresh lock would break that run.
                if time.time() - st.st_mtime < STALE_AGE:
                    continue
                with root.enter(name, owner_rule=False) as d:
                    try:
                        lock = d.open_lock(create=False, exclusive=False)
                    except FileNotFoundError:
                        # a stage dir with no lock file is dead
                        lock = None
                    if lock is not None:
                        try:
                            live = not lock.try_acquire()
                        finally:
                            # released first: Windows can't delete an open file
                            lock.release()
                        if live:
                            continue
                fsops.remove_tree(root, name, self._tick)
            except (OSError, VCharonError) as e:
                self._note("couldn't remove the old stage dir %s: %s" % (name, _why(e)))

    def _drop_stage(self):
        """Releases the lock and removes the stage dir; an error's text, or None."""
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        if self._stage is not None:
            self._stage.close()
            self._stage = None
        name, self._stage_name = self._stage_name, None
        if name is None:
            return None
        try:
            fsops.remove_tree(self._root, name, _no_tick)
        except (OSError, VCharonError) as e:
            why = "couldn't remove the stage dir %s: %s" % (name, _why(e))
            self._log(why)
            return why
        return None

    # --- commit ---

    def _drop_cache(self):
        if self._cache is not None:
            d = self._cache[1]
            self._cache = None
            if d is not self._root:
                d.close()

    def _dir_at(self, parts, create):
        """The directory at parts, opened one part at a time from the root, so the device and
        owner rules run again at every step. None if it's missing (or a file) and not create."""
        if not parts:
            return self._root
        if self._cache is not None and self._cache[0] == parts:
            return self._cache[1]
        self._drop_cache()
        d = self._root
        try:
            for k, name in enumerate(parts):
                st = d.lstat(name)
                if st is None:
                    if not create:
                        return self._close(d)
                    try:
                        d.mkdir(name)
                    except FileExistsError:
                        # a race; enter() below checks what's there
                        pass
                else:
                    k_ = fsops.kind(st)
                    if k_ == LINK:
                        raise _link("/".join(parts[:k + 1]))
                    if k_ != DIR:
                        if create:
                            raise _needs_dir("/".join(parts[:k + 1]))
                        return self._close(d)
                sub = d.enter(name)
                self._close(d)
                d = sub
        except BaseException:
            self._close(d)
            raise
        self._cache = (parts, d)
        return d

    def _close(self, d):
        if d is not self._root:
            d.close()

    def commit(self):
        self._need_checked("commit")
        missing = sorted(self._files - self._staged - set(self._have))
        if missing:
            raise VCharonError("protocol", "file %d wasn't staged" % missing[0])
        self._begin()
        try:
            self._apply()
        except BaseException as e:
            where = self._where()
            tb = traceback.format_exc()
            self._phase = "finished"
            self._drop_cache()
            problem = self._drop_stage()
            if problem:
                self.done.notes.append(problem)
            if isinstance(e, VCharonError):
                raise
            if isinstance(e, OSError):
                raise fsops.error(e, where)
            if isinstance(e, Exception):
                raise VCharonError("internal", "%s: %s" % (type(e).__name__, e), detail=tb)
            raise
        self._committed = True
        self._phase = "finished"
        self._drop_cache()
        problem = self._drop_stage()
        if problem:
            self.done.notes.append(problem)
        self._current = None
        return self.done

    def _apply(self):
        entries, parts, done = self._entries, self._parts, self.done
        deletes = [i for i, e in enumerate(entries) if e.op == "delete"]
        dirs = [i for i, e in enumerate(entries) if e.op == "put" and e.kind == "dir"]
        files = [i for i, e in enumerate(entries) if e.op == "put" and e.kind == "file"]

        # 1. deletes, deepest first
        for i in sorted(deletes, key=lambda i: (-len(parts[i]), parts[i])):
            e, p = entries[i], parts[i]
            self._current = e.path
            parent = self._dir_at(p[:-1], False)
            st = parent.lstat(p[-1]) if parent is not None else None
            if st is None:
                pass
            elif fsops.kind(st) == DIR:
                # The cache holds only this parent, never the directory removed here.
                if e.tree:
                    done.deleted += fsops.remove_tree(parent, p[-1], self._tick)
                else:
                    try:
                        parent.rmdir(p[-1])
                        done.deleted += 1
                    except OSError as err:
                        if err.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                            raise
                        done.notes.append("kept %s: it isn't empty" % pathrules.show(e.path))
            else:
                try:
                    fsops.remove_entry(parent, p[-1], st)
                    done.deleted += 1
                except FileNotFoundError:
                    pass
            # through with it: a state saved after a later failure must not list it (DESIGN, "The
            # path source")
            done.deletes_done.append(e.path)
            self._tick()
        self._drop_cache()

        # 2. directory puts, shallowest first
        for i in sorted(dirs, key=lambda i: (len(parts[i]), parts[i])):
            e, p = entries[i], parts[i]
            self._current = e.path
            parent = self._dir_at(p[:-1], True)
            try:
                parent.mkdir(p[-1])
            except FileExistsError:
                st = parent.lstat(p[-1])
                k = fsops.kind(st) if st is not None else None
                if k == LINK:
                    raise _link(e.path)
                if k != DIR:
                    raise _needs_dir(e.path)
            done.written.append(e.path)
            self._tick()

        # 3. file puts, in plan order
        for i in files:
            e, p = entries[i], parts[i]
            self._current = e.path
            parent = self._dir_at(p[:-1], True)
            name = p[-1]
            ident = self._have.get(i)
            if ident is not None:
                # The target holds the bytes already (DESIGN, "Full syncs"). It gets the plan's
                # mtime and execute bits, through the handle, if it's still the file the check
                # hashed.
                parent.touch(name, round(e.mtime * 1e9), e.executable, ident)
                self._respell(parent, i, name)
                done.written.append(e.path)
                self._tick()
                continue
            try:
                parent.move_in(self._stage, str(i), name)
            except OSError as err:
                if err.errno in (errno.EISDIR, errno.ENOTEMPTY, errno.EEXIST):
                    raise _puts_file(e.path)
                raise
            # at the target now, whatever happens next
            done.written.append(e.path)
            self._respell(parent, i, name)
            if not WINDOWS and parent.st.st_mode & stat.S_ISGID:
                gid = parent.st.st_gid
                st = parent.lstat(name)
                if st is not None and st.st_gid != gid:
                    try:
                        parent.set_group(name, gid)
                    except OSError as err:
                        done.notes.append("couldn't give %s its directory's group: %s"
                                          % (pathrules.show(e.path), _why(err)))
            self._tick()
        self._drop_cache()

    def _respell(self, parent, i, name):
        old = self._old_spelling.get(i)
        if old is not None:
            # The replaced file kept its old spelling (DESIGN, "Files already at the target"); make
            # it the plan's.
            try:
                parent.rename(old, name)
            except FileNotFoundError:
                pass

    # --- abort and close ---

    def abort(self):
        """Drops staged data; safe to call twice, and a no-op after a successful commit."""
        if self._committed:
            return
        self._phase = "finished"
        self._drop_cache()
        self._drop_stage()
        self._close_root()

    def _close_root(self):
        if self._root is not None:
            self._root.close()
            self._root = None

    def close(self):
        """abort() unless committed; releases every fd and the lock."""
        if not self._committed:
            self.abort()
        self._drop_cache()
        self._drop_stage()
        self._close_root()
