#!/usr/bin/env bash
# Installs a release of the Beads hooks for the current user, and wires it into
# Auggie CLI (`~/.augment/settings.json`) and/or Antigravity
# (`~/.gemini/config/hooks.json`), so no project needs its own copy.
#
# Each release goes in PREFIX/TAG, a shallow checkout of the tag, and
# PREFIX/current links to the one in use. The wiring names PREFIX/current, so
# installing another compatible release, or rolling back, moves the link
# atomically without touching settings again.
set -euo pipefail
unset CDPATH

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
PREFIX=${AUGGIE_BEADS_HOOKS_PREFIX:-${XDG_DATA_HOME:-$HOME/.local/share}/auggie-beads-hooks}
SOURCE=${AUGGIE_BEADS_HOOKS_SOURCE:-https://github.com/nick-llewellyn/auggie-beads-hooks.git}
SETTINGS=$HOME/.augment/settings.json
ANTIGRAVITY_HOOKS=$HOME/.gemini/config/hooks.json
AGENT=auggie

usage() {
  cat <<'EOF'
Usage:
  install.sh [options] TAG [--wire]  install release TAG and make it current
  install.sh [options] --wire        route hook events to the current release
  install.sh [options] --unwire      remove those routes
  install.sh [options] --status      show the installed releases and the wiring

Options:
  --prefix DIR              where releases go (default: $XDG_DATA_HOME/auggie-beads-hooks,
                            or ~/.local/share/auggie-beads-hooks)
  --source URL              the repository to clone a release from (default: GitHub)
  --agent AGENT             auggie, antigravity, or all (default: auggie)
  --settings FILE           the Auggie settings file to wire (default: ~/.augment/settings.json)
  --antigravity-hooks FILE  the Antigravity hooks file to wire (default: ~/.gemini/config/hooks.json)

TAG is a release tag, such as v0.3.0.
EOF
}

die() {
  echo "install.sh: $*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

TAG=
WIRE=0
UNWIRE=0
STATUS=0
while [ $# -gt 0 ]; do
  case $1 in
    --prefix | --source | --settings | --antigravity-hooks | --agent)
      [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"
      case $1 in
        --prefix) PREFIX=$2 ;;
        --source) SOURCE=$2 ;;
        --settings) SETTINGS=$2 ;;
        --antigravity-hooks) ANTIGRAVITY_HOOKS=$2 ;;
        --agent) AGENT=$2 ;;
      esac
      shift 2
      ;;
    --wire) WIRE=1; shift ;;
    --unwire) UNWIRE=1; shift ;;
    --status) STATUS=1; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) die "unknown option $1 (see --help)" ;;
    *)
      [ -z "$TAG" ] || die "give one tag"
      TAG=$1
      shift
      ;;
  esac
done

case $AGENT in
  auggie | antigravity | all) ;;
  *) die "unknown --agent '$AGENT' (expected auggie, antigravity, or all)" ;;
esac

[ -n "$TAG" ] || [ $((WIRE + UNWIRE + STATUS)) -gt 0 ] || { usage >&2; exit 2; }
[ $((WIRE + UNWIRE + STATUS)) -le 1 ] || die "give one of --wire, --unwire and --status"
[ -z "$TAG" ] || [ $((UNWIRE + STATUS)) -eq 0 ] || die "a tag goes with --wire only"

