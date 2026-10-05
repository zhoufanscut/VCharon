"""File kinds, OS errors as VCharonErrors, directory handles and tree walks, on both ends."""

from __future__ import annotations

import codecs
import collections
import errno
import locale
import os
import signal
import stat
import subprocess
import time

from . import pathrules
from .lock import Lock
from .proto import VCharonError

WINDOWS = os.name == "nt"

if not WINDOWS:
    import fcntl
    import grp
    import pwd

FILE, DIR, LINK, OTHER = "file", "dir", "link", "other"

LINK_HINT = "remove it at the target, or pick another root"
KIND_HINT = "remove or rename it at the target, then run again"
MODE_HINT = "fix its owner or mode (for example: chmod go-w %s), then run again"
DEVICE_HINT = "pick a root with no mount points under it"
# not the path source's NAME_HINT text: a root path can be too long too, and a mailbox job
# swaps that hint (cli.SOURCE_HINTS)
TOO_LONG_HINT = "rename it at the source, or pick a shorter root"
# An agent CLI's sandbox refuses with these on a folder that is fine: EROFS (Codex on Linux),
# EPERM (macOS's sandbox) (DESIGN, "Fix lines"). EACCES is usually the file's own owner or mode.
SANDBOX_ERRNOS = (errno.EROFS, errno.EPERM)
# fsops can't import platform (a cycle): the printer spells the command, as for every fix line
SANDBOX_HINT = ("if your CLI's sandbox blocked it, ask your user to allow vcharon's folders "
                "(vcharon doctor); else check the owner and permissions of %s")
PERMISSION_HINT = "check the owner and permissions of %s"
# True in the helper on a server, where no agent's sandbox runs: there the plain hint. Set only
# by helper.main, never reset: a test that runs helper.main in-process must restore it.
SERVER = False

# Sharing-violation retries on Windows (DESIGN, "Staging and commit"): virus scanners briefly hold
# new files.
RETRIES = 3
RETRY_DELAY = 0.2

# Windows errors of a step blocked by another process holding the path: access denied (5; seen
# for a folder held open, and expected for a file replaced while a program has it open without
# FILE_SHARE_DELETE, as Python opens files), a sharing violation (32)
HELD_CODES = (5, 32)


# A reparse tag with this bit is a name surrogate: a junction or a directory symlink.
NAME_SURROGATE = 0x20000000


def kind(st):
    if stat.S_ISLNK(st.st_mode):
        return LINK
    if stat.S_ISDIR(st.st_mode):
        # A name-surrogate reparse point (junction, directory symlink) is a link, as in the path
        # source (DESIGN, "The path source"). Others, such as OneDrive cloud placeholders, are real
        # directories.
        if (getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                and getattr(st, "st_reparse_tag", 0) & NAME_SURROGATE):
            return LINK
        return DIR
    if stat.S_ISREG(st.st_mode):
        return FILE
    return OTHER


