# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Vericto for LangChain SQL agents and chains.

::

    from vericto.langchain import VerictoSQLDatabase, VerictoCheckSQLTool

    db = VerictoSQLDatabase.from_uri("postgresql+psycopg://...")   # same arguments as SQLDatabase
    toolkit = SQLDatabaseToolkit(db=db, llm=llm)                    # every tool is now checked
    agent = create_sql_agent(llm, toolkit=toolkit, extra_tools=[VerictoCheckSQLTool(db=db)])

:class:`VerictoSQLDatabase` is a ``langchain_community.utilities.SQLDatabase``, so it
goes wherever one goes: ``QuerySQLDatabaseTool``, ``SQLDatabaseToolkit``,
``create_sql_agent``, ``create_sql_query_chain`` pipelines. Every query it runs is
checked with Vericto first, after SQLAlchemy compiles it:

* ``BLOCKED``: nothing runs. With ``on_block="return"`` (default) ``run`` and
  ``run_no_throw`` return a message with the rule, the reason and the suggested
  fix, so the model can correct its query; with ``on_block="raise"`` they raise
  :class:`~vericto.VerictoBlocked`.
* masked columns: the masked rewrite runs, never the original.
* ``FLAGGED`` / ``MONITORED``: runs, and is logged / passed to ``on_verdict``.
* Vericto unavailable, or a configuration error: raised, never returned to the
  model (``fail_mode="open"`` runs the query unevaluated instead).

The sample rows ``get_table_info`` puts in the prompt are checked too (and masked);
a blocked sample is left out of the table info instead of failing.

Requires the ``langchain`` extra: ``pip install "vericto[langchain]"``.
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, Literal, Optional, Sequence, Type, Union

from langchain_community.utilities.sql_database import SQLDatabase, sanitize_schema
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine, Result
from sqlalchemy.sql.expression import Executable
from sqlalchemy.sql.schema import Table

from . import sqlalchemy as _vericto_sqlalchemy
from ._agent import EngineGate, block_message, check_on_block, describe_verdict
from .dbapi import _default_guard
from .dialects import normalize_dialect
from .errors import VerictoBlocked, VerictoUnavailable
from .guard import Vericto
from .verdict import Verdict

OnBlock = Literal["return", "raise"]


def _internal_statements(schema: Optional[str]) -> frozenset[str]:
    # The fixed statements SQLDatabase._execute issues to select the schema. They are
    # not model-written SQL; checking them would double the cost of every query.
    if schema is None:
        return frozenset()
    statements = {
        "ALTER SESSION SET search_path = %s",
        "SET search_path TO %s",
        "SET @@dataset_id=?",
        f"USE {schema}",
        f"SET search_path TO {schema}",
        f"ALTER SESSION SET CURRENT_SCHEMA = {schema}",
    }
    # Not a name SAP HANA accepts: SQLDatabase never issues it then.
    with contextlib.suppress(ValueError):
        statements.add(f"SET SCHEMA {sanitize_schema(schema)}")
    return frozenset(statements)


