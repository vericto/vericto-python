# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Client for the Vericto CI-check API (``POST /api/v1/ci/check-key``).

The endpoint vericto-cli uses for pre-execution checks: build-time SQL (migrations,
dbt models) is evaluated against the workspace's rules **without running it**, and
each statement spends one check of the workspace's monthly **CI-check** allowance,
not the runtime query quota. A key with the ``ci_dryrun:execute`` scope is required
(the Runtime API's ``proxy:evaluate`` key is a different key).

Used by :mod:`vericto.dbt`. Configuration follows vericto-cli: ``VERICTO_API_KEY``,
``VERICTO_API_URL``, ``VERICTO_TIMEOUT``, ``VERICTO_CA_BUNDLE``.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import urllib3

from ._version import __version__
from .errors import VerictoConfigError, VerictoError, VerictoQuotaExceeded, VerictoUnavailable
from .runtime_api import DEFAULT_API_URL

CHECK_PATH = "/api/v1/ci/check-key"
CONFIG_PATH = "/api/v1/ci/config"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
#: What the backend accepts per request (validators/workspaces.ts).
MAX_ENTRIES_PER_CALL = 500
_MAX_BACKOFF = 10.0
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class CiRequestError(VerictoError):
    """The backend rejected the request itself (HTTP 400): too large, no SQL, bad dialect."""

    def __init__(self, message: str, *, status: int = 400, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class CiConfig:
    """``GET /api/v1/ci/config``: the workspace settings a client needs before sending SQL."""

    telemetry_query_mode: str = "raw"
    capabilities: tuple[str, ...] = ()
    ci_checks_remaining: Optional[int] = None
    ruleset_version: Optional[str] = None
    workspace_name: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def sanitized(self) -> bool:
        return self.telemetry_query_mode == "sanitized"

    @property
    def check_files(self) -> bool:
        return "check_files" in self.capabilities


@dataclass(frozen=True)
class CiFile:
    path: str
    sql: str


class CiCheckClient:
    """Calls the CI-check API. The key never appears in ``repr()``, logs or errors."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        api_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff: float = 0.5,
        ca_bundle: Optional[str] = None,
        user_agent: Optional[str] = None,
        pool_manager: Optional[urllib3.PoolManager] = None,
    ) -> None:
        key = (api_key if api_key is not None else os.environ.get("VERICTO_API_KEY", "")).strip()
        if not key:
            raise VerictoConfigError(
                "No Vericto API key: set VERICTO_API_KEY to a vtro_... key with the ci_dryrun:execute scope."
            )
        if not key.startswith("vtro_"):
            raise VerictoConfigError("The Vericto API key must start with 'vtro_' (the value itself is not shown).")
        self._api_key = key

        url = (api_url if api_url is not None else os.environ.get("VERICTO_API_URL", "")).strip() or DEFAULT_API_URL
        url = url.rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise VerictoConfigError(f"Invalid Vericto API URL {url!r}: expected https://host.")
        if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
            raise VerictoConfigError(f"Refusing to send the API key over plain HTTP to {parts.hostname}: use https://.")
        self.api_url = url

        if timeout is None:
            env_timeout = os.environ.get("VERICTO_TIMEOUT", "").strip()
            try:
                timeout = float(env_timeout) if env_timeout else DEFAULT_TIMEOUT
            except ValueError:
                raise VerictoConfigError(f"VERICTO_TIMEOUT must be a number of seconds, got {env_timeout!r}.") from None
        if timeout <= 0:
            raise VerictoConfigError("timeout must be greater than 0 seconds.")
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._backoff = max(0.0, float(backoff))

        bundle = ca_bundle if ca_bundle is not None else (os.environ.get("VERICTO_CA_BUNDLE") or None)
        if pool_manager is None:
            kwargs: Dict[str, Any] = {"num_pools": 1, "maxsize": 4, "cert_reqs": "CERT_REQUIRED"}
            if bundle:
                kwargs["ca_certs"] = bundle
            pool_manager = urllib3.PoolManager(**kwargs)
        self._http = pool_manager
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": user_agent or f"vericto-python/{__version__}",
        }

    def __repr__(self) -> str:
        return f"CiCheckClient(api_url={self.api_url!r})"

    # -- requests -----------------------------------------------------------------

    def config(self) -> CiConfig:
        """The workspace's CI settings. Spends no check."""
        body = self._request("GET", CONFIG_PATH, None)
        caps = body.get("capabilities")
        remaining = body.get("ci_checks_remaining")
        return CiConfig(
            telemetry_query_mode=str(body.get("telemetry_query_mode") or "raw"),
            capabilities=tuple(str(c) for c in caps) if isinstance(caps, list) else (),
            ci_checks_remaining=remaining if isinstance(remaining, int) else None,
            ruleset_version=_opt(body.get("ruleset_version")),
            workspace_name=_opt(body.get("workspace_name")),
            raw=body,
        )

    def check_files(
        self,
        files: Sequence[CiFile],
        dialect: str,
        *,
        database_id: Optional[str] = None,
        file_name: Optional[str] = None,
        provenance: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        """Whole files: the backend splits them and reports each statement with
        ``file_index`` (1-based, in request order) and its ``line``."""
        payload: Dict[str, Any] = {
            "files": [{"path": f.path[:512], "sql": f.sql} for f in files],
            "dialect": dialect,
            "output_format": "json",
        }
        return self._check(payload, database_id, file_name, provenance)

    def check_queries(
        self,
        queries: Sequence[str],
        dialect: str,
        *,
        database_id: Optional[str] = None,
        file_name: Optional[str] = None,
        provenance: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        """One entry per query; ``line`` is the entry's 1-based position (older backends)."""
        payload: Dict[str, Any] = {
            "queries": [{"line": i + 1, "sql": q} for i, q in enumerate(queries)],
            "dialect": dialect,
            "output_format": "json",
        }
        return self._check(payload, database_id, file_name, provenance)

    def _check(
        self,
        payload: Dict[str, Any],
        database_id: Optional[str],
        file_name: Optional[str],
        provenance: Optional[Mapping[str, str]],
    ) -> Dict[str, Any]:
        if database_id:
            payload["database_id"] = database_id
        if file_name:
            payload["file_name"] = file_name[:255]
        if provenance:
            payload["provenance"] = dict(provenance)
        body = self._request("POST", CHECK_PATH, payload)
        if not isinstance(body.get("queries"), list):
            raise VerictoUnavailable(f"The Vericto API at {self.api_url} answered without results.")
        return body

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        headers = dict(self._headers)
        headers["X-API-Key"] = self._api_key
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last_error = "no attempt made"
        retry_after: Optional[float] = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(self._delay(attempt, retry_after))
            retry_after = None
            try:
                resp = self._http.request(
                    method,
                    self.api_url + path,
                    body=data,
                    headers=headers,
                    timeout=urllib3.Timeout(total=self.timeout),
                    retries=False,
                    redirect=False,
                )
            except urllib3.exceptions.HTTPError as exc:
                last_error = f"{type(exc).__name__} while calling {self.api_url}"
                continue
            body = _json_body(resp.data)
            if resp.status == 200:
                return body
            raw_error = body.get("error")
            error: Mapping[str, Any] = raw_error if isinstance(raw_error, Mapping) else {}
            code = _opt(error.get("code"))
            message = _opt(error.get("message")) or (raw_error if isinstance(raw_error, str) else None)
            if resp.status == 429 and code == "QUOTA_EXCEEDED":
                raise VerictoQuotaExceeded(
                    "The Vericto workspace has used its monthly CI-check allowance"
                    + (f"; it resets at {error.get('resets_at')}" if error.get("resets_at") else "")
                    + ".",
                    resets_at=_opt(error.get("resets_at")),
                    upgrade_url=_opt(error.get("upgrade_url")),
                )
            if resp.status == 429 or resp.status >= 500:
                last_error = f"HTTP {resp.status}" + (f" {code}" if code else "") + f" from {self.api_url}"
                retry_after = _retry_after(resp.headers.get("Retry-After"), error.get("retry_after_seconds"))
                continue
            if resp.status in (401, 403):
                raise VerictoConfigError(
                    f"Vericto refused the request (HTTP {resp.status}{', ' + code if code else ''}): "
                    + (message or "check that VERICTO_API_KEY is valid and has the ci_dryrun:execute scope")
                    + ".",
                    status=resp.status,
                    code=code,
                )
            if resp.status == 404 and path == CONFIG_PATH:
                raise VerictoConfigError(
                    f"The Vericto API at {self.api_url} has no {CONFIG_PATH}: check VERICTO_API_URL.", status=404
                )
            details = error.get("details")
            detail_text = ""
            if isinstance(details, list) and details:
                detail_text = " (" + "; ".join(str(d.get("message", d)) for d in details[:5] if d) + ")"
            raise CiRequestError(
                f"Vericto rejected the request (HTTP {resp.status}{', ' + code if code else ''})"
                + (f": {message}" if message else "")
                + detail_text,
                status=resp.status,
                code=code,
            )
        raise VerictoUnavailable(f"Vericto did not answer after {self.max_retries + 1} attempt(s): {last_error}.")

    def _delay(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return min(retry_after, _MAX_BACKOFF)
        base = self._backoff * (2 ** (attempt - 1))
        return float(min(_MAX_BACKOFF, base * (0.5 + random.random() / 2)))

    def close(self) -> None:
        self._http.clear()


def provenance_from_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Best-effort CI provenance (GitHub Actions, GitLab CI) for the run report."""
    e = os.environ if env is None else env
    out: Dict[str, str] = {}
    if e.get("GITHUB_ACTIONS") == "true":
        out["ci_provider"] = "github"
        if e.get("GITHUB_SHA"):
            out["git_sha"] = e["GITHUB_SHA"]
        if e.get("GITHUB_REF"):
            out["git_ref"] = e["GITHUB_REF"]
        if e.get("GITHUB_ACTOR"):
            out["actor"] = e["GITHUB_ACTOR"]
        if e.get("GITHUB_REPOSITORY") and e.get("GITHUB_RUN_ID"):
            server = e.get("GITHUB_SERVER_URL", "https://github.com")
            out["ci_run_url"] = f"{server}/{e['GITHUB_REPOSITORY']}/actions/runs/{e['GITHUB_RUN_ID']}"
    elif e.get("GITLAB_CI") == "true":
        out["ci_provider"] = "gitlab"
        for key, name in (
            ("git_sha", "CI_COMMIT_SHA"),
            ("git_ref", "CI_COMMIT_REF_NAME"),
            ("ci_run_url", "CI_JOB_URL"),
            ("actor", "GITLAB_USER_LOGIN"),
        ):
            if e.get(name):
                out[key] = e[name]
    limits = {"git_sha": 64, "git_ref": 255, "ci_provider": 32, "ci_run_url": 512, "actor": 128}
    return {k: v[: limits[k]] for k, v in out.items()}


def _json_body(data: bytes) -> Dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8")) if data else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _retry_after(header: Optional[str], body_value: Any) -> Optional[float]:
    for value in (header, body_value):
        try:
            if value is not None:
                return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return None


def _opt(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


__all__ = [
    "CHECK_PATH",
    "CONFIG_PATH",
    "MAX_ENTRIES_PER_CALL",
    "CiCheckClient",
    "CiConfig",
    "CiFile",
    "CiRequestError",
    "provenance_from_env",
]
