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

from typing import Any, Callable, Optional

from sqlalchemy import event

from .dbapi import _default_guard
from .dialects import normalize_dialect
from .guard import Vericto

EVENT = "before_cursor_execute"


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

    def remove() -> None:
        if event.contains(target, EVENT, before_cursor_execute):
            event.remove(target, EVENT, before_cursor_execute)

    return remove


__all__ = ["instrument"]
