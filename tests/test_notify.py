"""Notifications: alerts, sending once, quiet hours, email, iPhone (Web Push), scheduling."""

import base64
import datetime as dt
import json
import os
import plistlib
import shutil
import socketserver
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from finorganizer import db, ledger as L, notify, schedule, webpush
from finorganizer.profiles import Profiles
from finorganizer.server import App, respond

HAVE_CRYPTO = webpush.available()
NOON = dt.datetime(2026, 3, 16, 12, 0)  # a Monday
TODAY = NOON.date()


def call(app, method, path, body=None):
    status, _, data, _ = respond(app, method, path, "", json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(data)


# ----------------------------------------------------------------- fake phone

def make_phone():
    """A pretend iPhone subscription: returns (pairing code, decrypt function)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    ua = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    vapid = ec.generate_private_key(ec.SECP256R1())
    d = vapid.private_numbers().private_value.to_bytes(32, "big")
    code = webpush.CODE_PREFIX + webpush.b64u(json.dumps({
        "e": "https://web.push.apple.com/QGuQyavXutnMH", "p": webpush.b64u(webpush._raw_public(ua)),
        "a": webpush.b64u(auth), "k": webpush.b64u(d), "n": "Test iPhone"}).encode())

    def decrypt(body):
        """What the phone does with a message (RFC 8291), to check the computer's side."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt, idlen = body[:16], body[20]
        as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
        shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
        h = webpush._hmac
        ikm = h(h(auth, shared), b"WebPush: info\x00" + webpush._raw_public(ua) + as_public + b"\x01")
        prk = h(salt, ikm)
        plain = AESGCM(h(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(
            h(prk, b"Content-Encoding: nonce\x00\x01")[:12], ciphertext, None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])

    return code, decrypt, vapid


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography package")
class WebPushTests(unittest.TestCase):
    def test_rfc8291_example(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        sender = ec.derive_private_key(int.from_bytes(webpush.unb64u(
            "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        body = webpush.encrypt(b"When I grow up, I want to be a watermelon",
                               "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                               "BTBZMqHH6r4Tts7J_aSIgg", salt=webpush.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), sender_key=sender)
        self.assertEqual(webpush.b64u(body),
                         "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tlz"
                         "AC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")

    def test_phone_can_read_what_we_send(self):
        code, decrypt, vapid = make_phone()
        device = webpush.parse_pairing_code(code)
        sent = {}

        class Res:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def fake_urlopen(req, timeout, context):
            sent["req"] = req
            return Res()

        with mock.patch.object(webpush.urllib.request, "urlopen", fake_urlopen):
            webpush.send(device, {"title": "Hi", "body": "There", "url": "#budgets"})
        req = sent["req"]
        self.assertEqual(decrypt(req.data), {"title": "Hi", "body": "There", "url": "#budgets"})
        self.assertEqual(req.get_header("Content-encoding"), "aes128gcm")
        # The VAPID signature verifies with the phone's key, for Apple's push service.
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        token = req.get_header("Authorization").split("t=")[1].split(",")[0]
        head, claims, sig = token.split(".")
        raw = webpush.unb64u(sig)
        vapid.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                                  (head + "." + claims).encode(), ec.ECDSA(hashes.SHA256()))
        self.assertEqual(json.loads(webpush.unb64u(claims))["aud"], "https://web.push.apple.com")

    def test_pairing_codes_are_checked(self):
        code, _, _ = make_phone()
        self.assertEqual(webpush.parse_pairing_code(" " + code[:40] + "\n" + code[40:])["name"], "Test iPhone")
        for bad in ("", "hello", webpush.CODE_PREFIX + "abc"):
            with self.assertRaisesRegex(ValueError, "pairing code"):
                webpush.parse_pairing_code(bad)
        data = json.loads(webpush.unb64u(code[len(webpush.CODE_PREFIX):]))
        data["e"] = "https://evil.example.com/push"
        with self.assertRaisesRegex(ValueError, "known push service"):
            webpush.parse_pairing_code(webpush.b64u(json.dumps(data).encode()))


# ------------------------------------------------------------------- alerts

class Base(unittest.TestCase):
    def setUp(self):
        self.app = App(db.connect(":memory:"))
        self.c = self.app.conn
        self.chk = L.add_account(self.c, "Checking", "checking", 300000)
        notify.update_config(self.c, {"enabled": True})

    def keys(self, now=NOON):
        return sorted(a["key"] for a in notify.collect(self.c, notify.get_config(self.c), now))


class AlertTests(Base):
    def test_bills_due_soon(self):
        rent = L.add_recurring(self.c, "Rent", self.chk, -150000, "monthly", "2026-03-18")
        L.add_recurring(self.c, "Paycheck", self.chk, 250000, "monthly", "2026-03-17")  # not a bill
        L.add_recurring(self.c, "Gym", self.chk, -4000, "monthly", "2026-03-25")       # not soon
        alerts = notify.collect(self.c, notify.get_config(self.c), NOON)
        bills = [a for a in alerts if a["kind"] == "bills"]
        self.assertEqual([a["key"] for a in bills], ["bill:%d:2026-03-18" % rent])
        self.assertEqual(bills[0]["title"], "Bill due Wed Mar 18: Rent")
        self.assertIn("$1,500.00 from Checking", bills[0]["body"])

    def test_budgets_at_80_and_100_percent(self):
        dining = L.find_category(self.c, "Dining Out")["id"]
        L.set_budget(self.c, dining, "2026-03", 10000)
        L.add_transaction(self.c, self.chk, "2026-03-02", -8500, "Cafe", dining)
        self.assertIn("budget:%d:2026-03:80" % dining, self.keys())
        L.add_transaction(self.c, self.chk, "2026-03-03", -1500, "Cafe", dining)  # exactly on budget
        self.assertEqual([a["title"] for a in notify.collect(self.c, notify.get_config(self.c), NOON)
                          if a["kind"] == "budgets"], ["Dining Out budget 100% used"])
        L.add_transaction(self.c, self.chk, "2026-03-03", -500, "Cafe", dining)
        self.assertEqual([k for k in self.keys() if k.startswith("budget")], ["budget:%d:2026-03:100" % dining])

    def test_unusual_activity(self):
        L.add_transaction(self.c, self.chk, "2026-03-10", -90000, "Old TV")  # before notifications were on? no: after
        big = L.add_transaction(self.c, self.chk, "2026-03-14", -60000, "Laptop Store")
        L.add_transaction(self.c, self.chk, "2026-03-14", -1000, "Coffee")
        keys = self.keys()
        self.assertIn("large:%d" % big, keys)
        self.assertNotIn("lowbal:%d" % self.chk, keys)
        L.add_transaction(self.c, self.chk, "2026-03-15", -145000, "Rent")
        self.assertIn("lowbal:%d" % self.chk, self.keys())
        self.c.execute("INSERT INTO connections (label, access_url, last_error) VALUES ('My Bank', 'https://x', 'denied')")
        self.assertTrue(any(k.startswith("sync:") for k in self.keys()))

    def test_only_new_purchases_count(self):
        notify.update_config(self.c, {"enabled": False})
        old = L.add_transaction(self.c, self.chk, "2026-03-14", -60000, "Before")
        notify.update_config(self.c, {"enabled": True})  # turning on starts from now
        new = L.add_transaction(self.c, self.chk, "2026-03-14", -60000, "After")
        keys = self.keys()
        self.assertNotIn("large:%d" % old, keys)
        self.assertIn("large:%d" % new, keys)

    def test_weekly_summary_for_last_week(self):
        L.add_transaction(self.c, self.chk, "2026-03-10", -4500, "KROGER #1")
        weekly = [a for a in notify.collect(self.c, notify.get_config(self.c), NOON) if a["kind"] == "weekly"]
        self.assertEqual(weekly[0]["key"], "weekly:2026-03-09")
        self.assertEqual(weekly[0]["title"], "Your week: Mar 9 to Mar 15")
        self.assertIn("Groceries $45.00", weekly[0]["body"])

    def test_events_can_be_turned_off(self):
        L.add_recurring(self.c, "Rent", self.chk, -150000, "monthly", "2026-03-18")
        notify.update_config(self.c, {"events": {"bills": False}})
        self.assertFalse([k for k in self.keys() if k.startswith("bill")])


# ------------------------------------------------------------------ sending

class RunTests(Base):
    def setUp(self):
        super().setUp()
        L.add_recurring(self.c, "Rent", self.chk, -150000, "monthly", "2026-03-18")
        self.sent = []

    def fake_deliver(self, conn, cfg, alerts, profile_name=""):
        self.sent.append([a["title"] for a in alerts])
        return True, []

    def test_needs_a_way_to_reach_you(self):
        r = notify.run(self.c, NOON)
        self.assertEqual((r["status"], r["waiting"]), ("no_channel", 1))

    def test_each_alert_is_sent_once(self):
        notify.update_config(self.c, {"email": {"enabled": True, "to": "me@example.com", "host": "smtp.example.com"}})
        with mock.patch.object(notify, "deliver", self.fake_deliver):
            self.assertEqual(notify.run(self.c, NOON)["sent"], ["Bill due Wed Mar 18: Rent"])
            self.assertEqual(notify.run(self.c, NOON)["sent"], [])
        self.assertEqual(len(self.sent), 1)

    def test_quiet_hours_wait_until_morning(self):
        notify.update_config(self.c, {"email": {"enabled": True, "to": "me@example.com", "host": "smtp.example.com"}})
        with mock.patch.object(notify, "deliver", self.fake_deliver):
            r = notify.run(self.c, NOON.replace(hour=23))
            self.assertEqual((r["status"], r["waiting"]), ("quiet", 1))
            self.assertEqual(notify.run(self.c, NOON.replace(day=17, hour=7, minute=5))["sent"], ["Bill due tomorrow: Rent"])

    def test_failed_delivery_is_retried(self):
        notify.update_config(self.c, {"email": {"enabled": True, "to": "me@example.com", "host": "smtp.example.com"}})
        with mock.patch.object(notify, "deliver", return_value=(False, ["Email: down"])):
            r = notify.run(self.c, NOON)
        self.assertEqual((r["status"], r["errors"]), ("failed", ["Email: down"]))
        with mock.patch.object(notify, "deliver", self.fake_deliver):
            self.assertEqual(len(notify.run(self.c, NOON)["sent"]), 1)

    def test_off_means_off(self):
        notify.update_config(self.c, {"enabled": False})
        self.assertEqual(notify.run(self.c, NOON)["status"], "off")

    @unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography package")
    def test_iphone_delivery_and_removed_phones(self):
        code, decrypt, _ = make_phone()
        notify.add_device(self.c, code)
        got = []
        with mock.patch.object(webpush, "send", lambda d, m: got.append(m)):
            self.assertEqual(len(notify.run(self.c, NOON)["sent"]), 1)
        self.assertEqual(got[0]["url"], "#recurring")
        L.add_recurring(self.c, "Phone", self.chk, -5000, "monthly", "2026-03-17")
        with mock.patch.object(webpush, "send", side_effect=webpush.PushError("gone", gone=True)):
            r = notify.run(self.c, NOON)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(notify.devices(self.c), [])  # the phone unsubscribed: forgotten

    def test_many_alerts_fold_into_one_more(self):
        alerts = [notify._alert("k%d" % i, "x", "Alert %d" % i, "") for i in range(7)]
        shown = []
        with mock.patch.object(notify, "devices", return_value=[{"endpoint": "https://web.push.apple.com/x"}]), \
                mock.patch.object(webpush, "send", lambda d, m: shown.append(m["title"])):
            notify._send_push(self.c, alerts)
        self.assertEqual(shown, ["Alert 0", "Alert 1", "Alert 2", "4 more FinOrganizer alerts"])


class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.messages, self.logins = [], []

        class H(socketserver.StreamRequestHandler):
            def handle(h):
                h.wfile.write(b"220 fake ESMTP\r\n")
                while True:
                    line = h.rfile.readline().decode().strip()
                    if not line:
                        return
                    cmd = line.split(" ")[0].upper()
                    if cmd in ("EHLO", "HELO"):
                        h.wfile.write(b"250-fake\r\n250 AUTH PLAIN\r\n")
                    elif cmd == "AUTH":
                        user, pw = base64.b64decode(line.split(" ")[2]).split(b"\0")[1:]
                        self.logins.append((user.decode(), pw.decode()))
                        h.wfile.write(b"235 ok\r\n" if pw == b"app-pass" else b"535 bad credentials\r\n")
                    elif cmd == "DATA":
                        h.wfile.write(b"354 go\r\n")
                        data = b""
                        while not data.endswith(b"\r\n.\r\n"):
                            data += h.rfile.readline()
                        self.messages.append(data.decode())
                        h.wfile.write(b"250 queued\r\n")
                    elif cmd == "QUIT":
                        h.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        h.wfile.write(b"250 ok\r\n")

        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class EmailTests(Base):
    def setUp(self):
        super().setUp()
        self.smtp = FakeSMTP()
        self.addCleanup(self.smtp.shutdown)
        notify.update_config(self.c, {"password": "app-pass", "email": {
            "enabled": True, "to": "me@example.com", "host": "127.0.0.1", "port": self.smtp.server_address[1],
            "security": "none", "username": "me@example.com"}})

    def test_alerts_arrive_as_one_email(self):
        L.add_recurring(self.c, "Rent", self.chk, -150000, "monthly", "2026-03-18")
        L.add_transaction(self.c, self.chk, "2026-03-14", -60000, "Laptop Store")
        r = notify.run(self.c, NOON)
        self.assertEqual(r["errors"], [])
        self.assertEqual(len(self.smtp.messages), 1)
        msg = self.smtp.messages[0]
        self.assertIn("Subject: 3 FinOrganizer alerts", msg)  # bill, purchase, weekly summary
        self.assertIn("Laptop Store", msg)
        self.assertEqual(self.smtp.logins, [("me@example.com", "app-pass")])

    def test_wrong_password_is_explained(self):
        notify.update_config(self.c, {"password": "normal-password"})
        with self.assertRaisesRegex(ValueError, "app password"):
            notify.send_test(self.c, "email")

    def test_unencrypted_email_only_to_this_computer(self):
        notify.update_config(self.c, {"email": {"host": "smtp.example.com"}})
        with self.assertRaisesRegex(ValueError, "only allowed to a server on this computer"):
            notify.send_test(self.c, "email")


# ------------------------------------------------------- settings & endpoints

class SettingsTests(Base):
    def test_values_are_checked(self):
        for bad in ({"bill_days": 99}, {"large_cents": "lots"}, {"email": {"to": "not an email"}},
                    {"email": {"security": "carrier pigeon"}}, {"email": {"enabled": True, "to": "", "host": ""}}):
            with self.assertRaises(ValueError):
                notify.update_config(self.c, bad)

    def test_password_is_never_shown(self):
        status, state = call(self.app, "PUT", "/api/notifications", {"password": "secret-123", "email": {"username": "me"}})
        self.assertEqual(status, 200)
        self.assertTrue(state["has_password"])
        self.assertNotIn("secret-123", json.dumps(state))

    @unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography package")
    def test_devices_and_keys(self):
        code, _, _ = make_phone()
        status, r = call(self.app, "POST", "/api/notifications/devices", {"code": code})
        self.assertEqual((status, r["name"]), (200, "Test iPhone"))
        status, state = call(self.app, "GET", "/api/notifications")
        self.assertEqual([(d["name"], d["service"]) for d in state["devices"]], [("Test iPhone", "Apple")])
        self.assertNotIn(json.loads(webpush.unb64u(code[len(webpush.CODE_PREFIX):]))["k"], json.dumps(state))
        self.assertEqual(call(self.app, "DELETE", "/api/notifications/devices/" + r["id"])[0], 200)
        self.assertEqual(call(self.app, "GET", "/api/notifications")[1]["devices"], [])

    def test_test_needs_setup_first(self):
        status, r = call(self.app, "POST", "/api/notifications/test", {"channel": "push"})
        self.assertEqual((status, r["error"]), (400, "add an iPhone first"))

    def test_settings_belong_to_each_profile(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        profiles = Profiles(os.path.join(d, "finorganizer.db"))
        sam = profiles.create("Sam")
        c = profiles.open(sam)
        notify.update_config(c, {"enabled": True, "email": {"enabled": True, "to": "sam@example.com", "host": "h"}})
        c.close()
        c = profiles.open("default")
        self.assertFalse(notify.get_config(c)["enabled"])
        c.close()
        with mock.patch.object(notify, "deliver", return_value=(True, [])):
            results = notify.run_all(profiles, NOON)
        self.assertEqual(list(results), [sam])
        self.assertTrue(os.path.exists(notify.log_path(profiles)))


# --------------------------------------------------------------- scheduling

class ScheduleTests(unittest.TestCase):
    def test_windows_task(self):
        xml = schedule.task_xml(["C:\\Program Files\\FinOrganizer.exe", "--notify", "--db", "C:\\data & more\\f.db"])
        root = ET.fromstring(xml.split("?>", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Command", ns).text, "C:\\Program Files\\FinOrganizer.exe")
        self.assertEqual(root.find(".//t:Arguments", ns).text, '"--notify" "--db" "C:\\data & more\\f.db"')
        self.assertEqual(root.find(".//t:Interval", ns).text, "PT4H")
        self.assertEqual(root.find(".//t:StartWhenAvailable", ns).text, "true")

    def test_mac_agent(self):
        p = plistlib.loads(schedule.plist_bytes(["/Applications/FinOrganizer.app/Contents/MacOS/FinOrganizer", "--notify"], "/tmp"))
        self.assertEqual((p["Label"], p["StartInterval"], p["ProgramArguments"][1]), (schedule.LABEL, 14400, "--notify"))

    def test_only_for_the_installed_app(self):
        self.assertFalse(schedule.supported())  # running from source
        self.assertFalse(schedule.status()["supported"])
        with self.assertRaises(OSError):
            schedule.install("x.db")

    def test_installs_when_a_profile_wants_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        profiles = Profiles(os.path.join(d, "finorganizer.db"))
        with mock.patch.object(schedule, "supported", return_value=True), \
                mock.patch.object(schedule, "install") as install, \
                mock.patch.object(schedule, "installed", return_value=True), \
                mock.patch.object(schedule, "uninstall") as uninstall:
            notify.sync_schedule(profiles)
            uninstall.assert_called_once()  # nobody wants it
            c = profiles.open("default")
            notify.update_config(c, {"enabled": True})
            c.close()
            notify.sync_schedule(profiles)
            install.assert_called_once_with(profiles.base_path)


if __name__ == "__main__":
    unittest.main()