class VerictoSQLDatabase(SQLDatabase):
    """A LangChain ``SQLDatabase`` whose queries are checked by Vericto.

    Takes every ``SQLDatabase`` argument (and ``from_uri`` works unchanged), plus:

    Args:
        guard: the :class:`~vericto.Vericto` to use; built from the ``VERICTO_*``
            environment variables when omitted.
        dialect: the Vericto dialect; read from ``engine.dialect.name`` when omitted.
        on_block: ``"return"`` (default): a blocked query returns an explanation the
            agent can act on. ``"raise"``: it raises :class:`~vericto.VerictoBlocked`.

    Schema reflection at construction is not sent to Vericto. If the engine is
    already instrumented with :func:`vericto.sqlalchemy.instrument`, that listener
    does the checking and nothing is evaluated twice.
    """

    def __init__(
        self,
        engine: Engine,
        *args: Any,
        guard: Optional[Vericto] = None,
        dialect: Optional[str] = None,
        on_block: OnBlock = "return",
        **kwargs: Any,
    ) -> None:
        self._vericto_on_block = check_on_block(on_block)
        g = _default_guard(guard)
        self._vericto_guard = g
        self._vericto_dialect = g.require_dialect(normalize_dialect(dialect or engine.dialect.name))
        super().__init__(engine, *args, **kwargs)
        self._vericto_gate = EngineGate(
            engine,
            g,
            self._vericto_dialect,
            internal=_internal_statements(self._schema),
            passthrough=_vericto_sqlalchemy.is_instrumented(engine),
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

    def _execute(
        self,
        command: Union[str, Executable],
        fetch: Literal["all", "one", "cursor"] = "all",
        *,
        parameters: Optional[Dict[str, Any]] = None,
        execution_options: Optional[Dict[str, Any]] = None,
    ) -> Union[Sequence[Dict[str, Any]], Result[Any]]:
        with self._vericto_gate.active():
            return super()._execute(command, fetch, parameters=parameters, execution_options=execution_options)

    def run(
        self,
        command: Union[str, Executable],
        fetch: Literal["all", "one", "cursor"] = "all",
        include_columns: bool = False,
        *,
        parameters: Optional[Dict[str, Any]] = None,
        execution_options: Optional[Dict[str, Any]] = None,
    ) -> Union[str, Sequence[Dict[str, Any]], Result[Any]]:
        """``SQLDatabase.run``, checked. A block returns a message or raises (``on_block``).

        ``fetch="cursor"`` always raises on a block: its caller expects a cursor.
        """
        try:
            return super().run(
                command, fetch, include_columns, parameters=parameters, execution_options=execution_options
            )
        except VerictoBlocked as exc:
            if self._vericto_on_block == "raise" or fetch == "cursor":
                raise
            return block_message(exc)

    def _get_sample_rows(self, table: Table) -> str:
        try:
            with self._vericto_gate.active():
                return str(super()._get_sample_rows(table))
        except VerictoBlocked as exc:
            columns = "\t".join(col.name for col in table.columns)
            return (
                f"{self._sample_rows_in_table_info} rows from {table.name} table:\n{columns}\n"
                f"(sample rows withheld: Vericto blocked the query, {exc.verdict.rule_code or 'no rule code'})"
            )

    def close_vericto(self) -> None:
        """Stop checking: detach the listener from the engine."""
        self._vericto_gate.remove()


class _CheckInput(BaseModel):
    query: str = Field(description="The SQL query to check, exactly as you intend to run it.")


class VerictoCheckSQLTool(BaseTool):
    """Ask Vericto about a SQL query without running it.

    Returns the verdict as text: allowed, flagged, masked (with the query that would
    run) or blocked (with the rule and the suggested fix). With ``on_block="raise"``
    a blocked query raises :class:`~vericto.VerictoBlocked` instead.

    Build it from a :class:`VerictoSQLDatabase` (``db=``) or from ``guard`` and
    ``dialect``. Each call is one evaluation in your Vericto workspace (Runtime API).
    """

    name: str = "vericto_check_sql"
    description: str = (
        "Check a SQL query with Vericto, the SQL firewall, before running it. "
        "Input: the exact SQL query. Output: whether it is allowed, and if it is blocked, "
        "the rule it breaks and a suggested fix. Use it to validate a query you are unsure about."
    )
    args_schema: Type[BaseModel] = _CheckInput

    model_config = ConfigDict(arbitrary_types_allowed=True)

    guard: Optional[Vericto] = None
    dialect: Optional[str] = None
    db: Optional[VerictoSQLDatabase] = Field(default=None, exclude=True)
    on_block: OnBlock = "return"

    def model_post_init(self, context: Any, /) -> None:
        check_on_block(self.on_block)
        if self.db is not None:
            if self.guard is None:
                self.guard = self.db.vericto_guard
            if self.dialect is None:
                self.dialect = self.db.vericto_dialect
        if self.dialect is None:
            raise ValueError("VerictoCheckSQLTool needs dialect= (or db=VerictoSQLDatabase(...)).")
        if self.guard is None:
            self.guard = Vericto()
        self.dialect = self.guard.require_dialect(self.dialect)

    def check(self, query: str) -> Verdict:
        """The verdict for ``query`` (nothing runs); ``UNEVALUATED`` under fail-open."""
        if self.guard is None or self.dialect is None:  # set in model_post_init
            raise RuntimeError("VerictoCheckSQLTool is not initialised")
        try:
            return self.guard.check(query, self.dialect)
        except VerictoUnavailable as exc:
            if self.guard.fail_mode == "open":
                return Verdict.unevaluated(query, self.dialect, str(exc))
            raise

    def _run(self, query: str, run_manager: Any = None) -> str:
        verdict = self.check(query)
        if verdict.blocked and self.on_block == "raise":
            raise VerictoBlocked(verdict, query)
        return describe_verdict(verdict)


__all__ = ["VerictoCheckSQLTool", "VerictoSQLDatabase"]
