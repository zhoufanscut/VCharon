"""Packs a built binary into a release's three assets (DESIGN, "Releases"), and checks them.

    python tests/pack.py DIST_DIR ASSET OUT_DIR

ASSET is the platform's name: vcharon-linux-x64, vcharon-darwin-arm64 or vcharon-win-x64.
OUT_DIR gets, for Linux and macOS, ASSET (the bare binary), ASSET.tar.gz and
ASSET.tar.gz.sha256; for Windows, ASSET.exe, ASSET.zip and ASSET.zip.sha256. The archive holds
the binary and LICENSE at its top: vcharon --update, install.sh and install.ps1 take the member
named exactly vcharon (vcharon.exe), nothing in a folder. The .sha256 is one line,
`<hex>  <archive name>`, what sha256sum prints: they read its first word.

Python, not tar and zip: the same on all three runners (Windows' Git Bash may have no zip, and
macOS's tar adds ._ files for extended attributes)."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tarfile
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = ("vcharon-linux-x64", "vcharon-darwin-arm64", "vcharon-win-x64")


def tar_info(name, size, mode):
    # no owner, user or group of the build machine in the archive
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    info.uname = info.gname = ""
    return info


def pack_tar(binary, archive):
    with tarfile.open(archive, "w:gz") as tar:
        for path, name, mode in ((binary, "vcharon", 0o755),
                                 (os.path.join(REPO, "LICENSE"), "LICENSE", 0o644)):
            with open(path, "rb") as f:
                info = tar_info(name, os.fstat(f.fileno()).st_size, mode)
                info.mtime = int(os.fstat(f.fileno()).st_mtime)
                tar.addfile(info, f)


def pack_zip(binary, archive):
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, name, mode in ((binary, "vcharon.exe", 0o755),
                                 (os.path.join(REPO, "LICENSE"), "LICENSE", 0o644)):
            info = zipfile.ZipInfo.from_file(path, name)
            # a regular file's Unix mode, whatever the host: --update refuses any other type
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as f:
                zf.writestr(info, f.read())


def check_tar(archive, binary):
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = sorted(m.name for m in members)
        if names != ["LICENSE", "vcharon"]:
            sys.exit("pack: %s holds %s, not vcharon and LICENSE at its top" % (archive, names))
        member = tar.getmember("vcharon")
        if not member.isfile() or not member.mode & 0o100:
            sys.exit("pack: %s's vcharon isn't an executable file" % archive)
        with tar.extractfile(member) as f, open(binary, "rb") as g:
            if f.read() != g.read():
                sys.exit("pack: %s's vcharon isn't the binary" % archive)


def check_zip(archive, binary):
    with zipfile.ZipFile(archive) as zf:
        names = sorted(zf.namelist())
        if names != ["LICENSE", "vcharon.exe"]:
            sys.exit("pack: %s holds %s, not vcharon.exe and LICENSE at its top"
                     % (archive, names))
        info = zf.getinfo("vcharon.exe")
        if info.is_dir() or stat.S_IFMT(info.external_attr >> 16) != stat.S_IFREG:
            sys.exit("pack: %s's vcharon.exe isn't a file" % archive)
        with zf.open(info) as f, open(binary, "rb") as g:
            if f.read() != g.read():
                sys.exit("pack: %s's vcharon.exe isn't the binary" % archive)


def write_sums(archive):
    h = hashlib.sha256()
    with open(archive, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    # binary: LF on Windows too
    with open(archive + ".sha256", "wb") as f:
        f.write(("%s  %s\n" % (h.hexdigest(), os.path.basename(archive))).encode())


def main(argv):
    if len(argv) != 3 or argv[1] not in ASSETS:
        sys.exit("usage: python tests/pack.py DIST_DIR {%s} OUT_DIR" % ",".join(ASSETS))
    dist, asset, out = argv
    windows = asset.endswith("-win-x64")
    binary = os.path.join(dist, "vcharon.exe" if windows else "vcharon")
    if not os.path.isfile(binary):
        sys.exit("pack: no binary at %s" % binary)
    os.makedirs(out, exist_ok=True)
    bare = os.path.join(out, asset + (".exe" if windows else ""))
    shutil.copy2(binary, bare)
    archive = os.path.join(out, asset + (".zip" if windows else ".tar.gz"))
    if windows:
        pack_zip(binary, archive)
        check_zip(archive, binary)
    else:
        pack_tar(binary, archive)
        check_tar(archive, binary)
    write_sums(archive)
    for path in (bare, archive, archive + ".sha256"):
        print("%s  %d bytes" % (path, os.path.getsize(path)))


if __name__ == "__main__":
    main(sys.argv[1:])
