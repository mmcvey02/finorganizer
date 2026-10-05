"""In-app updates from GitHub Releases.

Each Windows build is published as a GitHub Release (tag ``v<major>.<minor>.<build>``)
with FinOrganizer.exe and FinOrganizer-cli.exe attached. The app compares its own
version with the latest release and, in the packaged Windows desktop app, can
replace itself:

  1. download the new .exe next to the running one (``FinOrganizer.exe.new``),
     checking its size and SHA-256 against what GitHub reports;
  2. rename the running .exe to ``FinOrganizer.exe.old`` (Windows allows renaming a
     running program, just not overwriting it) and move the new one into place;
  3. start the new version and close this one. The ``.old`` file is deleted on the
     next start.

Your data lives elsewhere (``%APPDATA%\\FinOrganizer``), so updates never touch it.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .net import describe_ssl_error, ssl_context

REPO = "mmcvey02/finorganizer"
LATEST_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases/latest" % REPO
TIMEOUT = 20
# Only download from GitHub's own hosts.
ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")


class UpdateError(Exception):
    pass


def current_version():
    """Version of the running program (CI stamps the build number into _build.py)."""
    try:
        from ._build import VERSION
        return VERSION
    except ImportError:
        return __version__


def parse_version(text):
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


def can_self_update():
    """Only the packaged Windows desktop app replaces itself; elsewhere we link to the download."""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def _request(url, accept="application/vnd.github+json"):
    from .banksync import USER_AGENT
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_context())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("no published releases found (the repository may be private, "
                              "or no release has been published yet)")
        if e.code == 403:
            raise UpdateError("GitHub is rate-limiting update checks; try again later")
        raise UpdateError("GitHub returned HTTP %d" % e.code)
    except urllib.error.URLError as e:
        raise UpdateError("couldn't reach GitHub: %s" % describe_ssl_error(e))


def check():
    """Look up the latest release. Never raises; problems are reported in ``error``."""
    info = {"current": current_version(), "latest": None, "available": False, "notes": "",
            "published": None, "page": RELEASES_PAGE, "can_install": can_self_update(), "error": None}
    try:
        with _request(LATEST_URL) as res:
            rel = json.loads(res.read())
    except (UpdateError, ValueError) as e:
        info["error"] = str(e)
        return info
    info["latest"] = (rel.get("tag_name") or "").lstrip("v")
    info["notes"] = (rel.get("body") or "").strip()[:2000]
    info["published"] = rel.get("published_at")
    info["page"] = rel.get("html_url") or RELEASES_PAGE
    info["available"] = bool(info["latest"]) and is_newer(info["latest"], info["current"])
    info["assets"] = {a.get("name"): {"url": a.get("browser_download_url"), "size": a.get("size"),
                                      "digest": a.get("digest")}
                      for a in rel.get("assets") or []}
    return info


def _download(asset, dest):
    url = asset.get("url") or ""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise UpdateError("refusing to download from an unexpected address: %s" % url)
    tmp = dest + ".part"
    digest = hashlib.sha256()
    size = 0
    with _request(url, accept="application/octet-stream") as res, open(tmp, "wb") as f:
        while True:
            chunk = res.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    expected_size = asset.get("size")
    expected_digest = (asset.get("digest") or "").lower()
    if (expected_size and size != expected_size) or \
            (expected_digest.startswith("sha256:") and digest.hexdigest() != expected_digest[7:]):
        os.remove(tmp)
        raise UpdateError("the download was incomplete or corrupted; nothing was changed")
    os.replace(tmp, dest)


def install(info, exe_path=None):
    """Download and swap in the new version. Returns the path of the program to start.

    Also updates FinOrganizer-cli.exe if it sits in the same folder.
    """
    exe_path = os.path.abspath(exe_path or sys.executable)
    folder = os.path.dirname(exe_path)
    assets = info.get("assets") or {}
    main_name = os.path.basename(exe_path)
    if main_name not in assets:
        main_name = "FinOrganizer.exe"
    if main_name not in assets:
        raise UpdateError("the latest release has no %s attached" % main_name)
    if not os.access(folder, os.W_OK):
        raise UpdateError("FinOrganizer can't write to %s; download the update manually" % folder)

    new_path = exe_path + ".new"
    _download(assets[main_name], new_path)
    old_path = exe_path + ".old"
    if os.path.exists(old_path):
        os.remove(old_path)
    os.rename(exe_path, old_path)      # allowed while running on Windows
    try:
        os.replace(new_path, exe_path)
    except OSError:
        os.rename(old_path, exe_path)  # put the working version back
        raise

    cli = os.path.join(folder, "FinOrganizer-cli.exe")
    if "FinOrganizer-cli.exe" in assets and os.path.exists(cli) and os.path.abspath(cli) != exe_path:
        try:
            _download(assets["FinOrganizer-cli.exe"], cli + ".new")
            os.replace(cli + ".new", cli)
        except (UpdateError, OSError):
            pass  # the CLI is optional; it can be updated next time (e.g. if it was running)
    return exe_path


def relaunch(exe_path):
    """Start the (new) program detached from this process."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe_path], close_fds=True, creationflags=flags, cwd=os.path.dirname(exe_path))


def cleanup_old(exe_path=None):
    """Remove the previous version left behind by an update."""
    exe_path = os.path.abspath(exe_path or sys.executable)
    for leftover in (exe_path + ".old", exe_path + ".new", exe_path + ".new.part"):
        try:
            if os.path.exists(leftover):
                os.remove(leftover)
        except OSError:
            pass
