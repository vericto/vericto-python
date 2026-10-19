# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest
import urllib3
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.conftest import API_KEY, DATABASE_ID, FakeVericto, blocked
from vericto import (
    Decision,
    RuntimeApiEvaluator,
    Vericto,
    VerictoConfigError,
    VerictoFailOpenWarning,
    VerictoQuotaExceeded,
    VerictoUnavailable,
    VerictoUnsupportedDialect,
    __version__,
)
from vericto.runtime_api import _retry_after


def make(url: str, **kwargs: object) -> RuntimeApiEvaluator:
    return RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, api_url=url, backoff=0.0, **kwargs)  # type: ignore[arg-type]


def test_allowed_request_shape(fake: FakeVericto, evaluator: RuntimeApiEvaluator) -> None:
    verdict = evaluator.evaluate("SELECT 1", "snowflake")
    assert verdict.decision is Decision.ALLOWED
    assert verdict.event_id == "evt-allowed"
    sent = fake.requests[0]
    assert sent["query"] == "SELECT 1"
    assert sent["dialect"] == "snowflake"
    assert sent["database_id"] == DATABASE_ID
    assert sent["_headers"]["X-Api-Key"] == API_KEY
    assert sent["_headers"]["User-Agent"] == f"vericto-python/{__version__}"
    assert "Authorization" not in sent["_headers"]


def test_blocked_is_a_verdict_not_an_error(fake: FakeVericto, evaluator: RuntimeApiEvaluator) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked(fix="DELETE FROM t WHERE id = 1"))
    verdict = evaluator.evaluate("DELETE FROM t", "postgres")
    assert verdict.blocked
    assert verdict.rule_code == "VERICTO-010"
    assert verdict.suggested_fix == "DELETE FROM t WHERE id = 1"
    assert verdict.findings[0].rule_code == "VERICTO-010"
    assert verdict.findings[0].ast_node_path == "Delete"
    assert verdict.rewritten_query is None


def _reply(httpserver: HTTPServer, status: int, body: object, headers: dict[str, str] | None = None) -> None:
    httpserver.expect_request("/api/v1/proxy/query").respond_with_json(body, status=status, headers=headers)


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (403, "PLAN_UPGRADE_REQUIRED"), (404, "NOT_FOUND"), (400, "X")],
)
def test_client_errors_are_config_errors(httpserver: HTTPServer, status: int, code: str) -> None:
    _reply(httpserver, status, {"error": {"code": code, "message": "nope"}})
    httpserver.expect_request("/api/v1/meta/config").respond_with_json({"dialects": [{"id": "postgres"}]})
    with pytest.raises(VerictoConfigError) as info:
        make(httpserver.url_for("").rstrip("/")).evaluate("SELECT 1", "postgres")
    assert info.value.status == status
    assert API_KEY not in str(info.value)
    assert len([r for r, _ in httpserver.log if r.path == "/api/v1/proxy/query"]) == 1  # not retried


def test_quota_exceeded_is_not_retried(httpserver: HTTPServer) -> None:
    body = {"error": {"code": "QUOTA_EXCEEDED", "message": "x", "resets_at": "2026-11-01T00:00:00.000Z"}}
    _reply(httpserver, 429, body, {"Retry-After": "86400"})
    with pytest.raises(VerictoQuotaExceeded) as info:
        make(httpserver.url_for("").rstrip("/")).evaluate("SELECT 1", "postgres")
    assert info.value.resets_at == "2026-11-01T00:00:00.000Z"
    assert isinstance(info.value, VerictoUnavailable)
    assert len(httpserver.log) == 1


def test_retries_5xx_then_succeeds(httpserver: HTTPServer) -> None:
    httpserver.expect_ordered_request("/api/v1/proxy/query").respond_with_json({"error": {}}, status=503)
    httpserver.expect_ordered_request("/api/v1/proxy/query").respond_with_json({"error": {}}, status=502)
    httpserver.expect_ordered_request("/api/v1/proxy/query").respond_with_json({"decision": "ALLOWED"})
    verdict = make(httpserver.url_for("").rstrip("/"), max_retries=2).evaluate("SELECT 1", "postgres")
    assert verdict.decision is Decision.ALLOWED
    assert len(httpserver.log) == 3


