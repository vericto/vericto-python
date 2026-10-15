"""Opt-in end-to-end test against a real Snowflake account. Skipped unless configured.

Vericto is the local Runtime API stand-in (tests/conftest.py), so this proves the
wrapper's behaviour with the real connector and a real account: allowed statements
run, blocked ones never reach Snowflake, masks run as rewritten.

    VERICTO_LIVE_SNOWFLAKE=1
    SNOWFLAKE_ACCOUNT=...  SNOWFLAKE_USER=...  SNOWFLAKE_PRIVATE_KEY_FILE=<path to a .p8 key>
    SNOWFLAKE_DATABASE=...  [SNOWFLAKE_WAREHOUSE=...]  [SNOWFLAKE_ROLE=...]
    uv run pytest tests/live -v

Everything happens in a fresh schema WRAPPER_E2E_<timestamp>, dropped at the end.
Never run in CI.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterator

import pytest

import vericto.snowflake
from tests.conftest import FakeVericto, blocked, masked
from vericto import Vericto, VerictoBlocked

REQUIRED = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY_FILE", "SNOWFLAKE_DATABASE")

pytestmark = pytest.mark.skipif(
    os.environ.get("VERICTO_LIVE_SNOWFLAKE") != "1" or any(not os.environ.get(v) for v in REQUIRED),
    reason="live Snowflake test: set VERICTO_LIVE_SNOWFLAKE=1 and " + ", ".join(REQUIRED),
)


@pytest.fixture
def live(fake: FakeVericto, guard: Vericto) -> Iterator[Any]:
    fake.on(lambda sql: sql.upper().startswith("DELETE FROM") and "WHERE" not in sql.upper(), blocked("VERICTO-010"))
    fake.on(lambda sql: sql.upper().startswith("DROP TABLE"), blocked("VERICTO-001"))
    fake.on(lambda sql: sql.startswith("SELECT email FROM e2e_users"), masked("SELECT '***' AS email FROM e2e_users"))
    params: dict[str, Any] = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "authenticator": "SNOWFLAKE_JWT",
        "private_key_file": os.environ["SNOWFLAKE_PRIVATE_KEY_FILE"],
        "database": os.environ["SNOWFLAKE_DATABASE"],
        "login_timeout": 30,
        "network_timeout": 60,
    }
    for opt in ("warehouse", "role"):
        value = os.environ.get(f"SNOWFLAKE_{opt.upper()}")
        if value:
            params[opt] = value
    conn = vericto.snowflake.connect(guard=guard, **params)
    schema = f"WRAPPER_E2E_{int(time.time())}"
    raw = conn.wrapped.cursor()  # setup and teardown bypass the guard on purpose
    raw.execute(f"CREATE SCHEMA {schema}")
    raw.execute(f"USE SCHEMA {schema}")
    try:
        yield conn, raw
    finally:
        raw.execute(f"DROP SCHEMA IF EXISTS {schema}")
        conn.close()


def test_live_snowflake(live: Any, fake: FakeVericto) -> None:
    conn, raw = live
    cur = conn.cursor()
    cur.execute("SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")
    assert cur.fetchone()[1]

    cur.execute("CREATE TABLE e2e_users (id INT, email STRING)")
    cur.execute("INSERT INTO e2e_users VALUES (%s, %s), (%s, %s)", (1, "ana@example.com", 2, "bo@example.com"))
    assert fake.requests[-1]["query"] == "INSERT INTO e2e_users VALUES (?, ?), (?, ?)"

    with pytest.raises(VerictoBlocked):
        cur.execute("DELETE FROM e2e_users")
    with pytest.raises(VerictoBlocked):
        conn.execute_string("INSERT INTO e2e_users VALUES (3, 'c'); DROP TABLE e2e_users;")
    raw.execute("SELECT COUNT(*) FROM e2e_users")
    assert raw.fetchone()[0] == 2  # neither the DELETE nor the script's INSERT ran

    cur.execute("SELECT email FROM e2e_users ORDER BY id")
    assert [r[0] for r in cur.fetchall()] == ["***", "***"]

    cur.execute("DELETE FROM e2e_users WHERE id = %s", (2,))
    raw.execute("SELECT COUNT(*) FROM e2e_users")
    assert raw.fetchone()[0] == 1
