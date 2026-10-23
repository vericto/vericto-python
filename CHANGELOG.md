# Changelog

All notable changes to this package are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.1] - 2026-10-23

Documentation release, with one small fix. No other behavior change.

### Fixed

- An empty or whitespace-only `VERICTO_FAIL_MODE` now counts as unset and means
  the documented default, `closed`. 0.4.0 raised `VerictoConfigError` for
  `VERICTO_FAIL_MODE=` (for example a CI variable that expands to nothing), as
  vericto-node did before its 0.2.2; every other `VERICTO_*` variable already
  fell back to its default. An invalid non-empty value still raises, and the
  error now names `VERICTO_FAIL_MODE` when the value came from it.

### Added

- **Framework guides** in `docs/frameworks/`, each with a runnable example and
  the versions it was tested with: LangChain (incl. LangChain 1.x
  `create_agent`), LlamaIndex, SQLAlchemy and any DB-API driver (official
  integrations), and Django, CrewAI, the OpenAI Agents SDK, Pydantic AI and
  Vanna 2.x (patterns: these frameworks have no vericto module; the guide shows
  where to call the guard).
- `scripts/docs_stub.py`, a local stand-in for the Runtime API with the
  service's status codes and response bodies, and `scripts/check_docs.py`,
  which runs every `python` block of the README and the guides against it. New
  CI job `Docs examples (...)`: the core examples in the dev environment, each
  framework guide against its pins in `ci/requirements-docs-*.txt`.

### Changed

- README: a **Core API** section (`check`, `enforce`, `prepare`, `enforce_all`,
  `require_dialect`, the `Verdict` fields), **Timeouts and retries** (the
  timeout is per attempt; the worst case before `VerictoUnavailable` is about
  `(max_retries + 1) × timeout` plus backoff, ~15.6 s with the defaults), **Sync
  and async** (the API is synchronous; use `asyncio.to_thread` in async code;
  `instrument()` on an `AsyncEngine` blocks the loop while it checks), a
  runnable fail-closed / fail-open example, and a **Frameworks** table that says
  which integrations are official and which are patterns. Corrected: the CA
  bundle is an argument of `RuntimeApiEvaluator`, not of `Vericto`. The Spanish
  section is updated to match.
- Docstrings for the public API that had none (`Verdict` fields,
  `Verdict.blocked`, `Vericto.clear_cache`, `RuntimeApiEvaluator.evaluate` and
  `close`, the DB-API, sqlite3 and Snowflake wrappers' entry points, the
  LangChain and LlamaIndex properties).
- The sdist ships `docs/` and `scripts/` and no longer lists a `README.es.md`
  that does not exist.
