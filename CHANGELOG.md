# Changelog

All notable changes to this package are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Tests for the alternate flows: the Runtime API down, slow, rate-limited, over
  quota, answering 401/403/400 or a malformed body, under both fail modes; empty,
  oversized and multi-statement SQL; bind parameters in every DB-API paramstyle;
  `vericto.dbapi.wrap()` against a fake driver; Snowflake `execute_async`;
  the CI-check client; the shared LangChain/LlamaIndex helpers. Line coverage is
  99.8% and branch coverage 99.6% across the core and integration jobs (was 92.1%
  and 80.2%). No change to the package itself.
- CI fails a job under 80% line and branch coverage (`coverage` in the dev
  group, `[tool.coverage]` in `pyproject.toml`); see README, "Coverage".

### Known issues

- In a workspace in `sanitized` telemetry mode, `vericto-dbt` rewrites an
  existing `$1` (a PostgreSQL bind parameter, a Snowflake stage column) as `$$1`,
  which opens a dollar-quoted string, and uses `$N` placeholders for Snowflake
  where vericto-cli 1.8.0 uses `?`. The port has not picked up vericto-cli
  1.8.0's sanitizer rules yet; two `xfail` tests in `tests/test_sanitize.py` track it.

## [0.3.0] - 2026-10-18

A support-policy release: Python 3.9 and Airflow 2 are dropped. The API is
unchanged; 0.2.x code runs as is on Python 3.10+ and Airflow 3.

### Changed

- **BREAKING**: `requires-python` is now `>=3.10`. Python 3.9 reached its end of
  life in October 2025, and no patched release of the affected dependencies
  (urllib3, cryptography, requests, filelock, pytest,
  snowflake-connector-python) supports it.
- **BREAKING**: the `airflow` extra requires `apache-airflow>=3.0` (was `>=2.9`).
  Airflow 2.x reached its end of life upstream on 2026-04-22
  ([supported versions](https://airflow.apache.org/docs/apache-airflow/stable/installation/supported-versions.html):
  "EOL versions will not get any fixes or support"), and its open advisories are
  fixed only in 3.x. CI no longer tests Airflow 2.11 (`ci/airflow-2/` and its
  Dependabot entry are removed); Airflow 3.3.2 is still tested.
- vericto 0.2.x stays available on PyPI for Python 3.9 and Airflow 2 users
  (`pip install "vericto<0.3"`).
- The core test matrix is Python 3.10, 3.12 and 3.14.

### Security

- The dev lock (`uv.lock`) is regenerated without the Python 3.9 resolution, so
  it pins only patched versions: urllib3 2.8.0 (was 1.26.20 on 3.9),
  cryptography 50.0.2 (47.0.0), snowflake-connector-python 4.8.0 (4.5.0),
  requests 2.34.2 (2.32.5), filelock 3.32.7 (3.19.1) and pytest 9.1.1 (8.4.2).
  This closes GHSA-pq67-6m6q-mj2v, GHSA-gm62-xv2j-4w53, GHSA-2xpw-w6gg-jr37,
  GHSA-38jv-5279-wg99, GHSA-qccp-gfcp-xxvc, GHSA-8988-9cw3-xx77 and
  GHSA-vxq7-64xx-v4gw (urllib3), GHSA-537c-gmf6-5ccf, GHSA-g6cj-pr64-35w5,
  GHSA-jwv3-5hgf-82ww and GHSA-m2h6-j472-rp4c (cryptography),
  GHSA-5cc2-282f-jjq2 and GHSA-qqj6-54q6-cxv6 (snowflake-connector-python),
  GHSA-gc5v-m9x4-r6x2 (requests), GHSA-w853-jp5j-5j7f and GHSA-qmgc-5h2g-mvrw
  (filelock) and GHSA-6w46-j5rx-g56g (pytest).
- Removing `ci/airflow-2/requirements.txt` closes its 27 apache-airflow
  advisories and its pytest advisory (GHSA-6w46-j5rx-g56g), all fixed only in
  Airflow 3.x / pytest 9.
- The README recommends urllib3 2.8.0 or later; the runtime range
  (`urllib3>=1.26.18,<3`) is unchanged.
- Remaining: nltk 3.10.3 (GHSA-8mgp-746c-j5xp, no patched release), locked only
  as a transitive dependency of llama-index-core (the optional `llamaindex`
  extra); vericto does not import it.

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

[0.3.0]: https://github.com/vericto/vericto-python/releases/tag/v0.3.0
[0.2.0]: https://github.com/vericto/vericto-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/vericto/vericto-python/releases/tag/v0.1.0
