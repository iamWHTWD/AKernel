"""Transient read failures must not turn into empty inventory or replay writes."""

import errno
import http.client
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from email.utils import formatdate
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from akernel_upgrade.clients import HTTP, Inventory
from akernel_upgrade.errors import Conflict, UnknownOutcome, UpgradeError


def unavailable(status=503, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://public/resources", status, "unavailable",
                                  headers, io.BytesIO(b"not an inventory"))


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.sleeps = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        token = Path(self.temp.name) / "token"
        token.write_text("test-token")
        self.http = HTTP("http://management", "https://public", str(token))
        for name, replacement in [("monotonic", lambda: self.now),
                                  ("time", lambda: 1700000000 + self.now),
                                  ("sleep", self.sleep)]:
            mock = patch("akernel_upgrade.clients.time." + name, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def responses(self, *values):
        self.http.opener.open = Mock(side_effect=[
            io.BytesIO(json.dumps(value).encode()) if isinstance(value, dict) else value
            for value in values])

    def test_resources_503_recovers_without_losing_identity_or_authentication(self):
        error = unavailable()
        item = {"id": "node-a", "status": 0,
                "capacity": {"CPU": 8000, "Memory": 8192},
                "allocatable": {"CPU": 3000, "Memory": 2048}}
        self.responses(error, {"items": [item]})
        self.assertEqual(Inventory(self.http).resources(), {"node-a": item})
        requests = [call.args[0] for call in self.http.opener.open.call_args_list]
        self.assertIs(requests[0], requests[1])
        self.assertEqual(requests[0].full_url, "https://public/api/environment/v1/resources")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-token")
        self.assertTrue(error.closed)
        self.assertEqual(self.sleeps, [0.5])

    def test_internal_inventory_retries_without_public_token(self):
        self.responses(unavailable(), {"count": 0, "instanceInfos": [], "epoch": 1, "revision": 2})
        self.assertEqual(Inventory(self.http).instances(), [])
        self.assertEqual(self.http.opener.open.call_count, 2)
        for call in self.http.opener.open.call_args_list:
            self.assertIsNone(call.args[0].get_header("Authorization"))

    def test_persistent_503_fails_closed_with_bounded_attempts(self):
        errors = [unavailable() for _ in range(4)]
        self.responses(*errors)
        with self.assertRaisesRegex(UpgradeError, "HTTP read failed \\(503\\)"):
            Inventory(self.http).resources()
        self.assertEqual(self.http.opener.open.call_count, 4)
        self.assertEqual(self.sleeps, [0.5, 1, 2])
        self.assertTrue(all(error.closed for error in errors))

    def test_timeout_is_shared_by_attempts_and_backoff(self):
        self.http.timeout = 5
        timeouts = []

        def fail(request, *, timeout):
            timeouts.append(timeout)
            self.now += timeout if len(timeouts) == 2 else 3
            raise unavailable()

        self.http.opener.open = fail
        with self.assertRaises(UpgradeError):
            self.http.request("GET", "/status")
        self.assertEqual(timeouts, [5, 1.5])
        self.assertEqual(self.sleeps, [0.5])
        self.assertEqual(self.now, 5)

    def test_retry_after_honored_for_delta_and_http_date(self):
        for header in ["2", formatdate(1700000002, usegmt=True)]:
            with self.subTest(header=header):
                self.now = 0
                self.sleeps.clear()
                self.responses(unavailable(retry_after=header), {"ok": True})
                self.assertEqual(self.http.request("GET", "/status"), {"ok": True})
                self.assertEqual(self.sleeps, [2])

    def test_retry_after_beyond_budget_does_not_retry_early(self):
        for header in ("60", "9" * 5000):
            self.responses(unavailable(retry_after=header))
            with self.assertRaises(UpgradeError):
                self.http.request("GET", "/status")
            self.assertEqual(self.http.opener.open.call_count, 1)
            self.assertEqual(self.sleeps, [])

    def test_bad_or_past_retry_after_uses_bounded_backoff(self):
        for header in ["garbage", "-1", "NaN", "inf", "1.5",
                       formatdate(1699999990, usegmt=True)]:
            with self.subTest(header=header[:30]):
                self.now = 0
                self.sleeps.clear()
                self.responses(unavailable(retry_after=header), {"ok": True})
                self.assertEqual(self.http.request("GET", "/status"), {"ok": True})
                self.assertEqual(self.sleeps, [0.5])

    def test_sleep_overrun_does_not_begin_another_attempt(self):
        self.responses(unavailable(), {"ok": True})
        with patch("akernel_upgrade.clients.time.sleep", lambda _: setattr(self, "now", 31)):
            with self.assertRaises(UpgradeError):
                self.http.request("GET", "/status")
        self.assertEqual(self.http.opener.open.call_count, 1)

    def test_only_transient_http_statuses_are_retried(self):
        for status in (408, 429, 502, 503, 504):
            with self.subTest(status=status):
                self.responses(unavailable(status), {"ok": True})
                self.assertEqual(self.http.request("GET", "/status"), {"ok": True})
                self.assertEqual(self.http.opener.open.call_count, 2)
        for status in (301, 400, 401, 403, 404, 405, 409, 422, 500, 501):
            with self.subTest(status=status):
                self.responses(unavailable(status))
                with self.assertRaises(Conflict if status == 409 else UpgradeError):
                    self.http.request("GET", "/status")
                self.assertEqual(self.http.opener.open.call_count, 1)

    def test_transport_disconnects_and_timeouts_retry_get(self):
        for error in [TimeoutError(), ConnectionResetError(),
                      http.client.RemoteDisconnected(),
                      urllib.error.URLError(OSError(errno.ECONNREFUSED, "refused"))]:
            with self.subTest(error=type(error).__name__):
                self.responses(error, {"ok": True})
                self.assertEqual(self.http.request("GET", "/status"), {"ok": True})
                self.assertEqual(self.http.opener.open.call_count, 2)

    def test_partial_response_is_discarded_and_retry_is_whole_read(self):
        class InterruptedBody(io.BytesIO):
            def read(self, limit):
                raise http.client.IncompleteRead(b'{"items": [', 100)

        first = InterruptedBody()
        self.responses(first, {"items": []})
        self.assertEqual(Inventory(self.http).resources(), {})
        self.assertTrue(first.closed)
        self.assertEqual(self.http.opener.open.call_count, 2)

    def test_tls_and_permanent_transport_errors_fail_without_retry(self):
        for error in [ssl.SSLCertVerificationError("invalid certificate"),
                      urllib.error.URLError(ssl.SSLCertVerificationError("invalid certificate")),
                      urllib.error.URLError("unknown URL type"),
                      PermissionError(errno.EACCES, "permission denied")]:
            with self.subTest(error=type(error).__name__):
                self.responses(error)
                with self.assertRaises(UpgradeError):
                    self.http.request("GET", "/status")
                self.assertEqual(self.http.opener.open.call_count, 1)

    def test_invalid_success_body_is_not_retried_or_treated_as_empty(self):
        for data in (b"invalid json", b"[]", b"x" * (8 * 1024 * 1024 + 1)):
            self.responses(io.BytesIO(data))
            with self.assertRaises(UpgradeError):
                Inventory(self.http).resources()
            self.assertEqual(self.http.opener.open.call_count, 1)

    def test_mutations_are_not_replayed_after_ambiguous_failure(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for error in (unavailable(), TimeoutError(), http.client.IncompleteRead(b"", 10)):
                with self.subTest(method=method, error=type(error).__name__):
                    self.responses(error, {"ok": True})
                    with self.assertRaises(UnknownOutcome):
                        self.http.request(method, "/mutation", {})
                    self.assertEqual(self.http.opener.open.call_count, 1)
                    self.assertEqual(self.http.opener.open.call_args.kwargs["timeout"], 660)


if __name__ == "__main__":
    unittest.main()
