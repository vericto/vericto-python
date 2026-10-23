# Pydantic AI

**Pattern, no official integration.** vericto has no Pydantic AI module. Register
a SQL tool that checks the query with the guard first; on a block, raise
`ModelRetry` with Vericto's reason and suggested fix, so the model rewrites the
query. Nothing blocked reaches the database.

## Install

```bash
pip install vericto pydantic-ai      # or pydantic-ai-slim plus your model provider
```

Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and `VERICTO_DATABASE_ID`.

## Example

The model is a scripted `FunctionModel` so the example runs without an LLM: it
sends a `DELETE` without `WHERE`, reads the block and retries with a `WHERE`.
Pass your model (`Agent("openai:gpt-4.1", ...)`) instead.

<!-- docs-check: requires=pydantic_ai -->
```python
import asyncio
import sqlite3

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from vericto import Vericto, VerictoBlocked

guard = Vericto()  # one per process
conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
conn.execute("INSERT INTO orders (status) VALUES ('shipped'), ('cancelled')")


def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(parts=[ToolCallPart("run_sql", {"query": "DELETE FROM orders"})])
    if len(messages) == 3:
        return ModelResponse(
            parts=[ToolCallPart("run_sql", {"query": "DELETE FROM orders WHERE status = 'cancelled'"})]
        )
    return ModelResponse(parts=[TextPart("Deleted the cancelled orders.")])


agent = Agent(FunctionModel(scripted), instructions="Answer questions about orders.")


@agent.tool_plain
async def run_sql(query: str) -> str:
    """Run one SQLite query against the orders table and return the rows."""
    try:
        # vericto is synchronous: keep the HTTP check off the event loop.
        sql = await asyncio.to_thread(guard.prepare, query, "sqlite")
    except VerictoBlocked as exc:
        raise ModelRetry(
            f"Blocked by Vericto ({exc.verdict.rule_code}), not executed. Suggested fix: {exc.suggested_fix}"
        ) from exc
    return str(conn.execute(sql).fetchall())


result = agent.run_sync("Delete the cancelled orders")
print(result.output)
assert conn.execute("SELECT status FROM orders").fetchall() == [("shipped",)]
```

## Notes

- `ModelRetry` counts against the tool's retry budget (`retries=` on the agent or
  on the tool, default 1); when the model keeps writing blocked SQL the run fails
  with `UnexpectedModelBehavior`. Return the message as a string instead if you prefer
  the model to give up and explain.
- `VerictoUnavailable` and `VerictoConfigError` are not caught above: they
  propagate out of `run()` / `run_sync()`, so a Vericto outage fails closed.
- `guard.prepare()` returns the masked rewrite when the query reads masked
  columns; run that, never `query`.
- `FLAGGED` and `MONITORED` statements run and are logged on the `vericto` logger;
  `Vericto(on_verdict=...)` receives every verdict.

## Tested with

Python 3.13.12, vericto 0.4.1, pydantic-ai-slim 2.60.0, against the local Runtime
API stand-in, with a scripted `FunctionModel`.
