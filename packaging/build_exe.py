"""Build a single-file executable with PyInstaller.

    pip install pyinstaller
    python packaging/build_exe.py

Produces dist/FinOrganizer.exe on Windows (dist/FinOrganizer elsewhere).
PyInstaller can't cross-compile: run this on Windows to get a Windows .exe.
"""

import os

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEP = ";" if os.name == "nt" else ":"

PyInstaller.__main__.run([
    os.path.join(ROOT, "packaging", "finorganizer_app.py"),
    "--name", "FinOrganizer",
    "--onefile",
    "--console",  # the window shows the app address and closing it stops the app
    "--noconfirm",
    "--clean",
    "--paths", ROOT,
    "--add-data", "%s%sfinorganizer/static" % (os.path.join(ROOT, "finorganizer", "static"), SEP),
    "--distpath", os.path.join(ROOT, "dist"),
    "--workpath", os.path.join(ROOT, "build"),
    "--specpath", os.path.join(ROOT, "build"),
])
