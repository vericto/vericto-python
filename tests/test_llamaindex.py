"""vericto.llamaindex against the real llama-index-core (scripted LLM, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

pytest.importorskip("llama_index.core")

from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.base.llms.types import CompletionResponse, CompletionResponseGen, LLMMetadata
from llama_index.core.bridge.pydantic import Field
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.llms import CustomLLM
from llama_index.core.query_engine import NLSQLTableQueryEngine
from llama_index.core.retrievers import NLSQLRetriever
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import vericto.sqlalchemy
from tests.conftest import FakeVericto, blocked, masked
from vericto import Vericto, VerictoBlocked, VerictoUnavailable
from vericto.llamaindex import VerictoSQLDatabase


class ScriptedLLM(CustomLLM):
    """Answers each completion with the next scripted text; records the prompts."""

    responses: List[str]
    prompts: List[str] = Field(default_factory=list)

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(model_name="scripted")

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        self.prompts.append(prompt)
        return CompletionResponse(text=self.responses.pop(0))

    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponseGen:
        raise NotImplementedError


def _embed() -> BaseEmbedding:
    return MockEmbedding(embed_dim=8)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'li.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'ana@example.com'), (2, 'bo@example.com')"))
    return eng


def _count(engine: Engine) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM users")).scalar_one())


def test_reflection_is_not_evaluated(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    db = VerictoSQLDatabase(engine, guard=guard)
    db.get_single_table_info("users")
    assert fake.requests == []


def test_allowed_runs(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    db = VerictoSQLDatabase(engine, guard=guard)
    out, meta = db.run_sql("SELECT email FROM users WHERE id = 1")
    assert out == "[('ana@example.com',)]"
    assert meta["col_keys"] == ["email"]
    assert fake.queries() == ["SELECT email FROM users WHERE id = 1"]


def test_blocked_returns_message_and_metadata(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-010", fix="DELETE FROM users WHERE id = 1"))
    db = VerictoSQLDatabase(engine, guard=guard)
    out, meta = db.run_sql("DELETE FROM users")
    assert out.startswith("Error: Vericto blocked this query (VERICTO-010")
    assert "Suggested fix: DELETE FROM users WHERE id = 1" in out
    assert meta["result"] == [] and meta["vericto_blocked"]["rule_code"] == "VERICTO-010"
    assert _count(engine) == 2


def test_on_block_raise(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    db = VerictoSQLDatabase(engine, guard=guard, on_block="raise")
    with pytest.raises(VerictoBlocked):
        db.run_sql("DELETE FROM users")
    assert _count(engine) == 2


def test_masked_runs_the_rewrite(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users WHERE id = 1"))
    db = VerictoSQLDatabase(engine, guard=guard)
    out, _ = db.run_sql("SELECT email FROM users WHERE id = 1")
    assert out == "[('***',)]"


def test_insert_into_table_is_checked(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    db = VerictoSQLDatabase(engine, guard=guard)
    db.insert_into_table("users", {"id": 3, "email": "cy@example.com"})
    assert fake.queries() == ["INSERT INTO users (id, email) VALUES (?, ?)"]
    fake.on(lambda sql: sql.startswith("INSERT"), blocked())
    with pytest.raises(VerictoBlocked):
        db.insert_into_table("users", {"id": 4, "email": "dee@example.com"})
    assert _count(engine) == 3


def test_unavailable_raises(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: True, {"status": 503, "body": {}})
    db = VerictoSQLDatabase(engine, guard=guard)
    with pytest.raises(VerictoUnavailable):
        db.run_sql("SELECT 1")


def test_instrumented_engine_is_checked_once(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    vericto.sqlalchemy.instrument(engine, guard=guard)
    db = VerictoSQLDatabase(engine, guard=guard)
    before = len(fake.requests)
    db.run_sql("SELECT 1")
    assert len(fake.requests) == before + 1


def test_from_uri(fake: FakeVericto, guard: Vericto, tmp_path: Path) -> None:
    db = VerictoSQLDatabase.from_uri(f"sqlite:///{tmp_path / 'u.db'}", guard=guard, on_block="raise")
    assert isinstance(db, VerictoSQLDatabase) and db.on_block == "raise"


def test_nl_sql_query_engine_reports_the_block(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-010", fix="DELETE FROM users WHERE id = 2"))
    db = VerictoSQLDatabase(engine, guard=guard)
    llm = ScriptedLLM(
        responses=[
            "SQLQuery: DELETE FROM users\nSQLResult: ",
            "The query was blocked by Vericto rule VERICTO-010.",
        ]
    )
    query_engine = NLSQLTableQueryEngine(sql_database=db, tables=["users"], llm=llm, embed_model=_embed())
    response = query_engine.query("Delete every user")
    assert str(response) == "The query was blocked by Vericto rule VERICTO-010."
    assert response.metadata is not None and response.metadata["sql_query"] == "DELETE FROM users"
    assert "Vericto blocked this query (VERICTO-010" in llm.prompts[-1]  # what the synthesis step read
    assert _count(engine) == 2


def test_raise_mode_inside_llamaindex(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    # NLSQLTableQueryEngine always catches retrieval errors (handle_sql_errors=True):
    # in raise mode its response is LlamaIndex's "Error: ..." text built from VerictoBlocked.
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    db = VerictoSQLDatabase(engine, guard=guard, on_block="raise")
    llm = ScriptedLLM(responses=["SQLQuery: DELETE FROM users\nSQLResult: "])
    query_engine = NLSQLTableQueryEngine(
        sql_database=db, tables=["users"], llm=llm, embed_model=_embed(), synthesize_response=False
    )
    assert str(query_engine.query("Delete every user")).startswith("Error: Query blocked by VERICTO-010")

    # A retriever built with handle_sql_errors=False lets the exception through.
    llm = ScriptedLLM(responses=["SQLQuery: DELETE FROM users\nSQLResult: "])
    retriever = NLSQLRetriever(db, tables=["users"], llm=llm, embed_model=_embed(), handle_sql_errors=False)
    with pytest.raises(VerictoBlocked):
        retriever.retrieve("Delete every user")
    assert _count(engine) == 2


def test_nl_sql_query_engine_masks(fake: FakeVericto, guard: Vericto, engine: Engine) -> None:
    fake.on(lambda sql: sql.startswith("SELECT email"), masked("SELECT '***' AS email FROM users"))
    db = VerictoSQLDatabase(engine, guard=guard)
    llm = ScriptedLLM(responses=["SQLQuery: SELECT email FROM users\nSQLResult: "])
    query_engine = NLSQLTableQueryEngine(
        sql_database=db, tables=["users"], llm=llm, embed_model=_embed(), synthesize_response=False
    )
    response = query_engine.query("List the emails")
    assert "ana@example.com" not in str(response) and "***" in str(response)