# Validates a release directory against the current Python-core hook layout.
check_release() {
  local hooks=$1/.augment/hooks f
  [ -x "$hooks/beads-prime.sh" ] && [ -x "$hooks/beads-sync.sh" ] && [ -x "$hooks/beads-guard.sh" ] || return 1
  [ -f "$hooks/lib/beads_hook.py" ] && [ -f "$hooks/lib/wiring.py" ] && [ -f "$hooks/lib/shell_tokens.py" ] || return 1
  for f in "$hooks"/*.sh; do
    bash -n "$f" 2>/dev/null || return 1
  done
  python3 -B -c '
import sys
for p in sys.argv[1:]:
    compile(open(p, "rb").read(), p, "exec")
' "$hooks/lib/shell_tokens.py" "$hooks/lib/beads_hook.py" "$hooks/lib/wiring.py" 2>/dev/null || return 1

  if [ -e "$1/install.sh" ]; then
    [ -x "$1/install.sh" ] && bash -n "$1/install.sh" 2>/dev/null || return 1
  fi
}

find_wiring_helper() {
  if [ -f "$SCRIPT_DIR/.augment/hooks/lib/wiring.py" ]; then
    printf '%s\n' "$SCRIPT_DIR/.augment/hooks/lib/wiring.py"
  elif [ -L "$PREFIX/current" ] && [ -f "$PREFIX/current/.augment/hooks/lib/wiring.py" ]; then
    printf '%s\n' "$PREFIX/current/.augment/hooks/lib/wiring.py"
  else
    die "wiring helper not found (.augment/hooks/lib/wiring.py)"
  fi
}

# Points PREFIX/current at $1 in one rename, so a hook starting meanwhile finds
# the old release or the new one, never no link at all.
link_current() {
  python3 -B - "$PREFIX" "$1" <<'PY'
import os
import sys

prefix, tag = sys.argv[1:]
new = os.path.join(prefix, ".current.%d" % os.getpid())
os.symlink(tag, new)
os.replace(new, os.path.join(prefix, "current"))
PY
}

install_release() {
  case $TAG in
    */* | *[[:space:]]* | .*) die "'$TAG' is not a release tag" ;;
    v[0-9]*.[0-9]*.[0-9]*) ;;
    *) die "'$TAG' is not a release tag (vMAJOR.MINOR.PATCH)" ;;
  esac
  need git
  need python3
  mkdir -p "$PREFIX"
  PREFIX=$(cd "$PREFIX" && pwd -P)
  local dest=$PREFIX/$TAG helper
  if [ -e "$dest" ]; then
    check_release "$dest" ||
      die "$dest is not a complete install of $TAG; remove it and run again"
    helper=$(find_wiring_helper)
    python3 -B "$helper" check-compat \
      --prefix "$PREFIX" \
      --agent "$AGENT" \
      --settings "$SETTINGS" \
      --antigravity-hooks "$ANTIGRAVITY_HOOKS" \
      --will-wire "$WIRE" \
      --target-tag "$TAG"
    echo "$TAG is already installed in $dest"
  else
    TMP=$(mktemp -d "$PREFIX/.install.XXXXXX")
    trap 'rm -rf "$TMP"' EXIT
    git -c init.defaultBranch=main init -q "$TMP/$TAG"
    git -C "$TMP/$TAG" fetch -q --depth 1 "$SOURCE" "refs/tags/$TAG:refs/tags/$TAG" ||
      die "could not fetch the tag $TAG from $SOURCE"
    git -C "$TMP/$TAG" -c advice.detachedHead=false checkout -q --detach "$TAG" ||
      die "could not check out $TAG"
    check_release "$TMP/$TAG" ||
      die "$TAG fails the release check: an entry point is missing or not executable, or a script does not parse"
    helper=$(find_wiring_helper)
    python3 -B "$helper" check-compat \
      --prefix "$PREFIX" \
      --agent "$AGENT" \
      --settings "$SETTINGS" \
      --antigravity-hooks "$ANTIGRAVITY_HOOKS" \
      --will-wire "$WIRE" \
      --target-tag "$TAG"
    python3 -B -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' \
      "$TMP/$TAG" "$dest" 2>/dev/null ||
      die "$dest appeared while $TAG was being fetched, perhaps from another install; run again"
    rm -rf "$TMP"
    trap - EXIT
    echo "installed $TAG in $dest"
  fi
  link_current "$TAG"
  echo "current release: $TAG"
}

wire() {
  need python3
  [ -d "$PREFIX" ] && PREFIX=$(cd "$PREFIX" && pwd -P)
  [ -L "$PREFIX/current" ] && check_release "$PREFIX/current" ||
    die "no release is installed in $PREFIX; run install.sh TAG first"
  [ -x "$PREFIX/current/.augment/hooks/beads-guard.sh" ] ||
    die "current release does not include beads-guard.sh; install a newer release before wiring"
  local helper
  helper=$(find_wiring_helper)
  python3 -B "$helper" wire \
    --agent "$AGENT" \
    --settings "$SETTINGS" \
    --antigravity-hooks "$ANTIGRAVITY_HOOKS" \
    --hooks-dir "$PREFIX/current/.augment/hooks"
  if [ "$AGENT" = auggie ] || [ "$AGENT" = all ]; then
    echo "Remove any other wiring of these hooks, such as a project's" \
      ".augment/settings.json entries: Auggie runs every matching hook, so" \
      "two wirings run the hooks twice."
  fi
}

unwire() {
  need python3
  [ -d "$PREFIX" ] && PREFIX=$(cd "$PREFIX" && pwd -P)
  local helper
  helper=$(find_wiring_helper)
  python3 -B "$helper" unwire \
    --agent "$AGENT" \
    --settings "$SETTINGS" \
    --antigravity-hooks "$ANTIGRAVITY_HOOKS"
}

status() {
  local entry helper
  if [ -L "$PREFIX/current" ]; then
    echo "current: $(readlink "$PREFIX/current") ($PREFIX)"
  else
    echo "current: none ($PREFIX)"
  fi
  if [ -d "$PREFIX" ]; then
    for entry in "$PREFIX"/v*; do
      [ -d "$entry" ] && [ ! -L "$entry" ] && echo "installed: ${entry##*/}"
    done
  fi
  need python3
  helper=$(find_wiring_helper)
  python3 -B "$helper" status \
    --agent "$AGENT" \
    --settings "$SETTINGS" \
    --antigravity-hooks "$ANTIGRAVITY_HOOKS"
}

if [ -n "$TAG" ]; then
  install_release
fi
if [ "$WIRE" -eq 1 ]; then
  wire
elif [ "$UNWIRE" -eq 1 ]; then
  unwire
elif [ "$STATUS" -eq 1 ]; then
  status
fi
