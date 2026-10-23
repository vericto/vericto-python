# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

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


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_empty_fail_mode_env_means_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # 0.4.0 raised VerictoConfigError for VERICTO_FAIL_MODE= (a CI variable that expands to nothing).
    monkeypatch.setenv("VERICTO_FAIL_MODE", value)
    assert Vericto(StubEvaluator()).fail_mode == "closed"
    assert Vericto(StubEvaluator(), fail_mode="open").fail_mode == "open"


def test_invalid_fail_mode_names_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERICTO_FAIL_MODE", "maybe")
    with pytest.raises(VerictoConfigError, match=r"^VERICTO_FAIL_MODE must be"):
        Vericto(StubEvaluator())
    with pytest.raises(VerictoConfigError, match=r"^fail_mode must be"):
        Vericto(StubEvaluator(), fail_mode=" ")


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


def test_deepcopy_returns_the_same_guard(guard: Vericto) -> None:
    import copy

    holder = {"guard": guard}
    assert copy.deepcopy(holder)["guard"] is guard


def test_default_evaluator_is_built_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import API_KEY, DATABASE_ID
    from vericto import RuntimeApiEvaluator

    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_DATABASE_ID", DATABASE_ID)
    monkeypatch.setenv("VERICTO_API_URL", "https://vericto.example")
    monkeypatch.setenv("VERICTO_FAIL_MODE", " OPEN ")
    guard = Vericto(timeout=3, max_retries=0)
    assert isinstance(guard.evaluator, RuntimeApiEvaluator)
    assert guard.evaluator.api_url == "https://vericto.example"
    assert guard.evaluator.timeout == 3.0 and guard.evaluator.max_retries == 0
    assert guard.fail_mode == "open"
    assert "fail_mode='open'" in repr(guard) and API_KEY not in repr(guard)


def test_missing_configuration_fails_at_construction() -> None:
    with pytest.raises(VerictoConfigError, match="VERICTO_API_KEY"):
        Vericto()


@pytest.mark.parametrize(("ttl", "size"), [(-1, 10), (1, 0)])
def test_bad_cache_settings(ttl: float, size: int) -> None:
    with pytest.raises(VerictoConfigError, match="cache_ttl"):
        Vericto(StubEvaluator(), cache_ttl=ttl, cache_size=size)


def test_cache_evicts_the_least_recently_used_and_can_be_cleared() -> None:
    stub = StubEvaluator()
    guard = Vericto(stub, cache_ttl=60, cache_size=2)
    for sql in ("A", "B", "A", "C", "A", "B"):
        guard.enforce(sql, "postgres")
    # A, B miss; A hits (and becomes most recent); C evicts B; A hits; B misses again.
    assert [c[0] for c in stub.calls] == ["A", "B", "C", "B"]
    guard.clear_cache()
    guard.enforce("A", "postgres")
    assert [c[0] for c in stub.calls][-1] == "A"
    Vericto(stub).clear_cache()  # no cache: a no-op


def test_cache_never_stores_unevaluated_or_answers_blocks_as_allowed() -> None:
    sql = "DELETE FROM t"
    stub = StubEvaluator({sql: v(Decision.BLOCKED, sql, rule_code="VERICTO-010")})
    guard = Vericto(stub, cache_ttl=60)
    for _ in range(2):
        with pytest.raises(VerictoBlocked):
            guard.enforce(sql, "postgres")
    assert len(stub.calls) == 1  # the cached verdict still blocks

    down = StubEvaluator(error=VerictoUnavailable("down"))
    open_guard = Vericto(down, fail_mode="open", cache_ttl=60)
    with pytest.warns(VerictoFailOpenWarning):
        open_guard.enforce("SELECT 1", "postgres")
    down.error = None
    assert open_guard.enforce("SELECT 1", "postgres").decision is Decision.ALLOWED
    assert len(down.calls) == 2


