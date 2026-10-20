# PyInstaller spec: one folder, two executables sharing the same runtime.
import os
#   Plaso.exe     - windowed GUI
#   plaso-cli.exe - console (get / capture sub-commands; the GUI drives this one)
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []
for pkg in ("mitmproxy", "mitmproxy_rs", "mitmproxy_windows"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ["plaso_get", "plaso_capture", "plaso_gui", "plaso_pipeline",
                  "plaso_cache", "plaso_titles"]

a = Analysis(
    [os.path.join(SPECPATH, "..", "plaso_app.py")],
    pathex=[os.path.join(SPECPATH, "..")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["mitmproxy.tools.web", "mitmproxy.tools.console", "urwid"],
    noarchive=False,
)
pyz = PYZ(a.pure)

gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Plaso",
          console=False, upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="plaso-cli",
          console=True, upx=False)

coll = COLLECT(gui, cli, a.binaries, a.datas, name="Plaso", upx=False)
