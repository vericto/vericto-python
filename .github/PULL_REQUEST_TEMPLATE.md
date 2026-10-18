## What does this PR change?

<!-- A clear, concise description of the change. -->

## Why?

<!-- Context: a driver or ORM incompatibility, a statement that ran without being checked, an integration bug, a new driver. A wrong verdict for a statement, a rule or SQL parsing belong in vericto-engine. -->

## Type of change

- [ ] Driver wrapper (Snowflake, sqlite3, SQLAlchemy, DB-API)
- [ ] Runtime API or CI-check client (retries, fail mode, caching)
- [ ] Integration (LangChain, LlamaIndex, dbt, Airflow)
- [ ] Sanitized mode
- [ ] Bug fix
- [ ] Documentation or examples
- [ ] Dependencies or CI

## Tests

- [ ] Added or updated tests that cover the change, including the failure path
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run mypy` passes
- [ ] `uv run coverage run -m pytest` and `uv run coverage report --omit=...` (as in CI) pass, at or above 80%
- [ ] The `Integration (...)` job's commands pass locally (if an integration changed)
- [ ] `uv build` succeeds

## Checklist

- [ ] Commits follow Conventional Commits
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (if a user of the package can see the change)
- [ ] New or changed options, environment variables or errors documented in `README.md`
- [ ] Parameter values are still never sent to Vericto
- [ ] CI is green
