# SQLAlchemy

**Official integration:** `vericto.sqlalchemy.instrument(engine)` (extra
`vericto[sqlalchemy]`), SQLAlchemy 1.4 and 2.x, any dialect Vericto evaluates.

`instrument()` adds a `before_cursor_execute` listener to the engine: every
statement SQLAlchemy sends to the driver (ORM, Core and `text()`) is checked
**after compilation**, so Vericto sees the SQL that actually runs. It returns a
function that removes the listener.

## Install

```bash
pip install "vericto[sqlalchemy]"
```

## Example

```python
from sqlalchemy import create_engine, text

import vericto.sqlalchemy
from vericto import Vericto, VerictoBlocked

engine = create_engine("sqlite://")  # or postgresql+psycopg://, mysql+pymysql://, snowflake://, oracle://, mssql://
remove = vericto.sqlalchemy.instrument(engine, guard=Vericto())

with engine.begin() as conn:
    conn.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT)"))
    conn.execute(text("INSERT INTO customers (email) VALUES (:email)"), [{"email": "a@example.com"}])
    print(conn.execute(text("SELECT id, email FROM customers WHERE id = :id"), {"id": 1}).all())  # masked rewrite

try:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM customers"))  # blocked before the driver is called
except VerictoBlocked as exc:  # propagates unwrapped, not as a SQLAlchemy DBAPIError
    print(exc.verdict.rule_code, exc.suggested_fix)

assert vericto.sqlalchemy.is_instrumented(engine)
remove()  # stop checking
```

The dialect comes from `engine.dialect.name`: `postgresql` → `postgres`,
`mysql`/`mariadb` → `mysql`, `snowflake`, `oracle`, `mssql`, `sqlite`; pass
`dialect=` to override. `instrument()` asks the server which dialects it evaluates
and raises `VerictoUnsupportedDialect` before anything runs if it does not list
this one.

## Async engines

`instrument()` accepts an `AsyncEngine` (it instruments its `sync_engine`). The
check itself is a **synchronous** HTTP call made inside SQLAlchemy's execution, so
while Vericto answers (one round trip, up to `VERICTO_TIMEOUT` per attempt) the
event loop is blocked. That is usually acceptable for a few milliseconds per
statement; for latency-sensitive services keep the timeout low, or run the
queries that need checking through a sync engine in a worker thread
(`await asyncio.to_thread(...)`).

## What each verdict does

| Verdict | Statement |
|---|---|
| `ALLOWED` | Runs. |
| `FLAGGED` / `MONITORED` | Runs; logged and passed to `on_verdict`. |
| mask | The compiled SQL is replaced by the masked rewrite; your bind parameters still apply. |
| `BLOCKED` | `VerictoBlocked` is raised before the driver is called (the transaction rolls back as for any error). |
| no verdict | `VerictoUnavailable` (fail closed), or runs with a warning under `fail_mode="open"`. |

Parameters are bound by the driver; with `format`/`pyformat` drivers (psycopg,
PyMySQL) the `%s` markers are evaluated as the dialect's placeholders. Schema
reflection and DDL your code runs through the engine are checked like any other
statement.

## Tested with

Python 3.13.12, vericto 0.4.1, SQLAlchemy 2.1.6 (SQLite; PostgreSQL 17 with
psycopg 3.3.6), against the local Runtime API stand-in.
