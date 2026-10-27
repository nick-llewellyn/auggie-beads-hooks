# auggie-beads-hooks

Auggie CLI hooks that keep a [Beads](https://github.com/gastownhall/beads)
(`bd`) issue store in step with an agent session:

- **Session start:** add the `bd prime` workflow context to the session
  (Auggie CLI 0.36.0 discards it; see [Limitations](#limitations)).
- **After each shell command:** if the command wrote to a bead store, commit
  that store and, unless it is set to stay local, push it to its Dolt remote.
- **Session end:** sync every store once more, as a backstop.

`bd init` sets up Claude Code, Codex and Cursor, and `bd setup` has recipes
for several other tools, but none for Auggie (as of `bd` 1.3.1). Use this
repository as a template for a new project, or copy its files into an
existing one.

## Contents

| Path | Purpose |
| --- | --- |
| `.augment/settings.json` | Routes SessionStart, PostToolUse (`launch-process`) and SessionEnd to the hooks. |
| `.augment/hooks/beads-prime.sh` | SessionStart: prints `bd prime` for the workspace. |
| `.augment/hooks/beads-sync.sh` | PostToolUse and SessionEnd: commits, and optionally pushes, each store that was written. |
| `.augment/hooks/lib/command-scan.sh` | Runs the scanner for `beads-sync.sh`. |
| `.augment/hooks/lib/command_scan.py` | Parses the command with `shfmt --to-json` and reports whether any `bd` call can write, and to which store. |
| `.augment/hooks/test/` | Classification, lock and prime test suites. |
| `.github/workflows/auggie-hooks.yaml` | Runs the suites on Ubuntu and macOS when the hooks change. |

## Requirements

- The Auggie CLI, with hooks (tested with 0.36.0). Hooks fire only under
  the CLI; see [Limitations](#limitations).
- `bd` (tested with 1.3.1).
- `bash` 3.2 or later. macOS `/bin/bash` (3.2) is supported and tested.
- `jq`. Without it, the sync hook warns and does nothing.
- `shfmt` and `python3`, for the scanner. Without them, the sync hook warns
  and syncs the workspace roots after every shell command as a precaution.

## How it works

### SessionStart: `beads-prime.sh`

Runs `bd -C "$AUGMENT_PROJECT_DIR" prime`, falling back to the current
directory, and prints the result. Auggie's hooks documentation says
SessionStart output is added to the session context, but Auggie CLI 0.36.0
discards it; see [Limitations](#limitations). If `bd prime` fails, the hook
prints a `beads:` notice instead. It exits 0 in every case, and does nothing
when `bd` is missing or the workspace has no `.beads/`.

### PostToolUse and SessionEnd: `beads-sync.sh`

The hook reads the event as JSON on stdin: `hook_event_name`,
`workspace_roots`, `tool_name`, `tool_input.command`, `tool_input.cwd` and
`tool_input.wait`.

On **PostToolUse** for `launch-process`, it scans the command. Any `bd` call
not known to be read-only counts as a write. The scanner also works out which
store each call addresses: a workspace root, `bd -C <dir>`, `bd --db <path>`,
`BEADS_DIR` or `BEADS_DB`, following `cd`, shell variables and wrappers such
as `env`, `xargs` and `bash -c`, and resolving symlinks as `bd`, `cd` and
`cd -P` do. A read-only command such as `bd list` or `bd show` causes no
sync. A command too deeply nested to scan within a few seconds is treated as
a failed scan: the workspace roots are synced and the hook warns.

On **SessionEnd**, it syncs every workspace root that has a `.beads/`, and
every recorded external store, without looking at any command. This catches
writes PostToolUse missed.

For each store it syncs, the hook:

1. writes `.beads/.augment-sync.pending`, so a write that arrives during the
   sync earns another pass;
2. takes `.beads/.augment-sync.lock`, waiting up to 30 seconds, and reclaims
   a lock whose owner has died;
3. reads the store's `dolt.auto-push` setting (see
   [Choosing where the store syncs](#choosing-where-the-store-syncs));
4. runs `bd dolt commit -m "bd: sync via auggie <event>"`, where "nothing to
   commit" is not an error;
5. runs `bd dolt push --remote origin`, unless `dolt.auto-push` is `false`;
6. repeats, up to three passes, while new requests arrive, then releases the
   lock.

A store outside the workspace roots, written through `bd -C`, is recorded in
the first root's `.beads/.augment-sync.external`, so a failed sync there is
retried by a later invocation. The entry is removed once a sync of that store
completes, or when the store no longer exists.

A write the command runs in the background, after `&`, in a coprocess or in a
process substitution, can land after the hook has synced, as can any write in
a `launch-process` call made with `wait: false`. Its external store is also recorded, with the
session's `conversation_id`, in the first root's `.beads/.augment-sync.async`,
which only SessionEnd reads. Each SessionEnd moves every store listed there
back into `.augment-sync.external` and syncs it, then removes its own
session's records once the registry holds them.

The hook always exits 0. It reports problems as `beads:` warnings: on
PostToolUse as context the agent sees with the tool result, on SessionEnd on
stderr. A failed commit or push leaves the request marked, and the next sync
retries it. `settings.json` gives the sync hook 60 seconds and the prime hook
15.

## Adopting the hooks

### New project

Create a repository from this template, with **Use this template** on GitHub
or `gh repo create <name> --template nick-llewellyn/auggie-beads-hooks`, then
follow steps 2 to 5 below.

### Existing project

1. Copy the files, keeping their executable bits:

   ```bash
   git clone --depth 1 https://github.com/nick-llewellyn/auggie-beads-hooks /tmp/auggie-beads-hooks
   mkdir -p .augment .github/workflows
   cp -Rp /tmp/auggie-beads-hooks/.augment/hooks .augment/
   cp -p /tmp/auggie-beads-hooks/.github/workflows/auggie-hooks.yaml .github/workflows/
   ```

   If the project has no `.augment/settings.json`, copy that file too. If it
   has one, add the three entries from this repository's file to its `hooks`
   object. The commands are relative paths, which Auggie resolves against the
   workspace root.

2. Initialise Beads if the project does not use it yet: `bd init`. Add
   `--skip-agents` to leave out the Claude Code, Codex and Cursor files it
   would otherwise generate.
3. Choose where the store syncs; see the next section.
4. Ignore the hook's state files; see
   [Git ignores](#git-ignores-for-augment-sync-state).
5. Commit, and check that git recorded the executable bits. This prints
   nothing when they are all set:

   ```bash
   git ls-files -s .augment/hooks | awk '$1 != "100755" {print "not executable:", $4}'
   ```

A new Auggie session in the project should then open with the Beads workflow
context, except under Auggie CLI 0.36.0 (see [Limitations](#limitations)).
You can also run the hooks by hand:

```bash
AUGMENT_PROJECT_DIR="$PWD" .augment/hooks/beads-prime.sh </dev/null | head
jq -n --arg w "$PWD" '{hook_event_name: "SessionEnd", workspace_roots: [$w]}' |
  .augment/hooks/beads-sync.sh
```

The second command syncs the store, and prints nothing when it succeeds.

### Keeping copies in step

A project that adopts the hooks holds its own copy, so a fix made in one copy
reaches the others only by hand. This repository is the upstream for all of
them:

1. Make every change to `.augment/hooks/` here first, including a fix found
   while working in a project's copy, with a test that fails without it.
2. Run the three suites here, and open the pull request.
3. Apply the same commit to each project's copy, typically with
   `git format-patch` and `git apply`. Adjust only lines that differ there
   for the project's own reasons, such as temporary-file prefixes.
4. Run the three suites in each copy before pushing, and say in the commit
   which upstream commit it ports.

A review finding raised on a project's pull request is answered there, but
fixed here first, so the copies stay identical apart from those adjusted
lines. To see what a copy has drifted from, run `diff -r` between its
`.augment/hooks/` and this repository's.

## Choosing where the store syncs

Each store's `dolt.auto-push` setting decides whether the hook pushes. The
hook reads it with `bd config get dolt.auto-push` on every sync, store by
store, so a session that writes to several stores honours each one's setting.

| `dolt.auto-push` | What the hook does |
| --- | --- |
| unset, `true` or any other value | Commits, then runs `bd dolt push --remote origin`. |
| `false` | Commits only. |

A setting that cannot be read counts as unset, so a failed lookup never turns
off a push that a store relies on.

### DoltHub, or another Dolt remote

Use this when the store should be shared beyond one machine. With the default
embedded store:

```bash
bd dolt remote add origin https://doltremoteapi.dolthub.com/<owner>/<database>
bd config unset dolt.auto-push
```

- The hook always pushes to the remote named `origin`.
- DoltHub needs credentials on each machine: run `dolt login`, or add a key
  under your DoltHub credentials settings.
- If the remote already has history, pull before the first push:
  `bd dolt pull`.
- New clones can fetch the store and record the remote in one step with
  `bd init --remote <url>`, which saves it as `sync.remote` in
  `.beads/config.yaml`.

### A shared local Dolt SQL server

Use this when the projects on one machine keep their issues in databases on a
local `dolt sql-server`, and nothing should leave the machine. `bd` writes
straight into the server, so there is nothing to push; the hook still commits
each write to the database's history.

1. Run a Dolt SQL server, for example `dolt sql-server --host 127.0.0.1
   --port 3307` in a directory of its own, started at login by launchd or
   systemd.
2. Create the project's database on it:

   ```bash
   bd init --server --external --server-host 127.0.0.1 --server-port 3307 \
     --prefix <prefix> --non-interactive
   ```

   `bd init` commits the files it changes to git. Run it in a scratch clone
   first if you want to control that commit.

3. Remove any Dolt remote `bd init` configured. It derives one from
   `sync.remote`, or from the git `origin`:

   ```bash
   bd dolt remote list
   bd dolt remote remove origin
   ```

4. Keep the store local: `bd config set dolt.auto-push false`.
5. Keep `dolt_server_port` in `.beads/metadata.json`, although `bd` reports
   the field as deprecated. Without it, and without the git-ignored
   `.beads/dolt-server.port`, `bd` starts a server of its own on a random
   port.

Further clones on the same machine share the database and need no `bd init`.

## Git ignores for `.augment-sync.*` state

The sync hook keeps its state beside each store, in `.beads/`:

| File | Meaning |
| --- | --- |
| `.augment-sync.lock` | The lock: a symlink naming its owner. |
| `.augment-sync.pending` | A sync is owed. |
| `.augment-sync.inflight` | A pass is running, or was killed part-way. |
| `.augment-sync.child` | The `bd` process doing the work. |
| `.augment-sync.reclaim` | Guard held while a dead owner's lock is reclaimed. |
| `.augment-sync.external` | External stores owed a sync, in workspace roots only, with its `.guard`. |
| `.augment-sync.async` | External stores written in the background, kept for SessionEnd, with its `.guard`. |

All of it is per-machine and transient. The `.beads/.gitignore` that
`bd init` generates covers only the lock, through `*.lock`, so add:

```gitignore
# Auggie sync hook state (.augment/hooks/beads-sync.sh)
.augment-sync.*
```

Check it with `git check-ignore -v .beads/.augment-sync.pending`.

## Running the tests

The suites need `shfmt`, `jq` and `python3`. They stub `bd` and work in
scratch directories, so `bd` need not be installed and no bead store is
touched.

```bash
/bin/bash .augment/hooks/test/classification_test.sh   # about 1 minute
/bin/bash .augment/hooks/test/prime_test.sh            # about 1 second
/bin/bash .augment/hooks/test/lock_test.sh             # about 8 minutes
```

Each prints `<suite>: pass=N fail=0` and exits non-zero on any failure. On
macOS, run them with `/bin/bash` to cover bash 3.2: the suites run the hooks
under the interpreter that runs them. A few stderr lines, such as
`Permission denied`, come from cases that cause failures on purpose.

## CI with GitHub Actions

Copy `.github/workflows/auggie-hooks.yaml` into the project's
`.github/workflows/`. It has two jobs:

- **`changes`** compares the pushed or proposed commits with their base, and
  reports whether `.augment/` or the workflow itself changed. When the base
  cannot be read (a manual run, a new branch, a force push), it reports a
  change. It uses `git diff` rather than a third-party filter action.
- **`auggie-hooks`** runs only when `changes` reports a change, on
  `ubuntu-latest` and `macos-latest`. It installs `shfmt` (apt on Ubuntu,
  Homebrew on macOS, deliberately unpinned so the two legs cover different
  versions), checks that the entry points are executable and every script
  parses, and runs the three suites under `/bin/bash`.

Each leg takes 8 to 10 minutes. On private repositories macOS minutes are
billed at a higher rate, which is what the path filter is for.

To fold the jobs into an existing workflow instead, copy both jobs and change
the workflow path in the `changes` filter to that workflow's file.

### Required status checks

When `auggie-hooks` is skipped, GitHub reports it under its unexpanded name,
`Auggie hook tests (${{ matrix.os }})`, so branch protection cannot require
the per-platform checks. To require one check, add a gate job to the workflow
and require `Auggie hook tests gate`:

```yaml
  auggie-hooks-gate:
    name: Auggie hook tests gate
    runs-on: ubuntu-latest
    needs: [changes, auggie-hooks]
    if: always()
    steps:
      - env:
          CHANGES: ${{ needs.changes.result }}
          HOOKS: ${{ needs.auggie-hooks.result }}
        run: |
          case "$CHANGES/$HOOKS" in
            success/success | success/skipped) exit 0 ;;
            *) echo "changes=$CHANGES auggie-hooks=$HOOKS" >&2; exit 1 ;;
          esac
```

## Limitations

- The hooks run only under the Auggie CLI. In a host that does not read
  `.augment/settings.json`, bead writes are neither committed nor pushed by
  these hooks; run `bd dolt commit`, and `bd dolt push --remote origin` where
  the store pushes, before ending such a session.
- Auggie CLI 0.36.0 runs the SessionStart hook but discards its output, so
  the `bd prime` context does not reach the agent
  ([augmentcode/auggie#181](https://github.com/augmentcode/auggie/issues/181)).
  Until that is fixed, use a workspace rule, which Auggie adds to every
  prompt: a file in `.augment/rules/` that tells the agent to run `bd prime`
  at the start of a session. A store with a custom `.beads/PRIME.md` can
  instead make `.augment/rules/beads-prime.md` the real file and
  `.beads/PRIME.md` a symlink to it; `bd prime` follows the link, but Auggie
  skips rule files that are symlinks. That rule carries the `PRIME.md` text
  only; `bd prime` adds the stored memories.
- A store the scanner cannot name is reported in a warning rather than
  synced: `bd -C "$X"` where `$X` is known only at run time; an argument
  known only at run time before `--`, as in `bd close "$ID"`, which may be
  an option such as `--db=<path>`; a plain `bd` in a directory the event did
  not report; or script run through `eval`.
- Only `&`, `coproc`, process substitution, a wrapper that detaches its
  command, such as `setsid -f` or `sudo -b`, and a `launch-process` call
  made with `wait: false` count as background. A `wait: true` call that
  returns at `max_wait_seconds` with its command still running is not
  recognised, and a background write still running when the session ends
  is not waited for.
- The push always targets the remote named `origin`.
- The hooks add to, and do not replace, the git hook shims `bd init`
  installs under `.beads/hooks/`.

## Troubleshooting

- **`beads: jq not found ...`**: install `jq`.
- **`beads: could not scan the command for bead writes ...`**: `shfmt` or
  `python3` is missing or failed, or the command nests loops, functions or
  scripts too deeply to scan in time. The workspace roots are still synced.
- **`beads: 'bd dolt push' failed ... NOT on the remote`**: the store has no
  working `origin`. Add one, fix its credentials, or set
  `dolt.auto-push: false` if the store is meant to stay local. The request
  stays marked and is retried.
- **`beads: a bead store that was written could not be resolved to a path
  ...`**: see [Limitations](#limitations), and commit (and push) that store
  by hand. If the command passed `bd` a value known only at run time, such
  as an issue ID from an earlier `bd create`, put it after `--`, behind any
  options (`bd close -- "$ID"`, `bd update --notes x -- "$ID"`). The scan
  then reads it as an operand rather than a possible option, and names the
  store.
- **No Beads context at session start**: expected under Auggie CLI 0.36.0;
  see [Limitations](#limitations). Otherwise, check that the session runs
  under the Auggie CLI, that `.augment/hooks/*.sh` are executable, and that
  the workspace has a `.beads/`.
- **A sync keeps waiting on a lock**: a lock whose owner has died is
  reclaimed automatically, but a `bd` the hook started keeps the lock until
  it exits. If no `bd` is running, delete `.beads/.augment-sync.lock`.

## Removing the hooks

Delete `.augment/hooks/`, the three entries in `.augment/settings.json` and
`.github/workflows/auggie-hooks.yaml`, then any `.beads/.augment-sync.*`
files.

## License

[MIT](LICENSE)
