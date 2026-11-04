# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Offline mode: check SQL with the local engine of the ``vericto`` CLI, no account.

``Vericto(offline=True)`` (or ``VERICTO_OFFLINE=1``) evaluates statements with
``vericto serve --stdio`` (Vericto CLI 2.1.0 or later), a separate process that
speaks JSON Lines over stdin/stdout (``docs/serve-protocol.md`` in vericto-cli).
Nothing is downloaded and no network connection is opened: the CLI must already be
installed (``npm i -g @vericto/vericto-cli``, ``brew install vericto/tap/vericto``,
Scoop, or a GitHub release binary).

The engine is AGPL-3.0-only and runs in that separate CLI process. This package
never links, imports or bundles it, so it stays Apache-2.0.

Offline verdicts use the engine's standard rules at their default severities, under
one of two profiles:

* ``"runtime"`` (default): what the Runtime API answers for a workspace with no
  overrides. ``DROP TABLE`` is ``BLOCKED``.
* ``"ci"``: exactly ``vericto check --offline``. Schema DDL (VERICTO-010 to 019) is
  capped at ``FLAGGED``.

What needs a workspace is not available offline: custom rules, severity overrides,
sensitive-column masking, agent allowlists, business policies and the audit trail.

The process is started on demand, one per evaluator, and shared by every
thread: a reader thread matches the answers to the requests by id. If it dies, a
check restarts it once and resends the request; if that fails too, the check raises
:class:`~vericto.VerictoUnavailable` and the guard's fail mode applies, as when the
Runtime API cannot be reached. It is stopped by :meth:`OfflineEvaluator.close`
(``Vericto.close()``, ``with Vericto(...)``), when the evaluator is garbage
collected, and at interpreter exit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import weakref
from collections import OrderedDict
from typing import IO, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .ci_api import CiConfig, CiFile, CiRequestError
from .dialects import KNOWN_DIALECTS
from .errors import VerictoConfigError, VerictoUnavailable, VerictoUnsupportedDialect
from .verdict import Verdict

logger = logging.getLogger("vericto")

PROTOCOL = 1
MIN_CLI_VERSION = (2, 1, 0)
PROFILES = ("runtime", "ci")
DEFAULT_PROFILE = "runtime"
#: Seconds to wait for one answer, and for the hello line of a new process.
DEFAULT_TIMEOUT = 30.0
STARTUP_TIMEOUT = 10.0
_STOP_TIMEOUT = 2.0

INSTALL_HINT = (
    "Install Vericto CLI 2.1.0 or later: npm i -g @vericto/vericto-cli, "
    "brew install vericto/tap/vericto, scoop bucket add vericto "
    "https://github.com/vericto/scoop-bucket && scoop install vericto/vericto, or a binary from "
    "https://github.com/vericto/vericto-cli/releases. Then put it on PATH, or pass "
    "offline_cli_path= / set VERICTO_CLI."
)
_WHERE = "The local Vericto engine (vericto serve)"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("", "0", "false", "no", "off")


