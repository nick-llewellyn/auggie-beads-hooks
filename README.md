# auggie-beads-hooks

Auggie CLI hooks that keep a [Beads](https://github.com/gastownhall/beads)
(`bd`) issue store in step with an agent session:

- **Session start:** add the `bd prime` workflow context to the session
  (Auggie CLI before 1.2.0 discards it; see [Limitations](#limitations)).
- **After each shell command:** if the command wrote to a bead store, commit
  that store and, unless it is set to stay local, push it to its Dolt remote.
- **Session end:** sync every store once more, as a backstop.

`bd init` sets up Claude Code, Codex and Cursor, and `bd setup` has recipes
for several other tools, but none for Auggie (as of `bd` 1.3.1). Install a
release of these hooks once per user and wire it into Auggie's user-level
settings; projects need neither a copy of the hooks nor any wiring of their
own. See [Installing](#installing).

## Contents

| Path | Purpose |
| --- | --- |
| `install.sh` | Installs a release for the current user and wires it into `~/.augment/settings.json`. |
| `.augment/hooks/beads-prime.sh` | SessionStart: prints `bd prime` for the workspace. |
| `.augment/hooks/beads-sync.sh` | PostToolUse and SessionEnd: commits, and optionally pushes, each store that was written. |
| `.augment/hooks/lib/command-scan.sh` | Runs the scanner for `beads-sync.sh`. |
| `.augment/hooks/lib/command_scan.py` | Parses the command with `shfmt --to-json` and reports whether any `bd` call can write, and to which store. |
| `.augment/hooks/test/` | Classification, lock and prime test suites. |
| `.github/workflows/auggie-hooks.yaml` | Runs the suites, and checks the installer, on Ubuntu and macOS when either changes. |

## Requirements

| Tool | Minimum | Tested with |
| --- | --- | --- |
| Auggie CLI | 0.36.0 | 0.36.0, 1.2.0 |
| `bd` | 1.3.4 | 1.3.4 |
| `bash` | 3.2 | 3.2.57 (macOS `/bin/bash`), 5.2.21, 5.3.9 |
| `jq` | 1.7.1 | 1.7.1, 1.8.2 |
| `shfmt` | 3.8.0 | 3.8.0, 3.12.0, 3.14.1 |
| `python3` | 3.9.6 | 3.9.6, 3.12.3, 3.15.2 |

A minimum is the oldest version supported. Older versions may work but are
not supported. The hooks run under bash; zsh is not needed.

- **Auggie CLI:** hooks fire only under the CLI; see
  [Limitations](#limitations). Before 1.2.0, the SessionStart context does
  not reach the agent.
- **`bd`:** checked on an embedded store and on a server-mode store (see
  [A shared local Dolt SQL server](#a-shared-local-dolt-sql-server)). The
  hooks run `bd prime`, `bd config get`, `bd dolt commit` and
  `bd dolt push --remote origin`.
- **`bash`:** macOS `/bin/bash` 3.2 is supported, and CI runs the suites
  under it.
- **`jq`:** without it, the sync hook warns and does nothing. `install.sh`
  needs it to wire the hooks.
- **`git`:** `install.sh` fetches a release with it.
- **`shfmt` and `python3`**, for the scanner: without them, the sync hook
  warns and syncs the workspace roots after every shell command it scans, as
  a precaution. An `shfmt` older than the minimum may lack the `--to-json`
  flag the scanner uses, with the same result: 3.4.3, which `apt` installs
  on Ubuntu 22.04, lacks it. `apt` installs 3.8.0 on Ubuntu 24.04 and
  3.12.0 on Ubuntu 26.04.

## How it works

### SessionStart: `beads-prime.sh`

Runs `bd prime` from `$AUGMENT_PROJECT_DIR`, falling back to the current
directory, and prints the result. It changes directory rather than passing
`-C`, because `bd -C <dir> prime` reads `.beads/PRIME.md` from the current
directory's store when that has one (bd 1.3.2). It looks `bd` up before
changing directory, so one found through a relative `PATH` entry still runs.
Auggie adds SessionStart output to the session context from CLI 1.2.0 on;
earlier versions, 0.36.0 among them, discard it; see
[Limitations](#limitations). If `bd prime` fails, the hook prints a `beads:`
notice instead. It exits 0 in every case, and does nothing when `bd` is
missing or the workspace has no `.beads/`.

### PostToolUse and SessionEnd: `beads-sync.sh`

The hook reads the event as JSON on stdin: `hook_event_name`,
`workspace_roots`, `tool_name`, `tool_input.command`, `tool_input.cwd` (or,
from Auggie CLI 1.x, the top-level `cwd`) and `tool_input.wait`.

On **PostToolUse** for the shell tool, it scans the command. Any `bd` call
not known to be read-only counts as a write. The scanner also works out which
store each call addresses: a workspace root, `bd -C <dir>`, `bd --db <path>`,
`BEADS_DIR` or `BEADS_DB`, following `cd`, shell variables and wrappers such
as `env`, `xargs` and `bash -c`, and resolving symlinks as `bd`, `cd` and
`cd -P` do. A read-only command such as `bd list` or `bd show` causes no
sync. A command too deeply nested to scan within a few seconds is treated as
a failed scan: the workspace roots are synced and the hook warns.

Where no workspace root has a `.beads/`, it first tests the command's text,
and neither scans nor syncs a command of one line whose text gives the scan
nothing to find: one that, its quotes and backslashes aside, holds no `bd`
in either case, none of `$`, a backquote, `{`, `}`, `*`, `?` or `[`, and no
word that runs code the text does not show, such as `eval`, `source`, `env`,
`xargs` or a shell. This tests the text, not what the command does: a script
or program the command runs, such as `./script.sh`, can still run `bd`
without the test or the scan seeing it; see [Limitations](#limitations).
Every other command is scanned, so `bd -C /other/repo close X` run there
still syncs that store. A command of several lines is always scanned, as
bash runs the lines before one it refuses.

Auggie CLI 1.2.0 renamed the shell tool from `launch-process` to `bash`, and
passes each hook the name its matcher uses. The wiring `install.sh` writes
matches `bash`; on an older CLI, set the matcher to `launch-process`. The hook
accepts either name, so wiring whose matcher still says `launch-process`,
which 1.x matches for now but flags as deprecated, keeps syncing.

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
a `launch-process` call made with `wait: false`, or in a call Auggie CLI 1.x
runs in the background, which it marks with `background: true`. Its external
store is also recorded, with the session's `conversation_id`, in the first
root's `.beads/.augment-sync.async`, which only SessionEnd reads. Each
SessionEnd moves every store listed there
back into `.augment-sync.external` and syncs it, then removes its own
session's records once the registry holds them.

The hook always exits 0. It reports problems as `beads:` warnings: on
PostToolUse as context the agent sees with the tool result, on SessionEnd on
stderr. A failed commit or push leaves the request marked, and the next sync
retries it. The wiring gives the sync hook 60 seconds and the prime hook 15.

## Installing

Install a release once per user, then wire it into Auggie's user-level
settings. `<tag>` is the latest release, from the repository's Releases
page:

```bash
git clone --depth 1 --branch <tag> https://github.com/nick-llewellyn/auggie-beads-hooks /tmp/auggie-beads-hooks
/tmp/auggie-beads-hooks/install.sh <tag> --wire
```

`install.sh <tag>` fetches the tag into
`~/.local/share/auggie-beads-hooks/<tag>` (under `$XDG_DATA_HOME` where that
is set, or `--prefix <dir>`), checks it, and points `current` beside it at
that release. `--wire` then writes three entries into
`~/.augment/settings.json`, replacing any earlier ones for these hooks and
keeping the rest of the file, which it leaves as `settings.json.bak`:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "<home>/.local/share/auggie-beads-hooks/current/.augment/hooks/beads-prime.sh", "timeout": 15000}]}
    ],
    "PostToolUse": [
      {"matcher": "bash", "hooks": [{"type": "command", "command": "<home>/.local/share/auggie-beads-hooks/current/.augment/hooks/beads-sync.sh", "timeout": 60000}]}
    ],
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "<home>/.local/share/auggie-beads-hooks/current/.augment/hooks/beads-sync.sh", "timeout": 60000}]}
    ]
  }
}
```

On Auggie CLI before 1.2.0, change the PostToolUse matcher to
`launch-process`; see
[PostToolUse and SessionEnd](#posttooluse-and-sessionend-beads-syncsh).

Auggie runs these hooks in every workspace. In one with no `.beads/`, they
change nothing unless a command writes a store elsewhere, and the sync hook
scans only a command that might run `bd` or has several lines. Any other
shell command it lets through in a few tens of milliseconds; see
[PostToolUse and SessionEnd](#posttooluse-and-sessionend-beads-syncsh).

Keep no other wiring of the hooks: Auggie runs every matching hook from
every settings file, so a project that also wires them runs them twice. To
move a project off its own copy, see
[Moving a project off a copy](#moving-a-project-off-a-copy).

`install.sh --status` shows the current release, the installed ones and the
wiring; `install.sh --help` lists the options.

### Setting up a project

1. Initialise Beads if the project does not use it yet: `bd init`. Add
   `--skip-agents` to leave out the Claude Code, Codex and Cursor files it
   would otherwise generate.
2. Choose where the store syncs; see the next section.
3. Ignore the hook's state files; see
   [Git ignores](#git-ignores-for-augment-sync-state).

A new Auggie session in the project should then open with the Beads workflow
context, except under Auggie CLI before 1.2.0 (see
[Limitations](#limitations)).
You can also run the hooks by hand:

```bash
H=~/.local/share/auggie-beads-hooks/current/.augment/hooks
AUGMENT_PROJECT_DIR="$PWD" "$H/beads-prime.sh" </dev/null | head
jq -n --arg w "$PWD" '{hook_event_name: "SessionEnd", workspace_roots: [$w]}' |
  "$H/beads-sync.sh"
