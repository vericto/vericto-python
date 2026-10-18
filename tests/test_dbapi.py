"""vericto.dbapi.wrap() against a recording fake DB-API driver and the local Runtime API stand-in."""

from __future__ import annotations

import sqlite3
import sys
import types
import warnings
from typing import Any, Iterator, List, Optional

import pytest

import vericto
from tests.conftest import API_KEY, DATABASE_ID, FakeVericto, blocked, flagged, masked
from vericto import (
    Decision,
    PlaceholderMappingError,
    Verdict,
    Vericto,
    VerictoBlocked,
    VerictoFailOpenWarning,
    VerictoFlaggedWarning,
    VerictoUnavailable,
    VerictoUnsupportedDialect,
)
from vericto.dbapi import GuardedConnection, GuardedCursor, infer_paramstyle

DRIVER = "vericto_fake_driver"


class FakeCursor:
    """Records what reaches the driver; returns itself like most drivers do."""

    def __init__(self, log: List[tuple[str, Any]], connection: Any = None) -> None:
        self.log = log
        self.connection = connection
        self.arraysize = 1
        self.rows: List[tuple[Any, ...]] = [(1,), (2,)]
        self.closed = False

    def execute(self, operation: str, parameters: Any = None) -> FakeCursor:
        self.log.append((operation, parameters))
        return self

    def executemany(self, operation: str, seq_of_parameters: Any) -> FakeCursor:
        self.log.append((operation, list(seq_of_parameters)))
        return self

    def fetchone(self) -> Optional[tuple[Any, ...]]:
        return self.rows.pop(0) if self.rows else None

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(list(self.rows))

    def __next__(self) -> tuple[Any, ...]:
        if not self.rows:
            raise StopIteration
        return self.rows.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """A DB-API connection of a ``format`` driver (psycopg-like), with no execute() shortcut."""

    def __init__(self) -> None:
        self.log: List[tuple[str, Any]] = []
        self.committed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.log, self)

    def commit(self) -> None:
        self.committed = True


FakeConnection.__module__ = f"{DRIVER}.connection"


@pytest.fixture(autouse=True)
def _driver_module(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType(DRIVER)
    module.paramstyle = "format"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, DRIVER, module)


@pytest.fixture
def raw() -> FakeConnection:
    return FakeConnection()


@pytest.fixture
def conn(raw: FakeConnection, guard: Vericto) -> GuardedConnection:
    return vericto.wrap(raw, dialect="postgresql", guard=guard)


# -- everyday paths ---------------------------------------------------------------


def test_allowed_statement_reaches_the_driver(fake: FakeVericto, raw: FakeConnection, conn: GuardedConnection) -> None:
    cur = conn.cursor()
    assert isinstance(cur, GuardedCursor)
    assert cur.execute("SELECT 1") is cur  # the guard stays on for chained calls
    assert raw.log == [("SELECT 1", None)]
    assert fake.requests[-1]["dialect"] == "postgres"
    assert fake.requests[-1]["database_id"] == DATABASE_ID
    assert cur.fetchone() == (1,)


def test_blocked_statement_never_reaches_the_driver(
    fake: FakeVericto, raw: FakeConnection, conn: GuardedConnection
) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked(fix="DELETE FROM users WHERE id = $1"))
    with pytest.raises(VerictoBlocked) as info:
        conn.cursor().execute("DELETE FROM users")
    with pytest.raises(VerictoBlocked):
        conn.cursor().executemany("DELETE FROM users WHERE id = %s", [(1,), (2,)])
    assert raw.log == []
    assert info.value.suggested_fix == "DELETE FROM users WHERE id = $1"
    assert info.value.sql == "DELETE FROM users"


