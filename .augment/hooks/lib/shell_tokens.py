#!/usr/bin/env python3
"""Lightweight POSIX-style shell tokenizer for Beads hooks and installer wiring.

Splits a shell command string into simple command segments while preserving:
  - single quotes ('...'), double quotes ("..."), and ANSI-C quotes ($'...')
  - backslash escapes and backslash-newline line continuations
  - `#` comments at word boundaries
  - unquoted command separators (`;`, `&&`, `||`, `|`, `&`, `(`, `)`, newline)
    strictly distinguished from quoted literals such as `echo ';'`
  - unquoted redirection operators (`>`, `>>`, `>|`, `>&`, `&>`, `&>>`, `<`,
    `<&`, `<>`, `<<<`, `<<`, `<<-`, plus optional leading fd digits like `2>`)
  - whether a leading `NAME=` / `NAME+=` assignment prefix was unquoted
    independently of whether its value contains quotes
  - whether `$` appeared inside single quotes or escaped (so callers can expand
    `$HOME` / `$XDG_DATA_HOME` only where the shell would expand them)
  - command substitutions (`$(...)`, backticks), process substitutions
    (`<(...)`, `>(...)`), and substitutions inside arithmetic expansions
    (`$((...))`) or unquoted heredocs, parsed as nested executable contexts
    without splitting the enclosing command
  - heredoc bodies (`<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`, `<<\\EOF`),
    skipping literal heredoc lines (including inside `$(...)` boundary scans)
    while resuming tokenization after the closing delimiter, and distinguishing
    here-strings (`<<<`) from heredocs
  - ordinary path/word characters (`+`, `@`, `:`, `%`, `,`, `=`, etc.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ASSIGN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?$")
MAX_SUBST_DEPTH = 4


@dataclass(frozen=True)
class Token:
    text: str
    quoted: bool = False
    is_redir: bool = False
    assign_prefix_unquoted: bool = False
    single_quoted_dollar: bool = False


def _read_ansi_c_quote(text: str, pos: int) -> tuple[str, int]:
    """Read an ANSI-C `$'...'` quoted fragment starting at `$`."""
    n = len(text)
    out: list[str] = []
    i = pos + 2  # skip $'
    while i < n:
        ch = text[i]
        if ch == "\\":
            if i + 1 < n:
                nxt = text[i + 1]
                escapes = {
                    "n": "\n",
                    "t": "\t",
                    "r": "\r",
                    "'": "'",
                    '"': '"',
                    "\\": "\\",
                }
                out.append(escapes.get(nxt, nxt))
                i += 2
                continue
            i += 1
            continue
        if ch == "'":
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return "".join(out), i


def _read_heredoc_delim_inline(text: str, pos: int) -> tuple[str, bool, int]:
    """Read a heredoc delimiter word starting at `pos` (after `<<` or `<<-`)."""
    n = len(text)
    i = pos
    while i < n and text[i] in " \t":
        i += 1
    chars: list[str] = []
    quoted = False
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                chars.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n and text[i + 1] in {'"', "\\", "$", "`", "\n"}:
                if text[i + 1] != "\n":
                    chars.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            else:
                chars.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            if text[i + 1] != "\n":
                chars.append(text[i + 1])
                quoted = True
            i += 2
            continue
        if ch in {"'", '"'}:
            quote = ch
            quoted = True
            i += 1
            continue
        if ch in " \t\r\n;&|()<>#":
            break
        chars.append(ch)
        i += 1
    return "".join(chars), quoted, i


def _read_arith_expansion(text: str, pos: int) -> tuple[str, int, list[str]]:
    """Read `$((...))` starting at `$`; return `(raw, end_pos, nested_substitutions)`."""
    n = len(text)
    i = pos + 3
    depth = 2
    nested: list[str] = []
    while i < n and depth > 0:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            if text.startswith("$((", i):
                _, i, inner_subs = _read_arith_expansion(text, i)
                nested.extend(inner_subs)
            else:
                inner, i = _read_paren_subst(text, i)
                nested.append(inner)
            continue
        if ch == "`":
            inner, i = _read_backtick(text, i)
            nested.append(inner)
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue
        i += 1
    return text[pos:i], i, nested


def _read_paren_subst(text: str, pos: int) -> tuple[str, int]:
    """Read `$(...)`, `<(...)`, or `>(...)` starting at `$`/`<`/`>`; return `(inner, end_pos)`.

    Properly skips quotes, nested substitutions, comments, and heredoc bodies
    inside the substitution so unmatched quotes or `)` in heredoc data never
    corrupt the closing boundary.
    """
    n = len(text)
    i = pos + 2  # skip $(, <(, or >(
    depth = 1
    quote: str | None = None
    pending_heredocs: list[tuple[str, bool, bool]] = []
    while i < n and depth > 0:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            if ch == "$" and i + 1 < n and text[i + 1] == "(":
                if text.startswith("$((", i):
                    _, i, _ = _read_arith_expansion(text, i)
                else:
                    _, i = _read_paren_subst(text, i)
                continue
            if ch == "`":
                _, i = _read_backtick(text, i)
                continue
            i += 1
            continue

        # Outside quotes inside $(...) / <(...) / >(...)
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "'":
            _, i = _read_ansi_c_quote(text, i)
            continue
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            continue
        if ch == "#" and (i == pos + 2 or text[i - 1] in " \t\n;&|("):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "\n":
            i += 1
            if pending_heredocs:
                i, _ = _consume_heredoc_bodies(text, i, pending_heredocs)
                pending_heredocs.clear()
            continue
        if ch == "<" and text.startswith("<<<", i):
            i += 3
            continue
        if ch == "<" and text.startswith("<<", i):
            strip_tabs = text.startswith("<<-", i)
            after_op = i + (3 if strip_tabs else 2)
            delim, delim_quoted, i = _read_heredoc_delim_inline(text, after_op)
            if delim:
                pending_heredocs.append((delim, strip_tabs, delim_quoted))
            continue
        if ch in {"<", ">"} and i + 1 < n and text[i + 1] == "(":
            _, i = _read_paren_subst(text, i)
            continue
        if ch == "$" and text.startswith("$((", i):
            _, i, _ = _read_arith_expansion(text, i)
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            _, i = _read_paren_subst(text, i)
            continue
        if ch == "`":
            _, i = _read_backtick(text, i)
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue
        i += 1

    inner_end = i - 1 if depth == 0 else i
    return text[pos + 2 : inner_end], i


def _read_backtick(text: str, pos: int) -> tuple[str, int]:
    """Read a `` `...` `` command substitution starting at `` ` ``; return `(inner, end_pos)`."""
    n = len(text)
    i = pos + 1
    out: list[str] = []
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in {"$", "`", "\\", "\n"}:
            if text[i + 1] != "\n":
                out.append(text[i + 1])
            i += 2
            continue
        if ch == "`":
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return "".join(out), i