def offline_requested(offline: Optional[bool] = None) -> bool:
    """``offline`` when given, else ``VERICTO_OFFLINE`` (``1``/``true``/``yes``/``on``).

    An unrecognized value of the variable raises :class:`~vericto.VerictoConfigError`.
    """
    if offline is not None:
        return bool(offline)
    value = os.environ.get("VERICTO_OFFLINE", "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise VerictoConfigError(f"VERICTO_OFFLINE must be 1/true/yes/on or 0/false/no/off, got {value[:32]!r}.")


def find_cli(path: Optional[str] = None) -> str:
    """The ``vericto`` binary to run: ``path``, else ``VERICTO_CLI``, else ``vericto`` on PATH.

    Raises :class:`~vericto.VerictoConfigError` with the install commands when there is
    none. Nothing is ever downloaded.
    """
    explicit = path if path is not None else os.environ.get("VERICTO_CLI", "").strip() or None
    if explicit is not None:
        source = "offline_cli_path" if path is not None else "VERICTO_CLI"
        resolved = shutil.which(explicit) or (explicit if os.path.isfile(explicit) else None)
        if resolved is None or not os.access(resolved, os.X_OK):
            raise VerictoConfigError(
                f"Offline mode: {source} is {explicit!r}, which is not an executable file. {INSTALL_HINT}",
                code="CLI_NOT_FOUND",
            )
        return resolved
    found = shutil.which("vericto")
    if found is None:
        raise VerictoConfigError(
            f"Offline mode needs the vericto CLI, and none was found on PATH. {INSTALL_HINT}", code="CLI_NOT_FOUND"
        )
    return found


def parse_version(text: str) -> Optional[Tuple[int, int, int]]:
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


class _ServeFailure(Exception):
    """The process died, timed out or broke the protocol: no answer for this request."""

    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


def _encode(message: Mapping[str, Any]) -> bytes:
    """One request line. UTF-8 as is; a str that cannot be UTF-8 (lone surrogates) is escaped."""
    try:
        return json.dumps(message, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(message).encode("ascii")


class _ServeError(Exception):
    """A protocol-level error answer: ``{"ok": false, "error": {...}}``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class _Slot:
    __slots__ = ("event", "failure", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[Dict[str, Any]] = None
        self.failure: Optional[str] = None


def _stop_process(proc: subprocess.Popen[bytes], owner_pid: int) -> None:
    """Stop ``proc`` and reap it. Runs once: from close(), the finalizer or atexit."""
    if os.getpid() != owner_pid:
        # A forked child inherited the handle: the process belongs to the parent.
        return
    try:
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()  # EOF: the server answers what it read and exits 0
            except OSError:
                pass
            try:
                proc.wait(_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(_STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class _ServeProcess:
    """One running ``vericto serve --stdio``: request/response matched by id."""

    def __init__(self, cli: str) -> None:
        self.cli = cli
        self.pid_owner = os.getpid()
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - the user's own CLI, no shell
                [cli, "serve", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except OSError as exc:
            raise _ServeFailure(f"could not start {cli}: {type(exc).__name__}: {exc.strerror or exc}") from None
        self._stop = weakref.finalize(self, _stop_process, self.proc, self.pid_owner)
        self._lock = threading.RLock()  # pending + writes, so the write order is the pending order
        self._pending: OrderedDict[str, _Slot] = OrderedDict()
        self._next_id = 0
        self._dead: Optional[str] = None
        self._hello = _Slot()
        self._seen_hello = False
        stdout = self.proc.stdout
        stderr = self.proc.stderr
        assert stdout is not None and stderr is not None  # noqa: S101 - PIPE given above
        threading.Thread(target=self._read_stdout, args=(stdout,), name="vericto-serve-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(stderr,), name="vericto-serve-stderr", daemon=True).start()

    # -- lifecycle ------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._dead is None and self.proc.poll() is None and os.getpid() == self.pid_owner

    def hello(self, timeout: float) -> Optional[Dict[str, Any]]:
        """The hello line, or ``None`` if the process exited (or stayed silent) before sending it."""
        if not self._hello.event.wait(timeout):
            self.close()
            return None
        return self._hello.response

    def close(self) -> None:
        self._fail_all("the process was stopped")
        self._stop()

    def exit_detail(self) -> str:
        code = self.proc.poll()
        return f"exit code {code}" if code is not None else "still running"

    # -- requests -------------------------------------------------------------------

    def request(self, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        slot = _Slot()
        with self._lock:
            if self._dead is not None:
                raise _ServeFailure(self._dead)
            self._next_id += 1
            request_id = str(self._next_id)
            line = _encode({"id": request_id, **payload}) + b"\n"
            self._pending[request_id] = slot
            stdin = self.proc.stdin
            try:
                assert stdin is not None  # noqa: S101 - PIPE given in __init__
                stdin.write(line)
                stdin.flush()
            except (OSError, ValueError):
                self._pending.pop(request_id, None)
                self._mark_dead(f"the process stopped reading its input ({self.exit_detail()})")
                raise _ServeFailure(self._dead or "the process stopped") from None
        if not slot.event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            # A stuck process would hold every later request: replace it.
            self._mark_dead(f"no answer within {timeout:g}s")
            self.close()
            raise _ServeFailure(f"no answer within {timeout:g}s", timed_out=True)
        if slot.failure is not None:
            raise _ServeFailure(slot.failure)
        assert slot.response is not None  # noqa: S101 - set together with the event
        return slot.response

    # -- reader threads -------------------------------------------------------------

    def _read_stdout(self, stdout: IO[bytes]) -> None:
        try:
            for raw in stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    message = None
                if not isinstance(message, dict):
                    self._protocol_error("an answer that is not a JSON object")
                    return
                if not self._seen_hello:
                    self._seen_hello = True
                    self._hello.response = message
                    self._hello.event.set()
                    continue
                if not self._dispatch(message):
                    return
        except (OSError, ValueError):
            pass
        self._mark_dead(f"the process exited ({self._wait_detail()})")
        self._hello.event.set()

    def _dispatch(self, message: Dict[str, Any]) -> bool:
        if not isinstance(message.get("ok"), bool):
            self._protocol_error("an answer without 'ok'")
            return False
        request_id = message.get("id")
        with self._lock:
            if isinstance(request_id, str) and request_id in self._pending:
                slot = self._pending.pop(request_id)
            elif request_id is None and self._pending:
                # Errors for a line without a usable id (TOO_LARGE) come back with
                # "id": null; protocol 1 answers in order, so it is the oldest request.
                _, slot = self._pending.popitem(last=False)
            else:
                slot = None
        if slot is None:
            self._protocol_error("an answer for an unknown request id")
            return False
        slot.response = message
        slot.event.set()
        return True

    def _read_stderr(self, stderr: IO[bytes]) -> None:
        # The CLI's diagnostics; the protocol guarantees they never contain SQL.
        try:
            for raw in stderr:
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    logger.debug("vericto serve: %s", text)
        except (OSError, ValueError):
            pass

    def _protocol_error(self, what: str) -> None:
        self._mark_dead(f"the process sent {what}")
        self._hello.event.set()
        self.close()

    def _wait_detail(self) -> str:
        try:
            code = self.proc.wait(_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "still running"
        return f"exit code {code}"

    def _mark_dead(self, reason: str) -> None:
        with self._lock:
            if self._dead is None:
                self._dead = reason
        self._fail_all(reason)

    def _fail_all(self, reason: str) -> None:
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot.failure = reason
            slot.event.set()


class OfflineEngine:
    """The ``vericto serve --stdio`` process behind offline mode, started on demand.

    Thread-safe; one process per instance. :class:`OfflineEvaluator` (the guard) and
    :class:`OfflineCiClient` (``vericto-dbt --offline``) are built on it.

    Args:
        profile: ``"runtime"`` (default) or ``"ci"`` (see the module docs).
        cli_path: the ``vericto`` binary; default ``VERICTO_CLI``, then PATH.
        timeout: seconds to wait for one answer (default 30).
    """

    def __init__(
        self, *, profile: str = DEFAULT_PROFILE, cli_path: Optional[str] = None, timeout: Optional[float] = None
    ) -> None:
        if profile not in PROFILES:
            raise VerictoConfigError(f"offline_profile must be 'runtime' or 'ci', got {profile!r}.")
        if timeout is not None and timeout <= 0:
            raise VerictoConfigError("timeout must be greater than 0 seconds.")
        self.profile = profile
        self.cli_path = find_cli(cli_path)
        self.timeout = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
        self.cli_version: Optional[str] = None
        self.engine_version: Optional[str] = None
        self.max_line_bytes: Optional[int] = None
        self._lock = threading.Lock()
        # The running process, in a holder the finalizer can reach without keeping
        # this engine alive: a discarded engine stops its process when collected.
        self._held: List[Optional[_ServeProcess]] = [None]
        self._finalizer = weakref.finalize(self, _close_held, self._held)
        self._validated = False

    def __repr__(self) -> str:
        return f"OfflineEngine(profile={self.profile!r}, cli_path={self.cli_path!r})"

    @property
    def ruleset_version(self) -> Optional[str]:
        return f"standard@engine-{self.engine_version}" if self.engine_version else None

    def check(self, sql: str, dialect: str) -> Dict[str, Any]:
        """The ``result`` document for ``sql`` (``check --offline --format json``).

        Raises :class:`_ServeError` for an error answer, :class:`VerictoConfigError`
        when the CLI is unusable, and :class:`VerictoUnavailable` when the process
        failed twice (or timed out).
        """
        payload = {"op": "check", "sql": sql, "dialect": dialect, "profile": self.profile}
        last = "no attempt made"
        for attempt in range(2):
            proc = self._process(restart=attempt > 0)
            limit = self.max_line_bytes
            if limit is not None and len(_encode(payload)) + 16 > limit:
                raise _ServeError("TOO_LARGE", f"the request is larger than the engine's {limit}-byte line limit")
            try:
                answer = proc.request(payload, self.timeout)
            except _ServeFailure as exc:
                last = str(exc)
                self._discard(proc)
                if exc.timed_out:
                    break  # a statement that hangs the engine would hang a new one too
                continue
            if answer.get("ok") is True:
                result = answer.get("result")
                if not _valid_result(result):
                    self._discard(proc)
                    last = "the process sent a result without exactly one query"
                    continue
                assert isinstance(result, dict)  # noqa: S101 - checked by _valid_result
                return result
            error = answer.get("error")
            fields: Mapping[str, Any] = error if isinstance(error, Mapping) else {}
            raise _ServeError(str(fields.get("code") or "INTERNAL"), str(fields.get("message") or ""))
        raise VerictoUnavailable(f"{_WHERE} gave no verdict: {last}.", code="OFFLINE_ENGINE_FAILED")

    def close(self) -> None:
        """Stop the process. The next check starts a new one."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            proc.close()

    @property
    def _proc(self) -> Optional[_ServeProcess]:
        return self._held[0]

    @_proc.setter
    def _proc(self, proc: Optional[_ServeProcess]) -> None:
        self._held[0] = proc

    # -- internals ------------------------------------------------------------------

    def _discard(self, proc: _ServeProcess) -> None:
        with self._lock:
            if self._proc is proc:
                self._proc = None
        proc.close()

    def _process(self, *, restart: bool) -> _ServeProcess:
        with self._lock:
            if self._proc is not None and self._proc.alive:
                return self._proc
            if self._proc is not None:
                stale, self._proc = self._proc, None
                if os.getpid() == stale.pid_owner:
                    stale.close()
            if restart:
                logger.warning("vericto: restarting the local engine process (vericto serve)")
            try:
                proc = _ServeProcess(self.cli_path)
            except _ServeFailure as exc:
                if self._validated:
                    raise VerictoUnavailable(f"{_WHERE} could not start: {exc}.") from None
                raise VerictoConfigError(f"Offline mode: {exc}. {INSTALL_HINT}", code="CLI_NOT_FOUND") from None
            hello = proc.hello(STARTUP_TIMEOUT)
            try:
                self._accept(hello, proc)
            except (VerictoUnavailable, VerictoConfigError):
                proc.close()
                raise
            self._proc = proc
            self._validated = True
            logger.debug(
                "vericto: offline engine started (vericto %s, engine %s, profile %s)",
                self.cli_version,
                self.engine_version,
                self.profile,
            )
            return proc

    def _accept(self, hello: Optional[Dict[str, Any]], proc: _ServeProcess) -> None:
        if hello is None:
            if self._validated:
                raise VerictoUnavailable(f"{_WHERE} exited before it was ready ({proc.exit_detail()}).")
            found = _reported_version(self.cli_path)
            raise VerictoConfigError(
                f"Offline mode: {self.cli_path} ({found}) did not start 'vericto serve --stdio' "
                f"(added in Vericto CLI 2.1.0). {INSTALL_HINT}",
                code="CLI_TOO_OLD",
            )
        if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL:
            raise VerictoConfigError(
                f"Offline mode: {self.cli_path} speaks serve protocol {hello.get('protocol')!r}; this "
                f"version of the vericto package speaks protocol {PROTOCOL}. Install a matching "
                "vericto package or CLI.",
                code="PROTOCOL_MISMATCH",
            )
        cli_version = str(hello.get("cli_version") or "")
        parsed = parse_version(cli_version)
        if parsed is None or parsed < MIN_CLI_VERSION:
            raise VerictoConfigError(
                f"Offline mode needs Vericto CLI 2.1.0 or later; {self.cli_path} is {cli_version or 'unknown'}. "
                f"{INSTALL_HINT}",
                code="CLI_TOO_OLD",
            )
        profiles = hello.get("profiles")
        if isinstance(profiles, list) and self.profile not in profiles:
            raise VerictoConfigError(
                f"Offline mode: vericto {cli_version} does not offer the {self.profile!r} profile "
                f"(it offers {', '.join(map(str, profiles))}).",
                code="UNSUPPORTED_PROFILE",
            )
        max_line = hello.get("max_line_bytes")
        self.cli_version = cli_version
        self.engine_version = str(hello.get("engine_version") or "") or None
        self.max_line_bytes = max_line if isinstance(max_line, int) and max_line > 0 else None


def _close_held(held: List[Optional[_ServeProcess]]) -> None:
    proc, held[0] = held[0], None
    if proc is not None:
        proc.close()


def _valid_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    queries = result.get("queries")
    return isinstance(queries, list) and len(queries) == 1 and isinstance(queries[0], dict)


def _reported_version(cli: str) -> str:
    try:
        out = subprocess.run(  # noqa: S603 - the user's own CLI, no shell
            [cli, "--version"], capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "version unknown"
    text = out.stdout.decode("utf-8", "replace").strip()
    return text[:80] or "version unknown"


def _config_error(exc: _ServeError, dialect: str) -> VerictoConfigError:
    if exc.code == "UNSUPPORTED_DIALECT":
        return VerictoUnsupportedDialect(dialect, KNOWN_DIALECTS, "", where=_WHERE)
    detail = exc.message or "no detail"
    return VerictoConfigError(f"{_WHERE} refused the request ({exc.code}): {detail}.", code=exc.code)


_REQUEST_ERRORS = frozenset({"UNSUPPORTED_DIALECT", "INVALID_REQUEST", "TOO_LARGE"})


class OfflineEvaluator:
    """Evaluates statements with the local engine of the ``vericto`` CLI: no account,
    no network. What ``Vericto(offline=True)`` uses; see the module docs.

    Args:
        profile: ``"runtime"`` (default): a ``DROP TABLE`` is ``BLOCKED``, as on the
            Runtime API. ``"ci"``: exactly ``vericto check --offline``.
        cli_path: the ``vericto`` binary (default ``VERICTO_CLI``, then PATH).
        timeout: seconds to wait for one verdict (default 30).

    Verdicts carry ``mode="offline"``, ``engine_version`` and ``ruleset_version``
    ``standard@engine-<version>``. One verdict per call: a text with several
    statements gets the strongest finding among them, as ``vericto check
    --offline`` reports a file. ``context`` and ``end_user`` are accepted and have
    no effect (business policies live in a workspace).

    Errors: ``UNSUPPORTED_DIALECT`` raises :class:`~vericto.VerictoUnsupportedDialect`;
    ``INVALID_REQUEST`` (empty SQL) and ``TOO_LARGE`` raise
    :class:`~vericto.VerictoConfigError` (``.code`` is the engine's code), in both
    fail modes. ``INTERNAL`` and a process that fails twice raise
    :class:`~vericto.VerictoUnavailable`, which the guard's fail mode handles.
    """

    mode = "offline"

    def __init__(
        self, *, profile: str = DEFAULT_PROFILE, cli_path: Optional[str] = None, timeout: Optional[float] = None
    ) -> None:
        self.engine = OfflineEngine(profile=profile, cli_path=cli_path, timeout=timeout)
        self._noted_policy_fields = False

    def __repr__(self) -> str:
        return f"OfflineEvaluator(profile={self.profile!r}, cli_path={self.engine.cli_path!r})"

    @property
    def profile(self) -> str:
        return self.engine.profile

    def evaluate(
        self,
        sql: str,
        dialect: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
        end_user: Optional[Mapping[str, Any]] = None,
    ) -> Verdict:
        if (context or end_user) and not self._noted_policy_fields:
            logger.info("vericto: offline mode has no business policies; context and end_user have no effect.")
            self._noted_policy_fields = True
        try:
            result = self.engine.check(sql, dialect)
        except _ServeError as exc:
            if exc.code in _REQUEST_ERRORS:
                raise _config_error(exc, dialect) from None
            raise VerictoUnavailable(
                f"{_WHERE} failed on this statement ({exc.code}): {exc.message or 'no detail'}.", code=exc.code
            ) from None
        return verdict_from_result(result, sql, dialect, self.profile)

    def supported_dialects(self) -> Optional[Sequence[str]]:
        return KNOWN_DIALECTS

    def close(self) -> None:
        """Stop the ``vericto serve`` process; the next check starts a new one."""
        self.engine.close()


def verdict_from_result(result: Mapping[str, Any], sql: str, dialect: str, profile: str) -> Verdict:
    """Map a ``check --offline --format json`` document (one query) to a :class:`Verdict`,
    through the same :meth:`Verdict.from_response` as an online answer."""
    query: Mapping[str, Any] = result["queries"][0]
    summary = result.get("summary")
    summary_fields: Mapping[str, Any] = summary if isinstance(summary, Mapping) else {}
    engine_version = result.get("engine_version")
    ruleset = summary_fields.get("ruleset_version") or (f"standard@engine-{engine_version}" if engine_version else None)
    body = {
        "decision": query.get("status"),
        "action": query.get("action"),
        "rule_code": query.get("rule_code"),
        "severity": query.get("severity"),
        "ast_node_path": query.get("ast_node_path"),
        "suggested_safe_query": query.get("suggested_fix"),
        "ruleset_version": ruleset,
        "mode": "offline",
        "engine_version": engine_version,
        "profile": profile,
        # The whole document, as `vericto check --offline --format json` prints it.
        "offline_result": dict(result),
    }
    return Verdict.from_response(body, sql, dialect)


class OfflineCiClient:
    """A stand-in for :class:`~vericto.ci_api.CiCheckClient` that checks with the local
    engine, for ``vericto-dbt check --offline``. Uses the ``ci`` profile by default,
    so verdicts equal ``vericto check --offline``."""

    def __init__(self, *, profile: str = "ci", cli_path: Optional[str] = None, timeout: Optional[float] = None) -> None:
        self.engine = OfflineEngine(profile=profile, cli_path=cli_path, timeout=timeout)

    def __repr__(self) -> str:
        return f"OfflineCiClient(profile={self.engine.profile!r}, cli_path={self.engine.cli_path!r})"

    def config(self) -> CiConfig:
        # Raw SQL to a local process, one entry per statement; no allowance to report.
        return CiConfig(telemetry_query_mode="raw", capabilities=(), raw={"mode": "offline"})

    def check_files(
        self,
        files: Sequence[CiFile],
        dialect: str,
        *,
        database_id: Optional[str] = None,
        file_name: Optional[str] = None,
        provenance: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        body = self.check_queries([f.sql for f in files], dialect)
        for entry in body["queries"]:
            entry["file_index"], entry["line"] = entry["line"], 1
        return body

    def check_queries(
        self,
        queries: Sequence[str],
        dialect: str,
        *,
        database_id: Optional[str] = None,
        file_name: Optional[str] = None,
        provenance: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        """One engine request per entry; ``line`` is the entry's 1-based position."""
        out: List[Dict[str, Any]] = []
        ruleset: Optional[str] = None
        for index, sql in enumerate(queries, start=1):
            try:
                result = self.engine.check(sql, dialect)
            except _ServeError as exc:
                if exc.code in _REQUEST_ERRORS:
                    raise CiRequestError(
                        f"{_WHERE} refused entry {index} ({exc.code}): {exc.message or 'no detail'}.", code=exc.code
                    ) from None
                raise VerictoUnavailable(
                    f"{_WHERE} failed on entry {index} ({exc.code}): {exc.message or 'no detail'}.", code=exc.code
                ) from None
            entry = dict(result["queries"][0])
            entry["line"] = index
            out.append(entry)
            summary = result.get("summary")
            if isinstance(summary, Mapping) and summary.get("ruleset_version"):
                ruleset = str(summary["ruleset_version"])
        return {"queries": out, "summary": {"ruleset_version": ruleset or self.engine.ruleset_version}}

    def close(self) -> None:
        self.engine.close()


__all__ = [
    "DEFAULT_PROFILE",
    "INSTALL_HINT",
    "MIN_CLI_VERSION",
    "PROFILES",
    "OfflineCiClient",
    "OfflineEngine",
    "OfflineEvaluator",
    "find_cli",
    "offline_requested",
    "verdict_from_result",
]
