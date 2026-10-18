# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""``vericto-dbt check``: check a dbt project's compiled SQL with Vericto in CI.

Run it after ``dbt compile`` (or ``dbt build --empty``)::

    dbt compile
    VERICTO_API_KEY=vtro_... vericto-dbt check --format sarif --output vericto.sarif

It reads ``target/manifest.json`` and evaluates, with the Vericto CI-check API
(``POST /api/v1/ci/check-key``, billed against the monthly CI-check allowance, not
the runtime quota), the compiled SQL of every model and snapshot of the project,
their ``pre-hook`` / ``post-hook`` statements and the ``on-run-start`` /
``on-run-end`` hooks. Nothing runs against your warehouse, and dbt itself is not
imported: only its artifacts are read.

The dialect comes from the manifest's adapter type (``--dialect`` overrides it):
snowflake, postgres, redshift (evaluated as postgres), mysql/mariadb, sqlserver
(as mssql) and oracle. Other adapters stop with exit code 2.

Exit codes (as vericto-cli): 0 nothing at or above ``--fail-on``; 1 a finding at
or above it; 2 usage error (no manifest, unsupported adapter, nothing compiled);
3 key, scope or monthly allowance problem; 4 Vericto unreachable.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ._version import __version__
from .ci_api import MAX_ENTRIES_PER_CALL, CiCheckClient, CiFile, CiRequestError, provenance_from_env
from .errors import VerictoConfigError, VerictoQuotaExceeded, VerictoUnavailable
from .sanitize import sanitize

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_BACKEND = 4

#: dbt adapter type -> Vericto dialect.
ADAPTER_DIALECTS: Mapping[str, str] = {
    "snowflake": "snowflake",
    "postgres": "postgres",
    "redshift": "postgres",
    "mysql": "mysql",
    "mysql5": "mysql",
    "mariadb": "mysql",
    "sqlserver": "mssql",
    "oracle": "oracle",
}
#: Adapters evaluated with a related dialect: said in the output.
APPROXIMATED = {
    "redshift": "Redshift SQL is evaluated as PostgreSQL; Redshift-only syntax may come back as PARSE_ERROR."
}
DIALECTS = ("postgres", "mysql", "oracle", "mssql", "snowflake")

DEFAULT_RESOURCE_TYPES = ("model", "snapshot", "operation")
RESOURCE_TYPES = ("model", "snapshot", "operation", "analysis", "test")
FAIL_ON = {
    "blocked": {"BLOCKED"},
    "flagged": {"BLOCKED", "FLAGGED"},
    "any": {"BLOCKED", "FLAGGED", "MONITORED", "PARSE_ERROR"},
}
FAIL_ON_ALIASES = {"block": "blocked", "flag": "flagged"}
_ORDER = {"BLOCKED": 0, "FLAGGED": 1, "PARSE_ERROR": 2, "MONITORED": 3, "ALLOWED": 4}
#: Entries per request: well below the backend's 500 files and 2000 statements.
CHUNK = 100


class UsageError(Exception):
    """Bad arguments or artifacts: exit code 2."""


@dataclass
class Item:
    """One piece of SQL to check: a node's compiled SQL or one of its hooks."""

    unique_id: str
    resource_type: str
    name: str
    kind: str  # "model", "snapshot", "pre-hook", "post-hook", "on-run-start"...
    path: str  # the node's original_file_path, relative to the project
    sql: str
    hook_index: Optional[int] = None

    @property
    def label(self) -> str:
        if self.hook_index is None:
            return self.path
        return f"{self.path}#{self.kind}[{self.hook_index}]"


@dataclass
class Skipped:
    unique_id: str
    kind: str
    path: str
    reason: str


@dataclass
class Result:
    item: Item
    status: str
    line: int = 1
    rule_code: Optional[str] = None
    severity: Optional[str] = None
    action: Optional[str] = None
    suggested_fix: Optional[str] = None
    rewritten_query: Optional[str] = None
    statement_hash: Optional[str] = None


