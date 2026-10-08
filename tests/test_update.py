"""vcharon --update: version maths, the install kind, the release, the swap and the flag
(DESIGN, "Self-update").

Nothing here touches the network: update._open is the module's one network call, and every
test that needs a reply replaces it (Net, which fails on a URL no test declared); setUp also
puts a stand-in under it, so an _open a test forgot can't reach GitHub. The tests of _open
itself replace update._opener.

The swap tests build real archives whose binary is a small Python script that answers
--version, run with this Python (smoke_argv), so they run on every OS. They check what matters
for a program that replaces itself: after a success the target is the new binary; after any
failure it is byte for byte the old one, and no temp file is left.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest import mock

import vcharon
from vcharon import charter, cli, fsops, install, platform, skill, update
from vcharon.mailbox import watch

from tests import pack
from tests.util import FakeSshCase

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET = "vcharon-linux-x64"
TARBALL = ASSET + ".tar.gz"
DOWNLOAD = "https://github.com/%s/releases/download/v9.9.9/%s" % (update.REPO, TARBALL)
SUMS = DOWNLOAD + ".sha256"
WIN_ASSET = "vcharon-win-x64"
ZIP = WIN_ASSET + ".zip"
ZIP_URL = "https://github.com/%s/releases/download/v9.9.9/%s" % (update.REPO, ZIP)
ZIP_SUMS = ZIP_URL + ".sha256"
OLD = b"old binary\n"
# the real one: UpdateFlagTest replaces it, so the suite runs the same on a pre-release build
IS_PRERELEASE = update.is_prerelease


def release_json(tag="v9.9.9", with_checksum=True, assets=True, prerelease=False, draft=False):
    """The part of GitHub's releases/latest reply that latest_release reads; one entry of
    the releases list too."""
    files = []
    if assets:
        files.append({"name": TARBALL, "browser_download_url": DOWNLOAD})
        if with_checksum:
            files.append({"name": TARBALL + ".sha256", "browser_download_url": SUMS})
    return {"tag_name": tag, "html_url": "https://github.com/%s/releases/tag/%s"
            % (update.REPO, tag), "published_at": "2026-07-31T00:00:00Z", "assets": files,
            "prerelease": prerelease, "draft": draft}


def fake_binary(version="9.9.9", prints=None, exit_code=0):
    """A stand-in binary: a Python script that prints a version and exits."""
    reported = version if prints is None else prints
    return ("import sys\nsys.stdout.write(%r + '\\n')\nsys.exit(%d)\n"
            % (reported, exit_code)).encode("utf-8")


def make_tarball(path, binary=None, member="vcharon", extra=None):
    """A release-shaped .tar.gz: its member (the binary, by default named vcharon) and
    LICENSE; extra: another member's name (a path that climbs out, say)."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in ((member, binary or fake_binary()), ("LICENSE", b"MIT\n")) + (
                ((extra, b"evil\n"),) if extra else ()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return path


def make_zip(path, binary=None, name="vcharon.exe", extra=()):
    """A release-shaped .zip: name (the binary) and LICENSE, then (name, data) of extra."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, binary or fake_binary())
        zf.writestr("LICENSE", b"MIT\n")
        for one, data in extra:
            zf.writestr(one, data)
    return path


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read(path):
    with open(path, "rb") as f:
        return f.read()


class Net:
    """update._open's stand-in: routes are URL -> bytes, str or an exception to raise; any
    other URL fails the test."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def open(self, url, timeout):
        self.urls.append(url)
        if url not in self.routes:
            raise AssertionError("a test fetched an undeclared URL: %s" % url)
        reply = self.routes[url]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            reply = reply.encode("utf-8")
        return io.BytesIO(reply)


class NoNetwork:
    @staticmethod
    def open(request, timeout):
        raise AssertionError("a test reached the network: %s" % request.full_url)


def never_called(*args, **kwargs):
    raise AssertionError("apply_update must not run here")


