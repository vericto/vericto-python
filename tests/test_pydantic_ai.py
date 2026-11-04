# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""vericto.pydantic_ai against the real Pydantic AI Agent, with a scripted FunctionModel (no model calls)."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import sqlite3
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, cast

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent, FunctionToolset, ModelRetry, RunContext, UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

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
from vericto.pydantic_ai import VerictoToolset

UNAVAILABLE: Dict[str, Any] = {"status": 503, "body": {"error": {"code": "UNAVAILABLE"}}}


class Script:
    """A FunctionModel that makes each tool call in turn, then answers "done"; records what it read."""

    def __init__(self, calls: List[Tuple[str, Dict[str, Any]]]) -> None:
        self.calls = list(calls)
        self.seen: List[Tuple[str, str]] = []  # ("return" | "retry", content) per tool result
        self.tool_names: List[str] = []
        self.tool_schemas: Dict[str, Any] = {}
        self.history: List[ModelMessage] = []

    def respond(self, messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.history = list(messages)
        self.tool_names = [t.name for t in info.function_tools]
        self.tool_schemas = {t.name: t.parameters_json_schema for t in info.function_tools}
        last = messages[-1]
        if isinstance(last, ModelRequest):
            for part in last.parts:
                # A ModelRetry reaches the model as a RetryPromptPart, or (newer Pydantic AI) as a
                # ToolReturnPart whose outcome is "retried".
                if isinstance(part, RetryPromptPart):
                    self.seen.append(("retry", str(part.content)))
                elif isinstance(part, ToolReturnPart):
                    kind = "retry" if getattr(part, "outcome", "success") == "retried" else "return"
                    self.seen.append((kind, str(part.content)))
        if self.calls:
            name, args = self.calls.pop(0)
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    @property
    def model(self) -> FunctionModel:
        return FunctionModel(self.respond)


def query_calls(*queries: str, tool: str = "run_sql", argument: str = "query") -> List[Tuple[str, Dict[str, Any]]]:
    return [(tool, {argument: q}) for q in queries]


def run(toolset: VerictoToolset[Any], *queries: str, deps: Any = None) -> Script:
    script = Script(query_calls(*queries))
    agent: Agent[Any, str] = Agent(script.model, toolsets=[toolset], deps_type=object)
    result = agent.run_sync("go", deps=deps)
    assert result.output == "done"
    return script


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'ana@example.com'), (2, 'bo@example.com')")
    yield conn
    conn.close()


def sql_tools(db: sqlite3.Connection, ran: List[str], **kwargs: Any) -> FunctionToolset[Any]:
    tools: FunctionToolset[Any] = FunctionToolset(**kwargs)

    def run_sql(query: str) -> str:
        """Run one SQLite query.

        Args:
            query: The SQL query.
        """
        ran.append(query)
        return str(db.execute(query).fetchall())

    tools.add_function(run_sql)

    return tools


def _count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT count(*) FROM users").fetchone()[0])


def test_allowed_query_runs_and_the_schema_is_the_tool_s(
    fake: FakeVericto, guard: Vericto, db: sqlite3.Connection
) -> None:
    ran: List[str] = []
    script = run(
        VerictoToolset(sql_tools(db, ran), dialect="sqlite3", guard=guard), "SELECT email FROM users WHERE id = 1"
    )
    assert script.seen == [("return", "[('ana@example.com',)]")]
    assert ran == ["SELECT email FROM users WHERE id = 1"]
    assert fake.queries() == ran
    assert fake.requests[0]["dialect"] == "sqlite"
    assert script.tool_names == ["run_sql"]
    assert list(script.tool_schemas["run_sql"]["properties"]) == ["query"]


