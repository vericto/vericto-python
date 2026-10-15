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
