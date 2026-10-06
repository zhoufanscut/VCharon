"""vcharon --update: replace this binary with the latest GitHub release (DESIGN, "Self-update");
client. Only that command imports this module.

The only network call vcharon makes besides ssh, and only when --update is given: there is no
version check at start-up. Standard library only (urllib, tarfile, zipfile, hashlib): a
dependency would go into the binary this replaces.

What an update means depends on how vcharon was installed (install.detect): a standalone
binary is replaced in place (apply_update); pipx, uv, pip and a checkout belong to another
tool, so the command that updates them is printed and nothing is changed.

The order of the swap is the point:

  1. download the archive into a temp folder inside the binary's own folder, so the last
     rename stays on one filesystem (the system temp folder is often another mount: EXDEV);
  2. check the release's .sha256 (a mismatch fails; a release without one warns and goes on,
     as the installers do);
  3. extract only the binary, to a path of our choosing, never extractall: an archive member
     with ../ or an absolute path has nowhere to go;
  4. run the new binary's --version and require the release's version: a Linux binary built
     against a newer glibc, a cut-short download, a wrong asset all fail here, while the old
     binary is still in place;
  5. replace. POSIX: os.replace over the running binary, which keeps running from its own
     inode. Windows: a running .exe can't be replaced or deleted, but it can be renamed, so
     it becomes <name>.old-<unix time> and the new one moves in; a failed move renames the old
     one back. Every later start deletes the .old-* copies it can (install.sweep_old).

After the swap the running process imports nothing more: a one-file binary reads its code from
its own file by path, which is the new one now (DESIGN, "Running watchers"). So the skill copies
vcharon wrote are rewritten by the new binary, run as a child (refresh_skills): this process is
the old version, and its skill text is the old one."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import http.client
import json
import os
import platform as pyplatform
import re
import shutil
import ssl
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

from . import VERSION, fsops, install, platform

REPO = install.REPO
API_ROOT = "https://api.github.com/"
# read in this order; the first one set is sent
TOKEN_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
API_LATEST = "%srepos/%s/releases/latest" % (API_ROOT, REPO)
RELEASES_URL = install.RELEASES_URL

# Per socket operation, not per transfer: a stalled read is the guard, not a slow download.
META_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 120
# The new binary's --version: a one-file binary unpacks itself first, seconds on a cold cache.
SMOKE_TIMEOUT = 90
# The most the binary in an archive may hold; a bigger one is refused, never written.
MAX_BINARY = 200 * 1000 * 1000
# Windows' rename of the running binary, and the move of the new one: antivirus often holds a
# new file for a moment, so a few tries over about two seconds.
RENAME_TRIES = 5
RENAME_WAIT = 0.5
# temp folders in the binary's folder, and how old one must be before a later run removes it
WORK_PREFIX = ".vcharon-update-"
STALE_AFTER = 3600.0

USER_AGENT = "vcharon/%s (+https://github.com/%s)" % (VERSION, REPO)

# the fix lines, by kind of failure
RETRY_FIX = "nothing was changed; try again later, or download it from %s" % RELEASES_URL
NETWORK_FIX = "check this machine's network, then try again"
RATE_FIX = "wait a few minutes, or set GITHUB_TOKEN, then try again"
# a rejected token fails every try the same way until it is changed
BAD_TOKEN_FIX = "unset or renew %s, then try again"
PIPX_FIX = "install with pipx instead: pipx install %s" % install.GIT_SPEC
# a release whose asset is wrong: trying again gets the same asset
BROKEN_FIX = ("nothing was changed; the release's download looks broken: see %s, or %s"
              % (RELEASES_URL, PIPX_FIX))
# the downloaded binary didn't run here: on Linux most often a glibc older than the
# release's build machine's
SMOKE_FIX = "nothing was changed; this machine can't run the release's binary: " + PIPX_FIX
SMOKE_FIX_LINUX = ("nothing was changed; this Linux may be older than the binary needs (its "
                   "glibc): " + PIPX_FIX)


class UpdateError(Exception):
    """What stopped an update. kind: the reason, --json's "error"; fix: the line that says
    what to do. Every kind exits 1 (DESIGN, "Self-update")."""

    def __init__(self, kind, message, fix=RETRY_FIX):
        Exception.__init__(self, message)
        self.kind = kind
        self.message = message
        self.fix = fix


# --- versions ---

# <numbers>[<pre-release label><n>], after a leading v; what follows (+build, a -suffix that
# isn't a label) is ignored
_VERSION = re.compile(r"\A[vV]?(\d+(?:\.\d+)*)(?:[-._]?([A-Za-z]+)[-._]?(\d*))?")
# pre-release labels, in order; an unknown label ranks lowest, below every final release
_PRE = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "pre": 3, "preview": 3,
        "rc": 3}
# post-release labels: they sort above the final release (0.1.0.post1 is after 0.1.0)
_POST = ("post", "rev", "r")


def parse_version(text):
    """"v0.3.1" -> ((0, 3, 1), (1,)); "0.1.0rc2" -> ((0, 1, 0), (0, 3, 2)): the numbers, then
    a pre-release's (0, rank, n), which sorts below a final release's (1,), and a post-release's
    (2, n), above it ("0.1.0.post1" -> ((0, 1, 0), (2, 1))). None when nothing numeric is
    there. Lenient, since a tag is typed by a person: a leading v, a +build tail and a short 0.4
    are taken. A release candidate is tagged before the final, so 0.1.0rc1 < 0.1.0rc2 < 0.1.0
    must each sort as newer than the one before (--update itself never offers a pre-release:
    releases/latest leaves them out)."""
    if not isinstance(text, str):
        return None
    m = _VERSION.match(text.strip().split("+")[0])
    if m is None:
        return None
    numbers = tuple(int(n) for n in m.group(1).split("."))
    label = (m.group(2) or "").lower()
    if not label:
        return numbers, (1,)
    if label in _POST:
        return numbers, (2, int(m.group(3) or 0))
    return numbers, (0, _PRE.get(label, -1), int(m.group(3) or 0))


def compare(a, b):
    """-1, 0 or 1 as version a is older than, the same as or newer than b; None when either
    doesn't parse. 0.4 is 0.4.0."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    width = max(len(pa[0]), len(pb[0]))
    ka = (pa[0] + (0,) * (width - len(pa[0])), pa[1])
    kb = (pb[0] + (0,) * (width - len(pb[0])), pb[1])
    return (ka > kb) - (ka < kb)


