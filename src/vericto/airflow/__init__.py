# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Vericto for Apache Airflow (2.9+ and 3.x), on the common-sql provider.

Two ways in, sharing the same check:

* :class:`VerictoSQLExecuteQueryOperator`: a drop-in ``SQLExecuteQueryOperator``
  that checks every statement before any of them runs::

      from vericto.airflow import VerictoSQLExecuteQueryOperator

      VerictoSQLExecuteQueryOperator(task_id="cleanup", conn_id="warehouse", sql="sql/cleanup.sql")

* :func:`vericto.airflow.policy.guard_sql_tasks`: a cluster policy for
  ``airflow_local_settings.task_policy`` that guards existing SQL operators
  without DAG changes.

The SQL checked is the **rendered** SQL (templates are rendered before the check
runs). A script is split with the hook's own ``split_sql_string`` and every
statement is evaluated before the first one runs:

* ``BLOCKED``: the task fails with :class:`VerictoTaskBlocked` (an
  ``AirflowFailException``, so it is not retried: the same SQL would be blocked
  again) and nothing runs.
* masked columns: the masked rewrite runs instead.
* ``FLAGGED`` / ``MONITORED``: runs; logged on the ``vericto`` logger and passed to
  the guard's ``on_verdict``.
* Vericto unavailable: ``fail_mode="closed"`` (default) fails the task with
  :class:`~vericto.VerictoUnavailable`, which is retried like any other error.

The dialect comes from the connection type (``postgres``, ``redshift`` (as
postgres), ``mysql``, ``mssql``, ``oracle``, ``snowflake``, ``sqlite``), or from
``vericto_dialect`` in the connection's extra, or from the operator argument.

Requires the ``airflow`` extra: ``pip install "vericto[airflow]"``. The guard is
built from the ``VERICTO_*`` environment variables of the worker the first time a
task needs it (DAG parsing never calls Vericto).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Mapping, Optional

from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

from ..dialects import normalize_dialect
from ..errors import VerictoBlocked, VerictoConfigError
from ..guard import Vericto
from ..verdict import Verdict

try:  # Airflow 3
    from airflow.sdk.exceptions import AirflowFailException
except ImportError:  # Airflow 2
    from airflow.exceptions import AirflowFailException  # type: ignore[no-redef,attr-defined,unused-ignore]

logger = logging.getLogger("vericto")

#: Airflow connection type -> Vericto dialect.
CONN_TYPE_DIALECTS: Mapping[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "redshift": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "mssql",
    "oracle": "oracle",
    "snowflake": "snowflake",
    "sqlite": "sqlite",
}
ON_UNSUPPORTED = ("fail", "skip")
EXTRA_KEY = "vericto_dialect"


class VerictoTaskBlocked(VerictoBlocked, AirflowFailException):
    """Vericto blocked a statement of the task; nothing ran. Not retried."""


_shared: Optional[Vericto] = None
_shared_lock = threading.Lock()


