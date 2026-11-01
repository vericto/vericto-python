# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""An OpenAI Agents SDK agent whose SQL tool is checked by Vericto.

pip install "vericto[openai-agents]"
export OPENAI_API_KEY=...  VERICTO_API_KEY=vtro_...  VERICTO_DATABASE_ID=...
python examples/openai_agents_sql_tool.py "Delete the cancelled orders"
"""

from __future__ import annotations

import sqlite3
import sys

from agents import Agent, Runner

from vericto import Vericto
from vericto.openai_agents import vericto_sql_tool

guard = Vericto()  # one per process; VERICTO_* environment (proxy:evaluate key)
conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.executescript(
    """
    CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
    CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT);
    INSERT INTO customers VALUES (1, 'Ana', 'ana@example.com'), (2, 'Bo', 'bo@example.com');
    INSERT INTO orders VALUES (1, 1, 'shipped'), (2, 2, 'cancelled');
    """
)


@vericto_sql_tool(dialect="sqlite", guard=guard)
def run_sql(query: str) -> str:
    """Run one SQLite query against the customers and orders tables and return the rows.

    Args:
        query: The SQL query.
    """
    # Only reached when Vericto allows the query; `query` is then Vericto's approved
    # query (masked columns, a row limit) when it returned one.
    return str(conn.execute(query).fetchall())


agent = Agent(
    name="analyst",
    instructions="Answer questions about customers and orders with the run_sql tool.",
    tools=[run_sql],
)


def main() -> None:
    question = " ".join(sys.argv[1:]) or "Delete all orders"
    # A blocked query comes back to the model as "Error: Vericto blocked this query (...)
    # Suggested fix: ...", so it can rewrite it; nothing blocked reaches the database.
    # If Vericto cannot answer (fail_mode="closed", the default) the run stops.
    print(Runner.run_sync(agent, question).final_output)


if __name__ == "__main__":
    main()
