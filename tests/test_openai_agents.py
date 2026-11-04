# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""vericto.openai_agents against the real openai-agents Runner, with a scripted model (no OpenAI calls)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import sqlite3
from typing import Any, Dict, Iterator, List, Mapping, Optional

import pytest

pytest.importorskip("agents")

from agents import Agent, FunctionTool, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from agents.exceptions import ToolInputGuardrailTripwireTriggered, UserError
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from tests.conftest import API_KEY, FakeVericto, blocked, flagged, masked
from vericto import (
    Decision,
    Verdict,
    Vericto,
    VerictoBlocked,
    VerictoConfigError,
    VerictoFailOpenWarning,
    VerictoUnavailable,
)
from vericto.openai_agents import approved_query_message, vericto_sql_tool, vericto_tool_input_guardrail

set_tracing_disabled(True)

UNAVAILABLE: Dict[str, Any] = {"status": 503, "body": {"error": {"code": "UNAVAILABLE"}}}


class ScriptedModel(Model):
    """Calls the tool with each query in turn, then answers "done"; records what it read."""

    def __init__(self, tool: str, queries: List[str], argument: str = "query") -> None:
        self.outputs: List[Any] = [
            ResponseFunctionToolCall(
                type="function_call", call_id=f"call_{i}", name=tool, arguments=json.dumps({argument: q})
            )
            for i, q in enumerate(queries)
        ]
        self.outputs.append(
            ResponseOutputMessage(
                id="msg_1",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text="done", annotations=[])],
            )
        )
        self.inputs: List[Any] = []

    async def get_response(self, system_instructions: Any, input: Any, *args: Any, **kwargs: Any) -> ModelResponse:
        self.inputs.append(input)
        return ModelResponse(output=[self.outputs.pop(0)], usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def tool_outputs(self) -> List[str]:
        """Every function_call_output the model received, in order."""
        last = self.inputs[-1]
        return [
            str(item["output"])
            for item in last
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]


def run(tool: FunctionTool, *queries: str, context: Any = None) -> ScriptedModel:
    model = ScriptedModel(tool.name, list(queries))
    agent: Agent[Any] = Agent(name="analyst", tools=[tool], model=model)
    result = Runner.run_sync(agent, "go", context=context)
    assert result.final_output == "done"
    return model


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'ana@example.com'), (2, 'bo@example.com')")
    yield conn
    conn.close()


def _count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT count(*) FROM users").fetchone()[0])


# -- vericto_sql_tool -------------------------------------------------------------------


def test_allowed_query_runs_and_the_schema_is_the_function_s(
    fake: FakeVericto, guard: Vericto, db: sqlite3.Connection
) -> None:
    ran: List[str] = []

    @vericto_sql_tool(dialect="sqlite3", guard=guard)
    def run_sql(query: str) -> str:
        """Run one SQLite query.

        Args:
            query: The SQL query.
        """
        ran.append(query)
        return str(db.execute(query).fetchall())

    assert isinstance(run_sql, FunctionTool)
    assert run_sql.name == "run_sql"
    assert run_sql.description == "Run one SQLite query."
    assert list(run_sql.params_json_schema["properties"]) == ["query"]
    model = run(run_sql, "SELECT email FROM users WHERE id = 1")
    assert model.tool_outputs() == ["[('ana@example.com',)]"]
    assert ran == ["SELECT email FROM users WHERE id = 1"]
    assert fake.queries() == ran
    assert fake.requests[0]["dialect"] == "sqlite"


def test_blocked_returns_the_refusal_and_never_runs(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001", fix="DELETE FROM users WHERE id = ?"))
    ran: List[str] = []

    @vericto_sql_tool(dialect="sqlite", guard=guard)
    def run_sql(query: str) -> str:
        """Run SQL."""
        ran.append(query)
        return str(db.execute(query).fetchall())

    [out] = run(run_sql, "DELETE FROM users").tool_outputs()
    assert out.startswith("Error: Vericto blocked this query (VERICTO-001, severity critical). It was NOT executed.")
    assert "Suggested fix: DELETE FROM users WHERE id = ?" in out
    assert "(Vericto event evt-blocked)" in out
    assert ran == []
    assert _count(db) == 2


def test_on_block_raise_stops_the_run(fake: FakeVericto, guard: Vericto) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked())
    ran: List[str] = []
    tool = vericto_sql_tool(lambda query: ran.append(query), dialect="sqlite", guard=guard, on_block="raise")
    with pytest.raises(UserError) as info:
        run(tool, "DELETE FROM users")
    assert isinstance(info.value.__cause__, VerictoBlocked)
    assert ran == []


