# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Vericto for LlamaIndex text-to-SQL.

::

    from llama_index.core.query_engine import NLSQLTableQueryEngine
    from vericto.llamaindex import VerictoSQLDatabase

    sql_database = VerictoSQLDatabase(engine, include_tables=["orders"])
    query_engine = NLSQLTableQueryEngine(sql_database=sql_database, tables=["orders"], llm=llm)

:class:`VerictoSQLDatabase` is a ``llama_index.core.SQLDatabase``, so it works with
``NLSQLTableQueryEngine``, ``SQLTableRetrieverQueryEngine``, ``NLSQLRetriever`` and
``SQLDatabase.run_sql`` callers. Every statement ``run_sql`` (and
``insert_into_table``) sends is checked with Vericto first:

* ``BLOCKED``: nothing runs. With ``on_block="return"`` (default) ``run_sql``
  returns a message with the rule and the suggested fix as the SQL response (the
  metadata carries ``vericto_blocked``), so the response synthesis tells the user
  why; with ``on_block="raise"`` it raises :class:`~vericto.VerictoBlocked`.
  (``NLSQLTableQueryEngine`` catches retrieval errors itself, so there the raise
  shows up as LlamaIndex's ``"Error: Query blocked by ..."`` text; an
  ``NLSQLRetriever(handle_sql_errors=False)`` lets it propagate.)
* masked columns: the masked rewrite runs, never the original.
* ``FLAGGED`` / ``MONITORED``: runs, and is logged / passed to ``on_verdict``.
* Vericto unavailable: raised under ``fail_mode="closed"`` (the default);
  ``fail_mode="open"`` runs the statement unevaluated. A configuration error is
  raised in both modes.

``context`` and ``end_user`` (a value, or a callable read per statement) go with
every evaluation, as in :class:`vericto.langchain.VerictoSQLDatabase`.

Requires the ``llamaindex`` extra: ``pip install "vericto[llamaindex]"``.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Tuple

from llama_index.core import SQLDatabase
from sqlalchemy.engine import Engine

from . import sqlalchemy as _vericto_sqlalchemy
from ._agent import EngineGate, block_message, check_on_block, check_sources
from .dbapi import _default_guard
from .dialects import normalize_dialect
from .errors import VerictoBlocked
from .guard import Vericto
from .policy_context import ContextSource, EndUserSource

OnBlock = Literal["return", "raise"]


class VerictoSQLDatabase(SQLDatabase):
    """A LlamaIndex ``SQLDatabase`` whose queries are checked by Vericto.

    Takes every ``SQLDatabase`` argument (and ``from_uri`` works unchanged), plus
    ``guard``, ``dialect``, ``on_block``, ``context`` and ``end_user`` as in
    :class:`vericto.langchain.VerictoSQLDatabase`. Schema reflection is not sent to
    Vericto; an engine already instrumented with :func:`vericto.sqlalchemy.instrument`
    is not checked twice.
    """

    def __init__(
        self,
        engine: Engine,
        *args: Any,
        guard: Optional[Vericto] = None,
        dialect: Optional[str] = None,
        on_block: OnBlock = "return",
        context: ContextSource = None,
        end_user: EndUserSource = None,
        **kwargs: Any,
    ) -> None:
        self._vericto_on_block = check_on_block(on_block)
        check_sources(context, end_user)
        g = _default_guard(guard)
        self._vericto_guard = g
        self._vericto_dialect = g.require_dialect(normalize_dialect(dialect or engine.dialect.name))
        super().__init__(engine, *args, **kwargs)
        self._vericto_gate = EngineGate(
            engine,
            g,
            self._vericto_dialect,
            passthrough=_vericto_sqlalchemy.is_instrumented(engine),
            context=context,
            end_user=end_user,
        )

    @property
    def vericto_guard(self) -> Vericto:
        """The :class:`~vericto.Vericto` that checks this database's queries."""
        return self._vericto_guard

    @property
    def vericto_dialect(self) -> str:
        """The Vericto dialect the queries are evaluated as."""
        return self._vericto_dialect

    @property
    def on_block(self) -> str:
        """``"return"`` (a blocked query returns an explanation) or ``"raise"``."""
        return self._vericto_on_block

    def run_sql(self, command: str) -> Tuple[str, Dict[Any, Any]]:
        """``SQLDatabase.run_sql``, checked. A block returns a message or raises (``on_block``)."""
        try:
            with self._vericto_gate.active():
                return super().run_sql(command)
        except VerictoBlocked as exc:
            if self._vericto_on_block == "raise":
                raise
            verdict = exc.verdict
            return block_message(exc), {
                "result": [],
                "col_keys": [],
                "vericto_blocked": {
                    "rule_code": verdict.rule_code,
                    "severity": verdict.severity,
                    "suggested_fix": verdict.suggested_fix,
                    "event_id": verdict.event_id,
                },
            }

    def insert_into_table(self, table_name: str, data: dict[Any, Any]) -> None:
        """Checked like :meth:`run_sql`; a block always raises (there is no result to return)."""
        with self._vericto_gate.active():
            super().insert_into_table(table_name, data)

    def close_vericto(self) -> None:
        """Stop checking: detach the listener from the engine."""
        self._vericto_gate.remove()


__all__ = ["VerictoSQLDatabase"]
