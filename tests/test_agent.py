"""The framework-free pieces shared by the LangChain and LlamaIndex integrations (vericto._agent).

They need SQLAlchemy only, so they run in the core job; the integration jobs exercise them
again through the real frameworks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import vericto.sqlalchemy
from tests.conftest import FakeVericto, blocked, masked
from vericto import Decision, Finding, Verdict, Vericto, VerictoBlocked, VerictoConfigError
from vericto._agent import EngineGate, block_message, check_on_block, describe_verdict


def verdict(decision: Decision, **kw: Any) -> Verdict:
    return Verdict(decision=decision, sql="SELECT 1", dialect="postgres", **kw)


@pytest.mark.parametrize("value", ["return", "RAISE", " raise "])
def test_check_on_block_accepts(value: str) -> None:
    assert check_on_block(value) == value.strip().lower()


def test_check_on_block_rejects() -> None:
    with pytest.raises(VerictoConfigError, match="on_block must be"):
        check_on_block("ignore")


def test_block_message_with_everything() -> None:
    v = verdict(
        Decision.BLOCKED,
        rule_code="VERICTO-010",
        severity="critical",
        message="DELETE without WHERE",
        suggested_fix="DELETE FROM t WHERE id = 1",
        findings=(Finding("VERICTO-010"), Finding("VERICTO-030")),
        access_denied=({"schema": "hr", "table": "salaries", "column": "amount"}, {"other": 1}, "raw.table"),
        event_id="evt-9",
    )
    text_ = block_message(VerictoBlocked(v, "DELETE FROM t"))
    assert text_.splitlines() == [
        "Error: Vericto blocked this query (VERICTO-010, severity critical). It was NOT executed.",
        "Reason: DELETE without WHERE",
        "Other findings: VERICTO-030.",
        "Not allowed for this agent: hr.salaries.amount, {'other': 1}, raw.table.",
        "Suggested fix: DELETE FROM t WHERE id = 1",
        "Rewrite the query following the suggested fix and try again.",
        "(Vericto event evt-9)",
    ]


def test_block_message_minimal() -> None:
    text_ = block_message(VerictoBlocked(verdict(Decision.BLOCKED), "X"))
    assert text_.splitlines() == [
        "Error: Vericto blocked this query (a Vericto rule). It was NOT executed.",
        "Rewrite the query so that it complies with this rule, or tell the user it cannot be run.",
    ]


@pytest.mark.parametrize(
    ("v", "expected"),
    [
        (verdict(Decision.ALLOWED), "ALLOWED: Vericto found no problem with this query."),
        (
            verdict(Decision.UNEVALUATED, message="timeout"),
            "UNEVALUATED: Vericto could not evaluate the query (timeout).",
        ),
        (verdict(Decision.UNEVALUATED), "UNEVALUATED: Vericto could not evaluate the query (no verdict)."),
        (
            verdict(Decision.PARSE_ERROR, parse_error="near FROMM"),
            "PARSE_ERROR: Vericto could not parse the query; the workspace policy lets it run. Parser: near FROMM",
        ),
        (
            verdict(Decision.PARSE_ERROR),
            "PARSE_ERROR: Vericto could not parse the query; the workspace policy lets it run.",
        ),
        (
            verdict(Decision.FLAGGED, rule_code="VERICTO-030", message="no LIMIT"),
            "FLAGGED: allowed to run (VERICTO-030). no LIMIT",
        ),
        (verdict(Decision.MONITORED), "MONITORED: allowed to run (a Vericto rule)."),
        (
            verdict(Decision.ALLOWED, rewritten_query="SELECT '***'"),
            "ALLOWED: allowed to run (a Vericto rule). Sensitive columns are masked: the query that will run is: "
            "SELECT '***'",
        ),
    ],
)
def test_describe_verdict(v: Verdict, expected: str) -> None:
    assert describe_verdict(v) == expected


def test_describe_blocked_is_the_block_message() -> None:
    assert describe_verdict(verdict(Decision.BLOCKED, rule_code="V-1")).startswith(
        "Error: Vericto blocked this query (V-1)"
    )


# -- EngineGate ---------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'ana@example.com')"))
    return eng


def _emails(engine: Engine) -> List[Any]:
    with engine.connect() as c:
        return list(c.execute(text("SELECT email FROM users WHERE id = :id"), {"id": 1}).scalars())


def test_gate_checks_only_inside_active(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    gate = EngineGate(engine, guard, "sqlite", internal={"SELECT 42"})
    _emails(engine)
    assert fake.requests == []  # outside active(): not the integration's SQL
    with gate.active():
        assert _emails(engine) == ["ana@example.com"]
        with engine.connect() as c:
            c.execute(text("SELECT 42"))  # framework-internal statement: let through
    assert fake.queries() == ["SELECT email FROM users WHERE id = ?"]
    gate.remove()
    gate.remove()  # idempotent


def test_gate_blocks_and_masks(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users WHERE id = ?"))
    gate = EngineGate(engine, guard, "sqlite")
    with gate.active():
        assert _emails(engine) == ["***"]
        with pytest.raises(VerictoBlocked), engine.begin() as c:
            c.execute(text("DELETE FROM users"))
    assert _emails(engine) == ["ana@example.com"]
    gate.remove()


def test_gate_passthrough_adds_no_listener(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    remove = vericto.sqlalchemy.instrument(engine, guard=guard)
    gate = EngineGate(engine, guard, "sqlite", passthrough=True)
    with gate.active():
        _emails(engine)
    assert len(fake.requests) == 1  # evaluated once, by instrument()'s listener
    gate.remove()
    remove()