def is_newer(candidate, current):
    """Whether the tag candidate is a later version than current. A tag that doesn't parse is
    never newer: claiming an update that isn't there would swap the binary for nothing."""
    return compare(candidate, current) == 1


# --- the platform's release asset ---

def platform_asset():
    """This machine's asset name, or None when no binary is published for it: linux-x64,
    darwin-arm64 and win-x64, as release.yml builds them and install.sh and install.ps1 pick
    them. An Intel Mac and Linux arm64 install with pipx."""
    system = pyplatform.system()
    machine = pyplatform.machine().lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "vcharon-linux-x64"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "vcharon-darwin-arm64"
    if system == "Windows" and machine in ("amd64", "x86_64"):
        return "vcharon-win-x64"
    return None


def archive_name(base):
    """The archive the update downloads: Windows' is a .zip holding vcharon.exe, the others'
    a .tar.gz holding vcharon."""
    return base + (".zip" if base.endswith("-win-x64") else ".tar.gz")


# --- GitHub releases ---

@dataclasses.dataclass(frozen=True)
class Release:
    """One GitHub release, as much as an update needs."""

    tag: str
    # the tag without its leading v: what the binary's --version prints
    version: str
    # html_url, for the notes
    url: str
    published_at: str
    # asset name -> browser_download_url
    assets: dict = dataclasses.field(default_factory=dict)


