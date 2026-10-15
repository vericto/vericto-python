# vericto (Python)

> Check every SQL statement with [Vericto](https://vericto.com) **before** it runs:
> thin wrappers for `snowflake-connector-python`, `sqlite3`, SQLAlchemy and any
> DB-API driver.

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

- Python 3.9+, typed (`py.typed`), one dependency (`urllib3`).
- Drivers are optional extras; importing `vericto` never imports a driver.

## Install

```bash
pip install vericto                  # core + sqlite3 + generic DB-API wrapper
pip install "vericto[snowflake]"     # + snowflake-connector-python
pip install "vericto[sqlalchemy]"    # + SQLAlchemy
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
| `VERICTO_TIMEOUT` | Seconds per attempt | `5` |
| `VERICTO_CA_BUNDLE` | Extra CA bundle (PEM) to trust | system store |

Every variable has a keyword-argument equivalent on `Vericto(...)`. The key is
sent only in the `X-API-Key` header, only over HTTPS (plain HTTP is accepted for
`localhost` alone), and never appears in `repr()`, logs or exception messages.

## Quickstart

### Snowflake

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
import vericto.sqlite

conn = vericto.sqlite.connect("app.db")  # a real sqlite3.Connection subclass
conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
conn.executescript(open("migration.sql").read())
```

`execute`, `executemany` and `executescript` are checked on the connection and on
its cursors. `executescript` is split with `sqlite3.complete_statement` (SQLite's
own tokenizer, so triggers, strings and comments are handled) and evaluated whole
before it runs. See [SQLite status](#sqlite-status).

### SQLAlchemy (any dialect)

```python
from sqlalchemy import create_engine
import vericto.sqlalchemy

engine = create_engine("snowflake://...")  # or postgresql://, mysql://, oracle://, mssql://, sqlite://
remove = vericto.sqlalchemy.instrument(engine)  # AsyncEngine works too
```

A `before_cursor_execute` listener checks the compiled SQL of every statement
(ORM, Core and `text()`). Dialects map from `engine.dialect.name`: `snowflake`,
`postgresql` → `postgres`, `mysql`/`mariadb` → `mysql`, `oracle`, `mssql`,
`sqlite`. `VerictoBlocked` propagates unwrapped.

### Any DB-API driver, or by hand

```python
import psycopg
import vericto
from vericto import Vericto

guard = Vericto()  # one per process; thread-safe
conn = vericto.wrap(psycopg.connect("dbname=app"), dialect="postgres", guard=guard)

verdict = guard.enforce("SELECT email FROM customers LIMIT 10", "postgres")
run_this = verdict.sql_to_execute  # the masked rewrite, if any
```

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

Retries: connection errors, timeouts, 5xx and 429 `RATE_LIMIT_EXCEEDED` are
retried `max_retries` times (default 2) with exponential backoff and jitter
(capped at 2 s, honouring `Retry-After`). `QUOTA_EXCEEDED` is not retried.

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
enforcement point. The wrapper is ready, but the **Vericto engine does not
evaluate the `sqlite` dialect yet** (the server lists `postgres`, `mysql`,
`oracle`, `mssql`, `snowflake`). `vericto.sqlite.connect()` and
`vericto.sqlalchemy.instrument()` on a SQLite engine ask the server
(`GET /api/v1/meta/config`) and raise `VerictoUnsupportedDialect` before any SQL
runs while it is not listed. They start working, unchanged, when the server
adds it. There is deliberately no fallback to another dialect: evaluating SQLite
as PostgreSQL would give wrong answers.

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

## Integrations roadmap

Not implemented in 0.1.0; each is meant to be a thin layer over `Vericto.enforce`
or the SQLAlchemy hook:

- **LangChain / LlamaIndex SQL agents**: a `SQLDatabase` built on an
  instrumented SQLAlchemy engine already works today; a dedicated tool wrapper
  would surface `suggested_fix` back to the agent instead of an exception.
- **dbt**: an adapter plugin or `on-run-start` hook that checks compiled models
  (CI-time checks are already covered by `vericto check` in vericto-cli).
- **Airflow**: a hook for `SnowflakeHook`/`DbApiHook` that wraps `get_conn()`.
- **Embedded evaluator**, and SQLite's `set_authorizer` as a second deny layer
  (`ATTACH`, `load_extension`).

## Development

```bash
uv sync                      # Python 3.9+; uv.lock pins the dev tools
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests use a local stand-in for the Runtime API (pytest-httpserver), the real
`sqlite3` module, SQLAlchemy on SQLite, and a fake Snowflake connection (the real
connector's statement splitter is used); nothing connects to Snowflake in CI.
`tests/live/` holds an opt-in test against a real Snowflake account (its own
temporary schema, dropped at the end), skipped unless `VERICTO_LIVE_SNOWFLAKE=1`
and the `SNOWFLAKE_*` variables it lists are set.

## Security

See [SECURITY.md](SECURITY.md). Report vulnerabilities to security@vericto.com,
not in public issues.

## License

[Elastic License 2.0](LICENSE).

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
`VERICTO_TIMEOUT`. Los valores de los parámetros nunca se envían a Vericto.

SQLite: el wrapper está listo, pero el motor de Vericto todavía no evalúa el
dialecto `sqlite`; hasta que el servidor lo anuncie, `vericto.sqlite.connect()`
falla con `VerictoUnsupportedDialect` sin ejecutar nada.
