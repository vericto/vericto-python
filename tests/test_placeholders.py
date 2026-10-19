# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from vericto.placeholders import PlaceholderMappingError, from_evaluation_text, to_evaluation_text


def test_untouched_without_percent_style() -> None:
    sql = "SELECT * FROM t WHERE a = %s"
    assert to_evaluation_text(sql, "postgres", None) == (sql, None)
    assert to_evaluation_text(sql, "postgres", "qmark") == (sql, None)


def test_postgres_numbers_positional_markers() -> None:
    text, mapping = to_evaluation_text("SELECT * FROM t WHERE a = %s AND b = %s", "postgres", "format")
    assert text == "SELECT * FROM t WHERE a = $1 AND b = $2"
    assert mapping is not None and mapping.markers == ("%s", "%s")


def test_named_markers_keep_their_number() -> None:
    sql = "SELECT * FROM t WHERE a = %(x)s OR b = %(y)s OR c = %(x)s"
    text, mapping = to_evaluation_text(sql, "postgres", "pyformat")
    assert text == "SELECT * FROM t WHERE a = $1 OR b = $2 OR c = $1"
    assert mapping is not None
    assert from_evaluation_text(text, mapping) == sql


def test_other_dialects_use_question_marks() -> None:
    text, _ = to_evaluation_text("SELECT * FROM t WHERE a = %s", "snowflake", "pyformat")
    assert text == "SELECT * FROM t WHERE a = ?"


def test_escapes_and_literals() -> None:
    sql = "SELECT '%s literal', a %% 2 FROM t /* %s */ WHERE b LIKE 'x%%' AND c = %s -- %s"
    text, mapping = to_evaluation_text(sql, "mysql", "format")
    assert text == "SELECT '%s literal', a % 2 FROM t /* %s */ WHERE b LIKE 'x%' AND c = ? -- %s"
    assert mapping is not None and mapping.markers == ("%s",)


def test_dollar_quoted_bodies_are_left_alone() -> None:
    sql = "SELECT $$ %s $$, %s"
    text, _ = to_evaluation_text(sql, "snowflake", "format")
    assert text == "SELECT $$ %s $$, ?"


def test_rewrite_is_mapped_back() -> None:
    sql = "SELECT email FROM users WHERE id = %s AND name LIKE 'a%%'"
    text, mapping = to_evaluation_text(sql, "snowflake", "pyformat")
    assert mapping is not None
    rewritten = text.replace("SELECT email", "SELECT '***' AS email")
    assert (
        from_evaluation_text(rewritten, mapping) == "SELECT '***' AS email FROM users WHERE id = %s AND name LIKE 'a%%'"
    )


def test_mismatched_rewrite_fails_closed() -> None:
    _, mapping = to_evaluation_text("SELECT a FROM t WHERE id = %s", "mysql", "format")
    assert mapping is not None
    with pytest.raises(PlaceholderMappingError):
        from_evaluation_text("SELECT a FROM t", mapping)
    with pytest.raises(PlaceholderMappingError):
        from_evaluation_text("SELECT a FROM t WHERE id = ? AND x = ?", mapping)
    _, pg = to_evaluation_text("SELECT a FROM t WHERE id = %s", "postgres", "format")
    assert pg is not None
    with pytest.raises(PlaceholderMappingError):
        from_evaluation_text("SELECT a FROM t WHERE id = $2", pg)


def test_only_percent_escapes_still_unescape() -> None:
    text, mapping = to_evaluation_text("SELECT a %% 2 FROM t WHERE b LIKE 'x%%'", "postgres", "format")
    assert text == "SELECT a % 2 FROM t WHERE b LIKE 'x%'"
    assert mapping is not None and mapping.markers == ()
    # A rewrite is escaped again for the driver, inside and outside literals.
    assert from_evaluation_text("SELECT a % 2 FROM t WHERE b LIKE 'x%'", mapping) == (
        "SELECT a %% 2 FROM t WHERE b LIKE 'x%%'"
    )


def test_a_lone_percent_is_not_a_marker() -> None:
    text, mapping = to_evaluation_text("SELECT a % b, %(x FROM t WHERE c = %s", "mysql", "format")
    assert text == "SELECT a % b, %(x FROM t WHERE c = ?"
    assert mapping is not None and mapping.markers == ("%s",)


def test_named_markers_map_back_in_order_for_question_mark_dialects() -> None:
    sql = "SELECT email FROM users WHERE id = %(id)s OR parent = %(id)s AND org = %(org)s"
    text, mapping = to_evaluation_text(sql, "mysql", "pyformat")
    assert text == "SELECT email FROM users WHERE id = ? OR parent = ? AND org = ?"
    assert mapping is not None
    rewrite = text.replace("SELECT email", "SELECT '***' AS email")
    assert from_evaluation_text(rewrite, mapping) == sql.replace("SELECT email", "SELECT '***' AS email")


def test_backslash_escapes_in_mysql_and_snowflake_strings() -> None:
    sql = r"SELECT 'it\'s %s' FROM t WHERE a = %s"
    for dialect in ("mysql", "snowflake"):
        text, mapping = to_evaluation_text(sql, dialect, "format")
        assert text == r"SELECT 'it\'s %s' FROM t WHERE a = ?"
        assert mapping is not None and mapping.markers == ("%s",)


def test_unterminated_literals_and_comments_run_to_the_end() -> None:
    assert to_evaluation_text("SELECT %s, 'open %s", "mysql", "format")[0] == "SELECT ?, 'open %s"
    assert to_evaluation_text("SELECT %s -- trailing %s", "mysql", "format")[0] == "SELECT ? -- trailing %s"
    assert to_evaluation_text("SELECT %s /* open %s", "mysql", "format")[0] == "SELECT ? /* open %s"
    assert to_evaluation_text("SELECT %s, $tag$ open %s", "postgres", "format")[0] == "SELECT $1, $tag$ open %s"
    assert to_evaluation_text('SELECT "col%s", `x%s` FROM t WHERE a = %s', "mysql", "format")[0] == (
        'SELECT "col%s", `x%s` FROM t WHERE a = ?'
    )


def test_postgres_rewrite_with_other_dollar_signs() -> None:
    _, mapping = to_evaluation_text("SELECT a FROM t WHERE id = %s", "postgres", "format")
    assert mapping is not None
    rewritten = "SELECT a, $tag$ $1 $tag$, 'cost $5' FROM t WHERE id = $1 AND b = $"
    assert from_evaluation_text(rewritten, mapping) == (
        "SELECT a, $tag$ $1 $tag$, 'cost $5' FROM t WHERE id = %s AND b = $"
    )


def test_doubled_quotes_stay_inside_the_literal() -> None:
    text, mapping = to_evaluation_text("SELECT 'O''Brien %s' FROM t WHERE a = %s", "postgres", "format")
    assert text == "SELECT 'O''Brien %s' FROM t WHERE a = $1"
    assert mapping is not None and mapping.markers == ("%s",)
