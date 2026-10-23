# LangChain

**Official integration:** `vericto.langchain` (extra `vericto[langchain]`):
`VerictoSQLDatabase` and `VerictoCheckSQLTool`.

`VerictoSQLDatabase` is a `langchain_community.utilities.SQLDatabase`, so it goes
wherever one goes: `QuerySQLDatabaseTool`, `SQLDatabaseToolkit`,
`create_sql_agent`, LangChain 1.x `create_agent` with the toolkit's tools, and
`create_sql_query_chain` pipelines. Every query it runs (and the sample rows
`get_table_info` puts in the prompt) is checked with Vericto after SQLAlchemy
compiles it. A blocked query does not run; the agent receives a message with the
rule and the suggested fix instead, so it can correct itself.

## Install

```bash
pip install "vericto[langchain]"   # langchain-community, langchain-core, SQLAlchemy
pip install langchain              # for create_agent (LangChain 1.x), or langchain-classic for create_sql_agent
```

Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and `VERICTO_DATABASE_ID`.

## Example: an agent with LangChain 1.x `create_agent`

The model below is a scripted stand-in so the example runs without an LLM: it
first tries an unsafe `DELETE`, reads Vericto's answer and retries with the
suggested `WHERE`. Use your chat model (`init_chat_model(...)`) instead.

<!-- docs-check: requires=langchain,langchain_community -->
```python
import sqlite3

from langchain.agents import create_agent
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vericto.langchain import VerictoCheckSQLTool, VerictoSQLDatabase

with sqlite3.connect("app.db") as setup:  # a small database for the example
    setup.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, active INTEGER);"
        "INSERT INTO customers (email, active) VALUES ('a@example.com', 1), ('b@example.com', 0);"
    )
db = VerictoSQLDatabase.from_uri("sqlite:///app.db")  # every SQLDatabase argument works


class ScriptedChatModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # the fake model ignores the tool schemas
        return self


def call(tool, **args):
    return AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": tool + str(len(args))}])


llm = ScriptedChatModel(
    messages=iter(
        [
            call("sql_db_query", query="DELETE FROM customers"),
            call("sql_db_query", query="DELETE FROM customers WHERE active = 0"),
            AIMessage(content="Deleted the inactive customers."),
        ]
    )
)
tools = [*SQLDatabaseToolkit(db=db, llm=llm).get_tools(), VerictoCheckSQLTool(db=db)]
agent = create_agent(llm, tools)

result = agent.invoke({"messages": [{"role": "user", "content": "Delete the inactive customers"}]})
replies = [m.content for m in result["messages"] if m.type == "tool"]
assert replies[0].startswith("Error: Vericto blocked this query (VERICTO-001")  # nothing ran
print(replies[0])
assert db.run("SELECT count(*) FROM customers") == "[(1,)]"
```

## Example: `create_sql_agent` (langchain-classic)

<!-- docs-check: skip needs a chat model; the same flow runs in tests/test_langchain.py with a scripted LLM -->
```python
from langchain_community.agent_toolkits import SQLDatabaseToolkit, create_sql_agent

from vericto.langchain import VerictoCheckSQLTool, VerictoSQLDatabase

db = VerictoSQLDatabase.from_uri("postgresql+psycopg://app@db/app")
agent = create_sql_agent(
    llm,  # your chat model
    toolkit=SQLDatabaseToolkit(db=db, llm=llm),
    agent_type="tool-calling",
    extra_tools=[VerictoCheckSQLTool(db=db)],
)
agent.invoke({"input": "How many customers signed up last week?"})
```

## The database and the tools on their own

<!-- docs-check: requires=langchain_community -->
```python
from langchain_community.tools import QuerySQLDatabaseTool
from sqlalchemy import create_engine

from vericto import Vericto
from vericto.langchain import VerictoCheckSQLTool, VerictoSQLDatabase

engine = create_engine("sqlite:///app.db")
with engine.begin() as conn:
    conn.exec_driver_sql("CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT)")
    conn.exec_driver_sql("INSERT INTO customers (email) VALUES ('a@example.com')")

guard = Vericto()  # share one guard per process
db = VerictoSQLDatabase(engine, guard=guard, on_block="return")  # or on_block="raise"

query = QuerySQLDatabaseTool(db=db)
print(query.invoke("SELECT id, email FROM customers"))  # the masked rewrite ran: [(1, '***')]
print(query.invoke("DELETE FROM customers"))  # "Error: Vericto blocked this query (...)"

check = VerictoCheckSQLTool(db=db)  # asks only, never runs anything
print(check.invoke("DELETE FROM customers WHERE id = 1"))  # "ALLOWED: ..."
```

## What each verdict does

| Verdict | What the agent sees |
|---|---|
| `ALLOWED` | The rows. |
| `FLAGGED` / `MONITORED` | The rows; the verdict is logged and passed to `on_verdict`. |
| mask | The rows of the masked rewrite (sample rows in the prompt too), never the real values. |
| `BLOCKED` | `on_block="return"` (default): `run` / `run_no_throw` / `QuerySQLDatabaseTool` return `Error: Vericto blocked this query (VERICTO-001, severity critical). It was NOT executed.` plus the reason and `Suggested fix: ...`. `on_block="raise"`: `VerictoBlocked` (`run(fetch="cursor")` always raises). |
| no verdict, configuration error | Raised (`VerictoUnavailable`, `VerictoConfigError`), never handed to the model. `fail_mode="open"` runs the query unevaluated instead. |

- A blocked sample-rows query is left out of the table info instead of failing.
- `VerictoCheckSQLTool` (`vericto_check_sql`) answers ALLOWED, FLAGGED, the masked
  query that would run, or the block with its fix. Each call is one Runtime API
  evaluation (quota, audit trail). Under `fail_mode="open"` it answers
  `UNEVALUATED` when Vericto cannot be reached.
- An engine already instrumented with `vericto.sqlalchemy.instrument` is not
  checked twice. Schema reflection is not sent to Vericto.
- Everything is synchronous: LangChain's async entry points (`ainvoke`) run the
  tools in a thread pool, so the check does not block the event loop.

## Your own tools

For a tool that runs SQL without `SQLDatabase` (your own `@tool`, a driver call),
check the SQL in the tool and run what the guard returns:

<!-- docs-check: requires=langchain_core -->
```python
import sqlite3

from langchain_core.tools import tool

from vericto import Vericto, VerictoBlocked

guard = Vericto()
conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")


@tool
def run_sql(query: str) -> str:
    """Run one SQLite query and return the rows."""
    try:
        sql = guard.prepare(query, "sqlite")  # raises VerictoBlocked; returns the masked rewrite if any
    except VerictoBlocked as exc:
        return f"Blocked by Vericto ({exc.verdict.rule_code}). Suggested fix: {exc.suggested_fix}"
    return str(conn.execute(sql).fetchall())


print(run_sql.invoke({"query": "DELETE FROM orders"}))
```

## Tested with

Python 3.13.12, vericto 0.4.1, langchain-core 1.7.0, langchain-community 0.4.2,
langchain-classic 1.0.8, langchain 1.4.7 (langgraph 1.2.16), SQLAlchemy 2.1.6,
against the local Runtime API stand-in, with scripted models only.
