#!/usr/bin/env bash
# Runs syntax checks and the Python unittest suite for Beads hooks.
set -euo pipefail
unset CDPATH

TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
HOOKS_DIR=$(cd "$TEST_DIR/.." && pwd -P)
REPO_ROOT=$(cd "$HOOKS_DIR/../.." && pwd -P)

export BASH_UNDER_TEST="${BASH:-/bin/bash}"

echo "Checking executable bits and syntax under $BASH_UNDER_TEST..."
for f in "$HOOKS_DIR/beads-prime.sh" "$HOOKS_DIR/beads-sync.sh" "$HOOKS_DIR/beads-guard.sh" "$REPO_ROOT/install.sh"; do
  [ -x "$f" ] || { echo "ERROR: $f is not executable" >&2; exit 1; }
done

for f in "$HOOKS_DIR"/*.sh "$TEST_DIR"/*.sh "$REPO_ROOT/install.sh"; do
  "$BASH_UNDER_TEST" -n "$f"
done

python3 -B -c '
import sys
for p in sys.argv[1:]:
    compile(open(p, "rb").read(), p, "exec")
' \
  "$HOOKS_DIR/lib/shell_tokens.py" \
  "$HOOKS_DIR/lib/beads_hook.py" \
  "$HOOKS_DIR/lib/wiring.py" \
  "$TEST_DIR/hook_test.py"

exec python3 -B -m unittest discover -s "$TEST_DIR" -p '*_test.py' -v
