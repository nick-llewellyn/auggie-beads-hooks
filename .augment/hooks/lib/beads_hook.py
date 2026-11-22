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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from shell_tokens import Token, split_shell_segments

HOOK_LOCK_NAME = ".beads-hook.lock"

# Whole-hook budgets stay safely inside the installed host timeouts
# (15s for SessionStart, 60s for Stop/SessionEnd) so output is always flushed.
DEFAULT_PRIME_TOTAL_TIMEOUT_S = 13.5
DEFAULT_SYNC_TOTAL_TIMEOUT_S = 55.0
DEFAULT_PRIME_RESERVE_S = 6.0

DEFAULT_LOCK_TIMEOUT_S = 25.0
DEFAULT_PULL_TIMEOUT_S = 5.0
DEFAULT_PRIME_TIMEOUT_S = 12.0
DEFAULT_COMMIT_TIMEOUT_S = 25.0
DEFAULT_PUSH_TIMEOUT_S = 30.0
DEFAULT_PROBE_TIMEOUT_S = 4.0

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
CONTROL_KEYWORDS = {"if", "then", "elif", "else", "while", "until", "do", "{", "!", "coproc"}

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")

BD_EDIT_BLOCK_MESSAGE = (
    "beads: 'bd edit' opens an interactive $EDITOR and hangs agent sessions. "
    "Use non-interactive 'bd update <id> --title/--description/--notes/--design/--acceptance' instead."
)


class LockTimeoutError(Exception):
    """Raised when the per-store flock cannot be acquired within the timeout."""


@dataclass(frozen=True)
class StoreTarget:
    run_dir: Path
    beads_dir: Path
    db_id: str


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
        return val if val >= 0 else default
    except ValueError:
        return default


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _bounded_timeout(op_timeout: float, deadline: float) -> float:
    rem = _remaining(deadline)
    if rem <= 0.0:
        return 0.0
    return min(op_timeout, rem)


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


