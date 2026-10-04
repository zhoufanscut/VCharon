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
# install.sh's MAX_BINARY, as install.ps1 and vcharon --update cap the binary
MAX_BINARY = 200_000_000


def fake_binary(version="9.9.9"):
    """A vcharon that only answers --version."""
    return ('#!/bin/sh\nif [ "$1" = --version ]; then echo %s; exit 0; fi\nexit 3\n'
            % version).encode()


class _Zeros(io.RawIOBase):
    """A file of zero bytes that is never stored: a big archive member costs no memory."""

    def readable(self):
        return True

    def readinto(self, b):
        b[:] = bytes(len(b))
        return len(b)


def tarball(members):
    """A .tar.gz of {name: bytes}, each executable; a str value makes a symlink to it, an int
    a file of that many zero bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=1) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            if isinstance(data, str):
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tar.addfile(info)
                continue
            info.mode = 0o755
            if isinstance(data, int):
                info.size = data
                tar.addfile(info, _Zeros())
                continue
            info.size = len(data)
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
        # found on this PATH: the child's may be cut down. A session of its own: sleeps()
        # counts only the processes it started, never another test's or program's.
        with subprocess.Popen([shutil.which(self.shell), INSTALL_SH],
                              env=env or self.env(path_has_bin), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True) as child:
            self.session = child.pid
            try:
                out, err = child.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise
        return child.returncode, out.decode(), err.decode()

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
        # the next steps end it, skill install among them: agents find vcharon through it;
        # one blank line before them
        self.assertEqual(out.splitlines()[-6:], [
            "",
            "Next:",
            "  vcharon --version        check that it runs",
            "  vcharon skill install    so Claude Code and Codex find vcharon: a skill that",
            "                           points them at vcharon guide",
            "  vcharon guide            the agent guide"])
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

    def publish(self, archive):
        """Serve this archive, with its right checksum, in place of release()'s."""
        self.server.files["/download/%s/%s" % (TAG, ASSET)] = archive
        self.server.files["/download/%s/%s.sha256" % (TAG, ASSET)] = (
            "%s  %s\n" % (hashlib.sha256(archive).hexdigest(), ASSET)).encode()

    def test_a_binary_over_200_mb_installs_nothing(self):
        # zeros compress to about 0.2 MB: the cap is on what comes out, not the download
        self.release()
        self.publish(tarball({"vcharon": MAX_BINARY + 1, "LICENSE": b"MIT\n"}))
        # the error reads the same after an unbounded write, so a head(1) that logs its
        # arguments shows the read itself stops one byte over the cap
        log = os.path.join(self.shims, "head.log")
        shim = os.path.join(self.shims, "head")
        with open(shim, "wb") as f:
            f.write(b'#!/bin/sh\necho "$*" >> "%s"\nexec "%s" "$@"\n'
                    % (log.encode(), shutil.which("head").encode()))
        os.chmod(shim, 0o755)
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("error: the vcharon in %s is over %d bytes; nothing was installed"
                      % (ASSET, MAX_BINARY), err)
        self.assert_nothing_installed()
        with open(log, encoding="utf-8") as f:
            self.assertIn("-c %d" % (MAX_BINARY + 1), f.read().splitlines())

    def test_an_archive_without_vcharon_installs_nothing(self):
        # sh has no pipefail: the member is looked for before the capped extraction
        self.release()
        self.publish(tarball({"bin/vcharon": fake_binary(), "LICENSE": b"MIT\n"}))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("error: %s holds no vcharon" % ASSET, err)
        self.assert_nothing_installed()

    def tools_path(self, without=()):
        """A PATH of the tools install.sh uses (timeout(1) among them), less these names."""
        tools = os.path.join(self.shims, "some-tools")
        os.mkdir(tools)
        for name in ("curl", "tar", "gzip", "sha256sum", "shasum", "openssl", "awk", "sed",
                     "grep", "head", "wc", "tr", "mktemp", "rm", "mkdir", "cp", "chmod", "mv",
                     "cat", "sleep", "timeout"):
            found = shutil.which(name)
            if found and name not in without:
                os.symlink(found, os.path.join(tools, name))
        for name in without:
            self.assertFalse(os.path.exists(os.path.join(tools, name)))
        return self.shims + os.pathsep + tools

    def test_a_published_checksum_with_no_tool_to_check_it_installs_nothing(self):
        # never "not checked" when the release has one: only a 404 goes on unchecked
        self.release()
        env = dict(self.env(), PATH=self.tools_path(("sha256sum", "shasum", "openssl")))
        code, _, err = self.install(env=env)
        self.assertEqual(code, 1)
        self.assertIn("error: no sha256sum, shasum or openssl here to check %s.sha256;"
                      " nothing was installed." % ASSET, err)
        self.assertIn("\n  pipx install git+https://github.com/zhoufanscut/VCharon\n", err)
        self.assert_nothing_installed()

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_openssl_checks_the_checksum_when_nothing_else_can(self):
        # the mismatch first: it must find nothing installed
        for sums, code in (("wrong", 1), ("right", 0)):
            with self.subTest(sums=sums):
                shutil.rmtree(os.path.join(self.shims, "some-tools"), True)
                self.release(sums=sums)
                env = dict(self.env(), PATH=self.tools_path(("sha256sum", "shasum")))
                got, out, err = self.install(env=env)
                self.assertEqual(got, code, out + err)
                if code:
                    self.assertIn("error: checksum mismatch for %s" % ASSET, err)
                    self.assert_nothing_installed()
                else:
                    self.assertEqual(err, "")
                    self.assertTrue(os.path.isfile(self.dest))

    def test_a_link_named_vcharon_installs_nothing(self):
        # tar -O writes a member's bytes only: a symlink gives an empty file, which won't run
        self.release()
        self.publish(tarball({"vcharon": "/bin/sh", "LICENSE": b"MIT\n"}))
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
        return self.tools_path(("timeout",))

    def sleeps(self, seconds):
        """The running `sleep <seconds>` processes the last install() started (Linux /proc
        only): those in its session, which outlives the installer itself."""
        found = []
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        argv = f.read().split(b"\0")
                    with open("/proc/%s/stat" % pid, "rb") as f:
                        stat_line = f.read()
                except OSError:
                    continue
                # the fields after the command name, which is in parentheses and may hold
                # spaces or parentheses itself; the session id is the stat's 6th field
                fields = stat_line[stat_line.rfind(b")") + 1:].split()
                if len(fields) < 4 or int(fields[3]) != self.session:
                    continue
                if argv[:2] == [b"sleep", str(seconds).encode()] or \
                        argv[:2] == [shutil.which("sleep").encode(), str(seconds).encode()]:
                    found.append(pid)
        return found

    def other_sleeper(self, seconds):
        """A `sleep <seconds>` loop outside the installer, as any program on the machine
        may run, stopped when the test ends."""
        loop = subprocess.Popen(["sh", "-c", "while :; do sleep %d; done" % seconds],
                                start_new_session=True)

        def stop():
            os.killpg(loop.pid, signal.SIGKILL)
            loop.wait()
        self.addCleanup(stop)
        # its first sleep running: the count below would see it without the session filter.
        # Found by parent pid in /proc/<pid>/stat (the 4th field, 2nd after the name), which
        # every Linux kernel has, unlike /proc/<pid>/task/<pid>/children.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open("/proc/%s/stat" % pid, "rb") as f:
                        stat_line = f.read()
                except OSError:
                    continue
                fields = stat_line[stat_line.rfind(b")") + 1:].split()
                if len(fields) >= 2 and int(fields[1]) == loop.pid:
                    return
            time.sleep(0.05)
        self.fail("the unrelated sleep loop never started its sleep")

    @unittest.skipUnless(os.path.isdir("/proc"), "counts processes through /proc")
    def test_without_timeout_a_quick_binary_leaves_no_sleeper(self):
        self.release()
        env = dict(self.env(), PATH=self.no_timeout_path(), VCHARON_INSTALL_VERSION_TIMEOUT="47")
        self.other_sleeper(47)
        code, out, err = self.install(env=env)
        self.assertEqual((code, err), (0, ""), out)
        time.sleep(0.3)
        self.assertEqual(self.sleeps(47), [])

    @unittest.skipUnless(os.path.isdir("/proc"), "counts processes through /proc")
    def test_without_timeout_a_hung_binary_is_stopped(self):
        # one that ignores TERM too: KILL after the grace
        hung = (b"#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n")
        self.release(binary=hung)
        # a timeout of 2, not 1: the hung binary's own `sleep 1`s aren't the watcher's
        env = dict(self.env(), PATH=self.no_timeout_path(), VCHARON_INSTALL_VERSION_TIMEOUT="2",
                   VCHARON_INSTALL_KILL_GRACE="1")
        self.other_sleeper(2)
        started = time.monotonic()
        code, _, err = self.install(env=env)
        self.assertEqual(code, 1)
        self.assertIn("the downloaded binary didn't run here", err)
        self.assertLess(time.monotonic() - started, 30)
        time.sleep(1.5)
        self.assertEqual(self.sleeps(2), [])
        self.assert_nothing_installed()

    def test_a_signal_cleans_up(self):
        # INT, TERM and HUP exit through the EXIT trap: no temp folder left
        for signum, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)):
            with self.subTest(signal=signum):
                self.release()
                # the download stalls long enough for the signal to land during it
                self.server.files["/download/%s/%s" % (TAG, ASSET)] = 1.0
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


def _sh_is_dash():
    sh, dash = shutil.which("sh"), shutil.which("dash")
    return bool(sh and dash) and os.path.realpath(sh) == os.path.realpath(dash)


@unittest.skipUnless(shutil.which("dash"), "needs dash")
@unittest.skipIf(_sh_is_dash(), "sh is dash: InstallShTest already ran under it")
class InstallDashTest(InstallShTest):
    """The same under dash, Debian's sh: no bash-only syntax, and its trap rules."""

    shell = "dash"


if __name__ == "__main__":
    unittest.main()
