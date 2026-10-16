from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import vericto.sqlalchemy
from tests.conftest import DIALECTS_WITHOUT_SQLITE, FakeVericto, blocked, masked
from vericto import Vericto, VerictoBlocked, VerictoUnsupportedDialect, normalize_dialect


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'sa.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'ana@example.com')"))
    return eng


def test_every_statement_is_checked(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    vericto.sqlalchemy.instrument(engine, guard=guard)
    with engine.connect() as c:
        assert c.execute(text("SELECT email FROM users WHERE id = :id"), {"id": 1}).scalar() == "ana@example.com"
    assert fake.requests[-1]["query"] == "SELECT email FROM users WHERE id = ?"
    assert fake.requests[-1]["dialect"] == "sqlite"


def test_block_raises_unwrapped_and_nothing_runs(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql == "DELETE FROM users", blocked())
    vericto.sqlalchemy.instrument(engine, guard=guard)
    with pytest.raises(VerictoBlocked), engine.begin() as c:
        c.execute(text("DELETE FROM users"))
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM users")).scalar() == 1


def test_mask_runs_the_rewrite(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users WHERE id = ?"))
    vericto.sqlalchemy.instrument(engine, guard=guard)
    with engine.connect() as c:
        assert c.execute(text("SELECT email FROM users WHERE id = :id"), {"id": 1}).scalar() == "***"


def test_remove_uninstruments(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    remove = vericto.sqlalchemy.instrument(engine, guard=guard)
    remove()
    remove()  # idempotent
    with engine.connect() as c:
        c.execute(text("SELECT 1"))
    assert fake.requests == []


def test_unsupported_dialect_fails_at_instrument_time(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.dialects = list(DIALECTS_WITHOUT_SQLITE)
    with pytest.raises(VerictoUnsupportedDialect):
        vericto.sqlalchemy.instrument(engine, guard=guard)


@pytest.mark.parametrize(
    ("sqlalchemy_name", "vericto_name"),
    [
        ("snowflake", "snowflake"),
        ("postgresql", "postgres"),
        ("postgresql+psycopg", "postgres"),
        ("mysql", "mysql"),
        ("mariadb", "mysql"),
        ("oracle", "oracle"),
        ("mssql", "mssql"),
        ("sqlite", "sqlite"),
    ],
)
def test_dialect_mapping(sqlalchemy_name: str, vericto_name: str) -> None:
    assert normalize_dialect(sqlalchemy_name) == vericto_name


def test_is_instrumented(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    assert not vericto.sqlalchemy.is_instrumented(engine)
    first = vericto.sqlalchemy.instrument(engine, guard=guard)
    second = vericto.sqlalchemy.instrument(engine, guard=guard)
    assert vericto.sqlalchemy.is_instrumented(engine)
    first()
    first()  # a second call does not count twice
    assert vericto.sqlalchemy.is_instrumented(engine)
    second()
    assert not vericto.sqlalchemy.is_instrumented(engine)
