# OpenAI Agents SDK

**Official integration: `vericto.openai_agents`** (since vericto 0.6.0). Database
guardrails for agents built with the OpenAI Agents SDK (`openai-agents`): Vericto
checks the SQL a tool call carries before the tool runs it.

- `vericto_sql_tool`: `function_tool` for a SQL tool you write. Vericto checks
  the SQL argument before your function runs, and runs Vericto's approved query
  instead of the original when there is one.
- `vericto_tool_input_guardrail`: a native SDK `ToolInputGuardrail` for SQL tools
  you do not write (an MCP server's query tool, a third-party `FunctionTool`).

| Verdict | `vericto_sql_tool` | `vericto_tool_input_guardrail` |
|---|---|---|
| `BLOCKED` | Your function is not called. The model gets a refusal with the rule code and the `suggested_fix` as the tool output. `on_block="raise"` stops the run. | The call is rejected with the same refusal. `on_block="raise"` raises `ToolInputGuardrailTripwireTriggered`. |
| `approved_query` (masked columns, a `LIMIT` bound) | Your function runs **that** query, never the original. | The call is rejected with the approved query and the instruction to call the tool again with it: a guardrail cannot change the arguments. The second call is checked too. |
| `FLAGGED`, `MONITORED` | Runs. The verdict is logged on the `vericto` logger and passed to `Vericto(on_verdict=...)`. | Allowed. Also logged and passed to `on_verdict`, and the `Verdict` is the guardrail's `output_info` in `result.tool_input_guardrail_results`. |
| Vericto unavailable, `fail_mode="closed"` (default) | The run stops: `agents.exceptions.UserError` whose `__cause__` is `VerictoUnavailable`. The model never sees it as text. | Same. |
| Vericto unavailable, `fail_mode="open"` | Runs unevaluated, with a `VerictoFailOpenWarning`. | Allowed unevaluated, with the same warning. |
| Configuration error (key, scope, dialect) | The run stops, in both fail modes. | Same. |

Both call `Vericto.enforce()`, so the guard's fail mode, verdict cache,
placeholder mapping and `on_verdict` work as they do in every other wrapper. The
guard is synchronous. In `async` tools and in the guardrail, the check runs in a
worker thread (`asyncio.to_thread`) and the event loop keeps running. The SDK
already runs a sync tool in a worker thread.

## Install

```bash
pip install "vericto[openai-agents]"
```

Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and `VERICTO_DATABASE_ID`. Each
checked tool call is one Runtime API evaluation.

## A SQL tool you write: `vericto_sql_tool`

The model below is a scripted stand-in (a `Model` subclass), so the example runs
without an OpenAI key. It sends a `DELETE` without `WHERE`, then a query that
reads a masked column, then answers. Drop `model=` to use your OpenAI model.

<!-- docs-check: requires=agents -->
```python
import json
import sqlite3

from agents import Agent, Runner, set_tracing_disabled
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from vericto import Vericto
from vericto.openai_agents import vericto_sql_tool

guard = Vericto()  # one per process
conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
conn.execute("INSERT INTO customers (name, email) VALUES ('Ana', 'ana@example.com'), ('Bo', 'bo@example.com')")
ran = []


@vericto_sql_tool(dialect="sqlite", guard=guard)  # every function_tool argument works too
def run_sql(query: str) -> str:
    """Run one SQLite query against the customers table and return the rows.

    Args:
        query: The SQL query.
    """
    ran.append(query)  # only allowed queries get here, as Vericto approved them
    return str(conn.execute(query).fetchall())


class ScriptedModel(Model):
    def __init__(self, outputs):
        self.outputs, self.inputs = outputs, []

    async def get_response(self, system_instructions, input, *args, **kwargs):
        self.inputs.append(input)
        return ModelResponse(output=[self.outputs.pop(0)], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def call(call_id, query):
    return ResponseFunctionToolCall(
        type="function_call", call_id=call_id, name="run_sql", arguments=json.dumps({"query": query})
    )


answer = ResponseOutputMessage(
    id="msg_1",
    type="message",
    role="assistant",
    status="completed",
    content=[ResponseOutputText(type="output_text", text="Vericto blocked the delete.", annotations=[])],
)
model = ScriptedModel(
    [call("call_1", "DELETE FROM customers"), call("call_2", "SELECT name, email FROM customers"), answer]
)
set_tracing_disabled(True)  # the example sends nothing to OpenAI
agent = Agent(name="analyst", instructions="Answer questions about customers.", tools=[run_sql], model=model)
result = Runner.run_sync(agent, "Delete every customer, then list them")
print(result.final_output)

outputs = [
    item["output"] for item in model.inputs[-1] if isinstance(item, dict) and item.get("type") == "function_call_output"
]
assert outputs[0].startswith("Error: Vericto blocked this query (VERICTO-001")  # what the model read
assert "Suggested fix:" in outputs[0]
assert ran == ["SELECT name, '***' AS email FROM customers"]  # the approved query ran, never the original
assert "ana@example.com" not in outputs[1]
assert conn.execute("SELECT count(*) FROM customers").fetchone() == (2,)
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

`vericto_sql_tool(func, dialect=..., ...)` also works as a plain call. Options:

- `dialect` (required): the dialect of the database your function runs SQL
  against (`"postgres"`, `"snowflake"`, `"mysql"`, `"mssql"`, `"oracle"`,
  `"sqlite"`).
- `guard`: a `Vericto`. When omitted, it is built from the `VERICTO_*` variables.
- `argument="query"`: the name of your function's SQL parameter. Other
  parameters, and a leading `RunContextWrapper` / `ToolContext` parameter, work as
  with `function_tool`.
- `on_block="return"` (default) or `"raise"`.
- `context=` (`{"ticket": ..., "tenant": ...}`) and `end_user=`: a value, or a
  callable read on each call (for example a `contextvars.ContextVar`'s `.get`).
  They take effect when business policies are enabled in your workspace.
- `paramstyle=`: when your function executes the query with bound parameters
  (`%s` markers), as in `Vericto.enforce()`.
- `failure_error_function`: as in `function_tool`, for errors raised by your own
  function (the SDK's default message, yours, or `None` to stop the run).
  Vericto's own errors are never turned into model text. To do that, the tool
  gets a `failure_error_function` that wraps the one you choose, so the SDK
  treats it as a custom one: `RunConfig.tool_error_formatter` does not apply to
  it.
- Every other keyword argument goes to `function_tool` (`name_override`,
  `needs_approval`, `timeout`, `tool_input_guardrails`...).

## A SQL tool you do not write: `vericto_tool_input_guardrail`

Attach the guardrail wherever the SDK takes tool input guardrails:
`function_tool(..., tool_input_guardrails=[...])`, the
`tool_input_guardrails` list of an existing `FunctionTool`, or an MCP server's
`tool_input_guardrails=`. On an MCP server it applies to every tool, so use
`tools=` to name the SQL ones. Calls to other tools pass unchecked.

<!-- docs-check: continue -->
```python
from agents import function_tool

from vericto.openai_agents import vericto_tool_input_guardrail


def vendor_query(query: str) -> str:
    """Run one SQLite query (a tool from a library you do not edit)."""
    return str(conn.execute(query).fetchall())


vendor_tool = function_tool(vendor_query)
vendor_tool.tool_input_guardrails = [
    vericto_tool_input_guardrail(dialect="sqlite", guard=guard, tools=["vendor_query"])
]

model = ScriptedModel(
    [
        ResponseFunctionToolCall(
            type="function_call", call_id="c1", name="vendor_query", arguments='{"query": "DELETE FROM customers"}'
        ),
        answer,
    ]
)
result = Runner.run_sync(Agent(name="ops", tools=[vendor_tool], model=model), "Clean up the customers table")
[check] = result.tool_input_guardrail_results
print(check.output.behavior["type"], check.output.output_info.rule_code)  # reject_content VERICTO-001
assert conn.execute("SELECT count(*) FROM customers").fetchone() == (2,)
```

For an MCP server whose SQL tool is `execute_sql(sql)`:

<!-- docs-check: skip a fragment (needs an MCP server) -->
```python
from agents.mcp import MCPServerStdio

postgres = MCPServerStdio(
    params={"command": "your-postgres-mcp-server"},
    tool_input_guardrails=[vericto_tool_input_guardrail(dialect="postgres", argument="sql", tools=["execute_sql"])],
)
agent = Agent(name="analyst", mcp_servers=[postgres])
```

Options: `dialect`, `guard`, `argument`, `on_block`, `context`, `end_user` as
above, plus `tools` (the tool names to check; `None` checks every tool the
guardrail is attached to) and `name` (`"vericto"`, the name the SDK reports). A
checked call whose arguments do not contain a string `argument` is rejected
without running.

## Async tools

`async def` tools work the same way; the check runs in a worker thread:

<!-- docs-check: skip a fragment (pool is your asyncpg pool) -->
```python
@vericto_sql_tool(dialect="postgres", guard=guard, context=current_ticket.get)
async def query_warehouse(query: str) -> str:
    """Run one read-only query against the warehouse."""
    return str(await pool.fetch(query))
```

## Tested with

Python 3.13.12, vericto 0.6.0, openai-agents 0.24.0. The examples above run
against the local Runtime API stand-in with a scripted model (tracing disabled,
no OpenAI calls), and `tests/test_openai_agents.py` runs against the real
`Runner` in CI.
