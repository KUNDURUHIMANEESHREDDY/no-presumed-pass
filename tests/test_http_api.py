from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
from threading import Thread
from http.server import ThreadingHTTPServer
import unittest

from no_presumed_pass.http_api import MAX_BODY_BYTES, make_handler
from no_presumed_pass import EvidenceStore, VerdictStatus

TOKEN = "test-token-that-is-long-enough-for-local-api"


class EvidenceHTTPAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "evidence.sqlite3"
        self.store = EvidenceStore(self.database)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, TOKEN))
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.tempdir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: bytes | dict | None = None,
        *,
        token: str | None = TOKEN,
        content_type: str | None = "application/json",
    ) -> tuple[int, dict | None, dict[str, str]]:
        if isinstance(payload, dict):
            body = json.dumps(payload).encode("utf-8")
        else:
            body = payload
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if content_type is not None:
            headers["Content-Type"] = content_type
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            parsed = json.loads(raw) if raw else None
            return response.status, parsed, {key.lower(): value for key, value in response.getheaders()}
        finally:
            connection.close()

    @staticmethod
    def evidence(evidence_id: str, criterion: str, outcome: str = "PASS") -> dict[str, str]:
        return {
            "evidence_id": evidence_id,
            "criterion": criterion,
            "outcome": outcome,
            "source": "unit-test",
            "observed_at": "2026-10-24T00:00:00Z",
        }

    def test_health_is_minimal_and_does_not_require_authentication(self) -> None:
        status, body, headers = self.request("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})
        self.assertEqual(headers["cache-control"], "no-store")

    def test_readiness_reflects_storage_availability_without_leaking_details(self) -> None:
        status, body, _ = self.request("GET", "/readyz", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ready"})

        self.store.close()
        status, body, _ = self.request("GET", "/readyz", token=None)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "not_ready")
        self.assertNotIn("sqlite", json.dumps(body).lower())
        self.assertNotIn(str(self.database), json.dumps(body))

    def test_evidence_read_and_write_require_bearer_token(self) -> None:
        status, body, _ = self.request("POST", "/v1/evidence", self.evidence("e-1", "build"), token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertEqual(self.store.list_evidence(), ())

        status, _, _ = self.request("POST", "/v1/evidence", self.evidence("e-1", "build"), token="wrong-token")
        self.assertEqual(status, 401)

    def test_evidence_to_verdict_round_trip(self) -> None:
        status, created, _ = self.request("POST", "/v1/evidence", self.evidence("build-1", "build"))
        self.assertEqual(status, 201)
        self.assertEqual(created["evidence"]["evidence_id"], "build-1")

        status, _, _ = self.request("POST", "/v1/evidence", self.evidence("test-1", "tests"))
        self.assertEqual(status, 201)

        status, response, _ = self.request("GET", "/v1/verdict?criterion=build&criterion=tests")
        self.assertEqual(status, 200)
        self.assertEqual(response["verdict"]["status"], VerdictStatus.PASS.value)
        self.assertEqual(set(response["verdict"]["evidence_ids"]), {"build-1", "test-1"})

    def test_evidence_listing_is_bounded_and_pages_are_deterministic(self) -> None:
        for evidence_id, criterion in (
            ("page-1", "build"),
            ("page-2", "build"),
            ("page-3", "security"),
        ):
            status, _, _ = self.request(
                "POST", "/v1/evidence", self.evidence(evidence_id, criterion)
            )
            self.assertEqual(status, 201)

        status, first, _ = self.request("GET", "/v1/evidence?limit=2&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["evidence_id"] for item in first["evidence"]],
            ["page-1", "page-2"],
        )
        self.assertEqual(
            first["pagination"],
            {"limit": 2, "offset": 0, "has_more": True, "next_offset": 2},
        )

        status, second, _ = self.request("GET", "/v1/evidence?limit=2&offset=2")
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["evidence_id"] for item in second["evidence"]],
            ["page-3"],
        )
        self.assertEqual(
            second["pagination"],
            {"limit": 2, "offset": 2, "has_more": False, "next_offset": None},
        )

        status, filtered, _ = self.request(
            "GET", "/v1/evidence?criterion=build&limit=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["criterion"] for item in filtered["evidence"]], ["build"])
        self.assertTrue(filtered["pagination"]["has_more"])

    def test_evidence_pagination_rejects_ambiguous_or_unbounded_values(self) -> None:
        for query in (
            "limit=0",
            "limit=101",
            "limit=1&limit=2",
            "limit=1.5",
            "offset=-1",
            "offset=1000001",
            "offset=1&offset=2",
            "limit=",
        ):
            with self.subTest(query=query):
                status, body, _ = self.request("GET", f"/v1/evidence?{query}")
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_query")

    def test_verdict_rejects_excessive_query_parameters_before_parsing_all(self) -> None:
        query = "&".join("criterion=check" for _ in range(104))
        status, body, _ = self.request("GET", f"/v1/verdict?{query}")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_query")

    def test_missing_required_evidence_is_indeterminate(self) -> None:
        status, _, _ = self.request("POST", "/v1/evidence", self.evidence("build-1", "build"))
        self.assertEqual(status, 201)
        status, response, _ = self.request("GET", "/v1/verdict?criterion=build&criterion=security")
        self.assertEqual(status, 200)
        self.assertEqual(response["verdict"]["status"], VerdictStatus.INDETERMINATE.value)
        self.assertEqual(response["verdict"]["missing_criteria"], ["security"])

    def test_duplicate_evidence_id_conflicts_without_overwriting(self) -> None:
        self.request("POST", "/v1/evidence", self.evidence("same-id", "security", "PASS"))
        status, response, _ = self.request("POST", "/v1/evidence", self.evidence("same-id", "security", "FAIL"))
        self.assertEqual(status, 409)
        self.assertEqual(response["error"]["code"], "evidence_conflict")
        self.assertEqual(self.store.list_evidence()[0].outcome.value, "PASS")

    def test_duplicate_json_keys_and_naive_timestamps_are_rejected(self) -> None:
        duplicate = b'{"evidence_id":"x","evidence_id":"y","criterion":"build","outcome":"PASS","source":"ci","observed_at":"2026-10-24T00:00:00Z"}'
        status, _, _ = self.request("POST", "/v1/evidence", duplicate)
        self.assertEqual(status, 400)

        naive = self.evidence("naive", "build")
        naive["observed_at"] = "2026-10-24T00:00:00"
        status, _, _ = self.request("POST", "/v1/evidence", naive)
        self.assertEqual(status, 400)

    def test_extremely_long_content_length_is_rejected_without_integer_conversion(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest("POST", "/v1/evidence")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "9" * 5000)
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 413)
            self.assertEqual(body["error"]["code"], "payload_too_large")
        finally:
            connection.close()

    def test_duplicate_authorization_headers_are_rejected(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest("POST", "/v1/evidence")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Authorization", "Bearer different-token-that-is-also-long")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "2")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 401)
            self.assertEqual(body["error"]["code"], "unauthorized")
            self.assertEqual(self.store.list_evidence(), ())
        finally:
            connection.close()

    def test_duplicate_content_type_headers_are_rejected(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest("POST", "/v1/evidence")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Type", "text/plain")
            connection.putheader("Content-Length", "2")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 400)
            self.assertEqual(body["error"]["code"], "invalid_content_type")
            self.assertEqual(self.store.list_evidence(), ())
        finally:
            connection.close()

    def test_duplicate_content_length_headers_are_rejected(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest("POST", "/v1/evidence")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "2")
            connection.putheader("Content-Length", "3")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 400)
            self.assertEqual(body["error"]["code"], "invalid_content_length")
        finally:
            connection.close()

    def test_body_limit_and_media_type_are_enforced(self) -> None:
        status, body, _ = self.request(
            "POST", "/v1/evidence", b"x" * (MAX_BODY_BYTES + 1)
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "payload_too_large")

        status, body, _ = self.request(
            "POST", "/v1/evidence", b"{}", content_type="text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

    def test_unknown_route_and_unsupported_method_are_rejected(self) -> None:
        status, _, _ = self.request("GET", "/debug")
        self.assertEqual(status, 404)
        status, _, _ = self.request("DELETE", "/v1/evidence")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