def missing(path):
    """True only when path is known not to exist: a folder that can't be read is the caller's to
    report, with its own hint."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def error(e, path):
    """An OSError as a VCharonError; path is what the message names (a plan path, or the root)."""
    # winerror first: Windows sets errno to EACCES for both 5 and 32.
    winerror = getattr(e, "winerror", None)
    if winerror in (32, 33):
        return VCharonError("in_use", "%s: another program has it open" % path,
                            "close that program, then run again")
    if winerror == 5:
        return VCharonError("permission", "%s: access denied" % path,
                            "another program may have it open")
    no = e.errno
    text = e.strerror or str(e)
    if no == errno.ENOENT:
        return VCharonError("not_found", "%s: no such file or directory" % path,
                            "it may have been removed during the run; run again")
    if no in (errno.EACCES, errno.EPERM, errno.EROFS):
        hint = SANDBOX_HINT if no in SANDBOX_ERRNOS and not SERVER else PERMISSION_HINT
        return VCharonError("permission", "%s: %s" % (path, text), hint % path)
    if no is not None and no in (errno.ENOSPC, getattr(errno, "EDQUOT", None)):
        return VCharonError("no_space", "%s: the disk is full" % path,
                            "free some space, then run again")
    if no in (errno.ENOTDIR, errno.EISDIR):
        return VCharonError("kind_change", "%s: %s" % (path, text), KIND_HINT)
    if no == errno.ELOOP:
        return VCharonError("unsafe_path", "%s: a symlink is in the way" % path,
                            "remove the symlink at the target, then run again")
    if no == errno.ENAMETOOLONG:
        return VCharonError("unsafe_path", "%s: the name is too long for this file system" % path,
                            TOO_LONG_HINT)
    if no == errno.EXDEV:
        return VCharonError("unsafe_dir", "%s is on another file system" % path, DEVICE_HINT)
    return VCharonError("io", "%s: %s" % (path, text), "see the log")


def retry_in_use(fn, *args, codes=(32,)):
    """fn(*args), tried again after a sharing violation (winerror 32), or after any of codes."""
    tries = 0
    while True:
        try:
            return fn(*args)
        except OSError as e:
            if getattr(e, "winerror", None) not in codes or tries >= RETRIES:
                raise
        tries += 1
        time.sleep(RETRY_DELAY)


# realpath's limit on symlinks met while resolving a path
MAX_LINKS = 40


def resolve_root(path):
    """The root with every symlink resolved, as os.path.realpath gives it; missing trailing
    parts are kept as they are. On POSIX every symlink met on the way must be yours or root's:
    realpath resolves links in user space, where Linux's protected_symlinks never applies, so
    another user's /tmp/drop -> ~/.ssh would redirect the run."""
    if WINDOWS:
        return os.path.realpath(path)
    # not abspath: it would drop "link/.." as text, where realpath and the kernel go up from
    # the link's target
    if not path.startswith("/"):
        path = os.path.join(os.getcwd(), path)
    euid = os.geteuid()
    # parts still to walk, the next one last
    todo = [p for p in reversed(path.split("/")) if p]
    resolved = "/"
    links = 0
    missing = False
    while todo:
        part = todo.pop()
        if part == ".":
            continue
        if part == "..":
            resolved = os.path.dirname(resolved)
            continue
        candidate = os.path.join(resolved, part)
        if missing:
            resolved = candidate
            continue
        try:
            st = os.lstat(candidate)
        except OSError:
            # as realpath: what can't be looked at isn't a link, and neither is anything below
            missing = True
            resolved = candidate
            continue
        if not stat.S_ISLNK(st.st_mode):
            resolved = candidate
            continue
        if st.st_uid not in (euid, 0):
            raise VCharonError("unsafe_dir", "the root %s goes through %s, a symlink owned by "
                               "another user" % (path, candidate),
                               "use the path it points to as the root, if you trust it")
        links += 1
        if links > MAX_LINKS:
            raise VCharonError("unsafe_path", "the root %s goes through more than %d symlinks; "
                               "they may form a loop" % (path, MAX_LINKS),
                               "fix the symlinks, or pick another root")
        target = os.readlink(candidate)
        if target.startswith("/"):
            resolved = "/"
        todo.extend(p for p in reversed(target.split("/")) if p)
    return resolved


# gid -> whether it's the effective user's private group
_private_groups = {}


def private_group(gid):
    """True if gid is your primary group, named after you, with nobody else in it. A lookup
    that fails means no."""
    if gid not in _private_groups:
        try:
            me = pwd.getpwuid(os.geteuid())
            group = grp.getgrgid(gid)
            _private_groups[gid] = (gid == me.pw_gid and group.gr_name == me.pw_name
                                    and all(m == me.pw_name for m in group.gr_mem))
        except Exception:  # noqa: BLE001
            _private_groups[gid] = False
    return _private_groups[gid]


def dir_problem(st, root_dev, owner_rule=True):
    """Why vcharon may not go through this directory, or None. root_dev None: no device rule (a
    source only reads, and may cross mount points)."""
    if root_dev is not None and st.st_dev != root_dev:
        return "is on another file system"
    if not owner_rule or WINDOWS:
        return None
    # A sticky directory, such as /tmp, may be root's and writable by all: others can't
    # rename or remove what's yours in it.
    sticky = st.st_mode & stat.S_ISVTX
    if st.st_uid != os.geteuid() and not (sticky and st.st_uid == 0):
        return "is owned by another user"
    if sticky:
        return None
    if st.st_mode & 0o002:
        return "is writable by others"
    if st.st_mode & 0o020 and not private_group(st.st_gid):
        return "is writable by its group"
    return None


