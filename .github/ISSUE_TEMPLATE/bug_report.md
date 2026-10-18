---
name: Bug report
about: Something the SDK gets wrong with a driver, an ORM, an integration or the Vericto API
title: "[BUG] "
labels: bug
---

<!--
Security vulnerabilities: do not open an issue. Report them privately, see
https://github.com/vericto/vericto-python/security/policy

Rules and SQL parsing live in vericto-engine. If a specific statement got the wrong
verdict, you can report it here or in vericto-engine; we move issues to the right
repository.
-->

**What happened**
A clear description of what went wrong, and what you expected instead.

**Driver or integration**
- [ ] snowflake-connector-python (`vericto.snowflake`)
- [ ] sqlite3 (`vericto.sqlite`)
- [ ] SQLAlchemy (`vericto.sqlalchemy`)
- [ ] Another DB-API driver (`vericto.dbapi.wrap`)
- [ ] LangChain / LlamaIndex
- [ ] dbt (`vericto-dbt`)
- [ ] Airflow

Driver, ORM or framework and its version (for example `psycopg 3.2`,
`SQLAlchemy 2.0.54 + pymysql`, `apache-airflow 3.3.2`):

**Dialect**
- [ ] postgres
- [ ] mysql
- [ ] oracle
- [ ] mssql
- [ ] snowflake
- [ ] sqlite

**Environment**
- SDK version (`python -c "import vericto; print(vericto.__version__)"`):
- Python version (`python --version`) and OS:
- `fail_mode` (`closed` or `open`):
- `VERICTO_API_URL`, if not the default. Remove API keys before pasting.

**Steps to reproduce**
A minimal script, with the API key and connection details removed.
```python
import vericto
```

**SQL statement (if a query was blocked or allowed when it should not have been)**
```sql
-- the exact statement
```

**Traceback or logs**
Output of the `vericto` logger and the full traceback. Remove anything sensitive.
```
paste relevant output here
```
