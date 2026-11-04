# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""A stand-in for ``vericto serve --stdio`` (protocol 1), for the offline unit tests.

It is not the engine: its rules are string checks. ``DROP`` is VERICTO-010 (BLOCKED
under the ``runtime`` profile, FLAGGED under ``ci``), ``DELETE`` without ``WHERE`` is
VERICTO-001 (BLOCKED), ``SELECT *`` is VERICTO-060 (MONITORED), ``GARBAGE`` is a
PARSE_ERROR, anything else is ALLOWED. Several statements give the strongest one.

``FAKE_SERVE_MODE`` changes the behavior (comma-separated):

* ``old``: the hello says CLI 2.0.0. ``protocol2``: protocol 2. ``no_hello``: exit 2
  before the hello, as a CLI without ``serve`` does. ``silent``: never says hello.
* ``crash_once``: exit 1 on the earliest request whose SQL contains ``CRASH`` (state in
  ``FAKE_SERVE_STATE``, a file path); ``crash``: on every one.
* ``malformed``: answer ``MALFORMED`` requests with a line that is not JSON.
* ``internal``: answer ``INTERNAL`` requests with an INTERNAL error.
* ``reverse``: buffer two ``PAIR`` requests and answer them in reverse order.
* ``hang``: never answer ``HANG`` requests.
* ``bad_result``: answer with a result that has no queries.

``FAKE_SERVE_MAX`` is the line limit it enforces (default 1 MiB), ``FAKE_SERVE_HELLO_MAX``
the one its hello announces. ``FAKE_SERVE_PIDS`` (a file path) records each PID.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ENGINE = "5.0.0"
_RANK = {"BLOCKED": 0, "FLAGGED": 1, "PARSE_ERROR": 2, "MONITORED": 3, "ALLOWED": 4}


def _verdict(statement: str, profile: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    upper = statement.upper()
    if "GARBAGE" in upper:
        return "PARSE_ERROR", None, None, None
    if "DROP" in upper:
        status, action = ("BLOCKED", "block") if profile == "runtime" else ("FLAGGED", "flag")
        return status, action, "VERICTO-010", "critical"
    if upper.lstrip().startswith("DELETE") and "WHERE" not in upper:
        return "BLOCKED", "block", "VERICTO-001", "critical"
    if "SELECT *" in upper:
        return "MONITORED", "monitor", "VERICTO-060", "low"
    return "ALLOWED", None, None, None


def result_for(sql: str, profile: str) -> Dict[str, Any]:
    found = [_verdict(s, profile) for s in sql.split(";") if s.strip()] or [("ALLOWED", None, None, None)]
    status, action, rule, severity = min(found, key=lambda v: _RANK[v[0]])
    counts = {k: 0 for k in ("allowed", "blocked", "flagged", "monitored", "parse_errors")}
    counts["parse_errors" if status == "PARSE_ERROR" else status.lower()] += 1
    return {
        "summary": {"total": 1, **counts, "ruleset_version": f"standard@engine-{ENGINE}"},
        "queries": [
            {
                "line": 1,
                "file_index": None,
                "status": status,
                "action": action,
                "rule_code": rule,
                "severity": severity,
                "ast_node_path": "DropStmt" if rule == "VERICTO-010" else None,
                "suggested_fix": "DELETE FROM t WHERE id = $1" if rule == "VERICTO-001" else None,
                "sql_preview": sql[:100],
            }
        ],
        "exit_code": 1 if status == "BLOCKED" else 0,
        "mode": "offline",
        "engine_version": ENGINE,
    }


def _emit(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    modes = set(filter(None, os.environ.get("FAKE_SERVE_MODE", "").split(",")))
    pids = os.environ.get("FAKE_SERVE_PIDS")
    if pids:
        with open(pids, "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
    if sys.argv[1:] != ["serve", "--stdio"]:
        sys.stderr.write("error: unrecognized subcommand\n")
        return 2
    if "no_hello" in modes:
        sys.stderr.write("error: unrecognized subcommand 'serve'\n")
        return 2
    if "silent" in modes:
        time.sleep(60)
        return 0
    limit = int(os.environ.get("FAKE_SERVE_MAX", str(1 << 20)))
    announced = int(os.environ.get("FAKE_SERVE_HELLO_MAX", str(limit)))
    _emit(
        {
            "max_line_bytes": announced,
            "profiles": ["runtime", "ci"],
            "type": "hello",
            "protocol": 2 if "protocol2" in modes else 1,
            "cli_version": "2.0.0" if "old" in modes else "2.1.0",
            "engine_version": ENGINE,
            "future_field": {"ignored": True},
        }
    )
    sys.stderr.write("ready\n")
    sys.stderr.flush()
    held: List[Dict[str, Any]] = []
    for raw in sys.stdin.buffer:
        line = raw.rstrip(b"\r\n")
        if not line.strip():
            continue
        if len(line) > limit:
            _emit({"id": None, "ok": False, "error": {"code": "TOO_LARGE", "message": "line too long"}})
            continue
        request = json.loads(line)
        op = request.get("op")
        rid = request.get("id")
        if op == "shutdown":
            break
        if op == "ping":
            _emit({"id": rid, "ok": True})
            continue
        sql = request.get("sql") or ""
        if not sql.strip():
            _emit({"id": rid, "ok": False, "error": {"code": "INVALID_REQUEST", "message": "sql is empty"}})
            continue
        if request.get("dialect") not in (None, "postgres", "mysql", "oracle", "mssql", "snowflake", "sqlite"):
            message = f"unsupported dialect '{request.get('dialect')}'"
            _emit({"id": rid, "ok": False, "error": {"code": "UNSUPPORTED_DIALECT", "message": message}})
            continue
        if "CRASH" in sql and ("crash" in modes or "crash_once" in modes):
            state = Path(os.environ.get("FAKE_SERVE_STATE", "/nonexistent"))
            if "crash" in modes or not state.exists():
                if "crash_once" in modes:
                    state.write_text("crashed", encoding="utf-8")
                sys.stderr.write("engine panicked\n")
                sys.stderr.flush()
                os._exit(1)
        if "MALFORMED" in sql and "malformed" in modes:
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            continue
        if "INTERNAL" in sql and "internal" in modes:
            _emit({"id": rid, "ok": False, "error": {"code": "INTERNAL", "message": "engine failed"}})
            continue
        if "HANG" in sql and "hang" in modes:
            continue
        if "bad_result" in modes:
            _emit({"id": rid, "ok": True, "result": {"queries": []}})
            continue
        answer = {"id": rid, "ok": True, "result": result_for(sql, request.get("profile") or "runtime")}
        if "reverse" in modes and "PAIR" in sql.upper():
            held.append(answer)
            if len(held) == 2:
                for item in reversed(held):
                    _emit(item)
                held.clear()
            continue
        _emit(answer)
    for item in held:
        _emit(item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
