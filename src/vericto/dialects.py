"""Dialect names as the Vericto API spells them."""

from __future__ import annotations

#: Dialects the Vericto engine evaluates today (vericto-engine 4.0.0). ``sqlite`` is
#: planned: the wrappers accept it, and evaluate it once the server lists it.
KNOWN_DIALECTS = ("postgres", "mysql", "oracle", "mssql", "snowflake", "sqlite")

_ALIASES = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",
    "psycopg": "postgres",
    "psycopg2": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "sql_server": "mssql",
    "snowflake": "snowflake",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
}


def normalize_dialect(name: str) -> str:
    """Map a driver or SQLAlchemy dialect name to the Vericto dialect id.

    Unknown names are passed through lower-cased: the server decides whether it
    evaluates them, and answers with a clear error if it does not.
    """
    key = name.strip().lower()
    # SQLAlchemy names can carry the driver: "postgresql+psycopg", "mysql+pymysql".
    key = key.split("+", 1)[0]
    return _ALIASES.get(key, key)