def test_flagged_statement_runs_and_is_reported(fake: FakeVericto, raw: FakeConnection, evaluator: Any) -> None:
    fake.on(lambda sql: sql.startswith("SELECT *"), flagged("VERICTO-030"))
    seen: List[Verdict] = []
    guard = Vericto(evaluator, on_verdict=seen.append, warn_on_flag=True)
    conn = vericto.wrap(raw, dialect="postgres", guard=guard)
    with pytest.warns(VerictoFlaggedWarning, match="VERICTO-030"):
        conn.cursor().execute("SELECT * FROM users")
    assert raw.log == [("SELECT * FROM users", None)]
    assert [v.decision for v in seen] == [Decision.FLAGGED]
    assert seen[0].rule_code == "VERICTO-030"


def test_masked_statement_runs_the_rewrite(fake: FakeVericto, raw: FakeConnection, conn: GuardedConnection) -> None:
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users WHERE id = $1"))
    conn.cursor().execute("SELECT email FROM users WHERE id = %s", (7,))
    # Evaluated with the dialect's own placeholder, run with the driver's.
    assert fake.requests[-1]["query"] == "SELECT email FROM users WHERE id = $1"
    assert raw.log == [("SELECT '***' AS email FROM users WHERE id = %s", (7,))]


def test_executemany_is_one_evaluation(fake: FakeVericto, raw: FakeConnection, conn: GuardedConnection) -> None:
    conn.cursor().executemany("INSERT INTO t (a, b) VALUES (%s, %s)", [(1, 2), (3, 4), (5, 6)])
    assert fake.queries() == ["INSERT INTO t (a, b) VALUES ($1, $2)"]
    assert raw.log == [("INSERT INTO t (a, b) VALUES (%s, %s)", [(1, 2), (3, 4), (5, 6)])]


def test_bind_values_are_never_sent(fake: FakeVericto, conn: GuardedConnection) -> None:
    conn.cursor().execute("SELECT * FROM users WHERE email = %s", ("ana@example.com",))
    assert "ana@example.com" not in str(fake.requests)


def test_masked_rewrite_that_cannot_be_mapped_back_fails_closed(
    fake: FakeVericto, raw: FakeConnection, evaluator: Any
) -> None:
    # A rewrite with a placeholder the statement did not have cannot be bound: nothing runs.
    fake.on(lambda sql: True, masked("SELECT '***' AS email FROM users WHERE id = $1 AND org = $2"))
    conn = vericto.wrap(raw, dialect="postgres", guard=Vericto(evaluator))
    with pytest.raises(PlaceholderMappingError):
        conn.cursor().execute("SELECT email FROM users WHERE id = %s", (1,))
    assert raw.log == []


# -- placeholders in every DB-API paramstyle ----------------------------------------


@pytest.mark.parametrize(
    ("paramstyle", "dialect", "sql", "evaluated"),
    [
        ("qmark", "sqlite", "SELECT * FROM t WHERE a = ? AND b = ?", "SELECT * FROM t WHERE a = ? AND b = ?"),
        ("numeric", "oracle", "SELECT * FROM t WHERE a = :1 AND b = :2", "SELECT * FROM t WHERE a = :1 AND b = :2"),
        ("named", "oracle", "SELECT * FROM t WHERE a = :a AND b = :b", "SELECT * FROM t WHERE a = :a AND b = :b"),
        (
            "format",
            "mysql",
            "SELECT * FROM t WHERE a = %s AND b LIKE 'x%%'",
            "SELECT * FROM t WHERE a = ? AND b LIKE 'x%'",
        ),
        ("format", "postgres", "SELECT * FROM t WHERE a = %s AND b = %s", "SELECT * FROM t WHERE a = $1 AND b = $2"),
        (
            "pyformat",
            "postgres",
            "SELECT * FROM t WHERE a = %(a)s OR b = %(b)s OR c = %(a)s",
            "SELECT * FROM t WHERE a = $1 OR b = $2 OR c = $1",
        ),
        ("pyformat", "mssql", "SELECT * FROM t WHERE a = %(a)s", "SELECT * FROM t WHERE a = ?"),
    ],
)
def test_every_paramstyle(
    fake: FakeVericto, raw: FakeConnection, guard: Vericto, paramstyle: str, dialect: str, sql: str, evaluated: str
) -> None:
    conn = vericto.wrap(raw, dialect=dialect, guard=guard, paramstyle=paramstyle)
    assert conn.paramstyle == paramstyle
    conn.cursor().execute(sql, {"a": 1, "b": 2} if "%(" in sql or ":a" in sql else (1, 2))
    assert fake.requests[-1]["query"] == evaluated
    assert raw.log == [(sql, raw.log[0][1])]  # the driver always gets the original text


