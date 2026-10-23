"""Loopback-only authenticated HTTP adapter for the evidence store.

This adapter is a local integration surface, not a public multi-tenant service.
It intentionally binds only to 127.0.0.1 and requires a strong bearer token for
all evidence reads and writes. Health checks reveal no database details.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .store import EvidenceStore
from .verdict import EvidenceOutcome, EvidenceRecord, Verdict, VerdictStatus

MAX_BODY_BYTES = 64 * 1024
MAX_CRITERIA = 100
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_PAGE_OFFSET = 1_000_000
_MIN_TOKEN_LENGTH = 32
_LOG = logging.getLogger("no_presumed_pass.http")


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _page_integer(
    query: dict[str, list[str]],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    values = query.get(name)
    if values is None:
        return default
    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
        raise ValueError(f"{name} must be supplied once as a decimal integer")
    value = int(values[0])
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def _record_payload(record: EvidenceRecord) -> dict[str, str]:
    return {
        "evidence_id": record.evidence_id,
        "criterion": record.criterion,
        "outcome": record.outcome.value,
        "source": record.source,
        "observed_at": record.observed_at.isoformat(),
    }


def _verdict_payload(verdict: Verdict) -> dict[str, Any]:
    return {
        "status": verdict.status.value,
        "required_criteria": list(verdict.required_criteria),
        "missing_criteria": list(verdict.missing_criteria),
        "failed_criteria": list(verdict.failed_criteria),
        "unmatched_criteria": list(verdict.unmatched_criteria),
        "evidence_ids": list(verdict.evidence_ids),
        "reason": verdict.reason,
    }


def make_handler(store: EvidenceStore, token: str) -> type[BaseHTTPRequestHandler]:
    """Build an isolated handler around a store and a configured bearer token."""
    if not isinstance(token, str) or len(token) < _MIN_TOKEN_LENGTH:
        raise ValueError(f"API token must contain at least {_MIN_TOKEN_LENGTH} characters")

    class EvidenceAPIHandler(BaseHTTPRequestHandler):
        server_version = "NoPresumedPass"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, _format: str, *_args: Any) -> None:
            # Never log request headers, bodies, query values, or bearer tokens.
            _LOG.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "method": self.command,
                        "route": urlsplit(self.path).path,
                        "status": getattr(self, "_response_status", 0),
                    },
                    separators=(",", ":"),
                )
            )

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            self._response_status = status
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._send_json(status, {"error": {"code": code, "message": message}})

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            scheme, separator, supplied = header.partition(" ")
            valid = (
                separator == " "
                and scheme.lower() == "bearer"
                and bool(supplied)
                and hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))
            )
            if not valid:
                self._error(401, "unauthorized", "A valid bearer token is required.")
            return valid

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            if parsed.path not in {"/v1/evidence", "/v1/verdict"}:
                self._error(404, "not_found", "Route not found.")
                return
            if not self._authorized():
                return

            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
            allowed = (
                {"criterion", "limit", "offset"}
                if parsed.path == "/v1/evidence"
                else {"criterion"}
            )
            if set(query) - allowed:
                self._error(400, "invalid_query", "Unsupported query parameter.")
                return

            if parsed.path == "/v1/evidence":
                values = query.get("criterion")
                if values is not None and (len(values) != 1 or not values[0].strip()):
                    self._error(400, "invalid_query", "criterion must be supplied at most once and be non-empty.")
                    return
                try:
                    limit = _page_integer(
                        query, "limit", DEFAULT_PAGE_SIZE, minimum=1, maximum=MAX_PAGE_SIZE
                    )
                    offset = _page_integer(
                        query, "offset", 0, minimum=0, maximum=MAX_PAGE_OFFSET
                    )
                    records = store.list_evidence(
                        values[0] if values else None,
                        limit=limit + 1,
                        offset=offset,
                    )
                except ValueError:
                    self._error(
                        400,
                        "invalid_query",
                        f"Use limit=1..{MAX_PAGE_SIZE} and offset=0..{MAX_PAGE_OFFSET}; each may appear once.",
                    )
                    return
                has_more = len(records) > limit
                page = records[:limit]
                self._send_json(
                    200,
                    {
                        "evidence": [_record_payload(item) for item in page],
                        "pagination": {
                            "limit": limit,
                            "offset": offset,
                            "has_more": has_more,
                            "next_offset": offset + limit if has_more else None,
                        },
                    },
                )
                return

            criteria = query.get("criterion", [])
            if not criteria or len(criteria) > MAX_CRITERIA:
                self._error(400, "invalid_query", f"Supply between 1 and {MAX_CRITERIA} criterion parameters.")
                return
            try:
                verdict = store.derive(criteria)
            except ValueError:
                self._error(400, "invalid_query", "Criteria must be non-empty, trimmed, and unique.")
                return
            self._send_json(200, {"verdict": _verdict_payload(verdict)})

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/v1/evidence":
                self._error(404, "not_found", "Route not found.")
                return
            if not self._authorized():
                return

            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._error(415, "unsupported_media_type", "Content-Type must be application/json.")
                return
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._error(411, "length_required", "Content-Length is required.")
                self.close_connection = True
                return
            try:
                length = int(raw_length)
            except ValueError:
                length = -1
            if length < 0:
                self._error(400, "invalid_content_length", "Content-Length must be a non-negative integer.")
                self.close_connection = True
                return
            if length > MAX_BODY_BYTES:
                self._error(413, "payload_too_large", "Request body exceeds the 64 KiB limit.")
                self.close_connection = True
                return
            if length == 0:
                self._error(400, "invalid_json", "A JSON evidence object is required.")
                return

            raw_body = self.rfile.read(length)
            if len(raw_body) != length:
                self._error(400, "invalid_body", "Request body ended before Content-Length bytes were read.")
                self.close_connection = True
                return
            try:
                payload = json.loads(
                    raw_body.decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicate_keys,
                    parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON constant")),
                )
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                expected = {"evidence_id", "criterion", "outcome", "source", "observed_at"}
                if set(payload) != expected:
                    raise ValueError("JSON body has missing or unsupported fields")
                timestamp = payload["observed_at"]
                if not isinstance(timestamp, str):
                    raise ValueError("observed_at must be an ISO-8601 string")
                observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                record = EvidenceRecord(
                    evidence_id=payload["evidence_id"],
                    criterion=payload["criterion"],
                    outcome=EvidenceOutcome(payload["outcome"]),
                    source=payload["source"],
                    observed_at=observed_at,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError, ValueError):
                self._error(
                    400,
                    "invalid_evidence",
                    "Evidence must contain valid IDs, criterion, PASS/FAIL outcome, source, and timezone-aware ISO-8601 timestamp.",
                )
                return

            try:
                store.append(record)
            except ValueError:
                self._error(409, "evidence_conflict", "The evidence ID already exists.")
                return
            except Exception:
                # Do not disclose database paths or internal exceptions to clients.
                self._error(500, "storage_error", "Evidence could not be persisted.")
                return
            self._send_json(201, {"evidence": _record_payload(record)})

        def _method_not_allowed(self) -> None:
            # Set the status before send_response(), which invokes request logging.
            self._response_status = 405
            self.send_response(405)
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_DELETE = _method_not_allowed

    return EvidenceAPIHandler


def serve(database: str | Path, token: str, port: int = 8765) -> None:
    """Serve the local API on loopback only; never expose it on a public interface."""
    store = EvidenceStore(database)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, token))
    server.daemon_threads = True
    try:
        _LOG.info(json.dumps({"event": "server_started", "host": "127.0.0.1", "port": port}))
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the loopback-only evidence API.")
    parser.add_argument("--database", default="evidence.sqlite3", help="SQLite database path.")
    parser.add_argument("--port", type=int, default=8765, help="Loopback port (default: 8765).")
    args = parser.parse_args()
    token = os.environ.get("NO_PRESUMED_PASS_API_TOKEN", "")
    if len(token) < _MIN_TOKEN_LENGTH:
        parser.error("set NO_PRESUMED_PASS_API_TOKEN to a random secret of at least 32 characters")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    serve(args.database, token, args.port)


if __name__ == "__main__":
    main()
