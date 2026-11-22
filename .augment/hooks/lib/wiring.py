#!/usr/bin/env python3
"""Wire, unwire, and inspect Beads hooks in Auggie and Antigravity configs.

Invoked by `install.sh` using Python 3.9+ stdlib only (no jq or PCRE regexes).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

HOOK_SCRIPT_NAMES = {"beads-prime.sh", "beads-sync.sh", "beads-guard.sh"}
SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash"}
GENERIC_WRAPPERS = {"env", "exec", "command", "nohup", "time", "nice"}
WRAPPER_VALUE_FLAGS: dict[str, set[str]] = {
    "env": {"-u", "-C", "-S"},
    "exec": {"-a"},
    "nice": {"-n"},
}
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
REDIR_OP_RE = re.compile(r"^(?:\d+)?(?:>>|>\||>&|>|<<<|<<-|<<|<>|<&|<|&>>|&>)$")
MENTION_RE = re.compile(
    r"(?:^|[/\'\"\s;&|(])beads-(?:prime|sync|guard)\.sh[\'\"]?(?:\s|$|[;&|)<>\`])"
)

ANTIGRAVITY_GROUP_NAME = "beads"


def die(msg: str) -> None:
    sys.stderr.write(f"install.sh: {msg}\n")
    raise SystemExit(1)


def quote_shell_path(path: str) -> str:
    if "'" in path:
        die(f"cannot wire a path holding a single quote: {path}")
    if re.search(r"[^A-Za-z0-9/._+-]", path):
        return f"'{path}'"
    return path


def _command_line_of(hook: dict[str, Any]) -> str:
    cmd = str(hook.get("command") or "")
    args = hook.get("args")
    if isinstance(args, list) and args:
        parts = [cmd] if cmd else []
        for a in args:
            s = str(a)
            if re.fullmatch(r"[A-Za-z0-9_./+=:@%,-]+", s):
                parts.append(s)
            else:
                parts.append(shlex.quote(s))
        return " ".join(parts)
    return cmd


def _display_line_of(hook: dict[str, Any]) -> str:
    cmd = str(hook.get("command") or "")
    args = hook.get("args")
    if isinstance(args, list) and args:
        return " ".join([cmd, *(str(a) for a in args)]).strip()
    return cmd


def _tokenize_segments(line: str) -> list[list[str]]:
    """Tokenize a shell command line into simple command segments."""
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = False
        lex.commenters = "#"
        raw_tokens = list(lex)
    except ValueError:
        # Unclosed quote or exotic syntax: fall back to whitespace splitting.
        raw_tokens = line.split()

    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in raw_tokens:
        if tok in {";", "&&", "||", "|", "&", "(", ")", "\n"}:
            if cur:
                segments.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _strip_redirection_suffix(tok: str) -> str:
    # Handle cases like `/old/beads-sync.sh>/dev/null` when fallback-split.
    for op in ("&>>", "&>", ">>", ">|", ">&", "<<<", "<<", "<>", "<&", ">", "<"):
        pos = tok.find(op)
        if pos > 0:
            return tok[:pos]
    return tok


def _is_hook_script(tok: str) -> bool:
    cleaned = _strip_redirection_suffix(tok)
    return os.path.basename(cleaned) in HOOK_SCRIPT_NAMES


def _segment_runs_our_hook(tokens: list[str], depth: int = 0) -> bool:
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if REDIR_OP_RE.match(tok):
            idx += 2
            continue
        if ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue

        base = os.path.basename(tok)
        if base in GENERIC_WRAPPERS:
            if base == "command" and idx + 1 < n and tokens[idx + 1] in {"-v", "-V"}:
                return False
            val_flags = WRAPPER_VALUE_FLAGS.get(base, set())
            idx += 1
            while idx < n:
                w = tokens[idx]
                if w == "--":
                    idx += 1
                    break
                if base == "env" and ENV_ASSIGN_RE.match(w):
                    idx += 1
                    continue
                if w in val_flags:
                    idx += 2
                    continue
                if w.startswith("-") and len(w) > 1:
                    if base == "command" and ("v" in w[1:] or "V" in w[1:]):
                        return False
                    idx += 1
                    continue
                break
            continue

        if base in SHELL_WRAPPERS:
            j = idx + 1
            reads_stdin = False
            while j < n:
                arg = tokens[j]
                if arg in {"--help", "--version", "-n", "--noexec"}:
                    return False
                if arg == "-o" and j + 1 < n:
                    if tokens[j + 1].lower().replace("_", "").replace("-", "") == "noexec":
                        return False
                    j += 2
                    continue
                if arg in {"+o", "-O", "+O", "--rcfile", "--init-file"}:
                    j += 2
                    continue
                if arg == "--":
                    j += 1
                    break
                if arg.startswith("-") and not arg.startswith("--"):
                    flags = arg[1:]
                    if "n" in flags:
                        return False
                    if "c" in flags:
                        if j + 1 < n and depth < 2:
                            return runs_our_hook_line(tokens[j + 1], depth=depth + 1)
                        return False
                    if "s" in flags:
                        reads_stdin = True
                    j += 1
                    continue
                if arg.startswith("--"):
                    j += 1
                    continue
                break
            if reads_stdin:
                # Shell reads stdin (`bash -s < script`); ignore positional args, check `<` redirect.
                for k in range(j, n - 1):
                    if tokens[k] in {"<", "0<"} and _is_hook_script(tokens[k + 1]):
                        return True
                return False
            if j < n:
                if tokens[j] in {"<", "0<"} and j + 1 < n:
                    # Only if no script operand follows after output redirects
                    candidate = tokens[j + 1]
                    k = j + 2
                    while k < n:
                        if REDIR_OP_RE.match(tokens[k]):
                            k += 2
                        else:
                            return False
                    return _is_hook_script(candidate)
                return _is_hook_script(tokens[j])
            return False

        return _is_hook_script(tok)

    return False


def runs_our_hook_line(line: str, depth: int = 0) -> bool:
    if not line or "beads-" not in line:
        return False
    # Ignore heredoc bodies by truncating at first `<<` line break.
    if "<<" in line and "\n" in line:
        head = line.split("\n", 1)[0]
        if re.search(r"<<-?\s*\S+", head):
            line = head
    for seg in _tokenize_segments(line):
        if _segment_runs_our_hook(seg, depth=depth):
            return True
    return False


def is_our_auggie_hook(hook: dict[str, Any]) -> bool:
    return runs_our_hook_line(_command_line_of(hook))


def mentions_our_hook(hook: dict[str, Any]) -> bool:
    if is_our_auggie_hook(hook):
        return False
    cmd = str(hook.get("command") or "")
    if MENTION_RE.search(cmd):
        return True
    args = hook.get("args")
    if isinstance(args, list):
        for a in args:
            if isinstance(a, str) and MENTION_RE.search(a):
                return True
    return False


def _iter_event_hooks(entry: Any) -> list[dict[str, Any]]:
    if not isinstance(entry, dict):
        return []
    nested = entry.get("hooks")
    if isinstance(nested, list):
        return [h for h in nested if isinstance(h, dict)]
    return [entry]


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"{path} is not a valid JSON object ({exc}); fix it and run again")
    if not isinstance(data, dict):
        die(f"{path} is not a JSON object; fix it and run again")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> bool:
    """Write JSON atomically with `.bak` backup; return True if file changed."""
    serialized = json.dumps(data, indent=2) + "\n"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == serialized:
                return False
        except OSError:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
        if path.exists():
            shutil.copy2(str(path), f"{path}.bak")
        os.replace(str(tmp_path), str(path))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return True


def _print_auggie_mentions(data: dict[str, Any]) -> None:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    lines: list[str] = []
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for h in _iter_event_hooks(entry):
                if mentions_our_hook(h):
                    lines.append(f"mentioned: {event}: {_display_line_of(h)}")
    if lines:
        for line in lines:
            print(line)
        print(
            "Hooks marked mentioned name beads-prime.sh, beads-sync.sh or beads-guard.sh "
            "but do not run them, as far as this can tell, so --wire and --unwire leave "
            "them in place. Remove any that runs these hooks by hand."
        )


def wire_auggie(settings_path: Path, hooks_dir: Path | None, wire: bool) -> None:
    prime_cmd = guard_cmd = sync_cmd = ""
    if wire:
        assert hooks_dir is not None
        prime_cmd = quote_shell_path(str(hooks_dir / "beads-prime.sh"))
        guard_cmd = quote_shell_path(str(hooks_dir / "beads-guard.sh"))
        sync_cmd = quote_shell_path(str(hooks_dir / "beads-sync.sh"))

    data: dict[str, Any] = load_json_object(settings_path) if settings_path.exists() else {}
    raw_hooks = data.get("hooks")
    hooks: dict[str, Any] = dict(raw_hooks) if isinstance(raw_hooks, dict) else {}

    cleaned_hooks: dict[str, Any] = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            cleaned_hooks[event] = entries
            continue
        new_entries: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                new_entries.append(entry)
                continue
            nested = entry.get("hooks")
            if isinstance(nested, list):
                kept = [
                    h for h in nested if not (isinstance(h, dict) and is_our_auggie_hook(h))
                ]
                if kept:
                    updated_entry = dict(entry)
                    updated_entry["hooks"] = kept
                    new_entries.append(updated_entry)
            elif not is_our_auggie_hook(entry):
                new_entries.append(entry)
        if new_entries:
            cleaned_hooks[event] = new_entries

    if wire:
        def append_event(event_name: str, entry: dict[str, Any]) -> None:
            existing = cleaned_hooks.get(event_name)
            if not isinstance(existing, list):
                existing = []
            cleaned_hooks[event_name] = existing + [entry]

        append_event(
            "SessionStart",
            {"hooks": [{"type": "command", "command": prime_cmd, "timeout": 15000}]},
        )
        append_event(
            "PreToolUse",
            {
                "matcher": "bash",
                "hooks": [{"type": "command", "command": guard_cmd, "timeout": 5000}],
            },
        )
        append_event(
            "Stop",
            {"hooks": [{"type": "command", "command": sync_cmd, "timeout": 60000}]},
        )
        append_event(
            "SessionEnd",
            {"hooks": [{"type": "command", "command": sync_cmd, "timeout": 60000}]},
        )

    if cleaned_hooks:
        data["hooks"] = cleaned_hooks
    else:
        data.pop("hooks", None)

    changed = atomic_write_json(settings_path, data)
    print(f"updated {settings_path}" if changed else f"{settings_path} is unchanged")
    _print_auggie_mentions(data)


def status_auggie(settings_path: Path) -> None:
    if not settings_path.exists():
        print(f"wired (auggie): none ({settings_path})")
        return
    data = load_json_object(settings_path)
    hooks = data.get("hooks")
    found: list[str] = []
    if isinstance(hooks, dict):
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                for h in _iter_event_hooks(entry):
                    if is_our_auggie_hook(h):
                        found.append(f"wired: {event}: {_display_line_of(h)}")
    if found:
        for line in found:
            print(line)
    else:
        print(f"wired (auggie): none ({settings_path})")
    _print_auggie_mentions(data)


def wire_antigravity(hooks_path: Path, hooks_dir: Path | None, wire: bool) -> None:
    prime_cmd = guard_cmd = sync_cmd = ""
    if wire:
        assert hooks_dir is not None
        prime_cmd = f"{quote_shell_path(str(hooks_dir / 'beads-prime.sh'))} --agent antigravity"
        guard_cmd = f"{quote_shell_path(str(hooks_dir / 'beads-guard.sh'))} --agent antigravity"
        sync_cmd = f"{quote_shell_path(str(hooks_dir / 'beads-sync.sh'))} --agent antigravity"

    data: dict[str, Any] = load_json_object(hooks_path) if hooks_path.exists() else {}

    if wire:
        data[ANTIGRAVITY_GROUP_NAME] = {
            "SessionStart": [
                {
                    "type": "command",
                    "command": prime_cmd,
                    "timeout": 15,
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": guard_cmd,
                            "timeout": 5,
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "type": "command",
                    "command": sync_cmd,
                    "timeout": 60,
                }
            ],
        }
    else:
        if not hooks_path.exists():
            print(f"{hooks_path} is unchanged")
            return
        data.pop(ANTIGRAVITY_GROUP_NAME, None)

    changed = atomic_write_json(hooks_path, data)
    print(f"updated {hooks_path}" if changed else f"{hooks_path} is unchanged")


def status_antigravity(hooks_path: Path) -> None:
    if not hooks_path.exists():
        print(f"wired (antigravity): none ({hooks_path})")
        return
    data = load_json_object(hooks_path)
    group = data.get(ANTIGRAVITY_GROUP_NAME)
    if not isinstance(group, dict):
        print(f"wired (antigravity): none ({hooks_path})")
        return
    found: list[str] = []
    for event, entries in group.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for h in _iter_event_hooks(entry):
                cmd = _display_line_of(h)
                if cmd:
                    found.append(f"wired (antigravity): {event}: {cmd}")
    if found:
        for line in found:
            print(line)
    else:
        print(f"wired (antigravity): none ({hooks_path})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiring.py")
    parser.add_argument("action", choices=("wire", "unwire", "status"))
    parser.add_argument("--agent", choices=("auggie", "antigravity", "all"), default="auggie")
    parser.add_argument("--settings", required=True)
    parser.add_argument("--antigravity-hooks", required=True)
    parser.add_argument("--hooks-dir", default="")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    settings_path = Path(args.settings)
    agy_hooks_path = Path(args.antigravity_hooks)
    hooks_dir = Path(args.hooks_dir) if args.hooks_dir else None

    do_auggie = args.agent in ("auggie", "all")
    do_agy = args.agent in ("antigravity", "all")

    if args.action == "wire":
        if hooks_dir is None:
            die("--hooks-dir is required for wire")
        if do_auggie:
            wire_auggie(settings_path, hooks_dir, wire=True)
        if do_agy:
            wire_antigravity(agy_hooks_path, hooks_dir, wire=True)
    elif args.action == "unwire":
        if do_auggie:
            wire_auggie(settings_path, None, wire=False)
        if do_agy:
            wire_antigravity(agy_hooks_path, None, wire=False)
    elif args.action == "status":
        if do_auggie:
            status_auggie(settings_path)
        if do_agy:
            status_antigravity(agy_hooks_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
