# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""The guard: turns verdicts into "run this SQL" or "raise"."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import warnings
from collections import OrderedDict
from dataclasses import replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, cast

from .dialects import normalize_dialect
from .errors import (
    VerictoBlocked,
    VerictoConfigError,
    VerictoFailOpenWarning,
    VerictoFlaggedWarning,
    VerictoUnavailable,
    VerictoUnsupportedDialect,
)
from .evaluator import Evaluator
from .placeholders import from_evaluation_text, to_evaluation_text
from .policy_context import policy_fields
from .runtime_api import DEFAULT_MAX_RETRIES, RuntimeApiEvaluator
from .verdict import Decision, Verdict

logger = logging.getLogger("vericto")

FAIL_MODES = ("closed", "open")

VerdictCallback = Callable[[Verdict], None]


class Vericto:
    """Checks SQL with Vericto before it runs.

    One instance is meant to be shared by the whole process (it is thread-safe and
    pools its HTTP connections). The driver wrappers call :meth:`enforce` for every
    statement; you can call it yourself around any other driver.

    Args:
        evaluator: what evaluates statements. Defaults to a
            :class:`~vericto.RuntimeApiEvaluator` built from the keyword arguments below
            and the ``VERICTO_*`` environment variables.
        api_key, database_id, api_url, timeout, max_retries: passed to the default
            evaluator.
        fail_mode: ``"closed"`` (default, or ``VERICTO_FAIL_MODE``): when Vericto cannot
            answer, raise :class:`~vericto.VerictoUnavailable` (from :meth:`check`,
            :meth:`enforce` and every wrapper) and do not run the statement.
            ``"open"``: never raise for it; :meth:`check` returns an ``UNEVALUATED``
            verdict with the reason, and :meth:`enforce` also lets the statement run
            unevaluated and emits a :class:`~vericto.VerictoFailOpenWarning`.
        cache_ttl: seconds to reuse a verdict for the same ``(sql, dialect)``. ``0``
            (default) disables the cache; see the README before enabling it.
        cache_size: maximum number of cached verdicts.
        on_verdict: called with every verdict, including ``UNEVALUATED`` ones under
            fail-open and ``BLOCKED`` ones (before the exception is raised).
        warn_on_flag: also emit a :class:`~vericto.VerictoFlaggedWarning` for
            flagged statements (they are always logged on the ``vericto`` logger).
    """

    def __init__(
        self,
        evaluator: Optional[Evaluator] = None,
        *,
        api_key: Optional[str] = None,
        database_id: Optional[str] = None,
        api_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        fail_mode: Optional[str] = None,
        cache_ttl: float = 0.0,
        cache_size: int = 1024,
        on_verdict: Optional[VerdictCallback] = None,
        warn_on_flag: bool = False,
    ) -> None:
        if evaluator is None:
            evaluator = RuntimeApiEvaluator(
                api_key, database_id=database_id, api_url=api_url, timeout=timeout, max_retries=max_retries
            )
        self.evaluator = evaluator

        # An empty or whitespace-only VERICTO_FAIL_MODE is unset: the default, closed.
        from_env = os.environ.get("VERICTO_FAIL_MODE", "").strip()
        source = "fail_mode" if fail_mode is not None or not from_env else "VERICTO_FAIL_MODE"
        mode = (fail_mode if fail_mode is not None else (from_env or "closed")).strip().lower()
        if mode not in FAIL_MODES:
            raise VerictoConfigError(f"{source} must be 'closed' or 'open', got {mode!r}.")
        self.fail_mode = mode
        if cache_ttl < 0 or cache_size < 1:
            raise VerictoConfigError("cache_ttl must be >= 0 and cache_size >= 1.")
        self._cache = _VerdictCache(cache_ttl, cache_size) if cache_ttl > 0 else None
        self.on_verdict = on_verdict
        self.warn_on_flag = warn_on_flag

    def __repr__(self) -> str:
        return f"Vericto(evaluator={self.evaluator!r}, fail_mode={self.fail_mode!r})"

    def __deepcopy__(self, memo: dict[int, Any]) -> Vericto:
        # A guard is a shared, thread-safe service (HTTP pool, locks, cache): frameworks
        # that deep-copy the objects holding it (Airflow operators, pydantic models)
        # get the same guard back instead of failing on its locks.
        return self

    # -- capabilities -------------------------------------------------------------

    def require_dialect(self, dialect: str) -> str:
        """Fail fast if the server reports that it does not evaluate ``dialect``.

        Returns the normalized dialect id. When the server cannot be asked, nothing is
        raised here: each statement is then handled by the fail mode.
        """
        name = normalize_dialect(dialect)
        supported = self.evaluator.supported_dialects()
        if supported is not None and name not in supported:
            api_url = getattr(self.evaluator, "api_url", "the configured evaluator")
            raise VerictoUnsupportedDialect(name, supported, str(api_url))
        return name

    # -- evaluation ---------------------------------------------------------------

    def check(
        self,
        sql: str,
        dialect: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
        end_user: Optional[Mapping[str, Any]] = None,
    ) -> Verdict:
        """Evaluate ``sql`` and return the verdict, whatever it is: use it to ask, and
        :meth:`enforce` to guard an execution.

        Does not raise on ``BLOCKED`` (the verdict says so). When Vericto cannot answer,
        the fail mode applies, as in :meth:`enforce`: ``"closed"`` raises
        :class:`~vericto.VerictoUnavailable`; ``"open"`` returns an ``UNEVALUATED``
        verdict whose ``message`` is the reason, and logs a warning.
        :class:`~vericto.VerictoConfigError` is raised in both modes.

        ``context`` (``{"ticket": ..., "tenant": ...}``) and ``end_user`` (up to 16
        attributes of the end user your service acts for) are sent with the request
        and checked first with the server's limits (:mod:`vericto.policy_context`).
        They take effect when business policies are enabled in your workspace.
        """
        if not isinstance(sql, str):
            raise TypeError(f"vericto: expected the SQL text as str, got {type(sql).__name__}.")
        fields = policy_fields(context, end_user)
        try:
            return self._evaluate(sql, dialect, fields)
        except VerictoUnavailable as exc:
            if self.fail_mode == "closed":
                raise
            logger.warning("vericto: no verdict, check answered UNEVALUATED (fail_mode=open): %s", exc)
            return Verdict.unevaluated(sql, normalize_dialect(dialect), str(exc))

    def enforce(
        self,
        sql: str,
        dialect: str,
        *,
        paramstyle: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
        end_user: Optional[Mapping[str, Any]] = None,
    ) -> Verdict:
        """Evaluate ``sql`` and apply the decision.

        ``paramstyle`` is the driver's DB-API paramstyle, given only when the statement
        is executed with parameters: ``format``/``pyformat`` markers (``%s``,
        ``%(name)s``) are then evaluated as the dialect's own placeholders (see
        :mod:`vericto.placeholders`). Values are never sent. ``context`` and
        ``end_user`` are sent as in :meth:`check`.

        * ``BLOCKED``: raises :class:`~vericto.VerictoBlocked`.
        * ``FLAGGED``, ``MONITORED``, ``PARSE_ERROR`` (allowed by the workspace
          policy): logged and passed to ``on_verdict``; the statement may run.
        * a sensitive-column mask: :attr:`Verdict.sql_to_execute` is the rewrite.
        * Vericto unavailable: raises under ``fail_mode="closed"``; returns an
          ``UNEVALUATED`` verdict under ``"open"``.

        Returns the verdict; run :attr:`Verdict.sql_to_execute`.
        """
        if not isinstance(sql, str):
            raise TypeError(f"vericto: expected the SQL text as str, got {type(sql).__name__}.")
        fields = policy_fields(context, end_user)
        name = normalize_dialect(dialect)
        evaluation_sql, mapping = to_evaluation_text(sql, name, paramstyle)
        try:
            verdict = self._evaluate(evaluation_sql, name, fields)
        except VerictoUnavailable as exc:
            if self.fail_mode == "closed":
                logger.error("vericto: no verdict, statement not executed (fail_mode=closed): %s", exc)
                raise
            verdict = Verdict.unevaluated(sql, name, str(exc))
            logger.warning("vericto: no verdict, statement executed unevaluated (fail_mode=open): %s", exc)
            warnings.warn(
                f"Vericto could not evaluate the statement and fail_mode is 'open', so it ran unchecked: {exc}",
                VerictoFailOpenWarning,
                stacklevel=3,
            )
            self._notify(verdict)
            return verdict

        if mapping is not None:
            rewritten = verdict.rewritten_query
            verdict = replace(
                verdict,
                sql=sql,
                rewritten_query=from_evaluation_text(rewritten, mapping) if rewritten else None,
            )
        self._notify(verdict)
        self._log(verdict)
        if verdict.blocked:
            raise VerictoBlocked(verdict, sql)
        if verdict.decision is Decision.FLAGGED and self.warn_on_flag:
            warnings.warn(
                f"Vericto flagged the statement ({verdict.rule_code or 'no rule code'}).",
                VerictoFlaggedWarning,
                stacklevel=3,
            )
        return verdict

    def prepare(
        self,
        sql: str,
        dialect: str,
        *,
        paramstyle: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
        end_user: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """:meth:`enforce` and return the SQL text to execute."""
        return self.enforce(sql, dialect, paramstyle=paramstyle, context=context, end_user=end_user).sql_to_execute

    def enforce_all(
        self,
        statements: Iterable[str],
        dialect: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
        end_user: Optional[Mapping[str, Any]] = None,
    ) -> List[Verdict]:
        """Enforce a batch: every statement is evaluated before any of them runs.

        A block anywhere raises before the first statement executes, so a script never
        stops half-applied because of Vericto.
        """
        return [self.enforce(statement, dialect, context=context, end_user=end_user) for statement in statements]

    def clear_cache(self) -> None:
        """Drop every cached verdict (a no-op when the cache is off)."""
        if self._cache is not None:
            self._cache.clear()

    # -- internals ----------------------------------------------------------------

    def _evaluate(self, sql: str, dialect: str, fields: Dict[str, Any]) -> Verdict:
        """One evaluation through the cache; raises what the evaluator raises."""
        name = self.require_dialect(dialect)
        # A verdict depends on the declared context too: it is part of the cache key.
        scope = json.dumps(fields, sort_keys=True) if fields else ""
        if self._cache is not None:
            hit = self._cache.get(sql, name, scope)
            if hit is not None:
                return hit
        if fields:
            # Passed only when given, so an evaluator written for evaluate(sql, dialect) keeps working.
            verdict = cast(Any, self.evaluator).evaluate(sql, name, **fields)
        else:
            verdict = self.evaluator.evaluate(sql, name)
        if self._cache is not None and verdict.decision is not Decision.UNEVALUATED:
            self._cache.put(verdict, scope)
        return cast(Verdict, verdict)

    def _notify(self, verdict: Verdict) -> None:
        if self.on_verdict is None:
            return
        try:
            self.on_verdict(verdict)
        except Exception:  # a broken callback must not change the decision
            logger.exception("vericto: on_verdict callback raised")

    @staticmethod
    def _log(verdict: Verdict) -> None:
        # The SQL text is never logged: it can carry personal data in literals.
        extra = f"rule={verdict.rule_code or '-'} severity={verdict.severity or '-'} event_id={verdict.event_id or '-'}"
        if verdict.blocked:
            logger.warning("vericto: statement BLOCKED %s", extra)
        elif verdict.decision is Decision.FLAGGED:
            logger.warning("vericto: statement FLAGGED %s", extra)
        elif verdict.decision is Decision.PARSE_ERROR:
            logger.warning("vericto: statement not parsed, allowed by workspace policy %s", extra)
        elif verdict.decision is Decision.MONITORED:
            logger.info("vericto: statement MONITORED %s", extra)
        if verdict.masked:
            logger.info("vericto: running the masked rewrite %s", extra)


class _VerdictCache:
    """Small thread-safe TTL + LRU cache of server verdicts.

    Keyed by ``(sql, dialect, scope)``, ``scope`` being the declared ``context`` and
    ``end_user``; the evaluator already binds the database. When a
    response carries a ``ruleset_version`` different from the one the cache was
    filled under, every entry is dropped.
    """

    def __init__(self, ttl: float, size: int) -> None:
        self._ttl = ttl
        self._size = size
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[str, str, str], tuple[float, Verdict]] = OrderedDict()
        self._ruleset: Optional[str] = None

    def get(self, sql: str, dialect: str, scope: str = "") -> Optional[Verdict]:
        key = (sql, dialect, scope)
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored_at, verdict = item
            if time.monotonic() - stored_at > self._ttl:
                del self._items[key]
                return None
            self._items.move_to_end(key)
        return replace(verdict, cached=True)

    def put(self, verdict: Verdict, scope: str = "") -> None:
        key = (verdict.sql, verdict.dialect, scope)
        with self._lock:
            if verdict.ruleset_version and verdict.ruleset_version != self._ruleset:
                if self._ruleset is not None:
                    self._items.clear()
                self._ruleset = verdict.ruleset_version
            self._items[key] = (time.monotonic(), verdict)
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
