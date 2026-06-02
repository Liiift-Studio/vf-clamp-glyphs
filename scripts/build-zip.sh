#!/usr/bin/env bash
# build-zip.sh — pack vf-clamp.glyphsPlugin into a distributable zip with a
# checksum so users can verify the artifact against the source commit.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE="vf-clamp.glyphsPlugin"
OUT="vf-clamp-glyphs.zip"

cd "$ROOT"

if [ ! -d "$BUNDLE" ]; then
	echo "Error: $BUNDLE not found in $ROOT" >&2
	exit 1
fi

# Remove macOS metadata files before zipping
find "$BUNDLE" -name ".DS_Store" -delete

rm -f "$OUT" "$OUT.sha256"
# -X strips extra file attributes; -r recurses
zip -X -r "$OUT" "$BUNDLE" >/dev/null
shasum -a 256 "$OUT" > "$OUT.sha256"

echo "Built $OUT"
cat "$OUT.sha256"