def unsafe_dir(what, problem, abs_path):
    """The unsafe_dir error for a dir_problem() answer; what: how the message names the dir."""
    if problem == "is on another file system":
        hint = DEVICE_HINT
    else:
        hint = MODE_HINT % abs_path
    return VCharonError("unsafe_dir", "%s %s" % (what, problem), hint)


def _link_error(rel):
    return VCharonError("unsafe_path",
                        "%s is a symlink or junction; vcharon never goes through one" % rel,
                        LINK_HINT)


def _not_dir_error(rel):
    return VCharonError("kind_change", "%s isn't a directory" % rel, KIND_HINT)


def _rel_text(rel):
    return "/".join(rel)


def _scan(entries, lstat=None):
    """(name, stat) of each entry of an os.scandir iterator, in listing order. An entry gone
    before its stat is left out. lstat(name): the stat to use for a reparse point (Windows)."""
    found = []
    with entries:
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
                # Only lstat always carries st_reparse_tag, which kind() needs to tell a
                # junction from a OneDrive folder.
                if (lstat is not None and getattr(st, "st_file_attributes", 0)
                        & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    st = lstat(entry.name)
            except FileNotFoundError:
                continue
            found.append((entry.name, st))
    return found


def _reader(fd, rel):
    """A binary reader for fd, which must be a regular file; fd is closed on every error."""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VCharonError("vanished", "%s isn't a regular file any more" % _rel_text(rel))
        if not WINDOWS:
            # Opened without blocking, so a FIFO swapped in couldn't hang the open; reads block.
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        return open(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _changed_at_target(rel):
    return VCharonError("vanished", "%s changed at the target during the run" % _rel_text(rel),
                        "run again")


def _same_file(st, ident):
    """Whether st is still the regular file the check hashed, with no other hard link (one may
    lead outside the root): ident is its (st_dev, st_ino, st_size, st_mtime_ns) then."""
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
            and (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) == tuple(ident))


def _touch_fd(fd, rel, mtime_ns, executable, ident):
    """Gives the file open at fd the plan's mtime and execute bits (DESIGN, "Full syncs"), if it's
    still the file the check hashed (_same_file)."""
    st = os.fstat(fd)
    if not _same_file(st, ident):
        raise _changed_at_target(rel)
    # As for a replaced file (DESIGN, "What is copied"): True adds x wherever there is r, False
    # clears it, None keeps it
    mode = st.st_mode & 0o777
    if executable is True:
        mode |= (mode & 0o444) >> 2
    elif executable is False:
        mode &= ~0o111
    # This drops setuid, setgid and sticky too, as a replace does.
    if stat.S_IMODE(st.st_mode) != mode:
        os.fchmod(fd, mode)
    os.utime(fd, ns=(time.time_ns(), mtime_ns))


# What opening the file for touch() gives when something else is there now: a link, a missing
# file or directory on the way, a socket (ENXIO on Linux, EOPNOTSUPP on macOS) or a device.
_TOUCH_GONE = tuple(getattr(errno, name) for name in ("ELOOP", "ENOENT", "ENOTDIR", "ENXIO",
                                                      "EOPNOTSUPP", "ENODEV")
                    if hasattr(errno, name))


def _open_to_touch(path, rel, dir_fd=None):
    """An fd for touch(): a link swapped in gives ELOOP, and a FIFO can't block the open."""
    try:
        if WINDOWS:
            # as for moves and deletes: a virus scanner may hold the file for a moment
            return retry_in_use(os.open, path, _READ_FLAGS)
        return os.open(path, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in _TOUCH_GONE:
            raise _changed_at_target(rel)
        raise


class _Dir:
    """What both handle types share. rel: the plan parts from the root; st: its own stat;
    root_dev: the root's device, or None for no device rule; root_name: the root as messages
    show it."""

    def __init__(self, rel, st, root_dev, root_name):
        self.rel = rel
        self.st = st
        self.root_dev = root_dev
        self.root_name = root_name

    def abs_text(self, rel):
        """An absolute path for a hint."""
        if not rel:
            return self.root_name
        sep = "\\" if WINDOWS else "/"
        return self.root_name.rstrip(sep) + sep + sep.join(rel)

    def _checked(self, rel, st, owner_rule):
        """Raises unless st, of the directory at rel, passes dir_problem()."""
        problem = dir_problem(st, self.root_dev, owner_rule)
        if problem:
            raise unsafe_dir(_rel_text(rel), problem, self.abs_text(rel))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# O_NONBLOCK: a FIFO swapped in for a directory can't block the open.
_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
              | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
_NEW_FILE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                   | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
                   | getattr(os, "O_NOINHERIT", 0))
# A directory that is only searched, never listed: a single file's own directory, which may
# grant x without r. O_PATH on Linux, O_SEARCH on macOS; elsewhere _DIR_FLAGS.
_SEARCH = getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0)
_SEARCH_FLAGS = ((_SEARCH | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                  | getattr(os, "O_CLOEXEC", 0)) if _SEARCH else _DIR_FLAGS)
# A source's file (DESIGN, "The path source"): a link isn't followed, and a FIFO or a device can't
# block the open; O_NOCTTY keeps a terminal device from becoming the helper's controlling terminal.
_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
               | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
               | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_NOCTTY", 0))