def test_flagged_runs_and_is_reported(
    fake: FakeVericto, evaluator: Any, db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    seen: List[Verdict] = []
    g = Vericto(evaluator, on_verdict=seen.append)
    fake.on(lambda sql: "users" in sql, flagged("VERICTO-030"))

    @vericto_sql_tool(dialect="sqlite", guard=g)
    def run_sql(query: str) -> str:
        """Run SQL."""
        return str(db.execute(query).fetchall())

    with caplog.at_level("WARNING", logger="vericto"):
        model = run(run_sql, "SELECT id FROM users")
    assert model.tool_outputs() == ["[(1,), (2,)]"]
    assert [(v.decision, v.rule_code) for v in seen] == [(Decision.FLAGGED, "VERICTO-030")]
    assert "statement FLAGGED rule=VERICTO-030" in caplog.text


def test_approved_query_runs_instead_of_the_original(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    rewrite = "SELECT '***' AS email FROM users WHERE id = 1 LIMIT 100"
    fake.on(lambda sql: sql.startswith("SELECT email"), masked(rewrite))
    ran: List[str] = []

    @vericto_sql_tool(dialect="sqlite", guard=guard)
    def run_sql(query: str) -> str:
        """Run SQL."""
        ran.append(query)
        return str(db.execute(query).fetchall())

    model = run(run_sql, "SELECT email FROM users WHERE id = 1")
    assert ran == [rewrite]
    assert model.tool_outputs() == ["[('***',)]"]
    assert "ana@example.com" not in str(model.inputs)


def test_fail_closed_stops_the_run_and_never_reaches_the_model(fake: FakeVericto, guard: Vericto) -> None:
    fake.on(lambda sql: True, UNAVAILABLE)
    ran: List[str] = []
    tool = vericto_sql_tool(lambda query: ran.append(query), dialect="sqlite", guard=guard)
    with pytest.raises(UserError) as info:
        run(tool, "SELECT 1")
    assert isinstance(info.value.__cause__, VerictoUnavailable)
    assert ran == []


def test_fail_open_runs_unevaluated_with_a_warning(fake: FakeVericto, evaluator: Any) -> None:
    fake.on(lambda sql: True, UNAVAILABLE)
    seen: List[Verdict] = []
    g = Vericto(evaluator, fail_mode="open", on_verdict=seen.append)
    tool = vericto_sql_tool(lambda query: f"ran {query}", dialect="sqlite", guard=g, name_override="run_sql")
    with pytest.warns(VerictoFailOpenWarning):
        model = run(tool, "SELECT 1")
    assert model.tool_outputs() == ["ran SELECT 1"]
    assert [v.decision for v in seen] == [Decision.UNEVALUATED]


def test_config_error_stops_the_run(fake: FakeVericto, evaluator: Any) -> None:
    fake.dialects = ["postgres"]
    tool = vericto_sql_tool(lambda query: "rows", dialect="sqlite", guard=Vericto(evaluator, fail_mode="open"))
    with pytest.raises(UserError) as info:
        run(tool, "SELECT 1")
    assert isinstance(info.value.__cause__, VerictoConfigError)


def test_errors_of_the_tool_itself_follow_function_tool(fake: FakeVericto, guard: Vericto) -> None:
    def broken(query: str) -> str:
        raise RuntimeError("driver exploded")

    # Default: the SDK's own message for the model, as with function_tool.
    [out] = run(vericto_sql_tool(broken, dialect="sqlite", guard=guard), "SELECT 1").tool_outputs()
    assert out.startswith("An error occurred")  # the SDK default (0.23+ leaves the error text out)

    async def custom(ctx: RunContextWrapper[Any], error: Exception) -> str:
        return f"custom: {error}"

    [out] = run(
        vericto_sql_tool(broken, dialect="sqlite", guard=guard, failure_error_function=custom), "SELECT 1"
    ).tool_outputs()
    assert out == "custom: driver exploded"

    with pytest.raises(UserError) as info:
        run(vericto_sql_tool(broken, dialect="sqlite", guard=guard, failure_error_function=None), "SELECT 1")
    assert isinstance(info.value.__cause__, RuntimeError)


def test_async_tool_with_run_context_and_policy_context(fake: FakeVericto, guard: Vericto) -> None:
    tenant: contextvars.ContextVar[Optional[Mapping[str, str]]] = contextvars.ContextVar("tenant", default=None)
    rewrite = "SELECT '***' AS email FROM users LIMIT 10"
    fake.on(lambda sql: sql.startswith("SELECT email"), masked(rewrite))
    fake.on(lambda sql: sql.startswith("DROP"), blocked("VERICTO-002"))
    ran: List[Any] = []

    @vericto_sql_tool(dialect="postgres", guard=guard, argument="sql", context=tenant.get, end_user={"role": "analyst"})
    async def query_db(ctx: RunContextWrapper[str], sql: str, limit: int = 10) -> str:
        """Query the warehouse.

        Args:
            sql: The SQL query.
            limit: Max rows.
        """
        ran.append((ctx.context, sql, limit))
        await asyncio.sleep(0)
        return "ok"

    assert list(query_db.params_json_schema["properties"]) == ["sql", "limit"]

    async def main() -> ScriptedModel:
        tenant.set({"tenant": "acme", "ticket": "OPS-1"})
        model = ScriptedModel("query_db", ["SELECT email FROM users", "DROP TABLE users"], argument="sql")
        agent: Agent[str] = Agent(name="analyst", tools=[query_db], model=model)
        result = await Runner.run(agent, "go", context="run-ctx")
        assert result.final_output == "done"
        return model

    model = asyncio.run(main())
    assert ran == [("run-ctx", rewrite, 10)]
    outputs = model.tool_outputs()
    assert outputs[0] == "ok"
    assert "VERICTO-002" in outputs[1] and "NOT executed" in outputs[1]
    assert fake.requests[0]["context"] == {"tenant": "acme", "ticket": "OPS-1"}
    assert fake.requests[0]["end_user"] == {"role": "analyst"}
    assert fake.requests[0]["dialect"] == "postgres"


def test_async_fail_closed(fake: FakeVericto, guard: Vericto) -> None:
    fake.on(lambda sql: True, UNAVAILABLE)

    @vericto_sql_tool(dialect="sqlite", guard=guard)
    async def run_sql(query: str) -> str:
        """Run SQL."""
        raise AssertionError("must not run")

    async def main() -> None:
        agent: Agent[Any] = Agent(name="a", tools=[run_sql], model=ScriptedModel("run_sql", ["SELECT 1"]))
        await Runner.run(agent, "go")

    with pytest.raises(UserError) as info:
        asyncio.run(main())
    assert isinstance(info.value.__cause__, VerictoUnavailable)


def test_direct_call_and_bad_arguments(guard: Vericto) -> None:
    def no_query(sql: str) -> str:
        return sql

    with pytest.raises(VerictoConfigError, match="no parameter 'query'"):
        vericto_sql_tool(no_query, dialect="sqlite", guard=guard)
    with pytest.raises(VerictoConfigError, match="on_block must be"):
        vericto_sql_tool(dialect="sqlite", guard=guard, on_block="ignore")  # type: ignore[call-overload]
    with pytest.raises(VerictoConfigError, match="argument must be"):
        vericto_sql_tool(dialect="sqlite", guard=guard, argument="")


def test_non_string_sql_is_a_type_error(fake: FakeVericto, guard: Vericto) -> None:
    @vericto_sql_tool(dialect="sqlite", guard=guard, strict_mode=False)
    def run_sql(query: Any) -> str:
        """Run SQL."""
        return "ran"

    model = ScriptedModel("run_sql", [])
    model.outputs.insert(
        0, ResponseFunctionToolCall(type="function_call", call_id="c", name="run_sql", arguments='{"query": 1}')
    )
    agent: Agent[Any] = Agent(name="a", tools=[run_sql], model=model)
    Runner.run_sync(agent, "go")
    [out] = model.tool_outputs()
    assert out != "ran"
    assert fake.requests == []


def test_default_guard_from_the_environment(fake: FakeVericto, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_API_URL", fake.url)
    monkeypatch.setenv("VERICTO_DATABASE_ID", "7c9e6679-7425-40de-944b-e07fc1f90ae7")
    tool = vericto_sql_tool(lambda query: "ran", dialect="sqlite", name_override="run_sql")
    assert run(tool, "SELECT 1").tool_outputs() == ["ran"]
    assert fake.queries() == ["SELECT 1"]


# -- vericto_tool_input_guardrail ------------------------------------------------------------


def guarded_tool(db: sqlite3.Connection, ran: List[str], **kwargs: Any) -> FunctionTool:
    def run_sql(query: str) -> str:
        """Run SQL."""
        ran.append(query)
        return str(db.execute(query).fetchall())

    return function_tool(run_sql, tool_input_guardrails=[vericto_tool_input_guardrail(dialect="sqlite", **kwargs)])


def test_guardrail_blocks_with_the_refusal(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001", fix="DELETE FROM users WHERE id = 1"))
    ran: List[str] = []
    model = ScriptedModel("run_sql", ["DELETE FROM users", "SELECT id FROM users WHERE id = 1"])
    agent: Agent[Any] = Agent(name="a", tools=[guarded_tool(db, ran, guard=guard)], model=model)
    result = Runner.run_sync(agent, "go")
    blocked_out, allowed_out = model.tool_outputs()
    assert "Vericto blocked this query (VERICTO-001" in blocked_out
    assert "Suggested fix: DELETE FROM users WHERE id = 1" in blocked_out
    assert allowed_out == "[(1,)]"
    assert ran == ["SELECT id FROM users WHERE id = 1"]
    assert _count(db) == 2
    infos = [r.output.output_info for r in result.tool_input_guardrail_results]
    assert [v.decision for v in infos] == [Decision.BLOCKED, Decision.ALLOWED]
    assert result.tool_input_guardrail_results[0].guardrail.get_name() == "vericto"


def test_guardrail_flagged_is_allowed_and_reported(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: True, flagged("VERICTO-030"))
    ran: List[str] = []
    model = ScriptedModel("run_sql", ["SELECT * FROM users"])
    agent: Agent[Any] = Agent(name="a", tools=[guarded_tool(db, ran, guard=guard)], model=model)
    result = Runner.run_sync(agent, "go")
    assert ran == ["SELECT * FROM users"]
    [gr] = result.tool_input_guardrail_results
    assert gr.output.behavior["type"] == "allow"
    assert gr.output.output_info.rule_code == "VERICTO-030"


def test_guardrail_approved_query_asks_for_a_second_call(
    fake: FakeVericto, guard: Vericto, db: sqlite3.Connection
) -> None:
    rewrite = "SELECT '***' AS email FROM users"
    fake.on(lambda sql: sql == "SELECT email FROM users", masked(rewrite))
    fake.on(lambda sql: sql == rewrite, masked(rewrite))  # idempotent: the rewrite approves itself
    ran: List[str] = []
    model = ScriptedModel("run_sql", ["SELECT email FROM users", rewrite])
    agent: Agent[Any] = Agent(name="a", tools=[guarded_tool(db, ran, guard=guard)], model=model)
    Runner.run_sync(agent, "go")
    first, second = model.tool_outputs()
    assert "NOT executed as written" in first
    assert f"Call the tool again with exactly this query: {rewrite}" in first
    assert second == "[('***',), ('***',)]"
    assert ran == [rewrite]


def test_guardrail_scope_and_missing_argument(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    ran: List[str] = []
    other = guarded_tool(db, ran, guard=guard, tools=["not_this_one"])
    run(other, "DELETE FROM users WHERE id = 2")
    assert fake.requests == []  # out of scope: not checked
    assert ran == ["DELETE FROM users WHERE id = 2"]

    ran.clear()
    wrong = guarded_tool(db, ran, guard=guard, argument="sql")
    [out] = run(wrong, "SELECT 1").tool_outputs()
    assert "could not find the SQL query in the 'sql' argument of run_sql" in out
    assert ran == []


def test_guardrail_unparseable_arguments_are_rejected(guard: Vericto) -> None:
    from agents.tool_context import ToolContext
    from agents.tool_guardrails import ToolInputGuardrailData

    guardrail = vericto_tool_input_guardrail(dialect="sqlite", guard=guard)
    ctx: ToolContext[Any] = ToolContext(context=None, tool_name="run_sql", tool_call_id="c", tool_arguments="{not json")
    out = asyncio.run(guardrail.run(ToolInputGuardrailData(context=ctx, agent=Agent(name="a"))))
    assert out.behavior["type"] == "reject_content"


def test_guardrail_on_block_raise_trips_the_wire(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001"))
    ran: List[str] = []
    with pytest.raises(ToolInputGuardrailTripwireTriggered) as info:
        run(guarded_tool(db, ran, guard=guard, on_block="raise"), "DELETE FROM users")
    assert info.value.output.output_info.rule_code == "VERICTO-001"
    assert ran == []


def test_guardrail_fail_closed_and_open(fake: FakeVericto, evaluator: Any, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: True, UNAVAILABLE)
    ran: List[str] = []
    with pytest.raises(UserError) as info:
        run(guarded_tool(db, ran, guard=Vericto(evaluator)), "SELECT 1")
    assert isinstance(info.value.__cause__, VerictoUnavailable)
    assert ran == []

    with pytest.warns(VerictoFailOpenWarning):
        model = run(guarded_tool(db, ran, guard=Vericto(evaluator, fail_mode="open")), "SELECT 1")
    assert ran == ["SELECT 1"]
    assert model.tool_outputs() == ["[(1,)]"]


def test_guardrail_async_tool(fake: FakeVericto, guard: Vericto) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001"))
    ran: List[str] = []

    async def run_sql(query: str) -> str:
        """Run SQL."""
        ran.append(query)
        return "ok"

    tool = function_tool(run_sql, tool_input_guardrails=[vericto_tool_input_guardrail(dialect="sqlite", guard=guard)])

    async def main() -> ScriptedModel:
        model = ScriptedModel("run_sql", ["DELETE FROM users", "SELECT 1"])
        await Runner.run(Agent(name="a", tools=[tool], model=model), "go")
        return model

    model = asyncio.run(main())
    assert ran == ["SELECT 1"]
    assert "VERICTO-001" in model.tool_outputs()[0]


def test_approved_query_message() -> None:
    v = Verdict(Decision.FLAGGED, "SELECT email FROM t", "postgres", rewritten_query="SELECT '***' FROM t")
    last = approved_query_message(v).splitlines()[-1]
    assert last == "Call the tool again with exactly this query: SELECT '***' FROM t"
    with_event = Verdict(Decision.FLAGGED, "x", "postgres", rewritten_query="y", event_id="evt-1")
    assert approved_query_message(with_event).endswith("(Vericto event evt-1)")


MCP_SERVER = '''
from mcp.server import MCPServer

server = MCPServer("db")


@server.tool()
def execute_sql(sql: str) -> str:
    """Run SQL."""
    return f"ran {sql}"


@server.tool()
def ping() -> str:
    """Ping."""
    return "pong"


server.run()
'''


def test_guardrail_on_an_mcp_server(fake: FakeVericto, guard: Vericto, tmp_path: Any) -> None:
    import sys

    from agents.mcp import MCPServerStdio

    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001"))
    script = tmp_path / "server.py"
    script.write_text(MCP_SERVER, encoding="utf-8")
    guardrail = vericto_tool_input_guardrail(dialect="postgres", guard=guard, argument="sql", tools=["execute_sql"])

    async def main() -> ScriptedModel:
        params: Any = {"command": sys.executable, "args": ["-I", str(script)]}
        async with MCPServerStdio(params=params, tool_input_guardrails=[guardrail]) as server:
            model = ScriptedModel("execute_sql", ["DELETE FROM users", "SELECT 1"], argument="sql")
            model.outputs.insert(
                2, ResponseFunctionToolCall(type="function_call", call_id="p", name="ping", arguments="{}")
            )
            await Runner.run(Agent(name="a", mcp_servers=[server], model=model), "go")
            return model

    model = asyncio.run(main())
    refused, ran, pong = model.tool_outputs()
    assert refused.startswith("Error: Vericto blocked this query (VERICTO-001")
    assert "ran SELECT 1" in ran
    assert "pong" in pong
    assert fake.queries() == ["DELETE FROM users", "SELECT 1"]  # ping is not a SQL tool: not checked


def test_offline_guard_sync_and_async_tools(offline_guard: Vericto, db: sqlite3.Connection) -> None:
    ran: List[str] = []

    @vericto_sql_tool(dialect="sqlite", guard=offline_guard)
    def run_sql(query: str) -> str:
        """Run SQL."""
        ran.append(query)
        return str(db.execute(query).fetchall())

    @vericto_sql_tool(dialect="sqlite", guard=offline_guard)
    async def run_sql_async(query: str) -> str:
        """Run SQL (the check runs in a worker thread)."""
        ran.append(query)
        return str(db.execute(query).fetchall())

    assert run(run_sql, "SELECT email FROM users WHERE id = 1").tool_outputs() == ["[('ana@example.com',)]"]
    [out] = run(run_sql_async, "DELETE FROM users").tool_outputs()
    assert out.startswith("Error: Vericto blocked this query (VERICTO-001, severity critical). It was NOT executed.")
    assert ran == ["SELECT email FROM users WHERE id = 1"]
    assert _count(db) == 2
