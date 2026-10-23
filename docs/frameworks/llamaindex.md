# LlamaIndex

**Official integration:** `vericto.llamaindex.VerictoSQLDatabase` (extra
`vericto[llamaindex]`).

It is a `llama_index.core.SQLDatabase`, so it works with `NLSQLTableQueryEngine`,
`SQLTableRetrieverQueryEngine`, `NLSQLRetriever` and any `SQLDatabase.run_sql`
caller. Every statement `run_sql` (and `insert_into_table`) sends is checked with
Vericto first.

## Install

```bash
pip install "vericto[llamaindex]"   # llama-index-core and SQLAlchemy
```

Set `VERICTO_API_KEY` (scope `proxy:evaluate`) and `VERICTO_DATABASE_ID`.

## Example

The LLM below is a scripted stand-in (`CustomLLM`) so the example runs without a
model: it "writes" an unsafe `DELETE`, then summarizes what it read. Pass your own
LLM (and embedding model) instead.

<!-- docs-check: requires=llama_index.core -->
```python
from typing import Any

from llama_index.core.embeddings import MockEmbedding
from llama_index.core.llms import CompletionResponse, CustomLLM, LLMMetadata
from llama_index.core.query_engine import NLSQLTableQueryEngine
from sqlalchemy import create_engine

from vericto.llamaindex import VerictoSQLDatabase

engine = create_engine("sqlite:///app.db")
with engine.begin() as conn:
    conn.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
    conn.exec_driver_sql("INSERT INTO orders (status) VALUES ('shipped'), ('cancelled')")


class ScriptedLLM(CustomLLM):
    responses: list[str]
    prompts: list[str] = []

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(model_name="scripted")

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        self.prompts.append(prompt)
        return CompletionResponse(text=self.responses.pop(0))

    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any):
        raise NotImplementedError


llm = ScriptedLLM(responses=["SQLQuery: DELETE FROM orders\nSQLResult: ", "Vericto blocked that query."])

sql_database = VerictoSQLDatabase(engine, include_tables=["orders"], on_block="return")
query_engine = NLSQLTableQueryEngine(
    sql_database=sql_database, tables=["orders"], llm=llm, embed_model=MockEmbedding(embed_dim=8)
)
response = query_engine.query("Delete all orders")

print(response)  # the synthesized answer
assert "Vericto blocked this query (VERICTO-001" in llm.prompts[-1]  # what the synthesis step read
assert sql_database.run_sql("SELECT count(*) FROM orders")[1]["result"] == [(2,)]  # nothing was deleted
```

## What each verdict does

| Verdict | `run_sql` |
|---|---|
| `ALLOWED` | Runs; returns the rows. |
| `FLAGGED` / `MONITORED` | Runs; logged and passed to `on_verdict`. |
| mask | Runs the masked rewrite; the rows come back masked. |
| `BLOCKED` | `on_block="return"` (default): nothing runs; returns the explanation (rule, reason, suggested fix) as the SQL result, with `metadata["vericto_blocked"]` (`rule_code`, `severity`, `suggested_fix`, `event_id`), so the synthesized answer says why. `on_block="raise"`: `VerictoBlocked`. |
| no verdict, configuration error | Raised (`fail_mode="open"` runs the statement unevaluated instead). |

- `NLSQLTableQueryEngine` turns any retrieval error into `Error: ...` text itself,
  so with `on_block="raise"` the block reaches the answer as text there;
  `NLSQLRetriever(..., handle_sql_errors=False)` lets `VerictoBlocked` propagate.
- `insert_into_table` is checked too; a block always raises (there is no result to
  return).
- Schema reflection is not sent to Vericto; an engine already instrumented with
  `vericto.sqlalchemy.instrument` is not checked twice.
- The check is synchronous. LlamaIndex's async query path (`aquery`) calls
  `run_sql` from the event loop, so each check blocks the loop for one round
  trip.

## Tested with

Python 3.13.12, vericto 0.4.1, llama-index-core 0.14.25, SQLAlchemy 2.1.6,
against the local Runtime API stand-in, with a scripted LLM.
