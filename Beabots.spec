# -*- mode: python ; coding: utf-8 -*-

# This now packages server.py (the Flask/SocketIO backend), not ui.py.
# The Electron shell (main.js) is the actual app entry point; this exe is
# spawned by main.js as a child process, expected at
# resources/server/server.exe once bundled (see package.json's
# build.extraResources, which copies python-build/server -> resources/server).
#
# Build with:
#   pyinstaller Beabots.spec --distpath python-build

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[('templates', 'templates')],
    hiddenimports=[
        # flask-socketio's threading async mode + its WebSocket driver —
        # PyInstaller's import scanner doesn't always catch these since
        # they're selected dynamically at runtime rather than imported
        # directly at the top of server.py.
        'engineio.async_drivers.threading',
        'simple_websocket',
        'socketio',
        'engineio',
        # openpyxl occasionally needs this pulled in explicitly too.
        'openpyxl.cell._writer',
    ],
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
    name='server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # console=True is intentional and required here, even though this has
    # no GUI of its own: with console=False, PyInstaller sets sys.stdout/
    # sys.stderr to None in the frozen exe, and server.py's own print()/
    # logging calls would crash. main.js hides the console window itself
    # (windowsHide: true on the spawn call) so nothing visible flashes on
    # screen — this just keeps a real (hidden) stdout pipe available.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['bot.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='server',
)
