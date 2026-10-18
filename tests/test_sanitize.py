# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""vericto.sanitize: vericto-cli's own test vectors, a property check, and the port's own edges."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from vericto.sanitize import sanitize

# vericto-cli's fixture (tests/fixtures/sanitize.json in vericto-cli 1.8.2), byte for byte: vericto-cli's
# own tests run the same file, so the port must give the same output for each vector. See "source".
VERICTO_CLI_VECTORS = Path(__file__).parent / "fixtures" / "sanitize" / "vericto-cli-1.8.2.json"

DIALECTS = ["postgres", "mysql", "oracle", "mssql", "snowflake", "sqlite", "bigquery"]


def _vectors() -> List[Dict[str, Any]]:
    data = json.loads(VERICTO_CLI_VECTORS.read_text(encoding="utf-8"))
    vectors: List[Dict[str, Any]] = data["vectors"]
    return vectors


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: f"{v['test']}-{v['dialect'].strip()}")
def test_matches_vericto_cli_test_vectors(vector: Dict[str, Any]) -> None:
    assert sanitize(vector["sql"], vector["dialect"]) == vector["expected"]


def test_vericto_cli_vectors_cover_every_dialect_and_leak() -> None:
    vectors = _vectors()
    assert len(vectors) >= 133
    assert set(DIALECTS) <= {v["dialect"] for v in vectors}
    groups = {v["test"] for v in vectors}
    for leak in (
        "snowflake_backslash_escape_does_not_leak",
        "mysql_reads_both_escape_modes",
        "oracle_q_quotes_are_literals",
        "mssql_bracket_identifiers_are_kept",
        "bind_parameters_are_kept_as_written",
        "numbering_starts_after_existing_parameters",
        "non_ascii_text_is_kept",
        "fails_closed",
    ):
        assert leak in groups


# -- property: no literal content survives ---------------------------------------------------------


def _string_piece(rng: random.Random, dialect: str, backslash: bool, quote: str) -> str:
    """Text that may sit inside a string quoted with ``quote`` in ``dialect`` without ending it."""
    pieces = [" ", "''", '""', "-- ", "/*", "*/", "$$", "ñ", "😀", "名", "]", "[", "#", "//", ":1", "$1", "?", "\n"]
    pieces += ['"', "q''["] if quote == "'" else ["'", "q'["]
    pieces += ["\\'", '\\"', "\\\\", "\\n"] if backslash else ["\\"]
    if dialect == "mysql":
        pieces.remove("*/")  # a literal inside /*! ... */ never holds */
    return "".join(rng.choice(pieces) for _ in range(rng.randrange(4)))


def _literal(rng: random.Random, dialect: str, marker: str, digits: str, hexa: str) -> str:
    """A literal holding ``marker`` (or ``digits`` / ``hexa``), of a kind ``dialect`` has."""
    plain_bs = dialect in ("mysql", "snowflake", "bigquery")

    def quoted(bs: bool) -> str:
        return f"'{_string_piece(rng, dialect, bs, chr(39))}{marker}{_string_piece(rng, dialect, bs, chr(39))}'"

    safe = ["", " ", "'", "ñ", "😀", "x", '"', "--", "/*"]

    def s() -> str:
        return rng.choice(safe) + rng.choice(safe)

    kinds: List[Callable[[], str]] = [
        lambda: quoted(plain_bs),
        lambda: digits,
        lambda: f"-{digits}",
        lambda: f"{digits}.{digits}",
        lambda: f".{digits}",
        lambda: f"{digits}e1{rng.randrange(9)}",
        lambda: f"0x{hexa}",
    ]
    if dialect in ("postgres", "bigquery"):
        kinds += [
            lambda: "E" + quoted(True),
            lambda: "N" + quoted(plain_bs),
            lambda: "U&" + quoted(False),
            lambda: f"X'{hexa}'",
            lambda: f"$${s()}{marker}{s()}$$",
            lambda: f"$tg${s()}{marker}$ {s()}$tg$",
            lambda: f"AS $$ {marker} $$",
            lambda: f"DO $x$ BEGIN PERFORM '{marker}'; END $x$",
        ]
    elif dialect == "mysql":

        def dq() -> str:
            return _string_piece(rng, dialect, True, '"')

        kinds += [
            lambda: f'"{dq()}{marker}\\"{dq()}"',
            lambda: "N" + quoted(True),
            lambda: "_utf8mb4" + quoted(True),
            lambda: f"X'{hexa}'",
            lambda: f"/*!50000 {quoted(True)} */",
            lambda: f"/*M!100100 {digits} */",
        ]
    elif dialect == "oracle":
        kinds += [
            lambda: f"q'[{s()}{marker}{s()}]'",
            lambda: f"q'{{{s()}{marker}{s()}}}'",
            lambda: f"q'({s()}{marker}{s()})'",
            lambda: f"Q'<{s()}{marker}{s()}>'",
            lambda: f"q'!{s()}{marker}{s()}!'",
            lambda: f"nq'§{s()}{marker}{s()}§'",
            lambda: "N" + quoted(False),
        ]
    elif dialect == "mssql":
        kinds += [lambda: "N" + quoted(False), lambda: f"${digits}.50"]
    elif dialect == "snowflake":
        kinds += [
            lambda: f"$${s()}{marker}{s()}$$",
            lambda: f'AS $$ return "{marker}"; $$',
            lambda: f"X'{hexa}'",
        ]
    else:
        kinds.append(lambda: f"X'{hexa}'")
    return rng.choice(kinds)()


