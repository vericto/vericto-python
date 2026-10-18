"""CiCheckClient (the CI-check API used by vericto-dbt): configuration, requests and failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple, cast

import pytest
import urllib3

from tests.conftest import API_KEY
from vericto import VerictoConfigError, VerictoQuotaExceeded, VerictoUnavailable
from vericto.ci_api import CiCheckClient, CiFile, CiRequestError, provenance_from_env


class ScriptedPool:
    def __init__(self, *steps: object) -> None:
        self.steps = list(steps)
        self.calls: List[Dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> urllib3.HTTPResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        status, body = cast(Tuple[int, Any], step)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        return urllib3.HTTPResponse(body=data, status=status, preload_content=True)

    def clear(self) -> None:
        self.steps.clear()


def client(*steps: object, max_retries: int = 1) -> tuple[CiCheckClient, ScriptedPool]:
    pool = ScriptedPool(*steps)
    c = CiCheckClient(
        API_KEY,
        api_url="https://api.example.com",
        backoff=0.0,
        max_retries=max_retries,
        pool_manager=pool,  # type: ignore[arg-type]
    )
    return c, pool


OK = {"summary": {"total": 1}, "queries": [{"line": 1, "status": "ALLOWED"}]}


class TestConfiguration:
    def test_rejects_a_non_vericto_key(self) -> None:
        with pytest.raises(VerictoConfigError, match="must start with 'vtro_'") as info:
            CiCheckClient("sk-live-secret")
        assert "sk-live-secret" not in str(info.value)

    @pytest.mark.parametrize("url", ["ftp://x.example", "x.example"])
    def test_rejects_malformed_urls(self, url: str) -> None:
        with pytest.raises(VerictoConfigError, match="Invalid Vericto API URL"):
            CiCheckClient(API_KEY, api_url=url)

    def test_refuses_plain_http_off_localhost(self) -> None:
        with pytest.raises(VerictoConfigError, match="plain HTTP"):
            CiCheckClient(API_KEY, api_url="http://api.example.com")
        assert CiCheckClient(API_KEY, api_url="http://127.0.0.1:8080/").api_url == "http://127.0.0.1:8080"

    def test_timeout_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERICTO_TIMEOUT", "12.5")
        assert CiCheckClient(API_KEY).timeout == 12.5
        monkeypatch.setenv("VERICTO_TIMEOUT", "later")
        with pytest.raises(VerictoConfigError, match="VERICTO_TIMEOUT"):
            CiCheckClient(API_KEY)
        with pytest.raises(VerictoConfigError, match="greater than 0"):
            CiCheckClient(API_KEY, timeout=-1)

    def test_negative_retries_are_clamped(self) -> None:
        assert CiCheckClient(API_KEY, max_retries=-3).max_retries == 0

    def test_ca_bundle_and_user_agent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        bundle = tmp_path / "ca.pem"
        bundle.write_text("")
        monkeypatch.setenv("VERICTO_CA_BUNDLE", str(bundle))
        c = CiCheckClient(API_KEY, user_agent="vericto-python/x (dbt)")
        assert c._http.connection_pool_kw["ca_certs"] == str(bundle)
        assert c._headers["User-Agent"] == "vericto-python/x (dbt)"


def test_config_parsing_tolerates_missing_and_wrong_types() -> None:
    c, _ = client((200, {"capabilities": "check_files", "ci_checks_remaining": "lots"}))
    cfg = c.config()
    assert cfg.telemetry_query_mode == "raw" and not cfg.sanitized
    assert cfg.capabilities == () and not cfg.check_files
    assert cfg.ci_checks_remaining is None and cfg.workspace_name is None

    c, _ = client(
        (
            200,
            {
                "telemetry_query_mode": "sanitized",
                "capabilities": ["check_files"],
                "ci_checks_remaining": 7,
                "ruleset_version": "rs-2",
                "workspace_name": "acme",
            },
        )
    )
    cfg = c.config()
    assert cfg.sanitized and cfg.check_files and cfg.ci_checks_remaining == 7
    assert (cfg.ruleset_version, cfg.workspace_name) == ("rs-2", "acme")


def test_check_payload_carries_database_file_name_and_provenance() -> None:
    c, pool = client((200, OK))
    c.check_files(
        [CiFile("models/" + "x" * 600 + ".sql", "SELECT 1")],
        "postgres",
        database_id="db-1",
        file_name="f" * 300,
        provenance={"git_sha": "abc"},
    )
    sent = json.loads(pool.calls[0]["body"])
    assert sent["database_id"] == "db-1"
    assert len(sent["file_name"]) == 255
    assert len(sent["files"][0]["path"]) == 512
    assert sent["provenance"] == {"git_sha": "abc"}
    assert sent["output_format"] == "json"
    assert pool.calls[0]["headers"]["X-API-Key"] == API_KEY


def test_check_queries_numbers_the_entries() -> None:
    c, pool = client((200, OK))
    c.check_queries(["SELECT 1", "SELECT 2"], "mysql")
    sent = json.loads(pool.calls[0]["body"])
    assert sent["queries"] == [{"line": 1, "sql": "SELECT 1"}, {"line": 2, "sql": "SELECT 2"}]
    assert "database_id" not in sent and "file_name" not in sent and "provenance" not in sent


@pytest.mark.parametrize("body", [{"summary": {}}, b"<html>gateway</html>", b"[1, 2]"])
def test_answers_without_results_are_unavailable(body: object) -> None:
    c, _ = client((200, body))
    with pytest.raises(VerictoUnavailable, match="without results"):
        c.check_queries(["SELECT 1"], "postgres")


def test_connection_errors_are_retried() -> None:
    c, pool = client(urllib3.exceptions.ProtocolError("reset"), (200, OK))
    assert c.check_queries(["SELECT 1"], "postgres")["queries"]
    assert len(pool.calls) == 2


def test_gives_up_after_retries() -> None:
    c, _ = client(
        urllib3.exceptions.ProtocolError("reset"),
        (503, {"error": {"code": "UNAVAILABLE", "retry_after_seconds": 0}}),
        max_retries=1,
    )
    with pytest.raises(VerictoUnavailable, match=r"2 attempt.*HTTP 503 UNAVAILABLE") as info:
        c.check_queries(["SELECT 1"], "postgres")
    assert API_KEY not in str(info.value)


def test_quota_is_not_retried() -> None:
    c, pool = client((429, {"error": {"code": "QUOTA_EXCEEDED", "resets_at": "2026-11-01", "upgrade_url": "u"}}))
    with pytest.raises(VerictoQuotaExceeded, match="CI-check allowance; it resets at 2026-11-01") as info:
        c.check_queries(["SELECT 1"], "postgres")
    assert info.value.upgrade_url == "u"
    assert len(pool.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors(status: int) -> None:
    c, _ = client((status, {"error": "Invalid API key"}))  # a plain-string error is used as the message
    with pytest.raises(VerictoConfigError, match=f"HTTP {status}\\): Invalid API key") as info:
        c.config()
    assert info.value.status == status
    c, _ = client((status, {}))
    with pytest.raises(VerictoConfigError, match="ci_dryrun:execute scope"):
        c.config()


def test_missing_config_endpoint_points_at_the_url() -> None:
    c, _ = client((404, {}))
    with pytest.raises(VerictoConfigError, match="has no /api/v1/ci/config: check VERICTO_API_URL"):
        c.config()


def test_validation_errors_list_their_details() -> None:
    details = [{"message": f"problem {i}"} for i in range(7)] + [{}]
    c, _ = client((400, {"error": {"code": "VALIDATION_ERROR", "message": "Validation failed", "details": details}}))
    with pytest.raises(CiRequestError) as info:
        c.check_queries(["SELECT 1"], "postgres")
    text = str(info.value)
    assert "Validation failed (problem 0; problem 1; problem 2; problem 3; problem 4)" in text
    assert "problem 5" not in text
    assert info.value.status == 400 and info.value.code == "VALIDATION_ERROR"


def test_retry_delay_honours_retry_after_and_caps() -> None:
    c = CiCheckClient(API_KEY, backoff=0.5)
    assert c._delay(1, 1.5) == 1.5
    assert c._delay(1, 999.0) == 10.0
    assert 0.25 <= c._delay(1, None) <= 0.5
    assert c._delay(10, None) == 10.0
    c.close()


def test_provenance_github_partial_and_truncated() -> None:
    env = {"GITHUB_ACTIONS": "true", "GITHUB_SHA": "a" * 100, "GITHUB_RUN_ID": "1"}
    assert provenance_from_env(env) == {"ci_provider": "github", "git_sha": "a" * 64}
    full = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "octo",
        "GITHUB_REPOSITORY": "acme/dbt",
        "GITHUB_RUN_ID": "42",
        "GITHUB_SERVER_URL": "https://ghe.example",
    }
    assert provenance_from_env(full) == {
        "ci_provider": "github",
        "git_ref": "refs/heads/main",
        "actor": "octo",
        "ci_run_url": "https://ghe.example/acme/dbt/actions/runs/42",
    }
    assert provenance_from_env({}) == {}
    assert provenance_from_env({"GITHUB_ACTIONS": "false", "GITLAB_CI": "false"}) == {}


def test_unparseable_retry_after_falls_back_to_backoff() -> None:
    from vericto.ci_api import _retry_after

    assert _retry_after("tomorrow", "later") is None
    assert _retry_after(None, 2) == 2.0