class UpdateCase(unittest.TestCase):
    """A temp folder with an installed stand-in binary, the platform fixed to linux-x64, the
    smoke check run with this Python, and no way to the network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.target = os.path.join(self.bin, "vcharon")
        with open(self.target, "wb") as f:
            f.write(OLD)
        os.chmod(self.target, 0o755)
        self.inst = install.Install("binary", self.target)
        self.patch(update, "platform_asset", return_value=ASSET)
        self.patch(update, "smoke_argv", new=lambda binary: [sys.executable, binary, "--version"])
        self.patch(update, "_opener", new=NoNetwork)

    def patch(self, obj, name, **kw):
        patcher = mock.patch.object(obj, name, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def serve(self, net):
        self.patch(update, "_open", new=net.open)
        return net

    def serving(self, **kw):
        """(Net, Release) serving a fresh tarball and its real checksum."""
        tarball = make_tarball(os.path.join(self.tmp, TARBALL), **kw)
        net = Net({DOWNLOAD: read(tarball), SUMS: "%s  %s\n" % (sha256(tarball), TARBALL)})
        release = update.Release("v9.9.9", "9.9.9", "https://example.invalid/r", "",
                                 {TARBALL: DOWNLOAD, TARBALL + ".sha256": SUMS})
        return net, release

    def listing(self):
        return sorted(os.listdir(self.bin))

    def assert_untouched(self):
        self.assertEqual(read(self.target), OLD)
        self.assertEqual(self.listing(), ["vcharon"])


# --- versions and platforms ---

class VersionTest(unittest.TestCase):
    def test_parse(self):
        final = (1,)
        for text, want in (("v0.3.0", ((0, 3, 0), final)), ("0.3.0", ((0, 3, 0), final)),
                           ("V1.2.3", ((1, 2, 3), final)), ("0.4", ((0, 4), final)),
                           ("v0.4.0-rc1", ((0, 4, 0), (0, 3, 1))),
                           ("0.4.0rc1", ((0, 4, 0), (0, 3, 1))),
                           ("0.4.0.rc.2", ((0, 4, 0), (0, 3, 2))),
                           ("1.0.0b3", ((1, 0, 0), (0, 2, 3))),
                           ("1.0.0+build7", ((1, 0, 0), final)),
                           ("0.1.0.post1", ((0, 1, 0), (2, 1))),
                           ("0.1.0post2", ((0, 1, 0), (2, 2))),
                           ("nightly", None), ("", None), (None, None)):
            with self.subTest(text=text):
                self.assertEqual(update.parse_version(text), want)

    def test_release_candidates_come_before_their_final(self):
        # tagged in this order: each is an update over the one before
        order = ["0.0.9", "0.1.0dev1", "0.1.0a1", "0.1.0b1", "0.1.0rc1", "0.1.0rc2", "v0.1.0",
                 "0.1.0.post1", "0.1.0.post2", "0.1.1rc1", "0.1.1"]
        for i, older in enumerate(order):
            for newer in order[i + 1:]:
                with self.subTest(older=older, newer=newer):
                    self.assertTrue(update.is_newer(newer, older))
                    self.assertFalse(update.is_newer(older, newer))
            self.assertEqual(update.compare(older, older), 0)
        self.assertEqual(update.compare("0.1", "0.1.0"), 0)
        self.assertEqual(update.compare("0.1.0rc1", "0.1rc1"), 0)
        self.assertIsNone(update.compare("nightly", "0.1.0"))
        # short against long: 0.4 is 0.4.0, and 0.4.1 is newer; a tag that doesn't parse
        # never claims an update
        for candidate, current, want in (("v0.4", "0.4.0", False), ("v0.4.1", "0.4", True),
                                         ("nightly", "0.3.0", False)):
            with self.subTest(candidate=candidate, current=current):
                self.assertIs(update.is_newer(candidate, current), want)

    def test_which_builds_are_pre_releases(self):
        # what makes a plain --update count pre-releases: a post-release is a final's
        for text, want in (("0.5.0rc1", True), ("v0.5.0b2", True), ("0.5.0.dev1", True),
                           ("0.5.0", False), ("0.5.0.post1", False), ("nightly", False)):
            with self.subTest(text=text):
                self.assertIs(update.is_prerelease(text), want)


class PlatformAssetTest(unittest.TestCase):
    def test_matrix(self):
        for system, machine, want in (
                ("Linux", "x86_64", "vcharon-linux-x64"), ("Linux", "amd64", "vcharon-linux-x64"),
                ("Darwin", "arm64", "vcharon-darwin-arm64"),
                ("Windows", "AMD64", "vcharon-win-x64"),
                # what the release doesn't build: pipx
                ("Darwin", "x86_64", None), ("Linux", "aarch64", None),
                ("Windows", "ARM64", None), ("FreeBSD", "amd64", None)):
            with self.subTest(system=system, machine=machine), \
                    mock.patch.object(update.pyplatform, "system", return_value=system), \
                    mock.patch.object(update.pyplatform, "machine", return_value=machine):
                self.assertEqual(update.platform_asset(), want)
                if want:
                    # Windows' archive is a zip, the others' a tarball
                    self.assertEqual(update.archive_name(want),
                                     want + (".zip" if system == "Windows" else ".tar.gz"))


# --- the install kind ---

class DetectTest(unittest.TestCase):
    def frozen(self, executable, meipass=True):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(sys, "frozen", True, create=True))
        if meipass:
            stack.enter_context(mock.patch.object(sys, "_MEIPASS", "/tmp/_MEI1", create=True))
        elif hasattr(sys, "_MEIPASS"):
            stack.enter_context(mock.patch.object(sys, "_MEIPASS", None))
            delattr(sys, "_MEIPASS")
        stack.enter_context(mock.patch.object(sys, "executable", executable))
        return stack

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "symlinks")
    def test_a_binary_resolves_links(self):
        with tempfile.TemporaryDirectory(prefix="vcharon-test-") as d:
            real = os.path.join(d, "vcharon")
            with open(real, "wb") as f:
                f.write(b"binary")
            link = os.path.join(d, "vcharon-link")
            os.symlink(real, link)
            with self.frozen(link):
                inst = install.detect()
        self.assertEqual(inst, install.Install("binary", os.path.realpath(real)))
        self.assertTrue(inst.self_updatable)

    def test_frozen_without_meipass_isnt_ours(self):
        # what --update does with a binary is replace sys.executable: never a real Python
        with self.frozen("/usr/bin/python3", meipass=False), \
                mock.patch.object(sys, "prefix", "/usr"), \
                mock.patch.object(install, "source_checkout", return_value=None):
            inst = install.detect()
        self.assertEqual(inst.kind, "pip")
        self.assertFalse(inst.self_updatable)

    def test_managed_installs(self):
        for prefix, kind in (
                (os.path.join(os.sep, "home", "u", ".local", "pipx", "venvs", "vcharon"), "pipx"),
                (os.path.join(os.sep, "home", "u", ".local", "share", "pipx", "venvs", "vcharon"),
                 "pipx"),
                (os.path.join(os.sep, "home", "u", ".local", "share", "uv", "tools", "vcharon"),
                 "uv"),
                # a uvx run, from uv's cache: no "tools" in its path
                (os.path.join(os.sep, "home", "u", ".cache", "uv", "archive-v0", "abc"), "uv"),
                (os.path.join(os.sep, "home", "u", "proj", ".venv"), "pip"),
                (os.path.join(os.sep, "usr"), "pip")):
            with self.subTest(prefix=prefix), \
                    mock.patch.object(platform, "is_frozen", return_value=False), \
                    mock.patch.object(sys, "prefix", prefix), \
                    mock.patch.object(install, "source_checkout", return_value=None):
                inst = install.detect()
                self.assertEqual((inst.kind, inst.self_updatable), (kind, False))

    def test_source_checkout(self):
        # before the environment's kind: a checkout installed editable into a pipx or uv
        # environment is updated with git
        for prefix in (os.path.join(os.sep, "home", "u", ".venv"),
                       os.path.join(os.sep, "home", "u", ".local", "pipx", "venvs", "vcharon"),
                       os.path.join(os.sep, "home", "u", ".local", "share", "uv", "tools", "v")):
            with self.subTest(prefix=prefix), \
                    mock.patch.object(platform, "is_frozen", return_value=False), \
                    mock.patch.object(sys, "prefix", prefix), \
                    mock.patch.object(install, "source_checkout",
                                      return_value="/home/u/VCharon"):
                self.assertEqual(install.detect(), install.Install("source", "/home/u/VCharon"))

    @unittest.skipUnless(os.path.isdir(os.path.join(REPO_ROOT, ".git"))
                         and os.path.isfile(os.path.join(REPO_ROOT, "src", "vcharon",
                                                         "install.py")),
                         "not run from a git checkout (an sdist, a git archive)")
    def test_this_suite_runs_from_a_checkout(self):
        root = install.source_checkout()
        self.assertIsNotNone(root)
        self.assertTrue(os.path.isfile(os.path.join(root, "src", "vcharon", "install.py")))

    def test_commands_pin_the_tag(self):
        for kind, start in (("pipx", "pipx install --force "),
                            ("uv", "uv tool install --force ")):
            with self.subTest(kind=kind):
                self.assertEqual(install.Install(kind).command_for("v9.9.9"),
                                 '%s"git+https://github.com/zhoufanscut/VCharon@v9.9.9"'
                                 % start)
        pip = install.Install("pip").command_for("v9.9.9")
        self.assertEqual(pip, '%s -m pip install --upgrade "%s@v9.9.9"'
                         % (platform.quote_command([sys.executable]), install.GIT_SPEC))
        self.assertEqual(install.Install("binary", "/x").command_for("v9.9.9"),
                         "vcharon --update")

    def test_source_command_checks_out_the_tag(self):
        with mock.patch.object(platform, "os_name", return_value="linux"):
            self.assertEqual(install.Install("source", "/tmp/my repo").command_for("v9.9.9"),
                             "git -C '/tmp/my repo' fetch --tags && git -C '/tmp/my repo' "
                             "checkout v9.9.9")
        with mock.patch.object(platform, "os_name", return_value="windows"):
            self.assertEqual(install.Install("source", "C:\\My Repo").command_for("v1"),
                             'git -C "C:/My Repo" fetch --tags && git -C "C:/My Repo" '
                             "checkout v1")


# --- GitHub ---

class LatestReleaseTest(UpdateCase):
    def test_parses_tag_and_assets(self):
        self.serve(Net({update.API_LATEST: json.dumps(release_json())}))
        release = update.latest_release()
        self.assertEqual((release.tag, release.version), ("v9.9.9", "9.9.9"))
        self.assertEqual(release.assets[TARBALL], DOWNLOAD)
        self.assertIn(TARBALL + ".sha256", release.assets)

    def test_one_leading_v_only(self):
        self.serve(Net({update.API_LATEST: json.dumps(release_json(tag="vv1.0"))}))
        self.assertEqual(update.latest_release().version, "v1.0")

    def test_without_pre_only_releases_latest(self):
        net = self.serve(Net({update.API_LATEST: json.dumps(release_json())}))
        release = update.latest_release(pre=False)
        self.assertEqual((release.tag, release.prerelease), ("v9.9.9", False))
        self.assertEqual(net.urls, [update.API_LATEST])

    def newest(self, *entries):
        net = self.serve(Net({update.API_RELEASES: json.dumps(list(entries))}))
        release = update.latest_release(pre=True)
        self.assertEqual(net.urls, [update.API_RELEASES])
        return release

    def test_with_pre_the_highest_version_not_the_first(self):
        # GitHub doesn't document the order: a fix tagged after an rc can come first
        release = self.newest(release_json("v0.4.2"), release_json("v0.5.0rc1", prerelease=True),
                              release_json("v0.4.1"))
        self.assertEqual((release.tag, release.version, release.prerelease),
                         ("v0.5.0rc1", "0.5.0rc1", True))
        self.assertEqual(release.assets[TARBALL], DOWNLOAD)
        release = self.newest(release_json("v0.5.0rc2", prerelease=True), release_json("v0.5.0"),
                              release_json("v0.5.0rc1", prerelease=True))
        self.assertEqual((release.tag, release.prerelease), ("v0.5.0", False))

    def test_with_pre_drafts_and_bad_tags_are_skipped(self):
        bare = release_json("v0.1.0")
        del bare["tag_name"]
        release = self.newest(release_json("v9.0.0", draft=True), release_json("nightly"),
                              release_json(""), bare, "not an object", release_json("v0.3.0"),
                              release_json("v0.2.0rc1", prerelease=True))
        self.assertEqual(release.tag, "v0.3.0")

    def test_with_pre_nothing_usable(self):
        for entries in ([], [release_json("v9.0.0", draft=True)], [release_json("latest")]):
            with self.subTest(entries=entries), self.assertRaises(update.UpdateError) as caught:
                self.newest(*entries)
            self.assertEqual(caught.exception.kind, "no_release")

    def test_with_pre_the_list_must_be_a_list(self):
        self.serve(Net({update.API_RELEASES: json.dumps(release_json())}))
        with self.assertRaises(update.UpdateError) as caught:
            update.latest_release(pre=True)
        self.assertEqual(caught.exception.kind, "bad_response")

    def test_missing_tag(self):
        self.serve(Net({update.API_LATEST: json.dumps({"assets": []})}))
        with self.assertRaises(update.UpdateError) as caught:
            update.latest_release()
        self.assertEqual(caught.exception.kind, "no_release")

    def test_not_json(self):
        for body in ("<html>rate limited</html>", "[1, 2]"):
            with self.subTest(body=body):
                self.serve(Net({update.API_LATEST: body}))
                with self.assertRaises(update.UpdateError) as caught:
                    update.latest_release()
                self.assertEqual(caught.exception.kind, "bad_response")

    def test_http_errors(self):
        # no token set: a 401 has no token to blame, so it isn't retried
        self.tokens()
        for code, kind in ((403, "rate_limited"), (429, "rate_limited"), (404, "not_found"),
                           (401, "http_error"), (500, "http_error")):
            def refuse(request, timeout, code=code):
                raise urllib.error.HTTPError(update.API_LATEST, code, "no", {}, None)

            with self.subTest(code=code), \
                    mock.patch.object(update, "_opener", types.SimpleNamespace(open=refuse)):
                with self.assertRaises(update.UpdateError) as caught:
                    update.latest_release()
                self.assertEqual(caught.exception.kind, kind)
                if kind == "rate_limited":
                    self.assertEqual(caught.exception.fix, update.RATE_FIX)

    def tokens(self, **set_):
        """Only the token variables in set_, until the test ends."""
        patcher = mock.patch.dict(os.environ, set_)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in update.TOKEN_VARS:
            if var not in set_:
                os.environ.pop(var, None)

    def answers(self, *codes):
        """An opener whose calls answer codes in turn (None: a release), and the
        Authorization header each call carried."""
        sent = []
        replies = iter(codes)

        def answer(request, timeout):
            sent.append(request.get_header("Authorization"))
            code = next(replies)
            if code is None:
                return io.BytesIO(json.dumps(release_json()).encode("utf-8"))
            raise urllib.error.HTTPError(update.API_LATEST, code, "no", {}, None)

        self.patch(update, "_opener", new=types.SimpleNamespace(open=answer))
        return sent

    def test_a_rejected_token_is_dropped(self):
        # a stale token: the public release is read without it
        for var in update.TOKEN_VARS:
            with self.subTest(var=var):
                self.tokens(**{var: "ghp_old"})
                sent = self.answers(401, None)
                self.assertEqual(update.latest_release().tag, "v9.9.9")
                self.assertEqual(sent, ["Bearer ghp_old", None])

    def test_a_rejected_token_then_a_refusal(self):
        self.tokens(GH_TOKEN="ghp_old")
        for code, kind in ((403, "bad_token"), (429, "bad_token"), (401, "http_error"),
                           (404, "not_found"), (500, "http_error")):
            with self.subTest(code=code):
                sent = self.answers(401, code)
                with self.assertRaises(update.UpdateError) as caught:
                    update.latest_release()
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(sent, ["Bearer ghp_old", None])
                if kind == "bad_token":
                    self.assertEqual(caught.exception.fix, update.BAD_TOKEN_FIX % "GH_TOKEN")
                    self.assertIn("GH_TOKEN (HTTP 401)", str(caught.exception))

    def test_a_reply_cut_short(self):
        # IncompleteRead is an HTTPException, not an OSError
        class Torn(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b"{", 999)

        self.patch(update, "_open", new=lambda url, timeout: Torn(b""))
        with self.assertRaises(update.UpdateError) as caught:
            update.latest_release()
        self.assertEqual(caught.exception.kind, "network")

    def test_only_https(self):
        for url in ("file:///etc/passwd", "http://api.github.com/x", "ftp://h/x"):
            with self.subTest(url=url), self.assertRaises(update.UpdateError) as caught:
                update._open(url, 5)
            self.assertEqual(caught.exception.kind, "bad_url")

    def test_no_route(self):
        def fail(request, timeout):
            raise urllib.error.URLError("no route to host")

        self.patch(update, "_opener", new=types.SimpleNamespace(open=fail))
        with self.assertRaises(update.UpdateError) as caught:
            update.latest_release()
        self.assertEqual((caught.exception.kind, caught.exception.fix),
                         ("network", update.NETWORK_FIX))


class CABundleTest(unittest.TestCase):
    """A binary's OpenSSL looks for CAs at its build machine's path: when that file is
    missing, the system's bundle stands in; never when the default works or the user set
    SSL_CERT_FILE."""

    def setup(self, default_exists, env=None):
        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        loaded = []

        class Ctx:
            def load_verify_locations(self, cafile=None):
                loaded.append(cafile)

        default = os.path.join(tmp, "default.pem")
        if default_exists:
            with open(default, "w") as f:
                f.write("x")
        bundle = os.path.join(tmp, "system.pem")
        with open(bundle, "w") as f:
            f.write("x")
        environ = {k: v for k, v in os.environ.items()
                   if k not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        environ.update(env or {})
        for patcher in (mock.patch.object(update.ssl, "create_default_context", Ctx),
                        mock.patch.object(update.ssl, "get_default_verify_paths",
                                          lambda: types.SimpleNamespace(cafile=default)),
                        mock.patch.object(update, "_CA_BUNDLES",
                                          (os.path.join(tmp, "nope.pem"), bundle)),
                        mock.patch.dict(os.environ, environ, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return loaded, bundle

    def test_a_missing_default_falls_back(self):
        loaded, bundle = self.setup(default_exists=False)
        update._ssl_context()
        self.assertEqual(loaded, [bundle])

    def test_a_working_default_is_left_alone(self):
        loaded, _ = self.setup(default_exists=True)
        update._ssl_context()
        self.assertEqual(loaded, [])

    def test_the_users_variable_wins(self):
        loaded, _ = self.setup(default_exists=False, env={"SSL_CERT_FILE": "/x/custom.pem"})
        update._ssl_context()
        self.assertEqual(loaded, [])


class TokenTest(unittest.TestCase):
    """GITHUB_TOKEN is for api.github.com's rate limit only: never sent with a download, and
    dropped on a redirect to another host."""

    def capture(self):
        seen = {}

        class Opener:
            @staticmethod
            def open(request, timeout):
                seen["headers"] = dict(request.headers)
                return io.BytesIO(b"{}")

        patchers = [mock.patch.object(update, "_opener", Opener),
                    mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_secret"})]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        return seen

    def test_the_api_call_carries_it(self):
        seen = self.capture()
        update._open(update.API_LATEST, 5)
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer ghp_secret")
        self.assertEqual(seen["headers"].get("User-agent"), update.USER_AGENT)
        self.assertTrue(update.USER_AGENT.startswith("vcharon/%s " % vcharon.VERSION))

    def test_a_download_never_does(self):
        seen = self.capture()
        update._open(DOWNLOAD, 5)
        self.assertFalse([k for k in seen["headers"] if k.lower() == "authorization"])

    def redirect(self, newurl):
        handler = update._StripAuthOnRedirect()
        request = urllib.request.Request(update.API_LATEST,
                                         headers={"Authorization": "Bearer ghp_secret"})
        return handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {}, newurl)

    def test_a_redirect_to_another_host_drops_it(self):
        new = self.redirect("https://objects.githubusercontent.com/thing")
        self.assertFalse([k for k in new.headers if k.lower() == "authorization"])

    def test_a_redirect_off_https_isnt_followed(self):
        for url in ("http://api.github.com/repos/x/y", "http://objects.githubusercontent.com/a",
                    "file:///etc/passwd"):
            with self.subTest(url=url):
                self.assertIsNone(self.redirect(url))

    def test_a_redirect_on_the_same_host_keeps_it(self):
        new = self.redirect(update.API_ROOT + "repos/x/y/releases/44")
        self.assertTrue([k for k in new.headers if k.lower() == "authorization"])


# --- the swap ---

class ApplyTest(UpdateCase):
    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_replaces_the_binary_and_keeps_its_mode(self):
        os.chmod(self.target, 0o700)
        net, release = self.serving()
        self.serve(net)
        steps = []
        result = update.apply_update(release, self.inst, on_step=steps.append)
        self.assertEqual(result, update.UpdateResult(self.target, "9.9.9", True))
        self.assertEqual(read(self.target), fake_binary())
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o700)
        self.assertTrue(any(s.startswith("downloading ") for s in steps), steps)
        self.assertIn("verifying the checksum ...", steps)

    def test_a_checksum_mismatch_changes_nothing(self):
        tarball = make_tarball(os.path.join(self.tmp, TARBALL))
        self.serve(Net({DOWNLOAD: read(tarball), SUMS: "%s  %s\n" % ("0" * 64, TARBALL)}))
        release = update.Release("v9.9.9", "9.9.9", "u", "",
                                 {TARBALL: DOWNLOAD, TARBALL + ".sha256": SUMS})
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, self.inst)
        self.assertEqual(caught.exception.kind, "checksum_mismatch")
        self.assert_untouched()

    def test_the_sums_files_first_word_counts(self):
        # `<hex>  <file>`, as sha256sum prints it; upper-case hex is the same hash
        tarball = make_tarball(os.path.join(self.tmp, TARBALL))
        self.serve(Net({DOWNLOAD: read(tarball),
                        SUMS: "%s *%s\r\n" % (sha256(tarball).upper(), TARBALL)}))
        release = update.Release("v9.9.9", "9.9.9", "u", "",
                                 {TARBALL: DOWNLOAD, TARBALL + ".sha256": SUMS})
        self.assertTrue(update.apply_update(release, self.inst).verified)

    def test_no_checksum_installs_but_says_so(self):
        tarball = make_tarball(os.path.join(self.tmp, TARBALL))
        self.serve(Net({DOWNLOAD: read(tarball)}))
        release = update.Release("v9.9.9", "9.9.9", "u", "", {TARBALL: DOWNLOAD})
        steps = []
        result = update.apply_update(release, self.inst, on_step=steps.append)
        self.assertFalse(result.verified)
        self.assertTrue(any(s.startswith("warning: ") for s in steps), steps)
        self.assertEqual(read(self.target), fake_binary())

    def test_a_listed_checksum_that_wont_download_fails(self):
        # only a release that lists no .sha256 warns; one it lists but that won't come is an
        # error, never "not verified"
        tarball = make_tarball(os.path.join(self.tmp, TARBALL))
        release = update.Release("v9.9.9", "9.9.9", "u", "",
                                 {TARBALL: DOWNLOAD, TARBALL + ".sha256": SUMS})
        for error in (update.UpdateError("not_found", "HTTP 404"),
                      update.UpdateError("http_error", "HTTP 500"),
                      update.UpdateError("network", "connection reset")):
            with self.subTest(kind=error.kind):
                self.serve(Net({DOWNLOAD: read(tarball), SUMS: error}))
                with self.assertRaises(update.UpdateError) as caught:
                    update.apply_update(release, self.inst)
                self.assertEqual(caught.exception.kind, error.kind)
                self.assert_untouched()

    def test_an_unusable_checksum_file_fails(self):
        # published but empty or not a sha256: not the same as none published
        tarball = make_tarball(os.path.join(self.tmp, TARBALL))
        release = update.Release("v9.9.9", "9.9.9", "u", "",
                                 {TARBALL: DOWNLOAD, TARBALL + ".sha256": SUMS})
        for sums in ("\n", "<html>not found</html>\n", "abc123  %s\n" % TARBALL):
            with self.subTest(sums=sums):
                self.serve(Net({DOWNLOAD: read(tarball), SUMS: sums}))
                with self.assertRaises(update.UpdateError) as caught:
                    update.apply_update(release, self.inst)
                self.assertEqual((caught.exception.kind, caught.exception.fix),
                                 ("bad_asset", update.BROKEN_FIX))
                self.assert_untouched()

    def test_a_binary_that_wont_run_isnt_installed(self):
        # the glibc-too-old case: the prebuilt Linux binary's known way to fail; its first
        # line is in the message, and the fix points to pipx
        net, release = self.serving(binary=fake_binary(
            prints="vcharon: /lib/libc.so.6: version GLIBC_2.39 not found", exit_code=1))
        self.serve(net)
        for osn, fix in (("linux", update.SMOKE_FIX_LINUX), ("darwin", update.SMOKE_FIX),
                         ("windows", update.SMOKE_FIX)):
            with self.subTest(os=osn), \
                    mock.patch.object(platform, "os_name", return_value=osn), \
                    self.assertRaises(update.UpdateError) as caught:
                update.apply_update(release, self.inst, windows=False)
            self.assertEqual((caught.exception.kind, caught.exception.fix), ("smoke_failed", fix))
            self.assertIn("GLIBC", caught.exception.message)
            self.assertIn("pipx install", fix)
            self.assert_untouched()
        self.assertIn("glibc", update.SMOKE_FIX_LINUX)

    def test_the_wrong_version_isnt_installed(self):
        net, release = self.serving(binary=fake_binary(prints="0.1.0"))
        self.serve(net)
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, self.inst)
        self.assertEqual(caught.exception.kind, "version_mismatch")
        self.assert_untouched()

    def test_a_binary_that_hangs_isnt_installed(self):
        net, release = self.serving(binary=b"import time\ntime.sleep(60)\n")
        self.serve(net)
        self.patch(update, "SMOKE_TIMEOUT", new=0.2)
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, self.inst)
        self.assertEqual(caught.exception.kind, "smoke_failed")
        self.assert_untouched()

    def test_a_vcharon_in_a_folder_isnt_it(self):
        # the top-level rule, as the zip's; and an archive without the binary at all
        for member in ("bin/vcharon", "x/./vcharon", "../vcharon", "vcharon-linux-x64"):
            with self.subTest(member=member):
                net, release = self.serving(member=member)
                self.serve(net)
                with self.assertRaises(update.UpdateError) as caught:
                    update.apply_update(release, self.inst)
                self.assertEqual((caught.exception.kind, caught.exception.fix),
                                 ("bad_asset", update.BROKEN_FIX))
                self.assert_untouched()
        net, release = self.serving(member="./vcharon")
        self.serve(net)
        update.apply_update(release, self.inst)
        self.assertEqual(read(self.target), fake_binary())

    def test_not_an_archive(self):
        self.serve(Net({DOWNLOAD: b"<html>not a tarball</html>"}))
        release = update.Release("v9.9.9", "9.9.9", "u", "", {TARBALL: DOWNLOAD})
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, self.inst)
        self.assertEqual(caught.exception.kind, "bad_asset")
        self.assert_untouched()

    def test_a_climbing_member_is_never_written(self):
        net, release = self.serving(extra="../../evil.sh")
        self.serve(net)
        update.apply_update(release, self.inst)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.sh")))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "evil.sh")))
        self.assertEqual(self.listing(), ["vcharon"])

    def test_a_link_named_vcharon_is_refused(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.subTest(kind=kind):
                path = os.path.join(self.tmp, TARBALL)
                with tarfile.open(path, "w:gz") as tar:
                    info = tarfile.TarInfo("vcharon")
                    info.type = kind
                    info.linkname = "/etc/passwd"
                    tar.addfile(info)
                self.serve(Net({DOWNLOAD: read(path)}))
                release = update.Release("v9.9.9", "9.9.9", "u", "", {TARBALL: DOWNLOAD})
                with self.assertRaises(update.UpdateError) as caught:
                    update.apply_update(release, self.inst)
                self.assertEqual(caught.exception.kind, "bad_asset")
                self.assert_untouched()

    def test_an_oversized_binary_is_refused(self):
        net, release = self.serving()
        self.serve(net)
        self.patch(update, "MAX_BINARY", new=10)
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, self.inst)
        self.assertEqual(caught.exception.kind, "bad_asset")
        self.assertIn("more than 10", caught.exception.message)
        self.assert_untouched()

    def test_a_download_cut_short(self):
        class Torn(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b"", 999)

        self.patch(update, "_open", new=lambda url, timeout: Torn(b""))
        release = update.Release("v9.9.9", "9.9.9", "u", "", {TARBALL: DOWNLOAD})
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, self.inst)
        self.assertEqual(caught.exception.kind, "network")
        self.assert_untouched()

    def test_a_failed_replace_changes_nothing(self):
        net, release = self.serving()
        self.serve(net)
        with mock.patch.object(update.os, "replace", side_effect=PermissionError(13, "denied")), \
                self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, self.inst, windows=False)
        self.assertEqual(caught.exception.kind, "install_failed")
        self.assert_untouched()

    def test_preflight_refuses_before_any_download(self):
        net = self.serve(Net({}))
        self.patch(update, "platform_asset", return_value=None)
        release = update.Release("v9.9.9", "9.9.9", "u", "", {TARBALL: DOWNLOAD})
        with self.assertRaises(update.UpdateError) as caught:
            update.preflight(self.inst, release)
        self.assertEqual((caught.exception.kind, caught.exception.fix),
                         ("unsupported_platform", update.PIPX_FIX))
        self.assertEqual(net.urls, [])

    def test_preflight_sees_a_missing_asset(self):
        release = update.Release("v9.9.9", "9.9.9", "u", "",
                                 {"vcharon-darwin-arm64.tar.gz": "u"})
        for check in (update.preflight, update.apply_update):
            with self.subTest(check=check.__name__):
                with self.assertRaises(update.UpdateError) as caught:
                    check(release=release, inst=self.inst)
                self.assertEqual(caught.exception.kind, "missing_asset")
        self.assert_untouched()

    def test_a_non_binary_install_is_never_swapped(self):
        release = update.Release("v9.9.9", "9.9.9", "u", "", {TARBALL: DOWNLOAD})
        for inst in (install.Install("pipx"), install.Install("source", self.tmp)):
            with self.subTest(kind=inst.kind):
                with self.assertRaises(update.UpdateError) as caught:
                    update.apply_update(release, inst)
                self.assertEqual(caught.exception.kind, "not_self_updatable")
                self.assertEqual(caught.exception.fix, inst.command_for("v9.9.9"))

    def test_the_writable_check_is_real(self):
        # a folder it can't make its temp folder in: refused before a byte is fetched, on
        # every OS (on Windows os.access says yes for a folder whose ACL says no)
        net = self.serve(Net({}))
        release = update.Release("v9.9.9", "9.9.9", "u", "", {TARBALL: DOWNLOAD})
        for osn, fix in (("linux", "run the installer again: " + install.INSTALL_SH),
                         ("darwin", "run the installer again: " + install.INSTALL_SH),
                         ("windows", "run the installer again, in PowerShell: "
                          + install.INSTALL_PS1)):
            with self.subTest(os=osn), \
                    mock.patch.object(update.tempfile, "mkdtemp",
                                      side_effect=PermissionError(13, "Access is denied")), \
                    mock.patch.object(platform, "os_name", return_value=osn):
                with self.assertRaises(update.UpdateError) as caught:
                    update.apply_update(release, self.inst)
                self.assertEqual((caught.exception.kind, caught.exception.fix),
                                 ("not_writable", fix))
        self.assertEqual(net.urls, [])
        self.assertIn("install.sh", install.INSTALL_SH)
        self.assertIn("install.ps1", install.INSTALL_PS1)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions, not as root")
    def test_an_unwritable_folder_refuses_before_downloading(self):
        net = self.serve(Net({}))
        os.chmod(self.bin, 0o500)
        try:
            release = update.Release("v9.9.9", "9.9.9", "u", "", {TARBALL: DOWNLOAD})
            with self.assertRaises(update.UpdateError) as caught:
                update.apply_update(release, self.inst)
        finally:
            os.chmod(self.bin, 0o700)
        self.assertEqual(caught.exception.kind, "not_writable")
        self.assertEqual(net.urls, [])

    def test_stale_temp_folders_go_and_live_ones_stay(self):
        stale = os.path.join(self.bin, update.WORK_PREFIX + "old")
        os.mkdir(stale)
        with open(os.path.join(stale, "junk"), "wb") as f:
            f.write(b"x" * 100)
        os.utime(os.path.join(stale, "junk"), (0, 0))
        os.utime(stale, (0, 0))
        live = os.path.join(self.bin, update.WORK_PREFIX + "live")
        os.mkdir(live)
        with open(os.path.join(live, "downloading"), "wb") as f:
            f.write(b"x")
        net, release = self.serving()
        self.serve(net)
        update.apply_update(release, self.inst)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(live))

    def test_a_slow_download_isnt_taken_for_an_abandoned_one(self):
        # a folder's mtime moves when an entry is made, not when a file in it is written
        slow = os.path.join(self.bin, update.WORK_PREFIX + "slow")
        os.mkdir(slow)
        archive = os.path.join(slow, TARBALL)
        with open(archive, "wb") as f:
            f.write(b"partial download")
        old = time.time() - 7200
        os.utime(slow, (old, old))
        update.sweep_stale(self.bin)
        self.assertTrue(os.path.exists(archive))

    def test_an_unreadable_temp_folder_is_left_alone(self):
        self.assertLess(time.time() - update.touched_at(os.path.join(self.bin, "nope")), 5)

    def test_the_check_runs_in_a_clean_environment(self):
        env = {"LD_LIBRARY_PATH": "/tmp/_MEI-old/lib", "LD_LIBRARY_PATH_ORIG": "/opt/mine/lib",
               "DYLD_LIBRARY_PATH": "/tmp/_MEI-old/lib", "_MEIPASS2": "/tmp/_MEI-old",
               "_PYI_ARCHIVE_FILE": "/usr/bin/vcharon", "PATH": "/usr/bin"}
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/_MEI-old", create=True), \
                mock.patch.dict(os.environ, env, clear=True):
            got = update.child_env()
        self.assertEqual(got, {"LD_LIBRARY_PATH": "/opt/mine/lib", "PATH": "/usr/bin",
                               "PYINSTALLER_RESET_ENVIRONMENT": "1"})

    def test_child_env_leaves_a_normal_run_alone(self):
        with mock.patch.object(sys, "frozen", False, create=True), \
                mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/opt/mine/lib"}, clear=True):
            self.assertEqual(update.child_env(), {"LD_LIBRARY_PATH": "/opt/mine/lib"})

    def fake_run(self, out):
        """Replaces fsops.run; returns the list its calls go to."""
        calls = []

        def run(argv, timeout, new_session=False, env=None, term_wait=0):
            calls.append((argv, timeout, new_session, env, term_wait))
            return update.fsops.Ran(0, out, b"")

        self.patch(update.fsops, "run", new=run)
        return calls

    def test_the_check_goes_through_fsops_run(self):
        # as vcharon runs every program: an argument list, a timeout, the clean environment;
        # in a session of its own with the grace, so a timeout ends a binary's whole group and
        # its bootloader removes its unpack folder
        calls = self.fake_run(b"9.9.9\n")
        update._smoke_test("/x/vcharon.new", "9.9.9")
        self.assertEqual(calls, [([sys.executable, "/x/vcharon.new", "--version"],
                                  update.SMOKE_TIMEOUT, True, update.child_env(),
                                  update.fsops.TERM_WAIT)])

    def test_the_skill_rewrite_goes_through_fsops_run_the_same_way(self):
        calls = self.fake_run(b"")
        self.assertIsNone(update.refresh_skills("/x/vcharon", ["claude"]))
        self.assertEqual(calls, [(["/x/vcharon", "skill", "install", "--claude"],
                                  update.SMOKE_TIMEOUT, True, update.child_env(),
                                  update.fsops.TERM_WAIT)])


@unittest.skipIf(os.name == "nt", "process groups")
class HangingBinaryTest(UpdateCase):
    """The new binary hanging past SMOKE_TIMEOUT, as a one-file binary does: a stand-in
    bootloader makes an unpack folder and starts a child, which holds the pipes; on SIGTERM it
    waits for the child, removes the folder and leaves a mark. A kill of the stand-in alone
    would leave the child running and the folder behind (DESIGN, "Self-update")."""

    def setUp(self):
        super().setUp()
        self.unpack = os.path.join(self.tmp, "_MEI")
        self.ready = os.path.join(self.tmp, "ready")
        self.termed = os.path.join(self.tmp, "termed")
        self.patch(update, "SMOKE_TIMEOUT", new=2)
        self.addCleanup(self.kill_left)

    def bootloader(self):
        """The stand-in's source, run with this Python."""
        child = "import time\ntime.sleep(60)\n"
        return ("import os, signal, subprocess, sys, time\n"
                "os.mkdir(%r)\n"
                "child = subprocess.Popen([sys.executable, '-c', %r])\n"
                "def term(*a):\n"
                "    child.wait()\n"
                "    os.rmdir(%r)\n"
                "    open(%r, 'w').close()\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, term)\n"
                "with open(%r + '.tmp', 'w') as f:\n"
                "    f.write('%%d %%d' %% (os.getpid(), child.pid))\n"
                "os.rename(%r + '.tmp', %r)\n"
                "time.sleep(60)\n"
                % (self.unpack, child, self.unpack, self.termed, self.ready, self.ready,
                   self.ready)).encode("utf-8")

    def pids(self):
        with open(self.ready) as f:
            return [int(pid) for pid in f.read().split()]

    def kill_left(self):
        # a failing run can leave the stand-in or its child behind: never let them run on
        if os.path.exists(self.ready):
            for pid in self.pids():
                with contextlib.suppress(OSError):
                    os.kill(pid, 9)

    def assert_cleaned_up(self):
        # the stand-in was reached by SIGTERM and cleaned up before the kill
        self.assertTrue(os.path.exists(self.ready), "the stand-in never got ready")
        self.assertTrue(os.path.exists(self.termed), "the stand-in got no SIGTERM")
        self.assertFalse(os.path.exists(self.unpack))
        # an orphan is reaped by init: wait for it to vanish, not just to die
        end = time.monotonic() + 5
        for pid in self.pids():
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                self.assertLess(time.monotonic(), end, "pid %d still there" % pid)
                time.sleep(0.05)

    def test_a_hanging_check_ends_the_whole_group(self):
        net, release = self.serving(binary=self.bootloader())
        self.serve(net)
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, self.inst)
        self.assertEqual(caught.exception.kind, "smoke_failed")
        self.assert_untouched()
        self.assert_cleaned_up()

    def test_a_hanging_skill_rewrite_ends_the_whole_group(self):
        binary = os.path.join(self.tmp, "vcharon.new")
        with open(binary, "wb") as f:
            f.write(self.bootloader())
        real = update.skill_argv
        self.patch(update, "skill_argv",
                   new=lambda binary, agents: [sys.executable] + real(binary, agents))
        self.assertEqual(update.refresh_skills(binary, ["claude"]),
                         "it didn't finish within 2 s")
        self.assert_cleaned_up()


