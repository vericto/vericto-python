# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""The evaluator interface.

An evaluator turns ``(sql, dialect)`` into a :class:`~vericto.Verdict`. This release
ships one, :class:`~vericto.RuntimeApiEvaluator`, which asks the Vericto Runtime API.
An embedded evaluator (the engine linked into the process, with synced rules) can be
added later behind the same interface without touching the wrappers.
"""

from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from .verdict import Verdict


@runtime_checkable
class Evaluator(Protocol):
    """Evaluates one SQL statement.

    Implementations return a verdict for every evaluated statement (``BLOCKED``
    included: blocking is the guard's job, not the evaluator's), raise
    :class:`~vericto.VerictoUnavailable` when no verdict can be produced, and
    :class:`~vericto.VerictoConfigError` when the request can never succeed.

    When the caller gives ``context`` or ``end_user`` (see
    :mod:`vericto.policy_context`), the guard passes them as keyword arguments of the
    same names: ``evaluate(sql, dialect, context=..., end_user=...)``. Without them it
    calls ``evaluate(sql, dialect)``, so an evaluator that does not take them keeps
    working for every other call.
    """

    def evaluate(self, sql: str, dialect: str) -> Verdict:
        """The verdict for ``sql`` in ``dialect`` (a normalized Vericto dialect id)."""
        ...

    def supported_dialects(self) -> Optional[Sequence[str]]:
        """The dialects this evaluator handles, or ``None`` when it cannot tell."""
        ...
