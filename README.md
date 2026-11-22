# auggie-beads-hooks

Cross-agent lifecycle hooks that keep a [Beads](https://github.com/gastownhall/beads)
(`bd`) issue store in sync with **Auggie CLI** and **Antigravity (`agy`)** sessions:

- **SessionStart (`beads-prime.sh`):** resolves the active store via `bd where`
  (including linked git/Orca worktrees and subdirectories), runs a short-timeout
  best-effort `bd dolt pull` when a Dolt remote is configured, and injects
  `bd prime` workflow context and persistent memories.
- **PreToolUse (`beads-guard.sh`):** blocks interactive `bd edit` commands that
  open `$EDITOR` and hang headless agents, redirecting the agent to
  non-interactive `bd update`.
- **Stop / SessionEnd (`beads-sync.sh`):** performs state-based `bd dolt commit`
  (and `bd dolt push --remote origin --no-adopt` when a Dolt remote is configured
  and pushing is enabled) under a kernel `fcntl.flock` lock with file-descriptor
  inheritance — no `PostToolUse` shell simulation, no synchronous per-tool
  latency, and no `shfmt` or `jq` dependency.

Install a release once per user and wire it into your user-level settings;
projects need neither a copy of the hooks nor any wiring of their own.

## Contents

| Path | Purpose |
| --- | --- |
| `install.sh` | Installs a tagged release per user and wires Auggie and/or Antigravity settings. |
| `.augment/hooks/beads-prime.sh` | SessionStart shim: invokes `lib/beads_hook.py prime`. |
| `.augment/hooks/beads-guard.sh` | PreToolUse shim: invokes `lib/beads_hook.py guard`. |
| `.augment/hooks/beads-sync.sh` | Stop / SessionEnd shim: invokes `lib/beads_hook.py sync`. |
| `.augment/hooks/lib/beads_hook.py` | Stdlib-only Python core implementing store discovery, `flock` locking, priming, guard, and sync. |
| `.augment/hooks/lib/shell_tokens.py` | Shared POSIX-style shell tokenizer (quotes, operators, newlines, redirections, heredocs). |
| `.augment/hooks/lib/wiring.py` | Stdlib-only Python helper used by `install.sh` to wire/unwire/inspect Auggie and Antigravity configs. |
| `.augment/hooks/test/` | Fast unittest suite (`hook_test.py`) and `/bin/bash` runner (`run_tests.sh`). |
| `.github/workflows/auggie-hooks.yaml` | Runs syntax checks, unit tests, and installer tests on Ubuntu and macOS. |

## Requirements

| Tool | Minimum | Tested with |
| --- | --- | --- |
| `bd` | 1.3.4 | 1.3.4 |
| `python3` | 3.9.6 | 3.9.6, 3.12, 3.14 |
| `bash` | 3.2 | 3.2.57 (macOS `/bin/bash`), 5.2+ |
| `git` | any | 2.40+ (`install.sh` release checkout) |
| Auggie CLI | 1.2.0 | 1.2.2 (for `SessionStart` context injection) |
| Antigravity (`agy`) | 1.5.x | 1.5.14 (optional, for `--agent antigravity` / `--agent all`) |

## How it works

### Store discovery (`bd where` + worktrees)

Every hook discovers stores by running `bd where` from each workspace root
(`workspace_roots` / `AUGMENT_PROJECT_DIR` / `cwd` for Auggie;
`workspacePaths` / `cwd` for Antigravity) and verifying that the resolved
`.beads/` directory contains project files (`metadata.json`, `config.yaml`,
`dolt/`, `embeddeddolt/`, `redirect`, or a non-backup `*.db`).

- Linked git worktrees (such as Orca workspaces under `~/orca/workspaces/`)
  resolve cleanly to the main clone's `.beads/` store even when no `.beads/`
  directory exists inside the worktree.
- Non-project `.beads/` directories (such as `~/.beads` in `$HOME`) are ignored.
- Multiple roots resolving to the same `.beads/` store are deduplicated so each
  store is primed or synced only once per event.

### SessionStart: `beads-prime.sh`

1. If a Dolt `origin` remote is configured (via `bd dolt remote list`) and
   `dolt.local-only` is not `true`, acquires the store lock briefly and runs
   `bd dolt pull --remote origin` with a 5-second timeout (`BEADS_HOOK_PULL_TIMEOUT`;
   set `BEADS_HOOK_NO_PULL=1` to disable). Pull failures or timeouts emit a
   one-line staleness notice without blocking `bd prime`.
2. Runs `bd prime` from the workspace directory (not `-C`, avoiding `bd`'s
   `PRIME.md` cwd lookup quirk).
3. Emits plain stdout for Auggie, or Antigravity's
   `{"injectSteps": [{"systemMessage": {"systemMessage": "..."}}]}` JSON schema
   when invoked with `--agent antigravity`. Always exits `0`.

### PreToolUse: `beads-guard.sh`

Inspects shell tool calls (`bash`, `launch-process`, `run_command`) for
interactive `bd edit` invocations (allowing `bd edit --help` / `-h`, quoted
mentions such as `echo "bd edit"`, comments, and `bd update --notes "...bd edit..."`):

- **Auggie:** prints a redirection message to `stderr` and exits `2` to block.
- **Antigravity (`--agent antigravity`):** prints
  `{"decision": "deny", "reason": "..."}` to `stdout` and exits `0`.
- **Non-matching commands:** prints **nothing** and exits `0`, preserving each
  host's normal permission checks.

### Stop and SessionEnd: `beads-sync.sh`

For each discovered store:

1. Acquires an exclusive kernel `fcntl.flock` lock on `.beads/.beads-hook.lock`
   (waits up to 30 seconds, configurable via `BEADS_HOOK_LOCK_TIMEOUT`). The open
   lock file descriptor is inherited by child `bd` processes (`pass_fds`), so
   even `SIGKILL` cannot leave a stale lock or allow overlapping `bd` writes.
2. Runs `bd dolt commit -m "bd: sync via <agent> <event>"`, treating "nothing to
   commit" as a clean no-op.
3. Pushes with `bd dolt push --remote origin --no-adopt` (with
   `BD_NO_REMOTE_ADOPT=1`) **only** when:
   - `origin` appears in `bd dolt remote list`,
   - `dolt.local-only` is not `true`,
   - `no-push` is not `true`, and
   - `dolt.auto-push` is not `false`.
4. Releases the lock immediately in a `finally` block before moving to any
   remaining workspace roots.

Always exits `0` (printing `{"decision": ""}` on stdout for Antigravity `Stop`
and any `beads:` warnings on `stderr`).

## Installing

Install a tagged release once per user, then wire it into Auggie, Antigravity,
or both:

```bash
git clone --depth 1 --branch <tag> https://github.com/nick-llewellyn/auggie-beads-hooks /tmp/auggie-beads-hooks
/tmp/auggie-beads-hooks/install.sh <tag> --agent all --wire
```

`install.sh <tag>` fetches the tag into
`~/.local/share/auggie-beads-hooks/<tag>` (or `$XDG_DATA_HOME/auggie-beads-hooks`,
or `--prefix <dir>`), verifies the scripts, and atomically switches `current`.

- `--agent auggie` (default): wires `SessionStart`, `PreToolUse` (`bash`),
  `Stop`, and `SessionEnd` in `~/.augment/settings.json` (automatically removing
  legacy `PostToolUse` `beads-sync.sh` entries while keeping unrelated hooks).
- `--agent antigravity`: wires the `"beads"` hook group (`SessionStart`,
  `PreToolUse` on `run_command`, `Stop`) in `~/.gemini/config/hooks.json`,
  preserving other groups such as `"orca-status"`.
- `--agent all`: wires, unwires, or inspects both agents in one command.

```bash
~/.local/share/auggie-beads-hooks/current/install.sh --agent all --status
~/.local/share/auggie-beads-hooks/current/install.sh --agent all --unwire
```

### Updating and rolling back

Between releases sharing this layout (`v0.3.0` and later):

```bash
~/.local/share/auggie-beads-hooks/current/install.sh <new-tag>
```

Moving `current` takes effect immediately for subsequent hook invocations, and
installing an already-installed `v0.3.x+` tag rolls `current` back to it.

This release is a breaking layout change from `v0.1.x`/`v0.2.x` (backwards
compatibility and rollbacks to pre-`v0.3.0` scanner releases are not supported).
To upgrade from an existing `v0.2.x` installation, clone the new tag and run
its `install.sh` with `--wire` so legacy `PostToolUse` wiring is migrated to
`PreToolUse` / `Stop` / `SessionEnd`:

```bash
git clone --depth 1 --branch <tag> https://github.com/nick-llewellyn/auggie-beads-hooks /tmp/auggie-beads-hooks
/tmp/auggie-beads-hooks/install.sh <tag> --agent all --wire
```

### Moving a project off an old local copy

Delete any repo-local copies of the files supplied by this repository:

```text
.augment/hooks/beads-guard.sh
.augment/hooks/beads-prime.sh
.augment/hooks/beads-sync.sh
.augment/hooks/lib/beads_hook.py
.augment/hooks/lib/shell_tokens.py
.augment/hooks/lib/wiring.py
.augment/hooks/test/hook_test.py
.augment/hooks/test/run_tests.sh
```

(Or, for `v0.2.x` copies, `lib/command-scan.sh`, `lib/command_scan.py`, and
`test/{classification,lock,prime}_test.sh`.) Remove project-level hook wiring
from `.augment/settings.json` so hooks do not run twice.

## Git ignores

The only transient state file created by the hooks is `.beads/.beads-hook.lock`,
which is already ignored by the `*.lock` rule in the `.beads/.gitignore` that
`bd init` generates.

## Running the tests

No `bd`, `shfmt`, or `jq` installation is required — the suite stubs `bd` in
scratch directories and completes in a few seconds:

```bash
/bin/bash .augment/hooks/test/run_tests.sh
```

## License

[MIT](LICENSE)
