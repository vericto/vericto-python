# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Vericto for the OpenAI Agents SDK (``openai-agents``).

::

    from agents import Agent
    from vericto.openai_agents import vericto_sql_tool, vericto_tool_input_guardrail

    @vericto_sql_tool(dialect="postgres")          # a FunctionTool, like @function_tool
    async def run_sql(query: str) -> str:
        \"\"\"Run one read-only SQL query and return the rows.\"\"\"
        return str(await pool.fetch(query))

    agent = Agent(name="analyst", tools=[run_sql])

Two ways in, for the two kinds of SQL tools an agent has:

* :func:`vericto_sql_tool`, for a SQL tool you write. It is ``function_tool``
  plus a check of the SQL argument before your function runs, and the only one
  that can run Vericto's rewrite for you:

  * ``BLOCKED``: your function is not called. The model receives the refusal
    (``Error: Vericto blocked this query (VERICTO-001, ...)``, the reason and the
    ``suggested_fix``) as the tool output, and can correct its query. With
    ``on_block="raise"`` the run stops instead.
  * an ``approved_query`` (masked columns and/or a ``LIMIT`` bound from a business
    policy): your function is called with that query, never the original.
  * ``FLAGGED`` / ``MONITORED``: your function runs; the verdict is logged on the
    ``vericto`` logger and passed to the guard's ``on_verdict``.
  * Vericto unavailable: under ``fail_mode="closed"`` (the default) the run stops
    (``agents.exceptions.UserError`` whose ``__cause__`` is the
    :class:`~vericto.VerictoUnavailable`); it is never handed to the model as text.
    ``fail_mode="open"`` runs the query unevaluated, with a
    :class:`~vericto.VerictoFailOpenWarning`. A configuration error stops the run in
    both modes.