class FdDir(_Dir):
    """A directory handle on POSIX: an open directory fd; every call is relative to it, so a
    directory swapped for a symlink makes a step fail instead of redirecting it."""

    def __init__(self, fd, rel, st, root_dev, root_name):
        _Dir.__init__(self, rel, st, root_dev, root_name)
        self.fd = fd

    @classmethod
    def open_root(cls, path, same_device=True, list=True):
        """path is the resolved root. same_device=False: no device rule below it (a source).
        list=False: opened to search only, so lstat, enter and open_read work but scan and
        listdir don't; it needs x on the directory, not r. OSError propagates."""
        fd = os.open(path, _DIR_FLAGS if list else _SEARCH_FLAGS)
        try:
            st = os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, (), st, st.st_dev if same_device else None, path)

    def lstat(self, name):
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def enter(self, name, owner_rule=True):
        rel = self.rel + (name,)
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=self.fd)
        except OSError as e:
            # A symlink gives ENOTDIR on macOS and ELOOP on Linux.
            if e.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise
            st = self.lstat(name)
            if st is None:
                raise
            if kind(st) == LINK:
                raise _link_error(_rel_text(rel))
            raise _not_dir_error(_rel_text(rel))
        try:
            st = os.fstat(fd)
            self._checked(rel, st, owner_rule)
        except BaseException:
            os.close(fd)
            raise
        return FdDir(fd, rel, st, self.root_dev, self.root_name)

    def listdir(self):
        return os.listdir(self.fd)

    def scan(self):
        """(name, lstat) of each entry, in listing order; each stat goes through this fd."""
        return _scan(os.scandir(self.fd))

    def open_read(self, name):
        """A binary reader for the regular file name: a link gives ELOOP, and anything else
        that isn't a regular file vanished. OSError propagates."""
        return _reader(os.open(name, _READ_FLAGS, dir_fd=self.fd), self.rel + (name,))

    def mkdir(self, name, mode=0o777):
        os.mkdir(name, mode, dir_fd=self.fd)

    def unlink(self, name):
        os.unlink(name, dir_fd=self.fd)

    def rmdir(self, name):
        os.rmdir(name, dir_fd=self.fd)

    def move_in(self, src_dir, src_name, name):
        # POSIX rename replaces a non-directory atomically; os.replace isn't in
        # os.supports_dir_fd.
        os.rename(src_name, name, src_dir_fd=src_dir.fd, dst_dir_fd=self.fd)

    def touch(self, name, mtime_ns, executable, ident):
        """Gives the regular file name the plan's mtime and execute bits, through an fd, if
        it's still the file the check hashed (ident); otherwise vanished."""
        rel = self.rel + (name,)
        fd = _open_to_touch(name, rel, dir_fd=self.fd)
        try:
            _touch_fd(fd, rel, mtime_ns, executable, ident)
        finally:
            os.close(fd)

    def rename(self, old, new):
        os.rename(old, new, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def set_group(self, name, gid):
        os.chown(name, -1, gid, dir_fd=self.fd, follow_symlinks=False)

    def create_file(self, name):
        return os.open(name, _NEW_FILE_FLAGS, 0o666, dir_fd=self.fd)

    def open_lock(self, create, exclusive):
        return Lock.open("lock", dir_fd=self.fd, create=create, exclusive=exclusive)

    def close(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)


class PathDir(_Dir):
    """A directory handle by path: Windows, which has no dir_fd, and POSIX in tests. Before
    every change it checks again that each directory from the root down is still a directory
    (DESIGN, "Staging and commit": this narrows the race on Windows, but can't close it)."""

    def __init__(self, path, rel, st, root_dev, root_name, chain):
        _Dir.__init__(self, rel, st, root_dev, root_name)
        self.path = path
        # (path, rel) of every directory below the root, down to this one
        self.chain = chain

    @classmethod
    def open_root(cls, path, same_device=True, list=True):
        """path is the resolved root. same_device=False: no device rule below it (a source).
        list is for FdDir's sake; a path handle never opens anything. OSError propagates."""
        if WINDOWS:
            path = pathrules.win_long_path(path)
        st = os.lstat(path)
        if kind(st) != DIR:
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), path)
        name = pathrules.win_display(path) if WINDOWS else path
        return cls(path, (), st, st.st_dev if same_device else None, name, [])

    def join(self, name):
        if WINDOWS:
            return pathrules.win_long_path(self.path, (name,))
        return os.path.join(self.path, name)

    def lstat(self, name):
        try:
            return os.lstat(self.join(name))
        except FileNotFoundError:
            return None

    def enter(self, name, owner_rule=True):
        rel = self.rel + (name,)
        path = self.join(name)
        st = os.lstat(path)
        k = kind(st)
        if k == LINK:
            raise _link_error(_rel_text(rel))
        if k != DIR:
            raise _not_dir_error(_rel_text(rel))
        self._checked(rel, st, owner_rule)
        return PathDir(path, rel, st, self.root_dev, self.root_name,
                       self.chain + [(path, rel)])

    def recheck(self):
        for path, rel in self.chain:
            k = kind(os.lstat(path))
            if k == LINK:
                raise _link_error(_rel_text(rel))
            if k != DIR:
                raise _not_dir_error(_rel_text(rel))

    def listdir(self):
        return os.listdir(self.path)

    def scan(self):
        """(name, lstat) of each entry, in listing order."""
        # A directory above swapped for a link since enter() would list another tree.
        self.recheck()
        return _scan(os.scandir(self.path),
                     (lambda name: os.lstat(self.join(name))) if WINDOWS else None)

    def open_read(self, name):
        """A binary reader for the regular file name (DESIGN, "The path source": on Windows, lstat
        first that it isn't a reparse point, then fstat the opened file). OSError propagates."""
        self.recheck()
        path = self.join(name)
        rel = self.rel + (name,)
        if kind(os.lstat(path)) != FILE:
            raise VCharonError("vanished", "%s isn't a regular file any more" % _rel_text(rel))
        return _reader(os.open(path, _READ_FLAGS), rel)

    def mkdir(self, name, mode=0o777):
        self.recheck()
        os.mkdir(self.join(name), mode)

    def _clear_read_only(self, path):
        # SVN's needs-lock sets it, and Windows won't delete or replace a read-only file.
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return
        if (kind(st) == FILE
                and getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY):
            os.chmod(path, stat.S_IWRITE)

    def unlink(self, name):
        self.recheck()
        path = self.join(name)
        if WINDOWS:
            self._clear_read_only(path)
            retry_in_use(os.unlink, path, codes=HELD_CODES)
        else:
            os.unlink(path)

    def rmdir(self, name):
        self.recheck()
        if WINDOWS:
            retry_in_use(os.rmdir, self.join(name), codes=HELD_CODES)
        else:
            os.rmdir(self.join(name))

    def move_in(self, src_dir, src_name, name):
        src_dir.recheck()
        self.recheck()
        src, dst = src_dir.join(src_name), self.join(name)
        if WINDOWS:
            # os.replace onto a directory gives WinError 5 (access denied) there; say what it is.
            st = self.lstat(name)
            if st is not None and kind(st) == DIR:
                raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), dst)
            self._clear_read_only(dst)
            retry_in_use(os.replace, src, dst, codes=HELD_CODES)
        else:
            os.rename(src, dst)

    def rename(self, old, new):
        self.recheck()
        os.rename(self.join(old), self.join(new))

    def touch(self, name, mtime_ns, executable, ident):
        """FdDir.touch by path. The lstat only checks that the name is a regular file, not a
        link; on Windows only the mtime is set, after the fd is closed (os.utime takes no fd
        there, and exec bits don't apply)."""
        self.recheck()
        rel = self.rel + (name,)
        st = self.lstat(name)
        if st is None or kind(st) != FILE:
            raise _changed_at_target(rel)
        path = self.join(name)
        fd = _open_to_touch(path, rel)
        try:
            # fstat here as at the check, which took the ident from its hashing fd: on Windows,
            # Python 3.12+'s lstat may take the file id from GetFileInformationByName (64 bits)
            # while fstat takes it from the handle (up to 128 bits on ReFS), so the two st_ino
            # of one file can differ.
            if WINDOWS:
                if not _same_file(os.fstat(fd), ident):
                    raise _changed_at_target(rel)
            else:
                _touch_fd(fd, rel, mtime_ns, executable, ident)
        finally:
            os.close(fd)
        if WINDOWS:
            os.utime(path, ns=(time.time_ns(), mtime_ns))

    def set_group(self, name, gid):
        os.chown(self.join(name), -1, gid, follow_symlinks=False)

    def create_file(self, name):
        self.recheck()
        return os.open(self.join(name), _NEW_FILE_FLAGS, 0o666)

    def open_lock(self, create, exclusive):
        self.recheck()
        return Lock.open(self.join("lock"), create=create, exclusive=exclusive)

    def close(self):
        pass


