# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""Client-side literal sanitization for CI checks.

When a Vericto workspace is in ``sanitized`` telemetry mode, literal values must
not leave the machine: they are replaced by placeholders before the SQL is sent.
This is a port of vericto-cli's ``src/sanitize.rs`` as of vericto-cli 1.8.2 (same
rules, same output), so a statement checked by ``vericto-dbt`` and by
``vericto check`` reads the same. ``tests/fixtures/sanitize/vericto-cli-1.8.2.json``
is vericto-cli's own fixture of test vectors, run by both test suites.

The pass is lexical: it finds where each literal starts and ends as the dialect's
own lexer would, replaces it, and keeps everything else as written.

Replaced by a placeholder: string literals (``'...'`` with ``''`` escapes; the
prefixes ``E'...'``, ``N'...'``, ``U&'...'``, ``B'...'``, ``X'...'``, MySQL
``_utf8mb4'...'``, Oracle ``q'[...]'`` / ``nq'...'`` with any delimiter; MySQL
``"..."``), numbers in every form (``42``, ``.5``, ``1e10``, ``0x1F``, ``1_000``,
with a unary sign: ``= -3`` becomes ``= $1``), MSSQL money (``$12.50``) and
dollar-quoted strings. After ``AS`` or ``DO`` a dollar-quoted string is a function
or ``DO`` body and becomes the empty body ``$$$$``, so the statement still parses.

