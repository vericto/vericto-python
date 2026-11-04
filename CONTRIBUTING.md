# Contributing to vericto (Python)

Thanks for your interest in improving the Vericto SDK for Python. This guide
covers the `vericto` package in this repository, which is open source under the
[Apache License 2.0](LICENSE).

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be
respectful, constructive, and technically precise.

## What belongs here

The SDK wraps a driver's `execute()` and asks the Vericto Runtime API for a
verdict before each statement runs. Contributions that fit here:

- **Driver wrappers** — `snowflake-connector-python`, `sqlite3`, SQLAlchemy and
  any DB-API driver (`src/vericto/snowflake.py`, `sqlite.py`, `sqlalchemy.py`,
  `dbapi.py`), placeholders and parameters.
- **The API clients** — the Runtime API (`runtime_api.py`), the CI-check API
  (`ci_api.py`), retries, fail mode and caching (`guard.py`).
- **Integrations** — LangChain, LlamaIndex, dbt (`vericto-dbt`) and Airflow.
- **Sanitized mode** — `sanitize.py`, which must give the same output as
  vericto-cli's sanitizer; `tests/fixtures/sanitize/` pins the CLI's test
  vectors.
- **Examples and documentation** — `examples/` and `README.md`.

Contributions that belong elsewhere:

