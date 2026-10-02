"""Read and check vcharon.ini (DESIGN §12)."""

from __future__ import annotations

import configparser
import dataclasses
import os
import posixpath
import re
import stat
import tempfile

from . import channels, fsops, pathrules, platform, ssh
from .helper import TICK_EVERY
from .proto import VCharonError
from .run import Side

# A busy helper ticks at most every TICK_EVERY seconds; a shorter idle limit would kill it.
MIN_IDLE = 3 * TICK_EVERY


@dataclasses.dataclass
class Settings:
    ssh_path: str = dataclasses.field(default_factory=platform.default_ssh_path)
    remote_python: str = "python3"
    connect_timeout: int = 10
    handshake_timeout: int = 30
    idle_timeout: int = 300
    run_timeout: int = 0
    compress: bool = False


@dataclasses.dataclass
class Mailbox:
    """A mailbox section's own values (DESIGN §12), on each job it expands into. Since M10
    every one is a channel section, [<channel>.<me>] in channels.d/."""

    section: str
    me: str
    local: str            # as written: absolute or ~, on the client
    remote: str           # as written, on the server
    leader: str = None    # the channel's leader, mailbox.leader
    channel: str = None   # the section name's first part

    @property
    def own_folder(self):
        """The writer's own folder on the client, as written; the run creates it."""
        return os.path.join(self.local, self.me)


@dataclasses.dataclass
class Job:
    """One of a channel section's two jobs (DESIGN §12). Its plugins and options are checked
    when it runs."""

    name: str
    ssh: str
    source: Side          # options as the raw strings
    sink: Side
    settings: Settings    # [vcharon]'s, with the section's overrides
    from_text: str        # the raw from and to values
    to_text: str
    # a job derived from a mailbox section: that section's values; else None
    mailbox: Mailbox = None


@dataclasses.dataclass
class Skipped:
    """A section vcharon goes on without (DESIGN §14 M10): a channels.d/ file that is broken or
    clashes, or a retired mailbox section in vcharon.ini. Only a command that names it fails, with
    its error; vcharon doctor lists it, every other command logs it."""

    where: str            # channels.d/<file>, or vcharon.ini [<section>]
    error: VCharonError     # code config; its message names where
    names: tuple          # the section and its jobs: what a command may name it by

    @property
    def problem(self):
        """The error's text without its leading where, for "skipped <where>: <problem>"."""
        text = self.error.message
        for head in (self.where + ": ", self.where + " "):
            if text.startswith(head):
                return text[len(head):]
        return text

    @property
    def line(self):
        return "skipped %s: %s" % (self.where, self.problem)


@dataclasses.dataclass
class Config:
    path: str
    exists: bool
    settings: Settings
    # name -> Job, in file order; a mailbox section's two jobs take its place
    jobs: dict = dataclasses.field(default_factory=dict)
    # mailbox section -> the names of its jobs, up first
    mailboxes: dict = dataclasses.field(default_factory=dict)
    # [vcharon] box: this box's name in channel members' names, or None: the OS's (box_name)
    box: str = None
    # [Skipped], vcharon.ini's first, then channels.d/'s in name order
    skipped: list = dataclasses.field(default_factory=list)

    @property
    def box_name(self):
        """The box in members' names: [vcharon] box, else this OS's word (mac, win,
        linux)."""
        return self.box or platform.os_word()

    @property
    def box_source(self):
        """Where box_name comes from: "config" or "os"."""
        return "config" if self.box else "os"

    def box_text(self):
        """box_name, and where it comes from, as whoami, doctor and setup print it."""
        if self.box:
            return "%s (set in %s)" % (self.box, self.path)
        return "%s (default, from the OS)" % self.box_name

    def named(self, name):
        """The jobs a name on the command line runs: a job's, or a mailbox section's two;
        None if it names neither."""
        if name in self.mailboxes:
            return [self.jobs[n] for n in self.mailboxes[name]]
        job = self.jobs.get(name)
        return None if job is None else [job]

    def skipped_for(self, name):
        """The Skipped a name on the command line names, or None. A name the config has
        (named) is never looked up here."""
        for skip in self.skipped:
            if name in skip.names:
                return skip
        # channels.d/ itself couldn't be listed: any name not found may be in it
        for skip in self.skipped:
            if not skip.names:
                return skip
        return None


