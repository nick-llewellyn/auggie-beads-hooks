#!/usr/bin/env bash
# Installs a release of the Auggie Beads hooks for the current user, and wires
# it into Auggie's user-level settings, so no project needs its own copy.
#
# Each release goes in PREFIX/TAG, a shallow checkout of the tag, and
# PREFIX/current links to the one in use. The wiring names PREFIX/current, so
# installing another release, or one installed before, takes effect without
# touching the settings again. Installing a tag that is already installed only
# moves the link, which is how to roll back.
set -euo pipefail
# An exported CDPATH makes `cd` print the directory it finds, which
# `PREFIX=$(cd "$PREFIX" && pwd -P)` would capture beside the path.
unset CDPATH

PREFIX=${AUGGIE_BEADS_HOOKS_PREFIX:-${XDG_DATA_HOME:-$HOME/.local/share}/auggie-beads-hooks}
SOURCE=${AUGGIE_BEADS_HOOKS_SOURCE:-https://github.com/nick-llewellyn/auggie-beads-hooks.git}
SETTINGS=$HOME/.augment/settings.json
# A hook command that runs one of these scripts: the script's own name, at the
# start or after a `/`, a quote or a space, quoted or not, ending the command
# or followed by its arguments, a redirection or a separator.
# `/x/custom-beads-sync.sh` is someone else's hook.
HOOK_RE="(^|[/'\" ])beads-(prime|sync)\\.sh['\"]?(\\s|\$|[;&|)])"
# The jq test for a hook that runs one: in its command or, where the command
# is a program run with `args`, in those. An event's entry holds its hooks in
# a `hooks` list, or is one hook itself.
OURS_JQ='def ours: ((.command // "") | test($re))
  or any((.args // [])[] | strings; test($re));
def hooks_of: if type != "object" then empty
  elif (.hooks | type) == "array" then .hooks[] | objects
  else . end;'

usage() {
  cat <<'EOF'
Usage:
  install.sh [options] TAG [--wire]  install release TAG and make it current
  install.sh [options] --wire        route Auggie's hook events to the current release
  install.sh [options] --unwire      remove those routes
  install.sh [options] --status      show the installed releases and the wiring

Options:
  --prefix DIR      where releases go (default: $XDG_DATA_HOME/auggie-beads-hooks,
                    or ~/.local/share/auggie-beads-hooks)
  --source URL      the repository to clone a release from (default: GitHub)
  --settings FILE   the Auggie settings file to wire (default: ~/.augment/settings.json)

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
    --prefix | --source | --settings)
      [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"
      case $1 in
        --prefix) PREFIX=$2 ;;
        --source) SOURCE=$2 ;;
        --settings) SETTINGS=$2 ;;
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
[ -n "$TAG" ] || [ $((WIRE + UNWIRE + STATUS)) -gt 0 ] || { usage >&2; exit 2; }
[ $((WIRE + UNWIRE + STATUS)) -le 1 ] || die "give one of --wire, --unwire and --status"
[ -z "$TAG" ] || [ $((UNWIRE + STATUS)) -eq 0 ] || die "a tag goes with --wire only"

# The release a directory holds, if it has the hooks this script wires: both
# entry points executable, every shell script parsing, and the scanner
# compiling, checked without writing bytecode into the release. Its own
# install.sh, which updating and rolling back from it run, must be executable
# and parse too, where it has one; releases before v0.2.0 have none.
check_release() {
  local hooks=$1/.augment/hooks f
  [ -x "$hooks/beads-prime.sh" ] && [ -x "$hooks/beads-sync.sh" ] || return 1
  [ -f "$hooks/lib/command-scan.sh" ] && [ -f "$hooks/lib/command_scan.py" ] || return 1
  for f in "$hooks"/*.sh "$hooks"/lib/*.sh; do
    bash -n "$f" 2>/dev/null || return 1
  done
  python3 -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' \
    "$hooks/lib/command_scan.py" 2>/dev/null || return 1
  if [ -e "$1/install.sh" ]; then
    [ -x "$1/install.sh" ] && bash -n "$1/install.sh" 2>/dev/null || return 1
  fi
}

# Points PREFIX/current at $1 in one rename, so a hook starting meanwhile finds
# the old release or the new one, never no link at all.
link_current() {
  python3 - "$PREFIX" "$1" <<'PY'
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
  local dest=$PREFIX/$TAG
  if [ -e "$dest" ]; then
    check_release "$dest" ||
      die "$dest is not a complete install of $TAG; remove it and run again"
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
    # A rename that refuses a destination with anything in it: where another
    # install of the tag has published it meanwhile, `mv` put this checkout
    # inside that one and reported success.
    python3 -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' \
      "$TMP/$TAG" "$dest" 2>/dev/null ||
      die "$dest appeared while $TAG was being fetched, perhaps from another install; run again"
    rm -rf "$TMP"
    trap - EXIT
    echo "installed $TAG in $dest"
  fi
  link_current "$TAG"
  echo "current release: $TAG"
}

# A /bin/sh command line that runs the script at $1. Auggie runs a hook's
# command through the shell, so a path with a space in it is quoted.
hook_command() {
  case $1 in
    *\'*) die "cannot wire a path holding a single quote: $1" ;;
    *[!A-Za-z0-9/._+-]*) printf "'%s'" "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}

# Rewrites the settings file: every user-level hook that runs a beads-prime.sh
# or beads-sync.sh is dropped, under any event and whichever install it names,
# and with --wire the three for this one are added. Everything else in the
# file is kept, and the old file is left beside it as settings.json.bak.
write_settings() {
  local wire=$1 prime=${2:-} sync=${3:-} dir tmp
  need jq
  dir=$(dirname "$SETTINGS")
  mkdir -p "$dir"
  if [ -e "$SETTINGS" ]; then
    jq -e 'type == "object"' "$SETTINGS" >/dev/null 2>&1 ||
      die "$SETTINGS is not a JSON object; fix it and run again"
  fi
  tmp=$(mktemp "$dir/.settings.XXXXXX")
  if ! { [ -e "$SETTINGS" ] && cat "$SETTINGS" || echo '{}'; } |
    jq --argjson wire "$wire" --arg prime "$prime" --arg sync "$sync" \
      --arg re "$HOOK_RE" "$OURS_JQ"'
      def clean: if type != "object" then .
        elif (.hooks | type) == "array"
        then .hooks |= map(select(type != "object" or (ours | not)))
        elif ours then empty else . end;
      def keep: if type == "array" then map(clean)
        | map(select(type != "object" or (.hooks | type) != "array"
            or .hooks != [])) else . end;
      def hook(cmd; ms): {type: "command", command: cmd, timeout: ms};
      .hooks = ((.hooks // {}) | map_values(keep)
        | .SessionStart = (.SessionStart // [])
            + (if $wire then [{hooks: [hook($prime; 15000)]}] else [] end)
        | .PostToolUse = (.PostToolUse // [])
            + (if $wire then [{matcher: "bash", hooks: [hook($sync; 60000)]}] else [] end)
        | .SessionEnd = (.SessionEnd // [])
            + (if $wire then [{hooks: [hook($sync; 60000)]}] else [] end)
        | with_entries(select(.value != [])))
      | if .hooks == {} then del(.hooks) else . end
    ' >"$tmp"; then
    rm -f "$tmp"
    die "could not rewrite $SETTINGS"
  fi
  if [ -e "$SETTINGS" ]; then
    cmp -s "$tmp" "$SETTINGS" && { rm -f "$tmp"; echo "$SETTINGS is unchanged"; return 0; }
    cp -p "$SETTINGS" "$SETTINGS.bak"
  fi
  mv "$tmp" "$SETTINGS"
  echo "updated $SETTINGS"
}

wire() {
  need python3
  [ -d "$PREFIX" ] && PREFIX=$(cd "$PREFIX" && pwd -P)
  [ -L "$PREFIX/current" ] && check_release "$PREFIX/current" ||
    die "no release is installed in $PREFIX; run install.sh TAG first"
  local hooks=$PREFIX/current/.augment/hooks prime sync
  # Assigned on their own, so a path hook_command refuses stops the script
  # here, before the settings file is touched.
  prime=$(hook_command "$hooks/beads-prime.sh")
  sync=$(hook_command "$hooks/beads-sync.sh")
  write_settings true "$prime" "$sync"
  echo "Remove any other wiring of these hooks, such as a project's" \
    ".augment/settings.json entries: Auggie runs every matching hook, so" \
    "two wirings run the hooks twice."
}

status() {
  local entry
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
  # A file this cannot read is reported as such, not taken for one with no
  # wiring in it.
  if [ -e "$SETTINGS" ]; then
    need jq
    jq -e 'type == "object"' "$SETTINGS" >/dev/null 2>&1 ||
      die "$SETTINGS is not a JSON object; fix it and run again"
    entry=$(jq -r --arg re "$HOOK_RE" "$OURS_JQ"'(.hooks // {}) | to_entries[]
      | .key as $event | .value | arrays | .[] | hooks_of | select(ours)
      | "wired: \($event): \(.command // "")" + ((.args // []) | map(" " + tostring) | add // "")' \
      "$SETTINGS" 2>/dev/null) || die "could not read the wiring in $SETTINGS"
    [ -n "$entry" ] && printf '%s\n' "$entry" || echo "wired: none ($SETTINGS)"
  else
    echo "wired: none ($SETTINGS)"
  fi
}

if [ -n "$TAG" ]; then
  install_release
fi
if [ "$WIRE" -eq 1 ]; then
  wire
elif [ "$UNWIRE" -eq 1 ]; then
  write_settings false
elif [ "$STATUS" -eq 1 ]; then
  status
fi