Kept as written: keywords, identifiers (also non-ASCII ones), quoted identifiers
(``"..."``, backticks, MSSQL and SQLite ``[...]``), comments, and the bind
parameters already in the statement (``$1``, ``?``, ``?1``, ``:1``, ``:name``,
``@p1``, ``$name``, Snowflake's ``$1`` stage columns).

Escapes and comments follow each dialect: Snowflake always reads ``\\`` as an
escape inside ``'...'``, PostgreSQL only in ``E'...'``, Oracle, MSSQL and SQLite
never. MySQL does unless the server runs with ``NO_BACKSLASH_ESCAPES``, which the
client cannot see, so a MySQL statement (and one in an unknown dialect) with a
backslash is read both ways: a reading in which something is left unterminated is
dropped; otherwise, if the two disagree, everything that is a literal in either is
replaced. ``#`` and ``-- `` (with the space) are MySQL comments, ``//`` a Snowflake
one; ``/* */`` nests in PostgreSQL and MSSQL; MySQL ``/*! ... */`` comments, which
the server executes, are sanitized inside.

Fail closed: when the text cannot be tokenized with certainty (an unterminated
literal, quoted identifier, comment, dollar quote or q-quote, a q-quote without a
usable delimiter, a nested comment in MySQL, or a MySQL comment form whose reading
depends on the server), everything from there to the end becomes one placeholder.

Placeholders are ``?`` for MySQL, Snowflake and SQLite, and ``$1, $2, ...`` for
PostgreSQL, Oracle, MSSQL and any other dialect, numbered after the highest
``$N`` (and Oracle ``:N``) already in the statement so they never collide.

Known limits, both settings the client cannot see: SQLite reads a ``"..."`` that
names no column as a string, and MSSQL does so with ``QUOTED_IDENTIFIER OFF``;
here ``"..."`` is an identifier, as the engine reads it. PostgreSQL is read with
``standard_conforming_strings`` on.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

_SPACE = " \t\n\r\x0b\x0c"

_POSTGRES = "postgres"
_MYSQL = "mysql"
_ORACLE = "oracle"
_MSSQL = "mssql"
_SNOWFLAKE = "snowflake"
_SQLITE = "sqlite"
#: Any other dialect: PostgreSQL lexing, both backslash readings.
_OTHER = "other"
_KNOWN = frozenset({_POSTGRES, _MYSQL, _ORACLE, _MSSQL, _SNOWFLAKE, _SQLITE})

#: How a reading ended (vericto-cli's ``Reading``).
_COMPLETE = "complete"  # every token read with certainty
_INVALID = "invalid"  # something unterminated: the text does not lex this way
_AMBIGUOUS = "ambiguous"  # the server may read the text either way

_OP = "op"
_OPERAND = "operand"

_PLAIN = "plain"
_ESCAPE = "escape"
_QQUOTE = "qquote"

#: A replaced range: (start, end, body). ``body``: rendered ``$$$$``, not a placeholder.
_Span = Tuple[int, int, bool]


def _ascii_lower(s: str) -> str:
    """``str.to_ascii_lowercase`` for the names compared here (all ASCII)."""
    return s.lower() if s.isascii() else s


def _is_alnum(c: str) -> bool:
    return ("0" <= c <= "9") or ("a" <= c <= "z") or ("A" <= c <= "Z")


def _is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def _word_char(c: str) -> bool:
    """Continues an unquoted word in any dialect: every non-ASCII character does."""
    return _is_alnum(c) or c in "_$" or ord(c) >= 0x80


def _word_start(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z") or c == "_" or ord(c) >= 0x80


class _Fail(Exception):
    """Fail closed from ``at``."""

    def __init__(self, at: int) -> None:
        super().__init__(at)
        self.at = at


class _Lexer:
    def __init__(self, sql: str, dialect: str, backslash: bool) -> None:
        self.s = sql
        self.n = len(sql)
        self.d = dialect
        self.pg_like = dialect in (_POSTGRES, _OTHER)
        # The reading: whether a backslash escapes where that depends on the server.
        self.backslash = backslash
        self.i = 0
        # The previous significant token: _OP, _OPERAND, or a word as (start, end).
        self.prev: object = _OP
        # The start of the MySQL executable comment (/*! ... */) being read.
        self.exec: Optional[int] = None
        self.ambiguous = False
        self.spans: List[_Span] = []
        self.max_bind: Optional[int] = None

    def at(self, k: int) -> str:
        return self.s[k] if k < self.n else ""

    def digit_at(self, k: int) -> bool:
        return k < self.n and _is_digit(self.s[k])

    def unsure(self, at: int) -> _Fail:
        self.ambiguous = True
        return _Fail(at)

    def run(self) -> Tuple[List[_Span], Optional[int], str]:
        try:
            while self.i < self.n:
                self.step()
            if self.exec is not None:
                raise _Fail(self.exec)
        except _Fail as f:
            return self.fail(f.at)
        return self.spans, self.max_bind, _COMPLETE

    def fail(self, at: int) -> Tuple[List[_Span], Optional[int], str]:
        if self.exec is not None:
            at = min(self.exec, at)
        while self.spans and self.spans[-1][0] >= at:
            self.spans.pop()
        self.spans.append((at, self.n, False))
        return self.spans, self.max_bind, _AMBIGUOUS if self.ambiguous else _INVALID

    def step(self) -> None:
        i = self.i
        c = self.s[i]
        nxt = self.at(i + 1)
        if c in _SPACE:
            self.i += 1
            return
        if c == "-" and nxt == "-":
            # MySQL reads `--` as a comment only before whitespace or a control character;
            # whether a non-ASCII character is one depends on the charset.
            comment = True
            if self.d == _MYSQL:
                x = self.at(i + 2)
                if x and ord(x) >= 0x80:
                    raise self.unsure(i)
                comment = x == "" or ord(x) <= 0x20 or ord(x) == 0x7F
            if comment:
                self.line_comment(i)
                return
        if (c == "#" and self.d == _MYSQL) or (c == "/" and nxt == "/" and self.d == _SNOWFLAKE):
            self.line_comment(i)
            return
        if c == "/" and nxt == "*":
            self.block_comment(i)
            return
        if c == "*" and nxt == "/" and self.exec is not None:
            self.exec = None
            self.i += 2
            return
        if c == "'":
            self.string(i, i, self.plain_backslash())
        elif c == '"' and self.d == _MYSQL:
            self.string(i, i, self.backslash)
        elif c in ('"', "`"):
            self.quoted(i, c, True)
        elif c == "[" and self.d == _MSSQL:
            self.quoted(i, "]", True)
        elif c == "[" and self.d == _SQLITE:
            self.quoted(i, "]", False)
        elif c == "$":
            self.dollar(i)
        elif c == "?":
            self.operand(self.digits(i + 1))
        elif c == ":" and nxt == ":":
            self.prev = _OP
            self.i += 2
        elif c == ":" and self.digit_at(i + 1) and self.d in (_ORACLE, _SNOWFLAKE, _SQLITE):
            j = self.digits(i + 1)
            if self.d == _ORACLE:
                self.bind(i + 1, j)
            self.operand(j)
        elif _is_digit(c) or (c == "." and self.digit_at(i + 1) and not self.member_access(i)):
            self.number(i, i)
        elif c in "-+" and self.prev == _OP and (self.digit_at(i + 1) or (nxt == "." and self.digit_at(i + 2))):
            self.number(i, i + 1)
        elif _word_start(c):
            self.word(i)
        elif c in ")]}":
            self.operand(i + 1)
        else:
            self.prev = _OP
            self.i += 1

    def plain_backslash(self) -> bool:
        if self.d == _SNOWFLAKE:
            return True
        if self.d in (_MYSQL, _OTHER):
            return self.backslash
        return False

    def operand(self, end: int) -> None:
        self.prev = _OPERAND
        self.i = end

    def literal(self, start: int, end: int, body: bool) -> None:
        self.spans.append((start, end, body))
        self.operand(end)

    def digits(self, j: int) -> int:
        while self.digit_at(j):
            j += 1
        return j

    def bind(self, start: int, end: int) -> None:
        # A value past u32::MAX is never reached by the numbering: ignored, as in vericto-cli.
        v = int(self.s[start:end])
        if v <= 0xFFFFFFFF and (self.max_bind is None or v > self.max_bind):
            self.max_bind = v

    def member_access(self, i: int) -> bool:
        return i > 0 and (_word_char(self.s[i - 1]) or self.s[i - 1] in '"`])')

    def line_comment(self, i: int) -> None:
        if self.exec is not None:
            raise self.unsure(i)
        end = self.s.find("\n", i)
        self.i = self.n if end == -1 else end

    def block_comment(self, i: int) -> None:
        if self.exec is not None:
            raise self.unsure(i)
        if self.d == _MYSQL:
            opened: Optional[int] = None
            if self.at(i + 2) == "!":
                opened = i + 3
            elif self.at(i + 2) == "M" and self.at(i + 3) == "!":
                opened = i + 4
            if opened is not None:
                # Executed by the server: its content is code, lexed up to the `*/` closing it.
                self.exec = i
                self.i = self.digits(opened)
                return
        nested = self.d in (_POSTGRES, _OTHER, _MSSQL)
        depth = 1
        j = i + 2
        s = self.s
        while True:
            if j + 1 >= self.n:
                raise _Fail(i)
            pair = s[j : j + 2]
            if pair == "*/":
                j += 2
                depth -= 1
                if depth == 0:
                    break
            elif pair == "/*" and nested:
                j += 2
                depth += 1
            elif pair == "/*" and self.d == _MYSQL:
                # MySQL does not nest comments: what it makes of this is not certain.
                raise self.unsure(i)
            else:
                j += 1
        self.i = j

    def string(self, start: int, quote: int, backslash: bool) -> None:
        s = self.s
        q = s[quote]
        j = quote + 1
        while True:
            if j >= self.n:
                raise _Fail(start)
            x = s[j]
            if x == q:
                if self.at(j + 1) == q:
                    j += 2
                    continue
                j += 1
                break
            if x == "\\" and backslash:
                if j + 1 >= self.n:
                    raise _Fail(start)
                j += 2
                continue
            j += 1
        self.closes_no_exec(start, j)
        self.literal(start, j, False)

    def closes_no_exec(self, start: int, end: int) -> None:
        # Whether a `*/` inside a quoted token closes the executable comment around it
        # depends on whether the server executes the comment.
        if self.exec is not None and "*/" in self.s[start:end]:
            raise self.unsure(start)

    def quoted(self, start: int, close: str, doubled: bool) -> None:
        j = start + 1
        while True:
            if j >= self.n:
                raise _Fail(start)
            x = self.s[j]
            j += 1
            if x == close:
                if doubled and self.at(j) == close:
                    j += 1
                    continue
                break
        self.closes_no_exec(start, j)
        self.operand(j)

    def qquote(self, start: int, quote: int) -> None:
        k = quote + 1
        if k >= self.n or self.s[k] in " \t\n\r":
            raise _Fail(start)
        opener = self.s[k]
        close = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opener, opener) + "'"
        end = self.s.find(close, k + 1)
        if end == -1:
            raise _Fail(start)
        self.literal(start, end + len(close), False)

    def number(self, start: int, frm: int) -> None:
        def digits(j: int) -> int:
            while j < self.n and (_is_digit(self.s[j]) or self.s[j] == "_"):
                j += 1
            return j

        j = digits(frm)
        if self.at(j) == ".":
            j = digits(j + 1)
        if self.at(j) in ("e", "E"):
            k = j + 2 if self.at(j + 1) in ("+", "-") else j + 1
            if self.digit_at(k):
                j = self.digits(k)
        # Hex digits (0x1F), type suffixes (1.5f) and trailing junk: all part of the token.
        while j < self.n and _word_char(self.s[j]):
            j += 1
        self.literal(start, j, False)

    def dollar(self, i: int) -> None:
        s = self.s
        nxt = self.at(i + 1)
        if self.pg_like:
            if self.digit_at(i + 1):
                j = self.digits(i + 1)
                self.bind(i + 1, j)
                self.operand(j)
                return
            # `$$` or `$tag$`, the tag an identifier without `$`.
            j = i + 1
            if nxt != "$":
                if not (nxt and _word_start(nxt)):
                    self.prev = _OP
                    self.i += 1
                    return
                while j < self.n and s[j] != "$" and _word_char(s[j]):
                    j += 1
                if self.at(j) != "$":
                    self.prev = _OP
                    self.i += 1
                    return
            tag = s[i : j + 1]
            close = s.find(tag, j + 1)
            if close == -1:
                raise _Fail(i)
            self.dollar_string(i, close + len(tag))
            return
        if self.d == _SNOWFLAKE and nxt == "$":
            close = s.find("$$", i + 2)
            if close == -1:
                raise _Fail(i)
            self.dollar_string(i, close + 2)
            return
        if self.d == _MSSQL and (self.digit_at(i + 1) or (nxt == "." and self.digit_at(i + 2))):
            self.number(i, i + 1)
            return
        # `$name` / `$1`: a variable, a stage column or a bind parameter.
        j = i + 1
        while j < self.n and _word_char(s[j]):
            j += 1
        if self.d == _ORACLE and j > i + 1 and all(_is_digit(ch) for ch in s[i + 1 : j]):
            self.bind(i + 1, j)
        self.operand(j)

    def dollar_string(self, start: int, end: int) -> None:
        body = False
        if isinstance(self.prev, tuple):
            a, b = self.prev
            body = _ascii_lower(self.s[a:b]) in ("as", "do")
        self.literal(start, end, body)

    def word(self, i: int) -> None:
        s = self.s
        hash_ok = self.d in (_ORACLE, _MSSQL)
        j = i
        while j < self.n and (_word_char(s[j]) or (hash_ok and s[j] == "#")):
            j += 1
        after = self.at(j)
        if after == "'":
            kind = self.prefix(s[i:j])
            if kind == _PLAIN:
                self.string(i, j, self.plain_backslash())
                return
            if kind == _ESCAPE:
                self.string(i, j, True)
                return
            if kind == _QQUOTE:
                self.qquote(i, j)
                return
        elif after == "&" and self.pg_like and self.at(j + 1) == "'" and _ascii_lower(s[i:j]) == "u":
            # PostgreSQL U&'...': no backslash escape for the quote.
            self.string(i, j + 1, False)
            return
        self.prev = (i, j)
        self.i = j

    def prefix(self, word: str) -> Optional[str]:
        w = _ascii_lower(word)
        d = self.d
        if d in (_POSTGRES, _OTHER) and w == "e":
            return _ESCAPE
        if d in (_POSTGRES, _OTHER, _MYSQL) and w in ("n", "b", "x"):
            return _PLAIN
        if d == _MYSQL and len(w) > 1 and w.startswith("_"):
            return _PLAIN
        if d in (_ORACLE, _MSSQL) and w == "n":
            return _PLAIN
        if d == _ORACLE and w in ("q", "nq"):
            return _QQUOTE
        if d in (_SNOWFLAKE, _SQLITE) and w == "x":
            return _PLAIN
        return None


def _union(a: List[_Span], b: List[_Span]) -> List[_Span]:
    """Every character that is a literal in either reading; overlapping spans as one."""
    out: List[_Span] = []
    for start, end, _ in sorted(a + b, key=lambda s: (s[0], s[1])):
        if out and start < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end), False)
        else:
            out.append((start, end, False))
    return out