def test_rate_limit_honours_retry_after(httpserver: HTTPServer) -> None:
    body = {"error": {"code": "RATE_LIMIT_EXCEEDED", "retry_after_seconds": 0}}
    httpserver.expect_ordered_request("/api/v1/proxy/query").respond_with_json(body, status=429)
    httpserver.expect_ordered_request("/api/v1/proxy/query").respond_with_json({"decision": "MONITORED"})
    verdict = make(httpserver.url_for("").rstrip("/")).evaluate("SELECT 1", "postgres")
    assert verdict.decision is Decision.MONITORED


def test_gives_up_after_retries(httpserver: HTTPServer) -> None:
    _reply(httpserver, 500, {"error": {"code": "INTERNAL_SERVER_ERROR"}})
    with pytest.raises(VerictoUnavailable, match="3 attempt"):
        make(httpserver.url_for("").rstrip("/"), max_retries=2).evaluate("SELECT 1", "postgres")
    assert len(httpserver.log) == 3


def test_connection_refused_is_unavailable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(VerictoUnavailable):
        make(f"http://127.0.0.1:{port}", max_retries=1).evaluate("SELECT 1", "postgres")


def test_timeout_is_unavailable(httpserver: HTTPServer) -> None:
    def slow(_request: Request) -> Response:
        time.sleep(0.6)
        return Response(json.dumps({"decision": "ALLOWED"}), content_type="application/json")

    httpserver.expect_request("/api/v1/proxy/query").respond_with_handler(slow)
    started = time.monotonic()
    with pytest.raises(VerictoUnavailable):
        make(httpserver.url_for("").rstrip("/"), timeout=0.2, max_retries=0).evaluate("SELECT 1", "postgres")
    assert time.monotonic() - started < 0.6