def shared_guard() -> Vericto:
    """The process-wide guard, built from ``VERICTO_*`` on first use."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Vericto()
        return _shared


def _check_on_unsupported(value: str) -> str:
    if value not in ON_UNSUPPORTED:
        raise VerictoConfigError(f"on_unsupported must be 'fail' or 'skip', got {value!r}.")
    return value


def dialect_for_hook(hook: Any, override: Optional[str] = None) -> Optional[str]:
    """The Vericto dialect for a DB hook, or ``None`` when its connection type is not mapped."""
    if override:
        return normalize_dialect(override)
    conn = None
    try:
        conn = hook.connection
    except Exception:  # no connection property on this hook, or it cannot be read
        logger.debug("vericto: could not read the hook's connection", exc_info=True)
    if conn is not None:
        extra = getattr(conn, "extra_dejson", None) or {}
        if isinstance(extra, Mapping) and extra.get(EXTRA_KEY):
            return normalize_dialect(str(extra[EXTRA_KEY]))
    conn_type = (getattr(conn, "conn_type", None) or getattr(hook, "conn_type", None) or "").lower()
    return CONN_TYPE_DIALECTS.get(conn_type)


def _paramstyle(hook: Any, sql: str, parameters: Any) -> Optional[str]:
    if not parameters:
        return None
    if isinstance(parameters, Mapping) and "%(" in sql:
        return "pyformat"
    placeholder = getattr(hook, "placeholder", "%s")
    return "format" if placeholder == "%s" else None


def _statements(hook: Any, sql: Any) -> List[str]:
    if isinstance(sql, str):
        split = getattr(hook, "split_sql_string", None)
        parts = split(sql) if callable(split) else [sql]
        return [p for p in parts if p and p.strip()]
    if isinstance(sql, (list, tuple)):
        return [s for s in sql if isinstance(s, str) and s.strip()]
    raise TypeError(f"vericto: expected the task's sql as str or list of str, got {type(sql).__name__}.")


def check_task_sql(
    task: Any,
    *,
    guard: Optional[Vericto] = None,
    dialect: Optional[str] = None,
    on_unsupported: str = "fail",
) -> List[Verdict]:
    """Check a SQL task's rendered ``sql`` and apply masks to it. Call before it runs.

    Every statement is evaluated before any runs. Raises :class:`VerictoTaskBlocked`
    on a block, :class:`~vericto.VerictoUnavailable` (fail-closed) or
    :class:`~vericto.VerictoConfigError`. When a statement is masked, ``task.sql``
    is replaced with the statements to run.
    """
    hook = task.get_db_hook()
    name = dialect_for_hook(hook, dialect)
    if name is None:
        conn_type = getattr(getattr(hook, "connection", None), "conn_type", None) or getattr(hook, "conn_type", "?")
        message = (
            f"Vericto has no dialect for the connection type '{conn_type}' of task {task.task_id}. "
            f"Set '{EXTRA_KEY}' in the connection extra or vericto_dialect= on the operator "
            f"(one of: postgres, mysql, oracle, mssql, snowflake)."
        )
        if on_unsupported == "skip":
            logger.warning("vericto: %s The task runs unchecked (on_unsupported='skip').", message)
            return []
        raise VerictoConfigError(message + " Nothing was executed.")

    g = guard if guard is not None else shared_guard()
    g.require_dialect(name)
    sql = task.sql
    statements = _statements(hook, sql)
    parameters = getattr(task, "parameters", None)
    verdicts: List[Verdict] = []
    for statement in statements:
        try:
            verdicts.append(g.enforce(statement, name, paramstyle=_paramstyle(hook, statement, parameters)))
        except VerictoBlocked as exc:
            raise VerictoTaskBlocked(exc.verdict, exc.sql) from None

    if any(v.masked for v in verdicts):
        rewritten = [v.sql_to_execute for v in verdicts]
        task.sql = rewritten[0] if isinstance(sql, str) and len(rewritten) == 1 else rewritten
        logger.info("vericto: task %s runs the masked rewrite of %d statement(s)", task.task_id, len(rewritten))
    return verdicts


class VerictoSQLExecuteQueryOperator(SQLExecuteQueryOperator):
    """``SQLExecuteQueryOperator`` that checks the rendered SQL with Vericto first.

    Args:
        vericto_guard: the :class:`~vericto.Vericto` to use; the process-wide guard
            from ``VERICTO_*`` when omitted.
        vericto_dialect: override the dialect read from the connection.
        vericto_on_unsupported: ``"fail"`` (default): a connection type Vericto has
            no dialect for fails the task before anything runs. ``"skip"``: run it
            unchecked, with a warning.

    Every other argument is ``SQLExecuteQueryOperator``'s.
    """

    def __init__(
        self,
        *,
        vericto_guard: Optional[Vericto] = None,
        vericto_dialect: Optional[str] = None,
        vericto_on_unsupported: str = "fail",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.vericto_guard = vericto_guard
        self.vericto_dialect = vericto_dialect
        self.vericto_on_unsupported = _check_on_unsupported(vericto_on_unsupported)

    def execute(self, context: Any) -> Any:
        check_task_sql(
            self, guard=self.vericto_guard, dialect=self.vericto_dialect, on_unsupported=self.vericto_on_unsupported
        )
        return super().execute(context)  # type: ignore[no-untyped-call,unused-ignore]  # untyped upstream


__all__ = [
    "CONN_TYPE_DIALECTS",
    "VerictoSQLExecuteQueryOperator",
    "VerictoTaskBlocked",
    "check_task_sql",
    "dialect_for_hook",
    "shared_guard",
]
