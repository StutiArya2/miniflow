#!/usr/bin/env bash
# Copy this checkout's miniflow package into the macOS MiniFlow.app and re-sign it.
#
#   scripts/update-app.sh [--check] [APP]     APP defaults to /Applications/MiniFlow.app
#
# The app runs its own Python with a bundled copy of miniflow/ (Contents/Resources/miniflow).
# This script replaces that copy, bumps CFBundleVersion, records the source revision, and
# signs the bundle again (ad hoc, as it was built). The previous package is kept in a
# temporary folder so the update can be undone. --check only runs the tests first and stops
# if they fail; nothing in the app changes until they pass.
set -euo pipefail

check=0
if [[ "${1:-}" == "--check" ]]; then check=1; shift; fi
app="${1:-/Applications/MiniFlow.app}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
resources="$app/Contents/Resources"
plist="$app/Contents/Info.plist"

[[ "$(uname)" == "Darwin" ]] || { echo "This script updates the macOS app; run it on a Mac." >&2; exit 1; }
[[ -d "$resources/miniflow" && -f "$plist" ]] || { echo "No MiniFlow app at $app" >&2; exit 1; }
[[ -f "$repo/miniflow/__init__.py" ]] || { echo "Run this from a MiniFlow checkout." >&2; exit 1; }

if (( check )); then
  echo "Running the tests first…"
  (cd "$repo" && uv run pytest -q) || { echo "Tests failed; the app was not changed." >&2; exit 1; }
fi

if pgrep -f "$app/Contents/MacOS/" >/dev/null 2>&1; then
  echo "Note: MiniFlow is open. Quit and reopen it after the update to load the new code."
fi

tmp="${TMPDIR:-/tmp}"
backup="$(mktemp -d "${tmp%/}/miniflow-app-backup.XXXXXX")"
cp -R "$resources/miniflow" "$backup/miniflow"
cp "$plist" "$backup/Info.plist"

rsync -a --delete --exclude __pycache__ --exclude '*.pyc' "$repo/miniflow/" "$resources/miniflow/"
diff -rq -x __pycache__ "$repo/miniflow" "$resources/miniflow" >/dev/null \
  || { echo "The copy doesn't match the checkout; restore from $backup" >&2; exit 1; }

build="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$plist")"
next=$(( build + 1 ))
revision="$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ -n "$(git -C "$repo" status --porcelain -- miniflow 2>/dev/null)" ]]; then revision="$revision+uncommitted"; fi
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $next" "$plist"
/usr/libexec/PlistBuddy -c "Set :MiniFlowSourceRevision $revision" "$plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :MiniFlowSourceRevision string $revision" "$plist"

codesign --force --deep --sign - "$app" 2>/dev/null
codesign --verify --deep --strict "$app"

echo "Updated $app to build $next ($revision)."
echo "Previous version kept in $backup (copy miniflow/ and Info.plist back, then re-sign, to undo)."
echo "Restart Claude Code or Claude Desktop to load the new MCP server code."
