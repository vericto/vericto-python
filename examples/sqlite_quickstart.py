# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Guard the standard library sqlite3 module with Vericto.

The Vericto server evaluates the ``sqlite`` dialect. Against a server that does not
list it, ``connect`` raises VerictoUnsupportedDialect and no SQL runs.
"""

from __future__ import annotations

import vericto.sqlite
from vericto import VerictoBlocked, VerictoUnsupportedDialect

try:
    conn = vericto.sqlite.connect("app.db")
except VerictoUnsupportedDialect as exc:
    raise SystemExit(str(exc)) from None

conn.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, expires_at INTEGER)")
conn.execute("DELETE FROM sessions WHERE expires_at < ?", (1_700_000_000,))
try:
    conn.execute("DELETE FROM sessions")  # no WHERE: blocked by the workspace rules
except VerictoBlocked as exc:
    print("blocked:", exc)
conn.close()
