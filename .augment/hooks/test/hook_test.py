#!/usr/bin/env python3
"""Comprehensive test suite for Beads hooks (`beads_hook.py`, shims, `install.sh`).

Runs in a few seconds with stubbed `bd` in scratch directories — never touches
any real bead store or user configuration.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_DIR = REPO_ROOT / ".augment" / "hooks"
PRIME_SH = HOOKS_DIR / "beads-prime.sh"
SYNC_SH = HOOKS_DIR / "beads-sync.sh"
GUARD_SH = HOOKS_DIR / "beads-guard.sh"
INSTALL_SH = REPO_ROOT / "install.sh"
BASH_BIN = os.environ.get("BASH_UNDER_TEST", "/bin/bash")


def make_bd_stub(bin_dir: Path, log_file: Path) -> Path:
    """Create a configurable stub `bd` executable in `bin_dir`."""
    stub = bin_dir / "bd"
    stub.write_text(
        f"""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path

log_path = Path(os.environ.get("BD_LOG", {str(log_file)!r}))
args = sys.argv[1:]
cwd = os.getcwd()
with log_path.open("a", encoding="utf-8") as f:
    f.write(f"{{cwd}}|{{' '.join(args)}}\\n")

if not args:
    sys.exit(0)

cmd = args[0]