def test_non_json_or_missing_decision_is_unavailable(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/api/v1/proxy/query").respond_with_data("<html>proxy</html>", status=200)
    with pytest.raises(VerictoUnavailable):
        make(httpserver.url_for("").rstrip("/"), max_retries=0).evaluate("SELECT 1", "postgres")


def test_unknown_decision_fails_closed(httpserver: HTTPServer) -> None:
    _reply(httpserver, 200, {"decision": "SOMETHING_NEW"})
    verdict = make(httpserver.url_for("").rstrip("/")).evaluate("SELECT 1", "postgres")
    assert verdict.decision is Decision.BLOCKED


def test_unsupported_dialect(fake: FakeVericto, evaluator: RuntimeApiEvaluator) -> None:
    fake.dialects = ["postgres", "snowflake"]
    with pytest.raises(VerictoUnsupportedDialect) as info:
        evaluator.evaluate("SELECT 1", "sqlite")
    assert info.value.dialect == "sqlite"
    assert "postgres, snowflake" in str(info.value)


def test_supported_dialects_are_cached(fake: FakeVericto, evaluator: RuntimeApiEvaluator) -> None:
    assert evaluator.supported_dialects() == tuple(fake.dialects)
    evaluator.supported_dialects()
    assert fake.config_calls == 1


def test_supported_dialects_unknown_when_unreachable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert make(f"http://127.0.0.1:{port}").supported_dialects() is None


class TestConfiguration:
    def test_requires_a_key(self) -> None:
        with pytest.raises(VerictoConfigError, match="VERICTO_API_KEY"):
            RuntimeApiEvaluator(database_id=DATABASE_ID)

    def test_rejects_a_non_vericto_key_without_echoing_it(self) -> None:
        with pytest.raises(VerictoConfigError) as info:
            RuntimeApiEvaluator("sk-secret-value", database_id=DATABASE_ID)
        assert "sk-secret-value" not in str(info.value)

    def test_requires_a_uuid_database(self) -> None:
        with pytest.raises(VerictoConfigError, match="UUID"):
            RuntimeApiEvaluator(API_KEY, database_id="prod")
        with pytest.raises(VerictoConfigError, match="VERICTO_DATABASE_ID"):
            RuntimeApiEvaluator(API_KEY)

    def test_refuses_plain_http_off_localhost(self) -> None:
        with pytest.raises(VerictoConfigError, match="plain HTTP"):
            RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, api_url="http://api.example.com")

    def test_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
        monkeypatch.setenv("VERICTO_DATABASE_ID", DATABASE_ID.upper())
        monkeypatch.setenv("VERICTO_API_URL", "https://vericto.internal.example/")
        monkeypatch.setenv("VERICTO_TIMEOUT", "1.5")
        ev = RuntimeApiEvaluator()
        assert ev.api_url == "https://vericto.internal.example"
        assert ev.database_id == DATABASE_ID
        assert ev.timeout == 1.5

    def test_bad_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERICTO_TIMEOUT", "soon")
        with pytest.raises(VerictoConfigError, match="VERICTO_TIMEOUT"):
            RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID)

    def test_repr_hides_the_key(self) -> None:
        ev = RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID)
        assert API_KEY not in repr(ev)
        assert "api.vericto.com" in repr(ev)

    def test_explicit_arguments_win_over_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERICTO_API_KEY", "vtro_from_env")
        monkeypatch.setenv("VERICTO_API_URL", "https://env.example")
        monkeypatch.setenv("VERICTO_TIMEOUT", "9")
        ev = RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, api_url="http://localhost:9", timeout=1)
        assert ev.api_url == "http://localhost:9"  # plain HTTP is fine for localhost
        assert ev.timeout == 1.0
        assert ev._api_key == API_KEY

    @pytest.mark.parametrize("url", ["ftp://api.example.com", "api.example.com", "https://"])
    def test_rejects_malformed_urls(self, url: str) -> None:
        with pytest.raises(VerictoConfigError, match="Invalid Vericto API URL"):
            RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, api_url=url)

    def test_rejects_non_positive_timeout_and_negative_retries(self) -> None:
        with pytest.raises(VerictoConfigError, match="greater than 0"):
            RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, timeout=0)
        with pytest.raises(VerictoConfigError, match="negative"):
            RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, max_retries=-1)

    def test_blank_key_from_the_environment_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERICTO_API_KEY", "   ")
        with pytest.raises(VerictoConfigError, match="No Vericto API key"):
            RuntimeApiEvaluator(database_id=DATABASE_ID)

    def test_ca_bundle_from_the_environment(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        bundle = tmp_path / "ca.pem"
        bundle.write_text("")
        monkeypatch.setenv("VERICTO_CA_BUNDLE", str(bundle))
        ev = RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID)
        assert ev._http.connection_pool_kw["ca_certs"] == str(bundle)
        assert ev._http.connection_pool_kw["cert_reqs"] == "CERT_REQUIRED"


# -- alternate flows: the Runtime API misbehaves ----------------------------------------


