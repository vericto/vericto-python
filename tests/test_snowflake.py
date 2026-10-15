"""snowflake.connector wrappers against a fake connection: nothing connects to Snowflake.

The connector itself is installed (its statement splitter is used as is); the
connection and cursors are stand-ins that record what they were asked to run.
"""

from __future__ import annotations

from io import StringIO
from typing import Any, Iterator, List, Optional

import pytest

import vericto.snowflake
from tests.conftest import FakeVericto, blocked, masked
from vericto import Vericto, VerictoBlocked, VerictoUnsupportedDialect
from vericto.dbapi import GuardedCursor


class FakeCursor:
    def __init__(self, log: List[str]) -> None:
        self.log = log
        self.sfqid: Optional[str] = None

    def execute(self, command: str, params: Any = None, **kwargs: Any) -> FakeCursor:
        self.log.append(command)
        return self

    def executemany(self, command: str, seqparams: Any, **kwargs: Any) -> FakeCursor:
        self.log.append(command)
        return self

    def execute_async(self, command: str, params: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.log.append(command)
        return {"queryId": "01b2-async"}

    def fetchone(self) -> tuple[int]:
        return (1,)

    def close(self) -> None:
        pass


class FakeSnowflakeConnection:
    """Mimics SnowflakeConnection's public surface (paramstyle pyformat by default)."""

    def __init__(self, paramstyle: str = "pyformat") -> None:
        self._paramstyle = paramstyle
        self.log: List[str] = []
        self.scripts: List[str] = []

    def cursor(self, cursor_class: Any = None) -> FakeCursor:
        return FakeCursor(self.log)

    def execute_string(
        self, sql_text: str, remove_comments: bool = False, return_cursors: bool = True, **kw: Any
    ) -> List[FakeCursor]:
        self.scripts.append(sql_text)
        out = []
        for stmt in vericto.snowflake.split_script(sql_text, remove_comments):
            out.append(FakeCursor(self.log).execute(stmt))
        return out if return_cursors else []

    def execute_stream(self, stream: StringIO, remove_comments: bool = False, **kw: Any) -> Iterator[FakeCursor]:
        yield from self.execute_string(stream.read(), remove_comments)

    def close(self) -> None:
        pass


@pytest.fixture
def raw() -> FakeSnowflakeConnection:
    return FakeSnowflakeConnection()


@pytest.fixture
def conn(raw: FakeSnowflakeConnection, guard: Vericto) -> vericto.snowflake.SnowflakeGuardedConnection:
    return vericto.snowflake.wrap(raw, guard=guard)


def test_execute_allowed(fake: FakeVericto, raw: FakeSnowflakeConnection, conn: Any) -> None:
    cur = conn.cursor()
    assert isinstance(cur, GuardedCursor)
    assert cur.execute("SELECT 1") is cur
    assert raw.log == ["SELECT 1"]
    assert fake.requests[-1]["dialect"] == "snowflake"
    assert cur.fetchone() == (1,)


def test_pyformat_parameters_are_evaluated_as_placeholders(
    fake: FakeVericto, raw: FakeSnowflakeConnection, conn: Any
) -> None:
    conn.cursor().execute("SELECT * FROM orders WHERE id = %s AND note LIKE 'a%%'", (7,))
    assert fake.requests[-1]["query"] == "SELECT * FROM orders WHERE id = ? AND note LIKE 'a%'"
    assert raw.log == ["SELECT * FROM orders WHERE id = %s AND note LIKE 'a%%'"]  # the driver gets its own text
    conn.cursor().execute("SELECT '%%'")  # no parameters: sent exactly as written
    assert fake.requests[-1]["query"] == "SELECT '%%'"


def test_qmark_connection_is_sent_as_written(fake: FakeVericto, guard: Vericto) -> None:
    conn = vericto.snowflake.wrap(FakeSnowflakeConnection(paramstyle="qmark"), guard=guard)
    conn.cursor().execute("SELECT * FROM t WHERE a = ?", (1,))
    assert fake.requests[-1]["query"] == "SELECT * FROM t WHERE a = ?"


def test_blocked_never_reaches_snowflake(fake: FakeVericto, raw: FakeSnowflakeConnection, conn: Any) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    with pytest.raises(VerictoBlocked):
        conn.cursor().execute("DELETE FROM orders")
    with pytest.raises(VerictoBlocked):
        conn.cursor().executemany("DELETE FROM orders", [()])
    with pytest.raises(VerictoBlocked):
        conn.cursor().execute_async("DELETE FROM orders")
    assert raw.log == []


def test_masked_runs_the_rewrite_with_driver_placeholders(
    fake: FakeVericto, raw: FakeSnowflakeConnection, conn: Any
) -> None:
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users WHERE id = ?"))
    conn.cursor().execute("SELECT email FROM users WHERE id = %s", (1,))
    assert raw.log == ["SELECT '***' AS email FROM users WHERE id = %s"]


def test_execute_async(fake: FakeVericto, raw: FakeSnowflakeConnection, conn: Any) -> None:
    assert conn.cursor().execute_async("SELECT 1") == {"queryId": "01b2-async"}
    assert conn.cursor().execute_async(command="SELECT 2") == {"queryId": "01b2-async"}
    assert raw.log == ["SELECT 1", "SELECT 2"]


def test_execute_string_is_checked_per_statement_before_running(
    fake: FakeVericto, raw: FakeSnowflakeConnection, conn: Any
) -> None:
    fake.on(lambda sql: sql.startswith("DROP"), blocked("VERICTO-001"))
    script = "CREATE TABLE t (a INT);\nSELECT $$ ; $$;\nDROP TABLE t;"
    with pytest.raises(VerictoBlocked):
        conn.execute_string(script)
    assert raw.log == [] and raw.scripts == []
    assert fake.queries() == ["CREATE TABLE t (a INT);", "SELECT $$ ; $$;", "DROP TABLE t;"]


def test_execute_string_passes_the_script_through_unchanged(raw: FakeSnowflakeConnection, conn: Any) -> None:
    script = "CREATE TABLE t (a INT); -- note\nINSERT INTO t VALUES (1);"
    cursors = conn.execute_string(script)
    assert raw.scripts == [script]
    assert all(isinstance(c, GuardedCursor) for c in cursors)


def test_execute_string_with_mask(fake: FakeVericto, raw: FakeSnowflakeConnection, conn: Any) -> None:
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users"))
    conn.execute_string("USE SCHEMA s; SELECT email FROM users;")
    assert raw.scripts == ["USE SCHEMA s;\nSELECT '***' AS email FROM users;"]


def test_execute_stream(fake: FakeVericto, raw: FakeSnowflakeConnection, conn: Any) -> None:
    fake.on(lambda sql: sql.startswith("TRUNCATE"), blocked("VERICTO-011"))
    with pytest.raises(VerictoBlocked):
        list(conn.execute_stream(StringIO("SELECT 1; TRUNCATE TABLE t;")))
    assert raw.log == []
    assert len(list(conn.execute_stream(StringIO("SELECT 1; SELECT 2;")))) == 2


def test_connect_checks_the_dialect_before_connecting(
    fake: FakeVericto, guard: Vericto, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snowflake.connector

    made: List[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> FakeSnowflakeConnection:
        made.append(kwargs)
        return FakeSnowflakeConnection()

    monkeypatch.setattr(snowflake.connector, "connect", fake_connect)
    conn = vericto.snowflake.connect(guard=guard, account="xy12345", user="APP")
    assert made == [{"account": "xy12345", "user": "APP"}]
    assert conn.dialect == "snowflake" and conn.paramstyle == "pyformat"

    fake.dialects = ["postgres"]
    guard.evaluator._dialects = None  # type: ignore[attr-defined]
    with pytest.raises(VerictoUnsupportedDialect):
        vericto.snowflake.connect(guard=guard, account="xy12345")
    assert len(made) == 1


def test_guarded_cursor_context_manager_and_attributes(raw: FakeSnowflakeConnection, conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.arraysize = 50
        assert cur.wrapped.arraysize == 50
