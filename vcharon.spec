# PyInstaller spec for the standalone vcharon binary: pyinstaller vcharon.spec (from the repo
# root, after pip install -e ".[build]"). Checked against PyInstaller 6.22.3, the version the
# build extra pins.
#
# One file, console, named vcharon (vcharon.exe on Windows). Two things a plain
# `pyinstaller --onefile` would miss (DESIGN, "Packaging"):
#
# - datas: the package's files on disk under sys._MEIPASS/vcharon/, the .py files too.
#   bundle._sources() reads the .py files from there to send to the server (the PYZ holds
#   only bytecode), and guide/*.md and skill/SKILL.md are read with importlib.resources.
# - hiddenimports: every module of the package. The plugins are loaded by name with
#   importlib, and update.py is imported only inside vcharon --update; the analysis follows
#   neither.

import shutil
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("vcharon", include_py_files=True)
# not the __main__ modules: they run their command when imported
hiddenimports = collect_submodules("vcharon", filter=lambda name: not name.endswith(".__main__"))
# Codecs looked up by name at import (pathrules: utf-16-le, config: utf-8-sig). PyInstaller
# 6.22.3 already puts all of encodings in base_library.zip, unpacked to disk at start; named
# here so a change of that default can't drop them.
hiddenimports += ["encodings.utf_16_le", "encodings.utf_8_sig"]

# Strip the collected shared libraries on Linux: CI's Python (actions/setup-python) ships
# libpython and its extension modules with debug info, which more than doubled the binary. In a
# one-file build PyInstaller strips every BINARY and EXTENSION it packs, not the bootloader; a
# failed strip is only a warning there, so a missing strip(1) stops the build instead. Not on
# macOS (its binary is small already, and stripping there is untested); not on Windows.
strip = sys.platform.startswith("linux")
if strip and shutil.which("strip") is None:
    raise SystemExit("vcharon.spec: no strip on the PATH; install binutils")

a = Analysis(
    ["src/vcharon/__main__.py"],
    pathex=["src"],
    datas=datas,
    hiddenimports=hiddenimports,
    # nothing here needs a GUI, line editing, a REPL or a pager, and they pull in shared
    # libraries (libreadline, libncursesw, libtinfo)
    excludes=["tkinter", "readline", "_pyrepl", "pydoc", "curses", "_curses"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="vcharon",
    console=True,
    upx=False,
    strip=strip,
    debug=False,
    runtime_tmpdir=None,
)
