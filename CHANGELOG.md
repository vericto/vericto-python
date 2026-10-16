# Changelog

All notable changes to this package are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-16

Integrations for LangChain, LlamaIndex, dbt and Airflow, each a thin layer over
the guard. Nothing changes for 0.1.0 code.

### Added

- `vericto[langchain]`: `vericto.langchain.VerictoSQLDatabase`, a
  `langchain_community` `SQLDatabase` whose queries (and table-info sample rows)
  are checked; a blocked query returns an explanation with the rule and the
  suggested fix to the agent (`on_block="return"`, default) or raises
  (`on_block="raise"`). Masked queries run rewritten. Plus
  `VerictoCheckSQLTool`, a check-only tool.
- `vericto[llamaindex]`: `vericto.llamaindex.VerictoSQLDatabase` for
  `NLSQLTableQueryEngine` / `SQLTableRetrieverQueryEngine`, same behaviour.
- `vericto[dbt]`: the `vericto-dbt check` command (also
  `python -m vericto dbt check`). It reads `target/manifest.json` after
  `dbt compile`, checks compiled models, snapshots and hooks with the CI-check
  API (`POST /api/v1/ci/check-key`, monthly CI-check allowance), maps the
  adapter to the dialect, prints a table or writes JSON / SARIF, and exits as
  vericto-cli does (`--fail-on blocked|flagged|any`). It honours the workspace's
  `sanitized` telemetry mode with a port of vericto-cli's literal sanitizer.
- `vericto[airflow]`: `vericto.airflow.VerictoSQLExecuteQueryOperator` and the
  cluster policy `vericto.airflow.policy.guard_sql_tasks`. Both check the rendered
  SQL, statement by statement, before any of it runs. A block fails the task
  without retries (`VerictoTaskBlocked`). The dialect comes from the connection
  type or from `vericto_dialect` in the connection extra. Tested on Airflow 3.3.2
  and 2.11.2.
- `vericto.sqlalchemy.is_instrumented(engine)`.
- `vericto.ci_api.CiCheckClient`, a client for the CI-check API.

- `tests/live/test_sqlite_live.py`: an opt-in end-to-end test of
  `vericto.sqlite` against a real Runtime API (allowed statements, UPSERT, the
  SQLite rules VERICTO-098/099/100/101, DELETE without WHERE, scripts with a
  blocked statement). Skipped unless `VERICTO_LIVE_SQLITE=1` and the
  `VERICTO_*` variables are set; see "Live tests" in the README.

### Changed

- `copy.deepcopy` of a `Vericto` guard returns the same guard, so frameworks
  that deep-copy the objects holding it (Airflow operators) keep working.

- Documentation: the Vericto server now evaluates the `sqlite` dialect
  (vericto-engine 4.1.0; `GET /api/v1/meta/config` lists it), so
  `vericto.sqlite` and SQLAlchemy/LangChain/LlamaIndex/Airflow on SQLite work
  against it. The 0.1.0 known limitation no longer applies to such servers.
  Unchanged behaviour: the wrappers still ask `/meta/config` and raise
  `VerictoUnsupportedDialect`, before any SQL runs, against a server that does
  not list `sqlite`.

## [0.1.0] - never published

First release: Vericto checks in front of Python database drivers, evaluated by
the Vericto Runtime API (`POST /api/v1/proxy/query`).

### Added

- `Vericto` guard: raises `VerictoBlocked` on BLOCKED, runs the masked
  `rewritten_query` for sensitive columns, logs FLAGGED / MONITORED / allowed
  PARSE_ERROR verdicts and passes every verdict to `on_verdict`.
- Fail mode per guard: `closed` (default) raises `VerictoUnavailable` when
  Vericto cannot answer; `open` runs the statement and emits
  `VerictoFailOpenWarning`. Configuration errors are never failed open.
- `RuntimeApiEvaluator`: `VERICTO_*` environment configuration as in vericto-cli,
  HTTPS-only key handling, timeouts, retries with backoff on 5xx / 429 rate
  limits, `VerictoQuotaExceeded` for the monthly quota, and dialect discovery
  through `GET /api/v1/meta/config`.
- `Evaluator` protocol, so an embedded evaluator can be added later.
- Wrappers: `vericto.snowflake` (`execute`, `executemany`, `execute_async`,
  `execute_string`, `execute_stream`), `vericto.sqlite` (a `sqlite3.Connection`
  subclass; `execute`, `executemany`, `executescript`), `vericto.sqlalchemy`
  (`before_cursor_execute` for any dialect) and the generic `vericto.wrap` for
  DB-API drivers.
- Scripts are evaluated statement by statement before the first one runs.
- `%s` / `%(name)s` placeholders are evaluated as the dialect's own placeholders;
  parameter values are never sent.
- Optional verdict cache (off by default).

### Known limitations

- The Vericto engine does not evaluate the `sqlite` dialect yet: the SQLite
  wrappers raise `VerictoUnsupportedDialect` until the server lists it.

[0.2.0]: https://github.com/vericto/vericto-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/vericto/vericto-python/releases/tag/v0.1.0