@dataclass
class Report:
    project: Optional[str]
    adapter_type: Optional[str]
    dialect: str
    fail_on: str
    results: List[Result] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    ruleset_version: Optional[str] = None
    ci_checks_remaining: Optional[int] = None
    sanitized: bool = False

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def failing(self) -> List[Result]:
        statuses = FAIL_ON[self.fail_on]
        return [r for r in self.results if r.status in statuses]

    @property
    def exit_code(self) -> int:
        return EXIT_FINDINGS if self.failing else EXIT_OK

    def summary(self) -> Dict[str, int]:
        return {
            "statements": len(self.results),
            "blocked": self.count("BLOCKED"),
            "flagged": self.count("FLAGGED"),
            "monitored": self.count("MONITORED"),
            "parse_errors": self.count("PARSE_ERROR"),
            "allowed": self.count("ALLOWED"),
            "skipped": len(self.skipped),
        }


# -- manifest -----------------------------------------------------------------------


def load_manifest(path: Path) -> Dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise UsageError(f"{path} not found: run `dbt compile` (or `dbt build --empty`) first.") from None
    except (OSError, ValueError) as exc:
        raise UsageError(f"Cannot read {path}: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
        raise UsageError(f"{path} is not a dbt manifest (no 'nodes').")
    return data


def dialect_for(adapter_type: Optional[str], override: Optional[str]) -> Tuple[str, List[str]]:
    """The Vericto dialect for a dbt adapter, and notes to print."""
    notes: List[str] = []
    if override:
        name = override.strip().lower()
        if name not in DIALECTS:
            raise UsageError(f"--dialect must be one of {', '.join(DIALECTS)}, got {override!r}.")
        if adapter_type and ADAPTER_DIALECTS.get(adapter_type) != name:
            notes.append(f"The dbt adapter is '{adapter_type}'; evaluating as '{name}' because of --dialect.")
        return name, notes
    if not adapter_type:
        raise UsageError("The manifest has no metadata.adapter_type: pass --dialect.")
    dialect = ADAPTER_DIALECTS.get(adapter_type)
    if dialect is None:
        supported = ", ".join(sorted(ADAPTER_DIALECTS))
        raise UsageError(
            f"Vericto does not evaluate the dbt adapter '{adapter_type}' (supported adapters: {supported}). "
            "Nothing was checked. Pass --dialect only if its SQL really is one of: " + ", ".join(DIALECTS) + "."
        )
    if adapter_type in APPROXIMATED:
        notes.append(APPROXIMATED[adapter_type])
    return dialect, notes


def _has_jinja(sql: str) -> bool:
    return "{{" in sql or "{%" in sql or "{#" in sql


def _hooks(config: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    raw = config.get(key)
    if raw is None:
        raw = config.get(key.replace("-", "_"))
    if not isinstance(raw, list):
        return []
    out: List[Mapping[str, Any]] = []
    for hook in raw:
        if isinstance(hook, str):
            out.append({"sql": hook})
        elif isinstance(hook, Mapping):
            out.append(hook)
    return out


def _compiled_sql(node: Mapping[str, Any], project_dir: Path, target: Path, sql_source: str) -> Optional[str]:
    package = str(node.get("package_name") or "")
    original = str(node.get("original_file_path") or node.get("path") or "")
    if sql_source == "run":
        candidate = target / "run" / package / original
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        return None
    code = node.get("compiled_code") or node.get("compiled_sql")
    if isinstance(code, str) and code.strip():
        return code
    compiled_path = node.get("compiled_path")
    candidates = [project_dir / str(compiled_path)] if compiled_path else []
    candidates.append(target / "compiled" / package / original)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return None


def collect(
    manifest: Mapping[str, Any],
    *,
    project_dir: Path,
    target: Path,
    resource_types: Sequence[str] = DEFAULT_RESOURCE_TYPES,
    select: Sequence[str] = (),
    include_packages: bool = False,
    hooks: bool = True,
    sql_source: str = "compiled",
) -> Tuple[List[Item], List[Skipped]]:
    """The SQL to check, in manifest order, and what was skipped and why."""
    project = (manifest.get("metadata") or {}).get("project_name")
    items: List[Item] = []
    skipped: List[Skipped] = []
    wanted = set(resource_types)
    for unique_id, node in manifest.get("nodes", {}).items():
        if not isinstance(node, Mapping):
            continue
        rtype = str(node.get("resource_type") or "")
        if rtype not in wanted:
            continue
        if project and not include_packages and node.get("package_name") not in (None, project):
            continue
        name = str(node.get("name") or unique_id)
        path = str(node.get("original_file_path") or node.get("path") or unique_id)
        if path.startswith("./"):
            path = path[2:]
        if select and not any(
            fnmatch.fnmatchcase(value, pattern) for pattern in select for value in (name, unique_id, path)
        ):
            continue
        config = node.get("config") or {}
        if config.get("enabled") is False:
            continue

        if rtype == "operation":
            # on-run-start / on-run-end: "operation.<project>.<project>-on-run-start-0".
            kind = "on-run-end" if "on-run-end" in unique_id else "on-run-start"
            sql = node.get("compiled_code") or node.get("compiled_sql")
            if not (isinstance(sql, str) and sql.strip()):
                raw = node.get("raw_code") or node.get("raw_sql") or ""
                sql = raw if isinstance(raw, str) and not _has_jinja(raw) else None
            if sql is None or not str(sql).strip():
                skipped.append(Skipped(unique_id, kind, path, "hook contains Jinja; dbt renders it only at run time"))
                continue
            items.append(Item(unique_id, rtype, name, kind, path, str(sql), hook_index=_hook_index(unique_id)))
            continue

        if hooks:
            for key in ("pre-hook", "post-hook"):
                for index, hook in enumerate(_hooks(config, key)):
                    sql = str(hook.get("sql") or "")
                    if not sql.strip():
                        continue
                    if _has_jinja(sql):
                        skipped.append(
                            Skipped(
                                unique_id,
                                key,
                                f"{path}#{key}[{index}]",
                                "hook contains Jinja; rendered only at run time",
                            )
                        )
                        continue
                    items.append(Item(unique_id, rtype, name, key, path, sql, hook_index=index))

        compiled = _compiled_sql(node, project_dir, target, sql_source)
        if compiled is None or not compiled.strip():
            where = "target/run" if sql_source == "run" else "the manifest or target/compiled"
            skipped.append(Skipped(unique_id, rtype, path, f"no compiled SQL in {where}: run dbt compile first"))
            continue
        items.append(Item(unique_id, rtype, name, rtype, path, compiled))
    return items, skipped


def _hook_index(unique_id: str) -> Optional[int]:
    tail = unique_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


# -- evaluation ---------------------------------------------------------------------


def evaluate(
    client: CiCheckClient,
    items: Sequence[Item],
    dialect: str,
    *,
    report: Report,
    database_id: Optional[str] = None,
    allow_unsanitized: bool = False,
) -> None:
    """Check ``items`` and add the results to ``report``. Raises on API errors."""
    try:
        config = client.config()
    except VerictoUnavailable:
        if not allow_unsanitized:
            raise
        config = None
        report.notes.append("The workspace configuration could not be read: SQL sent as written (--allow-unsanitized).")
    sanitized = bool(config and config.sanitized)
    report.sanitized = sanitized
    use_files = bool(config and config.check_files)
    provenance = provenance_from_env()
    file_name = f"dbt:{report.project}" if report.project else "dbt"
    chunk = min(CHUNK, MAX_ENTRIES_PER_CALL)

    for start in range(0, len(items), chunk):
        batch = list(items[start : start + chunk])
        texts = [sanitize(i.sql, dialect) if sanitized else i.sql for i in batch]
        if use_files:
            body = client.check_files(
                [CiFile(i.label, t) for i, t in zip(batch, texts, strict=True)],
                dialect,
                database_id=database_id,
                file_name=file_name,
                provenance=provenance,
            )
        else:
            body = client.check_queries(
                texts, dialect, database_id=database_id, file_name=file_name, provenance=provenance
            )
        for q in body.get("queries", []):
            if not isinstance(q, Mapping):
                continue
            if use_files:
                index = int(q.get("file_index") or 0) - 1
                line = int(q.get("line") or 1)
            else:
                index = int(q.get("line") or 0) - 1
                line = 1
            if not 0 <= index < len(batch):
                continue
            report.results.append(
                Result(
                    item=batch[index],
                    status=str(q.get("status") or "BLOCKED").upper(),
                    line=line,
                    rule_code=_opt(q.get("rule_code")),
                    severity=_opt(q.get("severity")),
                    action=_opt(q.get("action")),
                    suggested_fix=_opt(q.get("suggested_fix")),
                    rewritten_query=_opt(q.get("rewritten_query")),
                    statement_hash=_opt(q.get("statement_hash")),
                )
            )
        summary = body.get("summary")
        if isinstance(summary, Mapping) and summary.get("ruleset_version"):
            report.ruleset_version = str(summary["ruleset_version"])
        remaining = body.get("ci_checks_remaining")
        report.ci_checks_remaining = remaining if isinstance(remaining, int) else None


def _opt(value: Any) -> Optional[str]:
    return None if value is None or value == "" else str(value)


# -- output -------------------------------------------------------------------------


def render_table(report: Report, *, quiet: bool = False) -> str:
    lines: List[str] = []
    for note in report.notes:
        lines.append(f"note: {note}")
    rows = sorted(report.results, key=lambda r: (_ORDER.get(r.status, 9), r.item.label, r.line))
    if not quiet and rows:
        header = ("STATUS", "RULE", "SEVERITY", "NODE", "LOCATION")
        table = [header] + [
            (r.status, r.rule_code or "-", r.severity or "-", r.item.unique_id, f"{r.item.label}:{r.line}")
            for r in rows
        ]
        widths = [max(len(row[c]) for row in table) for c in range(len(header))]
        for row in table:
            lines.append("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)).rstrip())
        fixes = [r for r in rows if r.status in ("BLOCKED", "FLAGGED") and r.suggested_fix]
        if fixes:
            lines.append("")
            for r in fixes:
                lines.append(f"{r.item.label}:{r.line} {r.rule_code or ''} suggested fix: {r.suggested_fix}")
    if not quiet and report.skipped:
        lines.append("")
        for skip in report.skipped:
            lines.append(f"skipped {skip.path} ({skip.unique_id}): {skip.reason}")
    s = report.summary()
    verdict = "FAIL" if report.exit_code else "PASS"
    lines.append("")
    lines.append(
        f"vericto-dbt: {verdict} - {s['statements']} statement(s) in {report.dialect}: {s['blocked']} blocked, "
        f"{s['flagged']} flagged, {s['monitored']} monitored, {s['parse_errors']} parse error(s), "
        f"{s['allowed']} allowed; {s['skipped']} skipped (fail-on {report.fail_on})"
    )
    if report.ci_checks_remaining is not None:
        lines.append(f"CI checks left this month: {report.ci_checks_remaining}")
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    doc = {
        "tool": "vericto-dbt",
        "version": __version__,
        "project": report.project,
        "adapter_type": report.adapter_type,
        "dialect": report.dialect,
        "fail_on": report.fail_on,
        "exit_code": report.exit_code,
        "sanitized": report.sanitized,
        "ruleset_version": report.ruleset_version,
        "ci_checks_remaining": report.ci_checks_remaining,
        "summary": report.summary(),
        "notes": report.notes,
        "results": [
            {
                "unique_id": r.item.unique_id,
                "resource_type": r.item.resource_type,
                "name": r.item.name,
                "kind": r.item.kind,
                "hook_index": r.item.hook_index,
                "path": r.item.path,
                "line": r.line,
                "status": r.status,
                "rule_code": r.rule_code,
                "severity": r.severity,
                "action": r.action,
                "suggested_fix": r.suggested_fix,
                "rewritten_query": r.rewritten_query,
                "statement_hash": r.statement_hash,
            }
            for r in report.results
        ],
        "skipped": [
            {"unique_id": s.unique_id, "kind": s.kind, "path": s.path, "reason": s.reason} for s in report.skipped
        ],
    }
    return json.dumps(doc, indent=2) + "\n"


_SARIF_LEVEL = {"BLOCKED": "error", "FLAGGED": "warning", "MONITORED": "note", "PARSE_ERROR": "note"}


def render_sarif(report: Report, *, uri_prefix: str = "") -> str:
    """SARIF 2.1.0 for GitHub code scanning. ALLOWED statements are not results.

    Compiled SQL lines do not match the Jinja source, so results point at line 1 of
    the model file and the message gives the line in the compiled SQL.
    """
    rules: Dict[str, Dict[str, Any]] = {}
    results: List[Dict[str, Any]] = []
    for r in report.results:
        level = _SARIF_LEVEL.get(r.status)
        if level is None:
            continue
        rule_id = r.rule_code or ("VERICTO-PARSE-ERROR" if r.status == "PARSE_ERROR" else "VERICTO")
        rules.setdefault(
            rule_id,
            {
                "id": rule_id,
                "name": rule_id,
                "shortDescription": {"text": f"Vericto rule {rule_id}"},
                "helpUri": "https://vericto.com/docs",
            },
        )
        where = r.item.kind if r.item.hook_index is None else f"{r.item.kind}[{r.item.hook_index}]"
        text = f"{r.status}: Vericto {rule_id}" + (f" ({r.severity})" if r.severity else "")
        text += f" in {r.item.unique_id} {where}, line {r.line} of the compiled SQL."
        if r.suggested_fix:
            text += f" Suggested fix: {r.suggested_fix}"
        results.append(
            {
                "ruleId": rule_id,
                "level": level,
                "message": {"text": text},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": (uri_prefix + r.item.path).replace(os.sep, "/")},
                            "region": {"startLine": 1},
                        }
                    }
                ],
                "partialFingerprints": {"verictoStatement/v1": f"{r.item.label}:{r.statement_hash or r.line}"},
            }
        )
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vericto-dbt",
                        "informationUri": "https://vericto.com",
                        "version": __version__,
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(doc, indent=2) + "\n"


