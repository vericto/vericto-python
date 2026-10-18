"""vericto.sanitize: vericto-cli's own test vectors, then per-dialect and edge cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from vericto.sanitize import sanitize

# vericto-cli's sanitize.rs test vectors (release 1.8.0), copied verbatim: the port must give the same
# output for each, so the two cannot drift apart unnoticed. See "source" in the file.
VERICTO_CLI_VECTORS = Path(__file__).parent / "fixtures" / "sanitize" / "vericto-cli-1.8.0.json"


def _vectors() -> List[Dict[str, Any]]:
    data = json.loads(VERICTO_CLI_VECTORS.read_text(encoding="utf-8"))
    vectors: List[Dict[str, Any]] = data["vectors"]
    return vectors


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: f"{v['test']}-{v['dialect']}")
def test_matches_vericto_cli_test_vectors(vector: Dict[str, Any]) -> None:
    assert sanitize(vector["sql"], vector["dialect"]) == vector["expected"]


def test_vericto_cli_vectors_cover_every_placeholder_style() -> None:
    dialects = {v["dialect"] for v in _vectors()}
    assert {"postgres", "mysql", "snowflake", "sqlite"} <= dialects


# Per dialect: placeholders, bind parameters already in the statement, dollar quotes, stage
# references and identifiers. Every expected value is also what vericto-cli 1.8.0 returns.
DIALECT_CASES = [
    # PostgreSQL: numbered placeholders; $N binds kept (the counter does not skip them, as in vericto-cli).
    ("SELECT a FROM t WHERE id = $1 AND n = 5", "postgres", "SELECT a FROM t WHERE id = $1 AND n = $1"),
    ("SELECT a FROM t WHERE id = $12", "postgres", "SELECT a FROM t WHERE id = $12"),
    ("SELECT $body$ 'x' $body$, 'y'", "postgres", "SELECT $body$ 'x' $body$, $1"),
    ('SELECT "col1", t1.c2 FROM t1 WHERE x = 3', "postgres", 'SELECT "col1", t1.c2 FROM t1 WHERE x = $1'),
    # MySQL: ? placeholders; existing ? binds kept; backtick names verbatim.
    ("SELECT a FROM t WHERE id = ? AND n = 5", "mysql", "SELECT a FROM t WHERE id = ? AND n = ?"),
    ("SELECT `col 1`, `t2` FROM t WHERE x = 'a\\'b'", "mysql", "SELECT `col 1`, `t2` FROM t WHERE x = ?"),
    ("SELECT $1 FROM t", "mysql", "SELECT $1 FROM t"),
    # Oracle: numbered placeholders; :name binds kept.
    (
        "SELECT 'a' || name FROM t WHERE id = :id AND n = 10",
        "oracle",
        "SELECT $1 || name FROM t WHERE id = :id AND n = $2",
    ),
    ("SELECT a FROM t WHERE id = $1", "oracle", "SELECT a FROM t WHERE id = $1"),
    # MSSQL: numbered placeholders; @p1 binds kept.
    (
        "SELECT [name] FROM t WHERE id = @p1 AND n = 3 AND s = 'x'",
        "mssql",
        "SELECT [name] FROM t WHERE id = @p1 AND n = $1 AND s = $2",
    ),
    # Snowflake: ? placeholders; $1 stage columns and ? binds kept; $$ strings verbatim.
    (
        "SELECT $1, $2 FROM @stage/file.csv WHERE a = 5",
        "snowflake",
        "SELECT $1, $2 FROM @stage/file.csv WHERE a = ?",
    ),
    ("SELECT t.$1 FROM @s t WHERE id = ? AND n = 2", "snowflake", "SELECT t.$1 FROM @s t WHERE id = ? AND n = ?"),
    ("SELECT $$it's$$, 'x'", "snowflake", "SELECT $$it's$$, ?"),
    # SQLite: ? placeholders; [name] verbatim; :a, @b, $c, ?3 binds kept; $$ is not a dollar quote.
    (
        "SELECT [col 1] FROM t WHERE a = :a AND b = @b AND c = $c AND d = ?3 AND e = 4",
        "sqlite",
        "SELECT [col 1] FROM t WHERE a = :a AND b = @b AND c = $c AND d = ?3 AND e = ?",
    ),
    ("SELECT $$ 'x' $$", "sqlite", "SELECT $$ ? $$"),
    ("SELECT [open 1", "sqlite", "SELECT [open 1"),
    # Any other dialect gets numbered placeholders, the safe default.
    ("WHERE a = 'x' AND b = 2", "bigquery", "WHERE a = $1 AND b = $2"),
]


@pytest.mark.parametrize(("sql", "dialect", "expected"), DIALECT_CASES)
def test_dialects(sql: str, dialect: str, expected: str) -> None:
    assert sanitize(sql, dialect) == expected


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        # Dollar-quoted bodies with a tag stay; an unterminated or invalid tag is plain text.
        ("SELECT $fn$ 'a' $fn$, 'b'", "postgres", "SELECT $fn$ 'a' $fn$, $1"),
        ("SELECT $fn$ 'a'", "postgres", "SELECT $fn$ $1"),
        ("SELECT $a1$ 'x' $a1$", "postgres", "SELECT $a1$ 'x' $a1$"),
        ("SELECT $", "postgres", "SELECT $"),
        ("SELECT $abc", "postgres", "SELECT $abc"),
        # Unterminated quoted names, comments and strings run to the end without hanging.
        ('SELECT "open', "postgres", 'SELECT "open'),
        ("SELECT 1 /* open 'x'", "postgres", "SELECT $1 /* open 'x'"),
        ("SELECT 1 -- 'x'", "postgres", "SELECT $1 -- 'x'"),
        ("SELECT 'open", "postgres", "SELECT $1"),
        # Decimals, identifiers with digits, signs.
        ("WHERE price > 10.50 AND t2.x = -3", "postgres", "WHERE price > $1 AND t2.x = -$2"),
        ("WHERE a = 1 AND b = 'two'", "mysql", "WHERE a = ? AND b = ?"),
        # An E prefix that is the end of an identifier is not an escape string.
        ("WHERE name = 'x' AND typE'y'", "postgres", "WHERE name = $1 AND typE$2"),
        # Backslashes are literal outside MySQL and E-strings.
        ("WHERE p = 'a\\' AND q = 'b'", "snowflake", "WHERE p = ? AND q = ?"),
        # Comments inside a MySQL statement stay as written.
        ("SELECT /* 'x' */ a FROM `t1`", "mysql", "SELECT /* 'x' */ a FROM `t1`"),
        ("", "postgres", ""),
    ],
)
def test_edge_cases(sql: str, dialect: str, expected: str) -> None:
    assert sanitize(sql, dialect) == expected


def test_no_literal_survives_a_realistic_model() -> None:
    sql = (
        "-- model: orders\n"
        "SELECT o.id, c.email, 'VIP' AS tier, 0.15 * o.total AS discount\n"
        "FROM orders o JOIN customers c ON c.id = o.customer_id\n"
        "WHERE c.email = 'ana@example.com' AND o.created_at > '2026-01-01' AND o.total > 1000"
    )
    out = sanitize(sql, "postgres")
    for secret in ("ana@example.com", "2026-01-01", "VIP", "1000", "0.15"):
        assert secret not in out
    assert out.count("$") == 5
    assert out.startswith("-- model: orders\n")


def test_existing_bind_parameters_are_kept() -> None:
    # A $1 that became "$$1" opened a dollar-quoted string, so the server evaluated different SQL.
    assert sanitize("SELECT a FROM t WHERE id = $1 AND name = $2", "postgres") == (
        "SELECT a FROM t WHERE id = $1 AND name = $2"
    )


def test_snowflake_uses_question_marks_and_keeps_stage_columns() -> None:
    assert sanitize("SELECT $1 FROM @stage WHERE a = 'x'", "snowflake") == "SELECT $1 FROM @stage WHERE a = ?"
