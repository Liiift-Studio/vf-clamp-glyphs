# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-06-01

This release is the result of a panel review covering correctness, security,
packaging, and developer experience. See repo issues labelled `review` for the
underlying findings.

### Added

- `core.py` extracted from `plugin.py` so all fonttools logic is unit-testable
  outside Glyphs.app.
- Pytest test suite (`tests/`) with an in-memory variable-font fixture
  (47 tests covering compact_name, compute_default_output_name, sanitisers,
  compute_hull, produce_restricted_vf, fvar/STAT filtering, WOFF/WOFF2 output).
- GitHub Actions CI matrix (Python 3.9–3.12) running on push/PR.
- `pyproject.toml` with pinned `fonttools >= 4.34` requirement.
- `scripts/build-zip.sh` reproducible build script + `.sha256` checksum for the
  distributable.
- Axis-range preview line in the dialog showing the computed hull per axis.
- Bulk-select buttons (All / None / Invert) above the instance list.
- Reveal-in-Finder button next to the status after a successful generate.
- Progress spinner during background generation.
- LICENSE shipped inside the bundle's `Resources/` so it travels with installs.

### Changed

- `Info.plist` now includes `CFBundleShortVersionString` (semver),
  `CFBundleInfoDictionaryVersion`, `CFBundleDevelopmentRegion`,
  `LSMinimumSystemVersion`, `NSHumanReadableCopyright`, and the
  `GlyphsRequiresAppVersion` key so Glyphs 2 fails cleanly.
- Reverse-DNS identifier switched to `studio.liiift.vfclamp`.
- `NSPrincipalClass` renamed to vendor-namespaced `LiiiftVFClampPlugin`
  (with a `VFClampPlugin = LiiiftVFClampPlugin` alias for back-compat).
- Menu registration now uses `Glyphs.menu[SCRIPT_MENU].submenu().addItem_()`
  and an ObjC selector string instead of a bound Python method.
- Menu action target now uses `setAction_('showDialog:')` rather than passing a
  bound method.
- `setMenuName` is set on the plugin so the submenu label is not derived from
  the bundle slug.
- Worker-thread main-thread callbacks now use `AppHelper.callAfter` instead of
  the non-existent `objc.callOnMainThread`, with a dialog-alive guard so a
  closed window cannot dereference deallocated state.
- File-open panel uses `setAllowedContentTypes_` with UTType when available,
  falling back to `setAllowedFileTypes_`; WOFF/WOFF2 sources are now accepted.
- ScrollView document view is set via `_nsObject.setDocumentView_`, and the
  inner Group is sized with an explicit width so checkboxes render visibly.
- `compute_hull` now anchors the AxisTriple default to a numeric value in
  range (current fontTools rejects `None`).
- Default-clamp warning now uses `isinstance(constraint, instancer.AxisTriple)`
  with a 3-value unpack — the original 2-value unpack always raised.
- `produce_restricted_vf` filters `fvar.instances`, prunes STAT AxisValues
  outside the new hull, and sets `partial.flavor = 'woff'|'woff2'` so WOFF
  outputs are actually WOFF-wrapped (instead of mislabelled sfnt bytes).
- `patch_name_table` strips non-English localised records for IDs it rewrites,
  pairs nameID 17 with nameID 16, drops unencodable mac_roman records (no more
  literal `?` chars), and respects PostScript spec length caps for IDs 6/25.
- `sanitize_filename` (new) strips control characters, Unicode directional
  overrides, and trailing dots/spaces in addition to Windows-reserved chars.
- `safe_output_path` (new) resolves the output path and refuses anything that
  would escape the chosen folder; auto-suffixes on file-name collision.
- Status label is now multi-line and selectable; absolute paths shown to the
  user are scrubbed to `~`-relative form.
- Worker thread catches specific exceptions and logs full tracebacks to the
  Macro Panel via `traceback.print_exc()`.
- Dialog switched from `vanilla.FloatingWindow` to `vanilla.Window` so it does
  not float over Finder windows.
- UI pixel constants moved off the module top-level onto `VFClampDialog`.
- Status prefix is text-only (`Error:`) instead of an emoji that can render as
  `.notdef` on older macOS.

### Fixed

- TTFont handles are now opened inside `contextlib.closing()` so file
  descriptors are released promptly in long-lived Glyphs sessions.
- File-size cap (64 MB) on input fonts to guard against crafted/oversized
  inputs.
- fontTools version is asserted on import (>= 4.34.0).

### Removed

- Unused `import warnings` and the duplicated `compact_name` implementation
  inside `plugin.py` (now lives in `core.py` only).
