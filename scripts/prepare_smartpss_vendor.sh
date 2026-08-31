#!/bin/sh
set -eu

ARCHIVE=${1:-}
DEST=${2:-./vendor/smartpss}
if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  echo "Usage: $0 SMARTPSS_ZIP [DEST]" >&2
  exit 2
fi
mkdir -p "$DEST"
for f in P2PDll.dll libdsl.dll jsonmd.dll; do
  entry=$(unzip -Z1 "$ARCHIVE" | awk -v n="$f" 'tolower($0) ~ ("/" tolower(n) "$") || tolower($0) == tolower(n) {print; exit}')
  if [ -z "$entry" ]; then
    echo "Missing $f in archive" >&2
    exit 1
  fi
  unzip -p "$ARCHIVE" "$entry" > "$DEST/$f"
done
chmod 0644 "$DEST"/*.dll
echo "Prepared private Dahua vendor directory: $DEST"
echo "Expected missing legacy VC80 DLLs (MSVCP80/MSVCR80) must come from Wine/official VC++ 2005 runtime."
