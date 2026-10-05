"""TLS settings shared by every outgoing HTTPS request (SimpleFIN, update checks).

Python's default certificate lookup works on Windows and most Linux systems, but the
python.org builds used for the macOS app carry no certificate store of their own,
so a packaged Mac app would reject every HTTPS server ("CERTIFICATE_VERIFY_FAILED").
We therefore prefer, in order:

1. ``truststore``: verify with the operating system's own trust store (macOS
   Keychain, Windows certificate store), exactly like a browser would;
2. ``certifi``: Mozilla's bundled list of trusted root certificates;
3. Python's defaults (fine when running from source on most systems).

Certificate checking is never turned off.
"""

import ssl

_context = None
SOURCE = "python-default"


def ssl_context():
    """A verifying SSLContext, created once and reused."""
    global _context, SOURCE
    if _context is not None:
        return _context
    try:
        import truststore
        _context, SOURCE = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "system"
        return _context
    except Exception:  # not installed, or unsupported on this OS/Python
        pass
    try:
        import certifi
        _context, SOURCE = ssl.create_default_context(cafile=certifi.where()), "certifi"
        return _context
    except Exception:
        pass
    _context = ssl.create_default_context()
    return _context


def describe_ssl_error(err):
    """Turn certificate failures into an actionable message."""
    text = str(getattr(err, "reason", err))
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return ("the secure connection couldn't be verified (%s). If you're on a work or school "
                "network that inspects traffic, try another network" % text)
    return text
