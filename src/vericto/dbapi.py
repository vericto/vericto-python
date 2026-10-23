# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Generic DB-API 2.0 (PEP 249) wrappers.

:func:`wrap` puts a guard in front of any DB-API connection: every
``cursor.execute()`` / ``executemany()`` is checked with Vericto first. The
driver-specific modules (:mod:`vericto.snowflake`, :mod:`vericto.sqlite`) build on
this or replace it where the driver has extra entry points.

Parameters are never sent to Vericto: the SQL text goes as the application wrote
it, placeholders included, and the values are bound by the driver afterwards.
"""

from __future__ import annotations

import sys
from typing import Any, Iterator, Optional

from .dialects import normalize_dialect
from .guard import Vericto


def _default_guard(guard: Optional[Vericto]) -> Vericto:
    return guard if guard is not None else Vericto()


class GuardedCursor:
    """A DB-API cursor whose SQL entry points are checked by Vericto.

    Everything else (``fetchone``, ``description``, ``rowcount``...) is the wrapped
    cursor's, available unchanged. :attr:`wrapped` is the original cursor: SQL run
    through it directly is not checked.
    """

    _cursor: Any
    _guard: Vericto
    _dialect: str
    _connection: Any
    _paramstyle: Optional[str]

    def __init__(
        self,
        cursor: Any,
        guard: Vericto,
        dialect: str,
        connection: Any = None,
        paramstyle: Optional[str] = None,
    ) -> None:
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_guard", guard)
        object.__setattr__(self, "_dialect", normalize_dialect(dialect))
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_paramstyle", paramstyle)

    @property
    def wrapped(self) -> Any:
        """The driver's own cursor. SQL run through it is **not** checked."""
        return self._cursor

    @property
    def connection(self) -> Any:
        """The guarded connection this cursor came from (else the driver cursor's)."""
        return self._connection if self._connection is not None else getattr(self._cursor, "connection", None)

    def _wrap_result(self, result: Any) -> Any:
        # Most drivers return the cursor itself (snowflake, sqlite3): keep the guard on.
        return self if result is self._cursor else result

    def _prepare(self, operation: str, has_params: bool) -> str:
        return self._guard.prepare(operation, self._dialect, paramstyle=self._paramstyle if has_params else None)

    def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """Check ``operation`` with Vericto, then run it (or its masked rewrite) on the driver.

        Raises :class:`~vericto.VerictoBlocked` before the driver is called when it is
        blocked, and :class:`~vericto.VerictoUnavailable` when there is no verdict and
        the guard fails closed. Parameters go to the driver, never to Vericto.
        """
        sql = self._prepare(operation, _has_params(args, kwargs))
        return self._wrap_result(self._cursor.execute(sql, *args, **kwargs))

    def executemany(self, operation: str, seq_of_parameters: Any, *args: Any, **kwargs: Any) -> Any:
        """:meth:`execute` for many parameter sets: one check for the statement."""
        sql = self._prepare(operation, True)
        return self._wrap_result(self._cursor.executemany(sql, seq_of_parameters, *args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # arraysize and friends are the driver's.
        setattr(self._cursor, name, value)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cursor)

    def __next__(self) -> Any:
        return next(self._cursor)

    def __enter__(self) -> GuardedCursor:
        enter = getattr(self._cursor, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        exit_ = getattr(self._cursor, "__exit__", None)
        if exit_ is not None:
            return exit_(*exc)
        self._cursor.close()
        return None

    def __repr__(self) -> str:
        return f"<vericto guarded {self._cursor!r}>"


class GuardedConnection:
    """A DB-API connection whose cursors are :class:`GuardedCursor` objects."""

    cursor_class: type[GuardedCursor] = GuardedCursor
    _conn: Any
    _guard: Vericto
    _dialect: str
    _paramstyle: Optional[str]

    def __init__(self, connection: Any, guard: Vericto, dialect: str, paramstyle: Optional[str] = None) -> None:
        object.__setattr__(self, "_conn", connection)
        object.__setattr__(self, "_guard", guard)
        object.__setattr__(self, "_dialect", normalize_dialect(dialect))
        object.__setattr__(self, "_paramstyle", paramstyle if paramstyle is not None else infer_paramstyle(connection))

    @property
    def wrapped(self) -> Any:
        """The driver's own connection. SQL run through it is **not** checked."""
        return self._conn

    @property
    def guard(self) -> Vericto:
        """The :class:`~vericto.Vericto` that checks this connection's statements."""
        return self._guard

    @property
    def dialect(self) -> str:
        """The Vericto dialect the statements are evaluated as."""
        return self._dialect

    @property
    def paramstyle(self) -> Optional[str]:
        """The driver's DB-API paramstyle (``format``/``pyformat`` change what is evaluated)."""
        return self._paramstyle

    def _guard_cursor(self, cursor: Any) -> GuardedCursor:
        return self.cursor_class(cursor, self._guard, self._dialect, self, self._paramstyle)

    def cursor(self, *args: Any, **kwargs: Any) -> GuardedCursor:
        """A :class:`GuardedCursor` over the driver's ``cursor(*args, **kwargs)``."""
        return self._guard_cursor(self._conn.cursor(*args, **kwargs))

    def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """Shortcut some drivers offer (``sqlite3``); checked like ``cursor.execute``."""
        if not hasattr(self._conn, "execute"):
            raise AttributeError(f"{type(self._conn).__name__} has no execute(); use cursor().execute()")
        has_params = _has_params(args, kwargs)
        sql = self._guard.prepare(operation, self._dialect, paramstyle=self._paramstyle if has_params else None)
        result = self._conn.execute(sql, *args, **kwargs)
        return self._guard_cursor(result) if _is_cursor(result) else result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._conn, name, value)

    def __enter__(self) -> GuardedConnection:
        enter = getattr(self._conn, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        exit_ = getattr(self._conn, "__exit__", None)
        if exit_ is not None:
            return exit_(*exc)
        return None

    def __repr__(self) -> str:
        return f"<vericto guarded {self._conn!r} dialect={self._dialect}>"


def infer_paramstyle(connection: Any) -> Optional[str]:
    """The DB-API ``paramstyle`` of a connection's driver, when it can be told."""
    own = getattr(connection, "_paramstyle", None)  # snowflake-connector-python, per connection
    if isinstance(own, str):
        return own
    module = sys.modules.get(type(connection).__module__.split(".", 1)[0])
    style = getattr(module, "paramstyle", None)
    return style if isinstance(style, str) else None


def _has_params(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    if args:
        return args[0] is not None
    return any(kwargs.get(key) is not None for key in ("params", "parameters", "args", "vars"))


def _is_cursor(obj: Any) -> bool:
    return hasattr(obj, "execute") and hasattr(obj, "fetchone")


def wrap(
    connection: Any, *, dialect: str, guard: Optional[Vericto] = None, paramstyle: Optional[str] = None
) -> GuardedConnection:
    """Guard any DB-API connection.

    Args:
        connection: an open DB-API connection (psycopg, PyMySQL, oracledb, pyodbc...).
        dialect: the Vericto dialect of the database (``postgres``, ``mysql``,
            ``oracle``, ``mssql``, ``snowflake``; driver names such as ``postgresql``
            are accepted).
        guard: the :class:`~vericto.Vericto` to use; one is built from the
            ``VERICTO_*`` environment variables when omitted.
        paramstyle: the driver's DB-API paramstyle; read from the driver module when
            omitted. Only ``format``/``pyformat`` change what is evaluated.
    """
    g = _default_guard(guard)
    return GuardedConnection(connection, g, g.require_dialect(dialect), paramstyle)
