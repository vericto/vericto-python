# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""``context`` and ``end_user``: the request body, the client-side limits, the cache."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Optional

import pytest

from tests.conftest import API_KEY, DATABASE_ID, FakeVericto
from vericto import Decision, RuntimeApiEvaluator, Verdict, Vericto, VerictoConfigError
from vericto.policy_context import policy_fields

TICKET = "CHG-1042"
END_USER = {"id": "u_829", "role": "support", "region": "eu"}


def test_check_sends_context_and_end_user(fake: FakeVericto, guard: Vericto) -> None:
    guard.check("SELECT 1", "postgres", context={"ticket": TICKET, "tenant": "acme"}, end_user=END_USER)
    sent = fake.requests[-1]
    assert sent["context"] == {"ticket": TICKET, "tenant": "acme"}
    assert sent["end_user"] == END_USER
    assert sent["query"] == "SELECT 1" and sent["database_id"] == DATABASE_ID


def test_enforce_prepare_and_enforce_all_send_them(fake: FakeVericto, guard: Vericto) -> None:
    guard.enforce("SELECT 1", "postgres", context={"tenant": "acme"})
    assert fake.requests[-1]["context"] == {"tenant": "acme"}
    assert "end_user" not in fake.requests[-1]
    guard.prepare("SELECT 2", "postgres", end_user={"id": "u_1"})
    assert fake.requests[-1]["end_user"] == {"id": "u_1"}
    assert "context" not in fake.requests[-1]
    guard.enforce_all(["SELECT 3", "SELECT 4"], "postgres", context={"ticket": TICKET})
    assert [r["context"] for r in fake.requests[-2:]] == [{"ticket": TICKET}] * 2


def test_without_them_the_body_is_unchanged(fake: FakeVericto, guard: Vericto) -> None:
    guard.check("SELECT 1", "postgres")
    guard.check("SELECT 1", "postgres", context={}, end_user={})
    guard.check("SELECT 1", "postgres", context={"ticket": None})
    for sent in fake.requests:
        assert set(sent) - {"_headers"} == {"query", "database_id", "dialect"}


def test_the_evaluator_takes_them_directly(fake: FakeVericto, evaluator: RuntimeApiEvaluator) -> None:
    evaluator.evaluate("SELECT 1", "postgres", context={"ticket": TICKET}, end_user={"id": "u_1"})
    assert fake.requests[-1]["context"] == {"ticket": TICKET}
    assert fake.requests[-1]["end_user"] == {"id": "u_1"}


def _long(n: int) -> str:
    return "x" * n


@pytest.mark.parametrize(
    ("context", "end_user", "message"),
    [
        ({"ticket": ""}, None, "context.ticket must not be empty"),
        ({"ticket": _long(129)}, None, "context.ticket: at most 128"),
        ({"tenant": "a\nb"}, None, "context.tenant: no control characters"),
        ({"tenant": "a\x7fb"}, None, "no control characters"),
        ({"tenant": 7}, None, "context.tenant must be a string"),
        ({"ticket": TICKET, "user": "x"}, None, "unknown key"),
        ("CHG-1", None, "context must be a mapping"),
        (None, {"id": ""}, "end_user.id must not be empty"),
        (None, {"id": _long(257)}, "end_user.id: at most 256"),
        (None, {"id": "a\tb"}, "no control characters"),
        (None, {"1st": "x"}, "keys are identifiers"),
        (None, {"a-b": "x"}, "keys are identifiers"),
        (None, {"k" * 64: "x"}, "keys are identifiers"),
        (None, {"id\n": "x"}, "keys are identifiers"),
        (None, {f"k{i}": "v" for i in range(17)}, "at most 16 keys"),
        (None, {"id": 1}, "end_user.id must be a string"),
        (None, ["id"], "end_user must be a mapping"),
    ],
)
def test_invalid_values_are_refused_before_anything_is_sent(
    fake: FakeVericto, evaluator: RuntimeApiEvaluator, context: Any, end_user: Any, message: str
) -> None:
    for mode in ("closed", "open"):  # a caller error, never failed open
        guard = Vericto(evaluator, fail_mode=mode)
        with pytest.raises(VerictoConfigError, match=message):
            guard.check("SELECT 1", "postgres", context=context, end_user=end_user)
        with pytest.raises(VerictoConfigError):
            guard.enforce("SELECT 1", "postgres", context=context, end_user=end_user)
    assert fake.requests == []