def _read_dolt_database_from_metadata(beads_dir: Path) -> str | None:
    meta_path = beads_dir / "metadata.json"
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("dolt_database", "database"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _find_nearest_local_beads_dir(candidate_dir: Path) -> Path | None:
    """Walk up from `candidate_dir` (stopping at git root) for a local `.beads/`."""
    cur = candidate_dir.resolve()
    while True:
        cand = cur / ".beads"
        if cand.is_dir():
            return cand
        if (cur / ".git").exists() or cur.parent == cur:
            return None
        cur = cur.parent


def _resolve_db_identity(candidate_dir: Path, beads_dir: Path, where_stdout: str) -> str:
    """Determine the effective database identity for deduplicating roots.

    Beads preserves a redirect source's `dolt_database` even when two roots
    redirect to the same `.beads` directory. Combine the `bd where` database
    line with any redirect-source `metadata.json` `dolt_database` override so
    distinct databases sharing a `.beads` directory are never collapsed.
    """
    where_db = ""
    for line in where_stdout.splitlines()[1:]:
        stripped = line.strip()
        if stripped.lower().startswith("database:"):
            where_db = stripped.split(":", 1)[1].strip()
            break

    local_beads = _find_nearest_local_beads_dir(candidate_dir)
    source_db = ""
    if local_beads is not None and local_beads.resolve() != beads_dir:
        source_db = _read_dolt_database_from_metadata(local_beads) or ""

    target_db = _read_dolt_database_from_metadata(beads_dir) or ""
    effective_db = source_db or where_db or target_db or str(beads_dir)
    return effective_db


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


def discover_store(
    bd_path: str, candidate_dir: Path, env: dict[str, str], deadline: float
) -> tuple[StoreTarget | None, str | None]:
    """Resolve the active Beads store for `candidate_dir` via `bd where`.

    Returns `(StoreTarget | None, diagnostic_warning | None)`.
    Normal non-Beads directories return `(None, None)`. Timeouts, launch errors,
    and unexpected `bd where` failures return `(None, warning)` so callers can
    surface them instead of silently dropping an existing store.
    """
    if not candidate_dir.is_dir():
        return None, None

    op_timeout = _bounded_timeout(
        _env_float("BEADS_HOOK_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT_S), deadline
    )
    if op_timeout <= 0.0:
        return None, f"beads: hook time budget exhausted before checking {candidate_dir}"

    try:
        res = subprocess.run(
            [bd_path, "where", "--json=false"],
            cwd=str(candidate_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=op_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            f"beads: 'bd where' timed out after {op_timeout:g}s in {candidate_dir}; skipping root.",
        )
    except OSError as exc:
        return None, f"beads: 'bd where' failed to start in {candidate_dir}: {exc}"

    if res.returncode == 0:
        first_line = ""
        for line in res.stdout.splitlines():
            stripped = line.strip()
            if stripped:
                first_line = stripped
                break
        if not first_line:
            return None, None
        beads_dir = Path(first_line)
        if not beads_dir.is_absolute():
            beads_dir = (candidate_dir / beads_dir).resolve()
        else:
            beads_dir = beads_dir.resolve()
        if holds_store(beads_dir):
            db_id = _resolve_db_identity(candidate_dir, beads_dir, res.stdout)
            return StoreTarget(run_dir=candidate_dir, beads_dir=beads_dir, db_id=db_id), None
        return None, None

    combined = (res.stdout + "\n" + res.stderr).strip()
    lower = combined.lower()
    if "no active beads workspace" in lower or "no beads database found" in lower:
        return None, None

    # Only fall back to direct `.beads` check if an older/minimal `bd` binary
    # literally does not recognize the `where` subcommand.
    if "unknown command" in lower and "where" in lower:
        direct = (candidate_dir / ".beads").resolve()
        if holds_store(direct):
            db_id = _read_dolt_database_from_metadata(direct) or str(direct)
            return StoreTarget(run_dir=candidate_dir, beads_dir=direct, db_id=db_id), None
        return None, None

    return (
        None,
        f"beads: 'bd where' failed in {candidate_dir} (exit {res.returncode}): {combined}",
    )


def discover_unique_stores(
    bd_path: str, candidate_dirs: list[Path], env: dict[str, str], deadline: float
) -> tuple[list[StoreTarget], list[str]]:
    """Discover deduplicated stores across workspace dirs, returning `(stores, warnings)`."""
    seen_keys: set[tuple[Path, str]] = set()
    stores: list[StoreTarget] = []
    warnings: list[str] = []
    for candidate in candidate_dirs:
        if _remaining(deadline) <= 0.0:
            warnings.append(
                f"beads: hook time budget exhausted before checking {candidate}; skipping remaining roots."
            )
            break
        target, warning = discover_store(bd_path, candidate, env, deadline)
        if warning:
            warnings.append(warning)
        if target is None:
            continue
        dedup_key = (target.beads_dir, target.db_id)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        stores.append(target)
    return stores, warnings


@contextlib.contextmanager
def lock_store(beads_dir: Path, timeout_s: float) -> Iterator[int]:
    """Acquire an exclusive kernel `flock` on `<beads_dir>/.beads-hook.lock`.

    Fails closed on symlinks (`O_NOFOLLOW`) without ever unlinking the lock path,
    preventing TOCTOU races where a concurrent hook could delete another hook's
    live lock inode. Yields the open file descriptor so callers can pass
    `pass_fds=(lock_fd,)` to `bd` subprocesses.
    """
    lock_path = beads_dir / HOOK_LOCK_NAME
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(
                errno.ELOOP,
                f"refusing to follow symlinked lock path {lock_path}; remove it manually if stale",
            ) from exc
        raise

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
                    raise LockTimeoutError(
                        f"timed out after {timeout_s:g}s waiting for {lock_path}"
                    ) from exc
                time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))
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
    bd_path: str,
    run_dir: Path,
    key: str,
    env: dict[str, str],
    deadline: float,
    pass_fds: tuple[int, ...] = (),
) -> str | None:
    """Read a `bd config get <key>` setting; return None if unset or unreadable."""
    op_timeout = _bounded_timeout(
        _env_float("BEADS_HOOK_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT_S), deadline
    )
    if op_timeout <= 0.0:
        return None
    try:
        res = subprocess.run(
            [bd_path, "config", "get", key, "--json=false"],
            cwd=str(run_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=op_timeout,
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
    bd_path: str,
    run_dir: Path,
    env: dict[str, str],
    deadline: float,
    pass_fds: tuple[int, ...] = (),
) -> tuple[set[str] | None, str | None]:
    """Return `(remotes, error)` from `bd dolt remote list --json=false`.

    Distinguishes a clean empty remote listing (`(set(), None)`) from probe
    timeouts, launch failures, or non-zero exits (`(None, warning)`).
    """
    op_timeout = _bounded_timeout(
        _env_float("BEADS_HOOK_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT_S), deadline
    )
    if op_timeout <= 0.0:
        return None, f"beads: hook time budget exhausted before checking Dolt remotes in {run_dir}."
    try:
        res = subprocess.run(
            [bd_path, "dolt", "remote", "list", "--json=false"],
            cwd=str(run_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=op_timeout,
            pass_fds=pass_fds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            f"beads: 'bd dolt remote list' timed out after {op_timeout:g}s in {run_dir}.",
        )
    except OSError as exc:
        return None, f"beads: 'bd dolt remote list' failed to start in {run_dir}: {exc}"
    if res.returncode != 0:
        detail = (res.stdout + "\n" + res.stderr).strip()
        return (
            None,
            f"beads: 'bd dolt remote list' failed in {run_dir} (exit {res.returncode}): {detail}",
        )

    remotes: set[str] = set()
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if (
            lower.startswith("no remotes configured")
            or lower.startswith("note:")
            or lower.startswith("warning:")
        ):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            name, target = parts[0], parts[1]
            if "://" in target or "/" in target or ":" in target:
                remotes.add(name)
    return remotes, None


def resolve_pull_remote(
    bd_path: str,
    run_dir: Path,
    env: dict[str, str],
    deadline: float,
    pass_fds: tuple[int, ...] = (),
) -> tuple[str | None, str | None]:
    """Return `(remote, error)` for SessionStart pull."""
    if get_bd_config(bd_path, run_dir, "dolt.local-only", env, deadline, pass_fds=pass_fds) == "true":
        return None, None
    remotes, err = list_dolt_remotes(bd_path, run_dir, env, deadline, pass_fds=pass_fds)
    if err is not None:
        return None, f"{err} Skipped SessionStart pull (context may be stale)."
    assert remotes is not None
    return ("origin" if "origin" in remotes else None), None


def resolve_push_remote(
    bd_path: str,
    run_dir: Path,
    env: dict[str, str],
    deadline: float,
    pass_fds: tuple[int, ...] = (),
) -> tuple[str | None, str | None]:
    """Return `(remote, error)` for Stop/SessionEnd push."""
    if get_bd_config(bd_path, run_dir, "dolt.local-only", env, deadline, pass_fds=pass_fds) == "true":
        return None, None
    if get_bd_config(bd_path, run_dir, "no-push", env, deadline, pass_fds=pass_fds) == "true":
        return None, None
    if get_bd_config(bd_path, run_dir, "dolt.auto-push", env, deadline, pass_fds=pass_fds) == "false":
        return None, None
    remotes, err = list_dolt_remotes(bd_path, run_dir, env, deadline, pass_fds=pass_fds)
    if err is not None:
        return None, f"{err} Commits are local and remote push status could not be verified."
    assert remotes is not None
    return ("origin" if "origin" in remotes else None), None


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

    total_budget = _env_float("BEADS_HOOK_TOTAL_TIMEOUT", DEFAULT_PRIME_TOTAL_TIMEOUT_S)
    deadline = time.monotonic() + total_budget
    prime_reserve = min(DEFAULT_PRIME_RESERVE_S, total_budget * 0.5)

    env = clean_subprocess_env()
    dirs = extract_workspace_dirs(event, agent)
    stores, discovery_warnings = discover_unique_stores(bd_path, dirs, env, deadline)

    pull_enabled = os.environ.get("BEADS_HOOK_NO_PULL", "").strip() != "1"
    pull_cap = _env_float("BEADS_HOOK_PULL_TIMEOUT", DEFAULT_PULL_TIMEOUT_S)
    prime_cap = _env_float("BEADS_HOOK_PRIME_TIMEOUT", DEFAULT_PRIME_TIMEOUT_S)

    chunks: list[str] = list(discovery_warnings)
    for idx, store in enumerate(stores):
        if _remaining(deadline) <= 0.1:
            skipped = ", ".join(str(s.run_dir) for s in stores[idx:])
            chunks.append(
                f"beads: SessionStart time budget exhausted before priming remaining store(s): {skipped}"
            )
            break

        if pull_enabled:
            # Reserve enough budget for `bd prime` across remaining stores.
            stores_left = len(stores) - idx
            pull_budget = min(pull_cap, max(0.0, _remaining(deadline) - prime_reserve))
            if pull_budget > 0.1 and stores_left > 0:
                pull_deadline = min(deadline - prime_reserve, time.monotonic() + pull_budget)
                remote, pull_probe_err = resolve_pull_remote(bd_path, store.run_dir, env, pull_deadline)
                if pull_probe_err:
                    chunks.append(pull_probe_err)
                elif remote:
                    actual_pull_timeout = _bounded_timeout(pull_cap, pull_deadline)
                    if actual_pull_timeout <= 0.05:
                        chunks.append(
                            f"beads: skipped 'bd dolt pull --remote {remote}' in {store.run_dir} "
                            "to preserve SessionStart time for 'bd prime' (context may be stale)."
                        )
                    else:
                        try:
                            lock_wait = min(2.0, actual_pull_timeout * 0.5)
                            with lock_store(store.beads_dir, timeout_s=lock_wait) as lock_fd:
                                run_timeout = _bounded_timeout(actual_pull_timeout, pull_deadline)
                                if run_timeout <= 0.05:
                                    chunks.append(
                                        f"beads: skipped 'bd dolt pull --remote {remote}' in {store.run_dir} "
                                        "(insufficient time remaining; context may be stale)."
                                    )
                                else:
                                    pull_res = subprocess.run(
                                        [bd_path, "dolt", "pull", "--remote", remote],
                                        cwd=str(store.run_dir),
                                        env=env,
                                        stdin=subprocess.DEVNULL,
                                        capture_output=True,
                                        text=True,
                                        timeout=run_timeout,
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
                                f"beads: 'bd dolt pull --remote {remote}' timed out after {run_timeout:g}s "
                                "(context may be stale)."
                            )
                        except OSError as exc:
                            chunks.append(f"beads: 'bd dolt pull' could not run at session start: {exc}")

        prime_timeout = _bounded_timeout(prime_cap, deadline)
        if prime_timeout <= 0.05:
            chunks.append(
                f"beads: SessionStart time budget exhausted before 'bd prime' in {store.run_dir}."
            )
            break

        try:
            prime_res = subprocess.run(
                [bd_path, "prime"],
                cwd=str(store.run_dir),
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


def _segment_invokes_bd_edit(tokens: list[Token], depth: int = 0) -> bool:
    """Return True if a single shell command segment invokes interactive `bd edit`."""
    idx = 0
    n = len(tokens)

    # Strip leading shell control keywords, redirections, env assignments, and wrappers.
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
        if base in WRAPPER_COMMANDS:
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
                    idx += 1
                    continue
                break
            continue

        if base in SHELL_COMMANDS and depth < 2:
            # Check for `bash -c "bd edit ..."` (including combined flags like `-lc`)
            j = idx + 1
            while j < n:
                arg = tokens[j]
                if arg.is_redir:
                    j += 2
                    continue
                if arg.text == "--":
                    break
                if arg.text == "-c" or (
                    arg.text.startswith("-") and not arg.text.startswith("--") and "c" in arg.text[1:]
                ):
                    if j + 1 < n:
                        return contains_interactive_bd_edit(tokens[j + 1].text, depth=depth + 1)
                    return False
                if arg.text in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
                    j += 2
                    continue
                if arg.text.startswith("-") or arg.text.startswith("+"):
                    j += 1
                    continue
                break
            return False

        break

    if idx >= n:
        return False

    if os.path.basename(tokens[idx].text) != "bd":
        return False

    # Walk past `bd` global flags (and interspersed redirections) to find the subcommand.
    idx += 1
    while idx < n:
        tok = tokens[idx]
        if tok.is_redir:
            idx += 2
            continue
        if tok.text in {"-h", "--help"}:
            return False
        if tok.text == "--":
            idx += 1
            break
        if tok.text in BD_VALUE_FLAGS:
            idx += 2
            continue
        if tok.text in BD_BOOL_FLAGS:
            idx += 1
            continue
        if tok.text.startswith("--") and "=" in tok.text:
            idx += 1
            continue
        if tok.text.startswith("-"):
            idx += 1
            continue
        break

    if idx >= n or tokens[idx].text != "edit":
        return False

    # Allow `bd edit --help` / `bd edit -h`.
    rest = tokens[idx + 1 :]
    k = 0
    while k < len(rest):
        tok = rest[k]
        if tok.is_redir:
            k += 2
            continue
        if tok.text == "--":
            break
        if tok.text in {"-h", "--help"}:
            return False
        k += 1
    return True


def contains_interactive_bd_edit(command: str, depth: int = 0) -> bool:
    if not command or not command.strip():
        return False
    for seg in split_shell_segments(command):
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
    store: StoreTarget,
    agent: str,
    event_name: str,
    env: dict[str, str],
    deadline: float,
    lock_cap: float,
    commit_cap: float,
    push_cap: float,
) -> list[str]:
    warnings: list[str] = []
    lock_timeout = _bounded_timeout(lock_cap, deadline)
    if lock_timeout <= 0.05:
        return [
            f"beads: sync time budget exhausted before locking {store.beads_dir}; "
            "run 'bd dolt commit' (and 'bd dolt push' if configured) manually."
        ]

    try:
        with lock_store(store.beads_dir, timeout_s=lock_timeout) as lock_fd:
            commit_timeout = _bounded_timeout(commit_cap, deadline)
            if commit_timeout <= 0.05:
                warnings.append(
                    f"beads: sync time budget exhausted before 'bd dolt commit' in {store.run_dir}; "
                    "run 'bd dolt commit' manually."
                )
                return warnings

            commit_msg = f"bd: sync via {agent} {event_name}"
            try:
                commit_res = subprocess.run(
                    [bd_path, "dolt", "commit", "-m", commit_msg],
                    cwd=str(store.run_dir),
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
                    f"beads: 'bd dolt commit' timed out after {commit_timeout:g}s in {store.run_dir}; "
                    "run 'bd dolt commit' manually."
                )
                return warnings
            except OSError as exc:
                warnings.append(f"beads: 'bd dolt commit' failed to start in {store.run_dir}: {exc}")
                return warnings

            combined_commit = (commit_res.stdout + "\n" + commit_res.stderr).strip()
            if commit_res.returncode != 0 and "nothing to commit" not in combined_commit.lower():
                warnings.append(
                    f"beads: 'bd dolt commit' failed in {store.run_dir} (exit {commit_res.returncode}): "
                    f"{combined_commit}"
                )
                return warnings

            remote, push_probe_err = resolve_push_remote(
                bd_path, store.run_dir, env, deadline, pass_fds=(lock_fd,)
            )
            if push_probe_err:
                warnings.append(push_probe_err)
                return warnings
            if not remote:
                return warnings

            push_timeout = _bounded_timeout(push_cap, deadline)
            if push_timeout <= 0.05:
                warnings.append(
                    f"beads: sync time budget exhausted before 'bd dolt push --remote {remote}' "
                    f"in {store.run_dir}; commits are local and NOT on the remote."
                )
                return warnings

            push_env = dict(env)
            push_env["BD_NO_REMOTE_ADOPT"] = "1"
            try:
                push_res = subprocess.run(
                    [bd_path, "dolt", "push", "--remote", remote, "--no-adopt"],
                    cwd=str(store.run_dir),
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
                    f"in {store.run_dir}; commits are local and NOT on the remote."
                )
                return warnings
            except OSError as exc:
                warnings.append(f"beads: 'bd dolt push' failed to start in {store.run_dir}: {exc}")
                return warnings

            if push_res.returncode != 0:
                detail = (push_res.stdout + "\n" + push_res.stderr).strip()
                warnings.append(
                    f"beads: 'bd dolt push --remote {remote}' failed in {store.run_dir} "
                    f"(exit {push_res.returncode}); commits are local and NOT on the remote: {detail}"
                )
    except LockTimeoutError as exc:
        warnings.append(
            f"beads: {exc}; run 'bd dolt commit' (and 'bd dolt push' if configured) manually."
        )
    except OSError as exc:
        warnings.append(f"beads: could not lock {store.beads_dir}: {exc}")

    return warnings


def cmd_sync(agent: str) -> int:
    event = read_event_json()
    bd_path = resolve_bd()
    warnings: list[str] = []

    event_name = str(
        event.get("hookEventName") or event.get("hook_event_name") or "Stop"
    ).strip() or "Stop"

    # Defense-in-depth: if legacy PostToolUse wiring invokes this script, do not
    # run full commits/pushes on every tool call.
    if event_name == "PostToolUse":
        sys.stderr.write(
            "beads: beads-sync.sh was invoked on legacy PostToolUse; "
            "run 'install.sh --wire' to migrate to Stop/SessionEnd.\n"
        )
        sys.stderr.flush()
        if agent == "antigravity":
            _emit_json({})
        return 0

    if bd_path:
        total_budget = _env_float("BEADS_HOOK_TOTAL_TIMEOUT", DEFAULT_SYNC_TOTAL_TIMEOUT_S)
        deadline = time.monotonic() + total_budget
        env = clean_subprocess_env()
        dirs = extract_workspace_dirs(event, agent)
        stores, discovery_warnings = discover_unique_stores(bd_path, dirs, env, deadline)
        warnings.extend(discovery_warnings)

        lock_cap = _env_float("BEADS_HOOK_LOCK_TIMEOUT", DEFAULT_LOCK_TIMEOUT_S)
        commit_cap = _env_float("BEADS_HOOK_COMMIT_TIMEOUT", DEFAULT_COMMIT_TIMEOUT_S)
        push_cap = _env_float("BEADS_HOOK_PUSH_TIMEOUT", DEFAULT_PUSH_TIMEOUT_S)

        for idx, store in enumerate(stores):
            if _remaining(deadline) <= 0.1:
                skipped = ", ".join(str(s.run_dir) for s in stores[idx:])
                warnings.append(
                    f"beads: sync time budget exhausted before remaining store(s): {skipped}"
                )
                break
            warnings.extend(
                _sync_one_store(
                    bd_path=bd_path,
                    store=store,
                    agent=agent,
                    event_name=event_name,
                    env=env,
                    deadline=deadline,
                    lock_cap=lock_cap,
                    commit_cap=commit_cap,
                    push_cap=push_cap,
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
