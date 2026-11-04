# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Offline mode against tests/fake_serve.py, a stand-in for ``vericto serve --stdio``."""

from __future__ import annotations

import gc
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

import vericto.offline
from tests.conftest import API_KEY, DATABASE_ID, make_fake_cli
from vericto import (
    Decision,
    OfflineEvaluator,
    RuntimeApiEvaluator,
    Vericto,
    VerictoBlocked,
    VerictoConfigError,
    VerictoFailOpenWarning,
    VerictoUnavailable,
    VerictoUnsupportedDialect,
    dbt,
)
from vericto.ci_api import CiFile, CiRequestError
from vericto.offline import OfflineCiClient, find_cli

FIXTURE = Path(__file__).parent / "fixtures" / "dbt" / "jaffle"

if os.name == "nt":  # pragma: no cover
    pytest.skip("the fake vericto CLI is a POSIX shell script", allow_module_level=True)


@pytest.fixture
def mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Set FAKE_SERVE_MODE (and a state file for crash_once)."""
    monkeypatch.setenv("FAKE_SERVE_STATE", str(tmp_path / "state"))

    def set_mode(value: str, **env: str) -> None:
        monkeypatch.setenv("FAKE_SERVE_MODE", value)
        for key, val in env.items():
            monkeypatch.setenv(key, val)

    return set_mode


@pytest.fixture
def pids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "pids"
    monkeypatch.setenv("FAKE_SERVE_PIDS", str(path))
    return path


def started(pids: Path) -> List[int]:
    return [int(p) for p in pids.read_text().split()] if pids.exists() else []


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def process_of(guard: Vericto) -> subprocess.Popen[bytes]:
    evaluator = guard.evaluator
    assert isinstance(evaluator, OfflineEvaluator)
    proc = evaluator.engine._proc
    assert proc is not None
    return proc.proc


# -- configuration ------------------------------------------------------------------


def test_offline_verdicts_carry_mode_engine_and_ruleset(offline_guard: Vericto) -> None:
    v = offline_guard.check("DROP TABLE users", "postgres")
    assert v.decision is Decision.BLOCKED
    assert (v.rule_code, v.severity, v.mode, v.engine_version) == ("VERICTO-010", "critical", "offline", "5.0.0")
    assert v.ruleset_version == "standard@engine-5.0.0"
    assert v.findings[0].rule_code == "VERICTO-010"
    assert v.findings[0].ast_node_path == "DropStmt"
    assert v.raw["profile"] == "runtime"
    assert v.raw["offline_result"]["queries"][0]["sql_preview"] == "DROP TABLE users"
    assert offline_guard.offline
    with pytest.raises(VerictoBlocked) as info:
        offline_guard.enforce("DELETE FROM users", "postgres")
    assert info.value.suggested_fix == "DELETE FROM t WHERE id = $1"
    assert offline_guard.prepare("SELECT 1", "sqlite") == "SELECT 1"


def test_ci_profile_caps_ddl(fake_cli: str) -> None:
    with Vericto(offline=True, offline_profile="ci", offline_cli_path=fake_cli) as guard:
        v = guard.enforce("DROP TABLE users", "postgres")
    assert v.decision is Decision.FLAGGED
    assert v.raw["profile"] == "ci"


def test_online_verdicts_say_online(fake: Any, guard: Vericto) -> None:
    v = guard.check("SELECT 1", "postgres")
    assert (v.mode, v.engine_version) == ("online", None)
    assert not guard.offline


def test_environment_variables(fake_cli: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERICTO_OFFLINE", "1")
    monkeypatch.setenv("VERICTO_CLI", fake_cli)
    with Vericto() as guard:
        assert guard.check("DROP TABLE t", "postgres").blocked
    monkeypatch.setenv("VERICTO_OFFLINE", "maybe")
    with pytest.raises(VerictoConfigError, match="VERICTO_OFFLINE must be"):
        Vericto()


def test_offline_false_beats_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERICTO_OFFLINE", "1")
    guard = Vericto(offline=False, api_key=API_KEY, database_id=DATABASE_ID)
    assert isinstance(guard.evaluator, RuntimeApiEvaluator)


@pytest.mark.parametrize(
    "kwargs",
    [{"api_key": API_KEY}, {"database_id": DATABASE_ID}, {"api_url": "https://api.vericto.com"}],
)
def test_offline_with_the_api_is_choose_one(fake_cli: str, kwargs: Dict[str, Any]) -> None:
    with pytest.raises(VerictoConfigError, match="choose one"):
        Vericto(offline=True, offline_cli_path=fake_cli, **kwargs)


def test_offline_with_an_evaluator_or_a_key_variable_is_choose_one(
    fake_cli: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    evaluator = OfflineEvaluator(cli_path=fake_cli)
    with pytest.raises(VerictoConfigError, match=r"choose one"):
        Vericto(evaluator, offline=True)
    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    monkeypatch.setenv("VERICTO_OFFLINE", "true")
    with pytest.raises(VerictoConfigError, match=r"VERICTO_OFFLINE\) and the Vericto API \(VERICTO_API_KEY\)"):
        Vericto(offline_cli_path=fake_cli)


def test_an_offline_evaluator_can_be_passed_directly(fake_cli: str) -> None:
    with Vericto(OfflineEvaluator(profile="ci", cli_path=fake_cli)) as guard:
        assert guard.offline
        assert guard.check("DROP TABLE t", "postgres").decision is Decision.FLAGGED


def test_offline_options_need_offline(fake_cli: str) -> None:
    with pytest.raises(VerictoConfigError, match="offline=True"):
        Vericto(api_key=API_KEY, database_id=DATABASE_ID, offline_profile="ci")
    with pytest.raises(VerictoConfigError, match="offline_profile must be"):
        Vericto(offline=True, offline_cli_path=fake_cli, offline_profile="strict")
    with pytest.raises(VerictoConfigError, match="timeout"):
        Vericto(offline=True, offline_cli_path=fake_cli, timeout=0)


def test_missing_binary_names_the_install_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(VerictoConfigError) as info:
        Vericto(offline=True, offline_cli_path=str(tmp_path / "nope"))
    assert info.value.code == "CLI_NOT_FOUND"
    assert "npm i -g @vericto/vericto-cli" in str(info.value)
    assert "brew install vericto/tap/vericto" in str(info.value)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(VerictoConfigError, match="none was found on PATH"):
        Vericto(offline=True)
    monkeypatch.setenv("VERICTO_CLI", str(tmp_path / "missing"))
    with pytest.raises(VerictoConfigError, match="VERICTO_CLI is"):
        Vericto(offline=True)


def test_binary_is_found_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_fake_cli(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert find_cli() == cli
    with Vericto(offline=True) as guard:
        assert guard.check("SELECT 1", "postgres").decision is Decision.ALLOWED


@pytest.mark.parametrize(
    ("value", "code", "message"),
    [
        ("old", "CLI_TOO_OLD", "2.1.0 or later; .* is 2.0.0"),
        ("protocol2", "PROTOCOL_MISMATCH", "protocol 2"),
        ("no_hello", "CLI_TOO_OLD", "did not start 'vericto serve --stdio'"),
    ],
)
def test_unusable_cli_is_a_config_error_in_both_fail_modes(
    fake_cli: str, mode: Any, value: str, code: str, message: str
) -> None:
    mode(value)
    for fail_mode in ("closed", "open"):
        with Vericto(offline=True, offline_cli_path=fake_cli, fail_mode=fail_mode) as guard:
            with pytest.raises(VerictoConfigError, match=message) as info:
                guard.check("SELECT 1", "postgres")
            assert info.value.code == code
            assert "npm i -g" in str(info.value) or code == "PROTOCOL_MISMATCH"


def test_silent_cli_times_out_at_startup(fake_cli: str, mode: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mode("silent")
    monkeypatch.setattr(vericto.offline, "STARTUP_TIMEOUT", 0.5)
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard, pytest.raises(VerictoConfigError):
        guard.check("SELECT 1", "postgres")


# -- protocol -----------------------------------------------------------------------


def test_answers_are_matched_by_id_not_order(fake_cli: str, mode: Any) -> None:
    mode("reverse")  # the fake answers each pair of PAIR requests in reverse order
    results: Dict[str, Any] = {}
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard:
        guard.check("SELECT 1", "postgres")  # start the process
        barrier = threading.Barrier(2)

        def worker(sql: str) -> None:
            barrier.wait()
            results[sql] = guard.check(sql, "postgres")

        threads = [
            threading.Thread(target=worker, args=(sql,)) for sql in ("DROP TABLE pair_a", "SELECT * FROM pair_b")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert results["DROP TABLE pair_a"].decision is Decision.BLOCKED
    assert results["SELECT * FROM pair_b"].decision is Decision.MONITORED


def test_eight_threads_share_one_process(offline_guard: Vericto, pids: Path) -> None:
    errors: List[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(25):
                sql = f"DROP TABLE t_{n}_{i}" if i % 2 else f"SELECT {n}, {i}"
                v = offline_guard.check(sql, "postgres")
                assert v.raw["offline_result"]["queries"][0]["sql_preview"] == sql
                assert v.sql == sql
                assert v.decision is (Decision.BLOCKED if i % 2 else Decision.ALLOWED)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(started(pids)) == 1


def test_multi_statement_text_is_one_verdict_with_the_strongest_finding(offline_guard: Vericto) -> None:
    v = offline_guard.check("SELECT * FROM a; DROP TABLE b; SELECT 1", "postgres")
    assert (v.decision, v.rule_code) == (Decision.BLOCKED, "VERICTO-010")


def test_parse_error_is_allowed(offline_guard: Vericto) -> None:
    v = offline_guard.enforce("GARBAGE", "postgres")
    assert v.decision is Decision.PARSE_ERROR


def test_crash_restarts_once_and_resends(fake_cli: str, mode: Any, pids: Path) -> None:
    mode("crash_once")
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard:
        assert guard.check("SELECT 'CRASH'", "postgres").decision is Decision.ALLOWED
        assert guard.check("DROP TABLE t", "postgres").blocked
    first, _second = started(pids)
    assert gone(first)


def test_a_process_that_keeps_crashing_is_unavailable(fake_cli: str, mode: Any, pids: Path) -> None:
    mode("crash")
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard:
        with pytest.raises(VerictoUnavailable, match="exit") as info:
            guard.check("SELECT 'CRASH'", "postgres")
        assert info.value.code == "OFFLINE_ENGINE_FAILED"
        assert len(started(pids)) == 2  # the first process and one restart
        # The next statement gets a new process.
        assert guard.check("DROP TABLE t", "postgres").blocked
        assert len(started(pids)) == 3


def test_fail_open_on_a_crash(fake_cli: str, mode: Any) -> None:
    mode("crash")
    with Vericto(offline=True, offline_cli_path=fake_cli, fail_mode="open") as guard:
        v = guard.check("SELECT 'CRASH'", "postgres")
        assert (v.decision, v.mode) == (Decision.UNEVALUATED, "offline")
        with pytest.warns(VerictoFailOpenWarning):
            v = guard.enforce("SELECT 'CRASH'", "postgres")
        assert v.degraded


def test_malformed_answer_restarts_then_fails(fake_cli: str, mode: Any, pids: Path) -> None:
    mode("malformed")
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard:
        with pytest.raises(VerictoUnavailable, match="not a JSON object"):
            guard.check("SELECT 'MALFORMED'", "postgres")
        assert len(started(pids)) == 2
        assert guard.check("SELECT 1", "postgres").decision is Decision.ALLOWED


def test_result_without_one_query_is_unavailable(fake_cli: str, mode: Any) -> None:
    mode("bad_result")
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard, pytest.raises(VerictoUnavailable):
        guard.check("SELECT 1", "postgres")


def test_internal_error_honors_the_fail_mode(fake_cli: str, mode: Any) -> None:
    mode("internal")
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard:
        with pytest.raises(VerictoUnavailable, match="INTERNAL") as info:
            guard.enforce("SELECT 'INTERNAL'", "postgres")
        assert info.value.code == "INTERNAL"
        assert guard.check("SELECT 1", "postgres").decision is Decision.ALLOWED  # same process, still running
    with Vericto(offline=True, offline_cli_path=fake_cli, fail_mode="open") as guard:
        assert guard.check("SELECT 'INTERNAL'", "postgres").decision is Decision.UNEVALUATED


def test_timeout_replaces_the_process(fake_cli: str, mode: Any, pids: Path) -> None:
    mode("hang")
    with Vericto(offline=True, offline_cli_path=fake_cli, timeout=0.5) as guard:
        began = time.monotonic()
        with pytest.raises(VerictoUnavailable, match="no answer within"):
            guard.check("SELECT 'HANG'", "postgres")
        assert time.monotonic() - began < 5
        assert len(started(pids)) == 1  # not resent: it would hang again
        assert guard.check("SELECT 1", "postgres").decision is Decision.ALLOWED
        assert len(started(pids)) == 2


def test_too_large_is_refused_before_sending(fake_cli: str, mode: Any, pids: Path) -> None:
    mode("", FAKE_SERVE_MAX="2048")
    with Vericto(offline=True, offline_cli_path=fake_cli, fail_mode="open") as guard:
        guard.check("SELECT 1", "postgres")
        with pytest.raises(VerictoConfigError) as info:
            guard.check("SELECT '" + "x" * 4000 + "'", "postgres")
        assert info.value.code == "TOO_LARGE"
        assert guard.check("SELECT 2", "postgres").decision is Decision.ALLOWED
    assert len(started(pids)) == 1


def test_too_large_answer_with_a_null_id(fake_cli: str, mode: Any, pids: Path) -> None:
    # The hello announces more than the fake enforces: the server's TOO_LARGE (id null) is used.
    mode("", FAKE_SERVE_MAX="2048", FAKE_SERVE_HELLO_MAX="1048576")
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard:
        with pytest.raises(VerictoConfigError, match="TOO_LARGE") as info:
            guard.enforce("SELECT '" + "x" * 4000 + "'", "postgres")
        assert info.value.code == "TOO_LARGE"
        assert guard.check("DROP TABLE t", "postgres").blocked
    assert len(started(pids)) == 1


def test_unsupported_dialect_is_refused_before_any_process(fake_cli: str, pids: Path) -> None:
    with (
        Vericto(offline=True, offline_cli_path=fake_cli, fail_mode="open") as guard,
        pytest.raises(VerictoUnsupportedDialect, match="local Vericto engine"),
    ):
        guard.check("SELECT 1", "db2")
    assert started(pids) == []
    evaluator = OfflineEvaluator(cli_path=fake_cli)
    try:
        with pytest.raises(VerictoUnsupportedDialect):
            evaluator.evaluate("SELECT 1", "db2")  # the server's UNSUPPORTED_DIALECT
    finally:
        evaluator.close()


def test_empty_sql_is_an_invalid_request(offline_guard: Vericto) -> None:
    with pytest.raises(VerictoConfigError) as info:
        offline_guard.check("   ", "postgres")
    assert info.value.code == "INVALID_REQUEST"


def test_lone_surrogates_are_sent_escaped(offline_guard: Vericto) -> None:
    assert offline_guard.check("SELECT '\udcff'", "postgres").decision is Decision.ALLOWED


def test_context_and_end_user_are_accepted(offline_guard: Vericto, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vericto")
    v = offline_guard.check("SELECT 1", "postgres", context={"ticket": "T-1"}, end_user={"id": "u1"})
    assert v.decision is Decision.ALLOWED
    assert "no business policies" in caplog.text


def test_cache_works_offline(fake_cli: str, pids: Path) -> None:
    with Vericto(offline=True, offline_cli_path=fake_cli, cache_ttl=60) as guard:
        assert not guard.check("DROP TABLE t", "postgres").cached
        assert guard.check("DROP TABLE t", "postgres").cached


def test_cli_stderr_goes_to_debug_and_sql_is_never_logged(
    fake_cli: str, mode: Any, caplog: pytest.LogCaptureFixture
) -> None:
    mode("crash")
    caplog.set_level(logging.DEBUG, logger="vericto")
    secret = "SELECT 'CRASH' AS s3cr3t_literal"
    with Vericto(offline=True, offline_cli_path=fake_cli, fail_mode="open") as guard:
        guard.check(secret, "postgres")
        with pytest.warns(VerictoFailOpenWarning):
            guard.enforce(secret, "postgres")
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert "vericto serve: ready" in debug
    assert "vericto serve: engine panicked" in debug
    assert "s3cr3t" not in caplog.text


# -- lifecycle ------------------------------------------------------------------------


def test_spawned_lazily_and_closed(fake_cli: str, pids: Path) -> None:
    guard = Vericto(offline=True, offline_cli_path=fake_cli)
    assert started(pids) == []
    guard.check("SELECT 1", "postgres")
    proc = process_of(guard)
    guard.close()
    assert proc.returncode == 0  # reaped: EOF on stdin, exit 0
    guard.close()  # idempotent
    guard.check("SELECT 1", "postgres")  # restarts after close
    assert len(started(pids)) == 2
    guard.close()


def test_context_manager_closes(fake_cli: str) -> None:
    with Vericto(offline=True, offline_cli_path=fake_cli) as guard:
        guard.check("SELECT 1", "postgres")
        proc = process_of(guard)
    assert proc.returncode is not None


def test_garbage_collected_guard_stops_its_process(fake_cli: str, pids: Path) -> None:
    guard = Vericto(offline=True, offline_cli_path=fake_cli)
    guard.check("SELECT 1", "postgres")
    proc = process_of(guard)
    del guard
    gc.collect()
    proc.wait(5)
    assert proc.returncode is not None


def test_interpreter_exit_stops_the_process(fake_cli: str, pids: Path, tmp_path: Path) -> None:
    script = (
        "import vericto\n"
        f"g = vericto.Vericto(offline=True, offline_cli_path={fake_cli!r})\n"
        "g.check('SELECT 1', 'postgres')\n"
        "keep = g\n"
    )
    env = dict(os.environ)
    subprocess.run([sys.executable, "-c", script], check=True, env=env, timeout=30)  # noqa: S603
    (pid,) = started(pids)
    deadline = time.monotonic() + 5
    while not gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert gone(pid)


def test_evaluator_repr_and_supported_dialects(fake_cli: str) -> None:
    evaluator = OfflineEvaluator(cli_path=fake_cli, profile="ci")
    assert "profile='ci'" in repr(evaluator)
    assert "postgres" in (evaluator.supported_dialects() or ())
    assert repr(Vericto(evaluator)).startswith("Vericto(evaluator=OfflineEvaluator(")
    evaluator.close()


# -- vericto-dbt --offline ----------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "jaffle"
    shutil.copytree(FIXTURE, dest)
    return dest


def test_dbt_offline(project: Path, fake_cli: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VERICTO_CLI", fake_cli)
    out = tmp_path / "report.json"
    code = dbt.main(["check", "--project-dir", str(project), "--offline", "--format", "json", "-o", str(out)])
    report = json.loads(out.read_text())
    assert report["mode"] == "offline"
    assert report["ruleset_version"] == "standard@engine-5.0.0"
    assert report["ci_checks_remaining"] is None
    assert report["summary"]["statements"] == len(report["results"]) > 0
    assert any("Offline: the standard rules" in n for n in report["notes"])
    assert code == (1 if report["summary"]["blocked"] else 0)


def test_dbt_offline_from_the_variable_and_choose_one(
    project: Path, fake_cli: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VERICTO_CLI", fake_cli)
    monkeypatch.setenv("VERICTO_OFFLINE", "1")
    assert dbt.main(["check", "--project-dir", str(project), "-q"]) in (dbt.EXIT_OK, dbt.EXIT_FINDINGS)
    assert "statement(s) in" in capsys.readouterr().out
    monkeypatch.setenv("VERICTO_API_KEY", API_KEY)
    assert dbt.main(["check", "--project-dir", str(project)]) == dbt.EXIT_CONFIG
    assert "choose one" in capsys.readouterr().err


def test_dbt_offline_engine_failure_is_exit_4(
    project: Path, fake_cli: str, mode: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    mode("crash")
    client = OfflineCiClient(cli_path=fake_cli)
    try:
        # Every model now says CRASH, which crashes the fake.
        path = project / "target" / "manifest.json"
        data = json.loads(path.read_text())
        for node in data["nodes"].values():
            if node.get("compiled_code"):
                node["compiled_code"] += " -- CRASH"
        path.write_text(json.dumps(data))
        assert dbt.main(["check", "--project-dir", str(project)], client=client) == dbt.EXIT_BACKEND
        assert "no verdict from the local engine" in capsys.readouterr().err
    finally:
        client.close()


def test_offline_ci_client_shapes(fake_cli: str, mode: Any) -> None:
    client = OfflineCiClient(cli_path=fake_cli)
    try:
        assert not client.config().check_files
        body = client.check_queries(["SELECT 1", "DROP TABLE t"], "postgres")
        assert [q["line"] for q in body["queries"]] == [1, 2]
        assert body["queries"][1]["status"] == "FLAGGED"  # ci profile
        files = client.check_files([CiFile("a.sql", "SELECT 1")], "postgres")
        assert (files["queries"][0]["file_index"], files["queries"][0]["line"]) == (1, 1)
        with pytest.raises(CiRequestError):
            client.check_queries([" "], "postgres")
        assert "OfflineCiClient(profile='ci'" in repr(client)
    finally:
        client.close()
