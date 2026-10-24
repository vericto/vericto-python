# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""The fail-mode contract over HTTP, shared with vericto-node 0.4.0.

When Vericto cannot answer (network error, timeout, 5xx, a body that is not JSON):

* ``fail_mode="open"``: ``check()`` returns an ``UNEVALUATED`` verdict with the
  reason, ``enforce()`` returns one too (and the statement may run, with a
  ``VerictoFailOpenWarning``); neither raises.
* ``fail_mode="closed"`` (default): both raise ``VerictoUnavailable``.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Callable

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.conftest import API_KEY, DATABASE_ID
from vericto import Decision, RuntimeApiEvaluator, Verdict, Vericto, VerictoFailOpenWarning, VerictoUnavailable

Setup = Callable[[HTTPServer], str]


def _config(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/api/v1/meta/config").respond_with_json({"dialects": [{"id": "postgres"}]})


def network_error(_httpserver: HTTPServer) -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"  # nothing listens there


def timeout(httpserver: HTTPServer) -> str:
    def slow(_request: Request) -> Response:
        time.sleep(0.5)
        return Response(json.dumps({"decision": "ALLOWED"}), content_type="application/json")

    _config(httpserver)
    httpserver.expect_request("/api/v1/proxy/query").respond_with_handler(slow)
    return httpserver.url_for("").rstrip("/")


def server_error(httpserver: HTTPServer) -> str:
    _config(httpserver)
    httpserver.expect_request("/api/v1/proxy/query").respond_with_json(
        {"error": {"code": "INTERNAL_ERROR", "message": "boom"}}, status=503
    )
    return httpserver.url_for("").rstrip("/")


def invalid_json(httpserver: HTTPServer) -> str:
    _config(httpserver)
    httpserver.expect_request("/api/v1/proxy/query").respond_with_data(
        "<html>bad gateway</html>", status=200, content_type="text/html"
    )
    return httpserver.url_for("").rstrip("/")


CASES = {
    "network error": (network_error, "while calling"),
    "timeout": (timeout, "while calling"),
    "5xx": (server_error, "HTTP 503"),
    "invalid JSON": (invalid_json, "without a decision"),
}


def _guard(url: str, mode: str) -> Vericto:
    evaluator = RuntimeApiEvaluator(
        API_KEY, database_id=DATABASE_ID, api_url=url, timeout=0.2, max_retries=0, backoff=0.0
    )
    return Vericto(evaluator, fail_mode=mode)


@pytest.mark.parametrize("case", list(CASES))
def test_fail_open_check_returns_unevaluated_with_the_reason(httpserver: HTTPServer, case: str) -> None:
    setup, reason = CASES[case]
    guard = _guard(setup(httpserver), "open")
    verdict = guard.check("DELETE FROM t", "postgres")
    assert verdict.decision is Decision.UNEVALUATED
    assert verdict.degraded and not verdict.blocked
    assert verdict.message is not None and reason in verdict.message
    assert verdict.sql == "DELETE FROM t" and verdict.dialect == "postgres"
    assert API_KEY not in verdict.message


@pytest.mark.parametrize("case", list(CASES))
def test_fail_open_enforce_returns_unevaluated_and_warns(httpserver: HTTPServer, case: str) -> None:
    setup, reason = CASES[case]
    seen: list[Verdict] = []
    guard = _guard(setup(httpserver), "open")
    guard.on_verdict = seen.append
    with pytest.warns(VerictoFailOpenWarning, match=reason):
        verdict = guard.enforce("SELECT 1", "postgres")
    assert verdict.decision is Decision.UNEVALUATED
    assert verdict.message is not None and reason in verdict.message
    assert verdict.sql_to_execute == "SELECT 1"
    assert seen == [verdict]


@pytest.mark.parametrize("case", list(CASES))
@pytest.mark.parametrize("method", ["check", "enforce"])
def test_fail_closed_raises_the_typed_error(httpserver: HTTPServer, case: str, method: str) -> None:
    setup, reason = CASES[case]
    guard = _guard(setup(httpserver), "closed")
    with pytest.raises(VerictoUnavailable, match=reason) as info:
        getattr(guard, method)("SELECT 1", "postgres")
    assert API_KEY not in str(info.value)
