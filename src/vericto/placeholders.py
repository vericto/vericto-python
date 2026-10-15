"""Driver placeholders that are not SQL.

``format`` / ``pyformat`` drivers (psycopg, PyMySQL, snowflake-connector-python by
default) write parameters as ``%s`` or ``%(name)s``. That is not SQL: Vericto would
answer ``PARSE_ERROR`` and a fail-closed workspace would block every parameterised
query. When such a statement is executed **with** parameters, the text sent to
Vericto has those markers replaced by the dialect's own placeholders (``$1`` for
PostgreSQL, ``?`` elsewhere) and ``%%`` turned back into ``%``. No value is ever
substituted. A masked rewrite is mapped back the other way before it runs.

Statements executed without parameters, and ``qmark`` / ``numeric`` / ``named``
drivers, are sent exactly as written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .errors import VerictoError

PERCENT_STYLES = frozenset({"format", "pyformat"})

_NAMED = re.compile(r"%\(([^)]+)\)s")


class PlaceholderMappingError(VerictoError):
    """A masked rewrite could not be mapped back to the driver's placeholders.

    The statement is not executed (masking is a protection, so this fails closed).
    """


@dataclass(frozen=True)
class PlaceholderMap:
    dialect: str
    #: The original markers, in order of appearance (``%s`` or ``%(name)s``).
    markers: Tuple[str, ...]
    #: For PostgreSQL: the ``$n`` given to each marker (a repeated name keeps its n).
    numbers: Tuple[int, ...]


def to_evaluation_text(sql: str, dialect: str, paramstyle: Optional[str]) -> Tuple[str, Optional[PlaceholderMap]]:
    """Return the text to evaluate and how to map a rewrite back (``None``: unchanged)."""
    if paramstyle not in PERCENT_STYLES or "%" not in sql:
        return sql, None
    out: List[str] = []
    markers: List[str] = []
    numbers: List[int] = []
    names: dict[str, int] = {}
    next_number = 1
    i = 0
    n = len(sql)
    while i < n:
        span = _quoted_span(sql, i, dialect)
        if span is not None:
            out.append(sql[i:span].replace("%%", "%"))
            i = span
            continue
        ch = sql[i]
        if ch == "%" and sql.startswith("%%", i):
            out.append("%")
            i += 2
            continue
        if ch == "%" and sql.startswith("%s", i):
            markers.append("%s")
            numbers.append(next_number)
            out.append(_native(dialect, next_number))
            next_number += 1
            i += 2
            continue
        if ch == "%":
            m = _NAMED.match(sql, i)
            if m:
                name = m.group(1)
                if name not in names:
                    names[name] = next_number
                    next_number += 1
                markers.append(m.group(0))
                numbers.append(names[name])
                out.append(_native(dialect, names[name]))
                i = m.end()
                continue
        out.append(ch)
        i += 1
    if not markers:
        # Only %% escapes: the evaluation text still needs them unescaped.
        return "".join(out), PlaceholderMap(dialect, (), ())
    return "".join(out), PlaceholderMap(dialect, tuple(markers), tuple(numbers))


def from_evaluation_text(rewritten: str, mapping: PlaceholderMap) -> str:
    """Map a rewrite of the evaluation text back to the driver's placeholders."""
    out: List[str] = []
    used = 0
    i = 0
    n = len(rewritten)
    by_number = {num: marker for marker, num in zip(mapping.markers, mapping.numbers)}
    while i < n:
        span = _quoted_span(rewritten, i, mapping.dialect)
        if span is not None:
            out.append(rewritten[i:span].replace("%", "%%"))
            i = span
            continue
        ch = rewritten[i]
        if ch == "%":
            out.append("%%")
            i += 1
            continue
        if mapping.dialect == "postgres" and ch == "$":
            m = re.match(r"\$(\d+)", rewritten[i:])
            if m:
                num = int(m.group(1))
                if num not in by_number:
                    raise PlaceholderMappingError(f"The masked rewrite uses ${num}, which the statement did not have.")
                out.append(by_number[num])
                used += 1
                i += len(m.group(0))
                continue
        if mapping.dialect != "postgres" and ch == "?":
            if used >= len(mapping.markers):
                raise PlaceholderMappingError("The masked rewrite has more placeholders than the statement.")
            out.append(mapping.markers[used])
            used += 1
            i += 1
            continue
        out.append(ch)
        i += 1
    if mapping.dialect != "postgres" and used != len(mapping.markers):
        raise PlaceholderMappingError(
            f"The masked rewrite has {used} placeholder(s), the statement had {len(mapping.markers)}."
        )
    return "".join(out)


def _native(dialect: str, number: int) -> str:
    return f"${number}" if dialect == "postgres" else "?"


def _quoted_span(sql: str, i: int, dialect: str) -> Optional[int]:
    """If a string literal, quoted identifier or comment starts at ``i``, its end."""
    ch = sql[i]
    if ch in ("'", '"', "`"):
        j = i + 1
        while j < len(sql):
            if sql[j] == ch:
                if j + 1 < len(sql) and sql[j + 1] == ch:  # doubled quote
                    j += 2
                    continue
                return j + 1
            if sql[j] == "\\" and ch == "'" and dialect in ("mysql", "snowflake"):
                j += 2
                continue
            j += 1
        return len(sql)
    if sql.startswith("--", i):
        j = sql.find("\n", i)
        return len(sql) if j < 0 else j
    if sql.startswith("/*", i):
        j = sql.find("*/", i + 2)
        return len(sql) if j < 0 else j + 2
    if ch == "$" and dialect in ("postgres", "snowflake"):
        m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
        if m:
            tag = m.group(0)
            j = sql.find(tag, i + len(tag))
            return len(sql) if j < 0 else j + len(tag)
    return None
