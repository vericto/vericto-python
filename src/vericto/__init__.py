# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Vericto: check every SQL statement with Vericto before it runs.

The core is driver-agnostic:

* :class:`Vericto` (the guard) applies a verdict: raise on block, run the masked
  rewrite, warn on flag, and the fail mode when Vericto cannot answer.
* :class:`Evaluator` is how a verdict is obtained; :class:`RuntimeApiEvaluator`
  asks the Vericto Runtime API.

Driver wrappers live in submodules so that importing ``vericto`` never imports a
database driver: :mod:`vericto.snowflake`, :mod:`vericto.sqlite`,
:mod:`vericto.sqlalchemy` and the generic :mod:`vericto.dbapi`.
"""

from ._version import __version__
from .dbapi import wrap
from .dialects import KNOWN_DIALECTS, normalize_dialect
from .errors import (
    VerictoBlocked,
    VerictoConfigError,
    VerictoError,
    VerictoFailOpenWarning,
    VerictoFlaggedWarning,
    VerictoQuotaExceeded,
    VerictoUnavailable,
    VerictoUnsupportedDialect,
)
from .evaluator import Evaluator
from .guard import Vericto
from .placeholders import PlaceholderMappingError
from .policy_context import EndUser, PolicyContext
from .runtime_api import DEFAULT_API_URL, RuntimeApiEvaluator
from .verdict import Decision, Finding, Verdict

__all__ = [
    "DEFAULT_API_URL",
    "KNOWN_DIALECTS",
    "Decision",
    "EndUser",
    "Evaluator",
    "Finding",
    "PlaceholderMappingError",
    "PolicyContext",
    "RuntimeApiEvaluator",
    "Verdict",
    "Vericto",
    "VerictoBlocked",
    "VerictoConfigError",
    "VerictoError",
    "VerictoFailOpenWarning",
    "VerictoFlaggedWarning",
    "VerictoQuotaExceeded",
    "VerictoUnavailable",
    "VerictoUnsupportedDialect",
    "__version__",
    "normalize_dialect",
    "wrap",
]
