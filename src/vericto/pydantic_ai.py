# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Vericto for Pydantic AI (``pydantic-ai`` / ``pydantic-ai-slim``).

::

    from pydantic_ai import Agent, FunctionToolset
    from vericto.pydantic_ai import VerictoToolset

    sql_tools = FunctionToolset()

    @sql_tools.tool_plain
    async def run_sql(query: str) -> str:
        \"\"\"Run one read-only SQL query and return the rows.\"\"\"
        return str(await pool.fetch(query))

    agent = Agent("openai:gpt-5", toolsets=[VerictoToolset(sql_tools, dialect="postgres")])

:class:`VerictoToolset` is a Pydantic AI ``WrapperToolset``: it wraps any toolset
(a ``FunctionToolset`` of tools you write, an ``MCPToolset`` of an MCP server's
tools, a combined or prefixed toolset) and overrides ``call_tool``, so Vericto
checks the SQL argument of every call to a SQL tool before the wrapped toolset
runs it:

* ``BLOCKED``: the tool is not called. By default (``on_block="retry"``) the call
  raises ``ModelRetry`` with the refusal (``Error: Vericto blocked this query
  (VERICTO-001, ...)``, the reason and the ``suggested_fix``), Pydantic AI's way of
  asking the model to correct a tool call; it counts against the tool's
  ``retries``. ``on_block="return"`` returns the refusal as the tool result
  instead (no retry budget spent; the model may correct the query or explain), and
  ``on_block="raise"`` stops the run with :class:`~vericto.VerictoBlocked`.
* an ``approved_query`` (masked columns and/or a ``LIMIT`` bound from a business
  policy): the wrapped tool is called with that query, never the original.
* ``FLAGGED`` / ``MONITORED``: the tool runs; the verdict is logged on the
  ``vericto`` logger and passed to the guard's ``on_verdict``.
* Vericto unavailable: under ``fail_mode="closed"`` (the default) the run stops
  with :class:`~vericto.VerictoUnavailable`; it is never handed to the model as
  text. ``fail_mode="open"`` runs the query unevaluated, with a
  :class:`~vericto.VerictoFailOpenWarning`. A configuration error stops the run in
  both modes.

The check goes through :meth:`Vericto.enforce <vericto.Vericto.enforce>`, so the
guard's fail mode, cache, placeholder mapping and ``on_verdict`` apply unchanged.
The guard is synchronous: the check runs in a worker thread
(``asyncio.to_thread``), so the event loop keeps going.

``context`` (``ticket``, ``tenant``) and ``end_user`` go with every evaluation: a
fixed value, or a callable read for each call (for example from a
``contextvars.ContextVar`` set per request). They take effect when business
policies are enabled in your workspace (:mod:`vericto.policy_context`).

Requires the ``pydantic-ai`` extra: ``pip install "vericto[pydantic-ai]"``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Collection, Literal, Optional, TypeVar

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from ._agent import ToolCheck, block_message
from .errors import VerictoBlocked, VerictoConfigError
from .guard import Vericto
from .policy_context import ContextSource, EndUserSource

OnBlock = Literal["retry", "return", "raise"]
ON_BLOCK = ("retry", "return", "raise")

DepsT = TypeVar("DepsT")


