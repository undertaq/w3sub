# Windows one-file GUI build. Optional converters remain external selections.
from pathlib import Path

project_root = Path(SPECPATH)
codec_docs = project_root / 'w3sub_app' / 'third_party' / 'w3strings_codec'

a = Analysis(
    [str(project_root / 'w3sub.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(codec_docs / 'LICENSE'), 'w3sub_app/third_party/w3strings_codec'),
        (str(codec_docs / 'NOTICE.md'), 'w3sub_app/third_party/w3strings_codec'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    # The native codec hashes its physical source file for generation provenance.
    module_collection_mode={'w3sub_app.w3strings_native': 'pyz+py'},
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='W3DualSubtitleManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
