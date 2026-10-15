from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence

import pytest

from vericto import (
    Decision,
    Verdict,
    Vericto,
    VerictoBlocked,
    VerictoConfigError,
    VerictoFailOpenWarning,
    VerictoFlaggedWarning,
    VerictoQuotaExceeded,
    VerictoUnavailable,
    VerictoUnsupportedDialect,
)


class StubEvaluator:
    """An in-memory evaluator: the interface an embedded engine would implement."""

    api_url = "stub://"

    def __init__(self, decide: Optional[dict[str, Verdict]] = None, *, error: Optional[Exception] = None) -> None:
        self.decide = decide or {}
        self.error = error
        self.calls: List[tuple[str, str]] = []
        self.dialects: Optional[Sequence[str]] = ("postgres", "snowflake", "sqlite")

    def evaluate(self, sql: str, dialect: str) -> Verdict:
        self.calls.append((sql, dialect))
        if self.error is not None:
            raise self.error
        return self.decide.get(sql) or Verdict(decision=Decision.ALLOWED, sql=sql, dialect=dialect)

    def supported_dialects(self) -> Optional[Sequence[str]]:
        return self.dialects


def v(decision: Decision, sql: str, **kw: object) -> Verdict:
    return Verdict(decision=decision, sql=sql, dialect="postgres", **kw)  # type: ignore[arg-type]


def test_blocked_raises_with_findings_and_fix() -> None:
    sql = "DELETE FROM users"
    body = {
        "decision": "BLOCKED",
        "rule_code": "VERICTO-010",
        "severity": "critical",
        "suggested_safe_query": "DELETE FROM users WHERE id = ?",
        "error": "Query blocked by rule VERICTO-010",
    }
    seen: List[Verdict] = []
    guard = Vericto(StubEvaluator({sql: Verdict.from_response(body, sql, "postgres")}), on_verdict=seen.append)
    with pytest.raises(VerictoBlocked) as info:
        guard.enforce(sql, "postgres")
    err = info.value
    assert err.sql == sql
    assert err.suggested_fix == "DELETE FROM users WHERE id = ?"
    assert [f.rule_code for f in err.findings] == ["VERICTO-010"]
    assert "VERICTO-010" in str(err) and "severity critical" in str(err) and "Suggested fix" in str(err)
    assert seen[0].blocked


def test_flagged_runs_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    sql = "SELECT * FROM users"
    guard = Vericto(StubEvaluator({sql: v(Decision.FLAGGED, sql, rule_code="VERICTO-030", severity="medium")}))
    with caplog.at_level(logging.WARNING, logger="vericto"):
        verdict = guard.enforce(sql, "postgres")
    assert verdict.decision is Decision.FLAGGED
    assert verdict.sql_to_execute == sql
    assert "FLAGGED rule=VERICTO-030" in caplog.text
    assert sql not in caplog.text  # SQL text is never logged


def test_warn_on_flag() -> None:
    sql = "SELECT * FROM users"
    guard = Vericto(StubEvaluator({sql: v(Decision.FLAGGED, sql, rule_code="VERICTO-030")}), warn_on_flag=True)
    with pytest.warns(VerictoFlaggedWarning):
        guard.enforce(sql, "postgres")


@pytest.mark.parametrize("decision", [Decision.MONITORED, Decision.PARSE_ERROR, Decision.ALLOWED])
def test_non_blocking_decisions_run(decision: Decision) -> None:
    guard = Vericto(StubEvaluator({"X": v(decision, "X")}))
    assert guard.prepare("X", "postgres") == "X"


def test_mask_runs_the_rewrite() -> None:
    sql = "SELECT email FROM users"
    rewritten = "SELECT '***' AS email FROM users"
    guard = Vericto(StubEvaluator({sql: v(Decision.FLAGGED, sql, rule_code="VERICTO-085", rewritten_query=rewritten)}))
    verdict = guard.enforce(sql, "postgres")
    assert verdict.masked
    assert guard.prepare(sql, "postgres") == rewritten


def test_blocked_verdict_never_carries_a_rewrite() -> None:
    verdict = Verdict.from_response({"decision": "BLOCKED", "rewritten_query": "SELECT 1"}, "X", "postgres")
    assert verdict.rewritten_query is None


def test_fail_closed_raises() -> None:
    guard = Vericto(StubEvaluator(error=VerictoUnavailable("down")))
    assert guard.fail_mode == "closed"
    with pytest.raises(VerictoUnavailable):
        guard.enforce("SELECT 1", "postgres")