@dataclass
class VerictoToolset(WrapperToolset[DepsT]):
    """A ``WrapperToolset`` that checks the SQL argument of tool calls with Vericto.

    ``VerictoToolset(toolset, dialect=...)``: pass it to ``Agent(toolsets=[...])`` (or
    to ``agent.run(..., toolsets=[...])``) in place of ``toolset``. Every tool of
    ``toolset`` keeps its name, schema and description; tools out of ``tools`` run
    unchecked.

    Every tool in scope must declare the SQL parameter ``argument`` in its schema:
    when the toolset lists one that does not, the run stops with
    :class:`~vericto.VerictoConfigError` before any tool runs, so a SQL tool never
    goes unchecked for want of a parameter name. Use ``tools`` to name the SQL tools
    of a toolset that has others (an MCP server's ``list_tables``, ``ping``...).

    Args:
        wrapped: the toolset to check (``FunctionToolset``, ``MCPToolset``...).
        dialect: the Vericto dialect of the database the tools run SQL against
            (``"postgres"``, ``"snowflake"``, ``"sqlite"``...).
        guard: the :class:`~vericto.Vericto` to use; built from the ``VERICTO_*``
            environment variables when omitted.
        argument: the name of the tools' SQL parameter (default ``"query"``).
        tools: the tool names to check; ``None`` (default) checks every tool of the
            wrapped toolset. Calls to other tools run unchecked.
        on_block: ``"retry"`` (default): a blocked query raises ``ModelRetry`` with
            the refusal (rule code, ``suggested_fix``). ``"return"``: the refusal is
            the tool result. ``"raise"``: the run stops with
            :class:`~vericto.VerictoBlocked`.
        context: ``{"ticket": ..., "tenant": ...}`` sent with every evaluation, or a
            callable that returns it (read per call).
        end_user: the end user's attributes sent with every evaluation, or a callable
            that returns them (read per call).
        paramstyle: the driver's DB-API paramstyle, when the tools execute the query
            with bound parameters (``%s`` markers are then evaluated as the
            dialect's placeholders; see :meth:`Vericto.enforce`).
    """

    dialect: str = field(kw_only=True)
    guard: Optional[Vericto] = field(default=None, kw_only=True)
    argument: str = field(default="query", kw_only=True)
    tools: Optional[Collection[str]] = field(default=None, kw_only=True)
    on_block: OnBlock = field(default="retry", kw_only=True)
    context: ContextSource = field(default=None, kw_only=True, repr=False)
    end_user: EndUserSource = field(default=None, kw_only=True, repr=False)
    paramstyle: Optional[str] = field(default=None, kw_only=True)
    _check: ToolCheck = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.tools, str):
            raise VerictoConfigError("tools must be a collection of tool names, not a string.")
        self._check = ToolCheck(
            self.guard,
            self.dialect,
            self.argument,
            self.on_block,
            self.context,
            self.end_user,
            self.paramstyle,
            allowed_on_block=ON_BLOCK,
        )
        # Pydantic AI copies a wrapper per run (dataclasses.replace): keep one guard.
        self.guard = self._check.guard
        if self.tools is not None:
            self.tools = frozenset(self.tools)

    def _in_scope(self, name: str) -> bool:
        return self.tools is None or name in self.tools

    async def get_tools(self, ctx: RunContext[DepsT]) -> dict[str, ToolsetTool[DepsT]]:
        tools = await super().get_tools(ctx)
        for name, tool in tools.items():
            if not self._in_scope(name):
                continue
            properties = (tool.tool_def.parameters_json_schema or {}).get("properties") or {}
            if self.argument not in properties:
                raise VerictoConfigError(
                    f"Tool {name!r} has no parameter {self.argument!r}: pass argument= with the name of its SQL "
                    "parameter, or tools= with the names of the SQL tools only."
                )
        return tools

    def _refuse(self, message: str, cause: Optional[BaseException] = None) -> str:
        if self._check.on_block == "return":
            return message
        raise ModelRetry(message) from cause

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[DepsT], tool: ToolsetTool[DepsT]
    ) -> Any:
        if not self._in_scope(name):
            return await super().call_tool(name, tool_args, ctx, tool)
        sql = tool_args.get(self.argument)
        if not isinstance(sql, str):
            return self._refuse(
                f"Vericto could not find the SQL query in the {self.argument!r} argument of {name}. "
                "It was NOT executed."
            )
        try:
            verdict = await asyncio.to_thread(self._check.enforce, sql)
        except VerictoBlocked as exc:
            if self._check.on_block == "raise":
                raise
            return self._refuse(block_message(exc), exc)
        return await super().call_tool(name, {**tool_args, self.argument: verdict.sql_to_execute}, ctx, tool)


__all__ = ["VerictoToolset"]
