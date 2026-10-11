# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Limitador.

Build with ``build.bat`` (which creates the venv and installs the dependencies first) or
directly:

    .venv\\Scripts\\pyinstaller.exe --clean --noconfirm Limitador.spec

Notes
-----
* ``pydivert`` ships the WinDivert driver as *data* files (``WinDivert64.dll`` and
  ``WinDivert64.sys``); ``collect_data_files`` puts them inside the bundle. At runtime the
  app copies them out to ``%ProgramData%\\Limitador\\driver`` and loads them from there, so
  the driver service never points into PyInstaller's temporary extraction directory.
* ``uac_admin=True`` embeds a manifest asking for elevation, so double-clicking the exe
  raises the UAC prompt by itself (the driver needs admin).
* ``console=False``: GUI build. The console features are still reachable with
  ``Limitador.exe --cli ...`` (output goes to the attached console when launched from one).
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve()

datas = [(str(ROOT / "Limitador.ico"), ".")]
datas += collect_data_files("pydivert", includes=["windivert_dll/*"])

hiddenimports = [
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "pydivert.windivert_dll",
]

excludes = [
    "matplotlib", "numpy", "scipy", "pandas", "tkinter.test", "test", "unittest",
    "pydoc_data",
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Limitador",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "Limitador.ico"),
    uac_admin=False,
)
