from __future__ import annotations

import json
import socket
import time

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.conftest import API_KEY, DATABASE_ID, FakeVericto, blocked
from vericto import (
    Decision,
    RuntimeApiEvaluator,
    VerictoConfigError,
    VerictoQuotaExceeded,
    VerictoUnavailable,
    VerictoUnsupportedDialect,
    __version__,
)


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
