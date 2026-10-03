"""install.sh, run by sh against a fake release on a local HTTP server: the asset it picks, the
checksum, the binary's own --version check, where it installs, the pipx answers."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest

from tests.util import TESTS_DIR

INSTALL_SH = os.path.join(os.path.dirname(TESTS_DIR), "install.sh")
TAG = "v9.9.9"
ASSET = "vcharon-linux-x64.tar.gz"


def fake_binary(version="9.9.9"):
    """A vcharon that only answers --version."""
    return ('#!/bin/sh\nif [ "$1" = --version ]; then echo %s; exit 0; fi\nexit 3\n'
            % version).encode()


def tarball(members):
    """A .tar.gz of {name: bytes}, each executable; a str value makes a symlink to it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            if isinstance(data, str):
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tar.addfile(info)
                continue
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Release(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.asked.append(self.path)
        body = self.server.files.get(self.path)
        if isinstance(body, int):
            self.send_error(body)
            return
        if isinstance(body, float):
            # a slow answer: the time to send a signal while curl waits
            time.sleep(body)
            body = None
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.name == "posix" and shutil.which("sh") and shutil.which("curl")
                     and shutil.which("tar"), "needs a POSIX sh, curl and tar")
class InstallShTest(unittest.TestCase):
    # the shell that runs install.sh, as `curl … | sh` does
    shell = "sh"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vcharon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Release)
        self.server.files = {}
        self.server.asked = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.root = "http://127.0.0.1:%d" % self.server.server_address[1]
        # uname answers as this test says: the installer's choice of asset, on any host
        self.shims = os.path.join(self.tmp, "shims")
        os.mkdir(self.shims)
        self.uname("Linux", "x86_64")

    def uname(self, system, machine):
        path = os.path.join(self.shims, "uname")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write('#!/bin/sh\ncase "$1" in -s) echo %s ;; -m) echo %s ;; *) echo %s ;; esac\n'
                    % (system, machine, system))
        os.chmod(path, 0o755)

    def release(self, binary=None, sums="right", asset=ASSET):
        archive = tarball({"vcharon": fake_binary() if binary is None else binary,
                           "LICENSE": b"MIT\n"})
        self.archive = archive
        files = self.server.files
        files["/latest"] = json.dumps({"tag_name": TAG, "name": TAG}).encode()
        files["/download/%s/%s" % (TAG, asset)] = archive
        digest = hashlib.sha256(archive).hexdigest()
        if sums == "right":
            files["/download/%s/%s.sha256" % (TAG, asset)] = (
                "%s  %s\n" % (digest, asset)).encode()
        elif sums == "wrong":
            files["/download/%s/%s.sha256" % (TAG, asset)] = (
                "%s  %s\n" % ("0" * 64, asset)).encode()
        elif sums == "junk":
            files["/download/%s/%s.sha256" % (TAG, asset)] = b"not a checksum\n"
        elif isinstance(sums, int):
            files["/download/%s/%s.sha256" % (TAG, asset)] = sums

    def env(self, path_has_bin=False):
        bin_dir = os.path.join(self.home, ".local", "bin")
        path = self.shims + os.pathsep + os.environ.get("PATH", os.defpath)
        if path_has_bin:
            path = bin_dir + os.pathsep + path
        return {"HOME": self.home, "PATH": path,
                "VCHARON_INSTALL_API_URL": self.root + "/latest",
                "VCHARON_INSTALL_DOWNLOAD_URL": self.root + "/download",
                "TMPDIR": self.tmp}

    def install(self, path_has_bin=False, env=None):
        # found on this PATH: the child's may be cut down
        ran = subprocess.run([shutil.which(self.shell), INSTALL_SH],
                             env=env or self.env(path_has_bin),
                             capture_output=True, timeout=60, check=False)
        return ran.returncode, ran.stdout.decode(), ran.stderr.decode()

    @property
    def dest(self):
        return os.path.join(self.home, ".local", "bin", "vcharon")

    def assert_nothing_installed(self):
        self.assertFalse(os.path.lexists(self.dest))
        # and no temp folder or staged copy left behind
        self.assertEqual(sorted(os.listdir(self.tmp)), ["home", "shims"])

    def test_installs_the_checked_binary(self):
        self.release()
        code, out, err = self.install()
        self.assertEqual((code, err), (0, ""), out)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), fake_binary())
        self.assertTrue(os.stat(self.dest).st_mode & stat.S_IXUSR)
        self.assertIn("Installed: %s (9.9.9)" % self.dest, out)
        self.assertIn("Checking the checksum ...", out)
        # ~/.local/bin isn't on this PATH: the line to add it
        self.assertIn("is not on your PATH", out)
        self.assertEqual(self.server.asked, ["/latest", "/download/%s/%s" % (TAG, ASSET),
                                             "/download/%s/%s.sha256" % (TAG, ASSET)])
        # only the binary came out of the archive, and nothing else is left
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["vcharon"])
        self.assertEqual(sorted(os.listdir(self.tmp)), ["home", "shims"])

    def test_replaces_an_older_one_and_knows_the_path(self):
        os.makedirs(os.path.dirname(self.dest))
        with open(self.dest, "wb") as f:
            f.write(fake_binary("0.0.1"))
        old_inode = os.stat(self.dest).st_ino
        self.release()
        code, out, _ = self.install(path_has_bin=True)
        self.assertEqual(code, 0, out)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), fake_binary())
        # renamed over it, not written into it: a running vcharon keeps its own file
        self.assertNotEqual(os.stat(self.dest).st_ino, old_inode)
        self.assertNotIn("is not on your PATH", out)
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["vcharon"])

    def test_a_checksum_mismatch_installs_nothing(self):
        self.release(sums="wrong")
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("error: checksum mismatch for %s" % ASSET, err)
        self.assert_nothing_installed()

    def test_a_checksum_file_without_a_sha256_installs_nothing(self):
        self.release(sums="junk")
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("holds no sha256", err)
        self.assert_nothing_installed()

    def test_no_checksum_published_warns_and_installs(self):
        # a 404: the release has none
        self.release(sums=None)
        code, _, err = self.install()
        self.assertEqual(code, 0)
        self.assertIn("warning: this release has no checksum file", err)
        self.assertTrue(os.path.isfile(self.dest))

    def test_a_checksum_that_wont_download_installs_nothing(self):
        # anything but a 404 is a failure, never "not checked"
        self.release(sums=500)
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("error: couldn't download %s.sha256 (HTTP 500); nothing was installed"
                      % ASSET, err)
        self.assert_nothing_installed()

    def test_a_link_named_vcharon_installs_nothing(self):
        # tar -O writes a member's bytes only: a symlink gives an empty file, which won't run
        archive = tarball({"vcharon": "/bin/sh", "LICENSE": b"MIT\n"})
        self.release()
        self.server.files["/download/%s/%s" % (TAG, ASSET)] = archive
        self.server.files["/download/%s/%s.sha256" % (TAG, ASSET)] = (
            "%s  %s\n" % (hashlib.sha256(archive).hexdigest(), ASSET)).encode()
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("the downloaded binary didn't run here", err)
        self.assert_nothing_installed()

    def test_an_override_must_be_https_or_this_machine(self):
        # a user part (an @) would make what comes before it look like the host
        for url in ("http://example.com/download", "http://127.0.0.1:1@evil.example/x",
                    "http://localhost:@evil.example/", "https://user@example.com/x",
                    "http://localhost.evil.example/x", "http://127.0.0.1:80x/",
                    "http://127.0.0.1#.evil.example/", "ftp://127.0.0.1/x"):
            with self.subTest(url=url):
                env = self.env()
                env["VCHARON_INSTALL_DOWNLOAD_URL"] = url
                code, _, err = self.install(env=env)
                self.assertEqual(code, 1)
                self.assertIn("an install URL must be https, or http to this machine: " + url,
                              err)
        self.assertEqual(self.server.asked, [])
        # the exact hosts, with or without a port, are taken
        self.release()
        for root in ("http://localhost:%d" % self.server.server_address[1], self.root):
            with self.subTest(root=root):
                env = self.env()
                env["VCHARON_INSTALL_API_URL"] = root + "/latest"
                env["VCHARON_INSTALL_DOWNLOAD_URL"] = root + "/download"
                code, out, _ = self.install(env=env)
                self.assertEqual(code, 0, out)

    def no_timeout_path(self):
        """A PATH of the tools install.sh uses, without timeout(1), as on macOS."""
        tools = os.path.join(self.shims, "tools")
        os.mkdir(tools)
        for name in ("curl", "tar", "gzip", "sha256sum", "shasum", "awk", "sed", "grep", "head",
                     "mktemp", "rm", "mkdir", "cp", "chmod", "mv", "cat", "sleep"):
            found = shutil.which(name)
            if found:
                os.symlink(found, os.path.join(tools, name))
        self.assertFalse(os.path.exists(os.path.join(tools, "timeout")))
        return self.shims + os.pathsep + tools

    def sleeps(self, seconds):
        """The running `sleep <seconds>` processes (Linux /proc only)."""
        found = []
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        argv = f.read().split(b"\0")
                except OSError:
                    continue
                if argv[:2] == [b"sleep", str(seconds).encode()] or \
                        argv[:2] == [shutil.which("sleep").encode(), str(seconds).encode()]:
                    found.append(pid)
        return found

    @unittest.skipUnless(os.path.isdir("/proc"), "counts processes through /proc")
    def test_without_timeout_a_quick_binary_leaves_no_sleeper(self):
        self.release()
        env = dict(self.env(), PATH=self.no_timeout_path(), VCHARON_INSTALL_VERSION_TIMEOUT="47")
        code, out, err = self.install(env=env)
        self.assertEqual((code, err), (0, ""), out)
        time.sleep(0.3)
        self.assertEqual(self.sleeps(47), [])

    @unittest.skipUnless(os.path.isdir("/proc"), "counts processes through /proc")
    def test_without_timeout_a_hung_binary_is_stopped(self):
        # one that ignores TERM too: KILL after the grace
        hung = (b"#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n")
        self.release(binary=hung)
        env = dict(self.env(), PATH=self.no_timeout_path(), VCHARON_INSTALL_VERSION_TIMEOUT="3")
        started = time.monotonic()
        code, _, err = self.install(env=env)
        self.assertEqual(code, 1)
        self.assertIn("the downloaded binary didn't run here", err)
        self.assertLess(time.monotonic() - started, 30)
        time.sleep(1.5)
        self.assertEqual(self.sleeps(3), [])
        self.assert_nothing_installed()

    def test_a_signal_cleans_up(self):
        # INT, TERM and HUP exit through the EXIT trap: no temp folder left
        for signum, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)):
            with self.subTest(signal=signum):
                self.release()
                self.server.files["/download/%s/%s" % (TAG, ASSET)] = 1.5
                child = subprocess.Popen([self.shell, INSTALL_SH], env=self.env(),
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while (not any(p.endswith(ASSET) for p in self.server.asked)
                           and time.monotonic() < deadline):
                        time.sleep(0.05)
                    time.sleep(0.2)
                    child.send_signal(signum)
                    child.communicate(timeout=30)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                self.assertEqual(child.returncode, code)
                self.assert_nothing_installed()
                self.server.asked.clear()

    def test_a_binary_that_wont_run_here_installs_nothing(self):
        self.release(binary=fake_binary("1.0.0"))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("the downloaded binary didn't run here", err)
        self.assertIn("pipx install git+https://github.com/zhoufanscut/VCharon", err)
        self.assert_nothing_installed()

    def test_a_mac_takes_the_darwin_arm64_asset(self):
        self.uname("Darwin", "arm64")
        self.release(asset="vcharon-darwin-arm64.tar.gz")
        code, out, _ = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn("/download/%s/vcharon-darwin-arm64.tar.gz" % TAG, self.server.asked)

    def test_an_intel_mac_and_linux_arm64_get_pipx(self):
        for system, machine in (("Darwin", "x86_64"), ("Linux", "aarch64")):
            with self.subTest(system=system, machine=machine):
                self.uname(system, machine)
                code, out, err = self.install()
                self.assertEqual((code, out), (1, ""))
                self.assertIn("install with pipx:\n  pipx install "
                              "git+https://github.com/zhoufanscut/VCharon\n", err)
        # refused before any download
        self.assertEqual(self.server.asked, [])
        self.assert_nothing_installed()

    def test_another_os_is_refused(self):
        self.uname("FreeBSD", "amd64")
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("unsupported OS: FreeBSD", err)


@unittest.skipUnless(shutil.which("dash"), "needs dash")
class InstallDashTest(InstallShTest):
    """The same under dash, Debian's sh: no bash-only syntax, and its trap rules."""

    shell = "dash"


if __name__ == "__main__":
    unittest.main()