_BOOLS = {"yes": True, "true": True, "1": True, "no": False, "false": False, "0": False}
_DIGITS = re.compile(r"\A[0-9]+\Z")
# A job's name names its state, lock and log files on every OS. At most 64 characters, so a
# "<job>.json.tmp" name stays far under 255 bytes.
_JOB_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# the [vcharon] settings a channel section may override
JOB_SETTINGS = ("idle_timeout", "run_timeout", "compress", "remote_python")
# A mailbox section [S] becomes the jobs S.up and S.down (DESIGN §12).
MAILBOX_KEYS = ("mailbox.me", "mailbox.leader", "mailbox.local", "mailbox.remote")
MAILBOX_JOBS = (".up", ".down")
# so S.down stays a job name
MAILBOX_NAME_MAX = 64 - len(".down")
# [vcharon] box: at most this long, so a member's name <box>-<project>-<role> fits 32 (M10)
BOX_MAX = 10
# next to the config file: one channel section per file (DESIGN §14 M10)
CHANNELS_DIR = "channels.d"
RETIRED = ("the fixed mailbox is retired (M10): delete [%s] from %s (MAILBOX.md in the "
           "vcharon folder, \"Retired\")")


def job_name_problem(name):
    """Why name can't name a job, or None."""
    if not _JOB_NAME.match(name):
        return ("a job name has only letters, digits, '.', '_' and '-', starts with a letter "
                "or digit, and is at most 64 characters long")
    if pathrules.is_reserved_windows(name):
        # its state, lock and log files could never be made on Windows
        return "%s is a reserved name on Windows" % name
    return None


def load():
    """Reads the config file (platform.config_path(), under VCHARON_HOME if set), which may be
    missing."""
    path = platform.config_path()
    hint = "fix %s" % path
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = None
    except OSError as e:
        raise VCharonError("config", "couldn't read %s: %s" % (path, e.strerror or e), hint=hint)
    if raw is None:
        cfg = Config(path, False, Settings())
        folded = {}
    else:
        name = os.path.basename(path)
        parser = _parse(raw, name, path, hint)
        settings = Settings()
        box = None
        if parser.has_section("vcharon"):
            box = _read_vcharon(parser, name, hint, settings)
        jobs, mailboxes, folded, skipped = _read_jobs(parser, name, hint, settings, path)
        cfg = Config(path, True, settings, jobs, mailboxes, box, skipped)
    _read_channels(cfg, folded)
    return cfg


def _parse(raw, name, source, hint):
    """A ConfigParser of one file's bytes, with vcharon's rules (DESIGN §12); config errors name
    the file as name."""
    try:
        # Windows Notepad may add a BOM.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise VCharonError("config", "%s isn't valid UTF-8 (byte %d)" % (name, e.start), hint=hint)
    # No interpolation and no inline comments, so "path = D:\Games #2" keeps its "#2". No
    # section can be named "\n", so none acts as [DEFAULT].
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None,
                                       comment_prefixes=("#", ";"), default_section="\n")
    try:
        parser.read_string(text, source=source)
    except configparser.Error as e:
        raise VCharonError("config", _parse_error(name, e), hint=hint)
    for section in parser.sections():
        # configparser would copy [DEFAULT]'s keys into every section.
        if section.upper() == "DEFAULT":
            raise VCharonError("config", "%s: a [%s] section isn't allowed" % (name, section),
                               hint=hint)
        for key, value in parser.items(section):
            # An indented line continues the previous value; refuse that rather than guess.
            if "\n" in value:
                raise VCharonError("config", "%s [%s] %s: the value continues on an indented line"
                                   % (name, section, key), hint=hint)
    return parser


def _parse_error(name, e):
    if isinstance(e, configparser.DuplicateSectionError):
        return "%s line %s: section [%s] appears twice" % (name, e.lineno, e.section)
    if isinstance(e, configparser.DuplicateOptionError):
        return "%s line %s: [%s] %s appears twice" % (name, e.lineno, e.section, e.option)
    if isinstance(e, configparser.MissingSectionHeaderError):
        return "%s line %s: a key outside any section: %s" % (name, e.lineno, e.line.strip())
    if isinstance(e, configparser.ParsingError) and e.errors:
        lineno, line = e.errors[0]
        return "%s line %s: can't read %s" % (name, lineno, line.strip())
    return "%s: %s" % (name, e.message)


