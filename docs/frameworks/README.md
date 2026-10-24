# Framework guides

How to put Vericto in front of the SQL that each framework runs. **Official**
means vericto ships the integration (a module you import); **pattern** means
there is no vericto module for that framework and the guide shows the few lines
that call the guard yourself. Every example in these guides runs in CI against a
local stand-in for the Runtime API (`scripts/check_docs.py`), with scripted
models, at the versions each guide lists.

| Framework | Kind | Guide | Tested with |
|---|---|---|---|
| LangChain (`SQLDatabase`, `create_sql_agent`, `create_agent`, SQL tools) | Official: `vericto.langchain` | [langchain.md](langchain.md) | langchain-core 1.7.0, langchain-community 0.4.2, langchain 1.4.7 |
| LlamaIndex (`NLSQLTableQueryEngine`, `NLSQLRetriever`) | Official: `vericto.llamaindex` | [llamaindex.md](llamaindex.md) | llama-index-core 0.14.25 |
| SQLAlchemy (Core, ORM, `text()`) | Official: `vericto.sqlalchemy` | [sqlalchemy.md](sqlalchemy.md) | SQLAlchemy 2.1.6 |
| Any DB-API driver (psycopg, PyMySQL, oracledb, pyodbc, sqlite3, Snowflake) | Official: `vericto.wrap`, `vericto.sqlite`, `vericto.snowflake` | [dbapi.md](dbapi.md) | sqlite3, psycopg 3.3.6 |
| Django | Pattern: `connection.execute_wrapper` | [django.md](django.md) | Django 6.1.2 |
| CrewAI | Pattern: a guarded `@tool` | [crewai.md](crewai.md) | crewai 1.15.30 |
| OpenAI Agents SDK | Pattern: a guarded `function_tool` | [openai-agents.md](openai-agents.md) | openai-agents 0.24.0 |
| Pydantic AI | Pattern: a guarded tool raising `ModelRetry` | [pydantic-ai.md](pydantic-ai.md) | pydantic-ai-slim 2.60.0 |
| Vanna 2.x | Pattern: a guarded `SqlRunner` | [vanna.md](vanna.md) | vanna 2.0.2 |

dbt and Airflow are covered in the [README](../../README.md#integrations)
(`vericto-dbt`, `vericto.airflow`). All versions: Python 3.13, vericto 0.5.0.

## The pattern, in one paragraph

Call `guard.prepare(sql, dialect)` right before your code executes `sql`, and
execute what it returns (the masked rewrite when the query reads masked columns).
It raises `VerictoBlocked` when Vericto blocks the query: return that to the model
as text (rule and `suggested_fix`) so it can correct itself. Let
`VerictoUnavailable` and `VerictoConfigError` propagate, so an outage or a revoked
key fails closed instead of becoming a message the model can argue with. vericto
is synchronous; in `async` code use `await asyncio.to_thread(guard.prepare, sql, dialect)`.
