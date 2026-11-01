# Pydantic AI

**Official integration: `vericto.pydantic_ai`** (since vericto 0.7.0). Database
guardrails for agents built with Pydantic AI: Vericto checks the SQL a tool call
carries before the tool runs it.

`VerictoToolset` is a Pydantic AI
[`WrapperToolset`](https://pydantic.dev/docs/ai/tools-toolsets/toolsets/#changing-tool-execution). It
wraps any toolset (a `FunctionToolset` of SQL tools you write, an `MCPToolset`
of an MCP server's tools, a combined or prefixed toolset) and overrides
`call_tool`, so Vericto checks every call to a SQL tool before it runs. The tools
keep their names, schemas and descriptions.

| Verdict | What happens |
|---|---|
| `BLOCKED` | The tool is not called. By default (`on_block="retry"`) the call raises `ModelRetry` with the refusal (rule code, reason, `suggested_fix`): the model reads it and can correct its query. `on_block="return"` makes the refusal the tool result instead, and `on_block="raise"` stops the run with `VerictoBlocked`. |
| `approved_query` (masked columns, a `LIMIT` bound) | The tool is called with **that** query, never the original. |
| `FLAGGED`, `MONITORED` | Runs. The verdict is logged on the `vericto` logger and passed to `Vericto(on_verdict=...)`. |
| Vericto unavailable, `fail_mode="closed"` (default) | The run stops: `run()` / `run_sync()` raise `VerictoUnavailable`. The model never sees it as text. |
| Vericto unavailable, `fail_mode="open"` | Runs unevaluated, with a `VerictoFailOpenWarning`. |
| Configuration error (key, scope, dialect) | The run stops with `VerictoConfigError`, in both fail modes. |

The check calls `Vericto.enforce()`, so the guard's fail mode, verdict cache,
placeholder mapping and `on_verdict` work as they do in every other wrapper. The
guard is synchronous: the check runs in a worker thread (`asyncio.to_thread`) and
the event loop keeps running.

## Install

```bash
pip install "vericto[pydantic-ai]"     # pydantic-ai-slim 2.x; add your model provider's extra
```

`pip install "vericto" pydantic-ai` works too: `pydantic-ai` includes
`pydantic-ai-slim`. Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and
`VERICTO_DATABASE_ID`. Each checked tool call is one Runtime API evaluation.

## SQL tools you write

Put the SQL tools in a `FunctionToolset` and give the agent the wrapped toolset.
The model below is a scripted `FunctionModel`, so the example runs without an
LLM: it sends a `DELETE` without `WHERE`, reads the refusal and retries with a
`WHERE`, then reads a masked column. Pass your model (`Agent("openai:gpt-5",
...)`) instead.

<!-- docs-check: requires=pydantic_ai -->
```python
import sqlite3

from pydantic_ai import Agent, FunctionToolset
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from vericto import Vericto
from vericto.pydantic_ai import VerictoToolset

guard = Vericto()  # one per process
conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT, status TEXT)")
conn.execute(
    "INSERT INTO customers (name, email, status) VALUES "
    "('Ana', 'ana@example.com', 'active'), ('Bo', 'bo@example.com', 'closed')"
)
ran = []

sql_tools = FunctionToolset()


@sql_tools.tool_plain
def run_sql(query: str) -> str:
    """Run one SQLite query against the customers table and return the rows."""
    ran.append(query)  # only allowed queries get here, as Vericto approved them
    return str(conn.execute(query).fetchall())


script = [
    "DELETE FROM customers",
    "DELETE FROM customers WHERE status = 'closed'",
    "SELECT name, email FROM customers",
]
seen = []


def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    seen.append(messages[-1])
    if script:
        return ModelResponse(parts=[ToolCallPart("run_sql", {"query": script.pop(0)})])
    return ModelResponse(parts=[TextPart("Deleted the closed customers.")])


agent = Agent(
    FunctionModel(scripted),
    instructions="Answer questions about customers.",
    toolsets=[VerictoToolset(sql_tools, dialect="sqlite", guard=guard)],
)
result = agent.run_sync("Delete the closed customers, then list the rest")
print(result.output)

refusal = str(seen[1].parts[0].content)  # what the model read after the DELETE
assert refusal.startswith("Error: Vericto blocked this query (VERICTO-001")
assert "Suggested fix:" in refusal
assert ran == ["DELETE FROM customers WHERE status = 'closed'", "SELECT name, '***' AS email FROM customers"]
assert conn.execute("SELECT count(*) FROM customers").fetchone() == (1,)
```

What the model reads for the blocked call (with a `Reason:` line when the
server adds one, and `Other findings:` / `Not allowed for this agent:` when
they apply):

```text
Error: Vericto blocked this query (VERICTO-001, severity critical). It was NOT executed.
Suggested fix: DELETE FROM customers WHERE <condition>
Rewrite the query following the suggested fix and try again.
(Vericto event ...)
```

`ModelRetry` counts against the tool's retry budget (`retries=` on the agent, the
toolset or the tool; 1 by default). When the model keeps writing blocked SQL,
the run fails with `UnexpectedModelBehavior` and nothing blocked ever runs. Use
`on_block="return"` to give the model the refusal as an ordinary tool result,
with no retry budget spent, so it can explain to the user instead.

Tools that take `RunContext` (`@sql_tools.tool`), `async def` tools and tools
with more parameters work the same way: only the SQL argument is checked and,
for an approved query, replaced.

## An MCP server's tools

`MCPToolset` is a toolset too. Wrap it, and name the SQL tools with `tools=`
(the others, such as `list_tables`, then run unchecked):

<!-- docs-check: skip a fragment (needs an MCP server) -->
```python
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset

from vericto.pydantic_ai import VerictoToolset

postgres = MCPToolset("http://localhost:8000/mcp")  # pydantic-ai-slim[mcp]
agent = Agent(
    "openai:gpt-5",
    toolsets=[VerictoToolset(postgres, dialect="postgres", argument="sql", tools=["execute_sql"])],
)
```

## Options

`VerictoToolset(toolset, dialect=..., ...)`:

- `dialect` (required): the dialect of the database the tools run SQL against
  (`"postgres"`, `"snowflake"`, `"mysql"`, `"mssql"`, `"oracle"`, `"sqlite"`).
- `guard`: a `Vericto`. When omitted, it is built from the `VERICTO_*` variables.
- `argument="query"`: the name of the tools' SQL parameter.
- `tools`: the names of the tools to check; `None` (default) checks every tool of
  the wrapped toolset. Every tool in scope must have the `argument` parameter in
  its schema: if one does not, the run stops with `VerictoConfigError` before any
  tool runs, so a SQL tool never goes unchecked because of a parameter name. A
  call whose SQL argument is not a string is refused without running.
- `on_block="retry"` (default), `"return"` or `"raise"`.
- `context=` (`{"ticket": ..., "tenant": ...}`) and `end_user=`: a value, or a
  callable read on each call (for example a `contextvars.ContextVar`'s `.get`).
  They take effect when business policies are enabled in your workspace.
- `paramstyle=`: when the tools execute the query with bound parameters (`%s`
  markers), as in `Vericto.enforce()`.

Errors raised by the tools themselves are not Vericto's: Pydantic AI handles them
as usual (a `ModelRetry` from your tool still asks the model to retry).

## Tested with

Python 3.13, vericto 0.7.0, pydantic-ai-slim 2.64.0 (and 2.0.0, the lowest
version the extra allows). The example above runs against the local Runtime API
stand-in with a scripted `FunctionModel` (no model calls), and
`tests/test_pydantic_ai.py` runs against the real `Agent` in CI, including an
`MCPToolset` over stdio.