def test_messages_name_the_field_never_the_value() -> None:
    secret = "tenant-secret-" + "z" * 120
    with pytest.raises(VerictoConfigError) as info:
        policy_fields({"tenant": secret}, None)
    assert "context.tenant" in str(info.value) and secret not in str(info.value)


def test_the_limits_are_inclusive_and_count_utf16_units() -> None:
    fields = policy_fields(
        {"ticket": _long(128), "tenant": "é" * 128},
        {f"k{i}": _long(256) for i in range(16)},
    )
    assert len(fields["end_user"]) == 16
    assert policy_fields(None, {"_": "x", "A" + "b" * 62: "y"})["end_user"] == {"_": "x", "A" + "b" * 62: "y"}
    # One astral character is two UTF-16 units, as the server (zod) counts it.
    assert policy_fields({"ticket": "\U0001f600" * 64}, None)
    with pytest.raises(VerictoConfigError, match="at most 128"):
        policy_fields({"ticket": "\U0001f600" * 64 + "x"}, None)


class RecordingEvaluator:
    """An evaluator written for 0.4: evaluate(sql, dialect), no keyword arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def evaluate(self, sql: str, dialect: str) -> Verdict:
        self.calls.append((sql, dialect))
        return Verdict(decision=Decision.ALLOWED, sql=sql, dialect=dialect)

    def supported_dialects(self) -> Optional[list[str]]:
        return None


def test_an_evaluator_without_the_keywords_keeps_working_without_them() -> None:
    ev = RecordingEvaluator()
    guard = Vericto(ev)
    assert guard.check("SELECT 1", "postgres").decision is Decision.ALLOWED
    with pytest.raises(TypeError):  # given to an evaluator that cannot take them: not silently dropped
        guard.check("SELECT 1", "postgres", context={"ticket": TICKET})


def test_the_cache_keys_on_the_declared_context(fake: FakeVericto, evaluator: RuntimeApiEvaluator) -> None:
    guard = Vericto(evaluator, cache_ttl=60)
    guard.check("SELECT 1", "postgres", context={"tenant": "a"})
    assert guard.check("SELECT 1", "postgres", context={"tenant": "a"}).cached
    assert not guard.check("SELECT 1", "postgres", context={"tenant": "b"}).cached
    assert not guard.check("SELECT 1", "postgres").cached
    assert not guard.check("SELECT 1", "postgres", end_user={"id": "u_1"}).cached
    assert len(fake.requests) == 4


# -- against scripts/docs_stub.py (the service's validation rules) ---------------------


def _docs_stub() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "docs_stub.py"
    spec = importlib.util.spec_from_file_location("vericto_docs_stub", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def docs_stub() -> Iterator[Any]:
    server = _docs_stub().StubServer().start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_the_docs_stub_receives_the_body(docs_stub: Any) -> None:
    guard = Vericto(RuntimeApiEvaluator(API_KEY, database_id=DATABASE_ID, api_url=docs_stub.url))
    verdict = guard.check("SELECT 1", "postgres", context={"ticket": TICKET, "tenant": "acme"}, end_user=END_USER)
    assert verdict.decision is Decision.ALLOWED
    assert docs_stub.requests == [
        {
            "query": "SELECT 1",
            "dialect": "postgres",
            "context": {"ticket": TICKET, "tenant": "acme"},
            "end_user": END_USER,
        }
    ]


def test_the_client_limits_match_the_docs_stub() -> None:
    errors = _docs_stub().policy_context_errors
    ok = {"context": {"ticket": _long(128), "tenant": "acme"}, "end_user": {f"k{i}": _long(256) for i in range(16)}}
    assert errors(ok) == []
    assert policy_fields(ok["context"], ok["end_user"]) == ok
    for bad in (
        {"context": {"ticket": _long(129)}},
        {"context": {"ticket": ""}},
        {"context": {"other": "x"}},
        {"end_user": {"1x": "y"}},
        {"end_user": {"id": "a\x00b"}},
        {"end_user": {f"k{i}": "v" for i in range(17)}},
    ):
        assert errors(bad), bad
        with pytest.raises(VerictoConfigError):
            policy_fields(bad.get("context"), bad.get("end_user"))
