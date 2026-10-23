# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Run the Python code blocks of the README and the framework guides.

Every fenced ``python`` block of the given Markdown files runs in its own
interpreter, in an empty temporary directory, against a local stand-in for the
Runtime API (``scripts/docs_stub.py``): ``VERICTO_API_URL`` points at it and
``VERICTO_API_KEY`` / ``VERICTO_DATABASE_ID`` are test values. A block fails the
check when it exits non-zero or takes longer than ``--timeout`` seconds.

An HTML comment on the line before a fence changes what happens to the block:

* ``<!-- docs-check: skip REASON -->``: not run (it needs a real database or
  account, or it is a fragment). The reason is printed.
* ``<!-- docs-check: requires=module[,module] -->``: run only when the modules
  import; otherwise skipped, or a failure with ``--strict`` (the CI jobs that
  install the framework pass ``--strict``).
* ``<!-- docs-check: continue -->``: appended to the previous block and run
  together (a guide that builds an example over several blocks).

Usage: ``python scripts/check_docs.py [--strict] [--timeout S] FILE.md...``
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docs_stub import StubServer

API_KEY = "vtro_docs_check_not_a_real_key"
DATABASE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
_DIRECTIVE = re.compile(r"<!--\s*docs-check:\s*(.*?)\s*-->")


@dataclass
class Block:
    path: Path
    line: int
    code: str
    skip: Optional[str] = None
    requires: List[str] = field(default_factory=list)


def blocks_of(path: Path) -> List[Block]:
    """The python blocks of ``path``, with their directives applied."""
    out: List[Block] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        fence = lines[i].strip()
        if fence.startswith("```") and fence[3:].strip() in ("python", "py"):
            j = i + 1
            while j < len(lines) and lines[j].strip() != "```":
                j += 1
            code = "\n".join(lines[i + 1 : j]) + "\n"
            prev = lines[i - 1].strip() if i > 0 else ""
            directive = _DIRECTIVE.match(prev)
            text = directive.group(1) if directive else ""
            if text == "continue" and out:
                out[-1].code += "\n" + code
            else:
                block = Block(path, i + 1, code)
                if text.startswith("skip"):
                    block.skip = text[4:].strip() or "marked skip"
                elif text.startswith("requires="):
                    block.requires = [m.strip() for m in text[len("requires=") :].split(",") if m.strip()]
                out.append(block)
            i = j
        i += 1
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--strict", action="store_true", help="a block whose requirements are missing fails")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("-v", "--verbose", action="store_true", help="print the output of every block")
    args = parser.parse_args(argv)

    stub = StubServer().start()
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("VERICTO_"):
            del env[name]
    env.update(
        VERICTO_API_URL=stub.url,
        VERICTO_API_KEY=API_KEY,
        VERICTO_DATABASE_ID=DATABASE_ID,
        PYTHONWARNINGS="default",
        # No example may reach a third-party service: framework telemetry and tracing off.
        OTEL_SDK_DISABLED="true",
        CREWAI_DISABLE_TELEMETRY="true",
        CREWAI_TRACING_ENABLED="false",
        OPENAI_AGENTS_DISABLE_TRACING="1",
        ANONYMIZED_TELEMETRY="false",
        DO_NOT_TRACK="1",
        PYDANTIC_AI_NO_BANNER="1",
    )
    ran = skipped = failed = 0
    try:
        for path in args.files:
            for block in blocks_of(path):
                where = f"{path}:{block.line}"
                if block.skip:
                    print(f"SKIP {where}: {block.skip}")
                    skipped += 1
                    continue
                missing = [m for m in block.requires if importlib.util.find_spec(m) is None]
                if missing:
                    if args.strict:
                        print(f"FAIL {where}: missing {', '.join(missing)}")
                        failed += 1
                    else:
                        print(f"SKIP {where}: needs {', '.join(missing)}")
                        skipped += 1
                    continue
                with tempfile.TemporaryDirectory(prefix="vericto-docs-") as tmp:
                    try:
                        proc = subprocess.run(  # noqa: S603 - our own interpreter on our own docs
                            [sys.executable, "-c", block.code],
                            cwd=tmp,
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=args.timeout,
                            check=False,
                        )
                    except subprocess.TimeoutExpired:
                        print(f"FAIL {where}: timed out after {args.timeout:g}s")
                        failed += 1
                        continue
                if proc.returncode == 0:
                    print(f"OK   {where}")
                    if args.verbose:
                        print(proc.stdout + proc.stderr)
                    ran += 1
                else:
                    print(f"FAIL {where} (exit {proc.returncode})\n{proc.stdout}{proc.stderr}")
                    failed += 1
    finally:
        stub.shutdown()
        stub.server_close()
    print(f"{ran} ran, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
