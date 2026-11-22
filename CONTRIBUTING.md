# Contributing: filing issues

This guide sets out what we expect in issues filed for these hooks, by people
and by AI agents. "Must" marks a rule; the rest is guidance.

## Where to file

- File issues on GitHub:
  <https://github.com/nick-llewellyn/auggie-beads-hooks/issues>. It is the
  repository's only public tracker, and anyone can file there.
- The maintainer also tracks work in a Beads store with the prefix `abh`. The
  store is local to the maintainer's machine and has no remote, so only the
  maintainer, and agents working in the maintainer's sessions, can write to
  it. Each GitHub issue the maintainer takes on gets a matching bead; see
  [The matching bead](#the-matching-bead).
- This repository is public. Issues must leave out code, paths, names, bead
  IDs and other details of private projects. Say where a problem was found in
  general terms, for example "Found in a downstream workspace."

## Before filing

1. **Search** the existing issues, open and closed. If one matches, comment
   on it instead of opening a duplicate:

   ```bash
   gh issue list -R nick-llewellyn/auggie-beads-hooks --state all --search "<terms>"
   ```

2. **Check the README.** Behaviour described under
   [How it works](README.md#how-it-works) (for example, skipping Dolt pulls
   and pushes on local-only or remote-less stores, or best-effort static
   tokenization in `beads-guard.sh`) is expected unless the issue argues
   otherwise.
3. **Reproduce it in scratch directories with a stubbed `bd`**, as the test
   suite does (see [Running the tests](README.md#running-the-tests)). Do not
   reproduce against a real store.
4. **Note the versions:** the hooks' release and commit, `bd`, the agent CLI
   (Auggie CLI and/or Antigravity CLI), the OS, `/bin/bash` and `python3`.

## Filing an issue

Write the body to a file, so that backticks, `$` and quotes reach GitHub
unchanged, then file it:

```bash
gh issue create -R nick-llewellyn/auggie-beads-hooks \
  --title "<Part>: <what it does wrong>" \
  --label bug \
  --body-file issue.md
```

### Title

One line that states the defect or request plainly. Name the part first, in
this repository's terms: `beads-prime.sh`, `beads-guard.sh`, `beads-sync.sh`,
`beads_hook.py`, `shell_tokens.py`, `wiring.py`, `install.sh`, `hook_test.py`
or `Main's CI`. Put code in backticks. For example:

- ``beads-prime.sh: cannot find `bd` through a relative PATH entry``
- ``beads-guard.sh: false positive when `bd edit` appears inside a here-string``
- ``beads-sync.sh: passes implicit auto-push through read-only `bd` probes``

### Label

Each issue must carry exactly one of these labels:

- `bug`: the hooks, tokenizer, wiring helper, installer, test suite or CI do
  something wrong.
- `enhancement`: a new capability, or an improvement such as speed or clarity.
- `documentation`: the README, docstrings or this guide is wrong or
  incomplete.

The maintainer sets `duplicate` when closing a repeat.

### Body

Use these sections, in this order:

- `## Summary` (required): what goes wrong, under what conditions, and since
  when if known ("Since #86 (be8959b)"). Relate it to earlier issues by
  number. End with one line on how it was found.
- `## Reproduce` (required for a bug): see
  [What a bug report must include](#what-a-bug-report-must-include).
- `## Cause` (optional): where in the code it goes wrong, and why.
- `## Fix` (required): the proposed change, and the `hook_test.py` cases
  (verified with `/bin/bash .augment/hooks/test/run_tests.sh`) that would pin
  it. This section is the issue's acceptance criteria. If you have no fix to
  propose, say so.
- `## Impact` (required): start with the level, `Low.`, `Medium.` or
  `High.`, then say who is affected, what is lost and any workaround.

An enhancement uses the same sections. A request with nothing to reproduce,
such as a documentation review, may use `## Summary`, `## Findings`,
`## Scope` and `## Done when` instead.

## What a bug report must include

In `## Reproduce`:

1. **Versions** on one line, for example: `bd 1.3.4 (Homebrew), Auggie CLI
   1.2.2, Antigravity 1.5.14, hooks v0.3.0 (be8959b), macOS, /bin/bash
   3.2.57, Python 3.13`.
2. **A minimal case** as a `console` block, run in scratch directories
   (`mktemp -d`) with a stubbed `bd` that logs its calls. To run a hook on
   its own, pipe it the event as JSON (`hook_event_name`, `workspace_roots`
   or `cwd`, `tool_input`), passing `--agent antigravity` when reproducing an
   Antigravity-specific path.
3. **The output as printed**, with any `beads:` warning verbatim and the exit
   code. Shorten local paths to `…/` or `path/to/`.
4. **Expected and actual behaviour**, if the output alone does not make the
   difference plain.

## AI agents

Agents filing issues here must also:

- run every step of [Before filing](#before-filing), and name any related
  issue the search found;
- show the draft title, label and body to the user they work for, and file
  only after the user approves it;
- file with `gh issue create --body-file`, never `--body`;
- file one defect per issue, and cross-reference related issues by number;
- leave out private project details, as above;
- create the matching bead only when working in this repository with access
  to its store.

## The matching bead

For the maintainer, and agents working in this repository with access to its
store. Each GitHub issue taken on gets one bead:

```bash
bd create --type=bug --priority=2 --labels=bug \
  --external-ref=gh-<N> \
  --title="Fix #<N>: <the issue's title>" \
  --description="GitHub #<N>: https://github.com/nick-llewellyn/auggie-beads-hooks/issues/<N>. Filed <YYYY-MM-DD>. <What goes wrong, the proposed fix, and the cases that pin it.>" \
  --acceptance="<The checks that show it is fixed, such as new hook_test.py cases passing under /bin/bash .augment/hooks/test/run_tests.sh.>"
```

Then comment on the issue: ``Tracked as bead `abh-<id>`.``

- **Parent** (optional): pass `--parent=<epic-id>` when the work belongs to
  an active epic.
- **Type:** from the GitHub label: `bug` gives `bug`, `enhancement` gives
  `feature`, `documentation` gives `task`. Other planned work, such as a
  release or a probe, is a `task`; housekeeping is a `chore`; a plan that
  groups beads is an `epic`.
- **Priority:** `0` (highest) to `4`; the default is `2`.
  - `1`: epics, and work that gates a release.
  - `2`: most fixes.
  - `3`: minor fixes, documentation and probes.
  - `4`: watch items.
  - `0`: critical problems only.
- **Labels:** the GitHub issue's label.
- **External ref:** `gh-<N>`.
- **Acceptance criteria:** required. Restate the issue's `## Fix` cases as
  checks under `/bin/bash .augment/hooks/test/run_tests.sh`.
- **Notes** (optional): progress, such as the branch, PR, merge commit and
  decisions.

## Linking

- **On GitHub:** refer to issues by number (`#86`), commits by short SHA
  (`be8959b`), and another project's issue as `owner/repo#123`. A PR that
  fixes an issue says `Closes #<N>` in its body; one that does part of it
  says `Part of #<N>`.
- **Blocking beads:** when one piece of work must wait for another, run
  `bd dep add <blocked-id> <blocker-id>`. A release waits for the fixes it
  ships, for example.
- **Related beads:** `bd dep add <id> <other-id> --type related`, for links
  with no ordering.

## Example

Title: ``beads-guard.sh: blocks `bd edit` when it appears as a here-string literal``.
Label: `bug`.

````markdown
## Summary

`beads-guard.sh` inspects shell commands with `shell_tokens.py` to block
interactive `bd edit` invocations. If a command passes `bd edit` as the word
after a here-string operator (`<<<`), treating `<<<` as a redirection
delimiter instead of consuming its word causes a false-positive deny.

Found while reviewing shell-tokenization edge cases.

## Reproduce

Versions: bd 1.3.4 (Homebrew), Antigravity 1.5.14, hooks v0.3.0 (be8959b),
macOS, /bin/bash 3.2.57, Python 3.13.

```console
$ printf '%s\n' '{"tool_input":{"command":"cat <<< \"bd edit abh-1\""}}' | \
>   /bin/bash path/to/.augment/hooks/beads-guard.sh --agent antigravity
{"decision": "deny", "reason": "beads: 'bd edit' opens an interactive $EDITOR ..."}
```

Expected: empty stdout and exit 0 (allow), because `bd edit abh-1` is the
here-string payload rather than an executed command.
Actual: JSON deny response.

## Cause

`shell_tokens.py` splits on `<<<` without marking the following token as a
redirection operand.

## Fix

In `.augment/hooks/lib/shell_tokens.py`, consume the word following `<<<` as
a here-string operand rather than a command word. Add regression cases in
`.augment/hooks/test/hook_test.py` covering `<<< "bd edit ..."` (allow) and
`cat <<< ok && bd edit abh-1` (deny), verified with
`/bin/bash .augment/hooks/test/run_tests.sh`.

## Impact

Low. Causes a false-positive guard block when echoing or piping literal
`bd edit` text through a here-string; workaround is passing the text via a
quoted heredoc or `printf`.
````

The matching bead:

```bash
bd create --type=bug --priority=2 --labels=bug \
  --external-ref=gh-<N> \
  --title="Fix #<N>: beads-guard.sh blocks bd edit inside a here-string" \
  --description="GitHub #<N>: https://github.com/nick-llewellyn/auggie-beads-hooks/issues/<N>. Filed <YYYY-MM-DD>. shell_tokens.py treats a here-string payload after <<< as a command word, causing beads-guard.sh to deny literal 'bd edit' text." \
  --acceptance="New hook_test.py cases for here-string allow and chained command deny pass under /bin/bash .augment/hooks/test/run_tests.sh."
```
