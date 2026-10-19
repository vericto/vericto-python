# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""vericto.langchain against the real langchain-community / langchain-core (no LLM calls)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

pytest.importorskip("langchain_community")
pytest.importorskip("langchain_core")

from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_community.tools.sql_database.tool import QuerySQLDatabaseTool
from langchain_core.language_models.fake import FakeListLLM
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import vericto.sqlalchemy
from tests.conftest import FakeVericto, blocked, flagged, masked
from vericto import Vericto, VerictoBlocked, VerictoConfigError, VerictoUnavailable
from vericto.langchain import VerictoCheckSQLTool, VerictoSQLDatabase


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'lc.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'ana@example.com'), (2, 'bo@example.com')"))
    return eng


def _count(engine: Engine) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM users")).scalar_one())


def test_reflection_is_not_evaluated(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    VerictoSQLDatabase(engine, guard=guard, sample_rows_in_table_info=0)
    assert fake.requests == []


def test_allowed_query_runs(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    db = VerictoSQLDatabase(engine, guard=guard)
    assert db.run("SELECT email FROM users WHERE id = 1") == "[('ana@example.com',)]"
    assert fake.queries() == ["SELECT email FROM users WHERE id = 1"]
    assert fake.requests[0]["dialect"] == "sqlite"


def test_parameters_are_not_sent(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    db = VerictoSQLDatabase(engine, guard=guard)
    assert db.run("SELECT email FROM users WHERE id = :id", parameters={"id": 2}) == "[('bo@example.com',)]"
    assert fake.queries() == ["SELECT email FROM users WHERE id = ?"]


def test_blocked_returns_an_agent_message_and_nothing_runs(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-010", fix="DELETE FROM users WHERE id = ?"))
    db = VerictoSQLDatabase(engine, guard=guard)
    out = db.run_no_throw("DELETE FROM users")
    assert isinstance(out, str)
    assert out.startswith("Error: Vericto blocked this query (VERICTO-010, severity critical)")
    assert "NOT executed" in out
    assert "Suggested fix: DELETE FROM users WHERE id = ?" in out
    assert "evt-blocked" in out
    assert _count(engine) == 2


def test_on_block_raise(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    db = VerictoSQLDatabase(engine, guard=guard, on_block="raise")
    with pytest.raises(VerictoBlocked):
        db.run_no_throw("DELETE FROM users")
    assert _count(engine) == 2


def test_cursor_fetch_always_raises(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    db = VerictoSQLDatabase(engine, guard=guard)
    with pytest.raises(VerictoBlocked):
        db.run("DELETE FROM users", fetch="cursor")


def test_invalid_on_block(guard: Vericto, engine: Engine) -> None:
    with pytest.raises(VerictoConfigError):
        VerictoSQLDatabase(engine, guard=guard, on_block="ignore")  # type: ignore[arg-type]


def test_masked_query_runs_the_rewrite(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users WHERE id = 1"))
    db = VerictoSQLDatabase(engine, guard=guard)
    assert db.run("SELECT email FROM users WHERE id = 1") == "[('***',)]"


def test_flagged_runs_and_reaches_on_verdict(fake: FakeVericto, evaluator: Any, engine: Engine) -> None:
    seen: List[str] = []
    g = Vericto(evaluator, on_verdict=lambda v: seen.append(v.decision.value))
    fake.on(lambda sql: "users" in sql, flagged())
    db = VerictoSQLDatabase(engine, guard=g)
    assert db.run("SELECT id FROM users WHERE id = 2") == "[(2,)]"
    assert seen == ["FLAGGED"]


def test_unavailable_is_raised_not_returned(guard: Vericto, engine: Engine, fake: FakeVericto) -> None:
    fake.on(lambda sql: True, {"status": 503, "body": {"error": {"code": "UNAVAILABLE"}}})
    db = VerictoSQLDatabase(engine, guard=guard)
    with pytest.raises(VerictoUnavailable):
        db.run_no_throw("SELECT 1")


def test_sample_rows_are_checked_masked_and_withheld_when_blocked(
    fake: FakeVericto, guard: Vericto, engine: Engine
) -> None:
    db = VerictoSQLDatabase(engine, guard=guard, sample_rows_in_table_info=2)
    fake.on(lambda sql: "LIMIT" in sql, blocked("VERICTO-085"))
    info = db.get_table_info()
    assert "sample rows withheld: Vericto blocked the query, VERICTO-085" in info
    assert "ana@example.com" not in info
    fake.rules.clear()
    fake.on(lambda sql: "LIMIT" in sql, masked("SELECT id, '***' AS email FROM users LIMIT ? OFFSET ?"))
    info = db.get_table_info()
    assert "***" in info and "ana@example.com" not in info


def test_from_uri_takes_the_vericto_arguments(fake: FakeVericto, guard: Vericto, tmp_path: Path) -> None:
    db = VerictoSQLDatabase.from_uri(f"sqlite:///{tmp_path / 'u.db'}", guard=guard, on_block="raise")
    assert isinstance(db, VerictoSQLDatabase)
    assert db.on_block == "raise"
    assert db.vericto_dialect == "sqlite"


def test_already_instrumented_engine_is_checked_once(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    vericto.sqlalchemy.instrument(engine, guard=guard)
    assert vericto.sqlalchemy.is_instrumented(engine)
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    db = VerictoSQLDatabase(engine, guard=guard, sample_rows_in_table_info=0)
    before = len(fake.requests)
    db.run("SELECT 1")
    assert len(fake.requests) == before + 1
    assert db.run("DELETE FROM users").startswith("Error: Vericto blocked")  # type: ignore[union-attr]


def test_close_vericto_detaches(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    db = VerictoSQLDatabase(engine, guard=guard)
    db.close_vericto()
    db.close_vericto()
    db.run("SELECT 1")
    assert fake.requests == []


def test_query_tool_returns_the_block_message(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DROP"), blocked("VERICTO-001"))
    tool = QuerySQLDatabaseTool(db=VerictoSQLDatabase(engine, guard=guard))
    out = tool.invoke({"query": "DROP TABLE users"})
    assert "Vericto blocked this query (VERICTO-001" in out
    assert _count(engine) == 2


def test_check_tool(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-010", fix="DELETE FROM users WHERE id = 1"))
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users"))
    db = VerictoSQLDatabase(engine, guard=guard)
    tool = VerictoCheckSQLTool(db=db)
    assert tool.name == "vericto_check_sql"
    assert tool.invoke({"query": "SELECT id FROM users"}).startswith("ALLOWED")
    masked_out = tool.invoke({"query": "SELECT email FROM users"})
    assert "masked" in masked_out and "SELECT '***' AS email FROM users" in masked_out
    out = tool.invoke({"query": "DELETE FROM users"})
    assert "VERICTO-010" in out and "Suggested fix: DELETE FROM users WHERE id = 1" in out
    assert _count(engine) == 2  # checking never executes

    raising = VerictoCheckSQLTool(guard=guard, dialect="sqlite", on_block="raise")
    with pytest.raises(VerictoBlocked):
        raising.invoke({"query": "DELETE FROM users"})


def test_check_tool_needs_a_dialect(guard: Vericto) -> None:
    with pytest.raises(ValueError, match="dialect"):
        VerictoCheckSQLTool(guard=guard)


def test_check_tool_fail_open(fake: FakeVericto, evaluator: Any) -> None:
    fake.on(lambda sql: True, {"status": 503, "body": {}})
    tool = VerictoCheckSQLTool(guard=Vericto(evaluator, fail_mode="open"), dialect="postgres")
    assert tool.invoke({"query": "SELECT 1"}).startswith("UNEVALUATED")
    closed = VerictoCheckSQLTool(guard=Vericto(evaluator), dialect="postgres")
    with pytest.raises(VerictoUnavailable):
        closed.invoke({"query": "SELECT 1"})


def test_sql_agent_reads_the_block_and_corrects_itself(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    pytest.importorskip("langchain_classic")
    from langchain_classic.agents.agent_types import AgentType
    from langchain_community.agent_toolkits import create_sql_agent

    fake.on(lambda sql: sql == "DELETE FROM users", blocked("VERICTO-010", fix="DELETE FROM users WHERE id = 2"))
    db = VerictoSQLDatabase(engine, guard=guard, sample_rows_in_table_info=0)
    llm = FakeListLLM(
        responses=[
            "I should delete the user.\nAction: sql_db_query\nAction Input: DELETE FROM users",
            "Vericto suggests a WHERE clause.\nAction: vericto_check_sql\nAction Input: DELETE FROM users WHERE id = 2",
            "It is allowed.\nAction: sql_db_query\nAction Input: DELETE FROM users WHERE id = 2",
            "Final Answer: deleted user 2",
        ]
    )
    agent = create_sql_agent(
        llm,
        toolkit=SQLDatabaseToolkit(db=db, llm=llm),
        agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
        extra_tools=[VerictoCheckSQLTool(db=db)],
        agent_executor_kwargs={"return_intermediate_steps": True},
    )
    result = agent.invoke({"input": "delete user 2"})
    steps = result["intermediate_steps"]
    assert [action.tool for action, _ in steps] == ["sql_db_query", "vericto_check_sql", "sql_db_query"]
    assert "Vericto blocked this query (VERICTO-010" in steps[0][1]
    assert steps[1][1].startswith("ALLOWED")
    assert result["output"] == "deleted user 2"
    with engine.connect() as c:
        assert [r[0] for r in c.execute(text("SELECT id FROM users"))] == [1]


def test_internal_schema_statements() -> None:
    from vericto.langchain import _internal_statements

    assert _internal_statements(None) == frozenset()
    statements = _internal_statements("analytics")
    assert {
        "SET search_path TO analytics",
        "USE analytics",
        "ALTER SESSION SET CURRENT_SCHEMA = analytics",
    } <= statements
    assert any(s.startswith("SET SCHEMA") for s in statements)
    # A name SAP HANA would refuse: no SET SCHEMA statement is expected for it.
    assert not any(s.startswith("SET SCHEMA") for s in _internal_statements('bad"; DROP TABLE x; --'))


def test_schema_database_costs_one_evaluation_per_query(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    db = VerictoSQLDatabase(engine, guard=guard, schema="main", sample_rows_in_table_info=0)
    before = len(fake.requests)
    assert "ana@example.com" in str(db.run("SELECT email FROM users WHERE id = 1"))
    assert len(fake.requests) == before + 1


def test_blocked_with_fetch_one_and_run_no_throw(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001"))
    db = VerictoSQLDatabase(engine, guard=guard, sample_rows_in_table_info=0)
    assert str(db.run("DELETE FROM users", fetch="one")).startswith("Error: Vericto blocked this query (VERICTO-001")
    assert "VERICTO-001" in str(db.run_no_throw("DELETE FROM users"))
    assert _count(engine) == 2


def test_database_properties(guard: Vericto, engine: Engine) -> None:
    db = VerictoSQLDatabase(engine, guard=guard, sample_rows_in_table_info=0)
    assert db.vericto_guard is guard and db.vericto_dialect == "sqlite" and db.on_block == "return"


def test_check_tool_builds_its_guard_from_the_environment(fake: FakeVericto, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import API_KEY, DATABASE_ID

    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_DATABASE_ID", DATABASE_ID)
    monkeypatch.setenv("VERICTO_API_URL", fake.url)
    tool = VerictoCheckSQLTool(dialect="postgresql")
    assert tool.dialect == "postgres"
    assert tool.invoke({"query": "SELECT 1"}).startswith("ALLOWED")
    assert fake.requests[-1]["dialect"] == "postgres"


def test_check_tool_prefers_explicit_guard_and_dialect_over_db(
    fake: FakeVericto, guard: Vericto, evaluator: Any, engine: Engine
) -> None:
    db = VerictoSQLDatabase(engine, guard=guard, sample_rows_in_table_info=0)
    other = Vericto(evaluator)
    tool = VerictoCheckSQLTool(db=db, guard=other, dialect="snowflake")
    assert tool.guard is other and tool.dialect == "snowflake"
    tool.guard = None
    with pytest.raises(RuntimeError, match="not initialised"):
        tool.check("SELECT 1")