def _render(sql: str, spans: List[_Span], question: bool, max_bind: Optional[int]) -> str:
    out: List[str] = []
    nxt = 1 if max_bind is None else max_bind + 1
    pos = 0
    last = ""
    for start, end, body in spans:
        if start > pos:
            out.append(sql[pos:start])
            last = sql[start - 1]
        # `ñ$1` or `typE$1` would read as one word: keep them apart.
        if last and (_word_char(last) or last == "?"):
            out.append(" ")
        if body:
            mark = "$$$$"
        elif question:
            mark = "?"
        else:
            mark = f"${nxt}"
            nxt += 1
        out.append(mark)
        last = mark[-1]
        pos = end
    out.append(sql[pos:])
    return "".join(out)


def sanitize(sql: str, dialect: str) -> str:
    """Return ``sql`` with every literal replaced by a dialect-correct placeholder."""
    name = _ascii_lower(dialect.strip(_SPACE))
    d = name if name in _KNOWN else _OTHER
    spans, max_bind, state = _Lexer(sql, d, True).run()
    if d in (_MYSQL, _OTHER) and "\\" in sql:
        spans2, max2, state2 = _Lexer(sql, d, False).run()
        if max2 is not None and (max_bind is None or max2 > max_bind):
            max_bind = max2
        # A reading is dropped only when the text does not lex that way; an ambiguous
        # one stays possible, so both count.
        if spans == spans2 or (state == _COMPLETE and state2 == _INVALID):
            pass
        elif state == _INVALID and state2 == _COMPLETE:
            spans = spans2
        else:
            spans = _union(spans, spans2)
    return _render(sql, spans, d in (_MYSQL, _SNOWFLAKE, _SQLITE), max_bind)


__all__ = ["sanitize"]
