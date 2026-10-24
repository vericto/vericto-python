# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Shared pieces of the LLM-framework integrations (LangChain, LlamaIndex).

* :func:`block_message`: the text an agent receives instead of rows when Vericto
  blocks its query, written so the model can correct itself.
* :func:`describe_verdict`: a one-paragraph verdict for check tools.
* :class:`EngineGate`: checks the SQL an integration sends through a SQLAlchemy
  engine, and only that SQL (not schema reflection, not the framework's own
  ``SET search_path``), so a query costs one evaluation.

Nothing here imports LangChain or LlamaIndex.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Collection, Iterator, Optional

from .errors import VerictoBlocked, VerictoConfigError
from .guard import Vericto
from .policy_context import ContextSource, EndUserSource, policy_fields, resolve
from .verdict import Decision, Verdict

ON_BLOCK = ("return", "raise")


def check_on_block(value: str) -> str:
    mode = str(value).strip().lower()
    if mode not in ON_BLOCK:
        raise VerictoConfigError(f"on_block must be 'return' or 'raise', got {value!r}.")
    return mode


def check_sources(context: ContextSource, end_user: EndUserSource) -> None:
    """Validate fixed ``context`` / ``end_user`` values at construction (callables are checked per query)."""
    policy_fields(
        None if callable(context) else context,
        None if callable(end_user) else end_user,
    )


def _rule_label(verdict: Verdict) -> str:
    rule = verdict.rule_code or "a Vericto rule"
    return f"{rule}, severity {verdict.severity}" if verdict.severity else rule


def _denied(verdict: Verdict) -> Optional[str]:
    if not verdict.access_denied:
        return None
    parts = []
    for item in verdict.access_denied:
        if isinstance(item, dict):
            name = ".".join(str(item[k]) for k in ("schema", "table", "column") if item.get(k))
            parts.append(name or str(item))
        else:
            parts.append(str(item))
    return ", ".join(parts)


def block_message(blocked: VerictoBlocked) -> str:
    """What the agent reads when its query is blocked. Never contains row data."""
    verdict = blocked.verdict
    lines = [f"Error: Vericto blocked this query ({_rule_label(verdict)}). It was NOT executed."]
    if verdict.message:
        lines.append(f"Reason: {verdict.message}")
    others = [f for f in verdict.findings if f.rule_code != verdict.rule_code]
    if others:
        lines.append("Other findings: " + ", ".join(f.rule_code for f in others) + ".")
    denied = _denied(verdict)
    if denied:
        lines.append(f"Not allowed for this agent: {denied}.")
    if verdict.suggested_fix:
        lines.append(f"Suggested fix: {verdict.suggested_fix}")
        lines.append("Rewrite the query following the suggested fix and try again.")
    else:
        lines.append("Rewrite the query so that it complies with this rule, or tell the user it cannot be run.")
    if verdict.event_id:
        lines.append(f"(Vericto event {verdict.event_id})")
    return "\n".join(lines)


def describe_verdict(verdict: Verdict) -> str:
    """A readable verdict for a check-only tool."""
    if verdict.blocked:
        return block_message(VerictoBlocked(verdict, verdict.sql))
    if verdict.decision is Decision.UNEVALUATED:
        return f"UNEVALUATED: Vericto could not evaluate the query ({verdict.message or 'no verdict'})."
    if verdict.decision is Decision.ALLOWED and not verdict.masked:
        text = "ALLOWED: Vericto found no problem with this query."
    elif verdict.decision is Decision.PARSE_ERROR:
        text = "PARSE_ERROR: Vericto could not parse the query; the workspace policy lets it run."
        if verdict.parse_error:
            text += f" Parser: {verdict.parse_error}"
    else:
        text = f"{verdict.decision.value}: allowed to run ({_rule_label(verdict)})."
        if verdict.message:
            text += f" {verdict.message}"
    if verdict.masked:
        text += f" Sensitive columns are masked: the query that will run is: {verdict.rewritten_query}"
    return text


class EngineGate:
    """A ``before_cursor_execute`` listener that is active only inside :meth:`active`.

    The integrations wrap their query entry points in ``with gate.active():``; the
    statements SQLAlchemy sends to the driver meanwhile are enforced (blocked ones
    raise :class:`~vericto.VerictoBlocked`, masked ones run as the rewrite).
    Statements in ``internal`` (fixed SQL the framework issues itself, such as
    ``SET search_path``) are let through unevaluated.

    When ``passthrough`` is true the engine is already checked by
    :func:`vericto.sqlalchemy.instrument`: no listener is added, so a statement is
    evaluated once, and blocks still raise from that listener (``context`` and
    ``end_user`` then belong to ``instrument``; giving them here raises
    :class:`~vericto.VerictoConfigError` rather than dropping them).

    ``context`` and ``end_user`` (a value, or a callable read for every statement) are
    sent with each evaluation; see :mod:`vericto.policy_context`.
    """

    def __init__(
        self,
        engine: Any,
        guard: Vericto,
        dialect: str,
        *,
        internal: Collection[str] = (),
        passthrough: bool = False,
        context: ContextSource = None,
        end_user: EndUserSource = None,
    ) -> None:
        from sqlalchemy import event

        check_sources(context, end_user)
        if passthrough and (context is not None or end_user is not None):
            raise VerictoConfigError(
                "The engine is already instrumented: pass context and end_user to vericto.sqlalchemy.instrument()."
            )
        self.guard = guard
        self.context = context
        self.end_user = end_user
        self.dialect = dialect
        self.internal = frozenset(internal)
        self._active: contextvars.ContextVar[bool] = contextvars.ContextVar(f"vericto_gate_{id(self)}", default=False)
        self._target = getattr(engine, "sync_engine", engine)
        self._paramstyle: Optional[str] = getattr(self._target.dialect, "paramstyle", None)
        self._listening = False
        if not passthrough:
            event.listen(self._target, "before_cursor_execute", self._before_cursor_execute, retval=True)
            self._listening = True

    def _before_cursor_execute(
        self, conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> tuple[str, Any]:
        if not self._active.get() or statement in self.internal:
            return statement, parameters
        has_params = executemany or bool(parameters)
        final = self.guard.prepare(
            statement,
            self.dialect,
            paramstyle=self._paramstyle if has_params else None,
            context=resolve(self.context),
            end_user=resolve(self.end_user),
        )
        return final, parameters

    @contextmanager
    def active(self) -> Iterator[None]:
        token = self._active.set(True)
        try:
            yield
        finally:
            self._active.reset(token)

    def remove(self) -> None:
        """Detach the listener (idempotent)."""
        from sqlalchemy import event

        if self._listening and event.contains(self._target, "before_cursor_execute", self._before_cursor_execute):
            event.remove(self._target, "before_cursor_execute", self._before_cursor_execute)
        self._listening = False