class SmokeArgvTest(unittest.TestCase):
    def test_the_binary_itself(self):
        self.assertEqual(update.smoke_argv("/x/vcharon.new"), ["/x/vcharon.new", "--version"])


class PackedReleaseTest(UpdateCase):
    """The assets tests/pack.py makes for a release, as --update downloads them: the release
    workflow packs with pack.py and the updater reads with update.py, so a change to either
    alone must fail here, not in a user's update."""

    def test_packed_assets_install(self):
        for asset, archive, windows in ((ASSET, TARBALL, False), (WIN_ASSET, ZIP, True)):
            with self.subTest(archive=archive):
                dist = os.path.join(self.tmp, "dist-" + asset)
                out = os.path.join(self.tmp, "out-" + asset)
                os.mkdir(dist)
                with open(os.path.join(dist, "vcharon.exe" if windows else "vcharon"),
                          "wb") as f:
                    f.write(fake_binary())
                with contextlib.redirect_stdout(io.StringIO()):
                    pack.main([dist, asset, out])
                url = "https://example.invalid/%s" % archive
                self.serve(Net({url: read(os.path.join(out, archive)),
                                url + ".sha256": read(os.path.join(out, archive + ".sha256"))}))
                self.patch(update, "platform_asset", return_value=asset)
                release = update.Release("v9.9.9", "9.9.9", "u", "",
                                         {archive: url, archive + ".sha256": url + ".sha256"})
                result = update.apply_update(release, self.inst, windows=windows)
                self.assertEqual(result, update.UpdateResult(self.target, "9.9.9", True))
                self.assertEqual(read(self.target), fake_binary())
                with open(self.target, "wb") as f:
                    f.write(OLD)