def latest_release(timeout=META_TIMEOUT):
    """The repo's latest published release, or UpdateError. releases/latest, not the first of
    releases: GitHub leaves out drafts and pre-releases."""
    data = _get_json(API_LATEST, timeout)
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise UpdateError("no_release", "the GitHub API returned a release with no tag",
                          "see %s" % RELEASES_URL)
    tag = tag.strip()
    # one leading v, as the release job strips it: a vv1.0 tag would otherwise disagree with
    # the version the binary prints
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    assets = {}
    for asset in data.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") and asset.get("browser_download_url"):
            assets[asset["name"]] = asset["browser_download_url"]
    return Release(tag=tag, version=version,
                   url=data.get("html_url") or "%s/tag/%s" % (RELEASES_URL, tag),
                   published_at=data.get("published_at") or "", assets=assets)


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drops Authorization on a redirect to another host, and follows no redirect off https.
    urllib sends the first request's headers on a redirect, and GitHub sends asset downloads
    to another host: a GITHUB_TOKEN must reach api.github.com only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return new
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            # _open checks only the first URL; None makes urllib raise the HTTPError
            return None
        if _host(newurl) != _host(req.full_url):
            for name in [k for k in new.headers if k.lower() == "authorization"]:
                del new.headers[name]
        return new


def _host(url):
    return urllib.parse.urlsplit(url).netloc.lower()


# What a read can raise besides OSError: http.client.IncompleteRead (a body cut short) is an
# HTTPException, which an `except OSError` lets through as a traceback.
_READ_ERRORS = (OSError, http.client.HTTPException)

