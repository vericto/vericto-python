"""Evaluator backed by the Vericto Runtime API (``POST /api/v1/proxy/query``)."""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import urllib3

from ._version import __version__
from .errors import (
    VerictoConfigError,
    VerictoQuotaExceeded,
    VerictoUnavailable,
    VerictoUnsupportedDialect,
)
from .verdict import Verdict

DEFAULT_API_URL = "https://api.vericto.com"
EVALUATE_PATH = "/api/v1/proxy/query"
CONFIG_PATH = "/api/v1/meta/config"
DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_RETRIES = 2

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_BACKOFF = 2.0


class RuntimeApiEvaluator:
    """Evaluates statements with the Vericto Runtime API.

    Every argument falls back to an environment variable, as in ``vericto-cli``:

    * ``api_key``: ``VERICTO_API_KEY`` (required, a ``vtro_`` key)
    * ``database_id``: ``VERICTO_DATABASE_ID`` (required, the database registered
      in your Vericto workspace; its rules and sensitive-column tags apply)
    * ``api_url``: ``VERICTO_API_URL`` (default ``https://api.vericto.com``)
    * ``timeout``: ``VERICTO_TIMEOUT`` (default 5 seconds per attempt)
    * ``ca_bundle``: ``VERICTO_CA_BUNDLE`` (default: the system trust store)

    The API key needs the ``proxy:evaluate`` scope. It is sent in the ``X-API-Key``
    header over HTTPS only (plain HTTP is accepted for ``localhost`` alone) and never
    appears in ``repr()``, logs or exception messages.

    Retries: connection errors, timeouts, HTTP 5xx and HTTP 429 ``RATE_LIMIT_EXCEEDED``
    are retried ``max_retries`` times with exponential backoff and jitter. A monthly
    quota error (429 ``QUOTA_EXCEEDED``) is not retried.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        database_id: Optional[str] = None,
        api_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff: float = 0.2,
        ca_bundle: Optional[str] = None,
        dialects_ttl: float = 600.0,
        pool_manager: Optional[urllib3.PoolManager] = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("VERICTO_API_KEY", "")
        key = key.strip()
        if not key:
            raise VerictoConfigError(
                "No Vericto API key: pass api_key= or set VERICTO_API_KEY "
                "(a vtro_... key with the proxy:evaluate scope)."
            )
        if not key.startswith("vtro_"):
            raise VerictoConfigError("The Vericto API key must start with 'vtro_' (the value itself is not shown).")
        self._api_key = key

        db = database_id if database_id is not None else os.environ.get("VERICTO_DATABASE_ID", "")
        db = db.strip()
        if not db:
            raise VerictoConfigError(
                "No Vericto database id: pass database_id= or set VERICTO_DATABASE_ID "
                "(the id of the database registered in your Vericto workspace)."
            )
        if not _UUID_RE.match(db):
            raise VerictoConfigError(f"The Vericto database id must be a UUID, got {db[:64]!r}.")
        self.database_id = db.lower()

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
        if max_retries < 0:
            raise VerictoConfigError("max_retries cannot be negative.")
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self._backoff = max(0.0, float(backoff))
        self._dialects_ttl = float(dialects_ttl)

        bundle = ca_bundle if ca_bundle is not None else (os.environ.get("VERICTO_CA_BUNDLE") or None)
        if pool_manager is None:
            kwargs: dict[str, Any] = {"num_pools": 2, "maxsize": 10, "cert_reqs": "CERT_REQUIRED"}
            if bundle:
                kwargs["ca_certs"] = bundle
            pool_manager = urllib3.PoolManager(**kwargs)
        self._http = pool_manager
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"vericto-python/{__version__}",
        }

        self._dialects_lock = threading.Lock()
        self._dialects: Optional[tuple[str, ...]] = None
        self._dialects_at = 0.0

    def __repr__(self) -> str:
        return f"RuntimeApiEvaluator(api_url={self.api_url!r}, database_id={self.database_id!r})"

    # -- evaluation ---------------------------------------------------------------

    def evaluate(self, sql: str, dialect: str) -> Verdict:
        payload = json.dumps({"query": sql, "database_id": self.database_id, "dialect": dialect}).encode("utf-8")
        headers = dict(self._headers)
        headers["X-API-Key"] = self._api_key
        url = self.api_url + EVALUATE_PATH

        last_error = "no attempt made"
        last_retry_after: Optional[float] = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(self._delay(attempt, last_retry_after))
            last_retry_after = None
            try:
                resp = self._http.request(
                    "POST",
                    url,
                    body=payload,
                    headers=headers,
                    timeout=urllib3.Timeout(total=self.timeout),
                    retries=False,
                    redirect=False,
                )
            except urllib3.exceptions.HTTPError as exc:
                last_error = f"{type(exc).__name__} while calling {self.api_url}"
                continue

            status = resp.status
            body = _json_body(resp.data)
            if status == 200 or (status == 403 and str(body.get("decision", "")).upper() == "BLOCKED"):
                if "decision" not in body:
                    last_error = f"HTTP {status} without a decision from {self.api_url}"
                    continue
                return Verdict.from_response(body, sql, dialect)

            raw_error = body.get("error")
            error: Mapping[str, Any] = raw_error if isinstance(raw_error, Mapping) else {}
            code = str(error.get("code") or "") or None
            message = str(error.get("message") or "") or None

            if status == 429 and code == "QUOTA_EXCEEDED":
                raise VerictoQuotaExceeded(
                    "The Vericto workspace has used its monthly query quota"
                    + (f"; it resets at {error.get('resets_at')}" if error.get("resets_at") else "")
                    + ".",
                    resets_at=_opt(error.get("resets_at")),
                    upgrade_url=_opt(error.get("upgrade_url")),
                )
            if status == 429 or status in _RETRYABLE_STATUS:
                last_error = f"HTTP {status}" + (f" {code}" if code else "") + f" from {self.api_url}"
                last_retry_after = _retry_after(resp.headers.get("Retry-After"), error.get("retry_after_seconds"))
                continue
            raise self._config_error(status, code, message, dialect)

        raise VerictoUnavailable(
            f"Vericto did not return a verdict after {self.max_retries + 1} attempt(s): {last_error}."
        )

    def _delay(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return min(retry_after, _MAX_BACKOFF)
        base = self._backoff * (2 ** (attempt - 1))
        return float(min(_MAX_BACKOFF, base * (0.5 + random.random() / 2)))

    def _config_error(
        self, status: int, code: Optional[str], message: Optional[str], dialect: str
    ) -> VerictoConfigError:
        if status == 401:
            return VerictoConfigError(
                "Vericto rejected the API key (HTTP 401). Check VERICTO_API_KEY.", status=status, code=code
            )
        if status == 403:
            return VerictoConfigError(
                f"Vericto refused the request (HTTP 403{', ' + code if code else ''}): "
                f"{message or 'the key lacks the proxy:evaluate scope or this database'}.",
                status=status,
                code=code,
            )
        if status == 400:
            supported = self.supported_dialects()
            if supported is not None and dialect not in supported:
                return VerictoUnsupportedDialect(dialect, supported, self.api_url)
        return VerictoConfigError(
            f"Vericto rejected the request (HTTP {status}{', ' + code if code else ''})"
            + (f": {message}" if message else "."),
            status=status,
            code=code,
        )

    # -- capabilities -------------------------------------------------------------

    def supported_dialects(self) -> Optional[Sequence[str]]:
        """The dialects the server evaluates (``GET /api/v1/meta/config``), cached.

        Returns ``None`` when the server cannot be asked; the evaluation request then
        decides.
        """
        with self._dialects_lock:
            now = time.monotonic()
            if self._dialects is not None and now - self._dialects_at < self._dialects_ttl:
                return self._dialects
            try:
                resp = self._http.request(
                    "GET",
                    self.api_url + CONFIG_PATH,
                    headers=self._headers,
                    timeout=urllib3.Timeout(total=self.timeout),
                    retries=False,
                    redirect=False,
                )
            except urllib3.exceptions.HTTPError:
                return self._dialects
            if resp.status != 200:
                return self._dialects
            listed = _json_body(resp.data).get("dialects")
            if not isinstance(listed, list):
                return self._dialects
            ids = tuple(str(d.get("id")) for d in listed if isinstance(d, Mapping) and d.get("id"))
            self._dialects = ids
            self._dialects_at = now
            return ids

    def close(self) -> None:
        self._http.clear()


def _json_body(data: bytes) -> dict[str, Any]:
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
    return None if value is None else str(value)
