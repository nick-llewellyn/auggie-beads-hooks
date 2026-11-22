#!/usr/bin/env bash
# SessionStart hook: optionally pulls from a configured Dolt remote and injects
# `bd prime` context for Auggie CLI or Antigravity (`--agent antigravity`).
# Always exits 0 so startup is never blocked.

set -uo pipefail
unset CDPATH

HOOK_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

# Anchor `bd` before changing directories so relative PATH entries still resolve.
BD=$(command -v bd 2>/dev/null || true)
case "$BD" in
  /*) export BEADS_HOOK_BD="$BD" ;;
  */*) export BEADS_HOOK_BD="$PWD/$BD" ;;
  *) unset BEADS_HOOK_BD ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "beads: python3 not found; SessionStart priming skipped." >&2
  case " $* " in
    *" --agent antigravity "*|*" --agent=antigravity "*) printf '{}\n' ;;
  esac
  exit 0
fi

exec python3 -B "$HOOK_DIR/lib/beads_hook.py" prime "$@"