# The OS's CA bundle, first found wins. A binary's OpenSSL looks for CAs at a path from the
# build machine, which a user's machine may not have; there is no certifi in the standard
# library, so the system's own bundle stands in.
_CA_BUNDLES = (
    "/etc/ssl/cert.pem",                    # macOS, Alpine, the BSDs
    "/etc/ssl/certs/ca-certificates.crt",   # Debian, Ubuntu, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)


def _ssl_context():
    """A verifying context that finds CAs: a system bundle only when OpenSSL's own default CA
    file is missing and SSL_CERT_FILE and SSL_CERT_DIR are unset. Windows reads its own
    certificate store."""
    ctx = ssl.create_default_context()
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ctx
    default = ssl.get_default_verify_paths().cafile
    if default and os.path.isfile(default):
        return ctx
    for bundle in _CA_BUNDLES:
        if os.path.isfile(bundle):
            with contextlib.suppress(OSError, ssl.SSLError):
                ctx.load_verify_locations(cafile=bundle)
                break
    return ctx


_opener = None


def _open(url, timeout):
    """url opened for reading, or UpdateError: the module's one network call (the tests
    replace it; three replace _opener under it)."""
    global _opener
    # https only: build_opener keeps urllib's file: and ftp: handlers, and asset URLs come
    # from the release's JSON
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise UpdateError("bad_url", "refusing to fetch a URL that isn't https: %s" % url)
    if _opener is None:
        _opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context()), _StripAuthOnRedirect())
    # A token only for the API, to get past its 60 calls an hour per address on a shared NAT;
    # never sent with an asset download (public, and on another host).
    token_var = None
    if url.startswith(API_ROOT):
        token_var = next((name for name in TOKEN_VARS if os.environ.get(name)), None)
    try:
        try:
            return _opener.open(_request(url, token_var), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 401 or not token_var:
                raise
            _close(e)
        # A stale or revoked token: the release is public, so the call works without it, and
        # every later try would fail on the same token
        try:
            return _opener.open(_request(url, None), timeout=timeout)
        except urllib.error.HTTPError as e:
            # 403/429: the limit a valid token gets past; a 401 here carried no token, so the
            # token isn't what was refused, and it stays http_error
            if e.code in (403, 429):
                raise UpdateError("bad_token", "GitHub rejected %s (HTTP 401), and refused the "
                                  "request without it (HTTP %d)" % (token_var, e.code),
                                  BAD_TOKEN_FIX % token_var) from e
            raise
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise UpdateError("rate_limited", "GitHub refused the request for now (HTTP %d)"
                              % e.code, RATE_FIX) from e
        if e.code == 404:
            raise UpdateError("not_found", "%s returned HTTP 404: no published release yet?"
                              % url, "see %s" % RELEASES_URL) from e
        raise UpdateError("http_error", "%s returned HTTP %d" % (url, e.code)) from e
    except _READ_ERRORS as e:
        # URLError: DNS, TLS, a refused connection; a socket timeout is an OSError
        raise UpdateError("network", "couldn't reach %s: %s" % (_host(url) or url, e),
                          NETWORK_FIX) from e


def _request(url, token_var):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if url.startswith(API_ROOT):
        request.add_header("Accept", "application/vnd.github+json")
        if token_var:
            request.add_header("Authorization",
                               "Bearer %s" % os.environ[token_var].strip())
    return request


def _get_json(url, timeout):
    response = _open(url, timeout)
    try:
        raw = response.read()
    except _READ_ERRORS as e:
        raise UpdateError("network", "couldn't read %s: %s" % (_host(url) or url, e),
                          NETWORK_FIX) from e
    finally:
        _close(response)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise UpdateError("bad_response", "%s didn't return JSON: %s" % (url, e)) from e
    if not isinstance(data, dict):
        raise UpdateError("bad_response", "%s didn't return a JSON object" % url)
    return data


def _get_text(url, timeout):
    response = _open(url, timeout)
    try:
        return response.read().decode("utf-8", "replace")
    except _READ_ERRORS as e:
        raise UpdateError("network", "couldn't read %s: %s" % (_host(url) or url, e),
                          NETWORK_FIX) from e
    finally:
        _close(response)


def _download(url, dest, timeout):
    """url streamed to dest. A transfer that dies in the body is a network error, a failed
    write a write error: they point at different things to check."""
    response = _open(url, timeout)
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(response, f)
    except OSError as e:
        raise UpdateError("write_failed", "couldn't write %s: %s" % (dest, e),
                          "nothing was changed; check the free space in %s, then try again"
                          % os.path.dirname(dest)) from e
    except http.client.HTTPException as e:
        raise UpdateError("network", "the download from %s ended early: %s"
                          % (_host(url) or url, e), NETWORK_FIX) from e
    finally:
        _close(response)


def _close(response):
    # the bytes are read, or the read failed: a socket that won't close isn't the error
    with contextlib.suppress(Exception):
        response.close()


# --- the swap ---

@dataclasses.dataclass(frozen=True)
class UpdateResult:
    """What apply_update did. verified is False only when the release has no .sha256 (the new
    binary still had to pass its --version check)."""

    path: str
    version: str
    verified: bool


def not_writable_fix():
    """not_writable's fix: this OS's installer, which puts the binary where the user can
    write."""
    if platform.os_name() == "windows":
        return "run the installer again, in PowerShell: %s" % install.INSTALL_PS1
    return "run the installer again: %s" % install.INSTALL_SH


def preflight(inst, release=None):
    """Everything that makes an update impossible, checked before anyone is asked to confirm
    it; returns the archive's asset name, or raises UpdateError. Being asked "Update now?",
    answering yes, and only then hearing that this platform has no binary is a question that
    should never have been asked. apply_update runs it too."""
    if not inst.self_updatable or not inst.path:
        fix = (inst.command_for(release.tag) if release is not None
               else "see %s for the latest release, then update with %s"
               % (RELEASES_URL, inst.kind))
        raise UpdateError("not_self_updatable", "this vcharon was installed with %s, so "
                          "vcharon --update can't replace it (only the standalone binary)"
                          % inst.kind, fix)
    base = platform_asset()
    if base is None:
        raise UpdateError("unsupported_platform", "no binary is published for %s/%s"
                          % (pyplatform.system(), pyplatform.machine()), PIPX_FIX)
    folder = os.path.dirname(inst.path) or "."
    # For real, not os.access: on Windows that ignores ACLs, so the check could pass and the
    # update fail after the yes. The swap needs the folder, not the file, to be writable.
    try:
        probe = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=folder)
    except OSError as e:
        raise UpdateError("not_writable", "can't write in %s: %s" % (folder, e.strerror or e),
                          not_writable_fix()) from e
    with contextlib.suppress(OSError):
        os.rmdir(probe)
    archive = archive_name(base)
    if release is not None and not release.assets.get(archive):
        raise UpdateError("missing_asset", "release %s has no %s (it has: %s)"
                          % (release.tag, archive, ", ".join(sorted(release.assets))
                             or "nothing"), "see %s; try again later" % release.url)
    return archive