def test_blocked_raises_model_retry_and_never_runs(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    fake.on(
        lambda sql: sql.startswith("DELETE FROM users") and "WHERE" not in sql,
        blocked("VERICTO-001", fix="DELETE FROM users WHERE id = ?"),
    )
    ran: List[str] = []
    toolset: VerictoToolset[Any] = VerictoToolset(sql_tools(db, ran), dialect="sqlite", guard=guard)
    script = run(toolset, "DELETE FROM users", "DELETE FROM users WHERE id = 2")
    (kind, refusal), second = script.seen
    assert kind == "retry"
    assert refusal.startswith(
        "Error: Vericto blocked this query (VERICTO-001, severity critical). It was NOT executed."
    )
    assert "Suggested fix: DELETE FROM users WHERE id = ?" in refusal
    assert "(Vericto event evt-blocked)" in refusal
    assert second == ("return", "[]")
    assert ran == ["DELETE FROM users WHERE id = 2"]
    assert _count(db) == 1


def test_a_model_that_keeps_writing_blocked_sql_exhausts_the_retries(
    fake: FakeVericto, guard: Vericto, db: sqlite3.Connection
) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001"))
    ran: List[str] = []
    toolset: VerictoToolset[Any] = VerictoToolset(sql_tools(db, ran, max_retries=1), dialect="sqlite", guard=guard)
    script = Script(query_calls("DELETE FROM users", "DELETE FROM users", "DELETE FROM users"))
    agent: Agent[None, str] = Agent(script.model, toolsets=[toolset])
    with pytest.raises(UnexpectedModelBehavior):
        agent.run_sync("go")
    assert ran == []
    assert _count(db) == 2


def test_on_block_return_makes_the_refusal_the_tool_result(
    fake: FakeVericto, guard: Vericto, db: sqlite3.Connection
) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001"))
    ran: List[str] = []
    script = run(
        VerictoToolset(sql_tools(db, ran), dialect="sqlite", guard=guard, on_block="return"), "DELETE FROM users"
    )
    [(kind, out)] = script.seen
    assert kind == "return"
    assert out.startswith("Error: Vericto blocked this query (VERICTO-001")
    assert ran == []


def test_on_block_raise_stops_the_run(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001"))
    ran: List[str] = []
    with pytest.raises(VerictoBlocked) as info:
        run(VerictoToolset(sql_tools(db, ran), dialect="sqlite", guard=guard, on_block="raise"), "DELETE FROM users")
    assert info.value.verdict.rule_code == "VERICTO-001"
    assert ran == []


def test_flagged_runs_and_is_reported(
    fake: FakeVericto, evaluator: Any, db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    seen: List[Verdict] = []
    g = Vericto(evaluator, on_verdict=seen.append)
    fake.on(lambda sql: "users" in sql, flagged("VERICTO-030"))
    ran: List[str] = []
    with caplog.at_level("WARNING", logger="vericto"):
        script = run(VerictoToolset(sql_tools(db, ran), dialect="sqlite", guard=g), "SELECT id FROM users")
    assert script.seen == [("return", "[(1,), (2,)]")]
    assert [(v.decision, v.rule_code) for v in seen] == [(Decision.FLAGGED, "VERICTO-030")]
    assert "statement FLAGGED rule=VERICTO-030" in caplog.text


def test_approved_query_runs_instead_of_the_original(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    rewrite = "SELECT '***' AS email FROM users WHERE id = 1 LIMIT 100"
    fake.on(lambda sql: sql.startswith("SELECT email"), masked(rewrite))
    ran: List[str] = []
    script = run(
        VerictoToolset(sql_tools(db, ran), dialect="sqlite", guard=guard), "SELECT email FROM users WHERE id = 1"
    )
    assert ran == [rewrite]
    assert script.seen == [("return", "[('***',)]")]
    assert "ana@example.com" not in str(script.history)


def test_fail_closed_stops_the_run_and_never_reaches_the_model(
    fake: FakeVericto, guard: Vericto, db: sqlite3.Connection
) -> None:
    fake.on(lambda sql: True, UNAVAILABLE)
    ran: List[str] = []
    with pytest.raises(VerictoUnavailable):
        run(VerictoToolset(sql_tools(db, ran), dialect="sqlite", guard=guard), "SELECT 1")
    assert ran == []


def test_fail_open_runs_unevaluated_with_a_warning(fake: FakeVericto, evaluator: Any, db: sqlite3.Connection) -> None:
    fake.on(lambda sql: True, UNAVAILABLE)
    seen: List[Verdict] = []
    g = Vericto(evaluator, fail_mode="open", on_verdict=seen.append)
    ran: List[str] = []
    with pytest.warns(VerictoFailOpenWarning):
        script = run(VerictoToolset(sql_tools(db, ran), dialect="sqlite", guard=g), "SELECT 1")
    assert script.seen == [("return", "[(1,)]")]
    assert ran == ["SELECT 1"]
    assert [v.decision for v in seen] == [Decision.UNEVALUATED]


def test_config_error_stops_the_run(fake: FakeVericto, evaluator: Any, db: sqlite3.Connection) -> None:
    fake.dialects = ["postgres"]
    ran: List[str] = []
    with pytest.raises(VerictoConfigError):
        run(
            VerictoToolset(sql_tools(db, ran), dialect="sqlite", guard=Vericto(evaluator, fail_mode="open")), "SELECT 1"
        )
    assert ran == []


def test_errors_of_the_tool_itself_are_pydantic_ai_s(fake: FakeVericto, guard: Vericto) -> None:
    tools: FunctionToolset[Any] = FunctionToolset()
    calls: List[str] = []

    def run_sql(query: str) -> str:
        """Run SQL."""
        calls.append(query)
        if len(calls) == 1:
            raise ModelRetry("table not found, try again")
        raise RuntimeError("driver exploded")

    tools.add_function(run_sql)

    script = Script(query_calls("SELECT 1", "SELECT 2"))
    agent: Agent[None, str] = Agent(script.model, toolsets=[VerictoToolset(tools, dialect="sqlite", guard=guard)])
    with pytest.raises(RuntimeError, match="driver exploded"):
        agent.run_sync("go")
    assert script.seen == [("retry", "table not found, try again")]
    assert fake.queries() == ["SELECT 1", "SELECT 2"]


def test_async_tool_with_run_context_and_policy_context(fake: FakeVericto, guard: Vericto) -> None:
    tenant: contextvars.ContextVar[Optional[Mapping[str, str]]] = contextvars.ContextVar("tenant", default=None)
    rewrite = "SELECT '***' AS email FROM users LIMIT 10"
    fake.on(lambda sql: sql.startswith("SELECT email"), masked(rewrite))
    fake.on(lambda sql: sql.startswith("DROP"), blocked("VERICTO-002"))
    ran: List[Any] = []
    tools: FunctionToolset[str] = FunctionToolset()

    async def query_db(ctx: RunContext[str], sql: str, limit: int = 10) -> str:
        """Query the warehouse.

        Args:
            sql: The SQL query.
            limit: Max rows.
        """
        ran.append((ctx.deps, sql, limit))
        await asyncio.sleep(0)
        return "ok"

    tools.add_function(query_db)

    toolset: VerictoToolset[Any] = VerictoToolset(
        tools, dialect="postgres", guard=guard, argument="sql", context=tenant.get, end_user={"role": "analyst"}
    )

    async def main() -> Script:
        tenant.set({"tenant": "acme", "ticket": "OPS-1"})
        script = Script(query_calls("SELECT email FROM users", "DROP TABLE users", tool="query_db", argument="sql"))
        agent: Agent[str, str] = Agent(script.model, toolsets=[toolset], deps_type=str)
        result = await agent.run("go", deps="run-deps")
        assert result.output == "done"
        return script

    script = asyncio.run(main())
    assert ran == [("run-deps", rewrite, 10)]
    assert script.seen[0] == ("return", "ok")
    kind, refusal = script.seen[1]
    assert kind == "retry" and "VERICTO-002" in refusal and "NOT executed" in refusal
    assert list(script.tool_schemas["query_db"]["properties"]) == ["sql", "limit"]
    assert fake.requests[0]["context"] == {"tenant": "acme", "ticket": "OPS-1"}
    assert fake.requests[0]["end_user"] == {"role": "analyst"}
    assert fake.requests[0]["dialect"] == "postgres"


def test_async_fail_closed(fake: FakeVericto, guard: Vericto) -> None:
    fake.on(lambda sql: True, UNAVAILABLE)
    tools: FunctionToolset[None] = FunctionToolset()

    async def run_sql(query: str) -> str:
        """Run SQL."""
        raise AssertionError("must not run")

    tools.add_function(run_sql)

    async def main() -> None:
        script = Script(query_calls("SELECT 1"))
        await Agent(script.model, toolsets=[VerictoToolset(tools, dialect="sqlite", guard=guard)]).run("go")

    with pytest.raises(VerictoUnavailable):
        asyncio.run(main())


def test_scope_and_tools_without_the_sql_parameter(fake: FakeVericto, guard: Vericto, db: sqlite3.Connection) -> None:
    ran: List[str] = []
    tools = sql_tools(db, ran)

    def ping() -> str:
        """Ping."""
        return "pong"

    tools.add_function(ping)

    # Every tool is in scope by default: ping has no 'query' parameter, so the run stops before any tool runs.
    script = Script(query_calls("DELETE FROM users"))
    agent: Agent[None, str] = Agent(script.model, toolsets=[VerictoToolset(tools, dialect="sqlite", guard=guard)])
    with pytest.raises(VerictoConfigError, match="Tool 'ping' has no parameter 'query'"):
        agent.run_sync("go")
    assert ran == [] and fake.requests == []

    # tools= names the SQL tools; the others run unchecked.
    scoped: VerictoToolset[Any] = VerictoToolset(tools, dialect="sqlite", guard=guard, tools=["run_sql"])
    script = Script([("ping", {}), *query_calls("SELECT id FROM users WHERE id = 1")])
    Agent(script.model, toolsets=[scoped]).run_sync("go")
    assert script.seen == [("return", "pong"), ("return", "[(1,)]")]
    assert fake.queries() == ["SELECT id FROM users WHERE id = 1"]

    # A scope that leaves the SQL tool out checks nothing.
    fake.requests.clear()
    other: VerictoToolset[Any] = VerictoToolset(
        sql_tools(db, ran), dialect="sqlite", guard=guard, tools=["not_this_one"]
    )
    run(other, "DELETE FROM users WHERE id = 2")
    assert fake.requests == []
    assert _count(db) == 1


def test_non_string_sql_is_refused(fake: FakeVericto, guard: Vericto) -> None:
    tools: FunctionToolset[None] = FunctionToolset()
    ran: List[Any] = []

    def run_sql(query: Any) -> str:
        """Run SQL."""
        ran.append(query)
        return "ran"

    tools.add_function(run_sql)

    script = Script([("run_sql", {"query": 1})])
    Agent(script.model, toolsets=[VerictoToolset(tools, dialect="sqlite", guard=guard)]).run_sync("go")
    [(kind, out)] = script.seen
    assert kind == "retry"
    assert "could not find the SQL query in the 'query' argument of run_sql" in out

    script = Script([("run_sql", {"query": 1})])
    toolset: VerictoToolset[Any] = VerictoToolset(tools, dialect="sqlite", guard=guard, on_block="return")
    Agent(script.model, toolsets=[toolset]).run_sync("go")
    assert script.seen[0][0] == "return" and "NOT executed" in script.seen[0][1]
    assert ran == []
    assert fake.requests == []


def test_bad_arguments(guard: Vericto) -> None:
    tools: FunctionToolset[None] = FunctionToolset()
    with pytest.raises(VerictoConfigError, match="on_block must be one of 'retry', 'return', 'raise'"):
        VerictoToolset(tools, dialect="sqlite", guard=guard, on_block="ignore")  # type: ignore[arg-type]
    with pytest.raises(VerictoConfigError, match="argument must be"):
        VerictoToolset(tools, dialect="sqlite", guard=guard, argument="")
    with pytest.raises(VerictoConfigError, match="tools must be a collection"):
        VerictoToolset(tools, dialect="sqlite", guard=guard, tools="run_sql")
    with pytest.raises(VerictoConfigError):
        VerictoToolset(tools, dialect="sqlite", guard=guard, context={"nope": "x"})


def test_copies_keep_the_guard_and_settings(fake: FakeVericto, guard: Vericto, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_API_URL", fake.url)
    monkeypatch.setenv("VERICTO_DATABASE_ID", "7c9e6679-7425-40de-944b-e07fc1f90ae7")
    mode: Any = "RETURN"  # on_block is case-insensitive
    toolset: VerictoToolset[None] = VerictoToolset(FunctionToolset(), dialect="Postgres", tools=["a"], on_block=mode)
    copy = dataclasses.replace(cast(Any, toolset), wrapped=FunctionToolset())
    assert copy.guard is toolset.guard is not None
    assert copy.tools == frozenset({"a"})
    assert toolset.label == "VerictoToolset(FunctionToolset)"


def test_default_guard_from_the_environment(
    fake: FakeVericto, monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_API_URL", fake.url)
    monkeypatch.setenv("VERICTO_DATABASE_ID", "7c9e6679-7425-40de-944b-e07fc1f90ae7")
    ran: List[str] = []
    assert run(VerictoToolset(sql_tools(db, ran), dialect="sqlite"), "SELECT 1").seen == [("return", "[(1,)]")]
    assert fake.queries() == ["SELECT 1"]


MCP_SERVER = '''
try:
    from mcp.server import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

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


def test_an_mcp_toolset(fake: FakeVericto, guard: Vericto, tmp_path: Any) -> None:
    pytest.importorskip("fastmcp")
    import sys

    from fastmcp.client.transports import PythonStdioTransport
    from pydantic_ai.mcp import MCPToolset

    fake.on(lambda sql: sql.startswith("DELETE"), blocked("VERICTO-001"))
    rewrite = "SELECT '***' AS email FROM users"
    fake.on(lambda sql: sql == "SELECT email FROM users", masked(rewrite))
    script_path = tmp_path / "server.py"
    script_path.write_text(MCP_SERVER, encoding="utf-8")
    transport = PythonStdioTransport(script_path=script_path, python_cmd=sys.executable, args=[])
    server: MCPToolset[None] = MCPToolset(transport)
    toolset: VerictoToolset[Any] = VerictoToolset(
        server, dialect="postgres", guard=guard, argument="sql", tools=["execute_sql"]
    )

    async def main() -> Script:
        script = Script(
            [
                *query_calls("DELETE FROM users", "SELECT email FROM users", tool="execute_sql", argument="sql"),
                ("ping", {}),
            ]
        )
        async with toolset:
            await Agent(script.model, toolsets=[toolset]).run("go")
        return script

    script = asyncio.run(main())
    (k1, refused), (k2, ran), (k3, pong) = script.seen
    assert k1 == "retry" and refused.startswith("Error: Vericto blocked this query (VERICTO-001")
    assert k2 == "return" and ran == f"ran {rewrite}"
    assert k3 == "return" and pong == "pong"
    assert fake.queries() == ["DELETE FROM users", "SELECT email FROM users"]  # ping is not a SQL tool: not checked


def test_offline_guard(offline_guard: Vericto, db: sqlite3.Connection) -> None:
    ran: List[str] = []
    toolset: VerictoToolset[Any] = VerictoToolset(
        sql_tools(db, ran), dialect="sqlite", guard=offline_guard, on_block="return"
    )
    script = run(toolset, "SELECT email FROM users WHERE id = 1", "DELETE FROM users")
    assert script.seen[0] == ("return", "[('ana@example.com',)]")
    _, refusal = script.seen[1]
    assert "VERICTO-001" in refusal
    assert ran == ["SELECT email FROM users WHERE id = 1"]
    assert _count(db) == 2
