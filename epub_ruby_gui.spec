# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for epub-ruby GUI (single-file)
打包命令: pyinstaller epub_ruby_gui.spec
输出: dist/epub-ruby-gui.exe (单文件，约 63MB)
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

_here = Path(SPECPATH)

# ── 额外数据文件 ──
_datas = []
# 收集 unidic_lite 词典数据（fugashi 运行时需要 dicdir/ 下的文件）
_datas += collect_data_files("unidic_lite")
_readme = _here / "README.md"
if _readme.exists():
    _datas.append((str(_readme), "."))

a = Analysis(
    ["epub_ruby/gui.py"],
    pathex=[str(_here)],
    binaries=[],
    datas=_datas,
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "fugashi",
        "unidic_lite",
        "openai",
        "google.genai",
        "lxml",
        "bs4",
        "soupsieve",
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "unittest",
        "tkinter",
        "IPython",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ── 单文件可执行程序 ──
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,                        # 全部打包进 exe
    a.zipfiles,
    a.datas,
    [],
    name="epub-ruby-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
