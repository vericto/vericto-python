"""Exceptions raised by vericto.

Every exception derives from :class:`VerictoError`, so ``except VerictoError``
catches anything this package raises. The two you normally handle are:

* :class:`VerictoBlocked`: Vericto evaluated the statement and blocked it. The
  statement was **not** executed.
* :class:`VerictoUnavailable`: Vericto could not give a verdict (network,
  timeout, server error, monthly quota) and the guard runs with
  ``fail_mode="closed"``. The statement was **not** executed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from .verdict import Finding, Verdict


class VerictoError(Exception):
    """Base class for every error raised by vericto."""


class VerictoConfigError(VerictoError):
    """The client is misconfigured: missing or rejected API key, bad database id,
    a key without the ``proxy:evaluate`` scope, or a malformed request.

    Raised whatever the fail mode is: failing open on a revoked key would turn the
    firewall off without anyone noticing.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class VerictoUnsupportedDialect(VerictoConfigError):
    """The Vericto server does not evaluate this dialect.

    Raised before any SQL runs, when the dialect is missing from the server's
    ``GET /api/v1/meta/config`` (for example ``sqlite`` against a server older than
    vericto-engine 4.1.0, or a dialect Vericto does not support at all).
    """

    def __init__(self, dialect: str, supported: Sequence[str], api_url: str) -> None:
        listed = ", ".join(supported) if supported else "none reported"
        super().__init__(
            f"The Vericto API at {api_url} does not support the '{dialect}' dialect "
            f"(supported: {listed}). No SQL was executed. "
            "Upgrade the server, or use a supported dialect.",
            code="UNSUPPORTED_DIALECT",
        )
        self.dialect = dialect
        self.supported = tuple(supported)


class VerictoUnavailable(VerictoError):
    """Vericto could not return a verdict after the configured retries.

    With ``fail_mode="closed"`` (the default) the statement is not executed and this
    is raised. With ``fail_mode="open"`` the statement runs and a
    :class:`VerictoFailOpenWarning` is emitted instead.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class VerictoQuotaExceeded(VerictoUnavailable):
    """The workspace used up its monthly query quota (HTTP 429 ``QUOTA_EXCEEDED``).

    Not retried: the quota resets at ``resets_at``. Subject to the fail mode like any
    other unavailability.
    """

    def __init__(self, message: str, *, resets_at: Optional[str] = None, upgrade_url: Optional[str] = None) -> None:
        super().__init__(message, status=429, code="QUOTA_EXCEEDED")
        self.resets_at = resets_at
        self.upgrade_url = upgrade_url


class VerictoBlocked(VerictoError):
    """Vericto blocked the statement. It was not executed.

    Attributes:
        verdict: the full :class:`~vericto.Verdict`.
        findings: the rule findings that led to the block.
        suggested_fix: a safer statement Vericto proposes, when it has one.
        sql: the statement as the application wrote it.
    """

    def __init__(self, verdict: Verdict, sql: str) -> None:
        self.verdict = verdict
        self.sql = sql
        rule = verdict.rule_code or "a Vericto rule"
        message = f"Query blocked by {rule}"
        if verdict.severity:
            message += f" (severity {verdict.severity})"
        if verdict.message:
            message += f": {verdict.message}"
        if verdict.suggested_fix:
            message += f". Suggested fix: {verdict.suggested_fix}"
        super().__init__(message)

    @property
    def findings(self) -> tuple[Finding, ...]:
        return self.verdict.findings

    @property
    def suggested_fix(self) -> Optional[str]:
        return self.verdict.suggested_fix


class VerictoFailOpenWarning(UserWarning):
    """Emitted when a statement runs unevaluated because ``fail_mode="open"``."""


class VerictoFlaggedWarning(UserWarning):
    """Emitted (only with ``warn_on_flag=True``) when a statement is flagged."""
