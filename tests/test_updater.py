import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from finorganizer import updater


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def fake_release(tag="v1.1.40", files=None):
    files = files or {}
    return {
        "tag_name": tag, "body": "Bank sync improvements", "published_at": "2026-10-05T06:00:00Z",
        "html_url": "https://github.com/mmcvey02/finorganizer/releases/tag/" + tag,
        "assets": [{"name": name, "size": len(data),
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "browser_download_url": "https://github.com/mmcvey02/finorganizer/releases/download/%s/%s" % (tag, name)}
                   for name, data in files.items()],
    }


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def serve(self, release, files):
        def fake_request(url, accept=None):
            if url == updater.LATEST_URL:
                return _Resp(json.dumps(release).encode())
            return _Resp(files[url.rsplit("/", 1)[1]])
        return mock.patch.object(updater, "_request", fake_request)

    def test_versions(self):
        self.assertTrue(updater.is_newer("1.1.40", "1.1.9"))
        self.assertTrue(updater.is_newer("v1.2.0", "1.1.99"))
        self.assertFalse(updater.is_newer("1.1.9", "1.1.9"))
        self.assertFalse(updater.is_newer("", "1.1.9"))
        self.assertEqual(updater.parse_version("v2"), (2, 0, 0))

    def test_check(self):
        with self.serve(fake_release("v99.0.1"), {}):
            info = updater.check()
        self.assertTrue(info["available"])
        self.assertEqual((info["latest"], info["notes"]), ("99.0.1", "Bank sync improvements"))
        with self.serve(fake_release("v0.0.1"), {}):
            self.assertFalse(updater.check()["available"])

    def test_check_reports_errors_instead_of_raising(self):
        def boom(url, accept=None):
            raise updater.UpdateError("no published releases found")
        with mock.patch.object(updater, "_request", boom):
            info = updater.check()
        self.assertFalse(info["available"])
        self.assertIn("no published releases", info["error"])

    def test_install_swaps_program_and_cli(self):
        exe = os.path.join(self.dir, "FinOrganizer.exe")
        cli = os.path.join(self.dir, "FinOrganizer-cli.exe")
        for path in (exe, cli):
            with open(path, "wb") as f:
                f.write(b"old")
        files = {"FinOrganizer.exe": b"new desktop", "FinOrganizer-cli.exe": b"new cli"}
        with self.serve(fake_release(files=files), files):
            info = updater.check()
            self.assertEqual(updater.install(info, exe), exe)
        with open(exe, "rb") as f:
            self.assertEqual(f.read(), b"new desktop")
        with open(cli, "rb") as f:
            self.assertEqual(f.read(), b"new cli")
        with open(exe + ".old", "rb") as f:
            self.assertEqual(f.read(), b"old")  # the running program was moved aside, not overwritten
        updater.cleanup_old(exe)
        self.assertFalse(os.path.exists(exe + ".old"))

    def test_corrupted_download_changes_nothing(self):
        exe = os.path.join(self.dir, "FinOrganizer.exe")
        with open(exe, "wb") as f:
            f.write(b"old")
        release = fake_release(files={"FinOrganizer.exe": b"the real bytes"})
        with self.serve(release, {"FinOrganizer.exe": b"tampered bytes"}):
            with self.assertRaisesRegex(updater.UpdateError, "corrupted"):
                updater.install(updater.check(), exe)
        with open(exe, "rb") as f:
            self.assertEqual(f.read(), b"old")
        self.assertEqual(sorted(os.listdir(self.dir)), ["FinOrganizer.exe"])

    def test_only_downloads_from_github(self):
        with self.assertRaisesRegex(updater.UpdateError, "unexpected address"):
            updater._download({"url": "https://evil.example.com/FinOrganizer.exe"}, os.path.join(self.dir, "x"))
        with self.assertRaisesRegex(updater.UpdateError, "unexpected address"):
            updater._download({"url": "http://github.com/x.exe"}, os.path.join(self.dir, "x"))

    def test_self_update_only_in_packaged_windows_app(self):
        self.assertFalse(updater.can_self_update())  # tests run from source


if __name__ == "__main__":
    unittest.main()
