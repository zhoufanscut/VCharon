"""Reading and checking vcharon.ini."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from vcharon import config, platform
from vcharon.proto import VCharonError
from vcharon.run import Side

WINDOWS = os.name == "nt"

# a job section, which the config file no longer takes
JOB = """
[j]
ssh = devbox
from = local:path
from.path = ~/src
to = remote:dir
to.path = inbox
"""

# a vcharon.ini that holds settings only
VCHARON = "[vcharon]\ncompress = yes\n"


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"VCHARON_HOME": self.tmp, "HOME": self.tmp,
                                               "USERPROFILE": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self.tmp, "vcharon.ini")

    def write(self, data):
        with open(self.path, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)

    def load(self, data):
        self.write(data)
        return config.load()

    def refused(self, data, *words):
        with self.assertRaises(VCharonError) as cm:
            self.load(data)
        self.assertEqual(cm.exception.code, "config")
        self.assertEqual(cm.exception.exit_code, 3)
        for word in words:
            self.assertIn(word, cm.exception.message)
        return cm.exception


class ConfigTest(ConfigCase):
    def test_no_file_means_defaults(self):
        cfg = config.load()
        self.assertEqual(cfg.path, self.path)
        self.assertFalse(cfg.exists)
        self.assertEqual(cfg.settings, config.Settings())
        s = cfg.settings
        self.assertEqual((s.ssh_path, s.remote_python, s.connect_timeout, s.handshake_timeout,
                          s.idle_timeout, s.run_timeout, s.compress),
                         (platform.default_ssh_path(), "python3", 10, 30, 300, 0, False))

    def test_vcharon_home_is_the_default_path(self):
        cfg = self.load("[vcharon]\nidle_timeout = 70\n")
        self.assertEqual(cfg.path, os.path.join(self.tmp, "vcharon.ini"))
        self.assertTrue(cfg.exists)
        self.assertEqual(cfg.settings.idle_timeout, 70)

    def test_bom(self):
        cfg = self.load(b"\xef\xbb\xbf[vcharon]\nremote_python = python3.13\n")
        self.assertEqual(cfg.settings.remote_python, "python3.13")

    def test_hash_kept_inside_a_value(self):
        cfg = self.load("[vcharon]\nremote_python = py #2\n")
        self.assertEqual(cfg.settings.remote_python, "py #2")

    def test_comment_lines(self):
        cfg = self.load("# top\n; also\n[vcharon]\n# ssh_path = relative\n  ; indented\n"
                        "idle_timeout = 90\n")
        self.assertEqual(cfg.settings.idle_timeout, 90)

    def test_default_section_refused(self):
        self.refused("[DEFAULT]\nidle_timeout = 5\n", "[DEFAULT]")
        self.refused("[vcharon]\n[default]\nx = 1\n", "[default]")

    def test_continuation_line_refused(self):
        self.refused("[vcharon]\nremote_python = python3\n  -X dev\n", "remote_python", "indented")

    def test_unknown_key(self):
        self.refused("[vcharon]\nidle_timeuot = 5\n", "vcharon.ini [vcharon] idle_timeuot",
                     "unknown key")

    def test_duplicate_key(self):
        self.refused("[vcharon]\nidle_timeout = 5\nidle_timeout = 6\n", "line 3", "twice")

    def test_duplicate_section(self):
        self.refused("[vcharon]\n[vcharon]\n", "twice")

    def test_key_outside_a_section(self):
        self.refused("idle_timeout = 5\n", "line 1")

    def test_bad_utf8(self):
        self.refused(b"[vcharon]\nremote_python = \xff\n", "UTF-8")

    def test_numbers(self):
        for key in ("connect_timeout", "handshake_timeout"):
            for value in ("0", "-5", "abc", "1.5", "", "+3", " 1_0"):
                err = self.refused("[vcharon]\n%s = %s\n" % (key, value), key,
                                   "whole number of seconds, 1 or more")
                self.assertIn("[vcharon] %s" % key, err.message)
            self.assertEqual(getattr(self.load("[vcharon]\n%s = 1\n" % key).settings, key), 1)
        # The helper ticks every 10 s, so less than three ticks would kill a busy helper.
        for value in ("0", "1", "29", "-30", "abc", "30.5"):
            self.refused("[vcharon]\nidle_timeout = %s\n" % value, "[vcharon] idle_timeout",
                         "whole number of seconds, 30 or more", "10 s tick")
        self.assertEqual(self.load("[vcharon]\nidle_timeout = 30\n").settings.idle_timeout, 30)
        self.refused("[vcharon]\nrun_timeout = -1\n", "0 or more")
        self.refused("[vcharon]\nrun_timeout = x\n", "0 or more")
        self.assertEqual(self.load("[vcharon]\nrun_timeout = 0\n").settings.run_timeout, 0)
        self.assertEqual(self.load("[vcharon]\nrun_timeout = 60\n").settings.run_timeout, 60)

    def test_message_format(self):
        err = self.refused("[vcharon]\nconnect_timeout = 0\n")
        self.assertEqual(err.message, "vcharon.ini [vcharon] connect_timeout: must be a whole "
                         "number of seconds, 1 or more")
        self.assertIn(self.path, err.hint)
        err = self.refused("[vcharon]\nidle_timeout = 10\n")
        self.assertEqual(err.message, "vcharon.ini [vcharon] idle_timeout: must be a whole number "
                         "of seconds, 30 or more (three times the helper's 10 s tick interval)")

    def test_bools(self):
        for value, want in (("yes", True), ("YES", True), ("true", True), ("1", True),
                            ("no", False), ("False", False), ("0", False)):
            self.assertIs(self.load("[vcharon]\ncompress = %s\n" % value).settings.compress, want)
        self.refused("[vcharon]\ncompress = maybe\n", "yes or no")

    def test_remote_python(self):
        for value in ("python3'", 'py"', "py\\3"):
            self.refused("[vcharon]\nremote_python = %s\n" % value, "quotes or backslashes")
        self.refused("[vcharon]\nremote_python =\n", "empty")
        # ssh would read it as an option, since it parses options after the destination too
        self.refused("[vcharon]\nremote_python = -oProxyCommand=touch /tmp/x\n",
                     "remote_python", "can't start with '-'")
        cfg = self.load("[vcharon]\nremote_python = /opt/py/bin/python3\n")
        self.assertEqual(cfg.settings.remote_python, "/opt/py/bin/python3")

    def test_ssh_path(self):
        self.refused("[vcharon]\nssh_path = ssh\n", "absolute")
        self.refused("[vcharon]\nssh_path = bin/ssh\n", "absolute")
        cfg = self.load("[vcharon]\nssh_path = ~/bin/ssh\n")
        self.assertEqual(os.path.normpath(cfg.settings.ssh_path),
                         os.path.join(self.tmp, "bin", "ssh"))
        absolute = os.path.join(self.tmp, "ssh")
        cfg = self.load("[vcharon]\nssh_path = %s\n" % absolute)
        self.assertEqual(cfg.settings.ssh_path, absolute)

    def test_jobs_leave_the_settings_alone(self):
        folder = os.path.join(self.tmp, "channels.d")
        os.mkdir(folder)
        with open(os.path.join(folder, "ch.windows.ini"), "w", encoding="utf-8") as f:
            f.write(MAILBOX + "idle_timeout = 60\n")
        cfg = self.load(VCHARON)
        self.assertEqual(cfg.settings, config.Settings(compress=True))
        self.assertEqual(cfg.jobs["ch.windows.up"].settings.idle_timeout, 60)

    def test_no_job_sections(self):
        # the config file holds settings only; a channel's section lives in channels.d/
        for text in (JOB, VCHARON + JOB.replace("[j]", "[ch.windows]")):
            with self.subTest(text=text):
                e = self.refused(text)
                section = "[ch.windows]" if "[ch.windows]" in text else "[j]"
                self.assertEqual(e.message, "vcharon.ini %s: vcharon.ini holds only [vcharon]; a "
                                            "channel's section goes in channels.d, which vcharon "
                                            "join writes" % section)


# A channel section: the retired fixed mailbox's [mailbox], now in channels.d/, with a
# leader and a <channel>.<me> name.
MAILBOX = """
[ch.windows]
ssh            = devbox
mailbox.me     = windows
mailbox.leader = laptop-ui
mailbox.local  = ~/vcharon_mailbox
mailbox.remote = ~/vcharon_mailbox
"""
WHERE = "channels.d/ch.windows.ini [ch.windows]"


class MailboxCase(ConfigCase):
    def write_channel(self, text, file="ch.windows.ini"):
        folder = os.path.join(self.tmp, "channels.d")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, file), "wb") as f:
            f.write(text.encode("utf-8") if isinstance(text, str) else text)

    def load_channel(self, text, ini="", file="ch.windows.ini"):
        """vcharon.ini holds ini, channels.d/<file> holds text; the config."""
        self.write_channel(text, file)
        return self.load(ini)

    def channel_refused(self, text, ini="", file="ch.windows.ini"):
        """The channels.d/ file is skipped: its error, which a command naming it fails with;
        the config itself loads."""
        cfg = self.load_channel(text, ini, file)
        section = file[:-len(".ini")]
        self.assertNotIn(section, cfg.mailboxes)
        skip = cfg.skipped_for(section)
        self.assertIsNotNone(skip, cfg.skipped)
        self.assertEqual(skip.where, "channels.d/" + file)
        self.assertEqual(skip.error.code, "config")
        self.assertEqual(skip.error.exit_code, 3)
        for name in (section + ".up", section + ".down"):
            self.assertIs(cfg.skipped_for(name), skip)
        return skip.error


class MailboxTest(MailboxCase):
    """A channel section in channels.d/ and its two jobs."""

    def test_two_jobs(self):
        cfg = self.load_channel(MAILBOX + "idle_timeout = 600\n", VCHARON)
        self.assertEqual(list(cfg.jobs), ["ch.windows.up", "ch.windows.down"])
        self.assertEqual(cfg.mailboxes, {"ch.windows": ["ch.windows.up", "ch.windows.down"]})
        up, down = cfg.jobs["ch.windows.up"], cfg.jobs["ch.windows.down"]
        own = os.path.join("~/vcharon_mailbox", "windows")
        self.assertEqual((up.ssh, up.from_text, up.to_text), ("devbox", "local:path", "remote:dir"))
        # the channel's folder limits: the defaults, as the section sets none
        sizes = {"max_bytes": "50000000", "max_files": "1000"}
        self.assertEqual(up.source, Side("local", "path", dict({"path": own, "prune": "yes",
                                                                "allow_empty": "yes"}, **sizes)))
        # up never creates; the claim made the member's folder
        self.assertEqual(up.sink, Side("remote", "dir", {"path": "~/vcharon_mailbox/windows",
                                                         "create": "no"}))
        self.assertEqual((down.from_text, down.to_text), ("remote:path", "local:dir"))
        self.assertEqual(down.source, Side("remote", "path", {
            "path": "~/vcharon_mailbox", "mailbox_me": "windows", "prune": "yes",
            "allow_empty": "yes", **sizes}))
        self.assertEqual(down.sink, Side("local", "dir", {"path": "~/vcharon_mailbox",
                                                          "create": "yes"}))
        for job in (up, down):
            self.assertEqual(job.settings.idle_timeout, 600)
            self.assertEqual(job.mailbox, config.Mailbox("ch.windows", "windows",
                                                         "~/vcharon_mailbox", "~/vcharon_mailbox",
                                                         "laptop-ui", "ch"))
        self.assertEqual([j.name for j in cfg.named("ch.windows")],
                         ["ch.windows.up", "ch.windows.down"])
        self.assertEqual([j.name for j in cfg.named("ch.windows.down")], ["ch.windows.down"])
        self.assertIsNone(cfg.named("nope"))
        self.assertEqual(cfg.skipped, [])

    def test_missing_keys(self):
        for key in ("ssh", "mailbox.me", "mailbox.leader", "mailbox.local", "mailbox.remote"):
            with self.subTest(key=key):
                text = "".join(line + "\n" for line in MAILBOX.splitlines()
                               if not line.startswith(key + " "))
                e = self.channel_refused(text)
                self.assertEqual(e.message, "%s %s: required" % (WHERE, key))

    def test_writer_names(self):
        for me in ("a", "0", "win-1_x", "x" * 32):
            with self.subTest(me=me):
                # each its own local tree: one tree per section
                cfg = self.load_channel(MAILBOX.replace("windows", me).replace(
                    "local  = ~/vcharon_mailbox", "local  = ~/l-" + me), file="ch.%s.ini" % me)
                self.assertEqual(cfg.jobs["ch.%s.up" % me].mailbox.me, me)
        # the name is checked before the section's: the file keeps its name
        for me in ("Windows", "WIN", "wIndows", "mAc", "..", ".vcharon-stage-x", "x" * 33, "-a",
                   "_a", "a.b",
                   "a/b", "a b", "ä"):
            with self.subTest(me=me):
                e = self.channel_refused(MAILBOX.replace("= windows", "= %s" % me))
                self.assertEqual(e.message, "%s mailbox.me: a writer's name has only lowercase "
                                            "letters, digits, '-' and '_', starts with a letter "
                                            "or digit, and is at most 32 characters long" % WHERE)
        # matches the pattern, but a Windows client can't hold the folder
        for me in ("con", "nul", "aux", "prn", "com1", "lpt9"):
            with self.subTest(me=me):
                e = self.channel_refused(MAILBOX.replace("= windows", "= %s" % me))
                self.assertEqual(e.message, "%s mailbox.me: %s is a reserved name on Windows, "
                                            "so a Windows client can't hold its folder"
                                 % (WHERE, me))
        for me in ("CON", "Nul"):
            with self.subTest(me=me):
                e = self.channel_refused(MAILBOX.replace("= windows", "= %s" % me))
                self.assertIn("a writer's name has only lowercase letters", e.message)
        for me in ("con1", "com", "conx", "com0"):
            with self.subTest(me=me):
                self.load_channel(MAILBOX.replace("windows", me).replace(
                    "local  = ~/vcharon_mailbox", "local  = ~/l-" + me), file="ch.%s.ini" % me)
        # the leader follows the same rule
        for leader in ("Laptop-ui", "con", "x" * 33):
            with self.subTest(leader=leader):
                e = self.channel_refused(MAILBOX.replace("= laptop-ui", "= %s" % leader))
                self.assertTrue(e.message.startswith("%s mailbox.leader: " % WHERE), e.message)

    def test_mailbox_me_is_a_mailbox_sections_own(self):
        # down's path option: no one is told of it
        from vcharon import plugin
        from vcharon.proto import VCharonError
        with self.assertRaises(VCharonError) as cm:
            plugin.check_side("remote", "path", "source", {"path": "x", "nope": "1"})
        self.assertEqual(cm.exception.hint, "the path plugin's options are: path, keep_name, "
                                            "exclude, symlinks, prune, allow_empty")

    def test_no_from_or_to(self):
        for line in ("from = local:path", "to = remote:dir", "from.path = ~/x", "to.create = yes",
                     "from.exclude = x"):
            with self.subTest(line=line):
                key = line.split(" = ")[0]
                e = self.channel_refused(MAILBOX + line + "\n")
                self.assertEqual(e.message, "%s %s: a mailbox section sets its own from and to; "
                                            "it can't have this key" % (WHERE, key))
        for line in ("mailbox.you = x", "path = x", "ssh_path = /usr/bin/ssh", "box = mac"):
            with self.subTest(line=line):
                e = self.channel_refused(MAILBOX + line + "\n")
                self.assertEqual(e.message, "%s %s: unknown key" % (WHERE, line.split(" = ")[0]))
        e = self.channel_refused(MAILBOX + "compress = maybe\n")
        self.assertEqual(e.message, "%s compress: must be yes or no" % WHERE)

    def test_paths(self):
        e = self.channel_refused(MAILBOX.replace("mailbox.local  = ~/vcharon_mailbox",
                                                 "mailbox.local  = vcharon_mailbox"))
        self.assertEqual(e.message, "%s mailbox.local: a local path must be absolute or start "
                                    "with ~" % WHERE)
        e = self.channel_refused(MAILBOX.replace("mailbox.remote = ~/vcharon_mailbox",
                                                 "mailbox.remote ="))
        self.assertEqual(e.message, "%s mailbox.remote: can't be empty" % WHERE)
        absolute = os.path.join(self.tmp, "box")
        cfg = self.load_channel(MAILBOX.replace("mailbox.local  = ~/vcharon_mailbox",
                                                "mailbox.local  = " + absolute)
                                .replace("mailbox.remote = ~/vcharon_mailbox",
                                         "mailbox.remote = box"))
        self.assertEqual(cfg.jobs["ch.windows.up"].source.options["path"],
                         os.path.join(absolute, "windows"))
        # relative to the server's home
        self.assertEqual(cfg.jobs["ch.windows.up"].sink.options["path"], "box/windows")
        self.assertEqual(cfg.jobs["ch.windows.down"].source.options["path"], "box")

    def test_section_names(self):
        # <channel>.<me>: the channel at most 24, so 24 + 1 + 32 fits MAILBOX_NAME_MAX
        channel, me = "c" * 24, "w" * 32
        name = "%s.%s" % (channel, me)
        self.assertLessEqual(len(name), config.MAILBOX_NAME_MAX)
        cfg = self.load_channel(MAILBOX.replace("ch.windows", name).replace("= windows",
                                                                           "= " + me),
                                file=name + ".ini")
        self.assertEqual(list(cfg.jobs), [name + ".up", name + ".down"])
        self.assertEqual(cfg.jobs[name + ".up"].mailbox.channel, channel)
        long = "c" * 25 + ".windows"
        e = self.channel_refused(MAILBOX.replace("ch.windows", long), file=long + ".ini")
        self.assertEqual(e.message, "channels.d/%s.ini [%s]: a channel's name is at most 24 "
                                    "characters long" % (long, long))
        e = self.channel_refused(MAILBOX.replace("ch.windows", "con.windows"),
                                 file="con.windows.ini")
        # a section name Windows reserves names no job
        self.assertIn("con.windows is a reserved name on Windows", e.message)
        # the section is named <channel>.<mailbox.me>
        for section in ("windows", "ch.mac", "ch.x.windows", "Ch.windows"):
            with self.subTest(section=section):
                e = self.channel_refused(MAILBOX.replace("ch.windows", section),
                                         file=section + ".ini")
                self.assertTrue(e.message.startswith("channels.d/%s.ini [%s]: a channel" % (
                    section, section)), e.message)

    def test_fingerprint_follows_the_writer_and_paths(self):
        from vcharon import state
        base = self.load_channel(MAILBOX)
        prints = {name[len("ch.windows"):]: state.fingerprint(job)
                  for name, job in base.jobs.items()}
        for old, new in (("= windows", "= mac"),
                         ("mailbox.local  = ~/vcharon_mailbox", "mailbox.local  = ~/box"),
                         ("mailbox.remote = ~/vcharon_mailbox", "mailbox.remote = ~/box")):
            with self.subTest(new=new):
                text = MAILBOX.replace(old, new)
                file = "ch.windows.ini"
                if new == "= mac":
                    text, file = text.replace("[ch.windows]", "[ch.mac]"), "ch.mac.ini"
                    os.remove(os.path.join(self.tmp, "channels.d", "ch.windows.ini"))
                cfg = self.load_channel(text, file=file)
                for name, job in cfg.jobs.items():
                    # down too: <me> reaches down's sides only through its mailbox_me
                    # option, so the fingerprint adds the writer's name (DESIGN §11.2)
                    self.assertNotEqual(state.fingerprint(job), prints[name[name.rindex("."):]],
                                        name)
                if file != "ch.windows.ini":
                    os.remove(os.path.join(self.tmp, "channels.d", file))
        # the leader isn't in it: it changes nothing vcharon sends
        cfg = self.load_channel(MAILBOX.replace("= laptop-ui", "= mac-other"))
        self.assertEqual({n[len("ch.windows"):]: state.fingerprint(j)
                          for n, j in cfg.jobs.items()}, prints)
        # the list it hashes: the derived sides, then the writer's name
        down = base.jobs["ch.windows.down"]
        want = json.dumps(["devbox", "remote:path", "local:dir", "~/vcharon_mailbox",
                           "~/vcharon_mailbox", "windows"], separators=(",", ":"))
        self.assertEqual(state.fingerprint(down), hashlib.sha256(want.encode()).hexdigest())

    def test_down_options_pass_the_path_source(self):
        from vcharon import plugin
        options = plugin.check_side("remote", "path", "source",
                                    self.load_channel(MAILBOX).jobs["ch.windows.down"]
                                    .source.options)
        self.assertEqual(options["mailbox_me"], "windows")
        self.assertEqual(options["exclude"], [])
        # the helper checks it again: the options came over the wire
        from vcharon.proto import VCharonError
        for raw, message in (({"mailbox_me": "Windows"}, "from.mailbox_me: a writer's name "
                              "has only lowercase letters"),
                             ({"mailbox_me": "nul"}, "from.mailbox_me: nul is a reserved name "
                              "on Windows"),
                             ({"mailbox_me": "mac", "keep_name": "yes"},
                              "from.mailbox_me can't go with keep_name")):
            with self.subTest(raw=raw):
                with self.assertRaises(VCharonError) as cm:
                    plugin.check_side("remote", "path", "source", dict(raw, path="box"))
                self.assertEqual(cm.exception.code, "bad_options")
                self.assertTrue(cm.exception.message.startswith(message), cm.exception.message)

    def test_remote_isnt_the_home_or_the_root(self):
        for value in ("~", "~/", "/", ".", "./", "~/.", "//"):
            with self.subTest(value=value):
                e = self.channel_refused(MAILBOX.replace("mailbox.remote = ~/vcharon_mailbox",
                                                         "mailbox.remote = " + value))
                self.assertEqual(e.message, "%s mailbox.remote: can't be the server's home or "
                                            "its root; give the tree a folder of its own, such "
                                            "as ~/.local/state/vcharon/mailbox" % WHERE)


class ChannelsDirTest(MailboxCase):
    """channels.d/ (DESIGN, "Config"): one file per section, read after vcharon.ini; a
    broken or clashing file is skipped, fatal only for a command that names it."""

    def test_one_section_per_file_in_name_order(self):
        self.write_channel(MAILBOX.replace("ch.windows", "b.windows").replace(
            "local  = ~/vcharon_mailbox", "local  = ~/b"), "b.windows.ini")
        self.write_channel(MAILBOX.replace("ch.windows", "a.windows"), "a.windows.ini")
        cfg = self.load(VCHARON)
        self.assertEqual(list(cfg.jobs), ["a.windows.up", "a.windows.down", "b.windows.up",
                                          "b.windows.down"])
        self.assertEqual(cfg.skipped, [])

    def test_only_one_channel_section(self):
        rows = (("[vcharon]\ncompress = yes\n" + MAILBOX,
                 "channels.d/ch.windows.ini: [vcharon] goes in vcharon.ini, not in channels.d"),
                (MAILBOX + JOB,
                 "channels.d/ch.windows.ini: holds exactly one section, [ch.windows], named as "
                 "the file"),
                (MAILBOX.replace("[ch.windows]", "[ch.mac]"),
                 "channels.d/ch.windows.ini: holds exactly one section, [ch.windows], named as "
                 "the file"),
                ("", "channels.d/ch.windows.ini: holds exactly one section, [ch.windows], "
                 "named as the file"),
                (JOB.replace("[j]", "[ch.windows]"),
                 "channels.d/ch.windows.ini [ch.windows]: holds only a channel section, with "
                 "mailbox keys"),
                # the retired fixed mailbox's section: no leader, and not <channel>.<me>
                ("[mailbox]\nssh = devbox\nmailbox.me = windows\nmailbox.local = ~/m\n"
                 "mailbox.remote = m\n",
                 "channels.d/ch.windows.ini: holds exactly one section, [ch.windows], named as "
                 "the file"),
                (MAILBOX.replace("mailbox.leader = laptop-ui\n", ""),
                 "%s mailbox.leader: required" % WHERE),
                ("\xff", "channels.d/ch.windows.ini isn't valid UTF-8 (byte 0)"),
                (MAILBOX + "[ch.windows]\n", "channels.d/ch.windows.ini line 8: section "
                 "[ch.windows] appears twice"))
        for text, message in rows:
            with self.subTest(message=message):
                raw = b"\xff" if text == "\xff" else text
                e = self.channel_refused(raw, VCHARON)
                self.assertEqual(e.message, message)
                self.assertTrue(e.hint.startswith("fix %s" % os.path.join(
                    self.tmp, "channels.d", "ch.windows.ini")), e.hint)
                # the config itself still loads
                self.assertEqual(list(config.load().jobs), [])

    def test_names_not_read(self):
        # temp files and dot files are never sections; X.INI isn't .ini (case counts, as
        # os.listdir gives it, not a glob)
        for file in (".ch.windows.ini.tmp", ".ch.windows.ini", "ch.windows.INI", "notes.txt",
                     "ch.windows.ini.bak"):
            self.write_channel("broken [", file)
        os.mkdir(os.path.join(self.tmp, "channels.d", "sub.ini.d"))
        cfg = self.load(VCHARON)
        self.assertEqual(list(cfg.jobs), [])
        self.assertEqual(cfg.skipped, [])

    def test_a_broken_file_is_skipped_for_the_others(self):
        self.write_channel("[a.windows\n", "a.windows.ini")
        self.write_channel(MAILBOX.replace("ch.windows", "b.windows"), "b.windows.ini")
        cfg = self.load(VCHARON)
        self.assertEqual(list(cfg.jobs), ["b.windows.up", "b.windows.down"])
        self.assertEqual([s.line for s in cfg.skipped], [
            "skipped channels.d/a.windows.ini: line 1: a key outside any section: [a.windows"])
        self.assertEqual(cfg.skipped[0].names, ("a.windows", "a.windows.up", "a.windows.down"))

    def test_next_to_the_config_file(self):
        # VCHARON_HOME's: the config file's folder
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(os.path.join(other, "channels.d"))
        with open(os.path.join(other, "vcharon.ini"), "w", encoding="utf-8") as f:
            f.write(VCHARON)
        with open(os.path.join(other, "channels.d", "ch.windows.ini"), "w",
                  encoding="utf-8") as f:
            f.write(MAILBOX)
        self.write_channel(MAILBOX.replace("ch.windows", "home.windows"), "home.windows.ini")
        with mock.patch.dict(os.environ, {"VCHARON_HOME": other}):
            cfg = config.load()
        self.assertEqual(list(cfg.jobs), ["ch.windows.up", "ch.windows.down"])
        self.assertEqual(config.channels_dir(cfg.path), os.path.join(other, "channels.d"))

    def test_two_sections_with_one_local_tree(self):
        # one tree per (channel, member): both skipped, each naming the other
        self.write_channel(MAILBOX, "ch.windows.ini")
        self.write_channel(MAILBOX.replace("ch.windows", "zz.windows"), "zz.windows.ini")
        # another spelling of the same folder
        self.write_channel(MAILBOX.replace("ch.windows", "x.windows").replace(
            "local  = ~/vcharon_mailbox", "local  = ~/./vcharon_mailbox/"), "x.windows.ini")
        self.write_channel(MAILBOX.replace("ch.windows", "y.windows").replace(
            "local  = ~/vcharon_mailbox", "local  = ~/own"), "y.windows.ini")
        cfg = self.load(VCHARON)
        self.assertEqual(list(cfg.jobs), ["y.windows.up", "y.windows.down"])
        lines = sorted(skip.line for skip in cfg.skipped)
        self.assertEqual(lines, sorted([
            "skipped channels.d/ch.windows.ini: [ch.windows]: its mailbox.local is the local "
            "tree of [x.windows] (channels.d/x.windows.ini) too; each section needs its own",
            "skipped channels.d/x.windows.ini: [x.windows]: its mailbox.local is the local "
            "tree of [ch.windows] (channels.d/ch.windows.ini) too; each section needs its own",
            "skipped channels.d/zz.windows.ini: [zz.windows]: its mailbox.local is the local "
            "tree of [ch.windows] (channels.d/ch.windows.ini) too; each section needs its own"]))
        self.assertIsNotNone(cfg.skipped_for("ch.windows.up"))

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "needs POSIX modes, not root")
    def test_a_channels_dir_that_cant_be_listed(self):
        self.write_channel(MAILBOX)
        folder = os.path.join(self.tmp, "channels.d")
        os.chmod(folder, 0)
        self.addCleanup(os.chmod, folder, 0o755)
        cfg = self.load(VCHARON)
        self.assertEqual(list(cfg.jobs), [])
        # a name the config lacks says why, not "no job named"
        skip = cfg.skipped_for("ch.windows.up")
        self.assertTrue(skip.error.message.startswith("channels.d: can't be listed: "),
                        skip.error.message)
        self.assertIsNone(cfg.named("ch.windows.up"))

    def test_without_a_vcharon_ini(self):
        # a box whose config is only channels.d/ still reads it
        self.write_channel(MAILBOX)
        cfg = config.load()
        self.assertFalse(cfg.exists)
        self.assertEqual(list(cfg.jobs), ["ch.windows.up", "ch.windows.down"])

    def test_a_retired_mailbox_in_vcharon_ini(self):
        fixed = ("[mailbox]\nssh = devbox\nmailbox.me = windows\nmailbox.local = ~/m\n"
                 "mailbox.remote = m\n")
        cfg = self.load(VCHARON + fixed)
        self.assertEqual(list(cfg.jobs), [])
        self.assertEqual(cfg.mailboxes, {})
        message = ("[mailbox] is a mailbox section, which vcharon.ini doesn't hold: delete it "
                   "(channel sections live in channels.d/ next to it)")
        self.assertEqual([s.line for s in cfg.skipped],
                         ["skipped vcharon.ini [mailbox]: " + message])
        for name in ("mailbox", "mailbox.up", "mailbox.down"):
            self.assertEqual(cfg.skipped_for(name).error.message, message)
                # broken as well: still only skipped, whatever it holds
        cfg = self.load(VCHARON + "[mailbox]\nmailbox.bogus = 1\n")
        self.assertEqual(list(cfg.jobs), [])
        # its names stay taken in vcharon.ini
        self.refused(fixed + fixed.replace("[mailbox]", "[Mailbox.Up]"), "same name")

    def test_box(self):
        for box in ("mac", "win", "linux", "laptop", "x" * 10, "a_b-c"):
            with self.subTest(box=box):
                self.assertEqual(self.load("[vcharon]\nbox = %s\n" % box).box, box)
        self.assertIsNone(self.load("[vcharon]\ncompress = yes\n").box)
        for box, words in (("x" * 11, "longer than 10"), ("Mac", "lowercase"), ("con",
                           "reserved"), ("", "lowercase"), ("a.b", "lowercase")):
            with self.subTest(box=box):
                e = self.refused("[vcharon]\nbox = %s\n" % box, words)
                self.assertTrue(e.message.startswith("vcharon.ini [vcharon] box: "), e.message)
                self.assertIn("such as mac, win, linux or laptop", e.message)


class TextWithBoxTest(unittest.TestCase):
    """vcharon setup --box's edit: one line changed or added, the rest byte for byte."""

    def test_edits(self):
        comment = config.BOX_COMMENT % "linux"
        for before, after in (
                ("", "[vcharon]\nbox = mac\n"),
                ("# mine\n[other]\nx = 1\n", "[vcharon]\nbox = mac\n\n# mine\n[other]\nx = 1\n"),
                ("[vcharon]\ncompress = yes\n", "[vcharon]\nbox = mac\ncompress = yes\n"),
                ("[vcharon]", "[vcharon]\nbox = mac\n"),
                ("; top\n[vcharon]\n# keep\nBOX : old\n[x]\nbox = other\n",
                 "; top\n[vcharon]\n# keep\nbox = mac\n[x]\nbox = other\n"),
                ("[vcharon]\n%s\nssh_path = /x\n" % comment,
                 "[vcharon]\nbox = mac\nssh_path = /x\n"),
                ("[vcharon]\r\n# c\r\nbox = a\r\n", "[vcharon]\r\n# c\r\nbox = mac\r\n"),
                ("[x]\nbox = 1\n[vcharon]\n", "[x]\nbox = 1\n[vcharon]\nbox = mac\n"),
                ("[vcharon]\nboxes = 1\n", "[vcharon]\nbox = mac\nboxes = 1\n")):
            with self.subTest(before=before):
                self.assertEqual(config.text_with_box(before, "mac"), after)