- A statement that got the wrong verdict, a new rule, SQL parsing →
  [vericto-engine](https://github.com/vericto/vericto-engine).
- The Node.js SDK → [vericto-node](https://github.com/vericto/vericto-node).
- The `vericto` command line tool →
  [vericto-cli](https://github.com/vericto/vericto-cli).

## Getting started

```bash
git clone https://github.com/<you>/vericto-python.git
cd vericto-python
uv sync                      # Python 3.11+; uv.lock pins the dev tools
uv run pytest
```

CI uses uv 0.10.8. Tests use a local stand-in for the Runtime API and the
CI-check API (pytest-httpserver), the real `sqlite3` module, SQLAlchemy on
SQLite and a fake Snowflake connection; nothing connects to Snowflake or to the
Vericto API. The tests under `tests/live/` are opt-in and never run in CI (see
the README, "Live tests").

## Development workflow

1. Fork the repo and create a branch: `feat/psycopg-wrapper` or
   `fix/sqlite-executescript`.
2. Make your change with tests.
3. Run the checks CI runs before opening a PR:
   ```bash
   uv sync --locked
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy
   uv run coverage run -m pytest
   uv run coverage report --omit="*/vericto/langchain.py,*/vericto/llamaindex.py,*/vericto/airflow/*"
   uv run python scripts/check_docs.py --strict README.md docs/frameworks/dbapi.md docs/frameworks/sqlalchemy.md
   uv build
   ```
   A change to a framework guide: run it against its pins too (README,
   "Documentation examples").
4. Open a PR using the template. Keep PRs focused and under ~500 lines.

### Coverage

CI measures line and branch coverage and fails a job under **80%**
(`fail_under` in `pyproject.toml`). The `Test (Python ...)` jobs run the two
`coverage` commands above, which report every module except the three
integrations whose framework they do not install. Each `Integration (...)` job
installs its framework at the version pinned in `ci/requirements-*.txt` and
gates its own module. To run one locally (LangChain here):

```bash
uv venv /tmp/vericto-lc && uv pip install --python /tmp/vericto-lc -e . -r ci/requirements-langchain.txt
uv pip install --python /tmp/vericto-lc coverage==7.17.0
/tmp/vericto-lc/bin/python -m coverage run -m pytest tests/test_langchain.py
/tmp/vericto-lc/bin/python -m coverage report --include=src/vericto/langchain.py
```

A test that documents a known bug is marked `xfail(strict=True)` with the
reason, so it fails the run as soon as the bug is fixed and the marker must go.

## Commit convention

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
test: cover the alternate flows and gate CI at 80% line and branch coverage
ci: a public repository never runs on self-hosted runners
chore(deps): bump the ci group across 1 directory with 2 updates
release: 0.3.1, sanitized mode follows vericto-cli 1.8.0
```

**Write in English** — commit subjects and bodies, code comments, test names,
identifiers, `CHANGELOG.md` entries and pull request descriptions.

## Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/). Add an entry
under `## [Unreleased]` for any change a user of the package can see, in the
section that fits (`Added`, `Changed`, `Fixed`, `Removed`, `Security`). Changes
to repository files that do not ship in the sdist or the wheel (CI, templates,
this guide) need no entry. Maintainers move the entries into a version section
when they cut a release.

## Pull request acceptance criteria

- ✅ Required checks pass: `Lint and type-check`, `Test (Python 3.11)`,
  `Test (Python 3.12)`, `Test (Python 3.14)`, `Build sdist and wheel` and
  `gitleaks`. The `Integration (...)` and `Docs examples (...)` jobs run on
  every pull request too and should be green.
- ✅ Code in the README and `docs/frameworks/` runs (`scripts/check_docs.py`), or
  is marked `<!-- docs-check: skip REASON -->` with a reason.
- ✅ Coverage stays at or above 80% in every job.
- ✅ New behavior has tests, including the failure path (`fail_mode`, API errors).
- ✅ `CHANGELOG.md` updated under `[Unreleased]` when a user can see the change.
- ✅ At least one maintainer review (CODEOWNERS requests it).
- ✅ Parameter values are never sent to Vericto.
- ✅ The Contributor License Agreement is signed (`CLA` check).

Pull requests are merged with a merge commit; squash and rebase merges are
disabled.

## Releases

Releases are cut by the maintainers:

1. Set `__version__` in `src/vericto/_version.py` and turn `## [Unreleased]`
   into `## [X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`, in a pull request.
2. After it is merged, tag the merge commit `vX.Y.Z` and push the tag.
3. The tag starts `release.yml`: it checks that the tag matches
   `_version.py`, runs `ruff check`, `mypy` and `pytest`, builds the sdist and
   the wheel, publishes them to PyPI with trusted publishing (the `pypi`
   environment, which needs a maintainer's approval) and creates the GitHub
   release with the files and their `SHA256SUMS`.

Release tags (`v*.*.*`) are protected: once pushed, they cannot be moved or
deleted.

## Reporting security issues

Do **not** open a public issue for vulnerabilities. See [SECURITY.md](SECURITY.md)
or email [security@vericto.com](mailto:security@vericto.com).

## Contributor License Agreement

Before a pull request can be merged, every human commit author must sign the
Vericto Individual Contributor License Agreement, version 1.1
([CLA index](CLA.md), permanent text [legal/cla/1.1.md](legal/cla/1.1.md)). The
agreement lets Vericto distribute accepted contributions under the project's
public license and under separate commercial licenses while the contributor
keeps copyright.

Read the agreement and post this exact pull-request comment:

```
I have read the CLA Document and I hereby sign the CLA
```

The CLA workflow records your GitHub username and numeric account ID,
repository ID, pull-request number, comment ID, comment timestamp, CLA version
and SHA-256 document hash in `signatures/v1.1/cla.json` on the
`cla-signatures` branch. The data and retention are described in
[CLA_PRIVACY_NOTICE.md](CLA_PRIVACY_NOTICE.md). If the check does not rerun,
comment `recheck`. A commit whose email is not linked to a GitHub account
cannot be matched to a signer. Closed pull requests are locked to preserve the
signature comment.

If an employer, client or other person may own rights in your contribution,
obtain written authorization and send it to
[legal@vericto.com](mailto:legal@vericto.com) before submitting.

## License

vericto is open source under the [Apache License 2.0](LICENSE) since 0.4.0
(versions 0.3.2 and earlier remain under the Elastic License 2.0). A
contribution you submit for inclusion is under the Apache License 2.0, as its
section 5 says, and under the [Contributor License
Agreement](#contributor-license-agreement). Add the `SPDX-FileCopyrightText` and
`SPDX-License-Identifier: Apache-2.0` header to new source files. For questions
about licensing a contribution, write to
[legal@vericto.com](mailto:legal@vericto.com) before opening the pull request.