# -- command line -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vericto-dbt",
        description="Check a dbt project's compiled SQL with Vericto (CI-check API) before it runs.",
    )
    parser.add_argument("--version", action="version", version=f"vericto-dbt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Check target/manifest.json after dbt compile.")
    check.add_argument("--project-dir", default=".", help="The dbt project directory (default: .).")
    check.add_argument(
        "--target-path",
        default=None,
        help="dbt's target directory, relative to the project (default: $DBT_TARGET_PATH or target).",
    )
    check.add_argument("--manifest", default=None, help="Path to manifest.json (default: <target>/manifest.json).")
    check.add_argument(
        "--dialect", default=None, help=f"Override the dialect read from the adapter ({', '.join(DIALECTS)})."
    )
    check.add_argument(
        "--database-id",
        default=None,
        help="Registered Vericto database (UUID) whose sensitive-column tags apply ($VERICTO_DATABASE_ID).",
    )
    check.add_argument(
        "--sql-source",
        choices=("compiled", "run"),
        default="compiled",
        help="compiled: the model's SELECT (default). run: target/run, the DDL dbt executes (after dbt build/run).",
    )
    check.add_argument(
        "--resource-type",
        action="append",
        choices=RESOURCE_TYPES,
        help="Resource types to check (repeatable; default: model, snapshot, operation).",
    )
    check.add_argument(
        "--select", "-s", action="append", default=[], help="Glob on node name, unique_id or path (repeatable)."
    )
    check.add_argument("--include-packages", action="store_true", help="Also check nodes of installed dbt packages.")
    check.add_argument("--no-hooks", action="store_true", help="Do not check pre-hook / post-hook statements.")
    check.add_argument(
        "--fail-on",
        default="blocked",
        choices=("blocked", "flagged", "any", "block", "flag"),
        help="What fails the run (default: blocked).",
    )
    check.add_argument(
        "--format", choices=("table", "json", "sarif"), default="table", help="Report format (default: table)."
    )
    check.add_argument("--output", "-o", default=None, help="Write the report to a file instead of stdout.")
    check.add_argument(
        "--sarif-uri-prefix",
        default=None,
        help="Prefix for SARIF file paths (default: the project dir relative to the cwd).",
    )
    check.add_argument(
        "--api-url", default=None, help="Vericto API URL ($VERICTO_API_URL, default https://api.vericto.com)."
    )
    check.add_argument(
        "--timeout", type=float, default=None, help="Seconds per request ($VERICTO_TIMEOUT, default 30)."
    )
    check.add_argument(
        "--allow-unsanitized",
        action="store_true",
        help="If the workspace configuration cannot be read, send the SQL as written instead of failing.",
    )
    check.add_argument("--quiet", "-q", action="store_true", help="Only print the summary line (table format).")
    return parser