# a retired fixed mailbox's section, with mailbox keys: the only section besides [vcharon] that
# vcharon.ini still takes (skipped, its names taken)
RETIRED = "[%s]\nssh = devbox\nmailbox.me = windows\nmailbox.local = ~/m\nmailbox.remote = m\n"


class VCharonIniSectionsTest(MailboxCase):
    """The checks of vcharon.ini's own sections, besides [vcharon]."""

    def test_settings_only(self):
        # a stray section of any name: the settings-only message, not a name rule's
        for name in ("My Jobs", "j", "con"):
            with self.subTest(name=name):
                e = self.refused(VCHARON + "[%s]\nx = 1\n" % name)
                self.assertEqual(e.message, "vcharon.ini [%s]: vcharon.ini holds only [vcharon]; a "
                                            "channel's section goes in channels.d, which vcharon "
                                            "join writes" % name)
                self.assertEqual(e.hint, "fix %s" % self.path)

    def test_the_global_section_spelling(self):
        for text in ("[VCharon]\nidle_timeout = 60\n", RETIRED % "VCHARON"):
            with self.subTest(text=text):
                e = self.refused(text)
                section = text.split("]")[0][1:]
                self.assertEqual(e.message, "vcharon.ini [%s]: the global section is spelled "
                                            "[vcharon]" % section)

    def test_section_names(self):
        for name in ("1a", "a.b_c-d", "x" * 64, "Game"):
            with self.subTest(name=name):
                cfg = self.load(RETIRED % name)
                self.assertEqual([s.where for s in cfg.skipped], ["vcharon.ini [%s]" % name])
        for name in (".a", "-a", "a b", "ä", "x" * 65, "CON", "nul.txt", "com1", "a/b", "_a"):
            with self.subTest(name=name):
                e = self.refused(RETIRED % name, "vcharon.ini [%s]: " % name)
                self.assertEqual(e.hint, "fix %s" % self.path)
        e = self.refused(RETIRED % "CON")
        self.assertEqual(e.message, "vcharon.ini [CON]: CON is a reserved name on Windows")
        e = self.refused(RETIRED % "a b")
        self.assertEqual(e.message, "vcharon.ini [a b]: a job name has only letters, digits, '.', "
                                    "'_' and '-', starts with a letter or digit, and is at most "
                                    "64 characters long")

    def test_case_folded_clashes(self):
        e = self.refused(RETIRED % "game" + RETIRED % "Game")
        self.assertEqual(e.message, "vcharon.ini [Game]: the same name as [game] when case is "
                                    "ignored")
        # a section named as another one's job
        e = self.refused(RETIRED % "game" + RETIRED % "Game.Up")
        self.assertEqual(e.message, "vcharon.ini [Game.Up]: the same name as the job game.up of "
                                    "[game] when case is ignored")
        # a section whose job is named as an earlier section
        e = self.refused(RETIRED % "game.down" + RETIRED % "Game")
        self.assertEqual(e.message, "vcharon.ini [Game]: its job Game.down has the same name as "
                                    "[game.down] when case is ignored")

    def test_a_clash_with_a_channels_d_file(self):
        # vcharon.ini's names are taken first: the channels.d/ file is skipped too
        for name in ("ch.windows.up", "CH.WINDOWS.DOWN", "Ch.Windows", "ch.windows"):
            with self.subTest(name=name):
                cfg = self.load_channel(MAILBOX, RETIRED % name)
                self.assertEqual(list(cfg.jobs), [])
                self.assertEqual([s.where for s in cfg.skipped],
                                 ["vcharon.ini [%s]" % name, "channels.d/ch.windows.ini"])
                e = cfg.skipped[1].error
                self.assertEqual(e.message, "%s: %s has the same name as [%s] when case is "
                                            "ignored" % (WHERE, name.lower(), name))
                self.assertEqual(e.code, "config")


if __name__ == "__main__":
    unittest.main()
