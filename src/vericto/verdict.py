# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""The result of evaluating one SQL statement."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class Decision(str, Enum):
    """What Vericto decided for a statement.

    ``ALLOWED``, ``FLAGGED``, ``MONITORED``, ``PARSE_ERROR`` and ``BLOCKED`` come from
    the server. ``UNEVALUATED`` is local: Vericto could not answer and the guard runs
    with ``fail_mode="open"``, so the statement ran without a verdict.
    """

    ALLOWED = "ALLOWED"
    FLAGGED = "FLAGGED"
    MONITORED = "MONITORED"
    PARSE_ERROR = "PARSE_ERROR"
    BLOCKED = "BLOCKED"
    UNEVALUATED = "UNEVALUATED"


@dataclass(frozen=True)
class Finding:
    """A rule that matched the statement."""

    rule_code: str
    severity: Optional[str] = None
    rule_id: Optional[str] = None
    ast_node_path: Optional[str] = None
    suggested_fix: Optional[str] = None


@dataclass(frozen=True)
class Verdict:
    """Vericto's answer for one statement.

    Attributes:
        decision: see :class:`Decision`.
        sql: the statement as submitted.
        rewritten_query: when set, the statement to run **instead of** ``sql``: the
            sensitive columns it reads come back masked (Sensitive Column Protection).
        findings: the rules that matched (empty when allowed).
        sensitive_columns: tagged columns the statement reads.
        access_denied: what the API key's allowlist denied (agent access).
        event_id: the audit-trail event id, to look the evaluation up in Vericto.
        raw: the response body, for fields this version does not model yet.
    """

    decision: Decision
    sql: str
    dialect: str
    rule_code: Optional[str] = None
    severity: Optional[str] = None
    message: Optional[str] = None
    suggested_fix: Optional[str] = None
    rewritten_query: Optional[str] = None
    parse_error: Optional[str] = None
    findings: tuple[Finding, ...] = ()
    sensitive_columns: tuple[Any, ...] = ()
    access_denied: tuple[Any, ...] = ()
    event_id: Optional[str] = None
    ruleset_version: Optional[str] = None
    cached: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def blocked(self) -> bool:
        return self.decision is Decision.BLOCKED

    @property
    def masked(self) -> bool:
        """True when the statement must run as :attr:`rewritten_query`."""
        return bool(self.rewritten_query)

    @property
    def degraded(self) -> bool:
        """True when the statement ran without a verdict (fail-open)."""
        return self.decision is Decision.UNEVALUATED

    @property
    def sql_to_execute(self) -> str:
        """The statement the driver must run: the masked rewrite when there is one."""
        return self.rewritten_query or self.sql

    @classmethod
    def from_response(cls, body: Mapping[str, Any], sql: str, dialect: str) -> Verdict:
        """Build a verdict from a ``POST /api/v1/proxy/query`` response body."""
        raw_decision = str(body.get("decision") or "").upper()
        try:
            decision = Decision(raw_decision)
        except ValueError:
            # An unknown decision must never be read as "allowed".
            decision = Decision.BLOCKED
        if decision is Decision.UNEVALUATED:
            decision = Decision.BLOCKED

        rule_code = _opt_str(body.get("rule_code"))
        severity = _opt_str(body.get("severity"))
        suggested = _opt_str(body.get("suggested_safe_query"))
        findings = _findings(body, rule_code, severity, suggested)
        message = _opt_str(body.get("error"))
        if message and rule_code and message == f"Query blocked by rule {rule_code}":
            message = None
        return cls(
            decision=decision,
            sql=sql,
            dialect=dialect,
            rule_code=rule_code,
            severity=severity,
            message=message,
            suggested_fix=suggested,
            # A blocked statement never runs, so it has nothing to rewrite.
            rewritten_query=None if decision is Decision.BLOCKED else _opt_str(body.get("rewritten_query")),
            parse_error=_opt_str(body.get("parse_error")),
            findings=findings,
            sensitive_columns=tuple(body.get("sensitive_columns") or ()),
            access_denied=tuple(body.get("access_denied") or ()),
            event_id=_opt_str(body.get("event_id")),
            ruleset_version=_opt_str(body.get("ruleset_version")),
            raw=dict(body),
        )

    @classmethod
    def unevaluated(cls, sql: str, dialect: str, reason: str) -> Verdict:
        return cls(decision=Decision.UNEVALUATED, sql=sql, dialect=dialect, message=reason)


def _opt_str(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _findings(
    body: Mapping[str, Any], rule_code: Optional[str], severity: Optional[str], suggested: Optional[str]
) -> tuple[Finding, ...]:
    # The Runtime API reports the deciding rule; a later version may send the full list.
    listed = body.get("findings") or body.get("violations")
    if isinstance(listed, list) and listed:
        out = []
        for item in listed:
            if not isinstance(item, Mapping):
                continue
            code = _opt_str(item.get("rule_code") or item.get("code"))
            if not code:
                continue
            out.append(
                Finding(
                    rule_code=code,
                    severity=_opt_str(item.get("severity")),
                    rule_id=_opt_str(item.get("rule_id")),
                    ast_node_path=_opt_str(item.get("ast_node_path")),
                    suggested_fix=_opt_str(item.get("suggested_safe_query") or item.get("suggested_fix")),
                )
            )
        if out:
            return tuple(out)
    if rule_code:
        return (
            Finding(
                rule_code=rule_code,
                severity=severity,
                rule_id=_opt_str(body.get("rule_id")),
                ast_node_path=_opt_str(body.get("ast_node_path")),
                suggested_fix=suggested,
            ),
        )
    return ()
