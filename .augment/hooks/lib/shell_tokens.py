#!/usr/bin/env python3
"""Lightweight POSIX-style shell tokenizer for Beads hooks and installer wiring.

Splits a shell command string into simple command segments while accurately
preserving:
  - single quotes ('...'), double quotes ("..."), and ANSI-C quotes ($'...')
  - backslash escapes and backslash-newline line continuations
  - `#` comments at word boundaries
  - unquoted command separators (`;`, `&&`, `||`, `|`, `&`, `(`, `)`, newline)
    strictly distinguished from quoted literals such as `echo ';'`
  - unquoted redirection operators (`>`, `>>`, `>|`, `>&`, `&>`, `&>>`, `<`,
    `<&`, `<>`, `<<<`, `<<`, `<<-`, plus optional leading fd digits like `2>`)
  - heredoc bodies (`<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`, `<<\\EOF`),
    skipping literal heredoc lines while resuming tokenization after the
    closing delimiter, and distinguishing here-strings (`<<<`) from heredocs
  - ordinary path/word characters (`+`, `@`, `:`, `%`, `,`, `=`, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass

REDIR_OPERATORS = {
    ">",
    ">>",
    ">|",
    ">&",
    "&>",
    "&>>",
    "<",
    "<&",
    "<>",
    "<<<",
    "<<",
    "<<-",
}


@dataclass(frozen=True)
class Token:
    text: str
    quoted: bool = False
    is_redir: bool = False


def _consume_heredoc_bodies(
    text: str, pos: int, pending: list[tuple[str, bool]]
) -> int:
    """Advance `pos` past all queued heredoc bodies starting after a newline."""
    n = len(text)
    for delim, strip_tabs in pending:
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
    return pos


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


def split_shell_segments(command: str) -> list[list[Token]]:
    """Split `command` into lists of `Token` representing simple commands."""
    segments: list[list[Token]] = []
    cur_seg: list[Token] = []
    cur_chars: list[str] = []
    in_token = False
    tok_quoted = False
    quote: str | None = None

    pending_heredocs: list[tuple[str, bool]] = []
    expect_heredoc_delim: bool | None = None  # None or strip_tabs bool

    i = 0
    n = len(command)

    def flush_tok() -> None:
        nonlocal in_token, tok_quoted, expect_heredoc_delim
        if not in_token:
            return
        word = "".join(cur_chars)
        cur_chars.clear()
        quoted = tok_quoted
        in_token = False
        tok_quoted = False
        cur_seg.append(Token(word, quoted=quoted, is_redir=False))
        if expect_heredoc_delim is not None:
            pending_heredocs.append((word, expect_heredoc_delim))
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
                cur_chars.append(ch)
            i += 1
            continue

        if quote == '"':
            if ch == "\\" and i + 1 < n and command[i + 1] in {'"', "\\", "$", "`", "\n"}:
                if command[i + 1] != "\n":
                    cur_chars.append(command[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            else:
                cur_chars.append(ch)
            i += 1
            continue

        # Outside quotes
        if ch == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                # Line continuation
                i += 2
                continue
            cur_chars.append(command[i + 1])
            in_token = True
            tok_quoted = True
            i += 2
            continue

        if ch == "$" and i + 1 < n and command[i + 1] == "'":
            frag, i = _read_ansi_c_quote(command, i)
            cur_chars.append(frag)
            in_token = True
            tok_quoted = True
            continue

        if ch in {"'", '"'}:
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
                i = _consume_heredoc_bodies(command, i, pending_heredocs)
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

        cur_chars.append(ch)
        in_token = True
        i += 1

    flush_seg()
    return segments
