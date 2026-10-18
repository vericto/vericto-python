# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in end-to-end test against a real Snowflake account. Skipped unless configured.

Vericto is the local Runtime API stand-in (tests/conftest.py), so this proves the
wrapper's behaviour with the real connector and a real account: allowed statements
run, blocked ones never reach Snowflake (checked with an unguarded cursor and with
Snowflake's own query history), masks run as rewritten, and the fail modes hold
when Vericto is down.

    VERICTO_LIVE_SNOWFLAKE=1
    SNOWFLAKE_ACCOUNT=...  SNOWFLAKE_USER=...  SNOWFLAKE_PRIVATE_KEY_FILE=<path to a .p8 key>
    SNOWFLAKE_DATABASE=...  [SNOWFLAKE_WAREHOUSE=...]  [SNOWFLAKE_ROLE=...]
    uv run pytest tests/live -v

Everything happens in a fresh schema WRAPPER_E2E_<timestamp>, dropped after each test.
Never run in CI.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any, Iterator, List

import pytest

import vericto.snowflake
from tests.conftest import API_KEY, DATABASE_ID, FakeVericto, blocked, masked
from vericto import RuntimeApiEvaluator, Vericto, VerictoBlocked, VerictoUnavailable
from vericto.errors import VerictoFailOpenWarning

REQUIRED = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY_FILE", "SNOWFLAKE_DATABASE")
SCHEMA = f"WRAPPER_E2E_{int(time.time())}"

pytestmark = pytest.mark.skipif(
    os.environ.get("VERICTO_LIVE_SNOWFLAKE") != "1" or any(not os.environ.get(v) for v in REQUIRED),
    reason="live Snowflake test: set VERICTO_LIVE_SNOWFLAKE=1 and " + ", ".join(REQUIRED),
)


def _params(**extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "authenticator": "SNOWFLAKE_JWT",
        "private_key_file": os.environ["SNOWFLAKE_PRIVATE_KEY_FILE"],
        "database": os.environ["SNOWFLAKE_DATABASE"],
        "schema": SCHEMA,
        "login_timeout": 30,
        "network_timeout": 60,
        **extra,
    }
    for opt in ("warehouse", "role"):
        value = os.environ.get(f"SNOWFLAKE_{opt.upper()}")
        if value:
            params[opt] = value
    return params


def _count(raw: Any) -> int:
    raw.execute("SELECT COUNT(*) FROM e2e_users")
    return int(raw.fetchone()[0])


def _dead_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"  # nothing listens there any more


def _down_guard(fail_mode: str) -> Vericto:
    evaluator = RuntimeApiEvaluator(
        API_KEY, database_id=DATABASE_ID, api_url=_dead_url(), timeout=1, max_retries=0, backoff=0.0
    )
    return Vericto(evaluator, fail_mode=fail_mode)


@pytest.fixture
def live(fake: FakeVericto, guard: Vericto) -> Iterator[Any]:
    fake.on(lambda sql: sql.upper().startswith("DELETE FROM") and "WHERE" not in sql.upper(), blocked("VERICTO-010"))
    fake.on(lambda sql: sql.upper().startswith("DROP TABLE"), blocked("VERICTO-001"))
    fake.on(lambda sql: sql.startswith("SELECT email FROM e2e_users"), masked("SELECT '***' AS email FROM e2e_users"))
    conn = vericto.snowflake.connect(guard=guard, **_params())
    raw = conn.wrapped.cursor()  # setup and teardown bypass the guard on purpose
    raw.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    raw.execute(f"USE SCHEMA {SCHEMA}")
    raw.execute("CREATE TABLE e2e_users (id INT, email STRING)")
    raw.execute("INSERT INTO e2e_users VALUES (1, 'ana@example.com'), (2, 'bo@example.com')")
    try:
        yield conn, raw
    finally:
        try:
            raw.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
        finally:
            conn.close()


def test_allowed_statements_run(live: Any, fake: FakeVericto) -> None:
    conn, raw = live
    cur = conn.cursor()
    cur.execute("SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")
    assert cur.fetchone()[1]

    cur.execute("CREATE TABLE e2e_orders (id INT, total NUMBER(10, 2))")
    cur.execute("INSERT INTO e2e_orders VALUES (%s, %s), (%s, %s)", (1, 9.5, 2, 20))
    assert fake.requests[-1]["query"] == "INSERT INTO e2e_orders VALUES (?, ?), (?, ?)"
    cur.execute("INSERT INTO e2e_orders VALUES (%(id)s, %(total)s)", {"id": 3, "total": 1})
    assert fake.requests[-1]["query"] == "INSERT INTO e2e_orders VALUES (?, ?)"
    cur.executemany("INSERT INTO e2e_orders VALUES (%s, %s)", [(4, 4), (5, 5)])

    cur.execute("SELECT id FROM e2e_orders WHERE total > %s ORDER BY id", (4,))
    assert [r[0] for r in cur.fetchall()] == [1, 2, 5]
    assert fake.requests[-1]["query"] == "SELECT id FROM e2e_orders WHERE total > ? ORDER BY id"
    assert "20" not in str(fake.requests)  # bind values never go to Vericto

    cur.execute("DELETE FROM e2e_users WHERE id = %s", (2,))
    assert _count(raw) == 1


def test_qmark_and_numeric_binds(live: Any, fake: FakeVericto, guard: Vericto) -> None:
    _, raw = live
    with vericto.snowflake.connect(guard=guard, paramstyle="qmark", **_params()) as conn:
        assert conn.paramstyle == "qmark"
        cur = conn.cursor()
        cur.execute("INSERT INTO e2e_users VALUES (?, ?)", (3, "cy@example.com"))
        assert fake.requests[-1]["query"] == "INSERT INTO e2e_users VALUES (?, ?)"
        cur.execute("INSERT INTO e2e_users VALUES (:1, :2)", (4, "di@example.com"))
        assert fake.requests[-1]["query"] == "INSERT INTO e2e_users VALUES (:1, :2)"
        cur.execute("SELECT id, email FROM e2e_users WHERE id >= :1 ORDER BY id", (3,))
        assert [r[1] for r in cur.fetchall()] == ["cy@example.com", "di@example.com"]
        with pytest.raises(VerictoBlocked):
            cur.execute("DELETE FROM e2e_users")
    assert _count(raw) == 4


def test_blocked_statements_never_reach_snowflake(live: Any, fake: FakeVericto) -> None:
    conn, raw = live
    cur = conn.cursor()
    with pytest.raises(VerictoBlocked) as exc:
        cur.execute("DELETE FROM e2e_users")
    assert exc.value.verdict.rule_code == "VERICTO-010"
    with pytest.raises(VerictoBlocked) as exc:
        cur.execute("DROP TABLE e2e_users")
    assert exc.value.verdict.rule_code == "VERICTO-001"
    assert _count(raw) == 2

    # A block in the middle of a script: none of the statements runs.
    script = "INSERT INTO e2e_users VALUES (3, 'c'); DROP TABLE e2e_users; INSERT INTO e2e_users VALUES (4, 'd');"
    with pytest.raises(VerictoBlocked):
        conn.execute_string(script)
    # Evaluation stops at the first block; nothing was sent to Snowflake yet.
    assert fake.queries()[-2:] == ["INSERT INTO e2e_users VALUES (3, 'c');", "DROP TABLE e2e_users;"]
    assert _count(raw) == 2
    raw.execute("SHOW TABLES LIKE 'E2E_USERS'")
    assert len(raw.fetchall()) == 1

    # An allowed script runs whole.
    conn.execute_string("INSERT INTO e2e_users VALUES (5, 'e'); INSERT INTO e2e_users VALUES (6, 'f');")
    assert _count(raw) == 4

    # Snowflake's own record of this session: the blocked statements never arrived.
    raw.execute("SELECT query_text FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(RESULT_LIMIT => 1000))")
    history: List[str] = [r[0] for r in raw.fetchall()]
    assert any("(5, 'e')" in q for q in history)  # the history is visible at all
    assert not any("DROP TABLE" in q.upper() for q in history)
    assert not any(q.strip().upper() == "DELETE FROM E2E_USERS" for q in history)
    assert not any("(3, 'c')" in q or "(4, 'd')" in q for q in history)


def test_masked_rewrite_runs(live: Any, fake: FakeVericto) -> None:
    conn, _ = live
    cur = conn.cursor()
    cur.execute("SELECT email FROM e2e_users ORDER BY id")
    assert [r[0] for r in cur.fetchall()] == ["***", "***"]
    assert cur.query == "SELECT '***' AS email FROM e2e_users"  # what Snowflake ran


def test_fail_closed_when_vericto_is_down(live: Any) -> None:
    conn, raw = live
    down = vericto.snowflake.wrap(conn.wrapped, guard=_down_guard("closed"))
    with pytest.raises(VerictoUnavailable):
        down.cursor().execute("INSERT INTO e2e_users VALUES (7, 'g')")
    with pytest.raises(VerictoUnavailable):
        down.execute_string("INSERT INTO e2e_users VALUES (8, 'h'); SELECT 1;")
    assert _count(raw) == 2


def test_fail_open_runs_with_a_warning(live: Any) -> None:
    conn, raw = live
    down = vericto.snowflake.wrap(conn.wrapped, guard=_down_guard("open"))
    with pytest.warns(VerictoFailOpenWarning):
        down.cursor().execute("INSERT INTO e2e_users VALUES (%s, %s)", (7, "g"))
    assert _count(raw) == 3
