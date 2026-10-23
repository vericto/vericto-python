# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""A local stand-in for the Vericto Runtime API, for the documentation examples.

It answers ``POST /api/v1/proxy/query`` and ``GET /api/v1/meta/config`` with the
response shapes of the real service (status codes, bodies, headers), so the README
and the framework guides can be run end to end without a real key or network
access. It is **not** the Vericto engine: the "rules" below are a handful of string
checks, and their rule codes are illustrative.

* ``DELETE`` / ``UPDATE`` without ``WHERE``: 403 ``BLOCKED`` (``VERICTO-001``,
  critical) with a suggested fix.
* ``DROP TABLE``: 403 ``BLOCKED`` (``VERICTO-010``, critical).
* A ``SELECT`` that reads a column named ``email``: 200 ``FLAGGED``
  (``VERICTO-085``) with ``rewritten_query``, the column masked.
* ``SELECT *`` without ``LIMIT``: 200 ``FLAGGED`` (``VERICTO-030``, medium).
* Anything else: 200 ``ALLOWED``.

Markers in the SQL force the other answers: ``/* stub:monitored */``,
``/* stub:parse_error */`` (200 ``PARSE_ERROR``), ``/* stub:parse_block */`` (403
``VERICTO-PARSE-ERROR``), ``/* stub:sleep=SECONDS */``, ``/* stub:status=503 */``
(any 5xx), ``/* stub:rate_limit */`` and ``/* stub:quota */`` (429). A key equal to
``vtro_revoked`` answers 401; a database id of all zeros answers 403 ``FORBIDDEN``;
a dialect the config does not list answers 400 ``VALIDATION_ERROR``.

Run it on its own with ``python scripts/docs_stub.py [port]``; it prints its URL.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

DIALECTS = ["postgres", "mysql", "oracle", "mssql", "snowflake", "sqlite"]
FORBIDDEN_DATABASE = "00000000-0000-0000-0000-000000000000"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_MARKER = re.compile(r"/\*\s*stub:(\w+)(?:=([\w.]+))?\s*\*/")

Reply = Tuple[int, Dict[str, Any], Dict[str, str]]


def _error(status: int, code: str, message: str, **extra: Any) -> Reply:
    return status, {"error": {"code": code, "message": message, **extra}}, {}


def _allowed(decision: str = "ALLOWED", **fields: Any) -> Reply:
    body: Dict[str, Any] = {
        "decision": decision,
        "rule_id": None,
        "rule_code": None,
        "severity": None,
        "ast_node_path": None,
        "estimated_rows_affected": None,
        "suggested_safe_query": None,
        "parse_error": None,
        "parse_error_kind": None,
        "rewritten_query": None,
        "sensitive_columns": [],
        "access_denied": [],
        "latency_ms": 1.0,
        "event_id": str(uuid.uuid4()),
    }
    body.update(fields)
    return 200, body, {}


def _blocked(rule: str, fix: Optional[str], node: str) -> Reply:
    return (
        403,
        {
            "error": f"Query blocked by rule {rule}",
            "decision": "BLOCKED",
            "rule_code": rule,
            "ast_node_path": node,
            "severity": "critical",
            "suggested_safe_query": fix,
            "rewritten_query": None,
            "sensitive_columns": [],
            "access_denied": [],
            "event_id": str(uuid.uuid4()),
        },
        {},
    )


