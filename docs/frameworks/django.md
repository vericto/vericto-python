# Django

**Pattern, no official integration.** vericto has no Django module. Django's own
hook for this, [`connection.execute_wrapper()`](https://docs.djangoproject.com/en/stable/topics/db/instrumentation/),
lets you check every statement with the guard before the database backend runs
it. A few lines, shown below.

## Install

```bash
pip install vericto django
```

Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and `VERICTO_DATABASE_ID` for the
database the connection points at.

## The wrapper

Django writes every query with `%s` placeholders whatever the backend (it turns
them into `?` for SQLite itself), so pass `paramstyle="format"` when the statement
has parameters: Vericto then evaluates `$1` / `?` instead of `%s` and maps a masked
rewrite back to `%s`. Run the SQL the guard returns, never the original.

<!-- docs-check: requires=django -->
```python
import django
from django.conf import settings

settings.configure(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}})
django.setup()

from django.db import connection

from vericto import Vericto, VerictoBlocked

guard = Vericto()  # one per process


def vericto_wrapper(execute, sql, params, many, context):
    dialect = context["connection"].vendor  # "postgresql", "mysql", "sqlite", "oracle"
    paramstyle = "format" if params else None
    return execute(guard.prepare(sql, dialect, paramstyle=paramstyle), params, many, context)


with connection.cursor() as cur:  # setup for the example, unchecked
    cur.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT)")
    cur.execute("INSERT INTO customers (email) VALUES (%s)", ["a@example.com"])

with connection.execute_wrapper(vericto_wrapper), connection.cursor() as cur:
    cur.execute("SELECT id, email FROM customers WHERE id = %s", [1])
    print(cur.fetchall())  # [(1, '***')] when email is masked: the rewrite ran
    try:
        cur.execute("DELETE FROM customers")
    except VerictoBlocked as exc:
        print("blocked:", exc.verdict.rule_code, exc.suggested_fix)

with connection.cursor() as cur:
    cur.execute("SELECT count(*) FROM customers")
    assert cur.fetchone() == (1,)
```

`VerictoBlocked`, `VerictoUnavailable` and `VerictoConfigError` propagate out of
`execute()` unchanged; Django does not wrap them in `DatabaseError`.

## Where to install it

- **For an AI agent or tool only** (recommended): wrap just the code that runs
  model-written SQL, as above, so your application's own ORM queries do not each
  cost an evaluation.
- **For every request**: a middleware, as Django's documentation shows:

<!-- docs-check: skip a fragment: vericto_wrapper is the function above -->
```python
from django.db import connections


def vericto_middleware(get_response):
    def middleware(request):
        with connections["default"].execute_wrapper(vericto_wrapper):
            return get_response(request)

    return middleware
```

The wrapper checks everything that goes through the connection, ORM queries,
`raw()`, `cursor.execute()` and Django's own (sessions, auth, migrations if run
inside it). Management commands and background workers outside the middleware
are not checked unless you wrap them too.

## What each verdict does

| Verdict | `execute()` |
|---|---|
| `ALLOWED` | Runs. |
| `FLAGGED` / `MONITORED` | Runs; logged on the `vericto` logger and passed to `on_verdict`. |
| mask | Runs the masked rewrite, with your parameters. |
| `BLOCKED` | Raises `VerictoBlocked`; the backend is never called. |
| no verdict | `VerictoUnavailable` under `fail_mode="closed"` (default); runs with a `VerictoFailOpenWarning` under `"open"`. |

The check is a synchronous HTTP call, like the database call it precedes. In async
views Django runs ORM calls in a worker thread (`sync_to_async`), so the event
loop is not blocked.

## Tested with

Python 3.13.12, vericto 0.5.0, Django 6.1.2 (SQLite backend; PostgreSQL 17 with psycopg 3.3.6), against the local
Runtime API stand-in.
