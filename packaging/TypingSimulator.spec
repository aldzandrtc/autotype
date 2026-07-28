# PyInstaller spec for the macOS app bundle.  Built by `make app`.
#
# Bundling is not only convenience.  macOS assigns Accessibility permission to
# the *responsible process*, so running from a terminal means granting it to
# the terminal - which is confusing, easy to get wrong, and has to be redone
# whenever the terminal changes.  A signed-in-place .app has its own identity,
# so the permission is granted to "Typing Simulator" itself and stays granted.
#
# ``LSUIElement`` marks it as an accessory app: no Dock icon, no menu bar, and
# it never steals focus - the same thing the code does at runtime, but applied
# before the process starts.

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "Typing Simulator"
BUNDLE_ID = "local.typing-simulator"
VERSION = "0.1.0"

hidden = collect_submodules("pynput")

analysis = Analysis(
    ["../src/typing_simulator/__main__.py"],
    pathex=["../src"],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # Qt modules this overlay never touches; excluding them keeps the bundle
    # to a sane size.
    excludes=[
        "PySide6.Qt3DCore",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtNetwork",
        "PySide6.QtOpenGL",
        "PySide6.QtPdf",
        "PySide6.QtPositioning",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="TypingSimulator",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="TypingSimulator",
)

app = BUNDLE(
    collection,
    name=f"{APP_NAME}.app",
    icon=None,
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        # Accessory app: no Dock icon, no menu bar, never steals focus.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": (
            "Typing Simulator needs Accessibility permission to send keystrokes "
            "to the application you choose."
        ),
    },
)
