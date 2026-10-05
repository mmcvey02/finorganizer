"""Build the FinOrganizer programs with PyInstaller.

    pip install pyinstaller pywebview
    python packaging/build_exe.py            # everything for this platform
    python packaging/build_exe.py desktop    # just the desktop app
    python packaging/build_exe.py cli        # just the command-line tool (Windows/Linux)

Output in dist/:
  Windows: FinOrganizer.exe (desktop app, no console; closing it quits) and
           FinOrganizer-cli.exe (command-line tool; with no arguments it serves the app
           to your browser)
  macOS:   FinOrganizer.app (desktop app); packaging/make_dmg.sh turns it into a .dmg

PyInstaller can't cross-compile: build on Windows for Windows and on a Mac for macOS.
"""

import os
import sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEP = ";" if os.name == "nt" else ":"
MAC = sys.platform == "darwin"

COMMON = [
    "--noconfirm",
    "--clean",
    "--paths", ROOT,
    "--add-data", "%s%sfinorganizer/static" % (os.path.join(ROOT, "finorganizer", "static"), SEP),
    "--distpath", os.path.join(ROOT, "dist"),
    "--workpath", os.path.join(ROOT, "build"),
    "--specpath", os.path.join(ROOT, "build"),
]

TARGETS = {
    "desktop": [
        os.path.join(ROOT, "packaging", "finorganizer_desktop.py"),
        "--name", "FinOrganizer",
        "--windowed",  # no console window; errors are shown in a message box and logged
        "--collect-all", "webview",
    ] + (
        # macOS: a normal .app bundle (one-file .apps are deprecated by PyInstaller).
        ["--onedir", "--osx-bundle-identifier", "io.github.mmcvey02.finorganizer"]
        if MAC else ["--onefile"]
    ),
    "cli": [
        os.path.join(ROOT, "packaging", "finorganizer_app.py"),
        "--name", "FinOrganizer-cli",
        "--onefile",
        "--console",  # shows the app address; closing it stops the server
        "--exclude-module", "webview",
    ],
}

default = ["desktop"] if MAC else list(TARGETS)
for name in sys.argv[1:] or default:
    PyInstaller.__main__.run(TARGETS[name] + COMMON)