def _extract_heredoc_substitutions(body: str) -> list[str]:
    """Extract executable `$(...)`, `$((...))`, and backtick substitutions from an unquoted heredoc."""
    subs: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n and body[i + 1] in {"$", "`", "\\", "\n"}:
            i += 2
            continue
        if ch == "$" and i + 1 < n and body[i + 1] == "(":
            if body.startswith("$((", i):
                _, i, arith_subs = _read_arith_expansion(body, i)
                subs.extend(arith_subs)
            else:
                inner, i = _read_paren_subst(body, i)
                subs.append(inner)
            continue
        if ch == "`":
            inner, i = _read_backtick(body, i)
            subs.append(inner)
            continue
        i += 1
    return subs


def _consume_heredoc_bodies(
    text: str, pos: int, pending: list[tuple[str, bool, bool]]
) -> tuple[int, list[str]]:
    """Advance `pos` past queued heredocs; return `(new_pos, nested_substitutions)`."""
    n = len(text)
    nested: list[str] = []
    for delim, strip_tabs, delim_quoted in pending:
        body_lines: list[str] = []
        while pos < n:
            nl = text.find("\n", pos)
            if nl == -1:
                line = text[pos:]
                pos = n
            else:
                line = text[pos:nl]
                pos = nl + 1
            candidate = line.rstrip("\r")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == delim:
                break
            if not delim_quoted:
                body_lines.append(line)
        if not delim_quoted and body_lines:
            nested.extend(_extract_heredoc_substitutions("\n".join(body_lines)))
    return pos, nested


