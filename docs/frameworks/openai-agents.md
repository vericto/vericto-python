# OpenAI Agents SDK

**Pattern, no official integration.** vericto has no module for the OpenAI Agents
SDK (`openai-agents`). Give the agent a `function_tool` that checks the SQL with
the guard before running it. A blocked query is returned to the model as the
tool's output; an unavailable Vericto stops the run.

## Install

```bash
pip install vericto openai-agents
```

Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and `VERICTO_DATABASE_ID`.

## Example

The model is a scripted stand-in (a `Model` subclass) so the example runs without
an OpenAI key: it calls the tool with a `DELETE` without `WHERE`, then answers.
Drop `model=` to use your OpenAI model.

<!-- docs-check: requires=agents -->
```python
import asyncio
import sqlite3

from agents import Agent, Runner, function_tool, set_tracing_disabled
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from vericto import Vericto, VerictoBlocked

guard = Vericto()  # one per process
conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
conn.execute("INSERT INTO orders (status) VALUES ('shipped'), ('cancelled')")


# failure_error_function=None: an exception (Vericto unavailable or misconfigured) stops
# the run instead of being sent to the model as text.
@function_tool(failure_error_function=None)
async def run_sql(query: str) -> str:
    """Run one SQLite query against the orders table and return the rows.

    Args:
        query: The SQL query.
    """
    try:
        # vericto is synchronous: keep the HTTP check off the event loop.
        sql = await asyncio.to_thread(guard.prepare, query, "sqlite")
    except VerictoBlocked as exc:
        return f"Blocked by Vericto ({exc.verdict.rule_code}), not executed. Suggested fix: {exc.suggested_fix}"
    return str(conn.execute(sql).fetchall())


class ScriptedModel(Model):
    def __init__(self, outputs):
        self.outputs, self.inputs = outputs, []

    async def get_response(self, system_instructions, input, *args, **kwargs):
        self.inputs.append(input)
        return ModelResponse(output=[self.outputs.pop(0)], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


model = ScriptedModel(
    [
        ResponseFunctionToolCall(
            type="function_call", call_id="call_1", name="run_sql", arguments='{"query": "DELETE FROM orders"}'
        ),
        ResponseOutputMessage(
            id="msg_1",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text="Vericto blocked the delete.", annotations=[])],
        ),
    ]
)
set_tracing_disabled(True)  # the example sends nothing to OpenAI
agent = Agent(name="analyst", instructions="Answer questions about orders.", tools=[run_sql], model=model)
result = Runner.run_sync(agent, "Delete all orders")

print(result.final_output)
assert "Blocked by Vericto (VERICTO-001)" in str(model.inputs[-1])  # what the model read
assert conn.execute("SELECT count(*) FROM orders").fetchone() == (2,)
```

## Notes

- With the SDK's default `failure_error_function`, any exception in a tool,
  `VerictoUnavailable` included, becomes a message for the model. With
  `failure_error_function=None` (above) the run raises
  `agents.exceptions.UserError` whose `__cause__` is the `VerictoUnavailable` or
  `VerictoConfigError`: the fail-closed decision stays with your code.
- `guard.prepare()` returns the masked rewrite when the query reads masked columns;
  run that, never `query`.
- Use `asyncio.to_thread(guard.prepare, ...)` in `async` tools; a sync
  `def` tool can call `guard.prepare()` directly.
- `FLAGGED` and `MONITORED` statements run and are logged on the `vericto` logger;
  `Vericto(on_verdict=...)` receives every verdict.

## Tested with

Python 3.13.12, vericto 0.4.1, openai-agents 0.24.0, against the local Runtime
API stand-in, with a scripted model (tracing disabled, no OpenAI calls).
