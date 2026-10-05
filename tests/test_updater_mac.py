"""macOS self-update logic, with the Mac-only tools (hdiutil, ditto, codesign) simulated.

The real tools are exercised against the real .dmg in the macOS CI job.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from finorganizer import updater


def make_app(path, marker):
    os.makedirs(os.path.join(path, "Contents", "MacOS"))
    with open(os.path.join(path, "Contents", "MacOS", "FinOrganizer"), "w") as f:
        f.write(marker)


def read_marker(app):
    with open(os.path.join(app, "Contents", "MacOS", "FinOrganizer")) as f:
        return f.read()


class FakeMacTools:
    """Stands in for hdiutil/ditto/codesign/xattr."""

    def __init__(self, new_marker="v2", codesign_ok=True, has_app=True):
        self.new_marker, self.codesign_ok, self.has_app = new_marker, codesign_ok, has_app
        self.calls = []

    def run(self, cmd):  # replaces updater._run
        self.calls.append(cmd[0])
        if cmd[0] == "hdiutil" and cmd[1] == "attach":
            mount = cmd[cmd.index("-mountpoint") + 1]
            if self.has_app:
                make_app(os.path.join(mount, "FinOrganizer.app"), self.new_marker)
        elif cmd[0] == "ditto":
            shutil.copytree(cmd[1], cmd[2])
        elif cmd[0] == "codesign" and not self.codesign_ok:
            raise updater.UpdateError("codesign failed: invalid signature")
        return ""

    def subprocess_run(self, cmd, **kw):  # replaces subprocess.run for detach/xattr
        self.calls.append(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, "", "")


class MacUpdateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.apps = os.path.join(self.dir, "Applications")
        self.bundle = os.path.join(self.apps, "FinOrganizer.app")
        make_app(self.bundle, "v1")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def install(self, tools):
        with mock.patch.object(updater, "_run", tools.run), \
                mock.patch.object(updater.subprocess, "run", tools.subprocess_run):
            updater._install_mac_from_dmg(os.path.join(self.dir, "x.dmg"), self.bundle)

    def test_swaps_in_the_new_app(self):
        tools = FakeMacTools()
        self.install(tools)
        self.assertEqual(read_marker(self.bundle), "v2")
        old = os.path.join(self.apps, ".FinOrganizer.app.old")
        self.assertEqual(read_marker(old), "v1")
        self.assertIn("codesign", tools.calls)
        self.assertIn("xattr", tools.calls)
        with mock.patch.object(updater.sys, "platform", "darwin"):
            exe = os.path.join(self.bundle, "Contents", "MacOS", "FinOrganizer")
            updater.cleanup_old(exe)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(sorted(os.listdir(self.apps)), ["FinOrganizer.app"])

    def test_bad_signature_leaves_the_installed_app_alone(self):
        with self.assertRaisesRegex(updater.UpdateError, "codesign"):
            self.install(FakeMacTools(codesign_ok=False))
        self.assertEqual(read_marker(self.bundle), "v1")
        self.assertEqual(sorted(os.listdir(self.apps)), ["FinOrganizer.app"])

    def test_dmg_without_app_is_rejected(self):
        with self.assertRaisesRegex(updater.UpdateError, "doesn't contain"):
            self.install(FakeMacTools(has_app=False))
        self.assertEqual(read_marker(self.bundle), "v1")

    def test_bundle_path(self):
        # Built with this OS's own path conventions so the test passes everywhere.
        exe = os.path.join(self.bundle, "Contents", "MacOS", "FinOrganizer")
        self.assertEqual(updater.bundle_path(exe), self.bundle)
        self.assertIsNone(updater.bundle_path(os.path.join(self.dir, "bin", "python3")))

    def test_locations_that_cannot_be_updated(self):
        with self.assertRaisesRegex(updater.UpdateError, "temporary copy"):
            updater._check_mac_location("/private/var/folders/x/AppTranslocation/ABC/d/FinOrganizer.app")
        with self.assertRaisesRegex(updater.UpdateError, "disk image"):
            updater._check_mac_location("/Volumes/FinOrganizer/FinOrganizer.app")
        os.chmod(self.apps, 0o555)
        try:
            if not os.access(self.apps, os.W_OK):  # root can write anyway
                with self.assertRaisesRegex(updater.UpdateError, "can't write"):
                    updater._check_mac_location(self.bundle)
        finally:
            os.chmod(self.apps, 0o755)
        updater._check_mac_location(self.bundle)  # a normal Applications folder is fine

    def test_picks_the_right_dmg(self):
        for out, name in (("1\n", "FinOrganizer-mac-apple-silicon.dmg"), ("0\n", "FinOrganizer-mac-intel.dmg")):
            done = subprocess.CompletedProcess([], 0, out, "")
            with mock.patch.object(updater.subprocess, "run", return_value=done):
                self.assertEqual(updater.mac_asset_name(), name)

    def test_relaunch_uses_open(self):
        with mock.patch.object(updater.sys, "platform", "darwin"), \
                mock.patch.object(updater.subprocess, "Popen") as popen:
            updater.relaunch("/Applications/FinOrganizer.app")
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ["open", "-n", "/Applications/FinOrganizer.app"])

    def test_full_install_downloads_the_matching_dmg(self):
        info = {"assets": {"FinOrganizer-mac-intel.dmg": {"url": "https://github.com/x.dmg"},
                           "FinOrganizer-mac-apple-silicon.dmg": {"url": "https://github.com/y.dmg"}}}
        got = {}

        def fake_download(asset, dest):
            got["url"] = asset["url"]
            open(dest, "w").close()

        exe = os.path.join(self.bundle, "Contents", "MacOS", "FinOrganizer")
        with mock.patch.object(updater, "_download", fake_download), \
                mock.patch.object(updater, "mac_asset_name", return_value="FinOrganizer-mac-apple-silicon.dmg"), \
                mock.patch.object(updater, "_install_mac_from_dmg") as swap:
            self.assertEqual(updater._install_mac(info, exe), self.bundle)
        self.assertEqual(got["url"], "https://github.com/y.dmg")
        swap.assert_called_once()


if __name__ == "__main__":
    unittest.main()
