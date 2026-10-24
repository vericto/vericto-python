# Any DB-API driver (PEP 249)

**Official integration:** `vericto.wrap()` (core package, no extra), plus
`vericto.sqlite.connect()` for the standard library `sqlite3` and
`vericto.snowflake.connect()` / `wrap()` for `snowflake-connector-python`.

`vericto.wrap(connection, dialect=...)` returns a connection whose cursors check
every `execute()` and `executemany()` with Vericto before the driver runs them.
Everything else (`fetchone`, `description`, `commit`, ...) is the driver's.

## Install

```bash
pip install vericto                 # sqlite3 and any DB-API driver
pip install "vericto[snowflake]"    # + snowflake-connector-python
```

Set `VERICTO_API_KEY` (a `vtro_...` key with the `proxy:evaluate` scope) and
`VERICTO_DATABASE_ID`; see the [README](../../README.md#configure).

## Example

```python
import sqlite3

import vericto
from vericto import Vericto, VerictoBlocked

guard = Vericto()  # one per process; reads VERICTO_* from the environment
conn = vericto.wrap(sqlite3.connect(":memory:"), dialect="sqlite", guard=guard)

cur = conn.cursor()
cur.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT)")
cur.executemany("INSERT INTO customers (email) VALUES (?)", [("a@example.com",), ("b@example.com",)])

# A statement that reads a masked column runs as Vericto's rewrite.
cur.execute("SELECT id, email FROM customers WHERE id = ?", (1,))
print(cur.fetchall())  # [(1, '***')] when `email` is masked in your workspace

try:
    cur.execute("DELETE FROM customers")  # no WHERE: blocked, never reaches SQLite
except VerictoBlocked as exc:
    print(exc.verdict.rule_code, exc.suggested_fix)

cur.execute("SELECT count(*) FROM customers")
assert cur.fetchone() == (2,)
```

For `sqlite3` specifically, `vericto.sqlite.connect("app.db")` returns a real
`sqlite3.Connection` subclass (so `isinstance` checks and pandas keep working) and
also checks `executescript`, evaluating every statement of the script before any
of it runs.

## Parameters and `%s` placeholders

Values are never sent to Vericto: only the SQL text, placeholders included.
`format`/`pyformat` drivers (psycopg, PyMySQL, snowflake-connector-python by
default) write `%s` / `%(name)s`, which is not SQL. When such a statement runs
**with** parameters, Vericto evaluates it with the dialect's own placeholders
(`$1` on PostgreSQL, `?` elsewhere). The driver's paramstyle is read from its
module; pass `paramstyle=` to `wrap()` when it cannot be (a driver that changes it
per connection).

<!-- docs-check: skip needs a PostgreSQL server (verified against postgres:17 in Docker with psycopg 3.3.6) -->
```python
import os

import psycopg

import vericto

conn = vericto.wrap(psycopg.connect(os.environ["DATABASE_URL"]), dialect="postgres")
with conn.cursor() as cur:
    cur.execute("SELECT id, email FROM customers WHERE id = %s", (42,))  # evaluated as ... id = $1
    print(cur.fetchall())
```

## What each verdict does

| Verdict | `execute()` |
|---|---|
| `ALLOWED` | Runs the statement. |
| `FLAGGED` / `MONITORED` | Runs it; logged on the `vericto` logger and passed to `on_verdict`. |
| mask (`rewritten_query`) | Runs the rewrite instead of your SQL. |
| `BLOCKED` | Raises `VerictoBlocked`; the driver is never called. |
| no verdict | `fail_mode="closed"` (default): raises `VerictoUnavailable`. `"open"`: runs unevaluated with a `VerictoFailOpenWarning`. |

`executemany()` is one evaluation for all parameter sets. `cursor.wrapped` and
`conn.wrapped` are the unguarded driver objects: SQL run through them is not
checked.

## Tested with

Python 3.13.12, vericto 0.5.0, `sqlite3` (SQLite 3.50), psycopg 3.3.6 against
PostgreSQL 17, against the local Runtime API stand-in
(`scripts/docs_stub.py`).
