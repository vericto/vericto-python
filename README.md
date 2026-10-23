Official Vericto SDK for Python: check SQL before it runs (snowflake-connector-python, sqlite3, SQLAlchemy, any DB-API driver, LangChain, LlamaIndex, dbt and Airflow).

# vericto (Python)

> Check every SQL statement with [Vericto](https://vericto.com) **before** it runs:
> thin wrappers for `snowflake-connector-python`, `sqlite3`, SQLAlchemy and any
> DB-API driver, and integrations for LangChain, LlamaIndex, dbt and Airflow.

Vericto is a deterministic SQL firewall: it parses each statement into an AST and
evaluates it against your workspace's rules (destructive statements, missing
`WHERE`/`LIMIT`, risky DDL, data exposure, sensitive columns, agent allowlists).
This package puts that check in front of your driver's `execute()`: blocked
statements raise before they reach the database, flagged ones run and are
reported, and statements that read masked columns run as the masked rewrite
Vericto returns.

It is a thin client of the **Vericto Runtime API** (`POST /api/v1/proxy/query`).
Evaluation happens in your Vericto workspace; nothing is evaluated locally in this
release (see [Evaluators](#evaluators)).

- Python 3.10+, typed (`py.typed`), one dependency (`urllib3`).
- Drivers are optional extras; importing `vericto` never imports a driver.

## Install

```bash
pip install vericto                  # core + sqlite3 + generic DB-API wrapper
pip install "vericto[snowflake]"     # + snowflake-connector-python
pip install "vericto[sqlalchemy]"    # + SQLAlchemy
pip install "vericto[langchain]"     # LangChain SQL agents and chains
pip install "vericto[llamaindex]"    # LlamaIndex text-to-SQL
pip install "vericto[airflow]"       # Airflow 3 operator and cluster policy
pip install "vericto[dbt]"           # vericto-dbt: CI checks for dbt projects (no extra dependency)
```

## Configure

Create an API key with the **`proxy:evaluate`** scope in the Vericto dashboard
(API keys) and register the database you are protecting. Then:

| Variable | Meaning | Default |
|---|---|---|
| `VERICTO_API_KEY` | API key (`vtro_...`), scope `proxy:evaluate` | required |
| `VERICTO_DATABASE_ID` | The registered database (UUID): its rules, sensitive-column tags and agent allowlists apply | required |
| `VERICTO_API_URL` | API base URL | `https://api.vericto.com` |
| `VERICTO_FAIL_MODE` | `closed` or `open` (see [Fail mode](#fail-mode)) | `closed` |
| `VERICTO_TIMEOUT` | Seconds per attempt (see [Timeouts and retries](#timeouts-and-retries)) | `5` |
| `VERICTO_CA_BUNDLE` | Extra CA bundle (PEM) to trust | system store |

A keyword argument beats its variable: `Vericto(api_key=..., database_id=...,
api_url=..., timeout=..., max_retries=..., fail_mode=...)`. The CA bundle is an
argument of the evaluator, not of `Vericto`:
`Vericto(RuntimeApiEvaluator(ca_bundle="corp-ca.pem"))`. An empty or
whitespace-only variable counts as unset (its default applies); an invalid value
raises `VerictoConfigError` when the guard is built. The key is sent only in the
`X-API-Key` header, only over HTTPS (plain HTTP is accepted for `localhost`,
`127.0.0.1` and `::1` alone), and never appears in `repr()`, logs or exception
messages.

## Quickstart

### Snowflake

<!-- docs-check: skip needs a Snowflake account -->
```python
import vericto.snowflake
from vericto import VerictoBlocked

conn = vericto.snowflake.connect(  # same arguments as snowflake.connector.connect
    account="myorg-myaccount",
    user="APP",
    authenticator="SNOWFLAKE_JWT",
    private_key_file="rsa_key.p8",
    warehouse="APP_WH",
)
cur = conn.cursor()
cur.execute("SELECT id, email FROM customers WHERE id = %s", (42,))

try:
    cur.execute("DELETE FROM customers")  # never reaches Snowflake
except VerictoBlocked as exc:
    print(exc.verdict.rule_code, exc.suggested_fix)
```

Already have a connection? `vericto.snowflake.wrap(conn)`. Checked entry points:
`cursor.execute`, `executemany`, `execute_async`, `connection.execute_string` and
`execute_stream`. Scripts are split with the connector's own splitter and every
statement is evaluated **before the first one runs**: a block anywhere means
nothing executes.

### sqlite3

```python
import time

import vericto.sqlite

conn = vericto.sqlite.connect("app.db")  # a real sqlite3.Connection subclass
conn.executescript("CREATE TABLE sessions (id INTEGER PRIMARY KEY, expires_at REAL);")  # checked whole first
conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
```

`execute`, `executemany` and `executescript` are checked on the connection and on
its cursors. `executescript` is split with `sqlite3.complete_statement` (SQLite's
own tokenizer, so triggers, strings and comments are handled) and evaluated whole
before it runs. See [SQLite status](#sqlite-status).

### SQLAlchemy (any dialect)

<!-- docs-check: requires=sqlalchemy -->
```python
from sqlalchemy import create_engine, text

import vericto.sqlalchemy

engine = create_engine("sqlite://")  # or snowflake://, postgresql+psycopg://, mysql+pymysql://, oracle://, mssql://
remove = vericto.sqlalchemy.instrument(engine)  # AsyncEngine works too (see "Sync and async")
with engine.connect() as conn:
    print(conn.execute(text("SELECT 1")).scalar_one())
remove()
```

A `before_cursor_execute` listener checks the compiled SQL of every statement
(ORM, Core and `text()`). Dialects map from `engine.dialect.name`: `snowflake`,
`postgresql` → `postgres`, `mysql`/`mariadb` → `mysql`, `oracle`, `mssql`,
`sqlite`. `VerictoBlocked` propagates unwrapped.

### Any DB-API driver, or by hand

<!-- docs-check: skip needs a PostgreSQL server (verified against postgres:17 with psycopg 3.3.6) -->
```python
import psycopg
import vericto
from vericto import Vericto

guard = Vericto()  # one per process; thread-safe
conn = vericto.wrap(psycopg.connect("dbname=app"), dialect="postgres", guard=guard)

verdict = guard.enforce("SELECT email FROM customers LIMIT 10", "postgres")
run_this = verdict.sql_to_execute  # the masked rewrite, if any
```

More drivers and every agent framework: [Frameworks](#frameworks).

## Core API

`Vericto` is the guard: one instance per process (thread-safe, it pools its HTTP
connections). The wrappers call it for you; call it yourself around anything else.

| Method | Does | Raises |
|---|---|---|
| `enforce(sql, dialect, *, paramstyle=None) -> Verdict` | Evaluates and applies the decision: logs flags, applies the fail mode. Run `verdict.sql_to_execute`. | `VerictoBlocked`; `VerictoUnavailable` (fail closed); `VerictoConfigError` |
| `prepare(sql, dialect, *, paramstyle=None) -> str` | `enforce()`, returning the SQL text to execute (the masked rewrite when there is one). | as `enforce` |
| `enforce_all(statements, dialect) -> list[Verdict]` | `enforce()` for a batch, every statement before any of them runs. | as `enforce` |
| `check(sql, dialect) -> Verdict` | Asks only: returns the verdict whatever it is, `BLOCKED` included, and does **not** apply the fail mode. | `VerictoUnavailable`; `VerictoConfigError` |
| `require_dialect(dialect) -> str` | Fails fast if the server does not evaluate the dialect; returns the normalized id (`postgresql` → `postgres`). | `VerictoUnsupportedDialect` |
| `clear_cache()` | Drops cached verdicts (see [Caching](#caching)). | |

`paramstyle` is the driver's DB-API paramstyle, given only when the statement runs
with parameters (see [Parameters and placeholders](#parameters-and-placeholders)).
Dialects: `postgres`, `mysql`, `oracle`, `mssql`, `snowflake`, `sqlite`
(`KNOWN_DIALECTS`; driver names such as `postgresql`, `mariadb`, `sqlserver` and
`sqlite3` are accepted).

```python
from vericto import Decision, Vericto, VerictoBlocked

guard = Vericto(on_verdict=lambda v: print("audit:", v.decision.value, v.rule_code, v.event_id))

verdict = guard.check("DELETE FROM orders", "postgres")  # asks only
assert verdict.decision is Decision.BLOCKED and verdict.blocked
print(verdict.rule_code, verdict.severity, verdict.suggested_fix)

sql = guard.prepare("SELECT id, email FROM customers WHERE id = 7", "postgres")
print(sql)  # the masked rewrite when email is a masked column

try:
    guard.enforce("DELETE FROM orders", "postgres")
except VerictoBlocked as exc:
    print(exc)  # Query blocked by VERICTO-001 (severity critical). Suggested fix: ...
```

A `Verdict` (frozen dataclass) carries:

| Field | Meaning |
|---|---|
| `decision` | `Decision.ALLOWED`, `FLAGGED`, `MONITORED`, `PARSE_ERROR`, `BLOCKED`, or `UNEVALUATED` (local: no verdict under `fail_mode="open"`). An unknown decision from the server is read as `BLOCKED`. |
| `sql`, `dialect` | The statement as submitted and its dialect. |
| `rule_code`, `severity`, `message` | The deciding rule (`VERICTO-001`), its severity, and the server's reason when it adds one. |
| `suggested_fix` | A safer statement Vericto proposes, when it has one. |
| `rewritten_query` | When set, the statement to run **instead of** `sql` (masked columns). `sql_to_execute` is that or `sql`; `masked` says which. |
| `findings` | `Finding(rule_code, severity, rule_id, ast_node_path, suggested_fix)` for each rule that matched. |
| `sensitive_columns`, `access_denied` | The tagged columns the statement reads, and what the API key's allowlist denied. |
| `parse_error` | The parser's message, on `PARSE_ERROR`. |
| `event_id` | The audit-trail event, to look the evaluation up in Vericto. |
| `blocked`, `degraded`, `cached` | `decision is BLOCKED`; ran without a verdict (fail-open); came from the cache. |
| `raw` | The response body, for fields this version does not model. |

## What each decision does

| Verdict | What the wrapper does |
|---|---|
| `ALLOWED` | Runs the statement. |
| `FLAGGED` | Runs it, logs a warning on the `vericto` logger, calls `on_verdict` (and warns with `VerictoFlaggedWarning` if `warn_on_flag=True`). |
| `MONITORED` | Runs it, logs at INFO, calls `on_verdict`. |
| `PARSE_ERROR` (allowed) | The workspace's `parse_error_action` let it through: runs it and logs a warning. When the workspace blocks unparseable SQL, the server answers `BLOCKED`. |
| `BLOCKED` | Raises `VerictoBlocked` (with `verdict`, `findings`, `suggested_fix`) **before** the driver is called. |
| mask (`rewritten_query`) | Runs `rewritten_query` **instead of** your SQL: the sensitive columns come back masked. Never the original. |
| no verdict | The [fail mode](#fail-mode) decides. |

`on_verdict` (a callable) receives every verdict, blocked and unevaluated ones
included, for metrics or your own audit log. Log lines carry the rule code,
severity and Vericto `event_id`, **never the SQL text** (it can hold personal
data in literals). Every evaluation is recorded in the Vericto audit trail.

## Fail mode

When Vericto cannot answer (network error, timeout, HTTP 5xx, rate limit after
retries, or the monthly quota `QUOTA_EXCEEDED`):

- `fail_mode="closed"` (default): raise `VerictoUnavailable`; the statement does
  not run.
- `fail_mode="open"`: run the statement unevaluated, emit
  `VerictoFailOpenWarning`, log a warning, and pass an `UNEVALUATED` verdict to
  `on_verdict`.

Configuration errors are **never** failed open: a missing or revoked key (401), a
key without the scope or for another database (403), a malformed request (400) or
an unsupported dialect raise `VerictoConfigError` in both modes, because failing
open on them would switch the firewall off silently.

`VerictoUnavailable` is raised in both modes by `check()`, which never applies
the fail mode (the LangChain `VerictoCheckSQLTool` answers `UNEVALUATED` instead
under `fail_mode="open"`).

```python
from vericto import Vericto, VerictoUnavailable

# Nothing listens on port 9: a network error. Fail closed, the default:
guard = Vericto(api_url="http://127.0.0.1:9", max_retries=0)
try:
    guard.enforce("SELECT 1", "postgres")
except VerictoUnavailable as exc:
    print("not executed:", exc)

# Fail open: the statement may run unevaluated, with a VerictoFailOpenWarning.
open_guard = Vericto(api_url="http://127.0.0.1:9", max_retries=0, fail_mode="open")
verdict = open_guard.enforce("SELECT 1", "postgres")
assert verdict.degraded and open_guard.prepare("SELECT 1", "postgres") == "SELECT 1"
```

### Timeouts and retries

`timeout` (`VERICTO_TIMEOUT`, default 5 s) bounds **each attempt**: connecting,
sending and reading the whole response. Connection errors, timeouts, HTTP 5xx,
a 200 response without a decision and 429 `RATE_LIMIT_EXCEEDED` are retried
`max_retries` times (default 2) with exponential backoff and jitter
(`backoff=0.2` s base on `RuntimeApiEvaluator`, capped at 2 s, honouring
`Retry-After` up to that cap). `QUOTA_EXCEEDED` and configuration errors are not
retried. The worst case before `VerictoUnavailable` is therefore about
`(max_retries + 1) × timeout` plus the backoff: about 15.6 s with the defaults.
Lower both for latency-sensitive paths, for example `Vericto(timeout=1,
max_retries=1)`.

The first check also asks `GET /api/v1/meta/config` which dialects the server
evaluates (cached for 10 minutes, `dialects_ttl`); if that call fails, nothing is
raised for it and the evaluation request decides.

## Sync and async

The API is **synchronous** and blocking: there is no `async` variant in this
release. Every check is one HTTPS round trip on the calling thread, and the
guard is thread-safe, so share one per process.

- In `asyncio` code, run the check in a worker thread so the event loop keeps
  going: `sql = await asyncio.to_thread(guard.prepare, sql, "postgres")`.
- `vericto.sqlalchemy.instrument()` accepts an `AsyncEngine`, but the check runs
  inside SQLAlchemy's execution, on the event loop: it blocks the loop for one
  round trip per statement.
- The framework guides say, for each framework, where the check runs (LangChain,
  CrewAI and Django run sync tools and ORM calls in worker threads).

## Parameters and placeholders

Values are **never** sent to Vericto. The SQL text goes as your application wrote
it, placeholders included, and the driver binds the values afterwards.

One adjustment: `format`/`pyformat` drivers (psycopg, PyMySQL, and
`snowflake-connector-python` by default) write parameters as `%s` / `%(name)s`,
which is not SQL and would come back as `PARSE_ERROR`. When such a statement is
executed **with** parameters, Vericto evaluates it with the dialect's own
placeholders (`$1` for PostgreSQL, `?` elsewhere) and `%%` read as `%`. A masked
rewrite is mapped back to your markers before it runs; if that cannot be done
exactly, `PlaceholderMappingError` is raised and nothing runs. `qmark`, `numeric`
and `named` drivers, and statements executed without parameters, are sent
byte-for-byte.

## Caching

Off by default (`cache_ttl=0`). With `Vericto(cache_ttl=30)`, identical
`(sql, dialect)` pairs reuse a verdict for 30 seconds (LRU, `cache_size=1024`),
and the whole cache is dropped when a response reports a new `ruleset_version`.

Why off: a cached hit is not evaluated, so it is **not in the Vericto audit
trail** and a rule or sensitive-column change takes effect only when the entry
expires (the Runtime API does not report the ruleset version today, so the TTL
is the only bound). Enable it for hot, repetitive statements where latency
matters more than a per-execution audit record, and keep the TTL short.

## SQLite status

SQLite has no server, so there is no Vericto proxy for it: this wrapper is the
enforcement point. The **Vericto server evaluates the `sqlite` dialect**
(vericto-engine 4.1.0+; `GET /api/v1/meta/config` lists `postgres`, `mysql`,
`oracle`, `mssql`, `snowflake`, `sqlite`), including SQLite-specific rules such
as `ATTACH DATABASE` (VERICTO-098), `PRAGMA foreign_keys = OFF` (VERICTO-099),
`load_extension()` (VERICTO-100) and `VACUUM INTO` (VERICTO-101).

`vericto.sqlite.connect()` and `vericto.sqlalchemy.instrument()` on a SQLite
engine still ask the server first and raise `VerictoUnsupportedDialect` before
any SQL runs if it does not list `sqlite` (for example an older self-hosted
server). There is deliberately no fallback to another dialect: evaluating SQLite
as PostgreSQL would give wrong answers. See [Live tests](#live-tests) for an
end-to-end check against a real server.

## Enforcement boundary

The wrapper protects the SQL that goes through it. Code that opens its own
connection, uses `cursor.wrapped`, or another process with the same credentials
is not checked. Pair the wrapper with database-side least privilege (and, for
Snowflake, a network policy) when the guarantee matters, and use the Vericto TCP
proxy where one exists (PostgreSQL, MySQL).

## Errors

All inherit from `VerictoError`.

| Exception | When |
|---|---|
| `VerictoBlocked` | Vericto blocked the statement. `.verdict`, `.findings`, `.suggested_fix`, `.sql`. |
| `VerictoUnavailable` | No verdict and `fail_mode="closed"`. |
| `VerictoQuotaExceeded` | Subclass of `VerictoUnavailable`: monthly quota used (`.resets_at`). |
| `VerictoConfigError` | Key, scope, database id, URL or request problem. Raised in both fail modes. |
| `VerictoUnsupportedDialect` | Subclass of `VerictoConfigError`: the server does not evaluate that dialect. |
| `PlaceholderMappingError` | A masked rewrite could not be mapped back to `%s`-style placeholders. |

## Evaluators

`Vericto` takes an `evaluator`: anything with `evaluate(sql, dialect) -> Verdict`
and `supported_dialects()`. This release ships `RuntimeApiEvaluator`. An embedded
evaluator (the Vericto engine in-process with synced rules, for latency and
offline use) can be added later behind the same interface without changing the
wrappers or your code beyond the constructor.

## Frameworks

**Official** integrations are modules this package ships; **patterns** are
frameworks with no vericto module, where the guide shows the few lines that call
the guard. Each guide has a runnable example (checked in CI against a local
Runtime API stand-in, with scripted models) and the versions it was tested with.

| Framework | Kind | Guide | Tested with |
|---|---|---|---|
| LangChain (`SQLDatabase`, `create_sql_agent`, `create_agent`, SQL tools) | Official: `vericto.langchain` (`vericto[langchain]`) | [docs/frameworks/langchain.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/langchain.md) | langchain-core 1.7.0, langchain-community 0.4.2, langchain-classic 1.0.8, langchain 1.4.7 |
| LlamaIndex (`NLSQLTableQueryEngine`, `SQLTableRetrieverQueryEngine`, `NLSQLRetriever`) | Official: `vericto.llamaindex` (`vericto[llamaindex]`) | [docs/frameworks/llamaindex.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/llamaindex.md) | llama-index-core 0.14.25 |
| SQLAlchemy (ORM, Core, `text()`; `AsyncEngine`) | Official: `vericto.sqlalchemy` (`vericto[sqlalchemy]`) | [docs/frameworks/sqlalchemy.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/sqlalchemy.md) | SQLAlchemy 2.1.6 |
| Any DB-API driver; `sqlite3`; snowflake-connector-python | Official: `vericto.wrap`, `vericto.sqlite`, `vericto.snowflake` | [docs/frameworks/dbapi.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/dbapi.md) | sqlite3, psycopg 3.3.6 |
| Django | Pattern: `connection.execute_wrapper` | [docs/frameworks/django.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/django.md) | Django 6.1.2 |
| CrewAI | Pattern: a guarded `@tool` | [docs/frameworks/crewai.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/crewai.md) | crewai 1.15.29 |
| OpenAI Agents SDK | Pattern: a guarded `function_tool` | [docs/frameworks/openai-agents.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/openai-agents.md) | openai-agents 0.24.0 |
| Pydantic AI | Pattern: a guarded tool raising `ModelRetry` | [docs/frameworks/pydantic-ai.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/pydantic-ai.md) | pydantic-ai-slim 2.60.0 |
| Vanna 2.x | Pattern: a guarded `SqlRunner` | [docs/frameworks/vanna.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/vanna.md) | vanna 2.0.2 |
| dbt | Official: `vericto-dbt check` (`vericto[dbt]`) | [dbt](#dbt) | dbt manifest v12 fixtures (no dbt runtime) |
| Airflow 3 | Official: `vericto.airflow` (`vericto[airflow]`) | [Airflow](#airflow) | Airflow 3.3.2 (common-sql 2.1.1), official constraints |
| LangChain.js | `@vericto/sdk/langchain` (Node) | [vericto-node](https://github.com/vericto/vericto-node) | see vericto-node |

All on Python 3.13 with vericto 0.4.1. The pattern in one line: call
`guard.prepare(sql, dialect)` right before your code executes `sql`, execute what
it returns, hand a `VerictoBlocked` back to the model as text (rule and
`suggested_fix`), and let `VerictoUnavailable` / `VerictoConfigError` propagate so
an outage fails closed. Overview: [docs/frameworks](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/README.md).

## Integrations

Each is a thin layer over the guard above; the core API is unchanged.

Runtime paths (agents, Airflow tasks) use the **Runtime API** with a
`proxy:evaluate` key, as the wrappers do, and spend the runtime query quota.
`vericto-dbt` checks SQL that is not being executed, so it uses the **CI-check
API** (`POST /api/v1/ci/check-key`, the endpoint of `vericto check` in
vericto-cli) with a `ci_dryrun:execute` key, and spends the monthly CI-check
allowance instead.

### LangChain

Full guide: [docs/frameworks/langchain.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/langchain.md).

<!-- docs-check: skip a fragment (llm is your chat model); the guide's examples run -->
```python
from langchain_community.agent_toolkits import SQLDatabaseToolkit, create_sql_agent
from vericto.langchain import VerictoCheckSQLTool, VerictoSQLDatabase

db = VerictoSQLDatabase.from_uri("postgresql+psycopg://app@db/app")  # every SQLDatabase argument works
agent = create_sql_agent(llm, toolkit=SQLDatabaseToolkit(db=db, llm=llm), extra_tools=[VerictoCheckSQLTool(db=db)])
```

`VerictoSQLDatabase` is a `langchain_community.utilities.SQLDatabase` (also
`VerictoSQLDatabase(engine, guard=..., dialect=..., on_block=...)`). Every query
it runs, and the sample rows `get_table_info` puts in the prompt, is checked
after SQLAlchemy compiles it; schema reflection is not sent.

- **BLOCKED**, `on_block="return"` (default): nothing runs; `run` /
  `run_no_throw` (so `QuerySQLDatabaseTool`) return a message the model can act on:
  `Error: Vericto blocked this query (VERICTO-010, severity critical). It was NOT
  executed.` + the reason + `Suggested fix: ...`. `on_block="raise"` raises
  `VerictoBlocked` instead (`run(fetch="cursor")` always raises).
- **Mask**: the masked rewrite runs, sample rows included. **FLAGGED /
  MONITORED**: run, logged, passed to `on_verdict`.
- **Vericto unavailable or misconfigured**: raised, never handed to the model
  (`fail_mode="open"` runs the query unevaluated).
- A blocked sample-rows query is left out of the table info instead of failing.
- `VerictoCheckSQLTool` (`vericto_check_sql`) only asks: it returns ALLOWED,
  FLAGGED, the masked query that would run, or the block with its fix. Each call is
  one Runtime API evaluation.
- An engine already instrumented with `vericto.sqlalchemy.instrument` is not
  checked twice (new helper: `vericto.sqlalchemy.is_instrumented`).

### LlamaIndex

Full guide: [docs/frameworks/llamaindex.md](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/llamaindex.md).

<!-- docs-check: skip a fragment (engine and llm are yours); the guide's example runs -->
```python
from llama_index.core.query_engine import NLSQLTableQueryEngine
from vericto.llamaindex import VerictoSQLDatabase

sql_database = VerictoSQLDatabase(engine, include_tables=["orders"], on_block="return")
query_engine = NLSQLTableQueryEngine(sql_database=sql_database, tables=["orders"], llm=llm)
```

A `llama_index.core.SQLDatabase`: `run_sql` and `insert_into_table` are checked.
With `on_block="return"` (default) a blocked `run_sql` returns the same
explanation as the SQL result (metadata `vericto_blocked`), so the synthesized
answer says why; `"raise"` raises `VerictoBlocked` (`NLSQLTableQueryEngine`
turns any retrieval error into `Error: ...` text itself; `NLSQLRetriever(...,
handle_sql_errors=False)` lets it through). Masks, flags and unavailability as
for LangChain.

### dbt

```bash
dbt compile                       # or: dbt build --empty
VERICTO_API_KEY=vtro_... vericto-dbt check                      # table, exit 1 on BLOCKED
vericto-dbt check --fail-on flagged --format sarif --output vericto.sarif
python -m vericto dbt check --format json                       # same command
```

Reads `target/manifest.json` (and `target/compiled`, or `target/run` with
`--sql-source run`) and checks, for the root project, the compiled SQL of every
model and snapshot, their `pre-hook`/`post-hook` statements and the
`on-run-start`/`on-run-end` hooks. Seeds have no SQL; tests and analyses are
opt-in (`--resource-type`), packages too (`--include-packages`); `--select`
takes globs on name, unique id or path. Hooks that still contain Jinja (dbt
renders them only at run time) are listed as skipped. dbt is never run or
imported: only its artifacts are read.

- **Dialect** from the manifest's adapter: `snowflake`, `postgres`, `redshift`
  (as `postgres`, said in the output), `mysql`/`mariadb`, `sqlserver` (as
  `mssql`), `oracle`. Any other adapter stops with exit code 2 before anything is
  sent; `--dialect` overrides.
- **Exit codes**, as vericto-cli: `0` pass, `1` a finding at or above
  `--fail-on` (`blocked` default, `flagged`, `any`), `2` usage (no manifest,
  nothing compiled, unsupported adapter), `3` key/scope/monthly allowance, `4`
  Vericto unreachable.
- **Privacy**: if the workspace is in `sanitized` telemetry mode, literals are
  replaced by placeholders before sending (vericto-cli's sanitizer, ported); if
  that setting cannot be read nothing is sent unless `--allow-unsanitized`.
- `--database-id` / `VERICTO_DATABASE_ID` applies that database's sensitive-column
  tags. Runs carry GitHub/GitLab provenance and appear in the dashboard's CI reports.
- SARIF points at the model file (line 1: compiled lines do not map to the Jinja
  source; the message gives the compiled line). GitHub Actions example:
  [examples/dbt_github_actions.yml](https://github.com/vericto/vericto-python/blob/main/examples/dbt_github_actions.yml).

The command is `vericto-dbt`, not `vericto dbt`: `vericto` is vericto-cli's
binary, and a Python script of the same name would shadow it on `PATH`.

### Airflow

Airflow 3 (`vericto[airflow]` requires `apache-airflow>=3.0`). Airflow 2 is
[end-of-life upstream](https://airflow.apache.org/docs/apache-airflow/stable/installation/supported-versions.html)
since 2026-04-22 and is no longer tested. vericto 0.2.x remains installable from
PyPI for Python 3.9 and Airflow 2 users (`pip install "vericto[airflow]<0.3"`).

<!-- docs-check: skip needs Airflow and a connection; tests/test_airflow.py covers it -->
```python
from vericto.airflow import VerictoSQLExecuteQueryOperator

VerictoSQLExecuteQueryOperator(task_id="purge", conn_id="warehouse", sql="sql/purge.sql")
```

Or guard the SQL tasks you already have, in `airflow_local_settings.py` on the
DAG processor **and** the workers ([example](https://github.com/vericto/vericto-python/blob/main/examples/airflow_local_settings.py)):

<!-- docs-check: skip needs Airflow; examples/airflow_local_settings.py -->
```python
from vericto.airflow.policy import guard_sql_tasks


def task_policy(task):
    guard_sql_tasks(task)  # every common-sql operator (SQLExecuteQueryOperator, SQL*Check, subclasses)
```

- The **rendered** SQL is checked when the task runs (the policy installs the
  check as the task's `pre_execute` hook, after any existing one; the class and
  serialization are unchanged). Scripts are split with the hook's
  `split_sql_string` and every statement is evaluated before the first runs.
- **BLOCKED**: the task fails with `VerictoTaskBlocked` (a `VerictoBlocked` and an
  `AirflowFailException`, so it is not retried) and nothing runs. **Mask**: the
  rewrite runs. **FLAGGED / MONITORED**: run and are logged. **Unavailable**:
  `fail_mode="closed"` fails the task (retried like any error).
- **Dialect** from the connection type (`postgres`, `redshift` as postgres,
  `mysql`, `mssql`, `oracle`, `snowflake`, `sqlite`), `vericto_dialect` in the
  connection extra (for ODBC/JDBC), or `vericto_dialect=` on the operator. Others
  fail before running (`on_unsupported="skip"` runs them unchecked, with a warning).
- The guard is built from the worker's `VERICTO_*` environment on first use; DAG
  parsing never calls Vericto.

## Roadmap

- **Embedded evaluator**, and SQLite's `set_authorizer` as a second deny layer
  (`ATTACH`, `load_extension`).
- dbt: a run-time `on-run-start` macro; LlamaIndex.TS.

## Development

```bash
uv sync                      # Python 3.10+; uv.lock pins the dev tools
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests use a local stand-in for the Runtime API and the CI-check API
(pytest-httpserver), the real `sqlite3` module, SQLAlchemy on SQLite, and a fake
Snowflake connection (the real connector's statement splitter is used); nothing
connects to Snowflake in CI. The integration tests (`tests/test_langchain.py`,
`test_llamaindex.py`, `test_airflow.py`) skip unless their framework is
installed; CI runs each in its own job against the versions pinned in
`ci/requirements-*.txt` (Airflow with its official constraints file), with
scripted LLMs and no network calls:

```bash
uv venv /tmp/vericto-lc && uv pip install --python /tmp/vericto-lc -e . -r ci/requirements-langchain.txt
/tmp/vericto-lc/bin/python -m pytest tests/test_langchain.py
```

### Documentation examples

Every `python` block of this README and of `docs/frameworks/*.md` runs in CI
against `scripts/docs_stub.py`, a local stand-in for the Runtime API with the real
service's status codes and bodies (it is not the engine: its few rules are string
checks). Each block runs in its own interpreter and temporary directory; a block
marked `<!-- docs-check: skip REASON -->` is listed but not run, and
`<!-- docs-check: requires=module -->` skips it unless the module is installed
(`--strict` makes that a failure, as in the framework jobs).

```bash
uv run python scripts/check_docs.py README.md docs/frameworks/dbapi.md docs/frameworks/sqlalchemy.md
uv venv /tmp/vericto-docs-lc && uv pip install --python /tmp/vericto-docs-lc -e . -r ci/requirements-docs-langchain.txt
/tmp/vericto-docs-lc/bin/python scripts/check_docs.py --strict docs/frameworks/langchain.md
```

`python scripts/docs_stub.py` runs the stand-in on its own and prints its URL
(`VERICTO_API_URL`; any `vtro_...` key works, `vtro_revoked` answers 401).

### Coverage

CI measures line and branch coverage and fails a job under **80%**
(`fail_under` in `pyproject.toml`). The core job reports every module except the
three integrations, whose framework it does not install; each integration job
reports its own module (`langchain.py`, `llamaindex.py`, `dbt.py`, `airflow/`).
The same checks locally:

```bash
uv run coverage run -m pytest
uv run coverage report --omit="*/vericto/langchain.py,*/vericto/llamaindex.py,*/vericto/airflow/*"
uv pip install --python /tmp/vericto-lc coverage==7.17.0
/tmp/vericto-lc/bin/python -m coverage run -m pytest tests/test_langchain.py
/tmp/vericto-lc/bin/python -m coverage report --include=src/vericto/langchain.py
```

A test that documents a known bug is marked `xfail(strict=True)` with the reason,
so it fails the run as soon as the bug is fixed and the marker must go.

### Live tests

`tests/live/` holds opt-in tests against real services. They skip unless their
variables are set and never run in CI.

- `test_snowflake_live.py`: a real Snowflake account (its own temporary schema,
  dropped at the end) with the local Runtime API stand-in. Set
  `VERICTO_LIVE_SNOWFLAKE=1` and the `SNOWFLAKE_*` variables it lists.
- `test_sqlite_live.py`: a real SQLite file in a temporary directory, guarded by
  `vericto.sqlite.connect()` against a **real Vericto Runtime API**. Benign
  statements (`CREATE TABLE`, `INSERT` with `?` binds, `executemany`, UPSERT
  `ON CONFLICT ... DO UPDATE`, `SELECT`, `DELETE ... WHERE`) must run; `DELETE`
  without `WHERE` (VERICTO-001), `ATTACH DATABASE` (VERICTO-098),
  `load_extension()` (VERICTO-100) and `VACUUM INTO` (VERICTO-101) must raise
  `VerictoBlocked` before SQLite sees them (checked with SQLite's trace callback,
  the file's SHA-256 and the absence of the attached/copy files);
  `PRAGMA foreign_keys = OFF` (VERICTO-099) must run with a `VerictoFlaggedWarning`;
  an `executescript` with a blocked statement in the middle must run none of it.
  The database's workspace must use the default rules; a run makes about 30
  evaluations, counted against the workspace's monthly quota.

| Variable | Value |
|---|---|
| `VERICTO_LIVE_SQLITE` | `1` |
| `VERICTO_API_KEY` | a `vtro_...` key with the `proxy:evaluate` scope |
| `VERICTO_API_URL` | `https://api-staging.vericto.com` (or another server that lists `sqlite`) |
| `VERICTO_DATABASE_ID` | the id of a database of dialect `sqlite` in that workspace |

On the host (Python 3.10+, the SQLite file goes to pytest's temporary directory):

```bash
export VERICTO_LIVE_SQLITE=1 VERICTO_API_URL=https://api-staging.vericto.com
export VERICTO_API_KEY=vtro_... VERICTO_DATABASE_ID=...
uv run pytest tests/live/test_sqlite_live.py -v
```

Or in a throwaway `python:3.13-slim` container (the repository is mounted
read-only and copied; the SQLite file never leaves the container):

```bash
docker run --rm -v "$PWD":/src:ro \
  -e VERICTO_LIVE_SQLITE=1 -e VERICTO_API_KEY -e VERICTO_API_URL -e VERICTO_DATABASE_ID \
  python:3.13-slim sh -c 'cp -r /src /work && cd /work && rm -rf .venv \
    && pip install --quiet --no-cache-dir . pytest==9.1.1 pytest-httpserver==1.2.0 \
    && python -m pytest tests/live/test_sqlite_live.py -v -p no:cacheprovider'
```

A skipped run (`1 skipped` per test) means a variable is missing.

## Contributing

See [CONTRIBUTING.md](https://github.com/vericto/vericto-python/blob/main/CONTRIBUTING.md)
for the checks CI runs, the coverage gate, and how releases are cut.

## Security

See [SECURITY.md](https://github.com/vericto/vericto-python/blob/main/SECURITY.md). Report vulnerabilities to security@vericto.com,
not in public issues.

`vericto` accepts `urllib3>=1.26.18,<3` so it fits the environments it is
installed into, but use **urllib3 2.8.0 or later**: older releases are affected by
GHSA-pq67-6m6q-mj2v, GHSA-gm62-xv2j-4w53, GHSA-2xpw-w6gg-jr37,
GHSA-38jv-5279-wg99, GHSA-qccp-gfcp-xxvc, GHSA-8988-9cw3-xx77 and
GHSA-vxq7-64xx-v4gw. The repository's dev lock (`uv.lock`) pins 2.8.0.

## License

Open source under the [Apache License 2.0](https://github.com/vericto/vericto-python/blob/main/LICENSE) (SPDX `Apache-2.0`) since
0.4.0. Versions 0.3.2 and earlier were released under the Elastic License 2.0
and remain under it. Copyright 2026 Vericto S.A.S.; see [NOTICE](https://github.com/vericto/vericto-python/blob/main/NOTICE). The
license grants no right to the Vericto name or logo: see
[TRADEMARKS.md](https://github.com/vericto/vericto-python/blob/main/TRADEMARKS.md). The Vericto service the SDK calls is offered
under its own [terms](https://vericto.com/terms).

---

## En español

`vericto` valida cada sentencia SQL con Vericto **antes** de ejecutarla, envolviendo
el `execute()` del driver: `snowflake-connector-python`, `sqlite3`, SQLAlchemy
(cualquier dialecto) o cualquier driver DB-API.

- **BLOCKED**: lanza `VerictoBlocked` (con hallazgos y corrección sugerida) y la
  sentencia no llega a la base de datos.
- **FLAGGED / MONITORED**: se ejecuta y se registra (logger `vericto`, callback
  `on_verdict`).
- **Máscara de columnas sensibles**: se ejecuta `rewritten_query`, nunca la original.
- **Sin respuesta de Vericto**: `fail_mode="closed"` (por defecto) bloquea;
  `"open"` ejecuta y emite `VerictoFailOpenWarning`. Los errores de configuración
  (clave inválida, permiso, dialecto) nunca se dejan pasar.

Configuración: `VERICTO_API_KEY` (clave `vtro_...` con el scope `proxy:evaluate`),
`VERICTO_DATABASE_ID` y, opcionalmente, `VERICTO_API_URL`, `VERICTO_FAIL_MODE`,
`VERICTO_TIMEOUT` y `VERICTO_CA_BUNDLE`. Los valores de los parámetros nunca se
envían a Vericto.

Integraciones oficiales: `vericto.langchain` y `vericto.llamaindex` (un
`SQLDatabase` que valida cada consulta del agente; si se bloquea, el agente recibe
la regla y la corrección sugerida en vez de una excepción), `vericto.sqlalchemy`,
`vericto.wrap` para cualquier driver DB-API, `vericto-dbt check` (valida el SQL
compilado de un proyecto dbt en CI con la API de CI-check, con salida JSON/SARIF)
y `vericto.airflow` (operador y política de clúster). Para Django, CrewAI, el
OpenAI Agents SDK, Pydantic AI y Vanna no hay módulo de vericto: las guías de
[docs/frameworks](https://github.com/vericto/vericto-python/blob/main/docs/frameworks/README.md) muestran el patrón (llamar a
`guard.prepare(sql, dialecto)` justo antes de ejecutar y ejecutar lo que
devuelve), con ejemplos probados y las versiones usadas. Ver
[Frameworks](#frameworks).

La API es síncrona: en código `asyncio`, use
`await asyncio.to_thread(guard.prepare, sql, "postgres")`. `VERICTO_TIMEOUT` es
por intento; con los valores por defecto (5 s y 2 reintentos), Vericto responde o
falla cerrado en unos 16 s como máximo. Una variable `VERICTO_*` vacía cuenta como
no definida (desde 0.4.1 también `VERICTO_FAIL_MODE`).

Desde 0.3.0, `vericto` requiere Python 3.10+ y `vericto[airflow]` requiere
Airflow 3: Airflow 2 llegó al fin de su ciclo de vida
([versiones soportadas](https://airflow.apache.org/docs/apache-airflow/stable/installation/supported-versions.html))
y ya no se prueba. Quienes usen Python 3.9 o Airflow 2 pueden seguir instalando
0.2.x (`pip install "vericto<0.3"`). Se recomienda urllib3 2.8.0 o posterior (ver
[Security](#security)).

SQLite: el servidor de Vericto evalúa el dialecto `sqlite` (vericto-engine
4.1.0+). `vericto.sqlite.connect()` igual consulta `GET /api/v1/meta/config` y,
si un servidor no lo anuncia, falla con `VerictoUnsupportedDialect` sin ejecutar
nada.

Licencia: código abierto bajo la [Apache License 2.0](https://github.com/vericto/vericto-python/blob/main/LICENSE) desde 0.4.0. Las
versiones 0.3.2 y anteriores se publicaron bajo la Elastic License 2.0 y siguen
bajo ella. La licencia no otorga derechos sobre la marca Vericto
([TRADEMARKS.md](https://github.com/vericto/vericto-python/blob/main/TRADEMARKS.md)).