def apply_update(release, inst, timeout=DOWNLOAD_TIMEOUT, on_step=None, windows=None):
    """Downloads release and replaces inst.path with it; raises UpdateError, and on any
    failure the binary is left as it was. on_step(line): the progress lines (prose only).
    windows: the swap's kind (None: this OS's)."""
    def step(line):
        if on_step is not None:
            on_step(line)

    archive_asset = preflight(inst, release)
    url = release.assets[archive_asset]
    target = inst.path
    folder = os.path.dirname(target) or "."
    if windows is None:
        windows = platform.os_name() == "windows"
    sweep_stale(folder)
    try:
        work = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=folder)
    except OSError as e:
        raise UpdateError("not_writable", "couldn't make a temp folder in %s: %s"
                          % (folder, e.strerror or e), not_writable_fix()) from e
    try:
        archive = os.path.join(work, archive_asset)
        step("downloading %s (%s) ..." % (archive_asset, release.tag))
        _download(url, archive, timeout)
        verified = _verify_checksum(release, archive_asset, archive, timeout, step)
        step("extracting ...")
        if archive_asset.endswith(".zip"):
            staged = os.path.join(work, "vcharon.new.exe")
            _extract_zip(archive, staged)
        else:
            staged = os.path.join(work, "vcharon.new")
            _extract_tar(archive, staged)
            _apply_mode(target, staged)
        step("checking the new binary ...")
        _smoke_test(staged, release.version)
        if windows:
            swap_windows(staged, target)
        else:
            os.replace(staged, target)
        _sync_dir(folder)
    except OSError as e:
        raise UpdateError("install_failed", "couldn't install %s: %s" % (target, e),
                          "nothing was changed; check that no other program holds %s, then "
                          "try again" % target) from e
    finally:
        # never while no binary is at target (a swap stopped half way): the work folder may
        # hold the only copy left
        if os.path.lexists(target):
            shutil.rmtree(work, ignore_errors=True)
    return UpdateResult(path=target, version=release.version, verified=verified)


def _rename(src, dst, rename, sleep):
    """rename(src, dst), tried RENAME_TRIES times RENAME_WAIT apart: antivirus or the indexer
    often holds a new file on Windows for a moment. The last failure is raised."""
    for n in range(RENAME_TRIES):
        try:
            rename(src, dst)
            return
        except OSError:
            if n == RENAME_TRIES - 1:
                raise
            sleep(RENAME_WAIT)


def old_name(target, now=None, exists=None):
    """A name not taken for the running binary's copy: <target>.old-<unix time>, the time
    counted up while taken. Unique, since a process of the last update may still run the last
    copy, which then can't be deleted. now, exists: time.time and os.path.lexists (tests)."""
    exists = exists or os.path.lexists
    t = int((now or time.time)())
    while exists("%s%s%d" % (target, install.OLD_MARK, t)):
        t += 1
    return "%s%s%d" % (target, install.OLD_MARK, t)