def evaluate(sql: str) -> Reply:
    """The stub's verdict for one statement (markers first, then the string rules)."""
    markers = {m.group(1): m.group(2) for m in _MARKER.finditer(sql)}
    if "sleep" in markers:
        time.sleep(float(markers["sleep"] or 1))
    if "status" in markers:
        status = int(markers["status"] or 503)
        return _error(status, "INTERNAL_ERROR", "Stub server error")
    if "rate_limit" in markers:
        return _error(
            429,
            "RATE_LIMIT_EXCEEDED",
            "Too many evaluations per minute for this API key. Retry shortly.",
            retry_after_seconds=60,
        )
    if "quota" in markers:
        status, body, _ = _error(
            429,
            "QUOTA_EXCEEDED",
            "Monthly query quota exceeded.",
            used=1000,
            limit=1000,
            remaining=0,
            upgrade_url="https://app.vericto.com/dashboard/billing",
            resets_at="2026-11-01T00:00:00.000Z",
            retry_after_seconds=3600,
        )
        return status, body, {"Retry-After": "3600"}
    if "monitored" in markers:
        return _allowed("MONITORED", rule_code="VERICTO-030", severity="low")
    if "parse_error" in markers:
        return _allowed("PARSE_ERROR", parse_error="syntax error at or near", parse_error_kind="syntax")
    if "parse_block" in markers:
        return (
            403,
            {
                "error": "Query blocked: VERICTO-PARSE-ERROR: syntax error at or near",
                "decision": "BLOCKED",
                "rule_code": "VERICTO-PARSE-ERROR",
                "ast_node_path": None,
                "severity": "medium",
                "suggested_safe_query": None,
                "parse_error": "syntax error at or near",
                "event_id": str(uuid.uuid4()),
            },
            {},
        )

    text = _MARKER.sub(" ", sql).strip().rstrip(";").strip()
    upper = " ".join(text.upper().split())
    if re.match(r"^(DELETE|UPDATE)\b", upper) and " WHERE " not in f"{upper} ":
        return _blocked("VERICTO-001", f"{text} WHERE <condition>", upper.split()[0].title())
    if re.match(r"^DROP\s+TABLE\b", upper):
        return _blocked("VERICTO-010", None, "Drop")
    if upper.startswith("SELECT") and re.search(r"\bemail\b", text, re.IGNORECASE):
        rewritten = re.sub(r"(?<![\w.'\"])email\b(?!\s*=)", "'***' AS email", text, count=1, flags=re.IGNORECASE)
        return _allowed(
            "FLAGGED",
            rule_code="VERICTO-085",
            severity="low",
            rewritten_query=rewritten,
            sensitive_columns=[{"table": "customers", "column": "email", "action": "mask"}],
        )
    if re.match(r"^SELECT\s+\*\s+FROM\b", upper) and " LIMIT " not in f"{upper} ":
        return _allowed("FLAGGED", rule_code="VERICTO-030", severity="medium")
    return _allowed()


class _Handler(BaseHTTPRequestHandler):
    server: StubServer

    def log_message(self, format: str, *args: Any) -> None:  # quiet; never logs the key
        return

    def _send(self, status: int, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/v1/meta/config":
            self._send(200, {"dialects": [{"id": d, "label": d} for d in self.server.dialects]})
        else:
            self._send(*_error(404, "NOT_FOUND", "Not found")[:2])

    def do_POST(self) -> None:
        if self.path != "/api/v1/proxy/query":
            self._send(*_error(404, "NOT_FOUND", "Not found")[:2])
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        key = self.headers.get("X-API-Key") or ""
        auth = self.headers.get("Authorization") or ""
        if not key and auth.startswith("Bearer "):
            key = auth[7:]
        if not key.startswith("vtro_") or key == "vtro_revoked":
            self._send(*_error(401, "UNAUTHORIZED", "Invalid or missing API key")[:2])
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            self._send(*_error(400, "INVALID_JSON_BODY", "The request body is not valid JSON.")[:2])
            return
        details: List[Dict[str, str]] = []
        if not isinstance(body, dict):
            body = {}
            details.append({"field": "", "message": "Expected object"})
        if not isinstance(body.get("query"), str) or not body.get("query"):
            details.append({"field": "query", "message": "Required"})
        if not isinstance(body.get("database_id"), str) or not _UUID.match(body.get("database_id", "")):
            details.append({"field": "database_id", "message": "Invalid uuid"})
        if body.get("dialect") not in self.server.dialects:
            details.append({"field": "dialect", "message": f"Must be one of: {', '.join(self.server.dialects)}"})
        if details:
            self._send(400, {"error": {"code": "VALIDATION_ERROR", "message": "Validation failed", "details": details}})
            return
        if body["database_id"] == FORBIDDEN_DATABASE:
            self._send(*_error(403, "FORBIDDEN", "This API key is not authorized for that workspace or database")[:2])
            return
        with self.server.lock:
            self.server.requests.append({"query": body["query"], "dialect": body["dialect"]})
        status, reply, headers = evaluate(body["query"])
        if status in (200, 403) and "decision" in reply:
            headers = {**headers, "X-Quota-Limit": "unlimited", "X-Quota-Remaining": "unlimited"}
        self._send(status, reply, headers)


class StubServer(ThreadingHTTPServer):
    """The stub, bound to 127.0.0.1. ``requests`` records what was evaluated (never the key)."""

    daemon_threads = True

    def __init__(self, port: int = 0, dialects: Optional[List[str]] = None) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.dialects = list(dialects or DIALECTS)
        self.requests: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> StubServer:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


def main(argv: List[str]) -> int:
    server = StubServer(int(argv[1]) if len(argv) > 1 else 0)
    print(server.url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
