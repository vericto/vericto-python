# Vanna

**Pattern, no official integration.** vericto has no Vanna module. In Vanna 2.x
the `RunSqlTool` runs SQL through a `SqlRunner`; wrap the runner you already use
(`SqliteRunner`, `PostgresRunner`, `SnowflakeRunner`...) in one that checks each
query with the guard first and passes on only what may run. This guide covers
Vanna 2.x; the legacy 0.x API (`vanna.legacy`) is not covered.

## Install

```bash
pip install vericto vanna
```

Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and `VERICTO_DATABASE_ID`.

## Example

The tool is called directly through the `ToolRegistry`, as the agent would call
it, so the example runs without an LLM. Register the same tool on your `Agent`.

<!-- docs-check: requires=vanna -->
```python
import asyncio
import sqlite3

import pandas as pd
from vanna import ToolContext, ToolRegistry, User
from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.core.tool import ToolCall
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.sqlite import SqliteRunner
from vanna.tools import RunSqlTool

from vericto import Vericto


class VerictoSqlRunner(SqlRunner):
    """Checks each query with Vericto, then hands what may run to the wrapped runner."""

    def __init__(self, runner: SqlRunner, dialect: str, guard: Vericto) -> None:
        self.runner, self.dialect, self.guard = runner, dialect, guard

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        # vericto is synchronous: keep the HTTP check off the event loop.
        sql = await asyncio.to_thread(self.guard.prepare, args.sql, self.dialect)  # raises VerictoBlocked
        return await self.runner.run_sql(RunSqlToolArgs(sql=sql), context)  # the masked rewrite, if any


with sqlite3.connect("app.db") as setup:
    setup.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);"
        "INSERT INTO orders (status) VALUES ('shipped'), ('cancelled');"
    )

tools = ToolRegistry()
runner = VerictoSqlRunner(SqliteRunner("app.db"), "sqlite", Vericto())
tools.register_local_tool(RunSqlTool(sql_runner=runner), access_groups=[])


async def main() -> None:
    context = ToolContext(user=User(id="u1"), conversation_id="c1", request_id="r1", agent_memory=DemoAgentMemory())
    result = await tools.execute(ToolCall(id="t1", name="run_sql", arguments={"sql": "DELETE FROM orders"}), context)
    print(result.result_for_llm)  # "Error executing query: Query blocked by VERICTO-001 ..."
    assert not result.success and "VERICTO-001" in result.result_for_llm


asyncio.run(main())
assert sqlite3.connect("app.db").execute("SELECT count(*) FROM orders").fetchone() == (2,)
```

## What each verdict does

| Verdict | `run_sql` |
|---|---|
| `ALLOWED` | Runs on the wrapped runner. |
| `FLAGGED` / `MONITORED` | Runs; logged on the `vericto` logger and passed to `on_verdict`. |
| mask | The masked rewrite runs instead of the model's SQL. |
| `BLOCKED` | `VerictoBlocked` is raised before the runner is called. `RunSqlTool` turns it into a failed `ToolResult` whose text (rule and suggested fix) the LLM reads. |
| no verdict | `VerictoUnavailable` (fail closed): the query does not run. |

`RunSqlTool` (and the `ToolRegistry` around it) turn **every** exception into a
failed `ToolResult` for the LLM, `VerictoUnavailable` and `VerictoConfigError`
included: the query still does not run, but the conversation carries on. To
notice an outage, watch the `vericto` logger (a missing verdict is logged at
`ERROR`) or handle those exceptions in `VerictoSqlRunner.run_sql` yourself.

## Tested with

Python 3.13.12, vericto 0.5.0, vanna 2.0.2 (`SqliteRunner`), against the local
Runtime API stand-in, without an LLM.