_FILLER = [
    "SELECT", "FROM", "WHERE", "AND", "AS", "DO", "a", "col2", "t.c", "ñandú", "名前", "😀", "=", "<>", "(", ")",
    ",", "+", "-", "*", "/", "||", "::", ";", "?", "@p1", "-- it's ñ 😀\n", "/* it's 名 */", "\"q 'id\"",
]  # fmt: skip
_DIALECT_FILLER = {
    "postgres": ["$1", "$7", "/* a /* it's */ b' */", "a[1:2]", 'U&"x\'"'],
    "bigquery": ["$1", "$7", "/* a /* it's */ b' */", "a[1:2]", 'U&"x\'"'],
    "mysql": ["`it's`", "# it's\n", "/*+ BKA(t) */", "@v"],
    "oracle": [":1", ":name", "$2", "a#b"],
    "mssql": ["[it's 1]", "[a]]'b]", "/* a /* it's */ b' */", "#tmp"],
    "snowflake": ["$1", ":1", "// it's\n", "@stage"],
    "sqlite": ["[it's]", "`it's`", ":a", "$c", "?3"],
}


def test_no_marker_survives_in_any_literal_kind() -> None:
    """Random statements with a secret marker inside literals of every kind: none comes out."""
    rng = random.Random(0x5EED00182026)
    for round_ in range(6000):
        dialect = DIALECTS[round_ % len(DIALECTS)]
        filler = _FILLER + _DIALECT_FILLER.get(dialect, [])
        parts: List[str] = []
        markers: List[str] = []
        for _ in range(rng.randrange(1, 12)):
            if rng.randrange(3) == 0:
                marker = f"SECRET{rng.getrandbits(32):08x}"
                digits = str(7_000_000_000 + rng.randrange(1_000_000_000))
                hexa = f"ABCDE{rng.getrandbits(24):06X}"
                parts.append(_literal(rng, dialect, marker, digits, hexa))
                markers += [marker, digits, hexa]
            else:
                parts.append(rng.choice(filler))
            parts.append("\n" if rng.randrange(4) == 0 else " ")
        sql = "".join(parts)
        out = sanitize(sql, dialect)
        for m in markers:
            assert m not in out, f"[{dialect}] {m} leaked\n in:  {sql!r}\n out: {out!r}"


def test_arbitrary_text_keeps_non_ascii_characters_whole() -> None:
    pool = [
        "'", '"', "`", "[", "]", "$", "$$", "$a$", "\\", "-", "--", "#", "/", "*", "/*", "*/", "/*!", "//", ":", "::",
        "?", ".", "e", "E", "N", "q", "x", "U&", "&", "0", "7", " ", "\n", "ñ", "😀", "名", "§", "é", "AS", "DO", "_x",
        "{", "(", "<", "!", "+", ";",
    ]  # fmt: skip
    rng = random.Random(0xDEADBEEF0018)
    for round_ in range(6000):
        dialect = DIALECTS[round_ % len(DIALECTS)]
        sql = "".join(rng.choice(pool) for _ in range(rng.randrange(40)))
        out = sanitize(sql, dialect)
        assert all(ch in sql for ch in out if not ch.isascii()), (dialect, sql, out)


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "$a" * 50_000),
        ("postgres", "/*" * 50_000),
        ("oracle", "q'[" * 30_000),
        ("mysql", "'\\'" * 30_000),
        ("snowflake", "$$ " * 30_000),
        ("mssql", "[[" * 50_000),
        ("bigquery", "'\\' '" * 20_000),
    ],
    ids=lambda v: v if len(v) < 20 else "",
)
def test_pathological_inputs_stay_linear(dialect: str, sql: str) -> None:
    # Quadratic for a scanner that re-searches from every opening it sees.
    assert len(sanitize(sql, dialect)) < 4 * len(sql)


# -- the port's own edges ----------------------------------------------------------------------------


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


def test_existing_bind_parameters_are_kept_and_not_reused() -> None:
    # A $1 that became "$$1" opened a dollar-quoted string (0.3.0); a new $1 next to an existing one
    # gave two different values the same parameter (0.3.1).
    assert sanitize("SELECT a FROM t WHERE id = $1 AND name = 'x'", "postgres") == (
        "SELECT a FROM t WHERE id = $1 AND name = $2"
    )


def test_numbering_counts_the_parameters_of_either_reading() -> None:
    # Without backslash escapes `$5` is a parameter; with them it is inside the literal. The reading with
    # escapes wins (the other leaves a literal unterminated), and a new placeholder still never reuses $5.
    assert sanitize("SELECT '\\' $5 ', 1", "bigquery") == "SELECT $6, $7"


def test_huge_bind_parameter_is_not_a_numbering_base() -> None:
    assert sanitize("WHERE a = $4294967295 AND b = 1", "postgres") == "WHERE a = $4294967295 AND b = $4294967296"
    assert sanitize("WHERE a = $4294967296 AND b = 1", "postgres") == "WHERE a = $4294967296 AND b = $1"
