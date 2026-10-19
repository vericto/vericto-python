# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""vericto.airflow against a real Airflow (2.9+ or 3.x) and the common-sql + sqlite providers.

Tasks run with ``dag.test()`` (Airflow's in-process runner, metadata DB in a temp
AIRFLOW_HOME), so templating, pre_execute hooks and the executor safeguard are the
real ones.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import pytest

if "AIRFLOW_HOME" not in os.environ:  # must be set before airflow is imported
    _home = tempfile.mkdtemp(prefix="vericto-airflow-")
    os.environ["AIRFLOW_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
os.environ.setdefault("AIRFLOW__CORE__UNIT_TEST_MODE", "True")

pytest.importorskip("airflow")
pytest.importorskip("airflow.providers.common.sql")
pytest.importorskip("airflow.providers.sqlite")

import airflow
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

from tests.conftest import API_KEY, DATABASE_ID, FakeVericto, blocked, flagged, masked
from vericto import Vericto, VerictoConfigError, VerictoUnavailable
from vericto.airflow import (
    VerictoSQLExecuteQueryOperator,
    VerictoTaskBlocked,
    check_task_sql,
    dialect_for_hook,
)
from vericto.airflow.policy import guard_sql_tasks

AIRFLOW_3 = int(airflow.__version__.split(".")[0]) >= 3
CONN_ID = "vericto_test_db"


@pytest.fixture(scope="session", autouse=True)
def _airflow_db() -> None:
    from airflow.utils import db

    db.resetdb() if not AIRFLOW_3 else db.initdb()


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "warehouse.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
        c.execute("INSERT INTO users VALUES (1, 'ana@example.com'), (2, 'bo@example.com')")
    monkeypatch.setenv(f"AIRFLOW_CONN_{CONN_ID.upper()}", json.dumps({"conn_type": "sqlite", "host": str(path)}))
    return path


def _rows(path: Path, sql: str = "SELECT id, email FROM users ORDER BY id") -> List[Any]:
    with sqlite3.connect(path) as c:
        return c.execute(sql).fetchall()


_counter = iter(range(10_000))

HEADER = """
from datetime import datetime
try:
    from airflow.sdk import DAG
except ImportError:
    from airflow import DAG
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from vericto.airflow import VerictoSQLExecuteQueryOperator
"""


@pytest.fixture
def vericto_env(fake: FakeVericto, monkeypatch: pytest.MonkeyPatch) -> FakeVericto:
    """Workers build the guard from VERICTO_*: point it at the stand-in API."""
    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_DATABASE_ID", DATABASE_ID)
    monkeypatch.setenv("VERICTO_API_URL", fake.url)
    return fake


@pytest.fixture
def dag_file() -> Iterator[Callable[[str], Any]]:
    """Write a DAG file into the dags folder and load it, as Airflow does."""
    from airflow.models.dagbag import DagBag

    folder = Path(os.environ["AIRFLOW_HOME"]) / "dags"
    folder.mkdir(exist_ok=True)
    written: List[Path] = []

    def make(tasks: str) -> Any:
        dag_id = f"vericto_test_{next(_counter)}"
        path = folder / f"{dag_id}.py"
        body = textwrap.indent(textwrap.dedent(tasks).strip(), "    ")
        path.write_text(
            HEADER
            + f"with DAG({dag_id!r}, schedule=None, start_date=datetime(2026, 1, 1), catchup=False):\n"
            + body
            + "\n"
        )
        written.append(path)
        bag = DagBag(dag_folder=str(path))
        assert not bag.import_errors, bag.import_errors
        return bag.dags[dag_id]

    yield make
    for path in written:
        path.unlink(missing_ok=True)


def _run(dag: Any) -> Dict[str, str]:
    """dag.test() and return the task-instance states by task id."""
    run = dag.test()
    return {ti.task_id: str(ti.state) for ti in run.get_task_instances()}


def _failed(states: Dict[str, str], task_id: str) -> bool:
    return "failed" in states[task_id]


# -- operator -----------------------------------------------------------------------


def test_operator_runs_allowed_rendered_sql(vericto_env: FakeVericto, database: Path, dag_file: Any) -> None:
    dag = dag_file(
        f"""
        VerictoSQLExecuteQueryOperator(
            task_id="update",
            conn_id="{CONN_ID}",
            sql="UPDATE users SET email = 'x@example.com' WHERE id = {{{{ params.user_id }}}}",
            params={{"user_id": 2}},
        )
        """
    )
    states = _run(dag)
    assert not _failed(states, "update")
    # the rendered SQL, not the template
    assert vericto_env.queries() == ["UPDATE users SET email = 'x@example.com' WHERE id = 2"]
    assert vericto_env.requests[0]["dialect"] == "sqlite"
    assert _rows(database)[1] == (2, "x@example.com")


def test_operator_blocks_before_any_statement_runs(vericto_env: FakeVericto, database: Path, dag_file: Any) -> None:
    vericto_env.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-010"))
    dag = dag_file(
        f"""
        VerictoSQLExecuteQueryOperator(
            task_id="script",
            conn_id="{CONN_ID}",
            sql="UPDATE users SET email = 'y@example.com' WHERE id = 1; DELETE FROM users;",
            retries=3,
        )
        """
    )
    states = _run(dag)
    assert _failed(states, "script")  # failed, not up_for_retry: a block is not retried
    assert len(vericto_env.requests) == 2
    assert _rows(database)[0] == (1, "ana@example.com")  # the allowed UPDATE did not run either


def test_operator_runs_the_masked_rewrite(vericto_env: FakeVericto, database: Path, dag_file: Any) -> None:
    vericto_env.on(
        lambda sql: sql.startswith("INSERT INTO audit"),
        masked("INSERT INTO audit SELECT id, '***' FROM users"),
    )
    with sqlite3.connect(database) as c:
        c.execute("CREATE TABLE audit (id INTEGER, email TEXT)")
    dag = dag_file(
        f"""
        VerictoSQLExecuteQueryOperator(
            task_id="copy", conn_id="{CONN_ID}", sql="INSERT INTO audit SELECT id, email FROM users"
        )
        """
    )
    states = _run(dag)
    assert not _failed(states, "copy")
    assert _rows(database, "SELECT email FROM audit") == [("***",), ("***",)]


def test_operator_flagged_runs(vericto_env: FakeVericto, database: Path, dag_file: Any) -> None:
    vericto_env.on(lambda sql: True, flagged())
    dag = dag_file(
        f"""
        VerictoSQLExecuteQueryOperator(task_id="q", conn_id="{CONN_ID}", sql=["DELETE FROM users WHERE id = 2"])
        """
    )
    states = _run(dag)
    assert not _failed(states, "q")
    assert _rows(database) == [(1, "ana@example.com")]


def test_operator_fail_closed(vericto_env: FakeVericto, database: Path, dag_file: Any) -> None:
    vericto_env.on(lambda sql: True, {"status": 503, "body": {}})
    dag = dag_file(
        f"""
        VerictoSQLExecuteQueryOperator(task_id="q", conn_id="{CONN_ID}", sql="DELETE FROM users WHERE id = 2")
        """
    )
    states = _run(dag)
    assert _failed(states, "q")
    assert len(_rows(database)) == 2


def test_parameters_are_not_sent(vericto_env: FakeVericto, database: Path, dag_file: Any) -> None:
    dag = dag_file(
        f"""
        VerictoSQLExecuteQueryOperator(
            task_id="q", conn_id="{CONN_ID}", sql="DELETE FROM users WHERE id = ?", parameters=(2,)
        )
        """
    )
    _run(dag)
    assert vericto_env.queries() == ["DELETE FROM users WHERE id = ?"]
    assert len(_rows(database)) == 1


def test_operator_validates_on_unsupported() -> None:
    with pytest.raises(VerictoConfigError):
        VerictoSQLExecuteQueryOperator(task_id="x", conn_id=CONN_ID, sql="SELECT 1", vericto_on_unsupported="ignore")


def test_operator_deep_copies_without_copying_the_guard(guard: Vericto) -> None:
    import copy

    op = VerictoSQLExecuteQueryOperator(task_id="x", conn_id=CONN_ID, sql="SELECT 1", vericto_guard=guard)
    assert copy.deepcopy(op).vericto_guard is guard


# -- check_task_sql and dialects ----------------------------------------------------


class _Conn:
    def __init__(self, conn_type: str, extra: Any = None) -> None:
        self.conn_type = conn_type
        self.extra_dejson = extra or {}


class _Hook:
    placeholder = "%s"

    def __init__(self, conn_type: str, extra: Any = None) -> None:
        self.connection = _Conn(conn_type, extra)

    @staticmethod
    def split_sql_string(sql: str) -> List[str]:
        return [s.strip() for s in sql.split(";") if s.strip()]


class _Task:
    task_id = "t"

    def __init__(self, sql: Any, conn_type: str = "postgres", extra: Any = None, parameters: Any = None) -> None:
        self.sql = sql
        self.parameters = parameters
        self._hook = _Hook(conn_type, extra)

    def get_db_hook(self) -> _Hook:
        return self._hook


@pytest.mark.parametrize(
    ("conn_type", "dialect"),
    [
        ("postgres", "postgres"),
        ("redshift", "postgres"),
        ("mysql", "mysql"),
        ("mssql", "mssql"),
        ("oracle", "oracle"),
        ("snowflake", "snowflake"),
        ("sqlite", "sqlite"),
        ("trino", None),
        ("odbc", None),
    ],
)
def test_dialect_from_connection_type(conn_type: str, dialect: Any) -> None:
    assert dialect_for_hook(_Hook(conn_type)) == dialect


def test_dialect_from_connection_extra_and_override() -> None:
    assert dialect_for_hook(_Hook("odbc", {"vericto_dialect": "sqlserver"})) == "mssql"
    assert dialect_for_hook(_Hook("postgres"), "snowflake") == "snowflake"


def test_unsupported_connection_fails_or_skips(fake: FakeVericto, guard: Vericto) -> None:
    with pytest.raises(VerictoConfigError, match="no dialect for the connection type 'trino'"):
        check_task_sql(_Task("SELECT 1", "trino"), guard=guard)
    assert check_task_sql(_Task("SELECT 1", "trino"), guard=guard, on_unsupported="skip") == []
    assert fake.requests == []


def test_pyformat_parameters_are_evaluated_as_dialect_placeholders(fake: FakeVericto, guard: Vericto) -> None:
    task = _Task("DELETE FROM users WHERE id = %(id)s", parameters={"id": 1})
    check_task_sql(task, guard=guard)
    assert fake.queries() == ["DELETE FROM users WHERE id = $1"]


def test_multi_statement_mask_runs_the_rewritten_list(fake: FakeVericto, guard: Vericto) -> None:
    fake.on(lambda sql: "email" in sql, masked("SELECT '***' AS email FROM users"))
    task = _Task("SELECT 1; SELECT email FROM users")
    check_task_sql(task, guard=guard)
    assert task.sql == ["SELECT 1", "SELECT '***' AS email FROM users"]
    single = _Task("SELECT email FROM users")
    check_task_sql(single, guard=guard)
    assert single.sql == "SELECT '***' AS email FROM users"


def test_blocked_is_a_fail_exception(fake: FakeVericto, guard: Vericto) -> None:
    from vericto import VerictoBlocked

    fake.on(lambda sql: True, blocked())
    with pytest.raises(VerictoTaskBlocked) as info:
        check_task_sql(_Task("DELETE FROM users"), guard=guard)
    assert isinstance(info.value, VerictoBlocked)
    assert any(c.__name__ == "AirflowFailException" for c in type(info.value).__mro__)
    assert info.value.verdict.rule_code == "VERICTO-010"


def test_unavailable_propagates(fake: FakeVericto, guard: Vericto) -> None:
    fake.on(lambda sql: True, {"status": 503, "body": {}})
    with pytest.raises(VerictoUnavailable):
        check_task_sql(_Task("SELECT 1"), guard=guard)


# -- cluster policy -----------------------------------------------------------------


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Install guard_sql_tasks as the cluster task_policy (what airflow_local_settings does)."""
    from airflow import settings

    applied: List[str] = []

    def task_policy(task: Any) -> None:
        if guard_sql_tasks(task):
            applied.append(task.task_id)

    monkeypatch.setattr(settings, "task_policy", task_policy)
    return applied


def test_policy_guards_a_stock_operator(
    vericto_env: FakeVericto, database: Path, dag_file: Any, policy: List[str]
) -> None:
    vericto_env.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-010"))
    dag = dag_file(
        f"""
        SQLExecuteQueryOperator(task_id="stock", conn_id="{CONN_ID}", sql="DELETE FROM users")
        """
    )
    assert policy == ["stock"]
    assert vericto_env.requests == []  # parsing never calls Vericto
    states = _run(dag)
    assert _failed(states, "stock")
    assert vericto_env.queries() == ["DELETE FROM users"]
    assert len(_rows(database)) == 2
    assert type(dag.get_task("stock")) is SQLExecuteQueryOperator  # class unchanged


def test_policy_checks_rendered_sql_and_keeps_the_pre_execute_hook(
    vericto_env: FakeVericto, database: Path, dag_file: Any, policy: List[str], tmp_path: Path
) -> None:
    marker = tmp_path / "hook-ran"
    dag = dag_file(
        f"""
        SQLExecuteQueryOperator(
            task_id="stock",
            conn_id="{CONN_ID}",
            sql="DELETE FROM users WHERE id = {{{{ params.id }}}}",
            params={{"id": 1}},
            pre_execute=lambda context: open({str(marker)!r}, "w").close(),
        )
        """
    )
    states = _run(dag)
    assert not _failed(states, "stock")
    assert marker.exists()
    assert vericto_env.queries() == ["DELETE FROM users WHERE id = 1"]
    assert _rows(database) == [(2, "bo@example.com")]


def test_policy_skips_other_tasks_and_vericto_operators(guard: Vericto) -> None:
    own = VerictoSQLExecuteQueryOperator(task_id="own", conn_id=CONN_ID, sql="SELECT 1", vericto_guard=guard)
    stock = SQLExecuteQueryOperator(task_id="stock", conn_id="other", sql="SELECT 1")
    assert guard_sql_tasks(own) is False
    assert guard_sql_tasks(object()) is False
    assert guard_sql_tasks(stock, when=lambda t: t.conn_id == CONN_ID) is False
    with pytest.raises(VerictoConfigError):
        guard_sql_tasks(stock, on_unsupported="ignore")


@pytest.fixture(autouse=True)
def _reset_shared_guard() -> Iterator[None]:
    import vericto.airflow as module

    yield
    module._shared = None


class _BrokenHook:
    conn_type = "mysql"

    @property
    def connection(self) -> Any:
        raise RuntimeError("no connection configured")


def test_dialect_when_the_connection_cannot_be_read() -> None:
    assert dialect_for_hook(_BrokenHook()) == "mysql"  # falls back to the hook's own conn_type
    assert dialect_for_hook(_Hook("postgres", "not a mapping")) == "postgres"


def test_non_text_sql_is_refused(fake: FakeVericto, guard: Vericto) -> None:
    with pytest.raises(TypeError, match="str or list of str"):
        check_task_sql(_Task(123), guard=guard)
    assert fake.requests == []


def test_list_sql_skips_blank_entries_and_keeps_a_list_when_masked(fake: FakeVericto, guard: Vericto) -> None:
    fake.on(lambda sql: "email" in sql, masked("SELECT '***' AS email FROM users"))
    task = _Task(["SELECT email FROM users", "  ", None])
    verdicts = check_task_sql(task, guard=guard)
    assert len(verdicts) == 1
    assert task.sql == ["SELECT '***' AS email FROM users"]


def test_positional_parameters_and_qmark_hooks(fake: FakeVericto, guard: Vericto) -> None:
    check_task_sql(_Task("DELETE FROM users WHERE id = %s", parameters=(1,)), guard=guard)
    assert fake.queries()[-1] == "DELETE FROM users WHERE id = $1"
    task = _Task("DELETE FROM users WHERE id = ?", "sqlite", parameters=(1,))
    task._hook.placeholder = "?"
    check_task_sql(task, guard=guard)
    assert fake.queries()[-1] == "DELETE FROM users WHERE id = ?"


def test_shared_guard_is_built_once_from_the_environment(fake: FakeVericto, monkeypatch: pytest.MonkeyPatch) -> None:
    from vericto.airflow import shared_guard

    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_DATABASE_ID", DATABASE_ID)
    monkeypatch.setenv("VERICTO_API_URL", fake.url)
    first = shared_guard()
    assert shared_guard() is first
    check_task_sql(_Task("SELECT 1"))
    assert fake.queries() == ["SELECT 1"]


def test_policy_is_idempotent(guard: Vericto) -> None:
    stock = SQLExecuteQueryOperator(task_id="stock", conn_id=CONN_ID, sql="SELECT 1")
    assert guard_sql_tasks(stock, guard=guard) is True
    hook = stock._pre_execute_hook
    assert guard_sql_tasks(stock, guard=guard) is True
    assert stock._pre_execute_hook is hook  # not wrapped twice