class ScriptedPool:
    """A pool_manager stand-in: raises or answers from a script, records each call."""

    def __init__(self, *steps: object) -> None:
        self.steps = list(steps)
        self.calls: List[Dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> urllib3.HTTPResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        status, body = cast(Tuple[int, Any], step)
        return urllib3.HTTPResponse(body=json.dumps(body).encode(), status=status, preload_content=True)

    def clear(self) -> None:
        self.steps.clear()


def scripted(*steps: object, max_retries: int = 2) -> tuple[RuntimeApiEvaluator, ScriptedPool]:
    pool = ScriptedPool(*steps)
    ev = RuntimeApiEvaluator(
        API_KEY,
        database_id=DATABASE_ID,
        api_url="https://api.example.com",
        backoff=0.0,
        max_retries=max_retries,
        pool_manager=pool,  # type: ignore[arg-type]
    )
    return ev, pool


def test_connection_errors_are_retried_then_succeed() -> None:
    ev, pool = scripted(
        urllib3.exceptions.NewConnectionError(None, "refused"),  # type: ignore[arg-type]
        urllib3.exceptions.ReadTimeoutError(None, "/", "read timed out"),  # type: ignore[arg-type]
        (200, {"decision": "ALLOWED", "event_id": "e"}),
    )
    assert ev.evaluate("SELECT 1", "postgres").decision is Decision.ALLOWED
    assert len(pool.calls) == 3
    assert all(c["retries"] is False and c["redirect"] is False for c in pool.calls)
    assert pool.calls[0]["headers"]["X-API-Key"] == API_KEY


def test_connection_errors_exhaust_the_retries_without_leaking_the_key() -> None:
    ev, _ = scripted(*[urllib3.exceptions.ProtocolError("reset")] * 3)
    with pytest.raises(VerictoUnavailable, match=r"ProtocolError while calling https://api\.example\.com") as info:
        ev.evaluate("SELECT 1", "postgres")
    assert API_KEY not in str(info.value)


def test_rate_limit_exhausts_the_retries() -> None:
    body = {"error": {"code": "RATE_LIMIT_EXCEEDED", "retry_after_seconds": 0}}
    ev, pool = scripted((429, body), (429, body), max_retries=1)
    with pytest.raises(VerictoUnavailable, match="HTTP 429 RATE_LIMIT_EXCEEDED"):
        ev.evaluate("SELECT 1", "postgres")
    assert len(pool.calls) == 2


@pytest.mark.parametrize("body", [[1, 2], "just a string", {"decision_typo": "ALLOWED"}])
def test_malformed_success_bodies_are_unavailable(body: object) -> None:
    ev, _ = scripted((200, body), max_retries=0)
    with pytest.raises(VerictoUnavailable, match="without a decision"):
        ev.evaluate("SELECT 1", "postgres")


def test_quota_message_names_the_reset_and_the_upgrade_url() -> None:
    error = {"code": "QUOTA_EXCEEDED", "resets_at": "2026-11-01", "upgrade_url": "https://vericto.com/pricing"}
    ev, pool = scripted((429, {"error": error}))
    with pytest.raises(VerictoQuotaExceeded, match="resets at 2026-11-01") as info:
        ev.evaluate("SELECT 1", "postgres")
    assert info.value.upgrade_url == "https://vericto.com/pricing"
    assert info.value.status == 429 and info.value.code == "QUOTA_EXCEEDED"
    assert len(pool.calls) == 1


def test_403_without_a_block_is_a_config_error_with_a_default_reason() -> None:
    ev, _ = scripted((403, {"error": {}}))
    with pytest.raises(VerictoConfigError, match="lacks the proxy:evaluate scope"):
        ev.evaluate("SELECT 1", "postgres")


def test_401_message_points_at_the_key() -> None:
    ev, _ = scripted((401, {"error": {"code": "UNAUTHORIZED"}}))
    with pytest.raises(VerictoConfigError, match="rejected the API key") as info:
        ev.evaluate("SELECT 1", "postgres")
    assert info.value.code == "UNAUTHORIZED"


def test_oversized_statement_rejected_by_the_server_is_not_retried() -> None:
    # The backend validates the body (64 KiB per statement) and answers 400 VALIDATION_ERROR.
    error = {
        "code": "VALIDATION_ERROR",
        "message": "Validation failed",
        "details": [{"path": "query", "message": "each statement must be at most 65536 bytes"}],
    }
    ev, pool = scripted((400, {"error": error}), (200, {"dialects": [{"id": "postgres"}]}))
    with pytest.raises(VerictoConfigError, match="HTTP 400, VALIDATION_ERROR") as info:
        ev.evaluate("SELECT 1 " + "-" * 70_000, "postgres")
    assert info.value.status == 400 and info.value.code == "VALIDATION_ERROR"
    assert [c["method"] for c in pool.calls] == ["POST", "GET"]  # not retried; the dialect list is asked once


def test_oversized_statement_is_not_run_even_under_fail_open() -> None:
    ev, _ = scripted((400, {"error": {"code": "VALIDATION_ERROR"}}), (200, {"dialects": [{"id": "postgres"}]}))
    ev._dialects = ("postgres",)
    ev._dialects_at = time.monotonic()
    with pytest.raises(VerictoConfigError):
        Vericto(ev, fail_mode="open").enforce("SELECT 1 " + "-" * 70_000, "postgres")


def test_large_statement_is_sent_whole(fake: FakeVericto, evaluator: RuntimeApiEvaluator) -> None:
    sql = "SELECT " + ", ".join(f"c{i}" for i in range(50_000)) + " FROM t"
    assert evaluator.evaluate(sql, "postgres").decision is Decision.ALLOWED
    assert fake.requests[-1]["query"] == sql


def test_empty_statement_is_left_to_the_server(httpserver: HTTPServer) -> None:
    _reply(httpserver, 400, {"error": {"code": "VALIDATION_ERROR", "message": "query is required"}})
    httpserver.expect_request("/api/v1/meta/config").respond_with_json({"dialects": [{"id": "postgres"}]})
    with pytest.raises(VerictoConfigError, match="query is required"):
        make(httpserver.url_for("").rstrip("/")).evaluate("", "postgres")


def test_delay_honours_retry_after_up_to_the_cap() -> None:
    ev = RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, backoff=0.2)
    assert ev._delay(1, 0.5) == 0.5
    assert ev._delay(1, 3600.0) == 2.0  # a huge Retry-After never stalls the statement
    for attempt, (low, high) in {1: (0.1, 0.2), 2: (0.2, 0.4), 3: (0.4, 0.8), 6: (2.0, 2.0)}.items():
        assert low <= ev._delay(attempt, None) <= high