def test_without_parameters_percent_text_is_sent_as_written(
    fake: FakeVericto, raw: FakeConnection, conn: GuardedConnection
) -> None:
    conn.cursor().execute("SELECT 'a%%b'")
    conn.cursor().execute("SELECT 'a%%b'", None)  # None means "no parameters" in DB-API
    assert fake.queries() == ["SELECT 'a%%b'", "SELECT 'a%%b'"]


class FakeCursorKw(FakeCursor):
    def execute(self, operation: str, **kwargs: Any) -> FakeCursorKw:  # type: ignore[override]
        self.log.append((operation, kwargs))
        return self


@pytest.mark.parametrize("key", ["params", "parameters", "args", "vars"])
def test_parameters_passed_by_keyword_count(
    fake: FakeVericto, raw: FakeConnection, conn: GuardedConnection, key: str
) -> None:
    cur = FakeCursorKw(raw.log)
    guarded = GuardedCursor(cur, conn.guard, "postgres", conn, "format")
    guarded.execute("SELECT * FROM t WHERE a = %s", **{key: (1,)})
    assert fake.requests[-1]["query"] == "SELECT * FROM t WHERE a = $1"


def test_paramstyle_is_inferred_from_the_driver_module(raw: FakeConnection) -> None:
    assert infer_paramstyle(raw) == "format"

    class PerConnection:
        _paramstyle = "qmark"  # snowflake-connector-python keeps it on the connection

    assert infer_paramstyle(PerConnection()) == "qmark"
    assert infer_paramstyle(object()) is None  # builtins has no paramstyle
    assert infer_paramstyle(sqlite3.connect(":memory:")) == "qmark"


# -- the rest of the DB-API surface ----------------------------------------------------


def test_connection_execute_shortcut(fake: FakeVericto, guard: Vericto) -> None:
    raw = sqlite3.connect(":memory:")
    conn = vericto.wrap(raw, dialect="sqlite3", guard=guard)
    cur = conn.execute("SELECT ? + 1", (1,))
    assert isinstance(cur, GuardedCursor)  # sqlite3 returns a cursor: it is guarded too
    assert cur.fetchone() == (2,)
    assert fake.requests[-1]["query"] == "SELECT ? + 1"
    fake.on(lambda sql: sql.startswith("DROP"), blocked("VERICTO-001"))
    raw.execute("CREATE TABLE t (a INT)")
    with pytest.raises(VerictoBlocked):
        conn.execute("DROP TABLE t")
    assert raw.execute("SELECT count(*) FROM sqlite_master WHERE name = 't'").fetchone() == (1,)


def test_connection_execute_returning_a_non_cursor(fake: FakeVericto, guard: Vericto) -> None:
    class RowCountConnection(FakeConnection):
        def execute(self, operation: str) -> int:
            self.log.append((operation, None))
            return 3

    raw = RowCountConnection()
    assert vericto.wrap(raw, dialect="postgres", guard=guard).execute("UPDATE t SET a = 1 WHERE b = 2") == 3


def test_connection_without_execute_says_so(conn: GuardedConnection) -> None:
    with pytest.raises(AttributeError, match="use cursor"):
        conn.execute("SELECT 1")


def test_attributes_pass_through(raw: FakeConnection, conn: GuardedConnection) -> None:
    assert conn.wrapped is raw
    assert conn.dialect == "postgres"
    conn.commit()
    assert raw.committed
    conn.autocommit = True
    assert raw.autocommit is True
    assert "dialect=postgres" in repr(conn)

    cur = conn.cursor()
    assert cur.connection is conn
    assert cur.wrapped.connection is raw
    cur.arraysize = 100
    assert cur.wrapped.arraysize == 100
    assert "vericto guarded" in repr(cur)
    assert list(iter(cur)) == [(1,), (2,)]
    assert next(cur) == (1,)

    bare = GuardedCursor(FakeCursor([], raw), conn.guard, "postgres")
    assert bare.connection is raw  # falls back to the driver cursor's connection


