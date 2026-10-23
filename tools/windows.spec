# Portable Windows x64 application. Build this spec on Windows with the same
# Python architecture as the target machine; PyInstaller is not a cross-compiler.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


# PyInstaller sets SPECPATH to the directory containing this spec file
# (the project's tools directory), so one parent is the project root.
root = Path(SPECPATH).resolve().parent
datas = [
    (str(root / "fpv_matcher" / "static"), "fpv_matcher/static"),
    (str(root / "rate_presets.yaml"), "."),
]
hiddenimports = [
    *collect_submodules("fpv_matcher"),
    *collect_submodules("uvicorn"),
    "telemetry_parser",
]

a = Analysis(
    [str(root / "fpv_matcher" / "windows_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FPV Matcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
app = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FPV Matcher",
)