def split_shell_segments(command: str, depth: int = 0) -> list[list[Token]]:
    """Split `command` into lists of `Token` representing simple commands.

    Nested command substitutions (`$(...)`, backticks), process substitutions
    (`<(...)`, `>(...)`), and substitutions inside `$((...))` or unquoted
    heredocs are appended as their own command segments without splitting the
    enclosing command.
    """
    segments: list[list[Token]] = []
    nested_commands: list[str] = []
    cur_seg: list[Token] = []
    cur_chars: list[str] = []
    in_token = False
    tok_quoted = False
    saw_quote_before_eq = False
    assign_unquoted = False
    single_quoted_dollar = False
    quote: str | None = None

    pending_heredocs: list[tuple[str, bool, bool]] = []
    expect_heredoc_delim: bool | None = None  # None or strip_tabs bool

    i = 0
    n = len(command)

    def flush_tok() -> None:
        nonlocal in_token, tok_quoted, saw_quote_before_eq, assign_unquoted
        nonlocal single_quoted_dollar, expect_heredoc_delim
        if not in_token:
            return
        word = "".join(cur_chars)
        cur_chars.clear()
        quoted = tok_quoted
        assign_ok = assign_unquoted
        sq_dollar = single_quoted_dollar
        in_token = False
        tok_quoted = False
        saw_quote_before_eq = False
        assign_unquoted = False
        single_quoted_dollar = False
        cur_seg.append(
            Token(
                word,
                quoted=quoted,
                is_redir=False,
                assign_prefix_unquoted=assign_ok,
                single_quoted_dollar=sq_dollar,
            )
        )
        if expect_heredoc_delim is not None:
            pending_heredocs.append((word, expect_heredoc_delim, quoted))
            expect_heredoc_delim = None

    def flush_seg() -> None:
        flush_tok()
        if cur_seg:
            segments.append(list(cur_seg))
            cur_seg.clear()

    def emit_redir(op: str, prefix_fd: str = "") -> None:
        nonlocal expect_heredoc_delim
        cur_seg.append(Token(prefix_fd + op, quoted=False, is_redir=True))
        if op in {"<<", "<<-"}:
            expect_heredoc_delim = op == "<<-"

    while i < n:
        ch = command[i]

        if quote == "'":
            if ch == "'":
                quote = None
            else:
                if ch == "$":
                    single_quoted_dollar = True
                cur_chars.append(ch)
            i += 1
            continue

        if quote == '"':
            if ch == "\\" and i + 1 < n and command[i + 1] in {'"', "\\", "$", "`", "\n"}:
                if command[i + 1] == "$":
                    single_quoted_dollar = True
                if command[i + 1] != "\n":
                    cur_chars.append(command[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            if ch == "$" and i + 1 < n and command[i + 1] == "(":
                if command.startswith("$((", i):
                    raw, i, arith_subs = _read_arith_expansion(command, i)
                    cur_chars.append(raw)
                    nested_commands.extend(arith_subs)
                else:
                    start = i
                    inner, i = _read_paren_subst(command, i)
                    cur_chars.append(command[start:i])
                    nested_commands.append(inner)
                continue
            if ch == "`":
                start = i
                inner, i = _read_backtick(command, i)
                cur_chars.append(command[start:i])
                nested_commands.append(inner)
                continue
            cur_chars.append(ch)
            i += 1
            continue

        # Outside quotes
        if ch == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                i += 2
                continue
            if not assign_unquoted:
                saw_quote_before_eq = True
            if command[i + 1] == "$":
                single_quoted_dollar = True
            cur_chars.append(command[i + 1])
            in_token = True
            tok_quoted = True
            i += 2
            continue

        if ch == "$" and i + 1 < n and command[i + 1] == "'":
            if not assign_unquoted:
                saw_quote_before_eq = True
            frag, i = _read_ansi_c_quote(command, i)
            if "$" in frag:
                single_quoted_dollar = True
            cur_chars.append(frag)
            in_token = True
            tok_quoted = True
            continue

        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            if command.startswith("$((", i):
                raw, i, arith_subs = _read_arith_expansion(command, i)
                cur_chars.append(raw)
                nested_commands.extend(arith_subs)
            else:
                start = i
                inner, i = _read_paren_subst(command, i)
                cur_chars.append(command[start:i])
                nested_commands.append(inner)
            in_token = True
            continue

        if ch == "`":
            start = i
            inner, i = _read_backtick(command, i)
            cur_chars.append(command[start:i])
            nested_commands.append(inner)
            in_token = True
            continue

        # Process substitutions `<(...)` and `>(...)` stay part of the word without splitting
        if ch in {"<", ">"} and i + 1 < n and command[i + 1] == "(":
            start = i
            inner, i = _read_paren_subst(command, i)
            cur_chars.append(command[start:i])
            nested_commands.append(inner)
            in_token = True
            continue

        if ch in {"'", '"'}:
            if not assign_unquoted:
                saw_quote_before_eq = True
            quote = ch
            in_token = True
            tok_quoted = True
            i += 1
            continue

        if ch == "#" and not in_token:
            while i < n and command[i] != "\n":
                i += 1
            continue

        if ch == "\n":
            flush_seg()
            i += 1
            if pending_heredocs:
                i, heredoc_subs = _consume_heredoc_bodies(command, i, pending_heredocs)
                nested_commands.extend(heredoc_subs)
                pending_heredocs.clear()
            continue

        if ch in {" ", "\t", "\r"}:
            flush_tok()
            i += 1
            continue

        if ch in {";", "(", ")"}:
            flush_seg()
            i += 1
            continue

        # Redirections starting with `<`, `>`, or `&>` / `&>>`
        if ch == "&" and i + 1 < n and command[i + 1] == ">":
            flush_tok()
            if i + 2 < n and command[i + 2] == ">":
                emit_redir("&>>")
                i += 3
            else:
                emit_redir("&>")
                i += 2
            continue

        if ch in {"<", ">"}:
            prefix_fd = ""
            if in_token and not tok_quoted and "".join(cur_chars).isdigit():
                prefix_fd = "".join(cur_chars)
                cur_chars.clear()
                in_token = False
                saw_quote_before_eq = False
                assign_unquoted = False
                single_quoted_dollar = False
            else:
                flush_tok()

            if ch == "<":
                if command.startswith("<<<", i):
                    emit_redir("<<<", prefix_fd)
                    i += 3
                elif command.startswith("<<-", i):
                    emit_redir("<<-", prefix_fd)
                    i += 3
                elif command.startswith("<<", i):
                    emit_redir("<<", prefix_fd)
                    i += 2
                elif command.startswith("<&", i):
                    emit_redir("<&", prefix_fd)
                    i += 2
                elif command.startswith("<>", i):
                    emit_redir("<>", prefix_fd)
                    i += 2
                else:
                    emit_redir("<", prefix_fd)
                    i += 1
            else:
                if command.startswith(">>", i):
                    emit_redir(">>", prefix_fd)
                    i += 2
                elif command.startswith(">|", i):
                    emit_redir(">|", prefix_fd)
                    i += 2
                elif command.startswith(">&", i):
                    emit_redir(">&", prefix_fd)
                    i += 2
                else:
                    emit_redir(">", prefix_fd)
                    i += 1
            continue

        if ch in {"&", "|"}:
            flush_seg()
            if i + 1 < n and command[i + 1] == ch:
                i += 2
            else:
                i += 1
            continue

        if ch == "=" and not assign_unquoted and not saw_quote_before_eq:
            prefix_text = "".join(cur_chars)
            if ASSIGN_NAME_RE.fullmatch(prefix_text):
                assign_unquoted = True
        cur_chars.append(ch)
        in_token = True
        i += 1

    flush_seg()

    if depth < MAX_SUBST_DEPTH:
        for sub in nested_commands:
            if sub.strip():
                segments.extend(split_shell_segments(sub, depth=depth + 1))

    return segments