# --- Windows: the zip and the rename ---

class ZipTest(UpdateCase):
    """The Windows archive: only the top-level entry named exactly vcharon.exe, a file of at
    most MAX_BINARY, streamed to vcharon.new.exe; never extract or extractall."""

    def setUp(self):
        UpdateCase.setUp(self)
        self.patch(update, "platform_asset", return_value=WIN_ASSET)

    def release(self, zip_path):
        self.serve(Net({ZIP_URL: read(zip_path), ZIP_SUMS: "%s  %s\n" % (sha256(zip_path), ZIP)}))
        return update.Release("v9.9.9", "9.9.9", "u", "", {ZIP: ZIP_URL, ZIP + ".sha256": ZIP_SUMS})

    def test_takes_only_vcharon_exe(self):
        path = make_zip(os.path.join(self.tmp, ZIP), extra=(("../evil.exe", b"evil"),
                                                            ("sub/vcharon.exe", b"not me")))
        staged = []
        real = update._smoke_test

        def smoke(binary, expected):
            staged.append(os.path.basename(binary))
            return real(binary, expected)

        self.patch(update, "_smoke_test", new=smoke)
        with mock.patch.object(zipfile.ZipFile, "extract", never_called), \
                mock.patch.object(zipfile.ZipFile, "extractall", never_called):
            result = update.apply_update(self.release(path), self.inst, windows=True)
        self.assertTrue(result.verified)
        self.assertEqual(staged, ["vcharon.new.exe"])
        self.assertEqual(read(self.target), fake_binary())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.exe")))
        # the old copy: deleted at once when nothing runs it
        self.assertEqual(self.listing(), ["vcharon"])

    def test_a_folder_part_isnt_it(self):
        def in_a_folder(zf):
            zf.writestr("vcharon/vcharon.exe", fake_binary())

        def only_a_folder_of_that_name(zf):
            zf.mkdir("vcharon.exe")
            zf.writestr("vcharon.exe/vcharon.exe", fake_binary())

        for make in (in_a_folder, only_a_folder_of_that_name):
            with self.subTest(make=make.__name__):
                path = os.path.join(self.tmp, ZIP)
                with zipfile.ZipFile(path, "w") as zf:
                    make(zf)
                with self.assertRaises(update.UpdateError) as caught:
                    update.apply_update(self.release(path), self.inst, windows=True)
                self.assertEqual(caught.exception.kind, "bad_asset")
                self.assert_untouched()

    def test_an_entry_that_isnt_a_file_is_refused(self):
        # named exactly vcharon.exe, no slash: the MS-DOS folder bit alone (a zip made on
        # Windows has no Unix mode), or the file type in the Unix mode, each on its own
        for label, attr in (("windows folder", 0x10),
                            ("unix folder", (stat.S_IFDIR | 0o755) << 16),
                            ("link", (stat.S_IFLNK | 0o777) << 16)):
            with self.subTest(label):
                path = os.path.join(self.tmp, ZIP)
                info = zipfile.ZipInfo("vcharon.exe")
                info.external_attr = attr
                with zipfile.ZipFile(path, "w") as zf:
                    zf.writestr(info, b"/etc/passwd" if label == "link" else b"")
                with zipfile.ZipFile(path) as zf:
                    self.assertEqual(zf.getinfo("vcharon.exe").external_attr, attr)
                with self.assertRaises(update.UpdateError) as caught:
                    update.apply_update(self.release(path), self.inst, windows=True)
                self.assertEqual(caught.exception.kind, "bad_asset")
                self.assertIn("isn't a file", caught.exception.message)
                self.assert_untouched()

    def test_an_oversized_entry_is_refused(self):
        path = make_zip(os.path.join(self.tmp, ZIP))
        self.patch(update, "MAX_BINARY", new=10)
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(self.release(path), self.inst, windows=True)
        self.assertEqual(caught.exception.kind, "bad_asset")
        self.assertIn("more than 10", caught.exception.message)
        self.assert_untouched()

    def test_an_encrypted_entry(self):
        # zipfile raises RuntimeError for it (an unknown compression: the next test, for real)
        path = make_zip(os.path.join(self.tmp, ZIP))
        error = RuntimeError("File <ZipInfo> is encrypted, password required")
        with mock.patch.object(zipfile.ZipFile, "open", side_effect=error), \
                self.assertRaises(update.UpdateError) as caught:
            update.apply_update(self.release(path), self.inst, windows=True)
        self.assertEqual(caught.exception.kind, "bad_asset")
        self.assert_untouched()

    def test_a_real_unknown_compression(self):
        # the compression method field set to one no Python reads (99, AES)
        path = make_zip(os.path.join(self.tmp, ZIP))
        with open(path, "r+b") as f:
            data = bytearray(f.read())
            for sig, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
                at = data.find(sig)
                data[at + offset:at + offset + 2] = (99).to_bytes(2, "little")
            f.seek(0)
            f.write(data)
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(self.release(path), self.inst, windows=True)
        self.assertEqual(caught.exception.kind, "bad_asset")
        self.assert_untouched()

    def test_a_corrupt_zip(self):
        path = os.path.join(self.tmp, ZIP)
        with open(path, "wb") as f:
            f.write(b"PK\x03\x04 not really")
        with self.assertRaises(update.UpdateError) as caught:
            update.apply_update(self.release(path), self.inst, windows=True)
        self.assertEqual(caught.exception.kind, "bad_asset")
        self.assert_untouched()