def test_context_managers(raw: FakeConnection, conn: GuardedConnection) -> None:
    # Neither the fake connection nor its cursor are context managers: the wrapper still is.
    with conn as c:
        assert c is conn
        with c.cursor() as cur:
            cur.execute("SELECT 1")
        assert cur.wrapped.closed  # closed on exit when the driver cursor has no __exit__

    native = vericto.wrap(sqlite3.connect(":memory:"), dialect="sqlite", guard=conn.guard)
    with native as n:
        n.execute("CREATE TABLE t (a INT)")
        n.execute("INSERT INTO t VALUES (1)")
    assert native.wrapped.execute("SELECT count(*) FROM t").fetchone() == (1,)  # committed by sqlite3's __exit__


# -- configuration and availability ------------------------------------------------------


def test_wrap_builds_the_guard_from_the_environment(
    fake: FakeVericto, raw: FakeConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_DATABASE_ID", DATABASE_ID)
    monkeypatch.setenv("VERICTO_API_URL", fake.url)
    monkeypatch.setenv("VERICTO_TIMEOUT", "2")
    conn = vericto.wrap(raw, dialect="postgres")
    assert conn.guard.fail_mode == "closed"
    assert conn.guard.evaluator.api_url == fake.url  # type: ignore[attr-defined]
    conn.cursor().execute("SELECT 1")
    assert fake.requests[-1]["_headers"]["X-Api-Key"] == API_KEY


def test_wrap_refuses_an_unsupported_dialect_before_any_sql(
    fake: FakeVericto, raw: FakeConnection, guard: Vericto
) -> None:
    fake.dialects = ["postgres"]
    with pytest.raises(VerictoUnsupportedDialect):
        vericto.wrap(raw, dialect="mysql", guard=guard)
    assert fake.requests == []


def test_runtime_api_down_fail_closed_and_fail_open(fake: FakeVericto, raw: FakeConnection, evaluator: Any) -> None:
    fake.on(lambda sql: True, {"status": 503, "body": {"error": {"code": "SERVICE_UNAVAILABLE"}}})
    closed = vericto.wrap(raw, dialect="postgres", guard=Vericto(evaluator, fail_mode="closed"))
    with pytest.raises(VerictoUnavailable, match="HTTP 503"):
        closed.cursor().execute("SELECT 1")
    assert raw.log == []

    seen: List[Verdict] = []
    opened = vericto.wrap(raw, dialect="postgres", guard=Vericto(evaluator, fail_mode="open", on_verdict=seen.append))
    with pytest.warns(VerictoFailOpenWarning, match="ran unchecked"):
        opened.cursor().execute("SELECT 1")
    assert raw.log == [("SELECT 1", None)]
    assert seen[0].degraded


def test_fail_open_keeps_the_original_text_with_parameters(
    fake: FakeVericto, raw: FakeConnection, evaluator: Any
) -> None:
    fake.on(lambda sql: True, {"status": 500, "body": {}})
    conn = vericto.wrap(raw, dialect="postgres", guard=Vericto(evaluator, fail_mode="open"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", VerictoFailOpenWarning)
        conn.cursor().execute("SELECT * FROM t WHERE a = %s", (1,))
    assert raw.log == [("SELECT * FROM t WHERE a = %s", (1,))]


def test_driver_cursor_context_manager_is_used(raw: FakeConnection, conn: GuardedConnection) -> None:
    events: List[str] = []

    class ManagedCursor(FakeCursor):
        def __enter__(self) -> ManagedCursor:
            events.append("enter")
            return self

        def __exit__(self, *exc: Any) -> None:
            events.append("exit")

    with GuardedCursor(ManagedCursor(raw.log, raw), conn.guard, "postgres") as cur:
        cur.execute("SELECT 1")
    assert events == ["enter", "exit"]
    assert raw.log == [("SELECT 1", None)]
