"""Guard a Snowflake connection with Vericto.

export VERICTO_API_KEY=vtro_...           # scope proxy:evaluate
export VERICTO_DATABASE_ID=<uuid>         # the Snowflake database registered in Vericto
export SNOWFLAKE_ACCOUNT=... SNOWFLAKE_USER=... SNOWFLAKE_PRIVATE_KEY_FILE=...
python examples/snowflake_quickstart.py
"""

from __future__ import annotations

import os

import vericto.snowflake
from vericto import VerictoBlocked

conn = vericto.snowflake.connect(
    account=os.environ["SNOWFLAKE_ACCOUNT"],
    user=os.environ["SNOWFLAKE_USER"],
    authenticator="SNOWFLAKE_JWT",
    private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_FILE"],
    warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
)
cur = conn.cursor()

# Allowed: runs as usual. Parameters stay with the driver; Vericto sees "%s" as "?".
cur.execute("SELECT CURRENT_ACCOUNT(), CURRENT_USER() WHERE 1 = %s", (1,))
print(cur.fetchone())

# Blocked: raises before the statement reaches Snowflake.
try:
    cur.execute("DELETE FROM analytics.public.orders")
except VerictoBlocked as exc:
    print("blocked:", exc.verdict.rule_code, "fix:", exc.suggested_fix)
finally:
    conn.close()
