#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PROJECT_NAME=$(basename "$PROJECT_ROOT")
DIST="$PROJECT_ROOT/dist"
HISTORY="$DIST/history"
VERSION=$(tr -d '[:space:]' < "$PROJECT_ROOT/VERSION")
PACKAGE_BASE="$PROJECT_NAME-$VERSION"
FINAL="$DIST/$PACKAGE_BASE.zip"

python3 - "$VERSION" <<'PY'
import re
import sys

if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', sys.argv[1]):
    raise SystemExit('VERSION must contain a semantic version such as 0.1.0')
PY

mkdir -p "$HISTORY"

if [ -e "$FINAL" ]; then
  echo "Refusing to overwrite existing version: $FINAL" >&2
  exit 1
fi

for archived in "$HISTORY/$PACKAGE_BASE-"*.zip; do
  if [ -f "$archived" ]; then
    echo "Refusing to package a version already present in history: $VERSION" >&2
    exit 1
  fi
done

for previous in "$DIST/$PROJECT_NAME-"*.zip; do
  if [ -f "$previous" ]; then
    stamp=$(date -u +%Y%m%d-%H%M%S)
    previous_stem=${previous##*/}
    previous_stem=${previous_stem%.zip}
    mv "$previous" "$HISTORY/$previous_stem-$stamp.zip"
  fi
done

TEMP="$DIST/.$PACKAGE_BASE.$$.tmp.zip"
trap 'rm -f "$TEMP"' EXIT HUP INT TERM
PROJECT_PARENT=$(dirname "$PROJECT_ROOT")
(
  cd "$PROJECT_PARENT"
  zip -qr "$TEMP" "$PROJECT_NAME" \
    -x "$PROJECT_NAME/.git/*" \
       "$PROJECT_NAME/dist/*" \
       "$PROJECT_NAME/.codex/work/*" \
       "$PROJECT_NAME/work/*" \
       "$PROJECT_NAME/.venv/*" \
       "$PROJECT_NAME/**/__pycache__/*" \
       "$PROJECT_NAME/monitor-config.json" \
       "$PROJECT_NAME/mode-state.json" \
       "$PROJECT_NAME/context-handoff-state.json" \
       "$PROJECT_NAME/coordinator-continuation-state.json"
)
mv "$TEMP" "$FINAL"
trap - EXIT HUP INT TERM

if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$FINAL"
else
  sha256sum "$FINAL"
fi
echo "Package: $FINAL"
