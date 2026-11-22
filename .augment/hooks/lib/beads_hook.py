#!/usr/bin/env python3
"""Cross-agent Beads lifecycle hooks for Auggie CLI and Antigravity.

Subcommands:
  prime  (SessionStart)      Optional short-timeout pull + `bd prime` context injection
  guard  (PreToolUse)        Block interactive `bd edit` calls that hang agents
  sync   (Stop / SessionEnd) State-based Dolt commit + optional push under fcntl.flock
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

HOOK_LOCK_NAME = ".beads-hook.lock"
DEFAULT_LOCK_TIMEOUT_S = 30.0
DEFAULT_PULL_TIMEOUT_S = 5.0
DEFAULT_PRIME_TIMEOUT_S = 12.0
DEFAULT_COMMIT_TIMEOUT_S = 30.0
DEFAULT_PUSH_TIMEOUT_S = 45.0
DEFAULT_PROBE_TIMEOUT_S = 5.0

SHELL_TOOL_NAMES = {"bash", "launch-process", "run_command"}

# Global flags accepted by `bd` before a subcommand, split by whether they take
# a separate value argument.
BD_VALUE_FLAGS = {
    "-C",
    "--directory",
    "--db",
    "--database",
    "--actor",
    "--dolt-auto-commit",
    "--mem-profile",
}
BD_BOOL_FLAGS = {
    "-q",
    "--quiet",
    "-v",
    "--verbose",
    "--json",
    "--no-color",
    "--readonly",
    "--sandbox",
    "--global",
    "--ignore-schema-skew",
    "--cpu-profile",
}

WRAPPER_VALUE_FLAGS: dict[str, set[str]] = {
    "env": {"-u", "-C", "-S"},
    "exec": {"-a"},
    "nice": {"-n"},
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-T", "-U"},
}
WRAPPER_COMMANDS = {"env", "command", "exec", "nohup", "time", "nice", "sudo"}
SHELL_COMMANDS = {"sh", "bash", "zsh", "dash"}

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
REDIR_OP_RE = re.compile(r"^(?:\d+)?(?:>>|>\||>&|>|<<<|<<-|<<|<>|<&|<|&>>|&>)$")
REDIR_ATTACHED_RE = re.compile(r"^(?:\d+)?(?:>>|>\||>&|>|<<<|<<-|<<|<>|<&|<|&>>|&>).+")

BD_EDIT_BLOCK_MESSAGE = (
    "beads: 'bd edit' opens an interactive $EDITOR and hangs agent sessions. "
    "Use non-interactive 'bd update <id> --title/--description/--notes/--design/--acceptance' instead."
)


class LockTimeoutError(Exception):
    """Raised when the per-store flock cannot be acquired within the timeout."""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
        return val if val >= 0 else default
    except ValueError:
        return default


def clean_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("CDPATH", None)
    if extra:
        env.update(extra)
    return env


def resolve_bd() -> str | None:
    """Resolve `bd` executable, preferring the pre-anchored BEADS_HOOK_BD env var."""
    anchored = os.environ.get("BEADS_HOOK_BD", "").strip()
    if anchored:
        if os.path.isabs(anchored):
            if os.path.isfile(anchored) and os.access(anchored, os.X_OK):
                return anchored
            return None
        found = shutil.which(anchored)
        if found:
            return found
    return shutil.which("bd")


def read_event_json() -> dict[str, Any]:
    """Read optional JSON hook payload from stdin without failing on empty/bad input."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def holds_store(beads_dir: Path) -> bool:
    """Return True if `beads_dir` holds Beads project files (matches bd 1.3.4)."""
    if not beads_dir.is_dir():
        return False
    for marker in ("metadata.json", "config.yaml", "dolt", "embeddeddolt", "redirect"):
        p = beads_dir / marker
        if marker in {"dolt", "embeddeddolt"}:
            if p.is_dir():
                return True
        elif marker == "redirect":
            if p.is_file():
                return True
        elif p.exists():
            return True
    try:
        for entry in beads_dir.iterdir():
            name = entry.name
            if name.endswith(".db") and name != "vc.db" and ".backup" not in name:
                return True
    except OSError:
        return False
    return False


