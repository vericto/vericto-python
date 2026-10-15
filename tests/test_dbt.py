"""vericto-dbt against fixture manifests and a local stand-in for the CI-check API."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.conftest import API_KEY
from vericto import dbt
from vericto.__main__ import main as module_main
from vericto.ci_api import CiCheckClient, provenance_from_env
from vericto.sanitize import sanitize

FIXTURE = Path(__file__).parent / "fixtures" / "dbt" / "jaffle"

Verdict = Dict[str, Any]


def finding(
    status: str, rule: Optional[str] = None, severity: Optional[str] = None, fix: Optional[str] = None
) -> Verdict:
    return {"status": status, "rule_code": rule, "severity": severity, "suggested_fix": fix}


@dataclass
class FakeCi:
    """Splits each file on ';' (like the backend's splitter, for these fixtures) and
    answers by the first matching rule (default ALLOWED)."""

    server: HTTPServer
    rules: List[Callable[[str], Optional[Verdict]]] = field(default_factory=list)
    requests: List[Dict[str, Any]] = field(default_factory=list)
    config: Dict[str, Any] = field(
        default_factory=lambda: {
            "telemetry_query_mode": "raw",
            "capabilities": ["check_files"],
            "ci_checks_remaining": 990,
        }
    )
    config_status: int = 200
    check_status: Optional[int] = None
    check_error: Dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return self.server.url_for("").rstrip("/")

    def on(self, needle: str, verdict: Verdict) -> None:
        self.rules.append(lambda sql: verdict if needle in sql else None)

    def statements(self) -> List[str]:
        out: List[str] = []
        for r in self.requests:
            if "files" in r:
                for f in r["files"]:
                    out += [s.strip() for s in f["sql"].split(";") if s.strip()]
            else:
                out += [q["sql"] for q in r["queries"]]
        return out

    def _verdict(self, sql: str) -> Verdict:
        for rule in self.rules:
            hit = rule(sql)
            if hit is not None:
                return hit
        return finding("ALLOWED")

    def install(self) -> None:
        def config(request: Request) -> Response:
            assert request.headers.get("X-API-Key") == API_KEY
            return Response(json.dumps(self.config), status=self.config_status, content_type="application/json")

        def check(request: Request) -> Response:
            body = json.loads(request.get_data())
            body["_user_agent"] = request.headers.get("User-Agent")
            self.requests.append(body)
            if self.check_status is not None:
                return Response(json.dumps(self.check_error), status=self.check_status, content_type="application/json")
            queries = []
            if "files" in body:
                for index, f in enumerate(body["files"], start=1):
                    line = 1
                    for part in f["sql"].split(";"):
                        if part.strip():
                            lead = len(part) - len(part.lstrip("\n"))
                            v = self._verdict(part.strip())
                            queries.append({"line": line + lead, "file_index": index, "statement_hash": "h", **v})
                        line += part.count("\n")
            else:
                for q in body["queries"]:
                    queries.append({"line": q["line"], "statement_hash": "h", **self._verdict(q["sql"])})
            blocked = sum(1 for q in queries if q["status"] == "BLOCKED")
            resp = {
                "summary": {"total": len(queries), "blocked": blocked, "ruleset_version": "rs-1"},
                "queries": queries,
                "exit_code": 1 if blocked else 0,
                "ci_checks_remaining": 990 - len(queries),
            }
            return Response(json.dumps(resp), status=200, content_type="application/json")

        self.server.expect_request("/api/v1/ci/config", method="GET").respond_with_handler(config)
        self.server.expect_request("/api/v1/ci/check-key", method="POST").respond_with_handler(check)


@pytest.fixture
def ci(httpserver: HTTPServer) -> Iterator[FakeCi]:
    f = FakeCi(httpserver)
    f.install()
    yield f
    httpserver.clear()


@pytest.fixture(autouse=True)
def _no_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests run in CI themselves: the runner's GITHUB_* must not leak into them.
    for name in list(os.environ):
        if name.startswith(("GITHUB_", "GITLAB_", "CI_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "jaffle"
    shutil.copytree(FIXTURE, dest)
    return dest


@pytest.fixture
def client(ci: FakeCi) -> CiCheckClient:
    return CiCheckClient(API_KEY, api_url=ci.url, backoff=0.0, max_retries=1)


def run(project: Path, client: CiCheckClient, *args: str) -> int:
    return dbt.main(["check", "--project-dir", str(project), *args], client=client)


def edit_manifest(project: Path, change: Callable[[Dict[str, Any]], None]) -> None:
    path = project / "target" / "manifest.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


# -- collection -------------------------------------------------------------------


def test_collects_models_snapshots_hooks_and_operations(project: Path) -> None:
    manifest = dbt.load_manifest(project / "target" / "manifest.json")
    items, skipped = dbt.collect(manifest, project_dir=project, target=project / "target")
    labels = [(i.unique_id, i.kind, i.hook_index) for i in items]
    assert labels == [
        ("model.jaffle.stg_customers", "model", None),  # read from target/compiled
        ("model.jaffle.customers", "pre-hook", 0),
        ("model.jaffle.customers", "post-hook", 1),
        ("model.jaffle.customers", "model", None),
        ("snapshot.jaffle.customers_snapshot", "snapshot", None),
        ("operation.jaffle.jaffle-on-run-start-0", "on-run-start", 0),
    ]
    assert 'from "ANALYTICS"."RAW"."CUSTOMERS"' in items[0].sql
    reasons = {(s.unique_id, s.kind): s.reason for s in skipped}
    assert "Jinja" in reasons[("model.jaffle.customers", "post-hook")]
    assert "Jinja" in reasons[("operation.jaffle.jaffle-on-run-end-0", "on-run-end")]
    # seeds have no SQL, tests and packages are opt-in
    assert not any(i.resource_type in ("seed", "test") or "dbt_utils" in i.unique_id for i in items)


def test_resource_types_select_and_packages(project: Path) -> None:
    manifest = dbt.load_manifest(project / "target" / "manifest.json")
    items, _ = dbt.collect(
        manifest,
        project_dir=project,
        target=project / "target",
        resource_types=("model", "test"),
        include_packages=True,
    )
    ids = {i.unique_id for i in items}
    assert "model.dbt_utils.date_spine" in ids
    assert "test.jaffle.not_null_customers_customer_id.5c9bf9911d" in ids
    assert "snapshot.jaffle.customers_snapshot" not in ids
    items, _ = dbt.collect(manifest, project_dir=project, target=project / "target", select=["stg_*"], hooks=False)
    assert [i.unique_id for i in items] == ["model.jaffle.stg_customers"]


@pytest.mark.parametrize(
    ("adapter", "dialect"),
    [
        ("snowflake", "snowflake"),
        ("postgres", "postgres"),
        ("redshift", "postgres"),
        ("mysql", "mysql"),
        ("mariadb", "mysql"),
        ("sqlserver", "mssql"),
        ("oracle", "oracle"),
    ],
)
def test_adapter_dialects(adapter: str, dialect: str) -> None:
    assert dbt.dialect_for(adapter, None)[0] == dialect


def test_redshift_says_it_is_evaluated_as_postgres() -> None:
    assert "PostgreSQL" in dbt.dialect_for("redshift", None)[1][0]


# -- runs ---------------------------------------------------------------------------


def test_clean_run_exits_zero(
    project: Path, client: CiCheckClient, ci: FakeCi, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(project, client) == 0
    out = capsys.readouterr().out
    assert "PASS - 6 statement(s) in snowflake" in out
    assert "CI checks left this month: 984" in out
    req = ci.requests[0]
    assert req["dialect"] == "snowflake"
    assert req["file_name"] == "dbt:jaffle"
    assert [f["path"] for f in req["files"]][:3] == [
        "models/staging/stg_customers.sql",
        "models/customers.sql#pre-hook[0]",
        "models/customers.sql#post-hook[1]",
    ]
    assert "database_id" not in req


def test_blocked_exits_one_and_prints_the_fix(
    project: Path, client: CiCheckClient, ci: FakeCi, capsys: pytest.CaptureFixture[str]
) -> None:
    ci.on("delete from ANALYTICS.DBT.audit_log", finding("BLOCKED", "VERICTO-010", "critical", "add a WHERE clause"))
    assert run(project, client) == 1
    out = capsys.readouterr().out
    assert "BLOCKED  VERICTO-010" in out
    assert "models/customers.sql#pre-hook[0]:1 VERICTO-010 suggested fix: add a WHERE clause" in out
    assert "FAIL" in out


def test_fail_on_flagged(project: Path, client: CiCheckClient, ci: FakeCi, capsys: pytest.CaptureFixture[str]) -> None:
    ci.on("cluster by", finding("FLAGGED", "VERICTO-050", "medium"))
    assert run(project, client) == 0
    assert run(project, client, "--fail-on", "flagged") == 1
    assert run(project, client, "--fail-on", "flag") == 1


def test_fail_on_any_counts_monitored(project: Path, client: CiCheckClient, ci: FakeCi) -> None:
    ci.on("customers_snapshot", finding("MONITORED", "VERICTO-070", "low"))
    ci.on("select * from", finding("MONITORED", "VERICTO-070", "low"))
    assert run(project, client, "--fail-on", "flagged") == 0
    assert run(project, client, "--fail-on", "any") == 1


def test_json_output(project: Path, client: CiCheckClient, ci: FakeCi, tmp_path: Path) -> None:
    ci.on("delete from", finding("BLOCKED", "VERICTO-010", "critical", "add a WHERE clause"))
    out = tmp_path / "report.json"
    assert run(project, client, "--format", "json", "--output", str(out)) == 1
    doc = json.loads(out.read_text())
    assert doc["exit_code"] == 1 and doc["dialect"] == "snowflake" and doc["adapter_type"] == "snowflake"
    assert doc["summary"] == {
        "statements": 6,
        "blocked": 1,
        "flagged": 0,
        "monitored": 0,
        "parse_errors": 0,
        "allowed": 5,
        "skipped": 2,
    }
    blocked = next(r for r in doc["results"] if r["status"] == "BLOCKED")
    assert blocked["unique_id"] == "model.jaffle.customers" and blocked["kind"] == "pre-hook"
    assert blocked["suggested_fix"] == "add a WHERE clause"
    assert doc["ruleset_version"] == "rs-1"


def test_sarif_output(
    project: Path, client: CiCheckClient, ci: FakeCi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci.on("delete from", finding("BLOCKED", "VERICTO-010", "critical", "add a WHERE clause"))
    ci.on("cluster by", finding("FLAGGED", "VERICTO-050", "medium"))
    monkeypatch.chdir(project.parent)
    out = tmp_path / "vericto.sarif"
    assert run(project, client, "--format", "sarif", "--output", str(out)) == 1
    doc = json.loads(out.read_text())
    assert doc["version"] == "2.1.0"
    results = doc["runs"][0]["results"]
    assert {(r["ruleId"], r["level"]) for r in results} == {("VERICTO-010", "error"), ("VERICTO-050", "warning")}
    uris = {r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for r in results}
    assert uris == {"jaffle/models/customers.sql"}
    assert {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]} == {"VERICTO-010", "VERICTO-050"}


def test_line_numbers_from_the_backend(project: Path, client: CiCheckClient, ci: FakeCi) -> None:
    edit_manifest(
        project,
        lambda m: m["nodes"]["model.jaffle.customers"].update(
            compiled_code="select 1;\n\ndelete from ANALYTICS.DBT.customers"
        ),
    )
    ci.on("delete from ANALYTICS.DBT.customers", finding("BLOCKED", "VERICTO-010", "critical"))
    report = dbt.Report(project="jaffle", adapter_type="snowflake", dialect="snowflake", fail_on="blocked")
    manifest = dbt.load_manifest(project / "target" / "manifest.json")
    items, _ = dbt.collect(manifest, project_dir=project, target=project / "target", select=["customers"], hooks=False)
    dbt.evaluate(client, items, "snowflake", report=report)
    assert [(r.status, r.line) for r in report.results] == [("ALLOWED", 1), ("BLOCKED", 3)]


def test_sql_source_run(project: Path, client: CiCheckClient, ci: FakeCi) -> None:
    run_dir = project / "target" / "run" / "jaffle" / "models"
    run_dir.mkdir(parents=True)
    (run_dir / "customers.sql").write_text("create or replace transient table ANALYTICS.DBT.customers as (select 1)")
    assert run(project, client, "--sql-source", "run", "--select", "customers", "--no-hooks") == 0
    assert ci.statements() == ["create or replace transient table ANALYTICS.DBT.customers as (select 1)"]


def test_chunks_large_projects(
    project: Path, client: CiCheckClient, ci: FakeCi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dbt, "CHUNK", 2)
    assert run(project, client) == 0
    assert [len(r["files"]) for r in ci.requests] == [2, 2, 2]


def test_older_backend_gets_queries(project: Path, client: CiCheckClient, ci: FakeCi) -> None:
    ci.config["capabilities"] = []
    ci.on("delete from", finding("BLOCKED", "VERICTO-010", "critical"))
    assert run(project, client) == 1
    assert "queries" in ci.requests[0] and len(ci.requests[0]["queries"]) == 6


def test_sanitized_workspace_never_sends_literals(project: Path, client: CiCheckClient, ci: FakeCi) -> None:
    ci.config["telemetry_query_mode"] = "sanitized"
    edit_manifest(
        project,
        lambda m: m["nodes"]["model.jaffle.customers"].update(
            compiled_code=(
                "select customer_id from ANALYTICS.DBT.stg_customers where email = 'ana@example.com' and id = 42"
            )
        ),
    )
    assert run(project, client, "--select", "customers", "--no-hooks") == 0
    assert ci.statements() == ["select customer_id from ANALYTICS.DBT.stg_customers where email = $1 and id = $2"]


def test_config_unreadable_fails_unless_allowed(project: Path, client: CiCheckClient, ci: FakeCi) -> None:
    ci.config_status = 503
    assert run(project, client) == 4
    assert ci.requests == []
    assert run(project, client, "--allow-unsanitized") == 0


def test_database_id_and_provenance(
    project: Path, client: CiCheckClient, ci: FakeCi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERICTO_DATABASE_ID", "7c9e6679-7425-40de-944b-e07fc1f90ae7")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/analytics")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert run(project, client) == 0
    req = ci.requests[0]
    assert req["database_id"] == "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    assert req["provenance"] == {
        "ci_provider": "github",
        "git_sha": "abc123",
        "ci_run_url": "https://github.com/acme/analytics/actions/runs/42",
    }


def test_provenance_gitlab() -> None:
    env = {"GITLAB_CI": "true", "CI_COMMIT_SHA": "def", "CI_JOB_URL": "https://gitlab.example/j/1"}
    assert provenance_from_env(env) == {
        "ci_provider": "gitlab",
        "git_sha": "def",
        "ci_run_url": "https://gitlab.example/j/1",
    }
    assert provenance_from_env({}) == {}


# -- failures -----------------------------------------------------------------------


def test_missing_manifest_is_a_usage_error(
    tmp_path: Path, client: CiCheckClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, client) == 2
    assert "run `dbt compile`" in capsys.readouterr().err


def test_unsupported_adapter_fails_clearly(
    project: Path, client: CiCheckClient, ci: FakeCi, capsys: pytest.CaptureFixture[str]
) -> None:
    edit_manifest(project, lambda m: m["metadata"].update(adapter_type="bigquery"))
    assert run(project, client) == 2
    err = capsys.readouterr().err
    assert "does not evaluate the dbt adapter 'bigquery'" in err and "Nothing was checked" in err
    assert ci.requests == []
    assert run(project, client, "--dialect", "postgres") == 0
    assert ci.requests[0]["dialect"] == "postgres"


def test_bad_dialect_override(project: Path, client: CiCheckClient) -> None:
    assert run(project, client, "--dialect", "bigquery") == 2


def test_uncompiled_manifest_is_a_usage_error(
    project: Path, client: CiCheckClient, ci: FakeCi, capsys: pytest.CaptureFixture[str]
) -> None:
    def strip(m: Dict[str, Any]) -> None:
        for node in m["nodes"].values():
            node.pop("compiled_code", None)
            node.pop("compiled_path", None)
            node["config"].pop("pre-hook", None)
            node["config"].pop("post-hook", None)
            if node["resource_type"] == "operation":
                node["raw_code"] = "{{ noop() }}"

    edit_manifest(project, strip)
    shutil.rmtree(project / "target" / "compiled")
    assert run(project, client) == 2
    assert "No compiled SQL" in capsys.readouterr().err
    assert ci.requests == []


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (401, {"error": {"code": "UNAUTHORIZED", "message": "Invalid API key"}}, 3),
        (403, {"error": {"code": "FORBIDDEN", "message": "Missing scope ci_dryrun:execute"}}, 3),
        (429, {"error": {"code": "QUOTA_EXCEEDED", "message": "limit", "resets_at": "2026-11-01T00:00:00Z"}}, 3),
        (429, {"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "slow down"}}, 4),
        (503, {}, 4),
        (
            400,
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Validation failed",
                    "details": [{"message": "too big"}],
                }
            },
            2,
        ),
    ],
)
def test_api_errors_map_to_exit_codes(
    project: Path,
    client: CiCheckClient,
    ci: FakeCi,
    status: int,
    error: Dict[str, Any],
    code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ci.check_status = status
    ci.check_error = error
    assert run(project, client) == code
    err = capsys.readouterr().err
    assert API_KEY not in err


def test_key_is_required(monkeypatch: pytest.MonkeyPatch, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert dbt.main(["check", "--project-dir", str(project)]) == 3
    assert "VERICTO_API_KEY" in capsys.readouterr().err


def test_module_entry_point(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert module_main(["dbt", "check", "--project-dir", str(project / "nope")]) == 2
    assert module_main([]) == 2


def test_client_repr_hides_the_key(ci: FakeCi) -> None:
    assert API_KEY not in repr(CiCheckClient(API_KEY, api_url=ci.url))


# -- sanitizer (vericto-cli's sanitize.rs cases) ------------------------------------


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        ("SELECT * FROM users WHERE email = 'a@b.com'", "postgres", "SELECT * FROM users WHERE email = $1"),
        ("SELECT * FROM users WHERE email = 'a@b.com'", "mysql", "SELECT * FROM users WHERE email = ?"),
        ("WHERE a = 'x' AND b = 'y' AND c = 5", "postgres", "WHERE a = $1 AND b = $2 AND c = $3"),
        ("WHERE name = 'O''Brien'", "postgres", "WHERE name = $1"),
        ("WHERE id = 42 AND col2 = 7", "postgres", "WHERE id = $1 AND col2 = $2"),
        ("DO $$ BEGIN PERFORM 'secret'; END $$", "postgres", "DO $$ BEGIN PERFORM 'secret'; END $$"),
        ('SELECT "user\'s col" FROM t', "postgres", 'SELECT "user\'s col" FROM t'),
        (r"WHERE note = 'it\'s secret-token' AND id = 1", "mysql", "WHERE note = ? AND id = ?"),
        (r"WHERE p = 'C:\\' AND q = 'x'", "mysql", "WHERE p = ? AND q = ?"),
        (r"WHERE note = E'it\'s secret-token' AND id = 1", "postgres", "WHERE note = $1 AND id = $2"),
        (r"WHERE note = e'a\\' AND b = 'y'", "postgres", "WHERE note = $1 AND b = $2"),
        (r"WHERE p = 'C:\' AND q = 'x'", "postgres", "WHERE p = $1 AND q = $2"),
        ("SELECT name FROM t WHERE type='a'", "postgres", "SELECT name FROM t WHERE type=$1"),
        ("SELECT 1 -- note: keep 'this'\nFROM t", "postgres", "SELECT $1 -- note: keep 'this'\nFROM t"),
        ("SELECT /* 'x' */ a FROM `t1`", "mysql", "SELECT /* 'x' */ a FROM `t1`"),
    ],
)
def test_sanitize_matches_vericto_cli(sql: str, dialect: str, expected: str) -> None:
    assert sanitize(sql, dialect) == expected