def _uri_prefix(project_dir: Path, explicit: Optional[str]) -> str:
    if explicit is not None:
        return explicit if not explicit or explicit.endswith("/") else explicit + "/"
    try:
        rel = os.path.relpath(project_dir.resolve(), Path.cwd().resolve())
    except ValueError:
        return ""
    if rel in (".", "") or rel.startswith(".."):
        return ""
    return rel.replace(os.sep, "/") + "/"


def run_check(args: argparse.Namespace, out: IO[str], err: IO[str], client: Optional[CiCheckClient] = None) -> int:
    project_dir = Path(args.project_dir)
    target_rel = args.target_path or os.environ.get("DBT_TARGET_PATH") or "target"
    target = Path(target_rel) if Path(target_rel).is_absolute() else project_dir / target_rel
    manifest_path = Path(args.manifest) if args.manifest else target / "manifest.json"
    fail_on = FAIL_ON_ALIASES.get(args.fail_on, args.fail_on)

    manifest = load_manifest(manifest_path)
    metadata = manifest.get("metadata") or {}
    adapter_type = metadata.get("adapter_type")
    dialect, notes = dialect_for(adapter_type, args.dialect)
    items, skipped = collect(
        manifest,
        project_dir=project_dir,
        target=target,
        resource_types=tuple(args.resource_type or DEFAULT_RESOURCE_TYPES),
        select=tuple(args.select or ()),
        include_packages=args.include_packages,
        hooks=not args.no_hooks,
        sql_source=args.sql_source,
    )
    report = Report(
        project=metadata.get("project_name"),
        adapter_type=adapter_type,
        dialect=dialect,
        fail_on=fail_on,
        skipped=skipped,
        notes=notes,
    )
    if not items:
        if any("no compiled SQL" in s.reason for s in skipped):
            raise UsageError("No compiled SQL found in the manifest: run `dbt compile` (or `dbt build --empty`) first.")
        report.notes.append("Nothing to check.")
    else:
        database_id = args.database_id or os.environ.get("VERICTO_DATABASE_ID") or None
        if client is None:
            client = CiCheckClient(
                api_url=args.api_url, timeout=args.timeout, user_agent=f"vericto-python/{__version__} (dbt)"
            )
        evaluate(
            client, items, dialect, report=report, database_id=database_id, allow_unsanitized=args.allow_unsanitized
        )

    if args.format == "json":
        text = render_json(report)
    elif args.format == "sarif":
        text = render_sarif(report, uri_prefix=_uri_prefix(project_dir, args.sarif_uri_prefix))
    else:
        text = render_table(report, quiet=args.quiet)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        if args.format != "table":
            err.write(render_table(report, quiet=True))
    else:
        out.write(text)
    return report.exit_code


def main(argv: Optional[Iterable[str]] = None, *, client: Optional[CiCheckClient] = None) -> int:
    """Entry point of ``vericto-dbt`` (and ``python -m vericto dbt``)."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    out, err = sys.stdout, sys.stderr
    try:
        return run_check(args, out, err, client)
    except UsageError as exc:
        err.write(f"vericto-dbt: {exc}\n")
        return EXIT_USAGE
    except VerictoQuotaExceeded as exc:
        err.write(f"vericto-dbt: {exc}\n")
        return EXIT_CONFIG
    except VerictoConfigError as exc:
        err.write(f"vericto-dbt: {exc}\n")
        return EXIT_CONFIG
    except CiRequestError as exc:
        err.write(f"vericto-dbt: {exc}\n")
        return EXIT_USAGE
    except VerictoUnavailable as exc:
        err.write(f"vericto-dbt: Vericto is unreachable, nothing was checked: {exc}\n")
        return EXIT_BACKEND


def console_main() -> None:
    sys.exit(main())


__all__ = ["ADAPTER_DIALECTS", "collect", "dialect_for", "main", "render_json", "render_sarif", "render_table"]
