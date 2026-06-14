#!/usr/bin/env bash
# build-zip.sh — pack vf-clamp.glyphsPlugin into a distributable zip with a
# checksum so users can verify the artifact against the source commit.
#
# Reproducibility:
#   * Excludes developer-machine artifacts (__pycache__, .pyc/.pyo, .DS_Store,
#     editor swap files) and any self-referential symlinks.
#   * Normalises file mtimes to SOURCE_DATE_EPOCH (or the latest git commit
#     time when SOURCE_DATE_EPOCH is not set) so the resulting zip is bit-for-
#     bit identical when built from the same source commit.
#   * Sets LANG=C / LC_ALL=C / TZ=UTC so locale-dependent zip metadata
#     (filename byte order, timezone offsets) stays deterministic.
#   * Asserts that every bundled file is tracked by git so the artifact has a
#     verifiable provenance — no developer-only files leak into the release.
#   * Asserts that Info.plist, pyproject.toml, and CHANGELOG.md all agree on
#     the current release version before producing the zip.

set -euo pipefail

# Force deterministic locale + UTC timezone so the zip's central directory
# stays byte-identical across builds.
export LANG=C
export LC_ALL=C
export TZ=UTC

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

# Provenance check: every bundled file (other than the Resources/__pycache__
# we already strip) must be tracked by git so the artifact can be verified
# against the source commit. Run this AFTER the strip step so untracked
# __pycache__/* etc. are gone before we inspect.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	UNTRACKED="$(git ls-files --others --exclude-standard "$BUNDLE" 2>/dev/null || true)"
	if [ -n "$UNTRACKED" ]; then
		echo "Error: $BUNDLE contains files not tracked by git:" >&2
		printf '  %s\n' $UNTRACKED >&2
		echo "  Commit or remove them before building the release zip." >&2
		exit 1
	fi
fi

# Reproducibility: normalise every bundled file's mtime to a deterministic
# timestamp before zipping. SOURCE_DATE_EPOCH is the Reproducible Builds
# convention; we fall back to the latest git commit time when callers haven't
# set one. Without this, the zip's central directory carries the developer's
# wallclock and the artifact differs run-to-run.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
	if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		SOURCE_DATE_EPOCH="$(git log -1 --pretty=%ct -- "$BUNDLE" 2>/dev/null || echo 0)"
	fi
fi
if [ -n "${SOURCE_DATE_EPOCH:-}" ] && [ "$SOURCE_DATE_EPOCH" != "0" ]; then
	# touch -t expects [[CC]YY]MMDDhhmm[.SS]; convert the epoch in UTC.
	TS="$(TZ=UTC date -r "$SOURCE_DATE_EPOCH" +%Y%m%d%H%M.%S 2>/dev/null || true)"
	if [ -n "$TS" ]; then
		find "$BUNDLE" -exec touch -t "$TS" {} +
	fi
fi

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
	-x "*/.git*" \
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

# Per-build dialog snapshot — best-effort. Renders the dialog mock via the
# render_dialog harness and saves it to versions/dialog-v$PLIST_VERSION.png
# so each shipped release has a visual artifact alongside the zip. Skips
# silently when a Python with PyObjC isn't available (e.g. minimal CI runners)
# rather than failing the whole build.
SNAPSHOT_DIR="$ROOT/versions"
SNAPSHOT_PATH="$SNAPSHOT_DIR/dialog-v$PLIST_VERSION.png"
mkdir -p "$SNAPSHOT_DIR"
RENDER_PY="$HOME/.pyenv/shims/python3"
if [ ! -x "$RENDER_PY" ]; then
	RENDER_PY="$(command -v python3 || true)"
fi
if [ -n "$RENDER_PY" ] && [ -f "$ROOT/tools/render_dialog.py" ]; then
	if "$RENDER_PY" -c "import objc, AppKit" >/dev/null 2>&1; then
		"$RENDER_PY" "$ROOT/tools/render_dialog.py" \
			--out "$SNAPSHOT_PATH" >/dev/null 2>&1 || true
		if [ -f "$SNAPSHOT_PATH" ]; then
			echo "Snapshot: versions/dialog-v$PLIST_VERSION.png"
		fi
	fi
fi
