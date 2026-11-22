#!/usr/bin/env bash
# PreToolUse hook: blocks interactive `bd edit` commands that open $EDITOR and
# hang headless agent sessions, redirecting the agent to `bd update`.

set -uo pipefail
unset CDPATH

HOOK_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

if ! command -v python3 >/dev/null 2>&1; then
  echo "beads: python3 not found; PreToolUse guard skipped." >&2
  exit 0
fi

exec python3 -B "$HOOK_DIR/lib/beads_hook.py" guard "$@"