def swap_windows(staged, target, rename=None, sleep=None, now=None, exists=None):
    """The Windows swap: the running target renamed to a unique .old-<time> name, then staged
    moved to target; if that move fails, the old one is renamed back. rename, sleep, now,
    exists: os.rename, time.sleep, time.time and os.path.lexists, the tests' to replace (they
    run it on every OS). Raises what stopped the swap (an OSError, a Ctrl-C) after the old one
    is back; when even the way back fails, UpdateError install_failed, whose fix names both
    paths."""
    rename = rename or os.rename
    sleep = sleep or time.sleep
    exists = exists or os.path.lexists
    old = old_name(target, now, exists)
    # set by the first rename itself, not after _rename returns: a Ctrl-C between the two would
    # leave a moved binary that nothing puts back. The old name was free before, so finding it
    # taken covers the few instructions between the rename and the flag.
    old_moved = False

    def move_old(src, dst):
        nonlocal old_moved
        rename(src, dst)
        old_moved = True

    try:
        _rename(target, old, move_old, sleep)
        _rename(staged, target, rename, sleep)
    except BaseException as stop:
        # any way out, a Ctrl-C too: no binary at target is the one outcome to avoid. Only
        # when target is empty: the new one may be in place already (stopped after its move)
        if (old_moved or exists(old)) and not exists(target):
            try:
                _rename(old, target, rename, sleep)
            except (OSError, KeyboardInterrupt) as back:
                raise UpdateError(
                    "install_failed", "the new binary couldn't be moved to %s (%s), and the old "
                    "one couldn't be put back (%s): it is %s now"
                    % (target, _why(stop), _why(back), old),
                    "rename %s back to %s" % (old, target)) from back
        raise
    # the old copy: deleted here if it can be (it can't while it runs), else at a later start
    with contextlib.suppress(OSError):
        os.remove(old)


def _why(error):
    """An error's own words, for a message: the OS's text, or what stopped it."""
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return str(error) or type(error).__name__


