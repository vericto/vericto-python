"""Vericto for the standard library ``sqlite3`` module.

::

    import vericto.sqlite

    conn = vericto.sqlite.connect("app.db")
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))

The connection is a real :class:`sqlite3.Connection` subclass (so libraries that
check ``isinstance(conn, sqlite3.Connection)``, such as pandas, keep working).
Checked entry points: ``execute``, ``executemany`` and ``executescript`` on both the
connection and its cursors. ``executescript`` is split with
:func:`sqlite3.complete_statement` and every statement is evaluated before the
script runs.

.. note:: SQLite has no server, so there is no proxy: this wrapper is the
   enforcement point, and SQL run through another connection to the same file is
   not checked. The Vericto server evaluates the ``sqlite`` dialect (vericto-engine
   4.1.0+); :func:`connect` still asks it (``GET /api/v1/meta/config``) and raises
   :class:`~vericto.VerictoUnsupportedDialect`, with no SQL run, against a server
   that does not list ``sqlite`` (for example an older self-hosted one).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, List, Optional

from .dbapi import _default_guard
from .guard import Vericto

DIALECT = "sqlite"

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _guard_of(conn: Any) -> Vericto:
    guard = getattr(conn, "_vericto_guard", None)
    if not isinstance(guard, Vericto):
        raise RuntimeError("vericto: this connection was not opened with vericto.sqlite.connect().")
    return guard


class VerictoSQLiteCursor(sqlite3.Cursor):
    """A ``sqlite3.Cursor`` whose SQL is checked by Vericto."""

    def execute(self, sql: str, parameters: Any = (), /) -> VerictoSQLiteCursor:
        final = _guard_of(self.connection).prepare(sql, DIALECT)
        super().execute(final, parameters)
        return self

    def executemany(self, sql: str, seq_of_parameters: Iterable[Any], /) -> VerictoSQLiteCursor:
        final = _guard_of(self.connection).prepare(sql, DIALECT)
        super().executemany(final, seq_of_parameters)
        return self

    def executescript(self, sql_script: str, /) -> VerictoSQLiteCursor:
        final = guard_script(_guard_of(self.connection), sql_script)
        super().executescript(final)
        return self


class VerictoSQLiteConnection(sqlite3.Connection):
    """A ``sqlite3.Connection`` whose cursors are :class:`VerictoSQLiteCursor`."""

    _vericto_guard: Optional[Vericto] = None

    def cursor(self, factory: Any = None) -> VerictoSQLiteCursor:  # type: ignore[override]
        if factory is None:
            factory = VerictoSQLiteCursor
        elif not (isinstance(factory, type) and issubclass(factory, VerictoSQLiteCursor)):
            raise TypeError("vericto: a cursor factory must subclass vericto.sqlite.VerictoSQLiteCursor.")
        cursor: VerictoSQLiteCursor = super().cursor(factory)
        return cursor

    def execute(self, sql: str, parameters: Any = (), /) -> VerictoSQLiteCursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Iterable[Any], /) -> VerictoSQLiteCursor:
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str, /) -> VerictoSQLiteCursor:
        return self.cursor().executescript(sql_script)


def split_script(script: str) -> List[str]:
    """Split a script into statements with SQLite's own completeness test.

    Trigger bodies (``BEGIN ... END``), string literals and comments are respected
    because :func:`sqlite3.complete_statement` is the tokenizer SQLite itself uses.
    """
    statements: List[str] = []
    buffer = ""
    for piece in script.split(";")[:-1]:
        buffer += piece + ";"
        if sqlite3.complete_statement(buffer):
            if _has_sql(buffer):
                statements.append(buffer.strip())
            buffer = ""
    tail = buffer + script.split(";")[-1]
    if _has_sql(tail):
        statements.append(tail.strip())
    return statements


def _has_sql(text: str) -> bool:
    return bool(_COMMENTS.sub("", text).strip().strip(";").strip())


def guard_script(guard: Vericto, script: str) -> str:
    """Evaluate every statement of ``script``; return the script to run."""
    verdicts = guard.enforce_all(split_script(script), DIALECT)
    if not any(v.masked for v in verdicts):
        return script
    return "\n".join(v.sql_to_execute.rstrip().rstrip(";") + ";" for v in verdicts)


def connect(database: Any, *, guard: Optional[Vericto] = None, **kwargs: Any) -> VerictoSQLiteConnection:
    """``sqlite3.connect(database, **kwargs)``, guarded.

    Raises :class:`~vericto.VerictoUnsupportedDialect` before opening the file when
    the server does not list the ``sqlite`` dialect.
    """
    if "factory" in kwargs:
        raise TypeError("vericto.sqlite.connect() sets the connection factory itself.")
    g = _default_guard(guard)
    g.require_dialect(DIALECT)
    conn = sqlite3.connect(database, factory=VerictoSQLiteConnection, **kwargs)
    conn._vericto_guard = g
    return conn


__all__ = ["DIALECT", "VerictoSQLiteConnection", "VerictoSQLiteCursor", "connect", "guard_script", "split_script"]