```

The second command syncs the store, and prints nothing when it succeeds.

### Updating and rolling back

Run `install.sh` with the new tag, from the current release or from a clone
of the new one:

```bash
~/.local/share/auggie-beads-hooks/current/install.sh <new-tag>
```

It installs the release beside the others and moves `current`, so the wiring
needs no change. Installing a release that is already installed only moves
`current`, which is how to roll back. Releases before `v0.2.0` have no
`install.sh`, so after rolling back to one, update from a clone of the
repository. Delete a release no longer needed with
`rm -rf ~/.local/share/auggie-beads-hooks/<tag>`. Each release's notes say
whether it needs anything beyond installing; see [Versioning](#versioning).

### Moving a project off a copy

Earlier versions of this README had each project hold its own copy of
`.augment/hooks/`, or run a checkout's scripts by path. To move such a
project to the installed release:

1. Install and wire a release as above.
2. In one change to the project:
   - delete the files this repository supplies, which are the same in every
     version, and remove any directory that leaves empty:

     ```text
     .augment/hooks/beads-prime.sh
     .augment/hooks/beads-sync.sh
     .augment/hooks/lib/command-scan.sh
     .augment/hooks/lib/command_scan.py
     .augment/hooks/test/classification_test.sh
     .augment/hooks/test/lock_test.sh
     .augment/hooks/test/prime_test.sh
     ```

     Any other file under `.augment/hooks/` is the project's own: keep it,
     and its wiring;
   - remove the three hook entries from `.augment/settings.json`, or from
     `.augment/settings.local.json`, and the file if nothing else is left in
     it;
   - remove the hook test job, whether a copy of `auggie-hooks.yaml` or jobs
     folded into another workflow, and any required status check on it,
     such as `Auggie hook tests gate`;
   - keep the `.augment-sync.*` ignore, or add it; see
     [Git ignores](#git-ignores-for-augment-sync-state).
3. On Auggie CLI 1.2.0 or later, remove any workspace rule that runs
   `bd prime`, or the context reaches the agent twice.

`/hooks` in an Auggie session in the project should then list one
SessionStart, one PostToolUse and one SessionEnd entry for these hooks, all
from the user-level settings.

A fix found while working in a project is made here, and reaches the project
in a release.

### Versioning

Releases are annotated tags `vMAJOR.MINOR.PATCH` on `main`, each listed on
the Releases page with its notes. From 1.0.0:

- **Patch:** fixes. Installing is all an update needs.
- **Minor:** new behaviour, such as reading a new Auggie event field.
  Installing is still all an update needs.
- **Major:** a change that needs more than installing: to the wiring (its
  events, matcher or commands) or the install layout, or to a minimum
  version in [Requirements](#requirements). The release notes say what to
  do.

Before 1.0.0, a change that needs more than installing bumps the minor
version instead, and its release notes say what to do. Only the latest
release is fixed; there are no backports.

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
5. Keep `dolt_server_port` in `.beads/metadata.json`. `bd` 1.3.4 reports
   the field as deprecated, warning that it "can cause cross-project data
   leakage", and names the git-ignored `.beads/dolt-server.port` as the
   primary source. Without either, `bd` starts a server of its own on a
   random port.

Further clones on the same machine share the database and need no `bd init`.
Git does not copy `core.hooksPath` into a clone, so if the project tracks the
`bd` hook shims in `.beads/hooks/`, point each further clone at them once:

```bash
git config core.hooksPath .beads/hooks
```

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
# Auggie Beads sync hook state (beads-sync.sh)
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

In a container, run the suites as a user other than root and with an init
process (`docker run --init`). The lock suite makes `.beads` read-only,
which root ignores, and expects a killed hook's orphaned children to be
reaped, which needs an init.

## CI with GitHub Actions

`.github/workflows/auggie-hooks.yaml` has two jobs:

- **`changes`** compares the pushed or proposed commits with their base, and
  reports whether `.augment/`, `install.sh` or the workflow itself changed.
  When the base cannot be read (a manual run, a new branch, a force push), it
  reports a change. It uses `git diff` rather than a third-party filter
  action.
- **`auggie-hooks`** runs only when `changes` reports a change, on
  `ubuntu-latest` and `macos-latest`. It installs `shfmt` (on Ubuntu, the
  v3.12.0 release binary, checked against its SHA-256; on macOS, Homebrew's
  latest, so the two legs cover different versions), checks that the entry
  points and the installer are executable and every script parses, installs
  and wires the checkout itself with `install.sh` in scratch directories,
  and runs the three suites under `/bin/bash`.

Each leg takes 8 to 10 minutes, which is what the path filter is for.

## Limitations

- The hooks run only under the Auggie CLI. In a host that does not run
  Auggie's hooks, bead writes are neither committed nor pushed by
  these hooks; run `bd dolt commit`, and `bd dolt push --remote origin` where
  the store pushes, before ending such a session.
- Auggie CLI before 1.2.0, 0.36.0 among them, runs the SessionStart hook
  but discards its output, so the `bd prime` context does not reach the
  agent
  ([augmentcode/auggie#181](https://github.com/augmentcode/auggie/issues/181),
  fixed in 1.2.0). On such a version, use a workspace rule, which Auggie
  adds to every prompt: a file in `.augment/rules/` that tells the agent to
  run `bd prime` at the start of a session. A store with a custom
  `.beads/PRIME.md` can instead make `.augment/rules/beads-prime.md` the
  real file and `.beads/PRIME.md` a symlink to it; `bd prime` follows the
  link, but Auggie skips rule files that are symlinks. That rule carries the
  `PRIME.md` text only; `bd prime` adds the stored memories. On 1.2.0 or
  later, remove such a rule, or the context reaches the agent twice. Where
  `.beads/PRIME.md` is a symlink to the rule, first replace the link with
  a copy of the rule's text, or `bd prime` loses the custom context.
- A store the scanner cannot name is reported in a warning rather than
  synced: `bd -C "$X"` where `$X` is known only at run time; an argument
  known only at run time before `--`, as in `bd close "$ID"`, which may be
  an option such as `--db=<path>`; a plain `bd` in a directory the event did
  not report; or script run through `eval`.
- The hook reads only the command's own text. A `bd` run by a script or
  program the command starts, such as `./script.sh`, `make` or `npm test`,
  is not seen, whether or not the workspace has a store. A write to a
  workspace root's store is still synced at SessionEnd at the latest; a
  write to any other store stays unsynced, so run `bd dolt commit` there,
  and `bd dolt push --remote origin` where the store pushes, by hand.
- Auggie CLI 1.2.0 was seen running shell commands in zsh on macOS, where
  zsh is the default login shell, while the scanner reads every command
  with bash's rules
  ([#66](https://github.com/nick-llewellyn/auggie-beads-hooks/issues/66)).
  A command that relies on a difference between the two shells may be read
  as writing a store other than the one it writes; how often that happens
  has not been measured.
- Only `&`, `coproc`, process substitution, a wrapper that detaches its
  command, such as `setsid -f` or `sudo -b`, a `launch-process` call made
  with `wait: false`, and an Auggie CLI 1.x call with `background: true`
  count as background. A `wait: true` call that returns at
  `max_wait_seconds` with its command still running is not recognised, and
  a background write still running when the session ends is not waited
  for.
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
- **No Beads context at session start**: expected under Auggie CLI before
  1.2.0; see [Limitations](#limitations). Otherwise, check that the session
  runs under the Auggie CLI, that `install.sh --status` shows a current
  release and its three wired entries, and that the workspace has a
  `.beads/`.
- **A sync keeps waiting on a lock**: a lock whose owner has died is
  reclaimed automatically, but a `bd` the hook started keeps the lock until
  it exits. If no `bd` is running, delete `.beads/.augment-sync.lock`.

## Removing the hooks

`install.sh --unwire` removes the three entries from
`~/.augment/settings.json`. Then delete `~/.local/share/auggie-beads-hooks/`,
and any `.beads/.augment-sync.*` files in projects.

## License

[MIT](LICENSE)
