# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""The declared context of an evaluation: ``context`` and ``end_user``.

The Runtime API (``POST /api/v1/proxy/query``) accepts two optional fields that
business policies can read:

* ``context``: ``{"ticket": ..., "tenant": ...}``, the change ticket the statement
  belongs to and the tenant it runs for. Each value is 1 to 128 characters, with no
  control characters. No other key is accepted.
* ``end_user``: the end user your service acts for, as up to 16 ``key: value``
  attributes. Keys are identifiers (a letter or ``_``, then letters, digits or
  ``_``; at most 63 characters); values are 1 to 256 characters, with no control
  characters.

They take effect when business policies are enabled in your workspace; otherwise
the server ignores them. The values are asserted by your service: they carry the
trust of the API key that sends them, never more.

The limits are checked here, before anything is sent, with the server's own
rules (lengths are counted as the server counts them, in UTF-16 code units), so an
invalid value raises :class:`~vericto.VerictoConfigError` whatever the fail mode
is, instead of a 400 from the server.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Mapping, Optional, TypedDict, Union

from .errors import VerictoConfigError

CONTEXT_KEYS = ("ticket", "tenant")
MAX_CONTEXT_VALUE = 128
MAX_END_USER_KEYS = 16
MAX_END_USER_VALUE = 256

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class PolicyContext(TypedDict, total=False):
    """``context`` of an evaluation: the change ticket and the tenant."""

    ticket: str
    tenant: str


EndUser = Mapping[str, str]
"""``end_user`` of an evaluation: up to 16 identifier keys with string values."""

ContextSource = Union[PolicyContext, Mapping[str, str], Callable[[], Optional[Mapping[str, str]]], None]
"""What the framework wrappers accept for ``context``: a value, or a callable read per query."""

EndUserSource = Union[Mapping[str, str], Callable[[], Optional[Mapping[str, str]]], None]
"""What the framework wrappers accept for ``end_user``: a value, or a callable read per query."""


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _printable(field: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise VerictoConfigError(f"{field} must be a string, got {type(value).__name__}.")
    if not value:
        raise VerictoConfigError(f"{field} must not be empty.")
    if _utf16_len(value) > maximum:
        raise VerictoConfigError(f"{field}: at most {maximum} characters.")
    if _CONTROL_RE.search(value):
        raise VerictoConfigError(f"{field}: no control characters.")
    return value


def policy_fields(
    context: Optional[Mapping[str, Any]] = None, end_user: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Validate ``context`` and ``end_user`` and return the request-body fields.

    An omitted, ``None`` or empty value adds nothing. Raises
    :class:`~vericto.VerictoConfigError` for a value the server would reject. The
    messages name the field, never its value.
    """
    fields: Dict[str, Any] = {}
    if context is not None:
        if not isinstance(context, Mapping):
            raise VerictoConfigError(f"context must be a mapping (ticket, tenant), got {type(context).__name__}.")
        unknown = [str(k) for k in context if k not in CONTEXT_KEYS]
        if unknown:
            raise VerictoConfigError(f"context: unknown key(s) {', '.join(sorted(unknown))}; only ticket and tenant.")
        declared = {
            key: _printable(f"context.{key}", context[key], MAX_CONTEXT_VALUE)
            for key in CONTEXT_KEYS
            if context.get(key) is not None
        }
        if declared:
            fields["context"] = declared
    if end_user is not None:
        if not isinstance(end_user, Mapping):
            raise VerictoConfigError(f"end_user must be a mapping of attributes, got {type(end_user).__name__}.")
        if len(end_user) > MAX_END_USER_KEYS:
            raise VerictoConfigError(f"end_user: at most {MAX_END_USER_KEYS} keys.")
        attrs: Dict[str, str] = {}
        for key, value in end_user.items():
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                raise VerictoConfigError("end_user: keys are identifiers (letters, digits, _), at most 63 characters.")
            attrs[key] = _printable(f"end_user.{key}", value, MAX_END_USER_VALUE)
        if attrs:
            fields["end_user"] = attrs
    return fields


def resolve(source: Any) -> Optional[Mapping[str, Any]]:
    """The value of a wrapper's ``context`` / ``end_user`` option (calls it when callable)."""
    value = source() if callable(source) else source
    return value  # type: ignore[no-any-return]


__all__ = [
    "CONTEXT_KEYS",
    "MAX_CONTEXT_VALUE",
    "MAX_END_USER_KEYS",
    "MAX_END_USER_VALUE",
    "ContextSource",
    "EndUser",
    "EndUserSource",
    "PolicyContext",
    "policy_fields",
]
