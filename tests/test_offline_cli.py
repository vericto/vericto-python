# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Offline mode against the real ``vericto`` CLI (2.1.0+): integration and parity.

The binary is ``VERICTO_CLI``, else ``vericto`` on PATH. Without one (or with one older
than 2.1.0) every test here is skipped, unless ``VERICTO_REQUIRE_CLI=1`` (set by the
CI job that installs the CLI), which turns the skip into a failure.

Parity: for each case, the verdict of ``Vericto(offline=True, offline_profile="ci")``
and its whole result document equal ``vericto check --offline --format json`` from
the same binary. Under the ``runtime`` profile, the schema DDL rules (VERICTO-010 to
019) block instead of flagging, and nothing else changes.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

import vericto.sqlite
from vericto import Decision, Vericto, VerictoBlocked, VerictoConfigError
from vericto.offline import MIN_CLI_VERSION, parse_version


def _binary() -> Optional[str]:
    path = os.environ.get("VERICTO_CLI", "").strip() or shutil.which("vericto")
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, check=False)  # noqa: S603
    except OSError:
        return None
    version = parse_version(out.stdout)
    return path if version is not None and version >= MIN_CLI_VERSION else None


CLI = _binary()
if CLI is None:
    reason = "no vericto CLI 2.1.0+ found (set VERICTO_CLI or put it on PATH)"
    if os.environ.get("VERICTO_REQUIRE_CLI") == "1":
        raise RuntimeError(f"VERICTO_REQUIRE_CLI=1 but {reason}")
    pytest.skip(reason, allow_module_level=True)
assert CLI is not None

# (id, dialect, sql, is_capped_ddl): 16 inputs over the 6 dialects.
CASES = [
    ("delete-no-where", "postgres", "DELETE FROM users", False),
    ("drop-table", "postgres", "DROP TABLE users", True),
    ("update-no-where", "postgres", "UPDATE accounts SET balance = 0", False),
    ("insert-no-columns", "postgres", "INSERT INTO t VALUES (1)", False),
    ("safe-select", "postgres", "SELECT id FROM users WHERE id = 1 LIMIT 1", False),
    ("parse-error", "postgres", "SELEC broken FROM", False),
    ("capped-ddl-then-block", "postgres", "DROP TABLE a;\nUPDATE accounts SET balance = 0;", True),
    ("constant-predicate", "postgres", "DELETE FROM users WHERE NULL IS NULL", False),
    ("tautology-or", "postgres", "SELECT id FROM users WHERE status = 'active' OR NULL IS NULL", False),
    ("mysql-backticks", "mysql", "DELETE FROM `users`", False),
    ("mysql-in-null", "mysql", "UPDATE users SET active = false WHERE 1 IN (1, NULL)", False),
    ("mssql-select-into", "mssql", "SELECT * INTO #users_copy FROM users", False),
    ("snowflake-insert-select-star", "snowflake", "INSERT INTO t (SELECT * FROM s)", False),
    ("oracle-truncate", "oracle", "TRUNCATE TABLE orders", True),
    ("sqlite-alter-drop-column", "sqlite", "ALTER TABLE users DROP COLUMN email", True),
    ("postgres-pg-sleep", "postgres", "SELECT pg_sleep(10)", False),
]


def cli_check(sql: str, dialect: str, cwd: Path) -> Dict[str, Any]:
    """``vericto check --offline --format json`` for one inline input."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VERICTO_")}
    proc = subprocess.run(  # noqa: S603 - the CLI under test
        [str(CLI), "check", "--offline", "--format", "json", "--dialect", dialect, "--sql", sql],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=cwd,
        env=env,
    )
    assert proc.returncode in (0, 1), proc.stderr
    doc = json.loads(proc.stdout[proc.stdout.index("{") :])
    assert isinstance(doc, dict)
    return doc


@pytest.fixture(scope="module")
def ci_guard() -> Iterator[Vericto]:
    with Vericto(offline=True, offline_profile="ci", offline_cli_path=CLI) as guard:
        yield guard


@pytest.fixture(scope="module")
def runtime_guard() -> Iterator[Vericto]:
    with Vericto(offline=True, offline_cli_path=CLI) as guard:
        yield guard


@pytest.mark.parametrize(("case", "dialect", "sql", "capped"), CASES, ids=[c[0] for c in CASES])
def test_parity_with_check_offline(
    ci_guard: Vericto, runtime_guard: Vericto, tmp_path: Path, case: str, dialect: str, sql: str, capped: bool
) -> None:
    expected = cli_check(sql, dialect, tmp_path)
    (query,) = expected["queries"]

    ci = ci_guard.check(sql, dialect)
    assert ci.raw["offline_result"] == expected  # the whole document, as the CLI prints it
    assert ci.decision.value == query["status"]
    assert ci.rule_code == query["rule_code"]
    assert ci.severity == query["severity"]
    assert ci.suggested_fix == query["suggested_fix"]
    assert (ci.mode, ci.engine_version) == ("offline", expected["engine_version"])
    assert ci.ruleset_version == expected["summary"]["ruleset_version"] == f"standard@engine-{ci.engine_version}"

    runtime = runtime_guard.check(sql, dialect)
    if capped:
        assert runtime.decision is Decision.BLOCKED
        assert runtime.rule_code is not None
        assert "VERICTO-010" <= runtime.rule_code <= "VERICTO-019"
    else:
        assert (runtime.decision, runtime.rule_code) == (ci.decision, ci.rule_code)


def test_runtime_profile_blocks_drop_table(runtime_guard: Vericto) -> None:
    with pytest.raises(VerictoBlocked) as info:
        runtime_guard.enforce("DROP TABLE users", "postgres")
    assert info.value.verdict.rule_code == "VERICTO-010"
    assert info.value.verdict.mode == "offline"


def test_engine_errors_map_to_sdk_errors(runtime_guard: Vericto) -> None:
    with pytest.raises(VerictoConfigError) as info:
        runtime_guard.check("  ", "postgres")
    assert info.value.code == "INVALID_REQUEST"
    with pytest.raises(VerictoConfigError) as info:
        runtime_guard.check("SELECT '" + "x" * (2 << 20) + "'", "postgres")
    assert info.value.code == "TOO_LARGE"
    assert runtime_guard.check("SELECT id FROM t WHERE id = 1 LIMIT 1", "postgres").decision is Decision.ALLOWED


def test_eight_threads(runtime_guard: Vericto) -> None:
    errors: List[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                sql = f"DELETE FROM t_{n}_{i}" if i % 2 else f"SELECT id FROM t_{n} WHERE id = {i} LIMIT 1"
                v = runtime_guard.check(sql, "postgres")
                assert v.raw["offline_result"]["queries"][0]["sql_preview"] == sql
                assert v.blocked == bool(i % 2)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors


def test_sqlite_wrapper_with_the_real_engine(runtime_guard: Vericto, tmp_path: Path) -> None:
    conn = vericto.sqlite.connect(str(tmp_path / "app.db"), guard=runtime_guard)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.execute("INSERT INTO users (id, email) VALUES (?, ?)", (1, "a@example.com"))
    with pytest.raises(VerictoBlocked):
        conn.execute("DELETE FROM users")
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id = 1 LIMIT 1").fetchone() == (1,)
    conn.commit()
    conn.close()
    assert sqlite3.connect(str(tmp_path / "app.db")).execute("SELECT COUNT(*) FROM users").fetchone() == (1,)