def test_check_does_not_apply_the_decision() -> None:
    sql = "DROP TABLE t"
    guard = Vericto(StubEvaluator({sql: v(Decision.BLOCKED, sql)}), fail_mode="open")
    assert guard.check(sql, "postgres").blocked  # returned, not raised
    with pytest.raises(TypeError):
        guard.check(None, "postgres")  # type: ignore[arg-type]
    down = Vericto(StubEvaluator(error=VerictoUnavailable("down")), fail_mode="open")
    with pytest.raises(VerictoUnavailable):  # no fail mode in check()
        down.check("SELECT 1", "postgres")


def test_every_decision_is_logged_without_sql(caplog: pytest.LogCaptureFixture) -> None:
    stub = StubEvaluator(
        {
            "P": v(Decision.PARSE_ERROR, "P", parse_error="unexpected token"),
            "M": v(Decision.MONITORED, "M", rule_code="VERICTO-050"),
            "R": v(Decision.ALLOWED, "R", rewritten_query="SELECT '***'", event_id="evt-7"),
        }
    )
    guard = Vericto(stub)
    with caplog.at_level(logging.INFO, logger="vericto"):
        for sql in ("P", "M", "R"):
            guard.enforce(sql, "postgres")
    assert "not parsed, allowed by workspace policy" in caplog.text
    assert "MONITORED rule=VERICTO-050" in caplog.text
    assert "running the masked rewrite" in caplog.text and "event_id=evt-7" in caplog.text
    assert "SELECT '***'" not in caplog.text


def test_flag_warning_without_a_rule_code() -> None:
    guard = Vericto(StubEvaluator({"X": v(Decision.FLAGGED, "X")}), warn_on_flag=True)
    with pytest.warns(VerictoFlaggedWarning, match="no rule code"):
        guard.enforce("X", "postgres")


def test_percent_placeholders_are_evaluated_natively_and_the_mask_mapped_back() -> None:
    evaluated = "SELECT email FROM users WHERE id = $1"
    rewrite = "SELECT '***' AS email FROM users WHERE id = $1"
    stub = StubEvaluator({evaluated: v(Decision.FLAGGED, evaluated, rewritten_query=rewrite)})
    seen: List[Verdict] = []
    verdict = Vericto(stub, on_verdict=seen.append).enforce(
        "SELECT email FROM users WHERE id = %s", "postgres", paramstyle="format"
    )
    assert stub.calls == [(evaluated, "postgres")]
    assert verdict.sql == "SELECT email FROM users WHERE id = %s"  # the application's text
    assert verdict.sql_to_execute == "SELECT '***' AS email FROM users WHERE id = %s"
    assert seen == [verdict]


def test_blocked_with_percent_placeholders_reports_the_application_text() -> None:
    evaluated = "DELETE FROM t WHERE a = $1 OR 1 = 1"
    stub = StubEvaluator({evaluated: v(Decision.BLOCKED, evaluated, rule_code="VERICTO-012")})
    with pytest.raises(VerictoBlocked) as info:
        Vericto(stub).enforce("DELETE FROM t WHERE a = %s OR 1 = 1", "postgres", paramstyle="format")
    assert info.value.sql == "DELETE FROM t WHERE a = %s OR 1 = 1"
    assert info.value.verdict.sql == "DELETE FROM t WHERE a = %s OR 1 = 1"


def test_enforce_all_on_an_empty_script() -> None:
    stub = StubEvaluator()
    assert Vericto(stub).enforce_all([], "postgres") == []
    assert stub.calls == []


def test_fail_open_in_a_batch_evaluates_everything() -> None:
    stub = StubEvaluator(error=VerictoUnavailable("down"))
    guard = Vericto(stub, fail_mode="open")
    with pytest.warns(VerictoFailOpenWarning):
        verdicts = guard.enforce_all(["SELECT 1", "SELECT 2"], "postgres")
    assert [x.decision for x in verdicts] == [Decision.UNEVALUATED, Decision.UNEVALUATED]
    assert [c[0] for c in stub.calls] == ["SELECT 1", "SELECT 2"]