def _read_vcharon(parser, name, hint, settings):
    """Reads [vcharon] into settings; returns its box, or None."""
    box = None
    for key, value in parser.items("vcharon"):
        where = "%s [vcharon] %s" % (name, key)
        if key == "box":
            problem = box_problem(value)
            if problem:
                raise VCharonError("config", "%s: %s" % (where, problem), hint=hint)
            box = value
        elif not _setting(settings, key, value, where, hint):
            raise VCharonError("config", "%s: unknown key" % where, hint=hint)
    return box


def box_problem(box):
    """Why box can't be [vcharon] box, or None: mailbox.me's rule, at most BOX_MAX long."""
    problem = pathrules.writer_problem(box)
    if problem is None and len(box) > BOX_MAX:
        problem = "it's longer than %d characters" % BOX_MAX
    if problem is None:
        return None
    return ("%s; a box's name is a writer's name of at most %d characters, such as mac, win, "
            "linux or laptop" % (problem, BOX_MAX))


# configparser's own section line (SECTCRE), after leading blanks
_SECTION = re.compile(r"\A\s*\[(.+)\]")
_BOX_LINE = re.compile(r"\A\s*box\s*[=:]", re.I)
# the commented line a bare vcharon setup writes; setup --box replaces it
BOX_COMMENT = "# box = %s   (the default: this OS); to set another: vcharon setup --box NAME"
_BOX_COMMENT = re.compile(r"\A# box = ")


def text_with_box(text, box):
    """vcharon.ini's text with [vcharon] box = box: a box line of [vcharon] replaced, else the
    commented one setup wrote, else a line added below [vcharon]; no [vcharon] gets one at the
    top. Edited line by line, not through configparser, which would drop the comments; every
    other line stays as it was, line ends too (\\r\\n kept)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    # split after each "\\n" only: a value's other line breaks stay inside its line
    lines = re.findall(r"[^\n]*\n|[^\n]+\Z", text)
    line = "box = %s%s" % (box, newline)
    header = None
    comment = None
    section = None
    for i, one in enumerate(lines):
        m = _SECTION.match(one)
        if m:
            section = m.group(1)
            if section == "vcharon" and header is None:
                header = i
            continue
        if section != "vcharon":
            continue
        if _BOX_LINE.match(one):
            lines[i] = line
            return "".join(lines)
        if comment is None and _BOX_COMMENT.match(one):
            comment = i
    if comment is not None:
        lines[comment] = line
    elif header is not None:
        if not lines[header].endswith(("\n", "\r")):
            lines[header] += newline
        lines.insert(header + 1, line)
    else:
        lines.insert(0, "[vcharon]%s%s%s" % (newline, line, newline if lines else ""))
    return "".join(lines)


SETUP_HINT = "pick another name: vcharon setup --box NAME"


def check_box(box):
    """--box's usage error, or nothing."""
    problem = box_problem(box)
    if problem:
        raise VCharonError("config", "--box %s: %s" % (pathrules.show(box), problem),
                           hint=SETUP_HINT)


def setup_file(box=None):
    """vcharon setup's write: with box, [vcharon] box = box in the config file (made when
    missing); without, the file made when missing, its box line commented out (the default
    stays the OS's), and an existing file left alone. The file must load before and after:
    a broken one is the user's to fix, never rewritten. Returns True if it wrote."""
    if box is not None:
        check_box(box)
    path = platform.config_path()
    hint = "fix %s" % path
    load()
    try:
        with open(path, "rb") as f:
            raw = f.read()
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        raw = None
        mode = None
    except OSError as e:
        raise VCharonError("config", "couldn't read %s: %s" % (path, e.strerror or e), hint=hint)
    if raw is None:
        text = "[vcharon]\n%s\n" % (BOX_COMMENT % platform.os_word() if box is None
                                     else "box = %s" % box)
        bom = b""
    elif box is None:
        return False
    else:
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        text = text_with_box(raw.decode("utf-8-sig"), box)
        if text == raw.decode("utf-8-sig"):
            return False
    data = bom + text.encode("utf-8")
    # what load() would read back
    parser = _parse(data, os.path.basename(path), path, hint)
    if box is not None and _read_vcharon(parser, os.path.basename(path), hint,
                                         Settings()) != box:
        raise VCharonError("internal", "setup's edit of %s doesn't read back box = %s"
                           % (path, box), hint="set box in [vcharon] of %s by hand" % path)
    # through a symlinked config (a dotfiles manager's): the file it points at is written,
    # the link stays
    _write_file(os.path.realpath(path), data, mode)
    return True