@pytest.mark.parametrize(
    ("header", "body", "expected"),
    [
        ("3", None, 3.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 1, 1.0),  # HTTP-date header: use the body value
        ("-5", None, 0.0),
        (None, "soon", None),
        (None, None, None),
    ],
)
def test_retry_after_parsing(header: Optional[str], body: object, expected: Optional[float]) -> None:
    assert _retry_after(header, body) == expected


def test_supported_dialects_tolerates_bad_answers_and_keeps_the_last_good_one() -> None:
    ev, _ = scripted(
        (500, {}),
        (200, {"dialects": "postgres"}),
        (200, {"dialects": [{"id": "postgres"}, {"label": "no id"}, "junk"]}),
        urllib3.exceptions.ProtocolError("reset"),
    )
    ev._dialects_ttl = 0.0  # ask every time
    assert ev.supported_dialects() is None
    assert ev.supported_dialects() is None
    assert ev.supported_dialects() == ("postgres",)
    assert ev.supported_dialects() == ("postgres",)  # unreachable: the last good answer
    ev.close()


# -- the guard's fail mode over HTTP ------------------------------------------------------


def test_slow_api_fail_closed_and_fail_open(httpserver: HTTPServer) -> None:
    def slow(_request: Request) -> Response:
        time.sleep(0.4)
        return Response(json.dumps({"decision": "ALLOWED"}), content_type="application/json")

    httpserver.expect_request("/api/v1/meta/config").respond_with_json({"dialects": [{"id": "postgres"}]})
    httpserver.expect_request("/api/v1/proxy/query").respond_with_handler(slow)
    ev = make(httpserver.url_for("").rstrip("/"), timeout=0.1, max_retries=0)
    with pytest.raises(VerictoUnavailable):
        Vericto(ev).enforce("SELECT 1", "postgres")
    with pytest.warns(VerictoFailOpenWarning):
        verdict = Vericto(ev, fail_mode="open").enforce("SELECT 1", "postgres")
    assert verdict.degraded and verdict.sql_to_execute == "SELECT 1"


def test_fail_open_never_hides_a_rejected_key(httpserver: HTTPServer) -> None:
    _reply(httpserver, 401, {"error": {"code": "UNAUTHORIZED"}})
    httpserver.expect_request("/api/v1/meta/config").respond_with_json({"dialects": [{"id": "postgres"}]})
    with pytest.raises(VerictoConfigError):
        Vericto(make(httpserver.url_for("").rstrip("/")), fail_mode="open").enforce("SELECT 1", "postgres")


def test_fail_open_covers_quota_and_5xx_over_http(httpserver: HTTPServer) -> None:
    _reply(httpserver, 429, {"error": {"code": "QUOTA_EXCEEDED"}})
    httpserver.expect_request("/api/v1/meta/config").respond_with_json({"dialects": [{"id": "postgres"}]})
    guard = Vericto(make(httpserver.url_for("").rstrip("/")), fail_mode="open")
    with pytest.warns(VerictoFailOpenWarning, match="monthly query quota"):
        assert guard.enforce("SELECT 1", "postgres").degraded