def extract_workspace_dirs(event: dict[str, Any], agent: str) -> list[Path]:
    """Collect deduplicated candidate workspace directories from event/env/cwd."""
    raw_dirs: list[str] = []

    def add(val: Any) -> None:
        if isinstance(val, str) and val.strip():
            raw_dirs.append(val.strip())

    def add_list(val: Any) -> None:
        if isinstance(val, list):
            for item in val:
                add(item)

    if agent == "antigravity":
        add_list(event.get("workspacePaths"))
        add(event.get("cwd"))
    else:
        add_list(event.get("workspace_roots"))
        add_list(event.get("workspacePaths"))
        add(event.get("cwd"))
        tool_input = event.get("tool_input")
        if isinstance(tool_input, dict):
            add(tool_input.get("cwd"))
        add(os.environ.get("AUGMENT_PROJECT_DIR"))

    if not raw_dirs:
        add(os.getcwd())

    seen: set[str] = set()
    result: list[Path] = []
    for item in raw_dirs:
        abs_path = os.path.abspath(os.path.expanduser(item))
        if abs_path not in seen:
            seen.add(abs_path)
            result.append(Path(abs_path))
    return result


def discover_store(bd_path: str, candidate_dir: Path, env: dict[str, str]) -> tuple[Path, Path] | None:
    """Resolve the active Beads store for `candidate_dir` via `bd where`.

    Returns `(run_dir, resolved_beads_dir)` or None if no store applies.
    Handles linked git worktrees (e.g. Orca worktrees), subdirectories, and
    parent-directory discovery natively through `bd where`, and verifies
    `holds_store` to avoid non-project `.beads` directories like `~/.beads`.
    """
    if not candidate_dir.is_dir():
        return None

    timeout_s = _env_float("BEADS_HOOK_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT_S)
    try:
        res = subprocess.run(
            [bd_path, "where"],
            cwd=str(candidate_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if res.returncode == 0:
        first_line = ""
        for line in res.stdout.splitlines():
            stripped = line.strip()
            if stripped:
                first_line = stripped
                break
        if not first_line:
            return None
        beads_dir = Path(first_line)
        if not beads_dir.is_absolute():
            beads_dir = (candidate_dir / beads_dir).resolve()
        else:
            beads_dir = beads_dir.resolve()
        if holds_store(beads_dir):
            return (candidate_dir, beads_dir)
        return None

    # Only fall back to direct `.beads` check if an older/minimal `bd` binary
    # literally does not recognize the `where` subcommand.
    combined = f"{res.stdout}\n{res.stderr}".lower()
    if "unknown command" in combined and "where" in combined:
        direct = (candidate_dir / ".beads").resolve()
        if holds_store(direct):
            return (candidate_dir, direct)

    return None


def discover_unique_stores(
    bd_path: str, candidate_dirs: list[Path], env: dict[str, str]
) -> list[tuple[Path, Path]]:
    """Discover deduplicated `(run_dir, beads_dir)` pairs across workspace dirs."""
    seen_stores: set[Path] = set()
    stores: list[tuple[Path, Path]] = []
    for candidate in candidate_dirs:
        found = discover_store(bd_path, candidate, env)
        if found is None:
            continue
        run_dir, beads_dir = found
        if beads_dir in seen_stores:
            continue
        seen_stores.add(beads_dir)
        stores.append((run_dir, beads_dir))
    return stores


@contextlib.contextmanager
def lock_store(beads_dir: Path, timeout_s: float) -> Iterator[int]:
    """Acquire an exclusive kernel `flock` on `<beads_dir>/.beads-hook.lock`.

    Yields the open file descriptor so callers can pass `pass_fds=(lock_fd,)`
    to `bd` subprocesses; the kernel holds the lock until both the hook and any
    child `bd` process exit, even across `SIGKILL`.
    """
    lock_path = beads_dir / HOOK_LOCK_NAME
    if lock_path.is_symlink():
        try:
            lock_path.unlink()
        except OSError:
            pass

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(lock_path), flags, 0o600)
    locked = False
    try:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"timed out after {timeout_s:g}s waiting for {lock_path}") from exc
                time.sleep(0.05)
        yield fd
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def get_bd_config(
    bd_path: str, run_dir: Path, key: str, env: dict[str, str], pass_fds: tuple[int, ...] = ()
) -> str | None:
    """Read a `bd config get <key>` setting; return None if unset or unreadable."""
    timeout_s = _env_float("BEADS_HOOK_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT_S)
    try:
        res = subprocess.run(
            [bd_path, "config", "get", key],
            cwd=str(run_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            pass_fds=pass_fds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        val = line.strip()
        if not val:
            continue
        if "(not set" in val.lower():
            return None
        return val.lower()
    return None


def list_dolt_remotes(
    bd_path: str, run_dir: Path, env: dict[str, str], pass_fds: tuple[int, ...] = ()
) -> set[str]:
    """Return configured Dolt remote names from `bd dolt remote list`."""
    timeout_s = _env_float("BEADS_HOOK_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT_S)
    try:
        res = subprocess.run(
            [bd_path, "dolt", "remote", "list"],
            cwd=str(run_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            pass_fds=pass_fds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if res.returncode != 0:
        return set()

    remotes: set[str] = set()
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith("no remotes configured") or lower.startswith("note:") or lower.startswith("warning:"):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            name, target = parts[0], parts[1]
            if "://" in target or "/" in target or ":" in target:
                remotes.add(name)
    return remotes


def resolve_pull_remote(
    bd_path: str, run_dir: Path, env: dict[str, str], pass_fds: tuple[int, ...] = ()
) -> str | None:
    """Return remote name to pull from on SessionStart, or None if local-only/no remote."""
    if get_bd_config(bd_path, run_dir, "dolt.local-only", env, pass_fds=pass_fds) == "true":
        return None
    remotes = list_dolt_remotes(bd_path, run_dir, env, pass_fds=pass_fds)
    return "origin" if "origin" in remotes else None


def resolve_push_remote(
    bd_path: str, run_dir: Path, env: dict[str, str], pass_fds: tuple[int, ...] = ()
) -> str | None:
    """Return remote name to push to on sync, or None if pushing is disabled/no remote."""
    if get_bd_config(bd_path, run_dir, "dolt.local-only", env, pass_fds=pass_fds) == "true":
        return None
    if get_bd_config(bd_path, run_dir, "no-push", env, pass_fds=pass_fds) == "true":
        return None
    if get_bd_config(bd_path, run_dir, "dolt.auto-push", env, pass_fds=pass_fds) == "false":
        return None
    remotes = list_dolt_remotes(bd_path, run_dir, env, pass_fds=pass_fds)
    return "origin" if "origin" in remotes else None


def _emit_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Subcommand: prime (SessionStart)
# ---------------------------------------------------------------------------


def cmd_prime(agent: str) -> int:
    event = read_event_json()
    bd_path = resolve_bd()
    if not bd_path:
        if agent == "antigravity":
            _emit_json({})
        return 0

    env = clean_subprocess_env()
    dirs = extract_workspace_dirs(event, agent)
    stores = discover_unique_stores(bd_path, dirs, env)
    if not stores:
        if agent == "antigravity":
            _emit_json({})
        return 0

    pull_enabled = os.environ.get("BEADS_HOOK_NO_PULL", "").strip() != "1"
    pull_timeout = _env_float("BEADS_HOOK_PULL_TIMEOUT", DEFAULT_PULL_TIMEOUT_S)
    prime_timeout = _env_float("BEADS_HOOK_PRIME_TIMEOUT", DEFAULT_PRIME_TIMEOUT_S)

    chunks: list[str] = []
    for run_dir, beads_dir in stores:
        if pull_enabled:
            remote = resolve_pull_remote(bd_path, run_dir, env)
            if remote:
                try:
                    with lock_store(beads_dir, timeout_s=min(2.0, pull_timeout)) as lock_fd:
                        pull_res = subprocess.run(
                            [bd_path, "dolt", "pull", "--remote", remote],
                            cwd=str(run_dir),
                            env=env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            text=True,
                            timeout=pull_timeout,
                            pass_fds=(lock_fd,),
                            check=False,
                        )
                        if pull_res.returncode != 0:
                            detail = (pull_res.stderr or pull_res.stdout).strip()
                            chunks.append(
                                f"beads: 'bd dolt pull --remote {remote}' failed at session start "
                                f"(context may be stale): {detail}"
                            )
                except LockTimeoutError:
                    chunks.append(
                        f"beads: skipped 'bd dolt pull --remote {remote}' at session start "
                        "(store lock busy; context may be stale)."
                    )
                except subprocess.TimeoutExpired:
                    chunks.append(
                        f"beads: 'bd dolt pull --remote {remote}' timed out after {pull_timeout:g}s "
                        "(context may be stale)."
                    )
                except OSError as exc:
                    chunks.append(f"beads: 'bd dolt pull' could not run at session start: {exc}")

        try:
            prime_res = subprocess.run(
                [bd_path, "prime"],
                cwd=str(run_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=prime_timeout,
                check=False,
            )
            if prime_res.returncode == 0:
                out = prime_res.stdout.rstrip("\n")
                if out:
                    chunks.append(out)
            else:
                detail = (prime_res.stdout + prime_res.stderr).strip()
                chunks.append(
                    "beads: 'bd prime' failed, so this session has no Beads context "
                    f"and durable task tracking may be unavailable. Output: {detail}"
                )
        except subprocess.TimeoutExpired:
            chunks.append(
                f"beads: 'bd prime' timed out after {prime_timeout:g}s, so this session "
                "has no Beads context and durable task tracking may be unavailable."
            )
        except OSError as exc:
            chunks.append(
                "beads: 'bd prime' failed, so this session has no Beads context "
                f"and durable task tracking may be unavailable. Output: {exc}"
            )

    combined = "\n\n".join(chunks).strip()
    if agent == "antigravity":
        if combined:
            _emit_json({"injectSteps": [{"systemMessage": {"systemMessage": combined}}]})
        else:
            _emit_json({})
    else:
        if combined:
            sys.stdout.write(combined + "\n")
            sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Subcommand: guard (PreToolUse — block interactive `bd edit`)
# ---------------------------------------------------------------------------


def _split_shell_segments(command: str) -> list[list[str]]:
    """Split a shell command string into simple command token lists.

    Respects single quotes, double quotes, backslash escapes, `#` comments,
    and command separators (`;`, `&&`, `||`, `|`, `&`, `\\n`, `(`, `)`).
    Does not evaluate expansions or attempt full shell simulation.
    """
    segments: list[list[str]] = []
    cur_seg: list[str] = []
    cur_tok: list[str] = []
    in_token = False
    quote: str | None = None
    i = 0
    n = len(command)

    def flush_tok() -> None:
        nonlocal in_token
        if in_token:
            cur_seg.append("".join(cur_tok))
            cur_tok.clear()
            in_token = False

    def flush_seg() -> None:
        flush_tok()
        if cur_seg:
            segments.append(list(cur_seg))
            cur_seg.clear()

    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                cur_tok.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n and command[i + 1] in {'"', "\\", "$", "`", "\n"}:
                if command[i + 1] != "\n":
                    cur_tok.append(command[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            else:
                cur_tok.append(ch)
            i += 1
            continue

        # Outside quotes
        if ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                cur_tok.append(command[i + 1])
                in_token = True
            i += 2
            continue
        if ch in {"'", '"'}:
            quote = ch
            in_token = True
            i += 1
            continue
        if ch == "#" and not in_token:
            # Comment to end of line
            while i < n and command[i] != "\n":
                i += 1
            continue
        if ch == "\n":
            flush_seg()
            i += 1
            continue
        if ch in {" ", "\t", "\r"}:
            flush_tok()
            i += 1
            continue
        if ch in {";", "(", ")"}:
            flush_seg()
            i += 1
            continue
        if ch in {"&", "|"}:
            # Preserve redirections like `2>&1`, `&>`, `>|`, `<&` inside tokens
            prev_ch = command[i - 1] if i > 0 else ""
            next_ch = command[i + 1] if i + 1 < n else ""
            if (ch == "&" and (prev_ch in {">", "<"} or next_ch == ">")) or (ch == "|" and prev_ch == ">"):
                cur_tok.append(ch)
                in_token = True
                i += 1
                continue
            flush_seg()
            if next_ch == ch:
                i += 2
            else:
                i += 1
            continue

        cur_tok.append(ch)
        in_token = True
        i += 1

    flush_seg()
    return segments


def _segment_invokes_bd_edit(tokens: list[str], depth: int = 0) -> bool:
    """Return True if a single shell command segment invokes interactive `bd edit`."""
    idx = 0
    n = len(tokens)

    # Strip leading redirections, env assignments, and common wrapper commands.
    while idx < n:
        tok = tokens[idx]
        if REDIR_OP_RE.match(tok):
            idx += 2
            continue
        if REDIR_ATTACHED_RE.match(tok):
            idx += 1
            continue
        if ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue

        base = os.path.basename(tok)
        if base in WRAPPER_COMMANDS:
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
                    idx += 1
                    continue
                break
            continue

        if base in SHELL_COMMANDS and depth < 2:
            # Check for `bash -c "bd edit ..."` (including combined flags like `-lc`)
            j = idx + 1
            while j < n:
                arg = tokens[j]
                if arg == "--":
                    break
                if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
                    if j + 1 < n:
                        return contains_interactive_bd_edit(tokens[j + 1], depth=depth + 1)
                    return False
                if arg in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
                    j += 2
                    continue
                if arg.startswith("-") or arg.startswith("+"):
                    j += 1
                    continue
                break
            return False

        break

    if idx >= n:
        return False

    if os.path.basename(tokens[idx]) != "bd":
        return False

    # Walk past `bd` global flags to find the subcommand.
    idx += 1
    while idx < n:
        tok = tokens[idx]
        if tok in {"-h", "--help"}:
            return False
        if tok == "--":
            idx += 1
            break
        if tok in BD_VALUE_FLAGS:
            idx += 2
            continue
        if tok in BD_BOOL_FLAGS:
            idx += 1
            continue
        if tok.startswith("--") and "=" in tok:
            idx += 1
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        break

    if idx >= n or tokens[idx] != "edit":
        return False

    # Allow `bd edit --help` / `bd edit -h`.
    rest = tokens[idx + 1 :]
    for tok in rest:
        if tok == "--":
            break
        if tok in {"-h", "--help"}:
            return False
    return True


def contains_interactive_bd_edit(command: str, depth: int = 0) -> bool:
    if not command or "bd" not in command or "edit" not in command:
        return False
    for seg in _split_shell_segments(command):
        if _segment_invokes_bd_edit(seg, depth=depth):
            return True
    return False


def _extract_tool_and_command(event: dict[str, Any], agent: str) -> tuple[str, str]:
    if agent == "antigravity":
        tool_call = event.get("toolCall")
        if isinstance(tool_call, dict):
            tool_name = str(tool_call.get("name") or "")
            args = tool_call.get("args")
            if isinstance(args, dict):
                cmd = args.get("CommandLine") or args.get("command") or ""
                return tool_name, str(cmd) if isinstance(cmd, str) else ""
            return tool_name, ""
    tool_name = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command") or tool_input.get("CommandLine") or ""
        return tool_name, str(cmd) if isinstance(cmd, str) else ""
    return tool_name, ""


def cmd_guard(agent: str) -> int:
    event = read_event_json()
    tool_name, command = _extract_tool_and_command(event, agent)

    if tool_name and tool_name not in SHELL_TOOL_NAMES:
        return 0

    if contains_interactive_bd_edit(command):
        if agent == "antigravity":
            _emit_json({"decision": "deny", "reason": BD_EDIT_BLOCK_MESSAGE})
            return 0
        sys.stderr.write(BD_EDIT_BLOCK_MESSAGE + "\n")
        sys.stderr.flush()
        return 2

    # Emit nothing on allow: Antigravity treats `{"decision": "allow"}` as an
    # explicit auto-approval override, whereas empty stdout preserves normal
    # permission checks.
    return 0


# ---------------------------------------------------------------------------
# Subcommand: sync (Stop / SessionEnd)
# ---------------------------------------------------------------------------


def _sync_one_store(
    bd_path: str,
    run_dir: Path,
    beads_dir: Path,
    agent: str,
    event_name: str,
    env: dict[str, str],
    lock_timeout: float,
    commit_timeout: float,
    push_timeout: float,
) -> list[str]:
    warnings: list[str] = []
    try:
        lock_ctx = lock_store(beads_dir, timeout_s=lock_timeout)
    except Exception as exc:
        return [f"beads: could not lock {beads_dir}: {exc}"]

    try:
        with lock_ctx as lock_fd:
            commit_msg = f"bd: sync via {agent} {event_name}"
            try:
                commit_res = subprocess.run(
                    [bd_path, "dolt", "commit", "-m", commit_msg],
                    cwd=str(run_dir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=commit_timeout,
                    pass_fds=(lock_fd,),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                warnings.append(
                    f"beads: 'bd dolt commit' timed out after {commit_timeout:g}s in {run_dir}; "
                    "run 'bd dolt commit' manually."
                )
                return warnings
            except OSError as exc:
                warnings.append(f"beads: 'bd dolt commit' failed to start in {run_dir}: {exc}")
                return warnings

            combined_commit = (commit_res.stdout + "\n" + commit_res.stderr).strip()
            if commit_res.returncode != 0 and "nothing to commit" not in combined_commit.lower():
                warnings.append(
                    f"beads: 'bd dolt commit' failed in {run_dir} (exit {commit_res.returncode}): "
                    f"{combined_commit}"
                )
                return warnings

            remote = resolve_push_remote(bd_path, run_dir, env, pass_fds=(lock_fd,))
            if not remote:
                return warnings

            push_env = dict(env)
            push_env["BD_NO_REMOTE_ADOPT"] = "1"
            try:
                push_res = subprocess.run(
                    [bd_path, "dolt", "push", "--remote", remote, "--no-adopt"],
                    cwd=str(run_dir),
                    env=push_env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=push_timeout,
                    pass_fds=(lock_fd,),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                warnings.append(
                    f"beads: 'bd dolt push --remote {remote}' timed out after {push_timeout:g}s "
                    f"in {run_dir}; commits are local and NOT on the remote."
                )
                return warnings
            except OSError as exc:
                warnings.append(f"beads: 'bd dolt push' failed to start in {run_dir}: {exc}")
                return warnings

            if push_res.returncode != 0:
                detail = (push_res.stdout + "\n" + push_res.stderr).strip()
                warnings.append(
                    f"beads: 'bd dolt push --remote {remote}' failed in {run_dir} "
                    f"(exit {push_res.returncode}); commits are local and NOT on the remote: {detail}"
                )
    except LockTimeoutError as exc:
        warnings.append(
            f"beads: {exc}; run 'bd dolt commit' (and 'bd dolt push' if configured) manually."
        )
    except OSError as exc:
        warnings.append(f"beads: could not lock {beads_dir}: {exc}")

    return warnings


def cmd_sync(agent: str) -> int:
    event = read_event_json()
    bd_path = resolve_bd()
    warnings: list[str] = []

    if bd_path:
        env = clean_subprocess_env()
        dirs = extract_workspace_dirs(event, agent)
        stores = discover_unique_stores(bd_path, dirs, env)
        event_name = str(
            event.get("hookEventName") or event.get("hook_event_name") or "Stop"
        ).strip() or "Stop"

        lock_timeout = _env_float("BEADS_HOOK_LOCK_TIMEOUT", DEFAULT_LOCK_TIMEOUT_S)
        commit_timeout = _env_float("BEADS_HOOK_COMMIT_TIMEOUT", DEFAULT_COMMIT_TIMEOUT_S)
        push_timeout = _env_float("BEADS_HOOK_PUSH_TIMEOUT", DEFAULT_PUSH_TIMEOUT_S)

        for run_dir, beads_dir in stores:
            warnings.extend(
                _sync_one_store(
                    bd_path=bd_path,
                    run_dir=run_dir,
                    beads_dir=beads_dir,
                    agent=agent,
                    event_name=event_name,
                    env=env,
                    lock_timeout=lock_timeout,
                    commit_timeout=commit_timeout,
                    push_timeout=push_timeout,
                )
            )

    if warnings:
        sys.stderr.write("\n".join(warnings) + "\n")
        sys.stderr.flush()

    if agent == "antigravity":
        _emit_json({"decision": ""})
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="beads_hook.py")
    parser.add_argument("action", choices=("prime", "guard", "sync"))
    parser.add_argument(
        "--agent",
        choices=("auggie", "antigravity"),
        default="auggie",
        help="Target host agent contract (default: auggie)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.action == "prime":
        return cmd_prime(args.agent)
    if args.action == "guard":
        return cmd_guard(args.agent)
    if args.action == "sync":
        return cmd_sync(args.agent)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # Defensive: hooks must not wedge the session.
        sys.stderr.write(f"beads: unexpected hook error: {exc}\n")
        sys.stderr.flush()
        raise SystemExit(0)