def remove_entry(d, name, st):
    """Removes one non-directory, or a link, from d."""
    if WINDOWS and getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY:
        # a directory symlink or a junction
        d.rmdir(name)
    else:
        d.unlink(name)


def _walk(parent, name, tick, remove):
    """count_tree and remove_tree: one explicit stack of open directories, as deep as the
    tree, never wider."""
    count = 0
    # [handle, names left to look at, its name in the directory above]
    stack = []

    def push(d, name_in_parent):
        try:
            names = d.listdir()
        except BaseException:
            d.close()
            raise
        # sorted, and popped from the end, so a walk is the same on every run
        names.sort(reverse=True)
        stack.append([d, names, name_in_parent])

    try:
        push(parent.enter(name, owner_rule=False), name)
        while stack:
            d, names, own = stack[-1]
            if not names:
                stack.pop()
                d.close()
                above = stack[-1][0] if stack else parent
                if remove:
                    try:
                        above.rmdir(own)
                    except FileNotFoundError:
                        continue
                count += 1
                tick()
                continue
            n = names.pop()
            st = d.lstat(n)
            if st is None:
                continue
            if kind(st) == DIR:
                # The device rule still holds inside: nothing crosses into another file system.
                push(d.enter(n, owner_rule=False), n)
                continue
            if remove:
                try:
                    remove_entry(d, n, st)
                except FileNotFoundError:
                    continue
            count += 1
            tick()
    finally:
        for d, names, own in stack:
            d.close()
    return count


