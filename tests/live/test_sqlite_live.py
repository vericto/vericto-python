"""Opt-in end-to-end test of vericto.sqlite against a real Vericto Runtime API. Skipped unless configured.

Unlike the rest of the suite, Vericto here is a real server (staging, by default):
the statements are evaluated by the real engine against the workspace's real rules,
and run on a real SQLite file in a temporary directory. It proves that allowed
statements execute, that blocked ones raise ``VerictoBlocked`` before SQLite sees
them (checked with SQLite's own trace callback, the file's SHA-256 and the
filesystem), and that a FLAGGED statement runs with a warning.

    VERICTO_LIVE_SQLITE=1
    VERICTO_API_KEY=vtro_...                         # proxy:evaluate scope
    VERICTO_API_URL=https://api-staging.vericto.com
    VERICTO_DATABASE_ID=<uuid of a database with dialect sqlite>
    uv run pytest tests/live/test_sqlite_live.py -v

The database's workspace must use the default rules (VERICTO-001 and
VERICTO-098/100/101 block, VERICTO-099 flags). Every request is a real evaluation and
counts against the workspace's monthly quota (about 30 per run). Never run in CI.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

import vericto.sqlite
from vericto import RuntimeApiEvaluator, Vericto, VerictoBlocked
from vericto.errors import VerictoFlaggedWarning
from vericto.verdict import Decision, Verdict

REQUIRED = ("VERICTO_API_KEY", "VERICTO_API_URL", "VERICTO_DATABASE_ID")
# Read at import time: tests/conftest.py clears VERICTO_* for every test.
ENV = {name: os.environ.get(name, "") for name in REQUIRED}

pytestmark = pytest.mark.skipif(
    os.environ.get("VERICTO_LIVE_SQLITE") != "1" or not all(ENV.values()),
    reason="live SQLite test: set VERICTO_LIVE_SQLITE=1 and " + ", ".join(REQUIRED),
)

ROWS = [(1, "ana"), (2, "bo")]


class Live:
    """A guarded connection, an unguarded reader on the same file, and what SQLite ran."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.path = tmp / "live.db"
        self.verdicts: List[Verdict] = []
        self.ran: List[str] = []  # every statement SQLite executed on the guarded connection
        evaluator = RuntimeApiEvaluator(
            ENV["VERICTO_API_KEY"], database_id=ENV["VERICTO_DATABASE_ID"], api_url=ENV["VERICTO_API_URL"]
        )
        self.guard = Vericto(evaluator, fail_mode="closed", warn_on_flag=True, on_verdict=self.verdicts.append)
        # Autocommit: each statement is on disk when it returns, so the file hash is meaningful.
        self.conn = vericto.sqlite.connect(str(self.path), guard=self.guard, isolation_level=None)
        self.conn.set_trace_callback(self.ran.append)
        self.raw = sqlite3.connect(str(self.path), isolation_level=None)  # never guarded

    def rows(self) -> List[Tuple[int, str]]:
        return [(int(r[0]), str(r[1])) for r in self.raw.execute("SELECT id, name FROM t ORDER BY id")]

    def digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def files(self) -> List[str]:
        return sorted(p.name for p in self.tmp.iterdir())

    def close(self) -> None:
        self.conn.close()
        self.raw.close()


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    session = Live(tmp_path)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seeded(live: Live) -> Live:
    # Setup through the guard too: these are the benign statements of the first test.
    live.conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    live.conn.executemany("INSERT INTO t (id, name) VALUES (?, ?)", ROWS)
    assert live.rows() == ROWS
    live.ran.clear()
    live.verdicts.clear()
    return live


def assert_blocked_before_sqlite(live: Live, sql: str, rule: str) -> None:
    before = (live.digest(), live.rows(), live.files(), len(live.ran))
    with pytest.raises(VerictoBlocked) as exc:
        live.conn.execute(sql)
    assert exc.value.verdict.rule_code == rule, exc.value.verdict
    assert exc.value.verdict.event_id  # recorded in the server's audit trail
    assert (live.digest(), live.rows(), live.files(), len(live.ran)) == before
    assert sql not in live.ran