class WindowsSwapTest(unittest.TestCase):
    """swap_windows with its file operations faked or on real files, on every OS: the running
    binary renamed to a unique .old-<time>, the new one moved in, the old one put back when
    that fails, each rename retried."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = os.path.join(self.tmp, "vcharon.exe")
        self.staged = os.path.join(self.tmp, "work", "vcharon.new.exe")
        os.mkdir(os.path.dirname(self.staged))
        for path, data in ((self.target, b"old"), (self.staged, b"new")):
            with open(path, "wb") as f:
                f.write(data)
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def test_rename_then_move_in(self):
        calls = []

        def rename(src, dst):
            calls.append((os.path.basename(src), os.path.basename(dst)))
            os.rename(src, dst)

        update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                            now=lambda: 1700000000.5)
        self.assertEqual(calls, [("vcharon.exe", "vcharon.exe.old-1700000000"),
                                 ("vcharon.new.exe", "vcharon.exe")])
        self.assertEqual(read(self.target), b"new")
        # nothing ran the old one here, so it was deleted at once
        self.assertEqual(sorted(os.listdir(self.tmp)), ["vcharon.exe", "work"])

    def test_old_names_are_unique(self):
        # a process of the last update may still run its .old copy, which can't be deleted
        taken = {self.target + ".old-100", self.target + ".old-101"}
        self.assertEqual(update.old_name(self.target, now=lambda: 100.9,
                                         exists=taken.__contains__),
                         self.target + ".old-102")
        self.assertEqual(update.old_name(self.target, now=lambda: 99,
                                         exists=taken.__contains__),
                         self.target + ".old-99")

    def test_a_running_old_copy_is_kept_for_later(self):
        real_remove = os.remove

        def remove(path):
            if ".old-" in path:
                raise PermissionError(13, "in use")
            real_remove(path)

        with mock.patch.object(update.os, "remove", remove):
            update.swap_windows(self.staged, self.target, sleep=self.sleep, now=lambda: 5)
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         ["vcharon.exe", "vcharon.exe.old-5", "work"])

    def test_a_failed_move_puts_the_old_one_back(self):
        calls = []

        def rename(src, dst):
            calls.append((os.path.basename(src), os.path.basename(dst)))
            if src == self.staged:
                raise PermissionError(13, "held by antivirus")
            os.rename(src, dst)

        with self.assertRaises(OSError):
            update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                                now=lambda: 7)
        tries = update.RENAME_TRIES
        self.assertEqual(calls, [("vcharon.exe", "vcharon.exe.old-7")]
                         + [("vcharon.new.exe", "vcharon.exe")] * tries
                         + [("vcharon.exe.old-7", "vcharon.exe")])
        self.assertEqual(self.slept, [update.RENAME_WAIT] * (tries - 1))
        self.assertEqual(read(self.target), b"old")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["vcharon.exe", "work"])

    def test_a_held_file_is_tried_again(self):
        fails = [PermissionError(13, "held"), PermissionError(13, "held")]

        def rename(src, dst):
            if src == self.staged and fails:
                raise fails.pop(0)
            os.rename(src, dst)

        update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                            now=lambda: 8)
        self.assertEqual(read(self.target), b"new")
        self.assertEqual(self.slept, [update.RENAME_WAIT] * 2)
        self.assertLessEqual(update.RENAME_TRIES * update.RENAME_WAIT, 3)

    def test_when_even_the_way_back_fails_it_says_where_the_old_one_is(self):
        def rename(src, dst):
            if src == self.target:
                os.rename(src, dst)
                return
            raise PermissionError(13, "held")

        with self.assertRaises(update.UpdateError) as caught:
            update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                                now=lambda: 9)
        old = self.target + ".old-9"
        self.assertEqual((caught.exception.kind, caught.exception.fix),
                         ("install_failed", "rename %s back to %s" % (old, self.target)))
        # the move's own error and the way back's, both
        self.assertEqual(caught.exception.message,
                         "the new binary couldn't be moved to %s (held), and the old one "
                         "couldn't be put back (held): it is %s now" % (self.target, old))
        self.assertEqual(read(old), b"old")

    def test_a_ctrl_c_on_the_way_back_still_says_where_the_old_one_is(self):
        def rename(src, dst):
            if src == self.target:
                os.rename(src, dst)
                return
            if src == self.staged:
                raise PermissionError(13, "held")
            raise KeyboardInterrupt

        with self.assertRaises(update.UpdateError) as caught:
            update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                                now=lambda: 12)
        old = self.target + ".old-12"
        self.assertEqual((caught.exception.kind, caught.exception.fix),
                         ("install_failed", "rename %s back to %s" % (old, self.target)))
        self.assertIn("(held)", caught.exception.message)
        self.assertIn("(interrupted)", caught.exception.message)
        self.assertEqual(read(old), b"old")

    def test_a_ctrl_c_just_after_the_first_rename_puts_it_back(self):
        # the rename was done, but never returned: the old one is moved all the same
        def rename(src, dst):
            os.rename(src, dst)
            if src == self.target and dst.endswith(".old-13"):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                                now=lambda: 13)
        self.assertEqual(read(self.target), b"old")
        self.assertEqual(read(self.staged), b"new")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["vcharon.exe", "work"])

    def test_a_ctrl_c_once_the_new_one_is_in_leaves_it(self):
        # stopped after the move: the new binary is at target, nothing is lost, and the old
        # copy waits for a later start's sweep
        def rename(src, dst):
            os.rename(src, dst)
            if src == self.staged:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                                now=lambda: 14)
        self.assertEqual(read(self.target), b"new")
        self.assertEqual(read(self.target + ".old-14"), b"old")

    def test_nothing_moved_nothing_put_back(self):
        calls = []

        def rename(src, dst):
            calls.append(os.path.basename(src))
            raise PermissionError(13, "in use")

        with self.assertRaises(PermissionError):
            update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                                now=lambda: 15)
        self.assertEqual(calls, ["vcharon.exe"] * update.RENAME_TRIES)
        self.assertEqual(read(self.target), b"old")

    def test_a_ctrl_c_during_the_move_puts_the_old_one_back(self):
        calls = []

        def rename(src, dst):
            calls.append((os.path.basename(src), os.path.basename(dst)))
            if src == self.staged:
                raise KeyboardInterrupt
            os.rename(src, dst)

        with self.assertRaises(KeyboardInterrupt):
            update.swap_windows(self.staged, self.target, rename=rename, sleep=self.sleep,
                                now=lambda: 11)
        self.assertEqual(calls, [("vcharon.exe", "vcharon.exe.old-11"),
                                 ("vcharon.new.exe", "vcharon.exe"),
                                 ("vcharon.exe.old-11", "vcharon.exe")])
        self.assertEqual(read(self.target), b"old")
        self.assertEqual(read(self.staged), b"new")

    def apply_case(self):
        """A ZipTest set up for apply_update on the Windows swap: (case, release)."""
        case = ZipTest("test_takes_only_vcharon_exe")
        case.setUp()
        self.addCleanup(case.doCleanups)
        release = case.release(make_zip(os.path.join(case.tmp, ZIP)))
        case.patch(update, "RENAME_WAIT", new=0)
        return case, release

    def test_apply_leaves_the_old_one_when_the_move_fails(self):
        # apply_update with the Windows swap and a move that never works: a Ctrl-C is
        # raised again, a held file is install_failed; the old binary stays either way
        real = os.rename
        for error, raised in ((KeyboardInterrupt, KeyboardInterrupt),
                              (PermissionError(13, "held"), update.UpdateError)):
            with self.subTest(error=raised.__name__):
                case, release = self.apply_case()

                def rename(src, dst, error=error):
                    if src.endswith("vcharon.new.exe"):
                        raise error
                    real(src, dst)

                with mock.patch.object(update.os, "rename", rename), \
                        self.assertRaises(raised) as caught:
                    update.apply_update(release, case.inst, windows=True)
                if raised is update.UpdateError:
                    self.assertEqual(caught.exception.kind, "install_failed")
                case.assert_untouched()

    def test_apply_keeps_the_new_one_when_nothing_is_at_the_target(self):
        # both renames back fail: the work folder holds the new binary, and stays
        case, release = self.apply_case()
        real = os.rename

        def rename(src, dst):
            if dst == case.target:
                raise PermissionError(13, "held")
            real(src, dst)

        with mock.patch.object(update.os, "rename", rename), \
                self.assertRaises(update.UpdateError) as caught:
            update.apply_update(release, case.inst, windows=True)
        self.assertEqual(caught.exception.kind, "install_failed")
        self.assertTrue(caught.exception.fix.startswith("rename %s.old-" % case.target))
        self.assertFalse(os.path.exists(case.target))
        names = case.listing()
        [old] = [n for n in names if n.startswith("vcharon.old-")]
        self.assertEqual(read(os.path.join(case.bin, old)), OLD)
        [work] = [n for n in names if n.startswith(update.WORK_PREFIX)]
        self.assertEqual(read(os.path.join(case.bin, work, "vcharon.new.exe")), fake_binary())


class SweepOldTest(unittest.TestCase):
    """Every start of a Windows binary deletes the .old-* copies next to it that it can."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.exe = os.path.join(self.tmp, "vcharon.exe")
        for name in ("vcharon.exe", "vcharon.exe.old-1", "vcharon.exe.old-2", "other.old-1",
                     "vcharon.exe.bak"):
            with open(os.path.join(self.tmp, name), "wb") as f:
                f.write(b"x")

    def test_sweep(self):
        real_remove = os.remove

        def remove(path):
            if path.endswith(".old-2"):
                raise PermissionError(13, "still running")
            real_remove(path)

        with mock.patch.object(install.os, "remove", remove):
            install.sweep_old(self.exe)
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         ["other.old-1", "vcharon.exe", "vcharon.exe.bak", "vcharon.exe.old-2"])

    def test_at_start_of_a_windows_binary(self):
        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.object(platform, "os_name", return_value="windows"), \
                mock.patch.object(sys, "executable", self.exe), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         ["other.old-1", "vcharon.exe", "vcharon.exe.bak"])

    def test_not_elsewhere(self):
        for frozen, osn in ((True, "linux"), (False, "windows")):
            with self.subTest(frozen=frozen, os=osn), \
                    mock.patch.object(platform, "is_frozen", return_value=frozen), \
                    mock.patch.object(platform, "os_name", return_value=osn), \
                    mock.patch.object(sys, "executable", self.exe), \
                    mock.patch.object(install, "sweep_old", never_called), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--version"]), 0)


# --- the watchers' check ---

