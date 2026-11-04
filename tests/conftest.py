# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""A local stand-in for the Vericto Runtime API (pytest-httpserver), and a fake
``vericto serve`` for offline mode."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from vericto import RuntimeApiEvaluator, Vericto

API_KEY = "vtro_test_0123456789abcdef"
DATABASE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
ALL_DIALECTS = ["postgres", "mysql", "oracle", "mssql", "snowflake", "sqlite"]
# A server older than vericto-engine 4.1.0, which did not list sqlite.
DIALECTS_WITHOUT_SQLITE = ["postgres", "mysql", "oracle", "mssql", "snowflake"]

Rule = Callable[[str, str], Optional[Dict[str, Any]]]


def allowed() -> Dict[str, Any]:
    return {"status": 200, "body": {"decision": "ALLOWED", "rule_code": None, "event_id": "evt-allowed"}}


def blocked(rule: str = "VERICTO-010", fix: Optional[str] = None) -> Dict[str, Any]:
    return {
        "status": 403,
        "body": {
            "error": f"Query blocked by rule {rule}",
            "decision": "BLOCKED",
            "rule_code": rule,
            "severity": "critical",
            "ast_node_path": "Delete",
            "suggested_safe_query": fix,
            "rewritten_query": None,
            "event_id": "evt-blocked",
        },
    }


def flagged(rule: str = "VERICTO-030") -> Dict[str, Any]:
    return {"status": 200, "body": {"decision": "FLAGGED", "rule_code": rule, "severity": "medium"}}


def masked(rewritten: str) -> Dict[str, Any]:
    return {
        "status": 200,
        "body": {
            "decision": "FLAGGED",
            "rule_code": "VERICTO-085",
            "severity": "low",
            "rewritten_query": rewritten,
            "sensitive_columns": [{"table": "users", "column": "email", "action": "mask"}],
        },
    }


@dataclass
class FakeVericto:
    """Routes evaluations through ``rules`` (first match wins; default ALLOWED)."""

    server: HTTPServer
    dialects: List[str] = field(default_factory=lambda: list(ALL_DIALECTS))
    rules: List[Rule] = field(default_factory=list)
    requests: List[Dict[str, Any]] = field(default_factory=list)
    config_calls: int = 0

    @property
    def url(self) -> str:
        return self.server.url_for("").rstrip("/")

    def on(self, predicate: Callable[[str], bool], response: Dict[str, Any]) -> None:
        self.rules.append(lambda sql, _dialect: response if predicate(sql) else None)

    def queries(self) -> List[str]:
        return [r["query"] for r in self.requests]

    def install(self) -> None:
        def config(_request: Request) -> Response:
            self.config_calls += 1
            body = {"dialects": [{"id": d, "label": d} for d in self.dialects]}
            return Response(json.dumps(body), status=200, content_type="application/json")

        def evaluate(request: Request) -> Response:
            body = json.loads(request.get_data())
            body["_headers"] = dict(request.headers)
            self.requests.append(body)
            if body["dialect"] not in self.dialects:
                error = {"code": "VALIDATION_ERROR", "message": "Validation failed"}
                return Response(json.dumps({"error": error}), status=400, content_type="application/json")
            for rule in self.rules:
                hit = rule(body["query"], body["dialect"])
                if hit is not None:
                    return Response(json.dumps(hit["body"]), status=hit["status"], content_type="application/json")
            ok = allowed()
            return Response(json.dumps(ok["body"]), status=200, content_type="application/json")

        self.server.expect_request("/api/v1/meta/config", method="GET").respond_with_handler(config)
        self.server.expect_request("/api/v1/proxy/query", method="POST").respond_with_handler(evaluate)


@pytest.fixture
def fake(httpserver: HTTPServer) -> Iterator[FakeVericto]:
    f = FakeVericto(httpserver)
    f.install()
    yield f
    httpserver.clear()


@pytest.fixture
def evaluator(fake: FakeVericto) -> RuntimeApiEvaluator:
    return RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, api_url=fake.url, backoff=0.0)


@pytest.fixture
def guard(evaluator: RuntimeApiEvaluator) -> Vericto:
    return Vericto(evaluator)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "VERICTO_API_KEY",
        "VERICTO_API_URL",
        "VERICTO_DATABASE_ID",
        "VERICTO_TIMEOUT",
        "VERICTO_FAIL_MODE",
        "VERICTO_CA_BUNDLE",
        "VERICTO_OFFLINE",
        "VERICTO_CLI",
    ):
        monkeypatch.delenv(name, raising=False)


# -- offline mode: a stand-in for `vericto serve --stdio` (tests/fake_serve.py) -----

FAKE_SERVE = Path(__file__).parent / "fake_serve.py"


def make_fake_cli(directory: Path, name: str = "vericto") -> str:
    """An executable ``name`` in ``directory`` that runs tests/fake_serve.py."""
    path = directory / name
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_SERVE}" "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fake_cli(tmp_path: Path) -> str:
    if os.name == "nt":
        pytest.skip("the fake vericto CLI is a POSIX shell script")
    return make_fake_cli(tmp_path)


@pytest.fixture
def offline_guard(fake_cli: str) -> Iterator[Vericto]:
    """A guard in offline mode, against the fake ``vericto serve``."""
    with Vericto(offline=True, offline_cli_path=fake_cli) as g:
        yield g
