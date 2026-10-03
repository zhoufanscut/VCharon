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

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("vcharon", include_py_files=True)
# not the __main__ modules: they run their command when imported
hiddenimports = collect_submodules("vcharon", filter=lambda name: not name.endswith(".__main__"))
# Codecs looked up by name at import (pathrules: utf-16-le, config: utf-8-sig). PyInstaller
# 6.22.3 already puts all of encodings in base_library.zip, unpacked to disk at start; named
# here so a change of that default can't drop them.
hiddenimports += ["encodings.utf_16_le", "encodings.utf_8_sig"]

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
    strip=False,
    debug=False,
    runtime_tmpdir=None,
)
