# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Vericto for ``snowflake-connector-python``.

::

    import vericto.snowflake

    conn = vericto.snowflake.connect(account=..., user=..., authenticator=..., warehouse=...)
    cur = conn.cursor()
    cur.execute("SELECT id, email FROM customers WHERE id = %s", (42,))

Checked entry points: ``cursor.execute``, ``cursor.executemany``,
``cursor.execute_async``, ``connection.execute_string`` and
``connection.execute_stream``. Multi-statement scripts are split with the
connector's own splitter and **every statement is evaluated before the first one
runs**: a block anywhere means nothing executes.

Requires the ``snowflake`` extra: ``pip install "vericto[snowflake]"``.
"""

from __future__ import annotations

from io import StringIO
from typing import Any, Iterator, List, Optional

from .dbapi import GuardedConnection, GuardedCursor, _default_guard, _has_params
from .guard import Vericto
from .verdict import Verdict

DIALECT = "snowflake"


class SnowflakeGuardedCursor(GuardedCursor):
    """A Snowflake cursor checked by Vericto (``execute_async`` included)."""

    def execute_async(self, *args: Any, **kwargs: Any) -> Any:
        """``cursor.execute_async``, checked like ``execute`` before the query is submitted."""
        if args:
            command, rest = args[0], args[1:]
            sql = self._prepare(command, _has_params(rest, kwargs))
            return self._cursor.execute_async(sql, *rest, **kwargs)
        command = kwargs.pop("command")
        sql = self._prepare(command, _has_params((), kwargs))
        return self._cursor.execute_async(sql, **kwargs)


class SnowflakeGuardedConnection(GuardedConnection):
    """A ``SnowflakeConnection`` whose SQL entry points are checked by Vericto."""

    cursor_class = SnowflakeGuardedCursor

    def execute_string(self, sql_text: str, remove_comments: bool = False, *args: Any, **kwargs: Any) -> Any:
        """``connection.execute_string``: every statement is evaluated before the first runs.

        A block anywhere raises :class:`~vericto.VerictoBlocked` and nothing executes;
        masked statements run as their rewrite. Returns guarded cursors.
        """
        guarded = self._guard_script(sql_text, remove_comments)
        result = self._conn.execute_string(guarded, remove_comments, *args, **kwargs)
        if isinstance(result, list):
            return [self._guard_cursor(c) for c in result]
        return result

    def execute_stream(
        self, stream: StringIO, remove_comments: bool = False, *args: Any, **kwargs: Any
    ) -> Iterator[Any]:
        """``connection.execute_stream``: reads the whole stream and checks it like :meth:`execute_string`."""
        guarded = self._guard_script(stream.read(), remove_comments)
        for cursor in self._conn.execute_stream(StringIO(guarded), remove_comments, *args, **kwargs):
            yield self._guard_cursor(cursor)

    def _guard_script(self, sql_text: str, remove_comments: bool) -> str:
        """Evaluate every statement of a script; return the script to run."""
        statements = split_script(sql_text, remove_comments)
        verdicts = self._guard.enforce_all(statements, self._dialect)
        if not any(v.masked for v in verdicts):
            return sql_text  # unchanged: the connector runs exactly what the app wrote
        return "\n".join(_terminated(v.sql_to_execute) for v in verdicts)


def split_script(sql_text: str, remove_comments: bool = False) -> List[str]:
    """Split a script the way ``execute_string`` does (the connector's splitter)."""
    # snowflake-connector-python 5.x keeps util_text as a compatibility re-export.
    from snowflake.connector.util_text import split_statements  # type: ignore[attr-defined,unused-ignore]

    return [stmt for stmt, _ in split_statements(StringIO(sql_text), remove_comments=remove_comments) if stmt.strip()]


def _terminated(statement: str) -> str:
    stripped = statement.rstrip()
    return stripped if stripped.endswith(";") else stripped + ";"


def wrap(connection: Any, *, guard: Optional[Vericto] = None) -> SnowflakeGuardedConnection:
    """Guard an open ``snowflake.connector`` connection."""
    g = _default_guard(guard)
    return SnowflakeGuardedConnection(connection, g, g.require_dialect(DIALECT))


def connect(*, guard: Optional[Vericto] = None, **kwargs: Any) -> SnowflakeGuardedConnection:
    """``snowflake.connector.connect(**kwargs)``, guarded.

    The dialect is checked before connecting, so an unsupported server fails before
    any credentials are used.
    """
    import snowflake.connector

    g = _default_guard(guard)
    g.require_dialect(DIALECT)
    return wrap(snowflake.connector.connect(**kwargs), guard=g)


__all__ = [
    "DIALECT",
    "SnowflakeGuardedConnection",
    "SnowflakeGuardedCursor",
    "Verdict",
    "connect",
    "split_script",
    "wrap",
]
