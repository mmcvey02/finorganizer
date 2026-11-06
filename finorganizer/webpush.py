"""Web Push: notifications to the FinOrganizer home-screen app on an iPhone (or any browser).

The phone app subscribes to push with a key pair it makes itself, and hands the computer
a *pairing code* holding everything needed to reach it: the push service address, the
subscription's public keys, and the private key that proves messages come from us.
FinOrganizer on the computer then encrypts each message for that phone (RFC 8291) and
signs the request (VAPID, RFC 8292). Apple's push service only ever sees ciphertext.

Needs the ``cryptography`` package (bundled with the desktop app).
"""

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from .net import describe_ssl_error, ssl_context

CODE_PREFIX = "FINPUSH1."
SUBJECT = "https://github.com/mmcvey02/finorganizer"
# Push services of the browsers that can run the app. Pairing codes may only point here.
PUSH_HOSTS = ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com",
              "notify.windows.com", "push.services.mozilla.com")
TIMEOUT = 30


class PushError(Exception):
    """Sending failed. ``gone`` means the phone unsubscribed: forget the device."""

    def __init__(self, message, gone=False):
        super().__init__(message)
        self.gone = gone


_available = None


def available():
    """Whether the cryptography package is installed and actually loads (a broken install
    can fail with errors that aren't ImportError). Checked once."""
    global _available
    if _available is None:
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
            _available = ec.SECP256R1 is not None
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            _available = False
    return _available


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text):
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------- pairing codes

def parse_pairing_code(code):
    """Check a pairing code from the phone app; returns the device dict to store."""
    text = "".join((code or "").split())
    if text.startswith(CODE_PREFIX):
        text = text[len(CODE_PREFIX):]
    try:
        data = json.loads(unb64u(text))
        endpoint, p256dh, auth, key = data["e"], data["p"], data["a"], data["k"]
        if len(unb64u(p256dh)) != 65 or len(unb64u(auth)) < 16 or len(unb64u(key)) != 32:
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise ValueError("that isn't a FinOrganizer pairing code. On the iPhone, open FinOrganizer from the "
                         "Home Screen, go to Settings, Notifications, and copy the whole code")
    parts = urllib.parse.urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        raise ValueError("that pairing code points somewhere other than a known push service")
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth, "key": key,
            "name": str(data.get("n") or "iPhone")[:60]}


# ------------------------------------------------------------------ crypto

def _private_key(d_b64):
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.derive_private_key(int.from_bytes(unb64u(d_b64), "big"), ec.SECP256R1())


def _raw_public(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(plaintext, p256dh, auth, salt=None, sender_key=None, record_size=4096):
    """Encrypt a message for one subscription (RFC 8291, "aes128gcm" encoding).

    ``salt`` and ``sender_key`` (a private key) are only passed by tests; normally a fresh
    random salt and a one-off key pair are used for every message.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    ua_public = unb64u(p256dh)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    sender_key = sender_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(sender_key)
    salt = salt or os.urandom(16)
    shared = sender_key.exchange(ec.ECDH(), ua_key)
    # HKDF (RFC 5869) with SHA-256, written out: each output here is a single block.
    prk_key = _hmac(unb64u(auth), shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(plaintext) + 1 + 16 > record_size:
        raise ValueError("message too long for one push")
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    return salt + struct.pack("!IB", record_size, len(as_public)) + as_public + ciphertext


def vapid_authorization(endpoint, key_b64, now=None):
    """The Authorization header proving the message comes from the key the phone chose."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    parts = urllib.parse.urlsplit(endpoint)
    claims = {"aud": "%s://%s" % (parts.scheme, parts.netloc),
              "exp": int((now or time.time()) + 12 * 3600), "sub": SUBJECT}
    signing_input = (b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." +
                     b64u(json.dumps(claims, separators=(",", ":")).encode()))
    key = _private_key(key_b64)
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    token = signing_input + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return "vapid t=%s, k=%s" % (token, b64u(_raw_public(key)))


# ------------------------------------------------------------------ sending

def send(device, message, ttl=24 * 3600, urgency="normal"):
    """Send one notification. ``message`` is {"title", "body", "url"?, "tag"?}."""
    if not available():
        raise PushError("iPhone notifications need the cryptography package; use the FinOrganizer app "
                        "download, or run: pip install cryptography")
    body = encrypt(json.dumps(message).encode("utf-8"), device["p256dh"], device["auth"])
    req = urllib.request.Request(device["endpoint"], data=body, method="POST", headers={
        "Authorization": vapid_authorization(device["endpoint"], device["key"]),
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": str(int(ttl)), "Urgency": urgency})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_context()) as res:
            return res.status
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise PushError("the phone turned notifications off or removed the app", gone=True)
        detail = e.read(300).decode("utf-8", "replace").strip()
        raise PushError("the push service refused the message (HTTP %d%s)" % (e.code, ": " + detail if detail else ""))
    except urllib.error.URLError as e:
        raise PushError("couldn't reach the push service: %s" % describe_ssl_error(e))