class OrphanTest(unittest.TestCase):
    """A binary's check that its bootloader parent is still there: a SIGKILL ends the
    bootloader alone, and the Python child would run on with its locks."""

    def test_not_in_a_python_install(self):
        with mock.patch.object(platform, "is_frozen", return_value=False):
            dog = install.Watchdog(__file__)
        self.assertIsNone(dog.parent)
        self.assertFalse(dog.orphaned())

    def test_posix_compares_with_the_parent_at_start(self):
        ppids = [4242]
        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.object(platform, "os_name", return_value="linux"), \
                mock.patch.object(install.os, "getppid", lambda: ppids[0]):
            dog = install.Watchdog(__file__)
            self.assertEqual(dog.parent.pid, 4242)
            self.assertFalse(dog.orphaned())
            # taken by a subreaper, not init: any other pid counts, not only 1
            ppids[0] = 977
            self.assertTrue(dog.orphaned())
            ppids[0] = 1
            self.assertTrue(dog.orphaned())

    def test_windows_waits_on_a_handle_opened_at_start(self):
        class FakeWinapi:
            WAIT_OBJECT_0 = 0
            WAIT_TIMEOUT = 258

            def __init__(self):
                self.opened = []
                self.state = self.WAIT_TIMEOUT

            def OpenProcess(self, access, inherit, pid):
                self.opened.append((access, inherit, pid))
                return 77

            def WaitForSingleObject(self, handle, ms):
                assert (handle, ms) == (77, 0)
                return self.state

        fake = FakeWinapi()
        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.object(platform, "os_name", return_value="windows"), \
                mock.patch.object(install, "_winapi", fake), \
                mock.patch.object(install.os, "getppid", lambda: 3100):
            dog = install.Watchdog(__file__)
            self.assertEqual(fake.opened, [(install.SYNCHRONIZE, False, 3100)])
            self.assertFalse(dog.orphaned())
            fake.state = fake.WAIT_OBJECT_0
            self.assertTrue(dog.orphaned())

    def test_windows_without_a_handle_is_never_orphaned(self):
        class Refusing:
            WAIT_OBJECT_0 = 0

            def OpenProcess(self, access, inherit, pid):
                raise PermissionError(5, "Access is denied")

        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.object(platform, "os_name", return_value="windows"), \
                mock.patch.object(install, "_winapi", Refusing()):
            dog = install.Watchdog(__file__)
            self.assertFalse(dog.orphaned())

    def test_every_self_start_gets_its_own_bootloader(self):
        # The orphan check takes the parent for the bootloader. A binary started with the
        # parent's _PYI_* environment and no reset runs without one of its own, and would
        # take its launcher for it: each place vcharon starts itself must reset or strip.
        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.dict(os.environ, {"_PYI_ARCHIVE_FILE": "/x", "_MEIPASS2": "/m"}):
            # the streaming sync child
            spawned = []

            def spawn(argv, env):
                spawned.append(env)
                raise OSError("only the environment was wanted")

            with self.assertRaises(OSError):
                watch.Stream("mb.debian", ["mb"], 2, spawn=spawn)._start()
            self.assertEqual(spawned[0]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            # a --no-stream round's sync
            ran = []

            def run(argv, timeout, new_session=False, env=None, term_wait=0):
                ran.append(env)
                return fsops.Ran(0, b"", b"")

            with mock.patch.object(watch.fsops, "run", run):
                watch.run_sync("mb.debian", ["mb"])
            self.assertEqual(ran[0]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            # the downloaded binary's --version, and its skill install after the swap
            env = update.child_env()
            self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertFalse([k for k in env if k.startswith(("_PYI_", "_MEIPASS"))])
        # and these are all the places: a new one must join the checks above
        starts = []
        root = os.path.dirname(os.path.abspath(vcharon.__file__))
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        text = f.read()
                    rel = os.path.relpath(os.path.join(dirpath, name), root)
                    starts += ["%s:%s" % (rel.replace(os.sep, "/"), m.group(1)) for m in
                               re.finditer(r"\b(sync_argv|smoke_argv|skill_argv|self_argv)\(",
                                           text)]
        self.assertEqual(sorted(starts), sorted([
            # each name's def and its docstring mentions count too
            "platform.py:self_argv", "platform.py:self_argv",
            "mailbox/watch.py:sync_argv", "mailbox/watch.py:self_argv",
            "mailbox/watch.py:sync_argv", "mailbox/watch.py:sync_argv",
            "update.py:smoke_argv", "update.py:smoke_argv",
            # the installed binary's skill install, with the same environment
            "update.py:skill_argv", "update.py:skill_argv"]))


class FakeWaitWinapi:
    """_winapi's calls a Windows binary makes on its bootloader: the handle opened at start,
    and a wait that ends when the test says the bootloader is gone."""

    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    INFINITE = 0xFFFFFFFF

    def __init__(self, result=0):
        self.opened = []
        self.gone = threading.Event()
        self.result = result

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return 77

    def WaitForSingleObject(self, handle, ms):
        if ms == 0:
            return self.WAIT_OBJECT_0 if self.gone.is_set() else self.WAIT_TIMEOUT
        assert (handle, ms) == (77, self.INFINITE)
        self.gone.wait()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class ExitWithParentTest(unittest.TestCase):
    """Every command of a Windows binary ends at once when its bootloader is gone: a kill of
    the bootloader alone would leave it running with its locks."""

    def setUp(self):
        self.exits = []
        self.exited = threading.Semaphore(0)

        def fake_exit(code):
            self.exits.append(code)
            self.exited.release()

        # os._exit would end the suite; the timer's call comes after the patch is gone, so the
        # fake is what the timer holds, never a name looked up later
        patcher = mock.patch.object(install, "_exit", fake_exit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_exits(self, n):
        for _ in range(n):
            self.assertTrue(self.exited.acquire(timeout=10), "no exit after %s" % self.exits)

    def main(self, fake, frozen=True, osn="windows"):
        """cli.main(["--version"]) as a binary on osn, with fake for _winapi; the run it
        made. The thread it starts reads _winapi after the wait too: a test that ends that
        wait keeps fake in place itself."""
        runs = []
        real_run = cli._Run

        def make_run():
            runs.append(real_run())
            return runs[-1]

        with mock.patch.object(platform, "is_frozen", return_value=frozen), \
                mock.patch.object(platform, "os_name", return_value=osn), \
                mock.patch.object(install, "_winapi", fake), \
                mock.patch.object(install, "sweep_old", lambda *a: None), \
                mock.patch.object(install.os, "getppid", lambda: 3100), \
                mock.patch.object(cli, "_Run", make_run), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--version"]), 0)
        return runs[0]

    def test_a_windows_binary_logs_and_exits_15_when_its_bootloader_goes(self):
        fake = FakeWaitWinapi()
        with mock.patch.object(install, "ORPHAN_WRITE_WAIT", 0.2), \
                mock.patch.object(install, "_winapi", fake):
            run = self.main(fake)
            self.assertEqual(fake.opened, [(install.SYNCHRONIZE, False, 3100)])
            self.assertEqual(run.parent.pid, 3100)
            # the command ran and returned; the thread still waits
            self.assertEqual(self.exits, [])
            fake.gone.set()
            # the thread's exit, then the timer's (os._exit would have ended it first)
            self.wait_exits(2)
        self.assertEqual(self.exits, [install.EXIT_ORPHANED] * 2)
        with open(run.log.path, encoding="utf-8") as f:
            last = f.read().splitlines()[-1]
        self.assertIn("info  the process that started this one (pid 3100) is gone; exiting "
                      "with 15", last)

    def test_a_blocked_write_cant_hold_the_exit(self):
        fake = FakeWaitWinapi()
        release = threading.Event()
        parent = mock.Mock(handle=77, followed=False)
        with mock.patch.object(install, "_winapi", fake), \
                mock.patch.object(install, "ORPHAN_WRITE_WAIT", 0.2):
            thread = install.follow(parent, release.wait)
            # after the release, the thread exits again: through the fake, so before the
            # patch is undone
            self.addCleanup(thread.join, 10)
            self.addCleanup(release.set)
            fake.gone.set()
            # on_gone never returns: the timer exits
            self.wait_exits(1)
        self.assertEqual(self.exits, [install.EXIT_ORPHANED])

    def test_no_exit_when_the_wait_fails(self):
        for result in (OSError(6, "The handle is invalid"), 0x102):
            fake = FakeWaitWinapi(result)
            parent = mock.Mock(handle=77, followed=False)
            with self.subTest(result=result), mock.patch.object(install, "_winapi", fake):
                thread = install.follow(parent, never_called)
                fake.gone.set()
                thread.join(10)
                self.assertFalse(thread.is_alive())
                self.assertIs(parent.followed, False)
        self.assertEqual(self.exits, [])

    def test_nothing_without_a_handle(self):
        class Refusing(FakeWaitWinapi):
            def OpenProcess(self, access, inherit, pid):
                raise PermissionError(5, "Access is denied")

        with mock.patch.object(install, "follow", never_called):
            run = self.main(Refusing())
        self.assertIsNone(run.parent.handle)

    def test_not_elsewhere(self):
        for frozen, osn in ((True, "linux"), (True, "darwin"), (False, "windows")):
            with self.subTest(frozen=frozen, os=osn), \
                    mock.patch.object(install, "exit_with_parent", never_called):
                run = self.main(FakeWaitWinapi(), frozen, osn)
                self.assertIsNone(run.parent)

    def test_the_watchdog_reuses_the_parent(self):
        fake = FakeWaitWinapi()
        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.object(platform, "os_name", return_value="windows"), \
                mock.patch.object(install, "_winapi", fake), \
                mock.patch.object(install.os, "getppid", lambda: 3100):
            run = cli._Run()
            run.parent = install.Parent()
            dog = run.watch_code(lambda line: None)
            self.assertIs(dog.parent, run.parent)
            self.assertEqual(len(fake.opened), 1)
            self.assertFalse(dog.orphaned())
            fake.gone.set()
            self.assertTrue(dog.orphaned())

    def test_watch_and_repeat_print_exit_orphaned(self):
        said = []
        run = cli._Run()
        parent = mock.Mock(pid=3100)
        run.parent_gone(parent)
        # any other command: the log line only
        self.assertEqual(said, [])
        with mock.patch.object(platform, "is_frozen", return_value=False):
            run.watch_code(said.append)
        run.parent_gone(parent)
        self.assertEqual(said, ["EXIT orphaned (exit 15)"])

    def test_a_bootloader_gone_before_main_stores_it(self):
        fake = FakeWaitWinapi()
        fake.gone.set()
        real_follow = install.follow

        def follow_to_the_end(parent, on_gone):
            # the thread runs to its exit before main has the Parent
            thread = real_follow(parent, on_gone)
            thread.join(10)
            return thread

        with mock.patch.object(install, "ORPHAN_WRITE_WAIT", 0.2), \
                mock.patch.object(install, "follow", follow_to_the_end):
            run = self.main(fake)
            self.wait_exits(2)
        self.assertEqual(self.exits, [install.EXIT_ORPHANED] * 2)
        with open(run.log.path, encoding="utf-8") as f:
            last = f.read().splitlines()[-1]
        self.assertIn("(pid 3100) is gone; exiting with 15", last)

    def test_a_followed_parent_is_the_threads_to_report(self):
        fake = FakeWaitWinapi()
        with mock.patch.object(platform, "os_name", return_value="windows"), \
                mock.patch.object(install, "_winapi", fake), \
                mock.patch.object(install.os, "getppid", lambda: 3100):
            parent = install.Parent()
            dog = install.Watchdog(__file__, parent=parent)
            fake.gone.set()
            self.assertTrue(dog.orphaned())
            # with the thread waiting, the round's check leaves the exit to it
            parent.followed = True
            self.assertFalse(dog.orphaned())

    def test_a_failed_wait_gives_the_check_back(self):
        fake = FakeWaitWinapi(OSError(6, "The handle is invalid"))
        # a plain False, not a Mock's attribute: any Mock attribute is truthy
        parent = mock.Mock(handle=77, followed=False)
        with mock.patch.object(install, "_winapi", fake):
            thread = install.follow(parent, never_called)
            self.assertIs(parent.followed, True)
            fake.gone.set()
            thread.join(10)
        self.assertIs(parent.followed, False)

    @unittest.skipUnless(os.name == "nt", "the real _winapi")
    def test_windows_ends_with_a_real_parent(self):
        import _winapi

        tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        pid_file = os.path.join(tmp, "pid")
        marker = os.path.join(tmp, "gone")
        # the child does what main does in a binary, then waits; its parent stands in for the
        # bootloader, waiting on it
        child = ("import os, sys, time\n"
                 "from vcharon import install\n"
                 "install.follow(install.Parent(), lambda: open(%r, 'w').close())\n"
                 "with open(%r + '.part', 'w') as f:\n"
                 "    f.write(str(os.getpid()))\n"
                 "os.replace(%r + '.part', %r)\n"
                 "time.sleep(120)\n" % (marker, pid_file, pid_file, pid_file))
        stand_in = ("import subprocess, sys\n"
                    "subprocess.run([sys.executable, '-c', %r])\n" % child)
        env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(
            os.path.abspath(install.__file__))))
        # the base interpreter: a venv's python.exe is a launcher that starts the real one as
        # its child, which would put a third process between the stand-in and the child
        python = getattr(sys, "_base_executable", None) or sys.executable
        parent = subprocess.Popen([python, "-c", stand_in], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(parent.wait)
        self.addCleanup(parent.kill)
        deadline = time.monotonic() + 30
        while not os.path.exists(pid_file):
            self.assertLess(time.monotonic(), deadline, "the child never started")
            time.sleep(0.05)
        with open(pid_file, encoding="utf-8") as f:
            pid = int(f.read())
        # taken while the child runs: a check after it ended could find another process
        # with its pid (and os.kill(pid, 0) would kill it on Windows)
        # PROCESS_QUERY_LIMITED_INFORMATION for the exit code, PROCESS_TERMINATE for the
        # cleanup of a child that didn't end
        handle = _winapi.OpenProcess(install.SYNCHRONIZE | 0x1000 | 0x0001, False, pid)
        self.addCleanup(_winapi.CloseHandle, handle)
        try:
            # the stand-in bootloader alone, as TerminateProcess does
            parent.kill()
            self.assertEqual(_winapi.WaitForSingleObject(handle, 10000),
                             _winapi.WAIT_OBJECT_0)
        finally:
            if _winapi.WaitForSingleObject(handle, 0) != _winapi.WAIT_OBJECT_0:
                _winapi.TerminateProcess(handle, 1)
        self.assertEqual(_winapi.GetExitCodeProcess(handle), install.EXIT_ORPHANED)
        self.assertTrue(os.path.exists(marker))


class WatchdogTest(unittest.TestCase):
    """The identity a long-running command compares at the top of each round: size,
    modification time and file id of a binary's own file, or of the package's __init__.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "vcharon")
        with open(self.path, "wb") as f:
            f.write(b"binary")
        os.utime(self.path, ns=(10 ** 18, 10 ** 18))

    def test_unchanged(self):
        dog = install.Watchdog(self.path)
        self.assertEqual(dog.start, install.identity(self.path))
        self.assertFalse(dog.changed())

    def test_each_part_counts(self):
        def size():
            with open(self.path, "ab") as f:
                f.write(b"!")
            os.utime(self.path, ns=(10 ** 18, 10 ** 18))

        def mtime():
            os.utime(self.path, ns=(10 ** 18, 10 ** 18 + 2 * 10 ** 9))

        def file_id():
            # same size and time, another file: what a rename over it leaves
            new = self.path + ".new"
            with open(new, "wb") as f:
                f.write(b"binary")
            os.utime(new, ns=(10 ** 18, 10 ** 18))
            os.replace(new, self.path)

        for change in (size, mtime, file_id, lambda: os.remove(self.path)):
            with self.subTest(change=change.__name__):
                with open(self.path, "wb") as f:
                    f.write(b"binary")
                os.utime(self.path, ns=(10 ** 18, 10 ** 18))
                dog = install.Watchdog(self.path)
                change()
                self.assertTrue(dog.changed())

    def test_the_file_it_watches(self):
        with mock.patch.object(platform, "is_frozen", return_value=True), \
                mock.patch.object(sys, "executable", self.path):
            self.assertEqual(install.code_path(), self.path)
        with mock.patch.object(platform, "is_frozen", return_value=False):
            self.assertEqual(install.code_path(), os.path.join(
                os.path.dirname(os.path.abspath(vcharon.__file__)), "__init__.py"))
            self.assertTrue(os.path.isfile(install.code_path()))

    def test_after_a_crash_only_a_binary(self):
        dog = install.Watchdog(self.path)
        os.utime(self.path, ns=(10 ** 18, 10 ** 18 + 2 * 10 ** 9))
        with mock.patch.object(platform, "is_frozen", return_value=True):
            self.assertTrue(dog.after_crash())
        with mock.patch.object(platform, "is_frozen", return_value=False):
            self.assertFalse(dog.after_crash())
        dog = install.Watchdog(self.path)
        with mock.patch.object(platform, "is_frozen", return_value=True):
            self.assertFalse(dog.after_crash())


class ImportsAtStartTest(unittest.TestCase):
    def test_the_command_line_loads_every_module(self):
        # a long-running command imports nothing after its start: every module it can need is
        # loaded with cli (DESIGN, "Running watchers"); --update's own is the one left out
        code = ("import json, os, sys\n"
                "import vcharon.cli\n"
                "root = os.path.dirname(vcharon.__file__)\n"
                "names = []\n"
                "for folder, dirs, files in os.walk(root):\n"
                "    dirs[:] = [d for d in dirs if d != '__pycache__']\n"
                "    for f in files:\n"
                "        if f.endswith('.py'):\n"
                "            rel = os.path.relpath(os.path.join(folder, f[:-3]), root)\n"
                "            parts = ['vcharon'] + rel.split(os.sep)\n"
                "            names.append('.'.join(parts[:-1] if parts[-1] == '__init__' "
                "else parts))\n"
                "print(json.dumps(sorted(n for n in names if n not in sys.modules)))\n")
        ran = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True,
                             timeout=60, check=False)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(ran.stdout), ["vcharon.__main__", "vcharon.guide.__main__",
                                                  "vcharon.update"])


# --- the flag ---

class UpdateFlagTest(FakeSshCase):
    """vcharon --update through the command line: a binary install of a stand-in, GitHub
    faked."""

    maxDiff = None

    def setUp(self):
        FakeSshCase.setUp(self)
        UpdateCase.setUp(self)
        self.tarball = make_tarball(os.path.join(self.tmp, TARBALL))
        self.net = self.serve(Net({update.API_LATEST: json.dumps(release_json()),
                                   DOWNLOAD: read(self.tarball),
                                   SUMS: "%s  %s\n" % (sha256(self.tarball), TARBALL)}))
        self.patch(install, "detect", return_value=self.inst)
        # no terminal unless a test gives one
        self.patch(cli, "_stdin_is_terminal", return_value=False)
        self.patch(platform, "self_command", return_value="vcharon")
        # a final build, whatever this one is; the pre-release build's tests say otherwise
        self.patch(update, "is_prerelease", return_value=False)

    patch = UpdateCase.patch
    serve = UpdateCase.serve
    listing = UpdateCase.listing
    assert_untouched = UpdateCase.assert_untouched

    def tty(self, answer):
        self.patch(cli, "_stdin_is_terminal", return_value=True)
        if isinstance(answer, BaseException):
            self.patch(cli, "input", create=True, side_effect=answer)
        else:
            self.patch(cli, "input", create=True, return_value=answer)

    def run_json(self, *argv):
        code, out, err = self.run_cli(*argv)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)
        return code, json.loads(lines[0]), err

    def test_up_to_date(self):
        self.serve(Net({update.API_LATEST: json.dumps(release_json(tag="v" + vcharon.VERSION))}))
        code, doc, err = self.run_json("--update", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((doc["ok"], doc["update_available"], doc["changed"]),
                         (True, False, False))
        code, out, err = self.run_cli("--update")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "vcharon: update", "  current  %s (binary, %s)" % (vcharon.VERSION, self.target),
            "  latest   %s (https://github.com/%s/releases/tag/v%s)"
            % (vcharon.VERSION, update.REPO, vcharon.VERSION),
            "  %s is the latest release" % vcharon.VERSION, "OK"])

    def test_ahead_of_the_release(self):
        self.serve(Net({update.API_LATEST: json.dumps(release_json(tag="v0.0.1"))}))
        code, out, _ = self.run_cli("--update")
        self.assertEqual(code, 0)
        self.assertIn("  %s is ahead of the latest release" % vcharon.VERSION, out.splitlines())

    def test_json_reports_without_installing(self):
        self.patch(update, "apply_update", new=never_called)
        code, doc, err = self.run_json("--update", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual({k: doc[k] for k in ("update_available", "latest", "tag", "changed",
                                              "confirmed", "ok")},
                         {"update_available": True, "latest": "9.9.9", "tag": "v9.9.9",
                          "changed": False, "confirmed": False, "ok": True})
        self.assertEqual(sorted(doc), list(self.BASE_KEYS))
        self.assertEqual((doc["install"], doc["path"], doc["current"]),
                         ("binary", self.target, vcharon.VERSION))
        self.assert_untouched()

    RC_URL = "https://github.com/%s/releases/tag/v99.0.0rc1" % update.REPO

    def serve_list(self, *entries, binary_version=None):
        """GitHub with the releases list entries, and releases/latest as in setUp; the archive
        holds a binary that prints binary_version."""
        tarball = make_tarball(os.path.join(self.tmp, "list.tar.gz"),
                               fake_binary(binary_version or "9.9.9"))
        return self.serve(Net({update.API_LATEST: json.dumps(release_json()),
                               update.API_RELEASES: json.dumps(list(entries)),
                               DOWNLOAD: read(tarball),
                               SUMS: "%s  %s\n" % (sha256(tarball), TARBALL)}))

    def as_build(self, version):
        self.patch(cli, "VERSION", new=version)
        self.patch(update, "is_prerelease", new=IS_PRERELEASE)

    def test_a_final_build_reads_only_full_releases(self):
        # a final VERSION: the release job runs the suite at the tag, which may be an rc
        self.as_build("1.0.0")
        self.patch(update, "apply_update", new=never_called)
        net = self.serve_list(release_json("v99.0.0rc1", prerelease=True))
        code, doc, _ = self.run_json("--update", "--json")
        self.assertEqual((code, doc["latest"], doc["prerelease"], doc["rc"]),
                         (0, "9.9.9", False, False))
        code, out, _ = self.run_cli("--update")
        self.assertEqual(code, 0)
        self.assertIn("  latest   9.9.9 (https://github.com/%s/releases/tag/v9.9.9)"
                      % update.REPO, out.splitlines())
        self.assertNotIn("pre-release", out)
        self.assertEqual(net.urls, [update.API_LATEST] * 2)

    def test_rc_offers_a_pre_release(self):
        self.patch(update, "apply_update", new=never_called)
        net = self.serve_list(release_json("v9.9.9"),
                              release_json("v99.0.0rc1", prerelease=True))
        code, doc, _ = self.run_json("--update", "--rc", "--json")
        self.assertEqual({k: doc[k] for k in ("latest", "tag", "prerelease", "rc",
                                              "update_available", "changed")},
                         {"latest": "99.0.0rc1", "tag": "v99.0.0rc1", "prerelease": True,
                          "rc": "flag", "update_available": True, "changed": False})
        self.assertEqual(sorted(doc), list(self.BASE_KEYS))
        code, out, err = self.run_cli("--update", "--rc")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[1:], [
            "  current  %s (binary, %s)" % (vcharon.VERSION, self.target),
            "  latest   99.0.0rc1 (pre-release, %s)" % self.RC_URL,
            "  99.0.0rc1 is available; to install it: vcharon --update --rc --yes"])
        self.assertEqual(net.urls, [update.API_RELEASES] * 2)
        self.assert_untouched()

    def test_rc_installs_with_yes_and_force(self):
        self.serve_list(release_json("v99.0.0rc1", prerelease=True),
                        binary_version="99.0.0rc1")
        code, doc, err = self.run_json("--update", "--rc", "--yes", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((doc["changed"], doc["installed"], doc["rc"]), (True, "99.0.0rc1", "flag"))
        self.assertEqual(read(self.target), fake_binary("99.0.0rc1"))
        # --force with --rc: an older pre-release can be installed
        self.serve_list(release_json("v0.0.1rc1", prerelease=True), binary_version="0.0.1rc1")
        code, out, _ = self.run_cli("--update", "--rc", "--force")
        self.assertEqual((code, out.splitlines()[-1]),
                         (0, "  0.0.1rc1 can be installed: vcharon --update --rc --force --yes"))
        code, doc, _ = self.run_json("--update", "--force", "--rc", "--yes", "--json")
        self.assertEqual((code, doc["installed"], doc["prerelease"]), (0, "0.0.1rc1", True))

    def test_a_pre_release_build_counts_pre_releases(self):
        self.as_build("0.5.0rc1")
        self.patch(update, "apply_update", new=never_called)
        rc2 = release_json("v0.5.0rc2", prerelease=True)
        net = self.serve_list(release_json("v0.4.2"), rc2)
        code, doc, _ = self.run_json("--update", "--json")
        self.assertEqual((code, doc["latest"], doc["prerelease"], doc["rc"],
                          doc["update_available"]), (0, "0.5.0rc2", True, "current", True))
        code, out, _ = self.run_cli("--update")
        self.assertEqual(out.splitlines()[2:4], [
            "  note: this build is a pre-release, so pre-releases count too",
            "  latest   0.5.0rc2 (pre-release, https://github.com/%s/releases/tag/v0.5.0rc2)"
            % update.REPO])
        # with the flag too, the flag is the reason given
        code, doc, _ = self.run_json("--update", "--rc", "--json")
        self.assertEqual((doc["latest"], doc["rc"]), ("0.5.0rc2", "flag"))
        code, out, _ = self.run_cli("--update", "--rc")
        self.assertNotIn("  note: this build", out)
        # the final, once out, is newer than every rc
        net = self.serve_list(release_json("v0.5.0"), rc2)
        code, doc, _ = self.run_json("--update", "--json")
        self.assertEqual((doc["latest"], doc["prerelease"], doc["rc"]), ("0.5.0", False, "current"))
        self.assertEqual(net.urls, [update.API_RELEASES])

    def test_a_pre_release_build_up_to_date(self):
        self.as_build("0.5.0rc2")
        self.serve_list(release_json("v0.5.0rc2", prerelease=True))
        code, out, _ = self.run_cli("--update")
        self.assertEqual((code, out.splitlines()[-2:]),
                         (0, ["  0.5.0rc2 is the latest release or pre-release", "OK"]))
        self.serve_list(release_json("v0.5.0rc1", prerelease=True))
        code, out, _ = self.run_cli("--update")
        self.assertIn("  0.5.0rc2 is ahead of the latest release or pre-release",
                      out.splitlines())

    def test_no_terminal_reports_and_says_how(self):
        self.patch(update, "apply_update", new=never_called)
        code, out, err = self.run_cli("--update")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("  9.9.9 is available; to install it: vcharon --update --yes",
                      out.splitlines())
        self.assert_untouched()

    def test_only_yes_means_yes(self):
        self.patch(update, "apply_update", new=never_called)
        for answer in ("", "n", "no", "N", "later", "Y ES", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                self.tty(answer)
                code, out, err = self.run_cli("--update")
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out.splitlines()[-1], "  cancelled; nothing was changed")
        self.assert_untouched()

    def test_confirmed_installs(self):
        for answer in ("y", "Y", "yes", " YES "):
            with self.subTest(answer=answer):
                with open(self.target, "wb") as f:
                    f.write(OLD)
                self.tty(answer)
                code, out, err = self.run_cli("--update")
                self.assertEqual((code, err), (0, ""))
                self.assertIn("  updated %s -> 9.9.9: %s" % (vcharon.VERSION, self.target),
                              out.splitlines())
                self.assertEqual(read(self.target), fake_binary())
        cli.input.assert_called_with("Update now? [y/N] ")

    def test_yes_skips_the_question(self):
        self.tty(AssertionError("--yes must not ask"))
        code, out, err = self.run_cli("--update", "--yes")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertTrue(any(line.startswith("  downloading ") for line in lines), lines)
        self.assertEqual(lines[-3:], [
            "  updated %s -> 9.9.9: %s" % (vcharon.VERSION, self.target),
            "  note: running watchers end with EXIT updated (exit 14); start them again",
            "OK"])
        self.assertEqual(read(self.target), fake_binary())

    def test_another_install_kind_gets_its_command(self):
        self.patch(install, "detect", return_value=install.Install("pipx"))
        self.patch(update, "apply_update", new=never_called)
        self.tty(AssertionError("nothing to ask: it can't install anyway"))
        command = 'pipx install --force "git+https://github.com/zhoufanscut/VCharon@v9.9.9"'
        code, doc, err = self.run_json("--update", "--json")
        self.assertEqual(code, 1)
        self.assertEqual((doc["ok"], doc["error"], doc["command"], doc["fix"]),
                         (False, "not_self_updatable", command, command))
        self.assertEqual(err.splitlines(), [
            "ERROR update: this vcharon was installed with pipx, so vcharon --update can't "
            "replace it (only the standalone binary)", "  fix: " + command])

    def test_a_network_failure_is_1(self):
        self.serve(Net({update.API_LATEST: update.UpdateError("network", "couldn't reach "
                                                              "api.github.com: no route",
                                                              update.NETWORK_FIX)}))
        code, doc, err = self.run_json("--update", "--json")
        self.assertEqual((code, doc["ok"], doc["error"], doc["fix"]),
                         (1, False, "network", update.NETWORK_FIX))
        self.assertEqual(sorted(doc), ["current", "error", "fix", "install", "message", "ok",
                                       "path"])
        self.assertEqual(err.splitlines(), ["ERROR update: couldn't reach api.github.com: no "
                                            "route", "  fix: " + update.NETWORK_FIX])

    def test_a_failed_install_is_1(self):
        # the failure's kind passes through unchanged; the exit code doesn't depend on it
        def fail(*args, **kwargs):
            raise update.UpdateError("checksum_mismatch", "it failed: checksum_mismatch")

        self.patch(update, "apply_update", new=fail)
        code, doc, err = self.run_json("--update", "--yes", "--json")
        self.assertEqual((code, doc["error"]), (1, "checksum_mismatch"))
        self.assertTrue(err.startswith("ERROR update: it failed: "), err)
        self.assertIn("\n  fix: ", err)

    def test_a_bug_still_gives_one_object(self):
        def boom(*args, **kwargs):
            raise RuntimeError("something nobody predicted")

        self.patch(update, "apply_update", new=boom)
        code, doc, _ = self.run_json("--update", "--yes", "--json")
        self.assertEqual((code, doc["ok"], doc["error"]), (1, False, "failed"))
        self.assertIn("RuntimeError", doc["message"])
        # the traceback is in the log, never on stdout
        with open(os.path.join(self.vcharon_home, "logs", "vcharon.log"),
                  encoding="utf-8") as f:
            self.assertIn("Traceback", f.read())
        self.patch(update, "latest_release", new=boom)
        code, doc, _ = self.run_json("--update", "--json")
        self.assertEqual((code, doc["error"]), (1, "failed"))

    def test_impossible_updates_refuse_before_the_question(self):
        self.tty(AssertionError("must not ask about an impossible update"))
        self.patch(update, "platform_asset", return_value=None)
        self.patch(update, "apply_update", new=never_called)
        code, _, err = self.run_cli("--update")
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[1], "  fix: " + update.PIPX_FIX)
        self.assert_untouched()

    BASE_KEYS = ("changed", "confirmed", "current", "install", "latest", "ok", "path",
                 "prerelease", "rc", "tag", "update_available", "url")

    def test_end_to_end(self):
        code, doc, err = self.run_json("--update", "--yes", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sorted(doc), sorted(self.BASE_KEYS + ("installed", "previous",
                                                               "skills", "verified")))
        self.assertEqual({k: doc[k] for k in ("confirmed", "changed", "installed", "previous",
                                              "verified", "path")},
                         {"confirmed": True, "changed": True, "installed": "9.9.9",
                          "previous": vcharon.VERSION, "verified": True, "path": self.target})
        # no skill installed (the sandbox's home): nothing to rewrite
        self.assertEqual(doc["skills"], {"paths": [], "ok": True, "fix": None})
        self.assertEqual(read(self.target), fake_binary())
        self.assertEqual(self.listing(), ["vcharon"])

    def skill_release(self, rc=0, err=""):
        """A user home with vcharon's skill copy for Claude Code (an older text) and the user's
        own file where Codex looks; the release's stand-in binary logs each other run's
        arguments, prints err and exits rc."""
        user = os.path.join(self.tmp, "user")
        patcher = mock.patch.dict(os.environ, HOME=user, USERPROFILE=user)
        patcher.start()
        self.addCleanup(patcher.stop)
        for agent, text in (("claude", "%s\nolder text\n" % skill.MARKER),
                            ("codex", "my own notes\n")):
            os.makedirs(os.path.dirname(skill.path(agent)))
            with open(skill.path(agent), "w", encoding="utf-8", newline="") as f:
                f.write(text)
        self.ran = os.path.join(self.tmp, "ran.json")
        binary = ("import json, sys\nif sys.argv[1:] == ['--version']:\n"
                  "    print('9.9.9')\n    sys.exit(0)\n"
                  "open(%r, 'w').write(json.dumps(sys.argv[1:]))\n"
                  "sys.stderr.write(%r)\nsys.exit(%d)\n" % (self.ran, err, rc)).encode()
        tarball = make_tarball(os.path.join(self.tmp, "skill.tar.gz"), binary)
        self.serve(Net({update.API_LATEST: json.dumps(release_json()), DOWNLOAD: read(tarball),
                        SUMS: "%s  %s\n" % (sha256(tarball), TARBALL)}))
        real = update.skill_argv
        self.patch(update, "skill_argv",
                   new=lambda binary, agents: [sys.executable] + real(binary, agents))

    def test_the_new_binary_rewrites_vcharons_skill_copies(self):
        self.skill_release()
        code, out, err = self.run_cli("--update", "--yes")
        self.assertEqual((code, err), (0, ""))
        # the installed binary, for vcharon's copy alone: the user's own file isn't named
        with open(self.ran, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.read()), ["skill", "install", "--claude"])
        self.assertIn("  skill rewritten by 9.9.9: %s" % skill.path("claude"), out.splitlines())
        code, doc, err = self.run_json("--update", "--yes", "--force", "--json")
        self.assertEqual((code, doc["skills"]),
                         (0, {"paths": [skill.path("claude")], "ok": True, "fix": None}))

    def test_a_failed_rewrite_is_a_note(self):
        self.skill_release(rc=1, err="ERROR refused: no\n")
        code, out, err = self.run_cli("--update", "--yes")
        # the update is done: exit 0, with the command to run
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(read(self.target)[:6], b"import")
        fix = platform.runnable("vcharon skill install --claude")
        self.assertIn("  note: the skill wasn't rewritten for 9.9.9 (it exited 1: ERROR refused: "
                      "no); run: %s" % fix, out.splitlines())
        self.assertEqual(out.splitlines()[-1], "OK")
        code, doc, err = self.run_json("--update", "--yes", "--force", "--json")
        self.assertEqual((code, doc["ok"], doc["skills"]),
                         (0, True, {"paths": [skill.path("claude")], "ok": False, "fix": fix}))

    def test_force_reinstalls_the_same_version_and_still_asks(self):
        version = vcharon.VERSION
        tarball = make_tarball(os.path.join(self.tmp, "same.tar.gz"), fake_binary(version))
        self.serve(Net({update.API_LATEST: json.dumps(release_json(tag="v" + version)),
                        DOWNLOAD: read(tarball),
                        SUMS: "%s  %s\n" % (sha256(tarball), TARBALL)}))
        self.tty("n")
        code, out, _ = self.run_cli("--update", "--force")
        self.assertEqual((code, out.splitlines()[-1]), (0, "  cancelled; nothing was changed"))
        self.assert_untouched()
        # no terminal: the line that says how keeps the --force
        self.patch(cli, "_stdin_is_terminal", return_value=False)
        code, out, _ = self.run_cli("--update", "--force")
        self.assertEqual((code, out.splitlines()[-1]),
                         (0, "  %s can be reinstalled: vcharon --update --force --yes" % version))
        self.assert_untouched()
        code, doc, _ = self.run_json("--update", "--force", "--yes", "--json")
        self.assertEqual((code, doc["changed"]), (0, True))
        self.assertEqual(read(self.target), fake_binary(version))

    def test_force_installs_an_older_release_too(self):
        # this build is ahead of the latest release: --force goes back to it
        tarball = make_tarball(os.path.join(self.tmp, "old.tar.gz"), fake_binary("0.0.1"))
        self.serve(Net({update.API_LATEST: json.dumps(release_json(tag="v0.0.1")),
                        DOWNLOAD: read(tarball),
                        SUMS: "%s  %s\n" % (sha256(tarball), TARBALL)}))
        code, out, _ = self.run_cli("--update", "--force")
        self.assertEqual((code, out.splitlines()[-1]),
                         (0, "  0.0.1 can be installed: vcharon --update --force --yes"))
        code, doc, _ = self.run_json("--update", "--force", "--yes", "--json")
        self.assertEqual((code, doc["changed"], doc["installed"]), (0, True, "0.0.1"))

    def test_it_isnt_a_verb(self):
        code, _, err = self.run_cli("update")
        self.assertEqual(code, 3)
        self.assertTrue(err.startswith("ERROR config: argument <command>: invalid choice: "
                                       "'update'"), err)

    def test_the_guide_never_tells_an_agent_to_run_it(self):
        from vcharon import guide
        for topic in guide.TOPICS:
            text = guide.text(topic)
            for line in text.splitlines():
                if "--update" in line:
                    with self.subTest(topic=topic, line=line):
                        self.assertRegex(line, r"(?i)never run `vcharon --update` yourself|"
                                         r"ask your user")

    def test_it_is_in_the_help(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("--update", out)
        self.assertIn("14 vcharon was updated (start it again)", out)


class FlagRefusalTest(FakeSshCase):
    """--update runs alone; --yes, --force, --json and --rc before a verb mean something only with
    it: each other way is a usage error (3) with a fix line, never ignored."""

    def setUp(self):
        FakeSshCase.setUp(self)
        patcher = mock.patch.object(update, "_open", side_effect=AssertionError("network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual((code, out), (3, ""), err)
        lines = err.splitlines()
        self.assertTrue(lines[0].startswith("ERROR config: "), err)
        self.assertTrue(lines[1].startswith("  fix: "), err)
        return lines

    def test_update_with_anything_else(self):
        for argv, rest in ((["--update", "doctor"], "doctor"),
                           (["--update", "list", "--local"], "list --local"),
                           (["--update", "--yes", "read", "c"], "read c"),
                           (["--update", "--version"], "--version")):
            with self.subTest(argv=argv):
                self.assertEqual(self.refused(*argv), [
                    "ERROR config: --update runs on its own, with only --yes, --force, --json "
                    "and --rc: not with " + rest,
                    "  fix: run vcharon --update by itself; then the rest"])

    def test_after_a_verb_its_unknown(self):
        lines = self.refused("doctor", "--update")
        self.assertEqual(lines[0], "ERROR config: unrecognized arguments: --update")

    def test_its_options_without_it(self):
        for argv, want in ((["--yes"], "--yes goes only with --update"),
                           (["--force"], "--force goes only with --update"),
                           (["--json"], "--json goes only with --update"),
                           (["--rc"], "--rc goes only with --update"),
                           (["--rc", "doctor"], "--rc goes only with --update"),
                           (["--yes", "doctor"], "--yes goes only with --update"),
                           (["--force", "whoami"], "--force goes only with --update"),
                           (["--json", "doctor"], "--json goes after the verb: vcharon doctor "
                                                  "... --json")):
            with self.subTest(argv=argv):
                self.assertEqual(self.refused(*argv)[0], "ERROR config: " + want)

    def test_the_legitimate_ones_get_past_the_check(self):
        # the network is a test failure here: each gets as far as asking GitHub
        for argv in (["--update"], ["--update", "--yes", "--json"], ["--update", "--force"],
                     ["--json", "--update"], ["--update", "--rc", "--yes", "--force", "--json"],
                     ["--rc", "--update"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1, err)
                self.assertIn("AssertionError: network", err)

    def test_help_with_it_is_help(self):
        code, out, _ = self.run_cli("--update", "--help")
        self.assertEqual(code, 0)
        self.assertIn("usage: vcharon", out)


class AgentFixTest(unittest.TestCase):
    """An agent never updates: the newer-format refusal's fix is advice for its user, and
    runnable() never turns `vcharon --update` into another install's spelling."""

    def test_runnable_leaves_it(self):
        for command in ("/home/u/.venv/bin/python -P -m vcharon", "python3 -P -m vcharon",
                        "/opt/vcharon/vcharon", "vcharon"):
            with self.subTest(command=command):
                self.assertEqual(platform.runnable(charter.UPDATE_HINT, command=command),
                                 charter.UPDATE_HINT)
                self.assertEqual(platform.runnable("run vcharon --update by itself; then "
                                                   "vcharon doctor", command=command),
                                 "run vcharon --update by itself; then %s doctor" % command)


if __name__ == "__main__":
    unittest.main()