def sweep_stale(folder):
    """Removes the temp folders a killed run left in folder, once they are STALE_AFTER old;
    never a newer one, which may be another --update's download under way."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not name.startswith(WORK_PREFIX):
            continue
        path = os.path.join(folder, name)
        if now - touched_at(path) >= STALE_AFTER:
            shutil.rmtree(path, ignore_errors=True)


def touched_at(path):
    """When anything in path last changed: the newest mtime of the folder and its files. A
    folder's own mtime moves when an entry is made or removed, not when a file in it is
    written, so it alone would make a slow download look abandoned. time.time() ("new, leave
    it") when the folder can't be read."""
    newest = 0.0
    try:
        newest = os.stat(path).st_mtime
        for name in os.listdir(path):
            with contextlib.suppress(OSError):
                newest = max(newest, os.stat(os.path.join(path, name)).st_mtime)
    except OSError:
        return time.time()
    return newest


def _sync_dir(path):
    """fsync of the folder, so the rename itself survives a crash; best effort (not every
    filesystem or OS opens a folder for it)."""
    with contextlib.suppress(OSError, AttributeError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _verify_checksum(release, asset, archive, timeout, step):
    """The archive checked against the release's <asset>.sha256, one line `<hex>  <file>`,
    whose first word counts. A mismatch fails, and so does a file without a sha256 in it; a
    release without one warns and goes on (verified False)."""
    sums_url = release.assets.get(asset + ".sha256")
    if not sums_url:
        step("warning: this release publishes no checksum; not verified")
        return False
    step("verifying the checksum ...")
    expected = _get_text(sums_url, timeout).split()
    if not expected or not re.fullmatch(r"[0-9a-fA-F]{64}", expected[0]):
        # published but unusable: not the same as a release that has none
        raise UpdateError("bad_asset", "%s.sha256 holds no sha256 (%r); nothing was installed"
                          % (asset, " ".join(expected)[:80]), BROKEN_FIX)
    digest = hashlib.sha256()
    with open(archive, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual.lower() != expected[0].strip().lower():
        raise UpdateError("checksum_mismatch", "%s doesn't match its published sha256 "
                          "(expected %s, got %s); nothing was installed"
                          % (asset, expected[0].strip(), actual))
    return True


def _copy_out(source, dest):
    """source's bytes into dest, fsynced before the caller renames dest over the binary: a
    rename can reach the disk before the data does, and a crash would leave an empty binary.
    Best effort: some mounts refuse fsync."""
    with open(dest, "wb") as f:
        shutil.copyfileobj(source, f)
        f.flush()
        with contextlib.suppress(OSError):
            os.fsync(f.fileno())


def _extract_tar(archive, dest):
    """The archive's one `vcharon` member (a file, at most MAX_BINARY) into dest. Never
    extractall: the destination is ours, so a crafted archive has nowhere to write, and
    LICENSE, which the archive also holds, isn't written."""
    name = os.path.basename(archive)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            # at the top, as the zip's: a vcharon in a folder isn't it
            member = next((m for m in tar.getmembers()
                           if m.isfile() and m.name in ("vcharon", "./vcharon")), None)
            if member is None:
                raise UpdateError("bad_asset", "%s holds no vcharon binary at its top" % name,
                                  BROKEN_FIX)
            if member.size > MAX_BINARY:
                raise UpdateError("bad_asset", "%s's vcharon is %d bytes, more than %d"
                                  % (name, member.size, MAX_BINARY), BROKEN_FIX)
            source = tar.extractfile(member)
            if source is None:
                raise UpdateError("bad_asset", "couldn't read vcharon out of %s" % name, BROKEN_FIX)
            with source:
                _copy_out(source, dest)
    except (tarfile.TarError, EOFError, zlib.error) as e:
        raise UpdateError("bad_asset", "%s isn't a readable archive: %s" % (name, e),
                          BROKEN_FIX) from e


def _extract_zip(archive, dest):
    """The archive's entry named exactly vcharon.exe (no folder part) into dest, streamed with
    ZipFile.open. Refused: none, a folder or a link of that name, one over MAX_BINARY. Never
    extract or extractall, for the reason of _extract_tar."""
    name = os.path.basename(archive)
    try:
        with zipfile.ZipFile(archive) as zf:
            info = next((i for i in zf.infolist() if i.filename == "vcharon.exe"), None)
            if info is None:
                raise UpdateError("bad_asset", "%s holds no vcharon.exe at its top" % name,
                                  BROKEN_FIX)
            # a folder: its name ends in /, or the MS-DOS folder bit (a zip made on Windows
            # often has no Unix mode); a link or anything else: the file type in its Unix mode,
            # when it has one (Python's own writestr gives none)
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or info.external_attr & 0x10 or kind not in (0, stat.S_IFREG):
                raise UpdateError("bad_asset", "%s's vcharon.exe isn't a file" % name, BROKEN_FIX)
            if info.file_size > MAX_BINARY:
                raise UpdateError("bad_asset", "%s's vcharon.exe is %d bytes, more than %d"
                                  % (name, info.file_size, MAX_BINARY), BROKEN_FIX)
            with zf.open(info) as source:
                _copy_out(source, dest)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error,
            # a compression this Python can't read; an encrypted entry
            NotImplementedError, RuntimeError) as e:
        raise UpdateError("bad_asset", "%s isn't a readable archive: %s" % (name, e),
                          BROKEN_FIX) from e


def _apply_mode(target, staged):
    """The new file gets the old one's permissions, plus read and execute for its owner: an
    install kept private (0700) stays so."""
    mode = 0o755
    with contextlib.suppress(OSError):
        mode = stat.S_IMODE(os.stat(target).st_mode)
    os.chmod(staged, mode | stat.S_IXUSR | stat.S_IRUSR)


def child_env():
    """The environment for running the downloaded binary, PyInstaller's own variables
    removed. Here a one-file binary starts another one-file binary: LD_LIBRARY_PATH (macOS:
    DYLD_LIBRARY_PATH) points at this binary's unpacked libraries, with the user's own value
    saved in <name>_ORIG, and _MEIPASS2 / _PYI_* tell a bootloader where an archive is. Either
    would fail the check of a good release. The user's values come back; what the bootloader
    made up goes; and PYINSTALLER_RESET_ENVIRONMENT makes the new binary unpack its own copy
    (platform.child_env)."""
    env = dict(os.environ)
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "LIBPATH"):
        original = env.pop(name + "_ORIG", None)
        if original is not None:
            env[name] = original
        elif getattr(sys, "frozen", False):
            # no _ORIG in a frozen run: the bootloader set it from nothing
            env.pop(name, None)
    for name in [k for k in env if k.startswith(("_MEIPASS", "_PYI_"))]:
        del env[name]
    return platform.child_env(env)


def smoke_argv(binary):
    """How the downloaded binary is run for its --version (the tests run a Python script)."""
    return [binary, "--version"]


def skill_argv(binary, agents):
    """How the installed binary is run to rewrite agents' skill copies (the tests run a Python
    script)."""
    return [binary, "skill", "install"] + ["--" + agent for agent in agents]


def run_new(argv):
    """Runs the new binary (argv from smoke_argv or skill_argv) through fsops.run: SMOKE_TIMEOUT,
    child_env, and on POSIX a session of its own with fsops.TERM_WAIT's grace, so a timeout or
    a Ctrl-C ends the bootloader's whole group and it removes its unpack folder (DESIGN,
    "Self-update")."""
    return fsops.run(argv, SMOKE_TIMEOUT, new_session=True, env=child_env(),
                     term_wait=fsops.TERM_WAIT)


def refresh_skills(binary, agents):
    """Runs the installed binary's vcharon skill install for agents, the ones whose copy carries
    vcharon's marker (found before the swap), as the --version check runs it: this process is
    the old version and would write the old text. None when it worked, else why not. Never
    raises: the update is done either way, and the fix is to run that command by hand."""
    try:
        ran = run_new(skill_argv(binary, agents))
    except OSError as e:
        return "it wouldn't run: %s" % e
    if ran.rc is None:
        return "it didn't finish within %d s" % SMOKE_TIMEOUT
    if ran.rc != 0:
        detail = (fsops.child_text(ran.err) or fsops.child_text(ran.out)).strip().splitlines()
        return "it exited %d: %s" % (ran.rc, detail[0] if detail else "no output")
    return None


def _smoke_test(binary, expected):
    """Runs the downloaded binary's --version (through run_new) and requires the release's
    version back."""
    fix = SMOKE_FIX_LINUX if platform.os_name() == "linux" else SMOKE_FIX
    try:
        ran = run_new(smoke_argv(binary))
    except OSError as e:
        raise UpdateError("smoke_failed", "the downloaded binary wouldn't run (%s); nothing "
                          "was installed" % e, fix) from e
    out = fsops.child_text(ran.out)
    if ran.rc is None:
        raise UpdateError("smoke_failed", "the downloaded binary didn't answer --version "
                          "within %d s; nothing was installed" % SMOKE_TIMEOUT, fix)
    if ran.rc != 0:
        detail = (fsops.child_text(ran.err) or out).strip().splitlines()
        raise UpdateError("smoke_failed", "the downloaded binary exited %d on --version (%s); "
                          "nothing was installed"
                          % (ran.rc, detail[0] if detail else "no output"), fix)
    reported = out.strip()
    if reported != expected:
        raise UpdateError("version_mismatch", "the downloaded binary says %r, but the release "
                          "is %r; nothing was installed" % (reported, expected), BROKEN_FIX)
