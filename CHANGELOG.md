# Changelog

All notable changes to this package are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - unreleased

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

[0.1.0]: https://github.com/vericto/vericto-python/releases/tag/v0.1.0
