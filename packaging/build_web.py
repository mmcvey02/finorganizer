"""Build the iPhone / web version of FinOrganizer: a static site, no server needed.

    npm pack pyodide@314.0.7 && tar xzf pyodide-314.0.7.tgz     # -> package/
    python packaging/build_web.py --pyodide-dir package [--out dist/web]

The site runs the same Python code as the desktop app on Pyodide (Python compiled to
WebAssembly) and keeps its data in the browser's storage on the device. Publish the
output folder anywhere that serves static files over HTTPS (CI uses GitHub Pages);
on an iPhone, open it in Safari and choose Share > Add to Home Screen.
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import zipfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from finorganizer import updater  # noqa: E402

PYODIDE_VERSION = "314.0.7"
PYODIDE_FILES = ["pyodide.js", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"]
STATIC = os.path.join(ROOT, "finorganizer", "static")
WEB = os.path.join(ROOT, "web")
THEME = "#2a78d6"
BACKGROUND = "#f4f4f2"

HEAD_TAGS = """<meta name="theme-color" content="#f4f4f2" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#111110" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="FinOrganizer">
<meta name="description" content="Personal finance tracking, budgets and planning. Runs entirely on your device.">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="icons/icon-192.png">
<link rel="manifest" href="manifest.webmanifest">
<link rel="stylesheet" href="finweb.css">
"""


def build_index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()

    def swap(old, new):
        nonlocal html
        if html.count(old) != 1:
            raise SystemExit("index.html changed: can't find %r" % old)
        html = html.replace(old, new)

    swap('<link rel="stylesheet" href="/style.css">\n', '<link rel="stylesheet" href="style.css">\n' + HEAD_TAGS)
    swap('<script src="/app.js"></script>',
         '<script src="pyodide/pyodide.js"></script>\n<script src="finweb.js"></script>\n<script src="app.js"></script>')
    return html


def build_package_zip():
    """The finorganizer Python package, as unpacked into the browser at startup."""
    buf = io.BytesIO()
    pkg = os.path.join(ROOT, "finorganizer")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(pkg)):
            if name.endswith(".py"):
                z.write(os.path.join(pkg, name), "finorganizer/" + name)
    return buf.getvalue()


# ------------------------------------------------------------------ app icon
def _png(width, height, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def make_icon(size, padding=0.0):
    """Rising white bars on blue, drawn with simple anti-aliasing.

    iOS rounds the corners itself, so the icon is a full square. ``padding`` shrinks
    the artwork for Android's "maskable" icons, which are cropped to a circle.
    """
    top, bottom = _hex("#3d8de8"), _hex("#1d5fb4")
    s = 1 - 2 * padding
    # Bars as (x0, y0, x1, y1) in a 0..1 square, corner radius r.
    bars = [(0.20, 0.56, 0.34, 0.78), (0.43, 0.42, 0.57, 0.78), (0.66, 0.24, 0.80, 0.78)]
    bars = [tuple(padding + v * s for v in b) for b in bars]
    r = 0.025 * s

    def inside(x, y):
        for x0, y0, x1, y1 in bars:
            if x0 <= x <= x1 and y0 <= y <= y1:
                cx = min(max(x, x0 + r), x1 - r)
                cy = min(max(y, y0 + r), y1 - r)
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    return True
        return False

    ss = 3
    rows = []
    for py in range(size):
        t = py / (size - 1)
        bg = [round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)]
        row = []
        for px in range(size):
            hits = sum(inside((px + (i + 0.5) / ss) / size, (py + (j + 0.5) / ss) / size)
                       for i in range(ss) for j in range(ss))
            a = hits / (ss * ss)
            row += [round(bg[i] + (255 - bg[i]) * a) for i in range(3)]
        rows.append(row)
    return _png(size, size, rows)


def manifest():
    return {
        "name": "FinOrganizer",
        "short_name": "FinOrganizer",
        "description": "Personal finance tracking, budgets and planning. Runs entirely on your device.",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": BACKGROUND,
        "theme_color": THEME,
        "icons": [
            {"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


def build(pyodide_dir, out):
    missing = [f for f in PYODIDE_FILES if not os.path.isfile(os.path.join(pyodide_dir, f))]
    if missing:
        raise SystemExit("Pyodide files missing from %s: %s" % (pyodide_dir, ", ".join(missing)))
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "pyodide"))
    os.makedirs(os.path.join(out, "icons"))

    files = {
        "index.html": build_index().encode("utf-8"),
        "finorganizer.zip": build_package_zip(),
        "manifest.webmanifest": json.dumps(manifest(), indent=2).encode(),
        "icons/apple-touch-icon.png": make_icon(180),
        "icons/icon-192.png": make_icon(192),
        "icons/icon-512.png": make_icon(512),
        "icons/icon-maskable-512.png": make_icon(512, padding=0.1),
    }
    for name in ("app.js", "style.css"):
        with open(os.path.join(STATIC, name), "rb") as f:
            files[name] = f.read()
    for name in ("finweb.js", "finweb.css"):
        with open(os.path.join(WEB, name), "rb") as f:
            files[name] = f.read()
    for name in PYODIDE_FILES:
        with open(os.path.join(pyodide_dir, name), "rb") as f:
            files["pyodide/" + name] = f.read()

    # The service worker's cache is named after the version plus a fingerprint of
    # every file, so any change at all reaches phones as an update.
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + files[name])
    version = "%s-%s" % (updater.current_version(), digest.hexdigest()[:10])
    with open(os.path.join(WEB, "sw.js"), encoding="utf-8") as f:
        sw = f.read()
    cached = ["./"] + sorted(n for n in files if n != "index.html")
    sw = sw.replace("__VERSION__", version).replace("__FILES__", json.dumps(cached))
    files["sw.js"] = sw.encode("utf-8")
    files[".nojekyll"] = b""  # serve files as-is on GitHub Pages

    for name, data in files.items():
        with open(os.path.join(out, name), "wb") as f:
            f.write(data)
    total = sum(len(d) for d in files.values())
    print("Built FinOrganizer web app %s in %s (%d files, %.1f MB)" % (version, out, len(files), total / 1e6))
    return version


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pyodide-dir", required=True,
                    help="folder with the Pyodide %s files (the npm package's package/ folder)" % PYODIDE_VERSION)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "web"))
    args = ap.parse_args(argv)
    build(args.pyodide_dir, args.out)


if __name__ == "__main__":
    main()
