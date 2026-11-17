#!/usr/bin/env bash
# Decide whether a shell command writes to a bead store, and which store.
#
# Sourced by beads-sync.sh and by test/classification_test.sh, so the suite
# exercises the scanner the hook actually runs. A suite carrying its own copy
# of the rules passes against logic the hook no longer has, which is the
# failure mode that let three rounds of defects through.
#
# The decision itself is in command_scan.py, over the syntax tree `shfmt
# --to-json` produces. This file is the bash contract around it: scan_command
# sets SCAN_MUTATES, SCAN_UNRESOLVED, SCAN_TARGETS, SCAN_ASYNC and SCAN_FAILED,
# which is what the hook and the suite are written against, and
# scan_may_run_bd says, from a command's text alone, whether a scan could find
# anything in it at all. Both `shfmt` and
# `python3` are required; a missing one is a broken install rather than a case
# to degrade through, and standing down quietly would leave bead writes local
# with nothing to say so.

_SC_LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_SC_PY="$_SC_LIB_DIR/command_scan.py"

# Results of scan_command.
SCAN_MUTATES=0
SCAN_UNRESOLVED=0
SCAN_TARGETS=()
SCAN_ASYNC=()
SCAN_FAILED=0

# Fills SCAN_MUTATES, SCAN_UNRESOLVED, SCAN_TARGETS, SCAN_ASYNC and SCAN_FAILED
# from command $1, which was launched from directory $2. An empty $2 says that
# directory is unknown, which leaves a relative `-C` target unresolved rather
# than resolving it against this process's own cwd -- a directory nothing
# requires to be the tool call's. $3 `unwaited` says the launch returned as
# the command started, so every target it writes is one SCAN_ASYNC holds.
#
# SCAN_ASYNC is the targets written by a command the shell does not wait for,
# which the scanner marks with a leading `&`. Each is in SCAN_TARGETS too, as
# the write may already have landed -- except one marked `&+`, a directory
# such a `bd` walks up from rather than a store, whose store may not be made
# yet. That stays in SCAN_ASYNC alone, its `+` kept, for SessionEnd to walk
# up from; see `take_async`.
#
# Targets are read back NUL-separated rather than by line, because a store path
# may contain a newline and a target silently truncated at one names some other
# store.
#
# The scanner's output is captured to a file rather than read from a process
# substitution, because a substitution's exit status is not the pipeline's and
# so cannot be observed at all. A scanner that dies before emitting anything
# leaves SCAN_MUTATES at its initial 0, which reads exactly like "this command
# writes nothing": the write is then treated as read-only and no sync is even
# attempted, silently. SCAN_FAILED distinguishes the two, so the caller can say
# so instead of standing down.
scan_command() {
  local command="$1" launch_cwd="$2" mode="${3:-}" field seen=0 out status
  SCAN_MUTATES=0
  SCAN_UNRESOLVED=0
  SCAN_TARGETS=()
  SCAN_ASYNC=()
  SCAN_FAILED=0
  out=$(mktemp "${TMPDIR:-/tmp}/beads-hook-scan.XXXXXX") || {
    SCAN_FAILED=1
    return 0
  }
  printf '%s' "$command" |
    python3 "$_SC_PY" "$launch_cwd" ${mode:+"$mode"} >"$out" 2>/dev/null
  status=$?
  # The two counts come first and always; everything past them is a target.
  while IFS= read -r -d '' field; do
    seen=$((seen + 1))
    case "$seen" in
      1) SCAN_MUTATES=$field ;;
      2) SCAN_UNRESOLVED=$field ;;
      *)
        case "$field" in
          '&+'*) SCAN_ASYNC+=("${field#&}") ;;
          '&'*)
            SCAN_ASYNC+=("${field#&}")
            SCAN_TARGETS+=("${field#&}")
            ;;
          *) SCAN_TARGETS+=("$field") ;;
        esac
        ;;
    esac
  done <"$out"
  rm -f "$out"
  # A nonzero status and a short answer are both failures, and neither implies
  # the other: the scanner can exit 0 having written nothing if its stdout was
  # lost, and can exit nonzero after a complete answer. Both counts are required,
  # since a truncated answer would otherwise leave the second reading 0 --
  # indistinguishable from "every target was resolved".
  if [ "$status" -ne 0 ] || [ "$seen" -lt 2 ]; then
    SCAN_FAILED=1
    SCAN_MUTATES=0
    SCAN_UNRESOLVED=0
    SCAN_TARGETS=()
    SCAN_ASYNC=()
  fi
  return 0
}

# What scan_may_run_bd looks for, built once, as this file is sourced. A quote
# or a backslash between two letters leaves them one word -- `b''d` and `b\d`
# both run `bd` -- so any run of those may stand between the letters of a name.
# Letters match in either case, as a case-insensitive filesystem finds the
# program.
_SC_JOIN="['\"\\\\]*"
_SC_RUNS_BD="[bB]$_SC_JOIN[dD]"
_SC_UPPER=ABCDEFGHIJKLMNOPQRSTUVWXYZ
_SC_LOWER=abcdefghijklmnopqrstuvwxyz
_SC_NAMES=""
for _sc_name in eval source . exec command builtin env xargs sh bash zsh dash \
  ksh mksh busybox; do
  _sc_re=""
  while [ -n "$_sc_name" ]; do
    _sc_c=${_sc_name:0:1}
    _sc_name=${_sc_name:1}
    if [ "$_sc_c" = . ]; then
      _sc_c='\.'
    else
      _sc_i=${_SC_LOWER%%"$_sc_c"*}
      _sc_c="[$_sc_c${_SC_UPPER:${#_sc_i}:1}]"
    fi
    _sc_re="$_sc_re$_SC_JOIN$_sc_c"
  done
  _SC_NAMES="${_SC_NAMES:+$_SC_NAMES|}$_sc_re$_SC_JOIN"
done
_SC_RUNS_WORD="(^|[[:space:];&|()<>/])($_SC_NAMES)([[:space:];&|()<>]|\$)"
unset _sc_name _sc_re _sc_c _sc_i

# Whether command $1 might run a `bd`, read from its text alone and without
# starting a process. The hook asks this before scanning in a workspace with no
# bead store, where the scan is most of what it costs, so it errs towards yes.
# It says no only where the text is one line, as bash runs the lines before
# one it rejects, so the scan's word on a line it cannot read stands; and,
# its quotes and backslashes aside, holds no `bd` in either case; none of `$`,
# a backquote, `{`, `}`, `*`, `?` or `[`, through which an expansion, a
# substitution or a glob could spell one; and no word, with or without a path,
# that runs code the text does not show: `eval`, `source`, `.`, `exec`,
# `command`, `builtin`, `xargs`, a shell, or `env`, whose `-S` decodes escapes
# into a command. The scan stays the only judge of what a command writes; this
# only says where it could find nothing. A script or program the command starts
# can still run a `bd`, which neither reads.
#
# Matched with regexes rather than by removing the quotes first, which bash 3.2
# does in time that grows with the square of the text: seconds for a few
# kilobytes of quotes. Read bytewise, so a command that is not valid UTF-8
# still matches, and a match that fails outright counts as a yes.
scan_may_run_bd() {
  local LC_ALL=C
  case "$1" in
    *$'\n'* | *'$'* | *'`'* | *'{'* | *'}'* | *'*'* | *'?'* | *'['*)
      return 0
      ;;
  esac
  [[ $1 =~ $_SC_RUNS_BD ]]
  [ $? -eq 1 ] || return 0
  [[ $1 =~ $_SC_RUNS_WORD ]]
  [ $? -eq 1 ] || return 0
  return 1
}
