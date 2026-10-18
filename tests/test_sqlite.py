"""End to end with the real sqlite3 module and a local Runtime API stand-in."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import vericto.sqlite
from tests.conftest import DIALECTS_WITHOUT_SQLITE, FakeVericto, blocked, masked
from vericto import RuntimeApiEvaluator, Vericto, VerictoBlocked, VerictoUnsupportedDialect


@pytest.fixture
def db(tmp_path: Path, guard: Vericto) -> vericto.sqlite.VerictoSQLiteConnection:
    conn = vericto.sqlite.connect(str(tmp_path / "app.db"), guard=guard)
    sqlite3.connect(str(tmp_path / "app.db")).executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);"
        "INSERT INTO users VALUES (1, 'ana@example.com'), (2, 'bo@example.com');"
    )
    return conn


def count(conn: sqlite3.Connection) -> int:
    row = sqlite3.Cursor.execute(conn.cursor(), "SELECT count(*) FROM users").fetchone()
    return int(row[0])


def test_is_a_real_sqlite_connection(db: vericto.sqlite.VerictoSQLiteConnection) -> None:
    assert isinstance(db, sqlite3.Connection)
    assert isinstance(db.cursor(), sqlite3.Cursor)


def test_allowed_statement_runs_with_parameters(fake: FakeVericto, db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT email FROM users WHERE id = ?", (1,)).fetchall()
    assert rows == [("ana@example.com",)]
    assert fake.requests[-1]["query"] == "SELECT email FROM users WHERE id = ?"
    assert fake.requests[-1]["dialect"] == "sqlite"
    assert "ana@example.com" not in str(fake.requests)  # values are never sent


def test_blocked_statement_never_runs(fake: FakeVericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql == "DELETE FROM users", blocked(fix="DELETE FROM users WHERE id = ?"))
    with pytest.raises(VerictoBlocked) as info:
        db.execute("DELETE FROM users")
    assert info.value.suggested_fix == "DELETE FROM users WHERE id = ?"
    assert count(db) == 2
    with pytest.raises(VerictoBlocked):
        db.cursor().execute("DELETE FROM users")
    assert count(db) == 2


def test_masked_statement_runs_the_rewrite(fake: FakeVericto, db: sqlite3.Connection) -> None:
    fake.on(
        lambda sql: sql.startswith("SELECT email"),
        masked("SELECT '***' AS email FROM users WHERE id = ?"),
    )
    assert db.execute("SELECT email FROM users WHERE id = ?", (2,)).fetchall() == [("***",)]


def test_executemany_checks_once(fake: FakeVericto, db: sqlite3.Connection) -> None:
    db.executemany("INSERT INTO users (email) VALUES (?)", [("c@x.io",), ("d@x.io",)])
    assert fake.queries().count("INSERT INTO users (email) VALUES (?)") == 1
    assert count(db) == 4


def test_executescript_evaluates_every_statement_first(fake: FakeVericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql.startswith("DROP"), blocked("VERICTO-001"))
    script = "INSERT INTO users (email) VALUES ('e@x.io');\nDROP TABLE users;\nINSERT INTO users (email) VALUES ('f');"
    with pytest.raises(VerictoBlocked):
        db.executescript(script)
    assert count(db) == 2  # the first INSERT did not run either
    assert fake.queries()[-2:] == ["INSERT INTO users (email) VALUES ('e@x.io');", "DROP TABLE users;"]


def test_executescript_with_mask_runs_rewritten_script(fake: FakeVericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql.startswith("CREATE VIEW"), masked("CREATE VIEW v AS SELECT id, '***' AS email FROM users;"))
    db.executescript("CREATE VIEW v AS SELECT id, email FROM users; SELECT 1;")
    assert db.execute("SELECT email FROM v WHERE id = 1").fetchone() == ("***",)


def test_split_script_respects_triggers_strings_and_comments() -> None:
    script = """
    -- leading comment
    CREATE TABLE a (x TEXT);
    INSERT INTO a VALUES ('semi;colon');
    CREATE TRIGGER t AFTER INSERT ON a BEGIN
      INSERT INTO a VALUES ('x'); DELETE FROM a WHERE x = 'y';
    END;
    /* trailing */ SELECT 1; SELECT 2
    """
    parts = vericto.sqlite.split_script(script)
    assert len(parts) == 5
    assert parts[2].startswith("CREATE TRIGGER") and parts[2].endswith("END;")
    assert parts[1] == "INSERT INTO a VALUES ('semi;colon');"
    assert parts[4] == "SELECT 2"


def test_connect_refuses_when_the_server_lacks_sqlite(fake: FakeVericto, guard: Vericto, tmp_path: Path) -> None:
    fake.dialects = list(DIALECTS_WITHOUT_SQLITE)
    with pytest.raises(VerictoUnsupportedDialect, match="'sqlite' dialect"):
        vericto.sqlite.connect(str(tmp_path / "x.db"), guard=guard)
    assert not (tmp_path / "x.db").exists()
    assert fake.requests == []


def test_foreign_cursor_factory_is_refused(db: vericto.sqlite.VerictoSQLiteConnection) -> None:
    with pytest.raises(TypeError):
        db.cursor(sqlite3.Cursor)


def test_factory_argument_is_refused(guard: Vericto) -> None:
    with pytest.raises(TypeError):
        vericto.sqlite.connect(":memory:", guard=guard, factory=sqlite3.Connection)


def test_a_connection_without_a_guard_refuses_to_run(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:", factory=vericto.sqlite.VerictoSQLiteConnection)
    with pytest.raises(RuntimeError, match=r"not opened with vericto\.sqlite\.connect"):
        conn.execute("SELECT 1")


def test_cursor_factory_subclass_is_accepted(fake: FakeVericto, db: vericto.sqlite.VerictoSQLiteConnection) -> None:
    class Mine(vericto.sqlite.VerictoSQLiteCursor):
        pass

    cur = db.cursor(Mine)
    assert isinstance(cur, Mine)
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    with pytest.raises(VerictoBlocked):
        cur.execute("DELETE FROM users")
    assert count(db) == 2


def test_cursor_executemany_and_executescript_are_checked(fake: FakeVericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql.startswith("UPDATE"), blocked("VERICTO-002"))
    cur = db.cursor()
    with pytest.raises(VerictoBlocked):
        cur.executemany("UPDATE users SET email = ?", [("x",), ("y",)])
    with pytest.raises(VerictoBlocked):
        cur.executescript("SELECT 1; UPDATE users SET email = 'z';")
    assert db.execute("SELECT email FROM users WHERE id = 1").fetchone() == ("ana@example.com",)


def test_named_and_numbered_parameters_are_sent_as_written(fake: FakeVericto, db: sqlite3.Connection) -> None:
    assert db.execute("SELECT email FROM users WHERE id = :id", {"id": 2}).fetchone() == ("bo@example.com",)
    assert db.execute("SELECT email FROM users WHERE id = ?1", (1,)).fetchone() == ("ana@example.com",)
    assert fake.queries()[-2:] == ["SELECT email FROM users WHERE id = :id", "SELECT email FROM users WHERE id = ?1"]


def test_flagged_statement_runs_and_warns(fake: FakeVericto, evaluator: RuntimeApiEvaluator, tmp_path: Path) -> None:
    from tests.conftest import flagged
    from vericto import VerictoFlaggedWarning

    fake.on(lambda sql: sql == "PRAGMA foreign_keys = OFF", flagged("VERICTO-099"))
    conn = vericto.sqlite.connect(str(tmp_path / "f.db"), guard=Vericto(evaluator, warn_on_flag=True))
    with pytest.warns(VerictoFlaggedWarning, match="VERICTO-099"):
        conn.execute("PRAGMA foreign_keys = OFF")
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (0,)


def test_fail_open_and_fail_closed(fake: FakeVericto, evaluator: RuntimeApiEvaluator, tmp_path: Path) -> None:
    from vericto import VerictoFailOpenWarning, VerictoUnavailable

    fake.on(lambda sql: True, {"status": 502, "body": {}})
    closed = vericto.sqlite.connect(str(tmp_path / "c.db"), guard=Vericto(evaluator))
    with pytest.raises(VerictoUnavailable):
        closed.execute("CREATE TABLE t (a INT)")
    assert sqlite3.connect(str(tmp_path / "c.db")).execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)
    opened = vericto.sqlite.connect(str(tmp_path / "o.db"), guard=Vericto(evaluator, fail_mode="open"))
    with pytest.warns(VerictoFailOpenWarning):
        opened.executescript("CREATE TABLE t (a INT); INSERT INTO t VALUES (1);")
    assert sqlite3.Cursor.execute(opened.cursor(), "SELECT a FROM t").fetchall() == [(1,)]


def test_split_script_edge_cases() -> None:
    assert vericto.sqlite.split_script("") == []
    assert vericto.sqlite.split_script("  ;;  -- nothing\n/* here */ ;") == []
    assert vericto.sqlite.split_script("SELECT 1;;SELECT 2;") == ["SELECT 1;", "SELECT 2;"]
    assert vericto.sqlite.split_script("SELECT 'a;b' -- c;d\n; SELECT 2") == ["SELECT 'a;b' -- c;d\n;", "SELECT 2"]


def test_empty_script_evaluates_nothing(fake: FakeVericto, db: sqlite3.Connection) -> None:
    before = len(fake.requests)
    db.executescript("-- nothing to do\n")
    assert len(fake.requests) == before


def test_masked_script_keeps_statement_boundaries(fake: FakeVericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql.startswith("CREATE VIEW"), masked("CREATE VIEW v AS SELECT id, '***' AS email FROM users"))
    # The last statement has no ';' and ends with a comment.
    db.executescript("CREATE VIEW v AS SELECT id, email FROM users; CREATE TABLE audit (n INT) -- tail")
    assert db.execute("SELECT email FROM v WHERE id = 2").fetchone() == ("***",)
    assert db.execute("SELECT count(*) FROM audit").fetchone() == (0,)


def test_connect_builds_the_guard_from_the_environment(
    fake: FakeVericto, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.conftest import API_KEY, DATABASE_ID

    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_DATABASE_ID", DATABASE_ID)
    monkeypatch.setenv("VERICTO_API_URL", fake.url)
    conn = vericto.sqlite.connect(str(tmp_path / "env.db"))
    conn.execute("SELECT 1")
    assert fake.requests[-1]["dialect"] == "sqlite"