def count_tree(parent, name, tick):
    """How many entries the directory parent/name holds, itself included."""
    return _walk(parent, name, tick, False)


def remove_tree(parent, name, tick):
    """Removes the directory parent/name, bottom-up; returns how many entries it held, itself
    included. Links are removed, never entered."""
    return _walk(parent, name, tick, True)


# --- other programs (DESIGN, "Rules for the code") ---

# What fsops.run gives back: the exit code (None when the timeout killed the program), and
# stdout and stderr as bytes.
Ran = collections.namedtuple("Ran", "rc out err")

# How long run() waits for a killed program's output after its timeout.
KILL_WAIT = 5


def _kill(proc, group):
    """Kills proc; with group (POSIX, a new session), its whole process group, so a child it
    started, such as ssh's ProxyCommand, can't hold the pipes open."""
    if group:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run(argv, timeout, new_session=False, env=None):
    """Runs argv, an argument list and never a shell line, with an empty pipe closed at once as
    stdin and stdout and stderr captured; returns a Ran. env: the program's environment, as
    Popen takes it; None inherits this process's. After timeout seconds it kills the
    program and returns what it had printed, with rc None, or the exit code if the program
    itself had already exited. An OSError from starting it propagates. The one way vcharon runs
    another program, apart from the session's own ssh and vcharon key's ssh-add (run_terminal).

    The empty pipe, not the null device: a program that reads stdin must see the end of input.
    Win32-OpenSSH 9.5p2 never signals it for a null-device stdin, so a probe's ssh, whose
    remote command reads a loader line that a probe never sends, blocked until the timeout
    killed it (measured 2026-09-28 on Windows 11 26200); with a pipe it ends at once."""
    group = new_session and not WINDOWS
    extra = {"start_new_session": True} if group else {}
    proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, **extra)
    stuck = False
    try:
        try:
            # The empty input closes stdin at once; a later communicate passes none (the pipe is
            # closed, and Python refuses input twice).
            out, err = proc.communicate(input=b"", timeout=timeout)
            return Ran(proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            pass
        # The program may have exited, and only a child it left behind holds the pipes: then
        # its exit code stands.
        rc = proc.poll()
        _kill(proc, group)
        try:
            out, err = proc.communicate(timeout=KILL_WAIT)
        except subprocess.TimeoutExpired as e:
            # Something that outlived the kill still holds the pipes: keep what came.
            stuck = True
            out, err = e.stdout, e.stderr
        return Ran(rc, out or b"", err or b"")
    except BaseException:
        # Ctrl-C, or a bug: never leave the program running.
        _kill(proc, group)
        raise
    finally:
        # communicate() closes stdin, but a Ctrl-C can come before it does.
        try:
            proc.stdin.close()
        except OSError:
            pass
        # On Windows a reader thread of communicate() still holds stuck pipes, and close()
        # would block until it's done: leave them to it, as Session.close does.
        if not (stuck and WINDOWS):
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            proc.wait(KILL_WAIT)
        except subprocess.TimeoutExpired:
            pass


def _ansi_cp():
    """The ANSI code page as a codec name ("cp936" on a Chinese Windows), or None. UTF-8 mode
    doesn't change it."""
    try:
        return locale.getencoding()
    except (AttributeError, OSError, ValueError):
        return None


def child_encodings():
    """The codecs child_text() tries on Windows after UTF-8: the ANSI code page's, or none
    when it is UTF-8 (a code page without a codec of its own reads through mbcs).
    Win32-OpenSSH writes the system's text of some errors (a host name it can't resolve) in
    the ANSI code page. Not the console's
    code page: ssh sets it to UTF-8 for its run, and an OEM one (cp437, cp850, cp866) decodes
    any byte, so it would hide the ANSI text (DESIGN, "Launch rules"). Never raises."""
    try:
        name = codecs.lookup(_ansi_cp()).name
    except (LookupError, TypeError, ValueError, OSError):
        return []
    return [] if name == "utf-8" else [name]


# Looked up once at start, codecs and all: a long-running command imports nothing later
# (DESIGN, "Running watchers").
CHILD_ENCODINGS = child_encodings() if WINDOWS else []


def child_text(data):
    """Another program's output (bytes) as text to show or log: UTF-8 if it is valid UTF-8; on
    Windows, else the first of CHILD_ENCODINGS that decodes it all; else UTF-8 with U+FFFD for
    the bad bytes. Never raises."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if WINDOWS:
        for name in CHILD_ENCODINGS:
            try:
                return data.decode(name)
            except (UnicodeDecodeError, LookupError):
                pass
    return data.decode("utf-8", "replace")


def run_terminal(argv):
    """Runs argv on this terminal: stdin, stdout and stderr stay the terminal's, so a person
    can type into it; returns its exit code. No timeout. Only vcharon key uses it, for ssh-add,
    which asks for the passphrase itself: vcharon never sees it (DESIGN, "Rules for the code")."""
    return subprocess.call(list(argv))