def test_server_lists_sqlite(live: Live) -> None:
    supported = live.guard.evaluator.supported_dialects()
    assert supported is not None and "sqlite" in supported


def test_benign_statements_execute(live: Live) -> None:
    conn = live.conn
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("INSERT INTO t (id, name) VALUES (?, ?)", (1, "ana"))
    conn.executemany("INSERT INTO t (id, name) VALUES (?, ?)", [(2, "bo"), (3, "cy")])
    conn.execute(
        "INSERT INTO t (id, name) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET name = excluded.name", (2, "bea")
    )
    conn.execute(
        "INSERT INTO t (id, name) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET name = excluded.name", (4, "di")
    )
    cur = conn.execute("SELECT id, name FROM t WHERE id >= ? ORDER BY id", (2,))
    assert cur.fetchall() == [(2, "bea"), (3, "cy"), (4, "di")]
    conn.execute("DELETE FROM t WHERE id = ?", (3,))

    assert live.rows() == [(1, "ana"), (2, "bea"), (4, "di")]  # read by an unguarded connection
    assert all(v.decision is not Decision.BLOCKED for v in live.verdicts)
    assert len(live.verdicts) == 7
    assert "bea" not in str([v.sql for v in live.verdicts])  # bind values never go to Vericto


def test_delete_without_where_is_blocked(seeded: Live) -> None:
    assert_blocked_before_sqlite(seeded, "DELETE FROM t", "VERICTO-001")


def test_attach_database_is_blocked(seeded: Live) -> None:
    other = seeded.tmp / "attached.db"
    assert_blocked_before_sqlite(seeded, f"ATTACH DATABASE '{other}' AS other", "VERICTO-098")
    assert not other.exists()
    names = [r[1] for r in sqlite3.Cursor.execute(seeded.conn.cursor(), "PRAGMA database_list")]
    assert names == ["main"]  # nothing attached to the guarded connection


def test_load_extension_is_blocked(seeded: Live) -> None:
    # A VerictoBlocked, not SQLite's "not authorized": the call never reached SQLite.
    assert_blocked_before_sqlite(seeded, f"SELECT load_extension('{seeded.tmp / 'evil'}')", "VERICTO-100")


def test_vacuum_into_is_blocked(seeded: Live) -> None:
    copy = seeded.tmp / "copy.db"
    assert_blocked_before_sqlite(seeded, f"VACUUM INTO '{copy}'", "VERICTO-101")
    assert not copy.exists()


def test_pragma_foreign_keys_off_is_flagged_and_runs(seeded: Live) -> None:
    sqlite3.Cursor.execute(seeded.conn.cursor(), "PRAGMA foreign_keys = ON")  # unguarded setup
    with pytest.warns(VerictoFlaggedWarning, match="VERICTO-099"):
        seeded.conn.execute("PRAGMA foreign_keys = OFF")
    assert seeded.verdicts[-1].decision is Decision.FLAGGED
    assert seeded.verdicts[-1].rule_code == "VERICTO-099"
    assert "PRAGMA foreign_keys = OFF" in seeded.ran
    assert sqlite3.Cursor.execute(seeded.conn.cursor(), "PRAGMA foreign_keys").fetchone() == (0,)


def test_script_with_a_blocked_statement_runs_none(seeded: Live) -> None:
    before = (seeded.digest(), seeded.rows(), len(seeded.ran))
    script = "INSERT INTO t (id, name) VALUES (10, 'x');\nDELETE FROM t;\nINSERT INTO t (id, name) VALUES (11, 'y');\n"
    with pytest.raises(VerictoBlocked) as exc:
        seeded.conn.executescript(script)
    assert exc.value.verdict.rule_code == "VERICTO-001"
    assert (seeded.digest(), seeded.rows(), len(seeded.ran)) == before

    # The same script without the DELETE runs whole.
    seeded.conn.executescript(script.replace("DELETE FROM t;\n", ""))
    assert seeded.rows() == [*ROWS, (10, "x"), (11, "y")]
