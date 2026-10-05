"""Build single-file executables with PyInstaller.

    pip install pyinstaller pywebview
    python packaging/build_exe.py            # both
    python packaging/build_exe.py desktop    # just the desktop app
    python packaging/build_exe.py cli        # just the command-line / browser version

Produces, in dist/:
  FinOrganizer.exe      desktop app in its own window (no console); closing it quits
  FinOrganizer-cli.exe  command-line tool; with no arguments it serves the app to your browser

PyInstaller can't cross-compile: run this on Windows to get Windows executables.
"""

import os
import sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEP = ";" if os.name == "nt" else ":"

COMMON = [
    "--onefile",
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
    ],
    "cli": [
        os.path.join(ROOT, "packaging", "finorganizer_app.py"),
        "--name", "FinOrganizer-cli",
        "--console",  # shows the app address; closing it stops the server
        "--exclude-module", "webview",
    ],
}

for name in sys.argv[1:] or list(TARGETS):
    PyInstaller.__main__.run(TARGETS[name] + COMMON)