* :func:`vericto_tool_input_guardrail`, for a SQL tool you do not write (an MCP
  server's ``query`` tool, a third-party ``FunctionTool``): a native
  ``ToolInputGuardrail`` that reads the SQL argument of the call. The SDK's tool
  guardrails can allow or reject a call but not change its arguments, so:
  ``BLOCKED`` is rejected with the same refusal; an ``approved_query`` is rejected
  with that query and the instruction to call the tool again with it (the second
  call is checked too); everything else is allowed, with the
  :class:`~vericto.Verdict` as the guardrail's ``output_info``
  (``result.tool_input_guardrail_results``). Fail mode as above.

Both go through :meth:`Vericto.enforce <vericto.Vericto.enforce>`, so the guard's
fail mode, cache, placeholder mapping and ``on_verdict`` apply unchanged. The guard
is synchronous: in ``async`` tools and in the guardrail the check runs in a worker
thread (``asyncio.to_thread``), so the event loop keeps going; a sync tool already
runs in one.

``context`` (``ticket``, ``tenant``) and ``end_user`` go with every evaluation: a
fixed value, or a callable read for each call (for example from a
``contextvars.ContextVar`` set per request). They take effect when business
policies are enabled in your workspace (:mod:`vericto.policy_context`).

Requires the ``openai-agents`` extra: ``pip install "vericto[openai-agents]"``.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
from typing import Any, Callable, Collection, Literal, Optional, Union, overload

from agents import FunctionTool, RunContextWrapper, function_tool
from agents.tool import default_tool_error_function
from agents.tool_guardrails import ToolGuardrailFunctionOutput, ToolInputGuardrail, ToolInputGuardrailData

from ._agent import ToolCheck, block_message
from .errors import VerictoBlocked, VerictoConfigError, VerictoError
from .guard import Vericto
from .policy_context import ContextSource, EndUserSource
from .verdict import Verdict

OnBlock = Literal["return", "raise"]
ToolErrorFunction = Callable[[RunContextWrapper[Any], Exception], Any]

_UNSET: Any = object()


def approved_query_message(verdict: Verdict) -> str:
    """What the model reads when a guardrail cannot run Vericto's rewrite for it."""
    lines = [
        "Vericto approved this query only as rewritten (masked columns and/or a row limit). "
        "It was NOT executed as written.",
        f"Call the tool again with exactly this query: {verdict.rewritten_query}",
    ]
    if verdict.event_id:
        lines.append(f"(Vericto event {verdict.event_id})")
    return "\n".join(lines)


def _vericto_failure_function(fallback: Optional[ToolErrorFunction]) -> ToolErrorFunction:
    """A ``failure_error_function`` that never turns a Vericto error into model text.

    ``VerictoUnavailable`` (fail-closed), ``VerictoConfigError`` and, with
    ``on_block="raise"``, ``VerictoBlocked`` are re-raised, so the run stops; every
    other error goes to ``fallback`` (the SDK's default unless you pass your own).
    """

    async def failure(ctx: RunContextWrapper[Any], error: Exception) -> str:
        if isinstance(error, VerictoError):
            raise error
        message = (fallback or default_tool_error_function)(ctx, error)
        if inspect.isawaitable(message):
            message = await message
        return str(message)

    return failure


def _guarded(func: Callable[..., Any], check: ToolCheck) -> Callable[..., Any]:
    """``func``, which Vericto checks (and gives the approved query) before it runs."""
    signature = inspect.signature(func)
    if check.argument not in signature.parameters:
        name = getattr(func, "__name__", repr(func))
        raise VerictoConfigError(
            f"{name}() has no parameter {check.argument!r}: pass argument= with the name of its SQL parameter."
        )

    def bind(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[inspect.BoundArguments, str]:
        bound = signature.bind(*args, **kwargs)
        sql = bound.arguments.get(check.argument)
        if not isinstance(sql, str):
            raise TypeError(f"vericto: expected the SQL text as str in {check.argument!r}, got {type(sql).__name__}.")
        return bound, sql

    def blocked(exc: VerictoBlocked) -> str:
        if check.on_block == "raise":
            raise exc
        return block_message(exc)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_tool(*args: Any, **kwargs: Any) -> Any:
            bound, sql = bind(args, kwargs)
            try:
                verdict = await asyncio.to_thread(check.enforce, sql)
            except VerictoBlocked as exc:
                return blocked(exc)
            bound.arguments[check.argument] = verdict.sql_to_execute
            return await func(*bound.args, **bound.kwargs)

        return async_tool

    @functools.wraps(func)
    def sync_tool(*args: Any, **kwargs: Any) -> Any:
        # The SDK runs a sync tool in a worker thread: the blocking check is fine here.
        bound, sql = bind(args, kwargs)
        try:
            verdict = check.enforce(sql)
        except VerictoBlocked as exc:
            return blocked(exc)
        bound.arguments[check.argument] = verdict.sql_to_execute
        return func(*bound.args, **bound.kwargs)

    return sync_tool


@overload
def vericto_sql_tool(
    func: Callable[..., Any],
    *,
    dialect: str,
    guard: Optional[Vericto] = None,
    argument: str = "query",
    on_block: OnBlock = "return",
    context: ContextSource = None,
    end_user: EndUserSource = None,
    paramstyle: Optional[str] = None,
    failure_error_function: Optional[ToolErrorFunction] = _UNSET,
    **function_tool_kwargs: Any,
) -> FunctionTool: ...


@overload
def vericto_sql_tool(
    func: None = None,
    *,
    dialect: str,
    guard: Optional[Vericto] = None,
    argument: str = "query",
    on_block: OnBlock = "return",
    context: ContextSource = None,
    end_user: EndUserSource = None,
    paramstyle: Optional[str] = None,
    failure_error_function: Optional[ToolErrorFunction] = _UNSET,
    **function_tool_kwargs: Any,
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


def vericto_sql_tool(
    func: Optional[Callable[..., Any]] = None,
    *,
    dialect: str,
    guard: Optional[Vericto] = None,
    argument: str = "query",
    on_block: OnBlock = "return",
    context: ContextSource = None,
    end_user: EndUserSource = None,
    paramstyle: Optional[str] = None,
    failure_error_function: Optional[ToolErrorFunction] = _UNSET,
    **function_tool_kwargs: Any,
) -> Union[FunctionTool, Callable[[Callable[..., Any]], FunctionTool]]:
    """``agents.function_tool`` for a SQL tool: Vericto checks the SQL before ``func`` runs.

    Use it as ``@vericto_sql_tool(dialect=...)`` or ``vericto_sql_tool(func, dialect=...)``.
    ``func`` is a sync or ``async`` function with a ``str`` parameter named
    ``argument`` (default ``"query"``); it may take the SDK's ``RunContextWrapper``
    or ``ToolContext`` as its leading parameter, as with ``function_tool``. Its name, docstring and
    signature make the tool schema, unchanged.

    Args:
        dialect: the Vericto dialect of the database ``func`` runs SQL against
            (``"postgres"``, ``"snowflake"``, ``"sqlite"``...).
        guard: the :class:`~vericto.Vericto` to use; built from the ``VERICTO_*``
            environment variables when omitted.
        argument: the name of ``func``'s SQL parameter.
        on_block: ``"return"`` (default): a blocked query becomes the tool output, a
            refusal with the rule code and the ``suggested_fix``. ``"raise"``: the run
            stops with :class:`~vericto.VerictoBlocked` as the ``UserError``'s cause.
        context: ``{"ticket": ..., "tenant": ...}`` sent with every evaluation, or a
            callable that returns it (read per call).
        end_user: the end user's attributes sent with every evaluation, or a callable
            that returns them (read per call).
        paramstyle: the driver's DB-API paramstyle, when ``func`` executes the query
            with bound parameters (``%s`` markers are then evaluated as the
            dialect's placeholders; see :meth:`Vericto.enforce`).
        failure_error_function: as in ``function_tool``, for the errors of ``func``
            itself. Vericto's own errors (unavailable under fail-closed, configuration,
            a block under ``on_block="raise"``) are never turned into text for the
            model: they stop the run. ``None`` makes every error stop the run.
        **function_tool_kwargs: passed to ``function_tool`` (``name_override``,
            ``description_override``, ``tool_input_guardrails``, ``needs_approval``,
            ``timeout``...).

    Returns:
        an ``agents.FunctionTool`` (or a decorator that makes one).
    """
    check = ToolCheck(guard, dialect, argument, on_block, context, end_user, paramstyle)
    if failure_error_function is None:
        on_failure: Optional[ToolErrorFunction] = None
    else:
        on_failure = _vericto_failure_function(None if failure_error_function is _UNSET else failure_error_function)

    def make(the_func: Callable[..., Any]) -> FunctionTool:
        return function_tool(
            _guarded(the_func, check),
            failure_error_function=on_failure,
            **function_tool_kwargs,
        )

    if func is not None:
        return make(func)
    return make


def vericto_tool_input_guardrail(
    *,
    dialect: str,
    guard: Optional[Vericto] = None,
    argument: str = "query",
    tools: Optional[Collection[str]] = None,
    on_block: OnBlock = "return",
    context: ContextSource = None,
    end_user: EndUserSource = None,
    name: str = "vericto",
) -> ToolInputGuardrail[Any]:
    """A native ``ToolInputGuardrail`` that checks the SQL argument of a tool call.

    Attach it where the SDK takes tool input guardrails: ``function_tool(...,
    tool_input_guardrails=[...])``, ``FunctionTool.tool_input_guardrails``, or an MCP
    server's ``tool_input_guardrails=`` (applied to every tool of the server; use
    ``tools`` to name the SQL ones).

    * ``BLOCKED``: the call is rejected; the model receives the refusal with the rule
      code and the ``suggested_fix``. ``on_block="raise"``: the run stops with the
      SDK's ``ToolInputGuardrailTripwireTriggered`` (the verdict is its
      ``output.output_info``).
    * an ``approved_query``: the call is rejected (a guardrail cannot change the
      arguments) and the model receives that query with the instruction to call the
      tool again with it. Prefer :func:`vericto_sql_tool` for a tool you write: it
      runs the approved query directly.
    * otherwise: allowed, with the :class:`~vericto.Verdict` as ``output_info``.
    * Vericto unavailable: ``fail_mode="closed"`` stops the run (the SDK raises
      ``UserError`` whose ``__cause__`` is the
      :class:`~vericto.VerictoUnavailable`); ``"open"`` allows the call unevaluated.

    A call in scope whose arguments have no string ``argument`` is rejected: an
    unchecked query never passes for want of a parameter name.

    Args:
        dialect: the Vericto dialect of the database the tool runs SQL against.
        guard: the :class:`~vericto.Vericto` to use; built from the ``VERICTO_*``
            environment variables when omitted.
        argument: the name of the tool's SQL argument (a key of its JSON arguments).
        tools: the tool names to check; ``None`` (default) checks every tool the
            guardrail is attached to. Calls to other tools are allowed unchecked.
        on_block: ``"return"`` (default, reject with a message) or ``"raise"``.
        context, end_user: sent with every evaluation, as in :func:`vericto_sql_tool`.
        name: the guardrail name the SDK reports.
    """
    check = ToolCheck(guard, dialect, argument, on_block, context, end_user, None)
    scope = None if tools is None else frozenset(tools)

    async def vericto_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        tool_name = data.context.tool_name
        if scope is not None and tool_name not in scope:
            return ToolGuardrailFunctionOutput.allow()
        try:
            arguments = json.loads(data.context.tool_arguments or "{}")
        except ValueError:
            arguments = None
        sql = arguments.get(check.argument) if isinstance(arguments, dict) else None
        if not isinstance(sql, str):
            return ToolGuardrailFunctionOutput.reject_content(
                f"Vericto could not find the SQL query in the {check.argument!r} argument of {tool_name}. "
                "It was NOT executed."
            )
        try:
            verdict = await asyncio.to_thread(check.enforce, sql)
        except VerictoBlocked as exc:
            if check.on_block == "raise":
                return ToolGuardrailFunctionOutput.raise_exception(output_info=exc.verdict)
            return ToolGuardrailFunctionOutput.reject_content(block_message(exc), output_info=exc.verdict)
        if verdict.masked and verdict.rewritten_query != sql:
            return ToolGuardrailFunctionOutput.reject_content(approved_query_message(verdict), output_info=verdict)
        return ToolGuardrailFunctionOutput.allow(output_info=verdict)

    return ToolInputGuardrail(guardrail_function=vericto_guardrail, name=name)


__all__ = ["approved_query_message", "vericto_sql_tool", "vericto_tool_input_guardrail"]
