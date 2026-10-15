"""Client-side literal sanitization for CI checks.

When a Vericto workspace is in ``sanitized`` telemetry mode, literal values must
not leave the machine: they are replaced by placeholders before the SQL is sent.
This is a port of vericto-cli's ``sanitize.rs`` (same rules, same output), so a
statement checked by ``vericto-dbt`` and by ``vericto check`` reads the same.

Replaced: single-quoted string literals (``''`` escapes; backslash escapes in
MySQL and in PostgreSQL ``E'...'`` strings) and numeric literals at a token
boundary. Left intact: dollar-quoted blocks, identifiers, ``"quoted"`` and
backtick names, comments. Placeholders are ``?`` for MySQL and ``$1, $2, ...``
elsewhere, so the result still parses.
"""

from __future__ import annotations


def _is_ident(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


def _dollar_tag(sql: str, start: int) -> tuple[int, str] | None:
    j = start + 1
    while j < len(sql):
        ch = sql[j]
        if ch == "$":
            return j + 1, sql[start : j + 1]
        ok = ch == "_" or (ch.isascii() and ch.isalpha()) or (j > start + 1 and ch.isascii() and ch.isdigit())
        if not ok:
            return None
        j += 1
    return None


def sanitize(sql: str, dialect: str) -> str:
    """Return ``sql`` with string and numeric literals replaced by placeholders."""
    question = dialect == "mysql"
    counter = 0
    out: list[str] = []

    def placeholder() -> None:
        nonlocal counter
        if question:
            out.append("?")
        else:
            counter += 1
            out.append(f"${counter}")

    def last() -> str:
        return out[-1][-1] if out and out[-1] else ""

    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(sql[i:end])
            i = end
            continue
        if c == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(sql[i:end])
            i = end
            continue
        if c == "$":
            tag = _dollar_tag(sql, i)
            if tag is not None:
                tag_end, name = tag
                close = sql.find(name, tag_end)
                if close != -1:
                    end = close + len(name)
                    out.append(sql[i:end])
                    i = end
                    continue
            out.append("$")
            i += 1
            continue
        if c in ('"', "`"):
            end = sql.find(c, i + 1)
            end = n if end == -1 else end + 1
            out.append(sql[i:end])
            i = end
            continue
        if c == "'":
            joined = "".join(out)
            e_string = dialect == "postgres" and joined[-1:] in ("E", "e") and not _is_ident(joined[-2:-1] or " ")
            if e_string:
                out[:] = [joined[:-1]]
            backslash = e_string or dialect == "mysql"
            i += 1
            while i < n:
                if backslash and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            placeholder()
            continue
        if c.isascii() and c.isdigit() and not _is_ident(last()):
            i += 1
            while i < n and ((sql[i].isascii() and sql[i].isdigit()) or sql[i] == "."):
                i += 1
            placeholder()
            continue
        out.append(c)
        i += 1
    return "".join(out)


__all__ = ["sanitize"]
