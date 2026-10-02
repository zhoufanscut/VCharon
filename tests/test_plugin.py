"""Plugin options, the registry and the Ctx (DESIGN §9.1)."""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from unittest import mock

from vcharon import platform, plugin, plugins
from vcharon.plugin import Option, choice
from vcharon.proto import VCharonError


def probe_module(name="probe", caps=(), ends=("local", "remote"), kinds=("source", "sink")):
    """A test-only plugin module with one option of each kind."""
    module = types.ModuleType("vcharon.plugins." + name)
    module.NAME = name
    module.KINDS = kinds
    module.ENDS = ends
    module.NEEDS = ()
    module.CAPS = caps
    module.OPTIONS = {"path": Option(str, required=True), "note": Option(str, default=""),
                      "flag": Option(bool, default=False), "count": Option(int, default=7),
                      "items": Option(list, default=["a"]),
                      "mode": Option(choice("error", "skip"), default="error")}

    class Source(plugin.Source):
        pass

    class Sink(plugin.StagerSink):
        pass

    module.Source, module.Sink = Source, Sink
    return module


class PluginCase(unittest.TestCase):
    def install(self, module):
        """Puts a test-only module into plugins.ALL and sys.modules for this test."""
        for patcher in (mock.patch.object(plugins, "ALL", plugins.ALL + (module.NAME,)),
                        mock.patch.dict(sys.modules, {module.__name__: module})):
            patcher.start()
            self.addCleanup(patcher.stop)
        return module

    def refused(self, code, fn, *args):
        with self.assertRaises(VCharonError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        return cm.exception


class OptionsTest(PluginCase):
    def setUp(self):
        self.module = self.install(probe_module())

    def parse(self, raw, role="source"):
        return plugin.parse_options(self.module, role, raw)

    def test_each_kind(self):
        got = self.parse({"path": "/x", "note": "", "flag": "YES", "count": "0042",
                          "items": " a, ,b ,, c d ", "mode": "skip"})
        self.assertEqual(got, {"path": "/x", "note": "", "flag": True, "count": 42,
                               "items": ["a", "b", "c d"], "mode": "skip"})
        for text, value in (("yes", True), ("True", True), ("1", True), ("no", False),
                            ("FALSE", False), ("0", False)):
            with self.subTest(text=text):
                self.assertIs(self.parse({"path": "p", "flag": text})["flag"], value)
        self.assertEqual(self.parse({"path": "p", "items": ""})["items"], [])

    def test_defaults(self):
        got = self.parse({"path": "p"})
        self.assertEqual(got, {"path": "p", "note": "", "flag": False, "count": 7,
                               "items": ["a"], "mode": "error"})
        # a list default is copied, never shared
        got["items"].append("b")
        self.assertEqual(self.parse({"path": "p"})["items"], ["a"])
        self.assertEqual(self.module.OPTIONS["items"].default, ["a"])

    def test_refusals(self):
        cases = [({"path": ""}, "from.path: can't be empty"),
                 ({"path": "p", "flag": "maybe"}, "from.flag: must be yes or no"),
                 ({"path": "p", "count": "-1"}, "from.count: must be a whole number, 0 or more"),
                 ({"path": "p", "count": "1.5"}, "from.count: must be a whole number, 0 or more"),
                 ({"path": "p", "count": ""}, "from.count: must be a whole number, 0 or more"),
                 ({"path": "p", "count": "²"}, "from.count: must be a whole number, 0 or more"),
                 ({"path": "p", "mode": "Skip"}, "from.mode: must be one of error, skip"),
                 ({"note": "n"}, "from.path: required")]
        for raw, message in cases:
            with self.subTest(raw=raw):
                e = self.refused("bad_options", self.parse, raw)
                self.assertEqual(e.message, message)
                self.assertEqual(e.hint, "fix %s" % message.split(":")[0])

    def test_huge_whole_number(self):
        # int() refuses more than 4300 digits on Python 3.11 and later
        count = self.parse({"path": "p", "count": "9" * 100})["count"]
        self.assertEqual(count, int("9" * 100))
        try:
            int("9" * 5000)
        except ValueError:
            e = self.refused("bad_options", self.parse, {"path": "p", "count": "9" * 5000})
            self.assertEqual(e.message, "from.count: must be a whole number, 0 or more")

    def test_unknown_option(self):
        e = self.refused("bad_options", self.parse, {"path": "p", "pth": "x"}, "sink")
        self.assertEqual(e.message, "to.pth: unknown option")
        self.assertEqual(e.hint, "the probe plugin's options are: path, note, flag, count, "
                                 "items, mode")

    def test_raw_must_be_strings(self):
        for raw in (None, [], {"path": 1}, {1: "x"}, {"path": None}, {"path": ["p"]}):
            with self.subTest(raw=raw):
                self.refused("protocol", self.parse, raw)

    def test_check_options_runs(self):
        seen = []

        def check_options(options):
            seen.append(dict(options))
            if options["count"] > 10:
                raise VCharonError("bad_options", "from.count: too big", "fix from.count")

        self.module.check_options = check_options
        self.parse({"path": "p", "count": "3"})
        self.assertEqual(seen[0]["count"], 3)
        self.refused("bad_options", self.parse, {"path": "p", "count": "11"})


class RegistryTest(PluginCase):
    def test_every_plugin(self):
        self.assertEqual(plugins.ALL, ("path", "dir"))
        for name in plugins.ALL:
            with self.subTest(name=name):
                module = plugin.load(name)
                # a module name can't hold "-"
                self.assertIs(module, importlib.import_module(
                    "vcharon.plugins." + name.replace("-", "_")))
                self.assertEqual(module.NAME, name)
                for attr in ("KINDS", "ENDS", "NEEDS", "CAPS"):
                    self.assertIsInstance(getattr(module, attr), tuple, attr)
                self.assertTrue(set(module.KINDS) <= {"source", "sink"})
                self.assertTrue(set(module.ENDS) <= {"local", "remote"})
                self.assertIsInstance(module.OPTIONS, dict)
                for option in module.OPTIONS.values():
                    self.assertIsInstance(option, Option)
                for kind in module.KINDS:
                    self.assertTrue(hasattr(module, kind.capitalize()), kind)

    def test_unknown_plugin(self):
        e = self.refused("config", plugin.load, "rsync")
        self.assertEqual(e.message, "unknown plugin rsync")
        self.assertEqual(e.hint, "the plugins are: path, dir")
        # only what ALL lists, even if a module by that name exists
        self.refused("config", plugin.load, "__init__")
        unlisted = probe_module("unlisted")
        with mock.patch.dict(sys.modules, {unlisted.__name__: unlisted}):
            e = self.refused("config", plugin.load, "unlisted")
        self.assertEqual(e.message, "unknown plugin unlisted")

    def test_name_must_match(self):
        module = self.install(probe_module())
        module.NAME = "other"
        self.refused("internal", plugin.load, "probe")

    def test_wrong_role_and_end(self):
        e = self.refused("config", plugin.check_side, "local", "path", "sink", {"path": "/x"})
        self.assertEqual(e.message, "path can't be a sink")
        e = self.refused("config", plugin.check_side, "local", "dir", "source", {"path": "/x"})
        self.assertEqual(e.message, "dir can't be a source")
        self.install(probe_module("localonly", ends=("local",)))
        e = self.refused("config", plugin.check_side, "remote", "localonly", "source",
                         {"path": "p"})
        self.assertEqual(e.message, "localonly can't run on the remote end")
        self.refused("config", plugin.check_side, "moon", "path", "source", {"path": "/x"})

    def test_missing_capability(self):
        self.install(probe_module("desk", caps=("desktop",)))
        with mock.patch.object(platform, "caps", return_value={"desktop": False}):
            e = self.refused("missing_capability", plugin.check_side, "local", "desk",
                             "source", {"path": "p"})
            self.assertEqual(e.message, "desk needs desktop, which this client lacks")
            self.assertEqual(e.exit_code, 3)
            # capabilities are the client's: the remote end never checks them
            self.assertEqual(plugin.check_side("remote", "desk", "source", {"path": "p"})["path"],
                             "p")
        with mock.patch.object(platform, "caps", return_value={"desktop": True}):
            self.assertEqual(plugin.check_side("local", "desk", "source", {"path": "p"})["path"],
                             "p")

    def test_make(self):
        ctx = plugin.Ctx("remote", home="/h")
        source = plugin.make("remote", "path", "source", {"path": "x"}, ctx)
        self.assertEqual(type(source).__module__, "vcharon.plugins.path")
        self.assertIs(source.ctx, ctx)
        self.assertEqual(source.options, {"path": "x", "keep_name": False, "exclude": [],
                                          "symlinks": "error", "prune": False,
                                          "allow_empty": False, "mailbox_me": None})
        sink = plugin.make("remote", "dir", "sink", {"path": "x", "create": "yes"}, ctx)
        self.assertEqual(sink.options, {"path": "x", "create": True, "max_deletes": 500})
        # make runs every check_side check
        self.refused("bad_options", plugin.make, "remote", "dir", "sink", {"pth": "x"}, ctx)
        self.refused("internal", plugin.make, "local", "dir", "sink", {"path": "/x"}, ctx)

    def test_stager_sink_before_check(self):
        sink = plugin.make("local", "dir", "sink", {"path": "/x"}, plugin.Ctx("local"))
        self.assertEqual(sink.done.written, [])
        # the engine may abort a sink whose source failed to plan
        sink.abort()
        sink.close()
        sink.close()
        self.refused("internal", sink.stage, 0, None)
        self.refused("internal", sink.commit)


class CtxTest(unittest.TestCase):
    def test_defaults(self):
        ctx = plugin.Ctx("local")
        self.assertEqual((ctx.end, ctx.os, ctx.home), ("local", platform.os_name(),
                                                        platform.home()))
        self.assertEqual(ctx.caps, platform.caps())
        self.assertIs(ctx.fs, plugin.fsops)
        ctx.log("nobody listens")
        ctx.tick()

    def test_log_never_raises(self):
        def failing(msg):
            raise OSError(5, "Input/output error")

        seen = []
        plugin.Ctx("remote", log=failing).log("x")
        plugin.Ctx("remote", log=seen.append).log("y")
        self.assertEqual(seen, ["y"])

    def test_tick_raises(self):
        def lost():
            raise VCharonError("lost", "the controller closed the connection")

        with self.assertRaises(VCharonError):
            plugin.Ctx("remote", tick=lost).tick()

    def test_resolve(self):
        cwd = os.getcwd()
        for end in ("local", "remote"):
            ctx = plugin.Ctx(end, home="/home/me", osn="linux")
            with self.subTest(end=end):
                self.assertEqual(ctx.resolve("~", "to.path"), "/home/me")
                self.assertEqual(ctx.resolve("~/x/y", "to.path"), "/home/me/x/y")
                self.assertEqual(ctx.resolve("~//x", "to.path"), "/home/me/x")
                # the trailing separator stays
                self.assertEqual(ctx.resolve("~/", "to.path"), "/home/me/")
                self.assertEqual(ctx.resolve("/srv/x", "to.path"), "/srv/x")
        remote = plugin.Ctx("remote", home="/home/me", osn="linux")
        # relative to the home, never the current directory
        self.assertEqual(remote.resolve("inbox/a", "to.path"), "/home/me/inbox/a")
        # "~x" is another user's home only when this end is this machine's (on a Linux client a
        # user named x could exist); on any other host the text stays a name under the home
        if platform.os_name() == "linux":
            want = (os.path.expanduser("~x") if os.path.isabs(os.path.expanduser("~x"))
                    else "/home/me/~x")
        else:
            want = "/home/me/~x"
        self.assertEqual(remote.resolve("~x", "to.path"), want)
        self.assertNotIn(cwd, remote.resolve("a", "to.path"))
        local = plugin.Ctx("local", home="/home/me", osn="linux")
        with self.assertRaises(VCharonError) as cm:
            local.resolve("inbox", "to.path")
        self.assertEqual(cm.exception.code, "bad_options")
        self.assertEqual(cm.exception.message, "to.path: must be an absolute path or start "
                                               "with ~")
        self.assertEqual(cm.exception.exit_code, 3)

    def test_resolve_other_users_home(self):
        user = platform.user()
        other = os.path.expanduser("~" + user)
        if not user or not os.path.isabs(other):
            self.skipTest("no home for ~%s here" % user)
        ctx = plugin.Ctx("local", home="/elsewhere", osn=platform.os_name())
        self.assertEqual(ctx.resolve("~%s/x" % user, "from.path"), os.path.join(other, "x"))

    def test_resolve_backslash(self):
        # ~\x is ~ and then x on a Windows end, whichever way the home reads, and the end's own
        # separator joins; on any other end \ is part of a name, so the text stays one name
        # under that end's home. Every OS runs this (DESIGN §15).
        windows = plugin.Ctx("remote", home="C:\\Users\\me", osn="windows")
        self.assertEqual(windows.resolve("~\\x", "to.path"), "C:\\Users\\me\\x")
        linux = plugin.Ctx("remote", home="/home/me", osn="linux")
        self.assertEqual(linux.resolve("~\\x", "to.path"), "/home/me/~\\x")

    def test_resolve_by_the_end_s_rules(self):
        # The end's join, isabs and ~user on any host (DESIGN §9.1, §15); each of these fails if
        # resolve used this host's instead.
        win = plugin.Ctx("remote", home="C:\\Users\\me", osn="windows")
        self.assertEqual(win.resolve("~/x/y", "to.path"), "C:\\Users\\me\\x\\y")
        self.assertEqual(win.resolve("~/", "to.path"), "C:\\Users\\me\\")
        self.assertEqual(win.resolve("inbox/a", "to.path"), "C:\\Users\\me\\inbox\\a")
        self.assertEqual(win.resolve("C:x", "to.path"), "C:\\Users\\me\\x")
        self.assertEqual(win.resolve("~root", "to.path"),
                         os.path.expanduser("~root") if platform.os_name() == "windows"
                         else "C:\\Users\\me\\~root")
        local = plugin.Ctx("local", home="C:\\Users\\me", osn="windows")
        self.assertEqual(local.resolve("C:\\data\\x", "to.path"), "C:\\data\\x")
        # a Windows path needs a drive or a share: these depend on the current directory, which
        # is never used (Python before 3.13 calls /srv/x and \x absolute)
        for bad in ("inbox", "/srv/x", "\\x", "C:x", "~/D:x"):
            with self.subTest(bad=bad), self.assertRaises(VCharonError):
                local.resolve(bad, "to.path")
        linux = plugin.Ctx("local", home="/home/me", osn="linux")
        self.assertEqual(linux.resolve("/srv/x", "to.path"), "/srv/x")
        self.assertEqual(linux.resolve("~/a\\b", "to.path"), "/home/me/a\\b")


class DoctorTest(PluginCase):
    """plugin.doctor and the checks' JSON (decision 21 of the M5 plan)."""

    def module(self, needs=(), checks=(), fail=None):
        module = probe_module("doc")
        module.NEEDS = needs
        closed = self.closed = []

        class Source(plugin.Source):
            def doctor(self):
                if fail is not None:
                    raise fail
                return list(checks)

            def close(self):
                closed.append(True)

        module.Source = Source
        return self.install(module)

    def doctor(self, raw=None, end="local"):
        return plugin.doctor(end, "doc", "source", {"path": "p"} if raw is None else raw,
                             plugin.Ctx(end, home="/h"))

    def test_needs(self):
        self.module(needs=("ferry-test-no-such-command",))
        self.assertEqual(self.doctor(), [("FAIL", "ferry-test-no-such-command isn't on this "
                                          "end's PATH", "install ferry-test-no-such-command on "
                                          "the local end")])
        self.assertEqual(self.doctor(end="remote")[0][2],
                         "install ferry-test-no-such-command on the remote end")

    @unittest.skipUnless(os.name == "posix", "sh")
    def test_needs_found(self):
        self.module(needs=("sh",), checks=[("warn", "own", "do this")])
        checks = self.doctor()
        self.assertEqual(checks[0][0], "ok")
        self.assertRegex(checks[0][1], r"\Ash: /.*sh\Z")
        self.assertIsNone(checks[0][2])
        # then the plugin's own checks
        self.assertEqual(checks[1:], [("warn", "own", "do this")])
        self.assertEqual(self.closed, [True])

    def test_bad_options_raise(self):
        self.module()
        self.refused("bad_options", self.doctor, {"path": "p", "nope": "x"})
        self.refused("bad_options", self.doctor, {})
        self.assertEqual(self.closed, [])

    def test_close_after_a_failing_doctor(self):
        self.module(fail=VCharonError("io", "boom"))
        self.refused("io", self.doctor)
        self.assertEqual(self.closed, [True])

    def test_default_doctor_is_empty(self):
        self.install(probe_module("plain"))
        self.assertEqual(plugin.doctor("local", "plain", "sink", {"path": "/x"},
                                       plugin.Ctx("local")), [])

    def test_json_round_trip(self):
        checks = [("ok", "a", None), ("warn", "b", "fix b"), ("FAIL", "c", "fix c")]
        obj = plugin.checks_to_json(checks)
        self.assertEqual(obj, {"checks": [["ok", "a", None], ["warn", "b", "fix b"],
                                          ["FAIL", "c", "fix c"]]})
        self.assertEqual(plugin.checks_from_json(obj), checks)
        self.assertEqual(plugin.checks_from_json({"checks": []}), [])

    def test_json_refused(self):
        for obj in ({"checks": [["OK", "a", None]]}, {"checks": [["ok", "a"]]},
                    {"checks": [["ok", 1, None]]}, {"checks": [["ok", "a", 2]]},
                    {"checks": [], "more": 1}, {"checks": "x"}, {}, [], None,
                    {"checks": [("ok", "a", None)]}):
            with self.subTest(obj=obj):
                self.refused("protocol", plugin.checks_from_json, obj)


if __name__ == "__main__":
    unittest.main()
