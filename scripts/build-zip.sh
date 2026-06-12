#!/usr/bin/env bash
# build-zip.sh — pack vf-clamp.glyphsPlugin into a distributable zip with a
# checksum so users can verify the artifact against the source commit.
#
# This script intentionally excludes developer-machine artifacts (__pycache__,
# .pyc/.pyo, .DS_Store, editor swap files) and any self-referential symlinks.
# It also asserts that Info.plist, pyproject.toml, and CHANGELOG.md all agree
# on the current release version before producing the zip.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE="vf-clamp.glyphsPlugin"
OUT="vf-clamp-glyphs.zip"

cd "$ROOT"

if [ ! -d "$BUNDLE" ]; then
	echo "Error: $BUNDLE not found in $ROOT" >&2
	exit 1
fi

# --- Preflight: refuse to package if version sources disagree ------------------
PLIST_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$BUNDLE/Contents/Info.plist" 2>/dev/null || true)"
PY_VERSION="$(grep -E '^version\s*=' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"

if [ -z "$PLIST_VERSION" ] || [ -z "$PY_VERSION" ]; then
	echo "Error: could not read version from Info.plist or pyproject.toml" >&2
	exit 1
fi

if [ "$PLIST_VERSION" != "$PY_VERSION" ]; then
	echo "Error: version drift detected:" >&2
	echo "  Info.plist CFBundleShortVersionString: $PLIST_VERSION" >&2
	echo "  pyproject.toml version:                $PY_VERSION" >&2
	echo "  Update both to match before building the zip." >&2
	exit 1
fi

if ! grep -qE "^## \[$PLIST_VERSION\]" CHANGELOG.md; then
	echo "Error: CHANGELOG.md has no '[$PLIST_VERSION]' section header" >&2
	exit 1
fi

# --- Strip developer-machine artifacts before zipping ------------------------
# Remove macOS metadata, Python bytecode caches, and editor swap files.
find "$BUNDLE" -name ".DS_Store" -delete
find "$BUNDLE" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLE" -name "*.pyc" -delete
find "$BUNDLE" -name "*.pyo" -delete
find "$BUNDLE" -name "*.swp" -delete
find "$BUNDLE" -name "*~" -delete

# Refuse to ship if a self-referential symlink slipped back in (regression guard).
SELF_LINK="$BUNDLE/$BUNDLE"
if [ -L "$SELF_LINK" ]; then
	echo "Error: self-referential symlink found at $SELF_LINK — refusing to zip" >&2
	exit 1
fi

# Verify the executable Mach-O is present so users do not download a broken bundle.
if [ ! -f "$BUNDLE/Contents/MacOS/plugin" ]; then
	echo "Warning: $BUNDLE/Contents/MacOS/plugin is missing (Python-only mode)" >&2
fi

rm -f "$OUT" "$OUT.sha256"
# -X strips extra file attributes; -r recurses; --symlinks preserves symlinks
# rather than following them (defensive — guards against any future symlinks
# in the bundle even though we explicitly forbid the self-referential one above).
# Exclusion patterns repeat the strip step as belt-and-braces.
zip -X -r --symlinks "$OUT" "$BUNDLE" \
	-x "*/__pycache__/*" \
	-x "*.pyc" \
	-x "*.pyo" \
	-x "*.DS_Store" \
	-x "*.swp" \
	-x "*~" \
	>/dev/null

# Smoke-check the produced zip: refuse if it ballooned past a sane size or has
# the recursive nesting from the self-referential symlink (regression guard).
ZIP_BYTES=$(stat -f %z "$OUT" 2>/dev/null || stat -c %s "$OUT")
ZIP_ENTRIES=$(unzip -l "$OUT" | tail -1 | awk '{print $2}')
if [ "$ZIP_BYTES" -gt 524288 ]; then
	echo "Error: $OUT is $ZIP_BYTES bytes — looks like the self-link bug returned" >&2
	exit 1
fi
if [ "$ZIP_ENTRIES" -gt 50 ]; then
	echo "Error: $OUT has $ZIP_ENTRIES entries — bundle is recursively nested" >&2
	exit 1
fi

shasum -a 256 "$OUT" > "$OUT.sha256"

echo "Built $OUT ($ZIP_BYTES bytes, $ZIP_ENTRIES entries, version $PLIST_VERSION)"
cat "$OUT.sha256"