def find_store(start: Path):
    override = os.environ.get("BD_WHERE_OVERRIDE", "").strip()
    if override:
        return Path(override)
    cur = start.resolve()
    while True:
        cand = cur / ".beads"
        if cand.is_dir():
            if any((cand / m).exists() for m in ("metadata.json", "config.yaml", "dolt", "embeddeddolt", "redirect")):
                return cand
        if (cur / ".git").exists():
            # Support linked git worktrees (.git file pointing to main repo)
            git_marker = cur / ".git"
            if git_marker.is_file():
                text = git_marker.read_text(encoding="utf-8", errors="ignore").strip()
                if text.startswith("gitdir:"):
                    gitdir = Path(text.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (cur / gitdir).resolve()
                    commondir_file = gitdir / "commondir"
                    if commondir_file.is_file():
                        common = (gitdir / commondir_file.read_text().strip()).resolve()
                        main_beads = common.parent / ".beads"
                        if main_beads.is_dir():
                            return main_beads
            return None
        if cur.parent == cur:
            return None
        cur = cur.parent

if cmd == "where":
    if os.environ.get("BD_WHERE_UNKNOWN") == "1":
        sys.stderr.write("Error: unknown command \\"where\\" for \\"bd\\"\\n")
        sys.exit(1)
    store = find_store(Path(cwd))
    if store is None:
        sys.stderr.write("Error: No active beads workspace found.\\n")
        sys.exit(1)
    print(str(store))
    print(f"  database: {{store / 'dolt'}}")
    sys.exit(0)

if cmd == "prime":
    if os.environ.get("BD_PRIME_FAIL") == "1":
        print("prime refused: store locked")
        sys.exit(1)
    print(os.environ.get("BD_PRIME_OUTPUT", "PRIMED-CONTEXT"))
    sys.exit(0)

if cmd == "config" and len(args) >= 3 and args[1] == "get":
    key = args[2]
    env_key = "BD_CFG_" + key.upper().replace(".", "_").replace("-", "_")
    val = os.environ.get(env_key)
    if val is None:
        print(f"{{key}} (not set in config.yaml)")
    else:
        print(val)
    sys.exit(0)

if cmd == "dolt" and len(args) >= 3 and args[1] == "remote" and args[2] == "list":
    remotes = os.environ.get("BD_REMOTES", "")
    if remotes.strip():
        print(remotes)
    else:
        print("No remotes configured.")
    sys.exit(0)

if cmd == "dolt" and len(args) >= 2 and args[1] == "pull":
    delay = float(os.environ.get("BD_PULL_SLEEP", "0"))
    if delay > 0:
        time.sleep(delay)
    if os.environ.get("BD_PULL_FAIL") == "1":
        sys.stderr.write("remote unreachable\\n")
        sys.exit(1)
    print("Pulled from remote")
    sys.exit(0)

if cmd == "dolt" and len(args) >= 2 and args[1] == "commit":
    sleep_dir = os.environ.get("BD_COMMIT_SLEEP_DIR", "")
    in_sleep_dir = (not sleep_dir) or (Path(cwd).resolve() == Path(sleep_dir).resolve())
    if os.environ.get("BD_COMMIT_STARTED_FILE") and in_sleep_dir:
        Path(os.environ["BD_COMMIT_STARTED_FILE"]).write_text(str(os.getpid()))
    delay = float(os.environ.get("BD_COMMIT_SLEEP", "0"))
    if delay > 0 and in_sleep_dir:
        time.sleep(delay)
    if os.environ.get("BD_COMMIT_OVERLAP_FILE"):
        f = Path(os.environ["BD_COMMIT_OVERLAP_FILE"])
        with f.open("a") as fh:
            fh.write(f"enter:{{time.monotonic()}}\\n")
        time.sleep(0.15)
        with f.open("a") as fh:
            fh.write(f"exit:{{time.monotonic()}}\\n")
    fail_dir = os.environ.get("BD_COMMIT_FAIL_DIR", "")
    if os.environ.get("BD_COMMIT_FAIL") == "1" or (fail_dir and Path(cwd).resolve() == Path(fail_dir).resolve()):
        sys.stderr.write("commit failed: disk full\\n")
        sys.exit(1)
    if os.environ.get("BD_COMMIT_NOTHING") == "1":
        print("Nothing to commit.")
        sys.exit(1)
    print("Committed")
    sys.exit(0)

if cmd == "dolt" and len(args) >= 2 and args[1] == "push":
    if os.environ.get("BD_PUSH_REQUIRE_NO_ADOPT") == "1":
        if "--no-adopt" not in args or os.environ.get("BD_NO_REMOTE_ADOPT") != "1":
            sys.stderr.write("missing --no-adopt safeguard\\n")
            sys.exit(2)
    if os.environ.get("BD_PUSH_FAIL") == "1":
        sys.stderr.write("push rejected: auth failed\\n")
        sys.exit(1)
    print("Pushed")
    sys.exit(0)

sys.exit(0)
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


class HookTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="abh-test-"))
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.log_file = self.tmp / "bd.log"
        self.log_file.write_text("", encoding="utf-8")
        make_bd_stub(self.bin_dir, self.log_file)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_store(self, path: Path) -> Path:
        beads = path / ".beads"
        beads.mkdir(parents=True, exist_ok=True)
        (beads / "metadata.json").write_text('{"prefix": "tst"}\n', encoding="utf-8")
        return beads

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        e = os.environ.copy()
        e["PATH"] = f"{self.bin_dir}:{e.get('PATH', '/usr/bin:/bin')}"
        e["BD_LOG"] = str(self.log_file)
        if extra:
            e.update(extra)
        return e

    def run_hook(
        self,
        script: Path,
        *,
        args: list[str] | None = None,
        payload: dict | None = None,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        stdin_text = json.dumps(payload) if payload is not None else ""
        return subprocess.run(
            [BASH_BIN, str(script), *(args or [])],
            input=stdin_text,
            cwd=str(cwd or self.tmp),
            env=self.env(extra_env),
            capture_output=True,
            text=True,
            check=False,
        )

    def bd_calls(self) -> list[tuple[str, str]]:
        lines = [ln for ln in self.log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        out = []
        for ln in lines:
            c, a = ln.split("|", 1)
            out.append((c, a))
        return out


class TestPrimeHook(HookTestBase):
    def test_auggie_prime_basic_and_cwd(self) -> None:
        ws = self.tmp / "proj"
        self.make_store(ws)
        other = self.tmp / "other"
        self.make_store(other)

        res = self.run_hook(PRIME_SH, cwd=other, extra_env={"AUGMENT_PROJECT_DIR": str(ws)})
        self.assertEqual(res.returncode, 0)
        self.assertIn("PRIMED-CONTEXT", res.stdout)
        prime_calls = [c for c, a in self.bd_calls() if a == "prime"]
        self.assertEqual([Path(c).resolve() for c in prime_calls], [ws.resolve()])

    def test_antigravity_prime_json_schema(self) -> None:
        ws = self.tmp / "proj"
        self.make_store(ws)
        res = self.run_hook(
            PRIME_SH,
            args=["--agent", "antigravity"],
            payload={"workspacePaths": [str(ws)], "cwd": str(ws)},
        )
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertEqual(
            data,
            {"injectSteps": [{"systemMessage": {"systemMessage": "PRIMED-CONTEXT"}}]},
        )

    def test_antigravity_prime_no_store_returns_empty_json(self) -> None:
        ws = self.tmp / "no-beads"
        ws.mkdir()
        res = self.run_hook(
            PRIME_SH,
            args=["--agent", "antigravity"],
            payload={"workspacePaths": [str(ws)]},
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(json.loads(res.stdout), {})

    def test_empty_beads_dir_without_project_files_ignored(self) -> None:
        home_like = self.tmp / "fake-home"
        (home_like / ".beads").mkdir(parents=True)
        res = self.run_hook(PRIME_SH, extra_env={"AUGMENT_PROJECT_DIR": str(home_like)})
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")

    def test_linked_git_worktree_resolves_main_store(self) -> None:
        main_repo = self.tmp / "main-repo"
        main_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
        subprocess.run(
            ["git", "-C", str(main_repo), "-c", "user.name=t", "-c", "user.email=t@e", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "init"],
            check=True,
        )
        self.make_store(main_repo)

        wt = self.tmp / "orca-worktree"
        subprocess.run(
            ["git", "-C", str(main_repo), "worktree", "add", "-q", "-b", "wt-branch", str(wt)],
            check=True,
        )
        self.assertFalse((wt / ".beads").exists())

        res = self.run_hook(
            PRIME_SH,
            args=["--agent", "antigravity"],
            payload={"workspacePaths": [str(wt)], "cwd": str(wt)},
        )
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertEqual(data["injectSteps"][0]["systemMessage"]["systemMessage"], "PRIMED-CONTEXT")

    def test_deduplicates_multiple_roots_sharing_same_store(self) -> None:
        ws = self.tmp / "repo"
        sub = ws / "sub"
        sub.mkdir(parents=True)
        self.make_store(ws)
        res = self.run_hook(
            PRIME_SH,
            payload={"workspace_roots": [str(ws), str(sub)]},
        )
        self.assertEqual(res.returncode, 0)
        prime_calls = [a for _, a in self.bd_calls() if a == "prime"]
        self.assertEqual(len(prime_calls), 1)

    def test_cdpath_and_relative_path_bd(self) -> None:
        decoy = self.tmp / "decoy" / "rel"
        real = self.tmp / "real" / "rel"
        self.make_store(decoy)
        self.make_store(real)
        res = self.run_hook(
            PRIME_SH,
            cwd=self.tmp / "real",
            extra_env={"AUGMENT_PROJECT_DIR": "rel", "CDPATH": str(self.tmp / "decoy")},
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "PRIMED-CONTEXT")
        prime_calls = [Path(c).resolve() for c, a in self.bd_calls() if a == "prime"]
        self.assertEqual(prime_calls, [real.resolve()])

        # Relative PATH entry (`bin:/usr/bin:/bin`) resolved before chdir
        proj = self.tmp / "proj2"
        self.make_store(proj)
        res2 = subprocess.run(
            [BASH_BIN, str(PRIME_SH)],
            cwd=str(self.tmp),
            env={
                "PATH": "bin:/usr/bin:/bin",
                "AUGMENT_PROJECT_DIR": str(proj),
                "BD_LOG": str(self.log_file),
                "HOME": os.environ.get("HOME", str(self.tmp)),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res2.returncode, 0)
        self.assertEqual(res2.stdout.strip(), "PRIMED-CONTEXT")

    def test_session_start_pull_when_remote_configured(self) -> None:
        ws = self.tmp / "remote-repo"
        self.make_store(ws)
        res = self.run_hook(
            PRIME_SH,
            payload={"workspace_roots": [str(ws)]},
            extra_env={"BD_REMOTES": "origin   https://doltremoteapi.dolthub.com/u/repo"},
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("PRIMED-CONTEXT", res.stdout)
        actions = [a for _, a in self.bd_calls()]
        self.assertIn("dolt pull --remote origin", actions)
        self.assertLess(actions.index("dolt pull --remote origin"), actions.index("prime"))

    def test_session_start_skips_pull_when_local_only_or_no_remote(self) -> None:
        ws = self.tmp / "local-repo"
        self.make_store(ws)
        self.run_hook(
            PRIME_SH,
            payload={"workspace_roots": [str(ws)]},
            extra_env={
                "BD_REMOTES": "origin   https://doltremoteapi.dolthub.com/u/repo",
                "BD_CFG_DOLT_LOCAL_ONLY": "true",
            },
        )
        actions = [a for _, a in self.bd_calls()]
        self.assertNotIn("dolt pull --remote origin", actions)
        self.assertIn("prime", actions)

    def test_session_start_pull_timeout_still_primes(self) -> None:
        ws = self.tmp / "slow-remote"
        self.make_store(ws)
        res = self.run_hook(
            PRIME_SH,
            payload={"workspace_roots": [str(ws)]},
            extra_env={
                "BD_REMOTES": "origin   https://doltremoteapi.dolthub.com/u/repo",
                "BD_PULL_SLEEP": "1.0",
                "BEADS_HOOK_PULL_TIMEOUT": "0.15",
            },
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("timed out", res.stdout)
        self.assertIn("PRIMED-CONTEXT", res.stdout)

    def test_prime_failure_reported_with_exit_0(self) -> None:
        ws = self.tmp / "broken"
        self.make_store(ws)
        res = self.run_hook(
            PRIME_SH,
            payload={"workspace_roots": [str(ws)]},
            extra_env={"BD_PRIME_FAIL": "1"},
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("'bd prime' failed", res.stdout)
        self.assertIn("store locked", res.stdout)


class TestGuardHook(HookTestBase):
    BLOCKED_COMMANDS = [
        "bd edit abh-1",
        "bd edit",
        "bd -C /tmp/repo edit abh-1 --title",
        "bd --db=/tmp/x edit abh-1",
        "bd --directory /tmp/x --quiet edit abh-1",
        "/opt/homebrew/bin/bd edit abh-1",
        "BEADS_DIR=/tmp/.beads bd edit abh-1",
        "env FOO=1 -u BAR bd edit abh-1",
        "command bd edit abh-1",
        "exec bd edit abh-1",
        "cd sub && bd edit abh-1",
        "true; bd edit abh-1",
        "(bd edit abh-1)",
        'bash -c "bd edit abh-1"',
        "2>/dev/null bd edit abh-1",
    ]

    ALLOWED_COMMANDS = [
        "bd list",
        "bd update abh-1 --title 'New title'",
        'bd update abh-1 --notes "never use bd edit"',
        "bd edit --help",
        "bd edit -h",
        "bd --help edit",
        'echo "bd edit abh-1"',
        "echo 'bd edit abh-1'",
        'git commit -m "block bd edit"',
        "# bd edit abh-1",
        "mybd edit abh-1",
        "rg 'bd edit' README.md",
    ]

    def test_auggie_blocks_interactive_bd_edit(self) -> None:
        for cmd in self.BLOCKED_COMMANDS:
            with self.subTest(cmd=cmd):
                res = self.run_hook(
                    GUARD_SH,
                    payload={"tool_name": "bash", "tool_input": {"command": cmd}},
                )
                self.assertEqual(res.returncode, 2, f"expected exit 2 for: {cmd}")
                self.assertIn("bd update", res.stderr)
                self.assertEqual(res.stdout, "")

    def test_auggie_allows_safe_commands(self) -> None:
        for cmd in self.ALLOWED_COMMANDS:
            with self.subTest(cmd=cmd):
                res = self.run_hook(
                    GUARD_SH,
                    payload={"tool_name": "bash", "tool_input": {"command": cmd}},
                )
                self.assertEqual(res.returncode, 0, f"unexpected block for: {cmd}")
                self.assertEqual(res.stdout, "")
                self.assertEqual(res.stderr, "")

    def test_antigravity_denies_with_json_and_emits_empty_on_allow(self) -> None:
        res_deny = self.run_hook(
            GUARD_SH,
            args=["--agent", "antigravity"],
            payload={"toolCall": {"name": "run_command", "args": {"CommandLine": "bd edit abh-42"}}},
        )
        self.assertEqual(res_deny.returncode, 0)
        data = json.loads(res_deny.stdout)
        self.assertEqual(data["decision"], "deny")
        self.assertIn("bd update", data["reason"])

        # Critical: allow MUST emit empty stdout, NOT `{"decision": "allow"}`!
        res_allow = self.run_hook(
            GUARD_SH,
            args=["--agent", "antigravity"],
            payload={"toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /tmp/x"}}},
        )
        self.assertEqual(res_allow.returncode, 0)
        self.assertEqual(res_allow.stdout, "")

    def test_non_shell_tool_ignored(self) -> None:
        res = self.run_hook(
            GUARD_SH,
            args=["--agent", "antigravity"],
            payload={"toolCall": {"name": "read_file", "args": {"CommandLine": "bd edit abh-1"}}},
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")


class TestSyncHook(HookTestBase):
    def test_commits_and_pushes_when_remote_present(self) -> None:
        ws = self.tmp / "push-repo"
        self.make_store(ws)
        res = self.run_hook(
            SYNC_SH,
            payload={"hook_event_name": "SessionEnd", "workspace_roots": [str(ws)]},
            extra_env={
                "BD_REMOTES": "origin   https://doltremoteapi.dolthub.com/u/repo",
                "BD_PUSH_REQUIRE_NO_ADOPT": "1",
            },
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stderr, "")
        actions = [a for _, a in self.bd_calls()]
        self.assertTrue(any(a.startswith("dolt commit -m bd: sync via auggie SessionEnd") for a in actions))
        self.assertIn("dolt push --remote origin --no-adopt", actions)

    def test_no_push_when_no_remote_or_disabled(self) -> None:
        cases = [
            ("no_remote", {"BD_REMOTES": ""}),
            (
                "auto_push_false",
                {
                    "BD_REMOTES": "origin   https://doltremoteapi.dolthub.com/u/repo",
                    "BD_CFG_DOLT_AUTO_PUSH": "false",
                },
            ),
            (
                "local_only_true",
                {
                    "BD_REMOTES": "origin   https://doltremoteapi.dolthub.com/u/repo",
                    "BD_CFG_DOLT_LOCAL_ONLY": "true",
                },
            ),
            (
                "no_push_true",
                {
                    "BD_REMOTES": "origin   https://doltremoteapi.dolthub.com/u/repo",
                    "BD_CFG_NO_PUSH": "true",
                },
            ),
        ]
        for label, extra in cases:
            with self.subTest(case=label):
                self.log_file.write_text("", encoding="utf-8")
                ws = self.tmp / f"repo-{label}"
                self.make_store(ws)
                res = self.run_hook(
                    SYNC_SH,
                    payload={"hook_event_name": "Stop", "workspace_roots": [str(ws)]},
                    extra_env=extra,
                )
                self.assertEqual(res.returncode, 0)
                self.assertEqual(res.stderr, "")
                actions = [a for _, a in self.bd_calls()]
                self.assertTrue(any(a.startswith("dolt commit") for a in actions))
                self.assertFalse(any(a.startswith("dolt push") for a in actions), f"pushed in {label}")

    def test_nothing_to_commit_is_silent_success(self) -> None:
        ws = self.tmp / "clean-repo"
        self.make_store(ws)
        res = self.run_hook(
            SYNC_SH,
            args=["--agent", "antigravity"],
            payload={"hookEventName": "Stop", "workspacePaths": [str(ws)]},
            extra_env={"BD_COMMIT_NOTHING": "1"},
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stderr, "")
        self.assertEqual(json.loads(res.stdout), {"decision": ""})

    def test_flock_serializes_concurrent_syncs(self) -> None:
        ws = self.tmp / "concurrent"
        self.make_store(ws)
        overlap_file = self.tmp / "overlap.log"
        overlap_file.write_text("", encoding="utf-8")

        threads = [
            threading.Thread(
                target=self.run_hook,
                args=(SYNC_SH,),
                kwargs={
                    "payload": {"hook_event_name": "Stop", "workspace_roots": [str(ws)]},
                    "extra_env": {"BD_COMMIT_OVERLAP_FILE": str(overlap_file)},
                },
            )
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        events = [ln.split(":")[0] for ln in overlap_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        # Strictly alternating enter/exit proves zero concurrent overlap.
        self.assertEqual(events, ["enter", "exit", "enter", "exit", "enter", "exit"])

    def test_sigkill_preserves_flock_until_bd_child_exits_then_releases(self) -> None:
        ws = self.tmp / "sigkill"
        beads = self.make_store(ws)
        started_file = self.tmp / "bd.pid"

        proc = subprocess.Popen(
            [BASH_BIN, str(SYNC_SH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ws),
            env=self.env(
                {
                    "BD_COMMIT_SLEEP": "0.6",
                    "BD_COMMIT_STARTED_FILE": str(started_file),
                }
            ),
            text=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"hook_event_name": "Stop", "workspace_roots": [str(ws)]}))
        proc.stdin.close()

        # Wait for child `bd` to start inside the lock
        deadline = time.monotonic() + 5.0
        while not started_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(started_file.exists(), "bd child never started")

        # SIGKILL the hook process while `bd` is still sleeping
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

        # While `bd` is still running, lock MUST remain held (via inherited fd)
        lock_path = beads / ".beads-hook.lock"
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Once `bd` finishes (~0.6s), lock MUST become acquirable immediately
            fcntl.flock(fd, fcntl.LOCK_EX)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_failed_first_root_releases_lock_before_second_root(self) -> None:
        r1 = self.tmp / "root1"
        r2 = self.tmp / "root2"
        b1 = self.make_store(r1)
        self.make_store(r2)
        started_r2 = self.tmp / "r2.pid"

        proc = subprocess.Popen(
            [BASH_BIN, str(SYNC_SH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.tmp),
            env=self.env(
                {
                    "BD_COMMIT_FAIL_DIR": str(r1),
                    "BD_COMMIT_SLEEP_DIR": str(r2),
                    "BD_COMMIT_SLEEP": "0.5",
                    "BD_COMMIT_STARTED_FILE": str(started_r2),
                }
            ),
            text=True,
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"hook_event_name": "SessionEnd", "workspace_roots": [str(r1), str(r2)]}))
            proc.stdin.close()

            # Wait deterministically until root2's commit has started
            deadline = time.monotonic() + 5.0
            while not started_r2.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started_r2.exists(), "root2 commit never started")

            # While root2 is still sleeping inside commit, root1's lock MUST already be released
            lock1 = b1 / ".beads-hook.lock"
            self.assertTrue(lock1.exists())
            fd = os.open(str(lock1), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

            _, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("commit failed: disk full", stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_dangling_lock_symlink_cleaned_and_timeout_warns(self) -> None:
        ws = self.tmp / "dangling"
        beads = self.make_store(ws)
        (beads / ".beads-hook.lock").symlink_to("999999:boot:0")

        res = self.run_hook(
            SYNC_SH,
            payload={"hook_event_name": "Stop", "workspace_roots": [str(ws)]},
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stderr, "")
        self.assertFalse((beads / ".beads-hook.lock").is_symlink())

        # Now hold lock externally and verify timeout warning + exit 0
        fd = os.open(str(beads / ".beads-hook.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            res2 = self.run_hook(
                SYNC_SH,
                args=["--agent", "antigravity"],
                payload={"hookEventName": "Stop", "workspacePaths": [str(ws)]},
                extra_env={"BEADS_HOOK_LOCK_TIMEOUT": "0.1"},
            )
            self.assertEqual(res2.returncode, 0)
            self.assertIn("timed out", res2.stderr)
            self.assertEqual(json.loads(res2.stdout), {"decision": ""})
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class TestInstallerAndWiring(HookTestBase):
    def _make_git_source(self) -> Path:
        src = self.tmp / "src-repo"
        src.mkdir()
        subprocess.run(["git", "init", "-q", str(src)], check=True)
        # Copy current repo files (excluding .git) into temp source repo
        for rel in (
            "install.sh",
            "README.md",
            ".augment/hooks/beads-prime.sh",
            ".augment/hooks/beads-sync.sh",
            ".augment/hooks/beads-guard.sh",
            ".augment/hooks/lib/beads_hook.py",
            ".augment/hooks/lib/wiring.py",
        ):
            dest = src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = (REPO_ROOT / rel).read_bytes()
            dest.write_bytes(data)
            dest.chmod((REPO_ROOT / rel).stat().st_mode)
        git = lambda *a: subprocess.run(
            ["git", "-C", str(src), "-c", "user.name=ci", "-c", "user.email=ci@e", "-c", "commit.gpgsign=false", *a],
            check=True,
            capture_output=True,
            text=True,
        )
        git("add", "-A")
        git("commit", "-qm", "v0.3.0")
        git("tag", "v0.3.0")
        git("tag", "v0.3.1")
        return src

    def test_install_rollback_and_cross_agent_wiring(self) -> None:
        src = self._make_git_source()
        prefix = self.tmp / "with space" / "abh"
        aug_settings = self.tmp / "settings.json"
        agy_hooks = self.tmp / "hooks.json"

        def run_inst(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    BASH_BIN,
                    str(INSTALL_SH),
                    "--prefix",
                    str(prefix),
                    "--source",
                    f"file://{src}",
                    "--settings",
                    str(aug_settings),
                    "--antigravity-hooks",
                    str(agy_hooks),
                    *args,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        # Install, reinstall, upgrade, roll back
        self.assertEqual(run_inst("v0.3.0").returncode, 0)
        self.assertEqual(os.readlink(prefix / "current"), "v0.3.0")
        r_dup = run_inst("v0.3.0")
        self.assertEqual(r_dup.returncode, 0)
        self.assertIn("already installed", r_dup.stdout)
        self.assertEqual(run_inst("v0.3.1").returncode, 0)
        self.assertEqual(os.readlink(prefix / "current"), "v0.3.1")
        self.assertEqual(run_inst("v0.3.0").returncode, 0)
        self.assertEqual(os.readlink(prefix / "current"), "v0.3.0")

        # Reject invalid tag / branch name
        self.assertNotEqual(run_inst("main").returncode, 0)
        self.assertNotEqual(run_inst("v9.9.9").returncode, 0)

        # Seed existing Auggie settings (with legacy PostToolUse + foreign + mention)
        # and existing Antigravity hooks.json (with orca-status)
        aug_settings.write_text(
            json.dumps(
                {
                    "model": "m",
                    "hooks": {
                        "PostToolUse": [
                            {"matcher": "edit", "hooks": [{"type": "command", "command": "/x/fmt.sh"}]},
                            {"matcher": "bash", "hooks": [{"type": "command", "command": "/old/beads-sync.sh"}]},
                            {"matcher": "bash", "hooks": [{"type": "command", "command": 'bash -c "/old/beads-sync.sh"'}]},
                        ],
                        "SessionEnd": [
                            {"hooks": [{"type": "command", "command": "/x/custom-beads-sync.sh"}]},
                            {"hooks": [{"type": "command", "command": "bash", "args": ["/old/beads-sync.sh"]}]},
                        ],
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "echo /old/beads-sync.sh"}]},
                        ],
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        agy_hooks.write_text(
            json.dumps(
                {
                    "orca-status": {
                        "Stop": [{"type": "command", "command": "/x/orca.sh", "timeout": 10}]
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # Wire BOTH agents
        r_wire = run_inst("--agent", "all", "--wire")
        self.assertEqual(r_wire.returncode, 0, r_wire.stderr)
        self.assertIn("mentioned: Stop: echo /old/beads-sync.sh", r_wire.stdout)
        self.assertTrue(Path(f"{aug_settings}.bak").exists())
        self.assertTrue(Path(f"{agy_hooks}.bak").exists())

        aug_data = json.loads(aug_settings.read_text(encoding="utf-8"))
        self.assertEqual(aug_data["model"], "m")
        # Legacy PostToolUse beads-sync entries removed, foreign /x/fmt.sh kept
        self.assertEqual(
            [e["hooks"][0]["command"] for e in aug_data["hooks"]["PostToolUse"]],
            ["/x/fmt.sh"],
        )
        # New 4 Auggie events wired cleanly
        self.assertEqual(len(aug_data["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(aug_data["hooks"]["PreToolUse"]), 1)
        self.assertEqual(aug_data["hooks"]["PreToolUse"][0]["matcher"], "bash")
        self.assertIn("beads-guard.sh", aug_data["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertEqual(len(aug_data["hooks"]["Stop"]), 2)  # mention kept + new sync added
        self.assertEqual(len(aug_data["hooks"]["SessionEnd"]), 2)  # custom-beads-sync kept + new sync

        # Antigravity keeps orca-status untouched and adds `beads` group with seconds timeouts
        agy_data = json.loads(agy_hooks.read_text(encoding="utf-8"))
        self.assertIn("orca-status", agy_data)
        self.assertIn("beads", agy_data)
        self.assertEqual(agy_data["beads"]["SessionStart"][0]["timeout"], 15)
        self.assertEqual(agy_data["beads"]["PreToolUse"][0]["matcher"], "run_command")
        self.assertEqual(agy_data["beads"]["PreToolUse"][0]["hooks"][0]["timeout"], 5)
        self.assertEqual(agy_data["beads"]["Stop"][0]["timeout"], 60)
        self.assertIn("--agent antigravity", agy_data["beads"]["Stop"][0]["command"])

        # Wired commands actually execute via `sh -c` (including space in prefix)
        ws = self.tmp / "ws"
        ws.mkdir()
        sh_prime = aug_data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(
            subprocess.run(["sh", "-c", sh_prime], cwd=str(ws), env=self.env(), capture_output=True).returncode,
            0,
        )
        agy_stop = agy_data["beads"]["Stop"][0]["command"]
        r_agy_stop = subprocess.run(
            ["sh", "-c", agy_stop],
            input=json.dumps({"workspacePaths": [str(ws)]}),
            cwd=str(ws),
            env=self.env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(r_agy_stop.returncode, 0)
        self.assertEqual(json.loads(r_agy_stop.stdout), {"decision": ""})

        # Idempotent re-wire
        r_wire2 = run_inst("--agent", "all", "--wire")
        self.assertEqual(r_wire2.returncode, 0)
        self.assertIn("is unchanged", r_wire2.stdout)

        # Status reports both agents
        r_status = run_inst("--agent", "all", "--status")
        self.assertEqual(r_status.returncode, 0)
        self.assertIn("current: v0.3.0", r_status.stdout)
        self.assertIn("wired: PreToolUse:", r_status.stdout)
        self.assertIn("wired (antigravity): Stop:", r_status.stdout)

        # Unwire both agents preserves foreign hooks & orca-status
        r_unwire = run_inst("--agent", "all", "--unwire")
        self.assertEqual(r_unwire.returncode, 0)
        aug_after = json.loads(aug_settings.read_text(encoding="utf-8"))
        self.assertNotIn("SessionStart", aug_after.get("hooks", {}))
        self.assertNotIn("PreToolUse", aug_after.get("hooks", {}))
        self.assertEqual(aug_after["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "/x/fmt.sh")
        agy_after = json.loads(agy_hooks.read_text(encoding="utf-8"))
        self.assertIn("orca-status", agy_after)
        self.assertNotIn("beads", agy_after)

    def test_refuses_single_quote_in_prefix(self) -> None:
        src = self._make_git_source()
        q_prefix = self.tmp / "it's"
        aug_settings = self.tmp / "s.json"
        aug_settings.write_text('{"keep": true}\n', encoding="utf-8")
        subprocess.run(
            [BASH_BIN, str(INSTALL_SH), "--prefix", str(q_prefix), "--source", f"file://{src}", "v0.3.0"],
            check=True,
            capture_output=True,
        )
        res = subprocess.run(
            [BASH_BIN, str(INSTALL_SH), "--prefix", str(q_prefix), "--settings", str(aug_settings), "--wire"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(aug_settings.read_text(encoding="utf-8"), '{"keep": true}\n')

    def test_supports_legacy_v02_rollback_layout(self) -> None:
        legacy_src = self.tmp / "legacy-src"
        legacy_src.mkdir()
        subprocess.run(["git", "init", "-q", str(legacy_src)], check=True)
        hooks = legacy_src / ".augment" / "hooks"
        (hooks / "lib").mkdir(parents=True)
        for name in ("beads-prime.sh", "beads-sync.sh"):
            p = hooks / name
            p.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            p.chmod(0o755)
        (hooks / "lib" / "command-scan.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (hooks / "lib" / "command_scan.py").write_text("x = 1\n", encoding="utf-8")
        git = lambda *a: subprocess.run(
            ["git", "-C", str(legacy_src), "-c", "user.name=ci", "-c", "user.email=ci@e", "-c", "commit.gpgsign=false", *a],
            check=True,
            capture_output=True,
        )
        git("add", "-A")
        git("commit", "-qm", "v0.2.1")
        git("tag", "v0.2.1")

        prefix = self.tmp / "rollback-prefix"
        res = subprocess.run(
            [BASH_BIN, str(INSTALL_SH), "--prefix", str(prefix), "--source", f"file://{legacy_src}", "v0.2.1"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(os.readlink(prefix / "current"), "v0.2.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
