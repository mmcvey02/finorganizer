import ssl
import unittest
import urllib.error
from unittest import mock

from finorganizer import net


class NetTests(unittest.TestCase):
    def setUp(self):
        net._context = None

    def tearDown(self):
        net._context = None

    def test_context_always_verifies_certificates(self):
        ctx = net.ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertIs(net.ssl_context(), ctx)  # created once

    def test_falls_back_when_truststore_is_missing(self):
        with mock.patch.dict("sys.modules", {"truststore": None}):
            ctx = net.ssl_context()
        self.assertIn(net.SOURCE, ("certifi", "python-default"))
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_certificate_errors_are_explained(self):
        err = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] nope"))
        self.assertIn("couldn't be verified", net.describe_ssl_error(err))
        self.assertEqual(net.describe_ssl_error(urllib.error.URLError("timed out")), "timed out")


if __name__ == "__main__":
    unittest.main()
