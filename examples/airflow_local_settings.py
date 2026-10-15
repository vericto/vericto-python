"""airflow_local_settings.py: guard every common-sql task with Vericto, no DAG changes.

Put it on the PYTHONPATH (or in $AIRFLOW_HOME/config) of the DAG processor and of
every worker, with VERICTO_API_KEY / VERICTO_DATABASE_ID in the workers' environment.
"""

from __future__ import annotations

from typing import Any

from vericto.airflow.policy import guard_sql_tasks


def task_policy(task: Any) -> None:
    # Fail the tasks Vericto cannot evaluate (BigQuery, Trino...) or let them run
    # unchecked with on_unsupported="skip"; map ODBC/JDBC connections with
    # {"vericto_dialect": "mssql"} in the connection's extra.
    guard_sql_tasks(task, on_unsupported="skip")
