# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Verdict.from_response(): how a Runtime API body becomes a decision, and the errors built from it."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from vericto import Decision, Finding, Verdict, VerictoBlocked, VerictoUnsupportedDialect


def parse(body: Dict[str, Any], sql: str = "SELECT 1") -> Verdict:
    return Verdict.from_response(body, sql, "postgres")


@pytest.mark.parametrize("decision", ["ALLOWED", "FLAGGED", "MONITORED", "PARSE_ERROR", "BLOCKED"])
def test_server_decisions(decision: str) -> None:
    assert parse({"decision": decision}).decision is Decision(decision)
    assert parse({"decision": decision.lower()}).decision is Decision(decision)


@pytest.mark.parametrize("decision", [None, "", "UNEVALUATED", "SOMETHING_NEW", 42])
def test_missing_unknown_or_local_decisions_fail_closed(decision: Any) -> None:
    # UNEVALUATED is the client's own fail-open marker: a server must never be able to send it.
    verdict = parse({"decision": decision, "rewritten_query": "SELECT '***'"})
    assert verdict.decision is Decision.BLOCKED
    assert verdict.blocked and not verdict.degraded
    assert verdict.rewritten_query is None and verdict.sql_to_execute == "SELECT 1"


def test_full_body_is_modelled() -> None:
    body = {
        "decision": "FLAGGED",
        "rule_code": "VERICTO-085",
        "rule_id": "r-85",
        "severity": "low",
        "ast_node_path": "Select.projection[0]",
        "rewritten_query": "SELECT '***' AS email FROM users",
        "parse_error": "",
        "sensitive_columns": [{"table": "users", "column": "email", "action": "mask"}],
        "access_denied": [{"schema": "hr", "table": "salaries"}],
        "event_id": "evt-1",
        "ruleset_version": "rs-9",
        "future_field": {"x": 1},
    }
    verdict = parse(body, "SELECT email FROM users")
    assert verdict.masked and verdict.sql_to_execute == "SELECT '***' AS email FROM users"
    assert verdict.parse_error is None  # "" is "absent"
    assert verdict.sensitive_columns == ({"table": "users", "column": "email", "action": "mask"},)
    assert verdict.access_denied == ({"schema": "hr", "table": "salaries"},)
    assert verdict.event_id == "evt-1" and verdict.ruleset_version == "rs-9"
    assert verdict.raw["future_field"] == {"x": 1}
    assert verdict.findings == (
        Finding(rule_code="VERICTO-085", severity="low", rule_id="r-85", ast_node_path="Select.projection[0]"),
    )
    assert not verdict.cached


def test_findings_list_takes_precedence_and_skips_malformed_items() -> None:
    body = {
        "decision": "BLOCKED",
        "rule_code": "VERICTO-010",
        "findings": [
            {"rule_code": "VERICTO-010", "severity": "critical", "suggested_safe_query": "DELETE ... WHERE id = 1"},
            {"code": "VERICTO-030", "suggested_fix": "add LIMIT"},
            "not a mapping",
            {"severity": "high"},  # no rule code
        ],
    }
    findings = parse(body).findings
    assert [f.rule_code for f in findings] == ["VERICTO-010", "VERICTO-030"]
    assert findings[0].suggested_fix == "DELETE ... WHERE id = 1"
    assert findings[1].suggested_fix == "add LIMIT"


def test_violations_alias_and_fallback_to_the_deciding_rule() -> None:
    assert [f.rule_code for f in parse({"decision": "FLAGGED", "violations": [{"code": "V-1"}]}).findings] == ["V-1"]
    # A list with nothing usable falls back to the top-level rule.
    assert [f.rule_code for f in parse({"decision": "FLAGGED", "rule_code": "V-2", "findings": [{}]}).findings] == [
        "V-2"
    ]
    assert parse({"decision": "ALLOWED", "findings": "garbage"}).findings == ()


def test_default_block_message_is_not_repeated() -> None:
    assert parse({"decision": "BLOCKED", "rule_code": "V-1", "error": "Query blocked by rule V-1"}).message is None
    assert parse({"decision": "BLOCKED", "rule_code": "V-1", "error": "DELETE without WHERE"}).message == (
        "DELETE without WHERE"
    )


def test_blocked_error_message() -> None:
    full = parse(
        {
            "decision": "BLOCKED",
            "rule_code": "VERICTO-010",
            "severity": "critical",
            "error": "DELETE without WHERE",
            "suggested_safe_query": "DELETE FROM t WHERE id = 1",
        },
        "DELETE FROM t",
    )
    err = VerictoBlocked(full, "DELETE FROM t")
    assert str(err) == (
        "Query blocked by VERICTO-010 (severity critical): DELETE without WHERE. "
        "Suggested fix: DELETE FROM t WHERE id = 1"
    )
    assert str(VerictoBlocked(parse({"decision": "BLOCKED"}), "X")) == "Query blocked by a Vericto rule"


def test_unevaluated_verdict() -> None:
    verdict = Verdict.unevaluated("SELECT 1", "sqlite", "timeout")
    assert verdict.degraded and not verdict.blocked and not verdict.masked
    assert verdict.message == "timeout" and verdict.sql_to_execute == "SELECT 1"


def test_unsupported_dialect_message() -> None:
    err = VerictoUnsupportedDialect("sqlite", [], "https://api.example.com")
    assert "none reported" in str(err) and err.code == "UNSUPPORTED_DIALECT"
    assert err.supported == ()
