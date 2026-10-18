"""vericto.sanitize edge cases beyond the vericto-cli parity table in test_dbt.py."""

from __future__ import annotations

import pytest

from vericto.sanitize import sanitize


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
        ("WHERE p = 'a\\' AND q = 'b'", "snowflake", "WHERE p = $1 AND q = $2"),
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


# vericto-cli 1.8.0 (sanitize.rs, 2026-10-15) changed two rules this port has not picked up yet.
# Both cases are copied from vericto-cli's own tests.


@pytest.mark.xfail(
    strict=True,
    reason="Port drift from vericto-cli 1.8.0: existing $N bind parameters become '$$N', which opens a "
    "dollar-quoted string, so the evaluated text no longer matches the statement.",
)
def test_existing_bind_parameters_are_kept() -> None:
    assert sanitize("SELECT a FROM t WHERE id = $1 AND name = $2", "postgres") == (
        "SELECT a FROM t WHERE id = $1 AND name = $2"
    )


@pytest.mark.xfail(
    strict=True,
    reason="Port drift from vericto-cli 1.8.0: Snowflake placeholders are '?' there and '$N' here, and the "
    "stage column reference $1 becomes '$$1'.",
)
def test_snowflake_uses_question_marks_and_keeps_stage_columns() -> None:
    assert sanitize("SELECT $1 FROM @stage WHERE a = 'x'", "snowflake") == "SELECT $1 FROM @stage WHERE a = ?"
