# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""One listener for any SQLAlchemy engine: Snowflake, PostgreSQL, MySQL, Oracle, SQL Server."""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text

import vericto.sqlalchemy
from vericto import Verdict, Vericto


def report(verdict: Verdict) -> None:
    if verdict.decision.value != "ALLOWED":
        print(f"vericto: {verdict.decision.value} {verdict.rule_code or ''} event={verdict.event_id}")


logging.basicConfig(level=logging.INFO)
guard = Vericto(fail_mode="closed", on_verdict=report)
engine = create_engine(os.environ.get("DATABASE_URL", "postgresql+psycopg://app@localhost/app"))
vericto.sqlalchemy.instrument(engine, guard=guard)

with engine.connect() as conn:
    print(conn.execute(text("SELECT id FROM customers WHERE id = :id"), {"id": 1}).all())
