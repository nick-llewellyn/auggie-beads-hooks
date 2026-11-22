#!/usr/bin/env python3
"""Wire, unwire, inspect, and check compatibility of Beads hooks for Auggie and Antigravity.

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

from shell_tokens import Token, split_shell_segments

HOOK_SCRIPT_NAMES = {"beads-prime.sh", "beads-sync.sh", "beads-guard.sh"}
SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash"}
GENERIC_WRAPPERS = {"env", "exec", "command", "nohup", "time", "nice"}
CONTROL_KEYWORDS = {"if", "then", "elif", "else", "while", "until", "do", "{", "!", "coproc"}
STDIN_FILE_REDIRS = {"<", "0<", "<>", "0<>"}
STDIN_OVERRIDE_REDIRS = {"<<<", "0<<<", "<<", "0<<", "<<-", "0<<-", "<&", "0<&"}
WRAPPER_VALUE_FLAGS: dict[str, set[str]] = {
    "env": {"-u", "-C", "-S"},
    "exec": {"-a"},
    "nice": {"-n"},
}
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
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


def _matched_hook_script(tok: Token) -> str | None:
    if tok.is_redir:
        return None
    base = os.path.basename(tok.text)
    return base if base in HOOK_SCRIPT_NAMES else None


def _segment_hook_scripts(tokens: list[Token], depth: int = 0) -> set[str]:
    """Return the set of our hook script basenames executed by `tokens`."""
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok.is_redir:
            idx += 2
            continue
        if not tok.quoted and tok.text in CONTROL_KEYWORDS:
            idx += 1
            continue
        if tok.assign_prefix_unquoted:
            idx += 1
            continue

        base = os.path.basename(tok.text)
        if base in GENERIC_WRAPPERS:
            if base == "command" and idx + 1 < n and tokens[idx + 1].text in {"-v", "-V"}:
                return set()
            val_flags = WRAPPER_VALUE_FLAGS.get(base, set())
            idx += 1
            while idx < n:
                w = tokens[idx]
                if w.is_redir:
                    idx += 2
                    continue
                if w.text == "--":
                    idx += 1
                    break
                if base == "env" and ENV_ASSIGN_RE.match(w.text):
                    idx += 1
                    continue
                if w.text in val_flags:
                    idx += 2
                    continue
                if w.text.startswith("-") and len(w.text) > 1:
                    if base == "command" and ("v" in w.text[1:] or "V" in w.text[1:]):
                        return set()
                    idx += 1
                    continue
                break
            continue

        if base in SHELL_WRAPPERS:
            j = idx + 1
            reads_stdin = False
            final_stdin_script: str | None = None
            while j < n:
                arg = tokens[j]
                if arg.is_redir:
                    target = tokens[j + 1] if j + 1 < n else None
                    if arg.text in STDIN_FILE_REDIRS:
                        final_stdin_script = _matched_hook_script(target) if target else None
                    elif arg.text in STDIN_OVERRIDE_REDIRS:
                        final_stdin_script = None
                    j += 2
                    continue
                if arg.text in {"--help", "--version", "-n", "--noexec"}:
                    return set()
                if arg.text == "-o" and j + 1 < n:
                    if tokens[j + 1].text.lower().replace("_", "").replace("-", "") == "noexec":
                        return set()
                    j += 2
                    continue
                if arg.text in {"+o", "-O", "+O", "--rcfile", "--init-file"}:
                    j += 2
                    continue
                if arg.text == "--":
                    j += 1
                    break
                if arg.text.startswith("-") and not arg.text.startswith("--"):
                    flags = arg.text[1:]
                    if "n" in flags:
                        return set()
                    if "c" in flags:
                        if j + 1 < n and depth < 2:
                            return executed_hook_scripts(tokens[j + 1].text, depth=depth + 1)
                        return set()
                    if "s" in flags:
                        reads_stdin = True
                    j += 1
                    continue
                if arg.text.startswith("--"):
                    j += 1
                    continue
                break

            # Track remaining positional operands and the FINAL stdin redirection source.
            positionals: list[Token] = []
            k = j
            while k < n:
                cur = tokens[k]
                if cur.is_redir:
                    target = tokens[k + 1] if k + 1 < n else None
                    if cur.text in STDIN_FILE_REDIRS:
                        final_stdin_script = _matched_hook_script(target) if target else None
                    elif cur.text in STDIN_OVERRIDE_REDIRS:
                        final_stdin_script = None
                    k += 2
                    continue
                positionals.append(cur)
                k += 1

            if reads_stdin:
                return {final_stdin_script} if final_stdin_script else set()
            if positionals:
                hit = _matched_hook_script(positionals[0])
                return {hit} if hit else set()
            return {final_stdin_script} if final_stdin_script else set()

        hit = _matched_hook_script(tok)
        return {hit} if hit else set()

    return set()


def executed_hook_scripts(line: str, depth: int = 0) -> set[str]:
    if not line or not line.strip():
        return set()
    found: set[str] = set()
    for seg in split_shell_segments(line):
        found.update(_segment_hook_scripts(seg, depth=depth))
    return found


def runs_our_hook_line(line: str, depth: int = 0) -> bool:
    return bool(executed_hook_scripts(line, depth=depth))


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


def _expand_shell_path_token(tok: Token) -> str:
    """Expand `~` and shell environment prefixes (`$HOME`, `$XDG_DATA_HOME`) when not single-quoted."""
    raw = tok.text
    if not tok.single_quoted_dollar and "$" in raw:
        raw = os.path.expandvars(raw)
    if not tok.quoted and raw.startswith("~"):
        raw = os.path.expanduser(raw)
    elif raw.startswith("~/") and not tok.single_quoted_dollar:
        raw = os.path.expanduser(raw)
    return raw


def _hook_targets_prefix(hook: dict[str, Any], prefix: Path | None) -> bool:
    if prefix is None:
        return True
    line = _command_line_of(hook)
    resolved_prefix = prefix.resolve()
    candidates = {
        str(resolved_prefix / "current"),
        str(Path(os.path.abspath(str(prefix))) / "current"),
    }
    home = str(Path.home().resolve())
    for c in list(candidates):
        if c.startswith(home + "/"):
            candidates.add("~" + c[len(home) :])
    if any(c in line for c in candidates):
        return True
    # Also resolve symlinked or env-var-prefixed paths (e.g. /tmp vs /private/tmp, $HOME)
    for seg in split_shell_segments(line):
        for tok in seg:
            if tok.is_redir:
                continue
            if os.path.basename(tok.text) in HOOK_SCRIPT_NAMES:
                expanded = _expand_shell_path_token(tok)
                parts = Path(expanded).parts
                if "current" in parts:
                    idx = parts.index("current")
                    if idx > 0:
                        hook_prefix = Path(*parts[:idx])
                        try:
                            if hook_prefix.resolve() == resolved_prefix:
                                return True
                        except OSError:
                            pass
    return False


def check_transition_compat(
    settings_path: Path,
    will_wire_auggie: bool,
    target_tag: str,
    prefix: Path | None = None,
) -> None:
    """Reject switching `current` when Auggie still wires legacy PostToolUse without migrating."""
    if will_wire_auggie or not settings_path.exists():
        return
    data = load_json_object(settings_path)
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        post_entries = hooks.get("PostToolUse")
        if isinstance(post_entries, list):
            for entry in post_entries:
                for h in _iter_event_hooks(entry):
                    if not _hook_targets_prefix(h, prefix):
                        continue
                    if "beads-sync.sh" in executed_hook_scripts(_command_line_of(h)):
                        die(
                            f"refusing to switch 'current' to {target_tag} while {settings_path} "
                            "still has legacy PostToolUse wiring for beads-sync.sh.\n"
                            f"Re-run with '--agent auggie --wire' or '--agent all --wire' "
                            f"(e.g. 'install.sh {target_tag} --agent all --wire') to migrate "
                            "Auggie wiring to PreToolUse/Stop/SessionEnd."
                        )


def _apply_staged_writes(staged: list[tuple[Path, dict[str, Any]]]) -> list[tuple[Path, bool]]:
    """Write all staged JSON files atomically, rolling back earlier writes on any failure."""
    applied: list[tuple[Path, bool, bool, bytes | None]] = []
    try:
        for path, data in staged:
            existed = path.exists()
            old_bytes = path.read_bytes() if existed else None
            changed = atomic_write_json(path, data)
            applied.append((path, changed, existed, old_bytes))
    except BaseException:
        # Roll back any files modified earlier in this transaction.
        for path, changed, existed, old_bytes in reversed(applied):
            if not changed:
                continue
            try:
                if existed and old_bytes is not None:
                    path.write_bytes(old_bytes)
                elif not existed:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return [(path, changed) for path, changed, _, _ in applied]


def _prepare_auggie_data(settings_path: Path, hooks_dir: Path | None, wire: bool) -> dict[str, Any]:
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
    return data


def _prepare_antigravity_data(
    hooks_path: Path, hooks_dir: Path | None, wire: bool
) -> dict[str, Any] | None:
    if not wire and not hooks_path.exists():
        return None
    data: dict[str, Any] = load_json_object(hooks_path) if hooks_path.exists() else {}
    if wire:
        assert hooks_dir is not None
        prime_cmd = f"{quote_shell_path(str(hooks_dir / 'beads-prime.sh'))} --agent antigravity"
        guard_cmd = f"{quote_shell_path(str(hooks_dir / 'beads-guard.sh'))} --agent antigravity"
        sync_cmd = f"{quote_shell_path(str(hooks_dir / 'beads-sync.sh'))} --agent antigravity"
        data[ANTIGRAVITY_GROUP_NAME] = {
            "SessionStart": [{"type": "command", "command": prime_cmd, "timeout": 15}],
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [{"type": "command", "command": guard_cmd, "timeout": 5}],
                }
            ],
            "Stop": [{"type": "command", "command": sync_cmd, "timeout": 60}],
        }
    else:
        data.pop(ANTIGRAVITY_GROUP_NAME, None)
    return data


def wire_all(
    settings_path: Path,
    agy_hooks_path: Path,
    hooks_dir: Path | None,
    *,
    do_auggie: bool,
    do_agy: bool,
    wire: bool,
) -> None:
    """Prepare and validate all selected configs before writing any of them."""
    staged: list[tuple[Path, dict[str, Any]]] = []
    aug_data: dict[str, Any] | None = None
    if do_auggie:
        aug_data = _prepare_auggie_data(settings_path, hooks_dir, wire=wire)
        staged.append((settings_path, aug_data))
    if do_agy:
        agy_data = _prepare_antigravity_data(agy_hooks_path, hooks_dir, wire=wire)
        if agy_data is None:
            print(f"{agy_hooks_path} is unchanged")
        else:
            staged.append((agy_hooks_path, agy_data))

    results = _apply_staged_writes(staged)
    for path, changed in results:
        print(f"updated {path}" if changed else f"{path} is unchanged")
        if path == settings_path and aug_data is not None:
            _print_auggie_mentions(aug_data)


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
    parser.add_argument("action", choices=("wire", "unwire", "status", "check-compat"))
    parser.add_argument("--agent", choices=("auggie", "antigravity", "all"), default="auggie")
    parser.add_argument("--settings", required=True)
    parser.add_argument("--antigravity-hooks", required=True)
    parser.add_argument("--hooks-dir", default="")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--will-wire", choices=("0", "1"), default="0")
    parser.add_argument("--target-tag", default="")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    settings_path = Path(args.settings)
    agy_hooks_path = Path(args.antigravity_hooks)
    hooks_dir = Path(args.hooks_dir) if args.hooks_dir else None
    prefix_path = Path(args.prefix) if args.prefix else None

    do_auggie = args.agent in ("auggie", "all")
    do_agy = args.agent in ("antigravity", "all")

    if args.action == "check-compat":
        check_transition_compat(
            settings_path=settings_path,
            will_wire_auggie=(args.will_wire == "1" and do_auggie),
            target_tag=args.target_tag or "target",
            prefix=prefix_path,
        )
    elif args.action == "wire":
        if hooks_dir is None:
            die("--hooks-dir is required for wire")
        wire_all(
            settings_path,
            agy_hooks_path,
            hooks_dir,
            do_auggie=do_auggie,
            do_agy=do_agy,
            wire=True,
        )
    elif args.action == "unwire":
        wire_all(
            settings_path,
            agy_hooks_path,
            None,
            do_auggie=do_auggie,
            do_agy=do_agy,
            wire=False,
        )
    elif args.action == "status":
        if do_auggie:
            status_auggie(settings_path)
        if do_agy:
            status_antigravity(agy_hooks_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
