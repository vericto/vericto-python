# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""A cluster policy that guards existing SQL tasks with Vericto, without DAG changes.

In ``airflow_local_settings.py`` (on ``PYTHONPATH`` or in ``$AIRFLOW_HOME/config``)
of the DAG processor **and the workers**::

    from vericto.airflow.policy import guard_sql_tasks

    def task_policy(task):
        guard_sql_tasks(task)

Every common-sql operator (``SQLExecuteQueryOperator`` and its subclasses, the
``SQL*Check`` operators, provider operators built on them) then checks its
rendered SQL with Vericto right before it runs, exactly as
:class:`~vericto.airflow.VerictoSQLExecuteQueryOperator` does. The check is
installed as the task's ``pre_execute`` hook (an existing hook still runs, first),
so the operator class, its serialization and the UI are unchanged.

Airflow applies ``task_policy`` whenever a DAG file is parsed, including on the
worker that runs the task, so the guard is in place where the SQL executes.
Parsing never calls Vericto; the guard is built from the worker's ``VERICTO_*``
environment on the first checked task.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple, Type

from airflow.providers.common.sql.operators.sql import BaseSQLOperator

from ..guard import Vericto
from . import VerictoSQLExecuteQueryOperator, _check_on_unsupported, check_task_sql

MARKER = "_vericto_guarded"


def guard_sql_tasks(
    task: Any,
    *,
    guard: Optional[Vericto] = None,
    dialect: Optional[str] = None,
    on_unsupported: str = "fail",
    operators: Tuple[Type[Any], ...] = (BaseSQLOperator,),
    when: Optional[Callable[[Any], bool]] = None,
) -> bool:
    """Guard ``task`` if it is a SQL operator. Returns whether it is now guarded.

    Args:
        task: the task ``task_policy`` receives.
        guard: the :class:`~vericto.Vericto` to use; the process-wide one from
            ``VERICTO_*`` when omitted.
        dialect: force a dialect instead of reading it from the connection.
        on_unsupported: ``"fail"`` (default) fails a task whose connection type has
            no Vericto dialect, before anything runs; ``"skip"`` runs it unchecked
            with a warning. Set ``vericto_dialect`` in a connection's extra to map
            ODBC/JDBC connections.
        operators: the operator classes to guard (default: every common-sql operator).
        when: an optional predicate on the task (e.g. by ``conn_id`` or DAG tags).

    Idempotent: a task is guarded once, and
    :class:`~vericto.airflow.VerictoSQLExecuteQueryOperator` tasks are left alone
    (they check themselves).
    """
    mode = _check_on_unsupported(on_unsupported)
    if not isinstance(task, operators) or isinstance(task, VerictoSQLExecuteQueryOperator):
        return False
    if getattr(task, MARKER, False):
        return True
    if when is not None and not when(task):
        return False

    previous = getattr(task, "_pre_execute_hook", None)

    def vericto_pre_execute(context: Any) -> None:
        if previous is not None:
            previous(context)
        # The task being executed: the rendered copy Airflow made for this run.
        running = context.get("task", task) if hasattr(context, "get") else task
        check_task_sql(running, guard=guard, dialect=dialect, on_unsupported=mode)

    task._pre_execute_hook = vericto_pre_execute
    setattr(task, MARKER, True)
    return True


__all__ = ["guard_sql_tasks"]