def test_fail_open_runs_unevaluated_and_warns() -> None:
    seen: List[Verdict] = []
    guard = Vericto(StubEvaluator(error=VerictoUnavailable("down")), fail_mode="open", on_verdict=seen.append)
    with pytest.warns(VerictoFailOpenWarning, match="down"):
        verdict = guard.enforce("SELECT 1", "postgres")
    assert verdict.decision is Decision.UNEVALUATED and verdict.degraded
    assert verdict.sql_to_execute == "SELECT 1"
    assert seen == [verdict]


def test_fail_open_covers_quota() -> None:
    guard = Vericto(StubEvaluator(error=VerictoQuotaExceeded("quota")), fail_mode="open")
    with pytest.warns(VerictoFailOpenWarning):
        assert guard.prepare("SELECT 1", "postgres") == "SELECT 1"


def test_fail_open_never_covers_config_errors() -> None:
    guard = Vericto(StubEvaluator(error=VerictoConfigError("bad key", status=401)), fail_mode="open")
    with pytest.raises(VerictoConfigError):
        guard.enforce("SELECT 1", "postgres")


def test_fail_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERICTO_FAIL_MODE", "open")
    assert Vericto(StubEvaluator()).fail_mode == "open"
    with pytest.raises(VerictoConfigError):
        Vericto(StubEvaluator(), fail_mode="maybe")


def test_unsupported_dialect_is_refused_before_evaluating() -> None:
    stub = StubEvaluator()
    stub.dialects = ("postgres",)
    guard = Vericto(stub)
    with pytest.raises(VerictoUnsupportedDialect):
        guard.enforce("SELECT 1", "sqlite")
    assert stub.calls == []


def test_unknown_capabilities_do_not_block() -> None:
    stub = StubEvaluator()
    stub.dialects = None
    assert Vericto(stub).prepare("SELECT 1", "sqlite3") == "SELECT 1"
    assert stub.calls == [("SELECT 1", "sqlite")]


def test_dialect_aliases() -> None:
    stub = StubEvaluator()
    Vericto(stub).enforce("SELECT 1", "postgresql+psycopg")
    assert stub.calls[0][1] == "postgres"


def test_non_text_sql_is_refused() -> None:
    with pytest.raises(TypeError):
        Vericto(StubEvaluator()).enforce(b"SELECT 1", "postgres")  # type: ignore[arg-type]


def test_cache_is_off_by_default() -> None:
    stub = StubEvaluator()
    guard = Vericto(stub)
    guard.enforce("SELECT 1", "postgres")
    guard.enforce("SELECT 1", "postgres")
    assert len(stub.calls) == 2


def test_cache_reuses_until_ttl() -> None:
    stub = StubEvaluator()
    guard = Vericto(stub, cache_ttl=0.2)
    assert not guard.enforce("SELECT 1", "postgres").cached
    assert guard.enforce("SELECT 1", "postgres").cached
    assert len(stub.calls) == 1
    time.sleep(0.25)
    guard.enforce("SELECT 1", "postgres")
    assert len(stub.calls) == 2


def test_cache_drops_everything_when_the_ruleset_changes() -> None:
    stub = StubEvaluator()
    guard = Vericto(stub, cache_ttl=60)
    stub.decide = {"A": v(Decision.ALLOWED, "A", ruleset_version="r1")}
    guard.enforce("A", "postgres")
    stub.decide = {"B": v(Decision.ALLOWED, "B", ruleset_version="r2")}
    guard.enforce("B", "postgres")
    guard.enforce("A", "postgres")
    assert [c[0] for c in stub.calls] == ["A", "B", "A"]


def test_a_broken_callback_does_not_change_the_decision() -> None:
    def boom(_verdict: Verdict) -> None:
        raise RuntimeError("callback bug")

    sql = "DROP TABLE t"
    guard = Vericto(StubEvaluator({sql: v(Decision.BLOCKED, sql, rule_code="VERICTO-001")}), on_verdict=boom)
    with pytest.raises(VerictoBlocked):
        guard.enforce(sql, "postgres")


def test_enforce_all_stops_before_anything_runs() -> None:
    stub = StubEvaluator({"DROP TABLE t": v(Decision.BLOCKED, "DROP TABLE t")})
    with pytest.raises(VerictoBlocked):
        Vericto(stub).enforce_all(["SELECT 1", "DROP TABLE t", "SELECT 2"], "postgres")
