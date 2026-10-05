"""In-app updates from GitHub Releases.

Each build is published as a GitHub Release (tag ``v<major>.<minor>.<build>``) with
FinOrganizer.exe, FinOrganizer-cli.exe and the two macOS .dmg files attached. The app
compares its own version with the latest release and the packaged desktop app can
replace itself. Every download is checked against the size and SHA-256 GitHub reports.

Windows: download ``FinOrganizer.exe.new`` next to the running program, rename the
running .exe to ``.old`` (Windows allows renaming a running program, just not
overwriting it), move the new one into place, restart.

macOS: download the right .dmg (Apple Silicon or Intel), mount it, copy the app next
to the installed one, verify its code signature, swap the two app folders, restart.

Leftovers (``.old``) are removed on the next start. Your data lives elsewhere, so
updates never touch it.
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
    """The packaged desktop apps (Windows .exe, macOS .app) replace themselves; when running
    from source we link to the download instead."""
    if not getattr(sys, "frozen", False):
        return False
    return os.name == "nt" or (sys.platform == "darwin" and bundle_path() is not None)


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
    """Download and swap in the new version. Returns the path of the program to start."""
    if sys.platform == "darwin":
        return _install_mac(info, exe_path)
    return _install_windows(info, exe_path)


# ------------------------------------------------------------------- Windows

def _swap_file(new_path, exe_path):
    """Replace a (possibly running) program file, restoring it if anything fails."""
    old_path = exe_path + ".old"
    if os.path.exists(old_path):
        os.remove(old_path)
    os.rename(exe_path, old_path)      # allowed while running on Windows
    try:
        os.replace(new_path, exe_path)
    except OSError:
        os.rename(old_path, exe_path)  # put the working version back
        raise


def _install_windows(info, exe_path=None):
    """Also updates FinOrganizer-cli.exe if it sits in the same folder."""
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
    _swap_file(new_path, exe_path)

    cli = os.path.join(folder, "FinOrganizer-cli.exe")
    if "FinOrganizer-cli.exe" in assets and os.path.exists(cli) and os.path.abspath(cli) != exe_path:
        try:
            _download(assets["FinOrganizer-cli.exe"], cli + ".new")
            os.replace(cli + ".new", cli)
        except (UpdateError, OSError):
            pass  # the CLI is optional; it can be updated next time (e.g. if it was running)
    return exe_path


# --------------------------------------------------------------------- macOS

def bundle_path(exe_path=None):
    """The FinOrganizer.app folder containing the running program, or None."""
    exe_path = os.path.abspath(exe_path or sys.executable)
    parts = exe_path.split(os.sep)
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].endswith(".app"):
            return os.sep.join(parts[:i + 1])
    return None


def _is_apple_silicon():
    """True on M-series Macs, even when an Intel build runs under Rosetta."""
    try:
        out = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True)
        return out.stdout.strip() == "1"
    except OSError:
        import platform
        return platform.machine() == "arm64"


def mac_asset_name():
    return "FinOrganizer-mac-%s.dmg" % ("apple-silicon" if _is_apple_silicon() else "intel")


def _run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise UpdateError("%s failed: %s" % (cmd[0], (result.stderr or result.stdout).strip()[:200]))
    return result.stdout


def _check_mac_location(bundle):
    if not bundle:
        raise UpdateError("FinOrganizer isn't running as an app bundle")
    if "/AppTranslocation/" in bundle:
        raise UpdateError("macOS is running FinOrganizer from a temporary copy. Drag FinOrganizer "
                          "into your Applications folder, open it from there, and update again")
    if bundle.startswith("/Volumes/"):
        raise UpdateError("FinOrganizer is running from the disk image. Drag it into your "
                          "Applications folder first, then open it from there")
    if not os.access(os.path.dirname(bundle), os.W_OK):
        raise UpdateError("FinOrganizer can't write to %s; download the update manually"
                          % os.path.dirname(bundle))


def _install_mac_from_dmg(dmg, bundle):
    """Replace the app at ``bundle`` with the FinOrganizer.app inside ``dmg``."""
    import shutil
    import tempfile
    parent = os.path.dirname(bundle)
    name = os.path.basename(bundle)
    staged = os.path.join(parent, "." + name + ".new")
    old = os.path.join(parent, "." + name + ".old")
    mount = tempfile.mkdtemp(prefix="finorganizer-update-")
    _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, dmg])
    try:
        source = os.path.join(mount, "FinOrganizer.app")
        if not os.path.isdir(source):
            raise UpdateError("the downloaded disk image doesn't contain FinOrganizer.app")
        shutil.rmtree(staged, ignore_errors=True)
        _run(["ditto", source, staged])   # preserves code signatures and permissions
    finally:
        subprocess.run(["hdiutil", "detach", "-force", mount], capture_output=True)
        shutil.rmtree(mount, ignore_errors=True)
    try:
        _run(["codesign", "--verify", "--deep", "--strict", staged])
        # Downloaded by the app itself (and checksum-verified), so it needs no
        # Gatekeeper "Open Anyway"; make sure no quarantine flag slipped in.
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", staged], capture_output=True)
    except UpdateError:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
    os.rename(bundle, old)            # the running app keeps working from its open files
    try:
        os.rename(staged, bundle)
    except OSError:
        os.rename(old, bundle)        # put the working version back
        raise


def _install_mac(info, exe_path=None):
    import tempfile
    bundle = bundle_path(exe_path)
    _check_mac_location(bundle)
    name = mac_asset_name()
    asset = (info.get("assets") or {}).get(name)
    if not asset:
        raise UpdateError("the latest release has no %s attached" % name)
    folder = tempfile.mkdtemp(prefix="finorganizer-dl-")
    dmg = os.path.join(folder, name)
    try:
        _download(asset, dmg)
        _install_mac_from_dmg(dmg, bundle)
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    return bundle


# ---------------------------------------------------------------- after update

def relaunch(path):
    """Start the (new) program detached from this process."""
    if sys.platform == "darwin" and path.endswith(".app"):
        # -n: start a fresh instance even though this (old) one hasn't quite exited yet.
        subprocess.Popen(["open", "-n", path], close_fds=True)
        return
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([path], close_fds=True, creationflags=flags, cwd=os.path.dirname(path))


def leftovers(exe_path=None):
    """Files or folders a previous update may have left behind."""
    exe_path = os.path.abspath(exe_path or sys.executable)
    if sys.platform == "darwin":
        bundle = bundle_path(exe_path)
        if not bundle:
            return []
        parent, name = os.path.dirname(bundle), os.path.basename(bundle)
        return [os.path.join(parent, "." + name + ".old"), os.path.join(parent, "." + name + ".new")]
    return [exe_path + ".old", exe_path + ".new", exe_path + ".new.part"]


def cleanup_old(exe_path=None):
    """Remove the previous version left behind by an update."""
    import shutil
    for leftover in leftovers(exe_path):
        try:
            if os.path.isdir(leftover):
                shutil.rmtree(leftover)
            elif os.path.exists(leftover):
                os.remove(leftover)
        except OSError:
            pass
