"""Guard any DB-API driver, or call the guard yourself."""

from __future__ import annotations

import psycopg  # type: ignore[import-not-found]

import vericto
from vericto import Vericto

guard = Vericto()  # VERICTO_API_KEY, VERICTO_DATABASE_ID, VERICTO_FAIL_MODE...

# 1. Wrap the connection: every cursor.execute() is checked.
conn = vericto.wrap(psycopg.connect("dbname=app"), dialect="postgres", guard=guard)
conn.cursor().execute("SELECT * FROM orders WHERE id = %s", (1,))

# 2. Or ask first and run the SQL Vericto returns (the masked rewrite, if any).
verdict = guard.enforce("SELECT email FROM customers LIMIT 10", "postgres")
rows = conn.cursor().wrapped.execute(verdict.sql_to_execute).fetchall()