- Dev lock (`uv.lock`) refreshed with compatible updates (SQLAlchemy 2.1.6,
  langchain-core 1.7.0, coverage 7.17.0, ruff 0.17.2, among others). The tests
  now run against snowflake-connector-python 5.1.0 on Python 3.11+ (within the
  `snowflake` extra's `>=3.0`; 4.8.0 stays on 3.10, which 5.x does not support),
  which also drops filelock 3.32.7 (CVE-2026-112907, -112913, -112940) from the
  3.11+ environments; `vericto.snowflake` works unchanged with both (its import
  of the connector's splitter only gained a type-checker annotation). CI pins
  langchain-core 1.7.0, SQLAlchemy 2.1.6 and coverage 7.17.0.

## [0.4.0] - 2026-10-18

**Now open source under the Apache License 2.0.** From this version on,
`vericto` is licensed under the Apache License, Version 2.0 (SPDX `Apache-2.0`)
instead of the Elastic License 2.0. **All previously released versions, 0.3.2
and earlier, remain under the Elastic License 2.0.** No code change: the
package behaves exactly as 0.3.2.

### Changed

- **License: Apache-2.0.** `LICENSE` is the Apache License 2.0 text from
  apache.org, verbatim. New `NOTICE` (product name and copyright, Vericto
  S.A.S.), which the license asks redistributors to keep, and
  `TRADEMARKS.md`: the license grants no right to the Vericto name or logo.
- **Package metadata (PEP 639):** `license = "Apache-2.0"` and
  `license-files = ["LICENSE", "NOTICE"]`, so the wheel and the sdist carry
  `License-Expression: Apache-2.0` and both files. The build needs
  hatchling 1.27 or later, the first with full PEP 639 support. There is no
  license classifier, as PEP 639 asks when a license expression is given.
- Every Python file starts with `SPDX-FileCopyrightText` and
  `SPDX-License-Identifier: Apache-2.0` lines (the REUSE convention).
- README (License section, in English and Spanish) and CONTRIBUTING name the
  new license. Contributions are accepted under the Apache License 2.0
  (inbound = outbound, its section 5); no contributor agreement is needed.

## [0.3.2] - 2026-10-18

A security release for `vericto-dbt`'s **sanitized mode** (a workspace whose
telemetry query mode is `sanitized`). `vericto.sanitize` mirrors vericto-cli
1.8.2, which fixes the same leaks: in the cases below, part of a literal was
sent as written although the mode promises that no literal value leaves the
machine. The sanitizer is now a lexer that ends every literal where the
dialect's own lexer ends it, and it fails closed. Nothing else changes in the
package; a workspace in `raw` mode is not affected.

### Security

- **Snowflake backslash escapes**: `'it\'s secret'` ended at `\'` and sent
  `s secret`. Snowflake strings now honor backslash escapes.
- **MySQL with `NO_BACKSLASH_ESCAPES`**: only the default escape mode was read,
  so on such a server `'C:\' AND q = 'secret'` sent `secret`. A MySQL statement
  with a backslash is now read in both modes: a reading that leaves something
  unterminated is dropped, and where both are possible and disagree, whatever
  is a literal in either is replaced.
- **MySQL `"..."` strings** were copied as identifiers, value included.
- **Oracle `q'[...]'`, `q'{...}'`, `q'(...)'`, `q'<...>'`, `q'!...!'`** (any
  delimiter) **and `nq'...'`**: their content was sent from its first inner
  quote on.
- **Dollar-quoted strings and bodies** (`$$...$$`, `$tag$...$tag$`) were copied
  with the literals inside. A dollar-quoted string is now a literal; after `AS`
  or `DO` it is a function or `DO` body, which the engine flags without reading,
  and becomes the empty body `$$$$` so the statement still parses.
- **Comments the dialect reads differently** opened a literal at a `'` inside
  them, so the real literal after it was sent: MySQL `#`, Snowflake `//`,
  nested PostgreSQL and MSSQL `/* */`. MySQL `5--'x'` (not a comment there)
  and the content of `/*! ... */`, which MySQL executes, are now sanitized.
- **Prefixes and numbers**: the `N`, `U&`, `X`, `B` and `_utf8mb4` prefixes
  stayed in front of the placeholder, and `0x4142`, `1e10`, `.5`, MSSQL
  `$12.50` and a unary sign were sent in part. Each is now one placeholder.
- **Fail closed**: an unterminated quoted identifier, comment or dollar quote
  was copied to the end as written. Now everything from where the text cannot
  be read with certainty to the end is one placeholder.

### Fixed

- Oracle `:1` became `:$1` and MSSQL `[col 1]` became `[col $1]`: bind
  parameters (`:1`, `:name`) and bracketed identifiers are kept as written.
- `id = $1 AND n = 5` became `id = $1 AND n = $1`, one parameter for two
  values. New placeholders are numbered after the highest `$N` (and Oracle
  `:N`) already in the statement.
- A digit after a non-ASCII letter was read as a number: `café2` became
  `café$1`. Identifiers in any script are now kept as written.
- The dialect name is matched case-insensitively.

### Tests

- `tests/fixtures/sanitize/vericto-cli-1.8.2.json` replaces the 1.8.0 file: it
  is vericto-cli 1.8.2's `tests/fixtures/sanitize.json` byte for byte (133
  vectors, run by both test suites). A seeded property test puts a secret marker
  inside literals of every kind in random statements across seven dialects and
  checks that none comes out; other tests cover non-ASCII text and linear time.
  A differential run against vericto-cli 1.8.2's `sanitize.rs` (214,133 inputs:
  the fixture, generated statements, mutated vectors and random Unicode) gave
  identical output for every input.

## [0.3.1] - 2026-10-18

A bug-fix release: `vericto-dbt`'s sanitized mode follows vericto-cli 1.8.0
again. Nothing else changes in the package.

### Fixed

- **Sanitized mode** (`vericto-dbt` in a workspace whose telemetry query mode is
  `sanitized`): `vericto.sanitize` now applies vericto-cli 1.8.0's rules, so a
  statement reads the same as with `vericto check`:
  - Bind parameters already in the statement are kept. The digits of `$1` and
    `?1` were read as a numeric literal, so `$1` (a PostgreSQL parameter, a
    Snowflake stage column) became `$$1`, which opens a dollar-quoted string:
    the server evaluated different SQL or answered `PARSE_ERROR`.
  - Snowflake and SQLite get `?` placeholders, as MySQL does (they got `$1, $2,
    ...`). On SQLite, `[name]` identifiers are copied as they are and `$name`
    is a bind parameter, not a dollar quote.
- `tests/fixtures/sanitize/vericto-cli-1.8.0.json` pins vericto-cli's own
  sanitizer test vectors (`src/sanitize.rs` at 1.8.0), and the port must give
  the same output for each; per-dialect cases cover PostgreSQL, MySQL, Oracle,
  MSSQL, Snowflake and SQLite. The two `xfail` tests for this bug now pass.

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

[0.4.1]: https://github.com/vericto/vericto-python/releases/tag/v0.4.1
[0.4.0]: https://github.com/vericto/vericto-python/releases/tag/v0.4.0
[0.3.2]: https://github.com/vericto/vericto-python/releases/tag/v0.3.2
[0.3.1]: https://github.com/vericto/vericto-python/releases/tag/v0.3.1
[0.3.0]: https://github.com/vericto/vericto-python/releases/tag/v0.3.0
[0.2.0]: https://github.com/vericto/vericto-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/vericto/vericto-python/releases/tag/v0.1.0
