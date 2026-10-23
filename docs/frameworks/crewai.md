# CrewAI

**Pattern, no official integration.** vericto has no CrewAI module. Give your
agents a SQL tool that checks the query with the guard first and runs only what
the guard returns. A blocked query comes back to the agent as the tool's output,
so it can correct itself; nothing reaches the database.

## Install

```bash
pip install vericto crewai
```

Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and `VERICTO_DATABASE_ID`.

## Example

The LLM is a scripted stand-in (a `BaseLLM` subclass) so the example runs without
a model: it tries a `DELETE` without `WHERE`, reads Vericto's answer and stops.
Use `llm="openai/gpt-4o-mini"` (or any CrewAI LLM) instead.

<!-- docs-check: requires=crewai -->
```python
import sqlite3
from typing import Any

from crewai import Agent, Crew, Task
from crewai.llms.base_llm import BaseLLM
from crewai.tools import tool

from vericto import Vericto, VerictoBlocked

guard = Vericto()  # one per process
conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
conn.execute("INSERT INTO orders (status) VALUES ('shipped'), ('cancelled')")


@tool("run_sql")
def run_sql(query: str) -> str:
    """Run one SQLite query against the orders table and return the rows."""
    try:
        sql = guard.prepare(query, "sqlite")  # the masked rewrite when there is one
    except VerictoBlocked as exc:
        return f"Blocked by Vericto ({exc.verdict.rule_code}), not executed. Suggested fix: {exc.suggested_fix}"
    return str(conn.execute(sql).fetchall())


class ScriptedLLM(BaseLLM):
    replies: list[str]
    seen: list[str] = []

    def call(self, messages, tools=None, callbacks=None, available_functions=None, **kwargs: Any) -> str:
        self.seen.append(str(messages))
        return self.replies.pop(0)

    def supports_function_calling(self) -> bool:
        return False


llm = ScriptedLLM(
    model="scripted",
    replies=[
        'Thought: delete them\nAction: run_sql\nAction Input: {"query": "DELETE FROM orders"}',
        "Thought: Vericto blocked it.\nFinal Answer: The delete was blocked by Vericto; nothing was deleted.",
    ],
)
agent = Agent(role="analyst", goal="Answer questions about orders", backstory="Careful", tools=[run_sql], llm=llm)
task = Task(description="Delete all orders", expected_output="What happened", agent=agent)
print(Crew(agents=[agent], tasks=[task]).kickoff())

assert "Blocked by Vericto (VERICTO-001)" in llm.seen[-1]  # what the agent read
assert conn.execute("SELECT count(*) FROM orders").fetchone() == (2,)
```

## Notes

- Return the block as text (as above) so the agent can fix the query, but let
  `VerictoUnavailable` and `VerictoConfigError` propagate: turning "Vericto is
  down" into a message would let the model keep trying, or work around it.
- `guard.prepare()` returns the SQL to run: the masked rewrite when the query
  reads masked columns. Never run `query` itself.
- CrewAI's `NL2SQLTool` (crewai-tools) opens its own SQLAlchemy engine on each
  call, so it cannot be instrumented; use a tool like the one above instead.
- The check is synchronous. Async crews (`kickoff_async`) run sync tools in a
  worker thread.
- `FLAGGED` and `MONITORED` statements run, are logged on the `vericto` logger and
  reach `on_verdict` (`Vericto(on_verdict=...)`), for your own metrics.

## Tested with

Python 3.13.12, vericto 0.4.1, crewai 1.15.29 (CrewAI telemetry and tracing off),
against the local Runtime API stand-in, with a scripted LLM.
