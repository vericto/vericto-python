# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Vericto for SQLAlchemy (1.4 and 2.x), any dialect.

::

    from sqlalchemy import create_engine
    import vericto.sqlalchemy

    engine = create_engine("snowflake://...")      # or postgresql://, mysql://, sqlite://
    vericto.sqlalchemy.instrument(engine)

A ``before_cursor_execute`` listener checks every statement SQLAlchemy sends to the
driver (ORM, Core and ``text()`` alike), after compilation, so Vericto sees the SQL
that actually runs. Blocked statements raise :class:`~vericto.VerictoBlocked`
before the driver is called; masked statements run as their rewrite.

The dialect comes from ``engine.dialect.name``: ``snowflake``, ``postgresql`` →
``postgres``, ``mysql``/``mariadb`` → ``mysql``, ``oracle``, ``mssql``, ``sqlite``.

Requires the ``sqlalchemy`` extra: ``pip install "vericto[sqlalchemy]"``.
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Optional

from sqlalchemy import event

from .dbapi import _default_guard
from .dialects import normalize_dialect
from .guard import Vericto

EVENT = "before_cursor_execute"

# Engines with an active instrument() listener (and how many), so integrations built on
# top (LangChain, LlamaIndex) can tell whether the engine is already checked.
_INSTRUMENTED: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()


def instrument(engine: Any, *, guard: Optional[Vericto] = None, dialect: Optional[str] = None) -> Callable[[], None]:
    """Check every statement ``engine`` executes. Returns a function that undoes it.

    Args:
        engine: an ``Engine``, or an ``AsyncEngine`` (its ``sync_engine`` is used).
        guard: the :class:`~vericto.Vericto` to use; built from the environment
            when omitted.
        dialect: override the dialect read from ``engine.dialect.name``.
    """
    target = getattr(engine, "sync_engine", engine)
    g = _default_guard(guard)
    name = g.require_dialect(normalize_dialect(dialect or target.dialect.name))
    paramstyle: Optional[str] = getattr(target.dialect, "paramstyle", None)

    def before_cursor_execute(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> tuple[str, Any]:
        has_params = executemany or bool(parameters)
        final = g.prepare(statement, name, paramstyle=paramstyle if has_params else None)
        return final, parameters

    event.listen(target, EVENT, before_cursor_execute, retval=True)
    _INSTRUMENTED[target] = _INSTRUMENTED.get(target, 0) + 1

    def remove() -> None:
        if event.contains(target, EVENT, before_cursor_execute):
            event.remove(target, EVENT, before_cursor_execute)
            left = _INSTRUMENTED.get(target, 1) - 1
            if left > 0:
                _INSTRUMENTED[target] = left
            else:
                _INSTRUMENTED.pop(target, None)

    return remove


def is_instrumented(engine: Any) -> bool:
    """True when :func:`instrument` is active on ``engine`` (or its ``sync_engine``)."""
    return _INSTRUMENTED.get(getattr(engine, "sync_engine", engine), 0) > 0


__all__ = ["instrument", "is_instrumented"]