def _write_file(path, data, mode):
    """data to path through a temp file in its folder, then one os.replace; mode kept. On
    Windows a read-only file can't be replaced: its read-only flag is cleared first, and the
    new file gets the old mode back (read-only again)."""
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=folder, prefix=".vcharon-", suffix=".tmp")
    except OSError as e:
        raise fsops.error(e, folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp's 0600; an existing file keeps its own
        if mode is not None:
            os.chmod(temp, mode)
        if fsops.WINDOWS and mode is not None and not mode & stat.S_IWRITE:
            os.chmod(path, mode | stat.S_IWRITE)
            try:
                fsops.retry_in_use(os.replace, temp, path)
            except BaseException:
                try:
                    os.chmod(path, mode)
                except OSError:
                    pass
                raise
        else:
            fsops.retry_in_use(os.replace, temp, path)
    except BaseException as e:
        try:
            os.remove(temp)
        except OSError:
            pass
        if isinstance(e, OSError):
            raise fsops.error(e, path)
        raise


def _setting(settings, key, value, where, hint):
    """Checks one [vcharon] setting and sets it; False for a key that isn't one. A channel section's
    overrides come through here too, so they get exactly the same checks."""
    if key == "ssh_path":
        path = os.path.expanduser(value)
        # Launch rules (DESIGN §13): never rely on PATH or the current directory.
        if not os.path.isabs(path):
            raise VCharonError("config", "%s: must be an absolute path" % where, hint=hint)
        settings.ssh_path = path
    elif key == "remote_python":
        if not value:
            raise VCharonError("config", "%s: can't be empty" % where, hint=hint)
        # It goes into the bootstrap's shell line as it is.
        if any(c in value for c in "'\"\\"):
            raise VCharonError("config", "%s: may not contain quotes or backslashes" % where,
                               hint=hint)
        # ssh parses options again after the destination, so "-oProxyCommand=..." here
        # would be an ssh option.
        if value.startswith("-"):
            raise VCharonError("config", "%s: can't start with '-'" % where, hint=hint)
        settings.remote_python = value
    elif key in ("connect_timeout", "handshake_timeout"):
        if not _DIGITS.match(value) or int(value) < 1:
            raise VCharonError("config", "%s: must be a whole number of seconds, 1 or more"
                               % where, hint=hint)
        setattr(settings, key, int(value))
    elif key == "idle_timeout":
        if not _DIGITS.match(value) or int(value) < MIN_IDLE:
            raise VCharonError("config", "%s: must be a whole number of seconds, %d or more "
                               "(three times the helper's %d s tick interval)"
                               % (where, MIN_IDLE, TICK_EVERY), hint=hint)
        settings.idle_timeout = int(value)
    elif key == "run_timeout":
        if not _DIGITS.match(value):
            raise VCharonError("config", "%s: must be a whole number of seconds, 0 or more"
                               % where, hint=hint)
        settings.run_timeout = int(value)
    elif key == "compress":
        if value.lower() not in _BOOLS:
            raise VCharonError("config", "%s: must be yes or no" % where, hint=hint)
        settings.compress = _BOOLS[value.lower()]
    else:
        return False
    return True


def _read_jobs(parser, name, hint, settings, path):
    """The sections besides [vcharon]; (name -> Job, mailbox sections, the folded names taken,
    [Skipped]). The config file holds no jobs of its own: a section here with mailbox keys is
    the retired fixed mailbox, skipped (M10) with its names still taken; any other section is
    refused. Channel sections live in channels.d/."""
    jobs = {}
    mailboxes = {}
    skipped = []
    # folded name -> the section that has it, or "[S] makes S.up" for a derived job
    folded = {}
    for section in parser.sections():
        if section == "vcharon":
            continue
        # configparser keeps section names as written, so [VCharon] would be a job
        if section.casefold() == "vcharon":
            raise VCharonError("config", "%s [%s]: the global section is spelled [vcharon]"
                               % (name, section), hint=hint)
        if not any(key.startswith("mailbox.") for key in parser.options(section)):
            raise VCharonError("config", "%s [%s]: %s holds only [vcharon]; a channel's section "
                               "goes in %s, which vcharon join writes"
                               % (name, section, name, CHANNELS_DIR), hint=hint)
        problem = job_name_problem(section)
        if problem:
            raise VCharonError("config", "%s [%s]: %s" % (name, section, problem), hint=hint)
        # Windows file names ignore case, and the job's files are named after it.
        other = folded.get(section.casefold())
        if other is not None:
            raise VCharonError("config", "%s [%s]: the same name as %s when case is ignored"
                               % (name, section, other), hint=hint)
        folded[section.casefold()] = "[%s]" % section
        names = (section,) + tuple(section + suffix for suffix in MAILBOX_JOBS)
        for job_name in names[1:]:
            other = folded.get(job_name.casefold())
            if other is not None:
                raise VCharonError("config", "%s [%s]: its job %s has the same name as %s when "
                                   "case is ignored" % (name, section, job_name, other),
                                   hint=hint)
            folded[job_name.casefold()] = "the job %s of [%s]" % (job_name, section)
        # Skipped, not fatal: a box that still has its [mailbox] keeps its channels running.
        skipped.append(Skipped("%s [%s]" % (name, section),
                               VCharonError("config", RETIRED % (section, name), hint=hint), names))
    return jobs, mailboxes, folded, skipped


def channels_dir(config_path):
    """channels.d/ next to the config file (DESIGN §14 M10)."""
    return os.path.join(os.path.dirname(config_path), CHANNELS_DIR)


def _read_channels(cfg, folded):
    """Adds channels.d/'s sections to cfg, after vcharon.ini's: each file whose name ends in
    exactly .ini and doesn't start with ".", in name order. A file that is broken or clashes
    is skipped (cfg.skipped), never fatal here: one agent's broken channel can't stop the
    user's other channels."""
    folder = channels_dir(cfg.path)
    try:
        # os.listdir, not a glob: a glob ignores case on Windows, so X.INI would match
        files = sorted(f for f in os.listdir(folder)
                       if f.endswith(".ini") and not f.startswith("."))
    except FileNotFoundError:
        return
    except OSError as e:
        cfg.skipped.append(Skipped(CHANNELS_DIR, VCharonError(
            "config", "%s: can't be listed: %s" % (CHANNELS_DIR, e.strerror or e),
            hint="fix %s" % folder), ()))
        return
    # the local tree's real path, as this OS compares names -> (section, where, names)
    trees = {}
    for f in files:
        where = "%s/%s" % (CHANNELS_DIR, f)
        section = f[:-len(".ini")]
        names = (section,) + tuple(section + suffix for suffix in MAILBOX_JOBS)
        path = os.path.join(folder, f)
        hint = "fix %s, or delete it" % path
        try:
            derived = _read_channel_file(path, where, section, hint, cfg.settings)
            for n in names:
                other = folded.get(n.casefold())
                if other is not None:
                    raise VCharonError("config", "%s [%s]: %s has the same name as %s when case "
                                       "is ignored" % (where, section, n, other), hint=hint)
        except VCharonError as e:
            cfg.skipped.append(Skipped(where, e, names))
            continue
        # one local tree per (channel, member): two sections pulling into one tree would
        # each pull the other's own folder over its own. Both are skipped, each naming the
        # other (a third names the first).
        local = derived[0].mailbox.local
        key = pathrules.fold(os.path.realpath(os.path.expanduser(local)), platform.os_name())
        first = trees.get(key)
        if first is not None:
            f_section, f_where, f_names = first
            for s_where, s_section, s_names, other, other_where in (
                    (f_where, f_section, f_names, section, where),
                    (where, section, names, f_section, f_where)):
                if s_section in cfg.mailboxes:
                    for n in cfg.mailboxes.pop(s_section):
                        cfg.jobs.pop(n, None)
                elif s_section != section:
                    continue
                cfg.skipped.append(Skipped(s_where, VCharonError(
                    "config", "%s [%s]: its mailbox.local is the local tree of [%s] (%s) too; "
                    "each section needs its own" % (s_where, s_section, other, other_where),
                    hint="fix %s, or delete it" % os.path.join(folder, s_where.split("/", 1)[1])),
                    s_names))
            continue
        trees[key] = (section, where, names)
        for job in derived:
            folded[job.name.casefold()] = "the job %s of %s" % (job.name, where)
            cfg.jobs[job.name] = job
        folded[section.casefold()] = "[%s] of %s" % (section, where)
        cfg.mailboxes[section] = [job.name for job in derived]


def _read_channel_file(path, where, section, hint, settings):
    """The two jobs of one channels.d/ file, which holds exactly one channel section, named as
    the file without .ini."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise VCharonError("config", "%s: can't be read: %s" % (where, e.strerror or e),
                           hint=hint)
    parser = _parse(raw, where, path, hint)
    sections = parser.sections()
    if any(s.casefold() == "vcharon" for s in sections):
        raise VCharonError("config", "%s: [vcharon] goes in vcharon.ini, not in %s"
                           % (where, CHANNELS_DIR), hint=hint)
    if sections != [section]:
        raise VCharonError("config", "%s: holds exactly one section, [%s], named as the file"
                           % (where, section), hint=hint)
    problem = job_name_problem(section)
    if problem:
        raise VCharonError("config", "%s [%s]: %s" % (where, section, problem), hint=hint)
    if not any(key.startswith("mailbox.") for key in parser.options(section)):
        raise VCharonError("config", "%s [%s]: holds only a channel section, with mailbox keys"
                           % (where, section), hint=hint)
    return _read_mailbox(parser, section, where, hint, settings)


def _read_mailbox(parser, section, name, hint, base):
    """A channel section (DESIGN §12, §14 M10): its two jobs, up then down. Up pushes the
    writer's own folder; down pulls the rest of the tree, less the writer's own folder."""
    def refuse(key, what):
        where = "%s [%s]" % (name, section) + (" " + key if key else "")
        raise VCharonError("config", "%s: %s" % (where, what), hint=hint)

    if len(section) > MAILBOX_NAME_MAX:
        refuse(None, "a mailbox section's name is at most %d characters long, so the names "
                     "of its jobs, with .up and .down, fit" % MAILBOX_NAME_MAX)
    settings = dataclasses.replace(base)
    values = {}
    for key, value in parser.items(section):
        if key == "ssh" or key in MAILBOX_KEYS:
            values[key] = value
        elif key in JOB_SETTINGS:
            _setting(settings, key, value, "%s [%s] %s" % (name, section, key), hint)
        elif key in ("from", "to") or key.startswith(("from.", "to.")):
            refuse(key, "a mailbox section sets its own from and to; it can't have this key")
        else:
            refuse(key, "unknown key")
    for key in ("ssh",) + MAILBOX_KEYS:
        if key not in values:
            refuse(key, "required")
    try:
        ssh.check_dest(values["ssh"])
    except VCharonError as e:
        refuse("ssh", e.message)
    me = values["mailbox.me"]
    problem = pathrules.writer_problem(me)
    if problem:
        refuse("mailbox.me", problem)
    leader = values["mailbox.leader"]
    problem = pathrules.writer_problem(leader)
    if problem:
        refuse("mailbox.leader", problem)
    # [<channel>.<me>]: one section per channel and member, as vcharon join names it
    channel = section[:-len(me) - 1] if section.endswith("." + me) else ""
    if not channel:
        refuse(None, "a channel section is named <channel>.<mailbox.me>, here [<channel>.%s]"
                     % me)
    problem = channels.channel_problem(channel)
    if problem:
        refuse(None, problem)
    local = values["mailbox.local"]
    # DESIGN §9.1: a hotkey run's current directory is unpredictable.
    if not (local.startswith("~") or os.path.isabs(local)):
        refuse("mailbox.local", "a local path must be absolute or start with ~")
    remote = values["mailbox.remote"]
    if not remote:
        refuse("mailbox.remote", "can't be empty")
    # down prunes the whole tree: never the server's home or its root
    if posixpath.normpath(remote) in ("/", "//", ".", "~"):
        refuse("mailbox.remote", "can't be the server's home or its root; give the tree a "
                                 "folder of its own, such as ~/.local/state/vcharon/mailbox")
    box = Mailbox(section, me, local, remote, leader, channel)
    up = Job(section + ".up", values["ssh"],
             Side("local", "path", {"path": box.own_folder, "prune": "yes",
                                    "allow_empty": "yes"}),
             # never create (M10): the claim made <remote>/<me>; with create, a member's up
             # would make a closed channel again
             Side("remote", "dir", {"path": posixpath.join(remote, me), "create": "no"}),
             settings, "local:path", "remote:dir", box)
    # mailbox_me: down plans only the other writers' folders at the top of the tree, and
    # leaves out everything else there, <me>/ in any case included (DESIGN §9.2, §12)
    down = Job(section + ".down", values["ssh"],
               Side("remote", "path", {"path": remote, "mailbox_me": me, "prune": "yes",
                                       "allow_empty": "yes"}),
               Side("local", "dir", {"path": local, "create": "yes"}),
               settings, "remote:path", "local:dir", box)
    return [up, down]
