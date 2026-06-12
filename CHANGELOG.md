# Changelog

## [1.1.5] — 2026-06-12

- Fix: Instance checkboxes correctly rendered but **overlapped** the Hull / Output Name / Format / Folder rows below them. Cause: scroll widget grew downward at populate time while the rows below stayed at their original Y positions (vanilla widgets with positive Y don't auto-reflow). Fix: reserve the maximum scroll height (`MAX_VISIBLE_INSTANCES = 8` rows × 24px) at build time so every widget below sits at a fixed Y regardless of how many instances the font has. Trade-off: a font with 1-2 instances now shows some empty scroll space, but nothing overlaps and the window doesn't jump on font load.

## [1.1.4] — 2026-06-12

- Fix: Format popup defaulted to `TTF` when launching with an open Glyphs document instead of `.glyphs` — the cross-mode "preserve user's selection" logic kept the file-mode default alive. Now always resets to the per-mode default on source mode change.
- Fix: Instance checkboxes were invisible inside the scroll area despite the font's instance count rendering correctly. Root cause: scroll widget moved from `CONTROL_X` to `PAD` during populate, leaving the inner_group's frame misaligned with the new clip view. Populate now resizes the scroll widget first (keeping it at `CONTROL_X` to match the build layout) before swapping the document view, explicitly sets the inner_group's frame, and calls `tile() + reflectScrolledClipView_()` so NSScrollView recomputes its scroll range.

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.3] — 2026-06-12

Final pass on the panel review backlog — closes the last 5 deferred findings
from the 1.1.2 follow-up cycle.

### Added

- Public naming aliases on `core.py` so call-sites can use the documented
  identifiers (closes deferred naming-alias finding).
- Type hints on the new `gsfont_core.py` / `formats.py` module surfaces so
  downstream consumers (and `mypy`) see a consistent signature across the
  fontTools and GSFont paths (closes deferred typing finding).
- Explicit `setAccessibilityLabel_` on the remaining controls in the dialog
  that were missed in the 1.1.1 a11y sweep (closes deferred a11y finding).
- `pathlib` adoption on the new GSFont path for consistency with the rest of
  `core.py` (partial — file-source path remains on `os.path` because
  `fontTools.ttLib.TTFont` takes raw paths) (closes deferred pathlib finding).

### Changed

- Sheet vs. window rationale captured in the dialog source so future
  contributors understand why generate-progress lives in a sheet rather than
  the main window (closes deferred Sheet-rationale finding).

## [1.1.2] — 2026-06-11

Follow-up release addressing the 20 deferred findings from the 1.1.1 panel
review — architectural separation, font correctness on edge cases, and
controller-panel UX items deferred from the initial fix pass.

### Added

- `formats.py` module — central registry of output formats so dispatch logic
  lives in one file instead of being threaded through `core.py` and `plugin.py`.
- `gsfont_core.py` module — extracted GSFont (Glyphs.app source) helpers from
  `core.py` into their own subsystem so the fontTools binary path and the
  Glyphs-app object path are no longer entangled.
- `tests/test_coverage_gaps.py` — 380 lines of additional tests covering
  previously-untested branches in `core.py`, `plugin.py`, and the new
  `gsfont_core.py` / `formats.py` modules.

### Changed

- `core.py` refactored to delegate GSFont-specific work to `gsfont_core.py`
  and format-specific work to `formats.py`; module shrinks accordingly.
- `plugin.py` reorganised around the new module split for clearer flow.
- `scripts/build-zip.sh` updated to bundle the new modules and exclude
  pre-built `vf-clamp-glyphs.zip` / `.sha256` from the source tree.

### Fixed

- 20 deferred findings from the 1.1.1 panel review covering architectural
  separation, font correctness on edge cases (round-trip / collapsed axes),
  and UX improvements in the controller panel.

## [1.1.1] — 2026-06-11

This release lands the targeted fixes from a 10-engineer panel review of the
1.1.0 GSFont source path. 50 findings were filed as GitHub issues #33–#82;
this release closes the CRITICAL packaging/version-sync/correctness items and
the highest-impact MAJOR UX/security items.

### Added

- `is_fonttools_ready`, `fonttools_import_error`, `is_glyphs_app_available`,
  `open_font_safely` public helpers in `core.py` so `plugin.py` no longer
  reaches into `_FONTTOOLS_*` / `_GLYPHS_AVAILABLE` underscore internals
  (closes #45).
- `FontParseError` (subclass of `ValueError`) so callers can distinguish
  parse failure from I/O failure on `_safe_open_font` (closes #47, #78).
- `_PIN_EPSILON` tolerance on `clamp_gsfont` so a single-instance pin can
  succeed against a master whose coordinates round-tripped through floating
  point; tailored error message when the pin point coincides with no
  master (closes #75).
- `_disambiguated_instance_labels` helper shared by
  `get_axis_hull_from_instances` and `filter_fvar_instances` so duplicate
  subfamily names match correctly (closes #71).
- `tests/test_gsfont_helpers.py` with a `FakeGSFont` factory exercising
  `list_open_glyphs_fonts`, `gsfont_label`, `gsfont_instance_names`,
  `compute_gsfont_hull`, `clamp_gsfont` (deep-copy, VF-setting preservation,
  family rewrite, empty selection, no-masters), `_container_for_format`,
  and `_outline_format_for`. Test count: 66 → 75 (closes #37, #77).
- CI now uploads coverage and runs `build-zip.sh` as a preflight smoke check
  (closes #53).

### Changed

- `clamp_gsfont` deep-clones the source GSFont via save-to-temp-and-reopen
  (with `copy.deepcopy` fallback in CI) so mutations never leak back into
  the user's open document (closes #40).
- VF Setting instances now get their `axes` trimmed when an axis collapses,
  keeping `font.axes` structurally parallel to `instance.axes`
  (closes #39).
- `save_gsfont_to_glyphs` default `format_version` now inherits the source
  font's `formatVersion` instead of hardcoding `3` (closes #76).
- `safe_output_path` uses `os.path.realpath` so macOS symlink mismatches
  (`/var` → `/private/var`) cannot smuggle a traversal past `commonpath`
  (closes #48).
- `_generate_from_gsfont` defers the heavy clamp + `Glyphs.open` work via
  `AppHelper.callAfter` so the spinner and status label paint before the
  main thread blocks (closes #38).
- `_on_cancel` signals a cancellation flag to in-flight file-source
  workers; the worker skips its success callback and unlinks any partial
  output (closes #43).
- `_auto_select_frontmost_gsfont` uses `is` for identity comparison
  (GSFont `__eq__` is NSObject `-isEqual:` pointer identity; PyObjC can
  reissue wrappers) (closes #70).
- `showDialog_` surfaces an existing dialog instead of spawning a zombie
  one (closes #68).
- `axisPreview` cell now wraps lines so the multi-line attributed string
  renders correctly instead of being clipped (closes #67).
- WOFF2 brotli-availability check now runs for the GSFont source path too
  (closes #57).
- All/None/Invert bulk-select buttons get explicit
  `setAccessibilityLabel_` (closes #82).
- `Glyphs.open` `showInterface=False` `TypeError` fallback no longer opens
  a visible window and steals focus (closes #50).
- `vf_inst.generate()` return value handling now treats non-True success
  values explicitly (closes #51).
- `produce_restricted_vf` and `export_gsfont_binary` unlink partial output
  on failure (closes #54).
- Plugin menu registration no longer mixes `self.menuName` with manual
  `NSMenuItem` injection (closes #69).
- `_on_generate_failure` no longer double-prefixes `Error:`; raw paths are
  scrubbed to `~`-relative form; error-state styling applied to status
  label (closes #55).
- Reveal button is disabled + hidden on a subsequent generate failure so
  it cannot surface a stale prior output (closes #56).
- Temp-directory cleanup is now structured try/finally and leaked Glyphs
  documents are closed from `Glyphs.fonts` (closes #49).
- Broad `except Exception` blocks narrowed across `core.py` and `plugin.py`
  so real errors propagate instead of being swallowed (closes #78).

### Fixed

- Self-referential `vf-clamp.glyphsPlugin/vf-clamp.glyphsPlugin` symlink
  removed; `build-zip.sh` aborts if one reappears (closes #33).
- `Contents/Resources/__pycache__` stripped from the shipped bundle;
  `build-zip.sh` strips `__pycache__/`, `*.pyc`, `*.pyo`, `*.swp`, `*~`
  and uses `--symlinks` to prevent regression (closes #34).
- `Info.plist`, `pyproject.toml`, and `CHANGELOG.md` versions are now
  asserted to agree in the build preflight; ships refuse to proceed
  otherwise (closes #35).
- README rewritten to cover both source paths; the "important: file-only"
  callout that contradicted 1.1.0 was removed (closes #36).
- Stale "47 tests" and "framework-agnostic core.py" claims corrected in
  README + Development section (closes #77).
- `_safe_open_font` size check no longer TOCTOU-races the open; raises
  `FontParseError` (subclass of `ValueError`) on parse failure to give
  the right exception taxonomy (closes #47).

## [1.1.0] — 2026-06-11

This release introduces a second source mode: clamping the open Glyphs.app
document directly to a new `.glyphs` file (or a Glyphs-native binary export),
in addition to the existing exported-font-file workflow.

### Added

- Open-Glyphs-font source path: `clamp_gsfont`, `save_gsfont_to_glyphs`,
  `export_gsfont_binary_via_glyphs`, `list_open_glyphs_fonts`, `gsfont_label`,
  `gsfont_instance_names`, `compute_gsfont_hull` in `core.py`.
- Optional `from GlyphsApp import ...` import block in `core.py` so the new
  helpers only resolve inside Glyphs (no-op in CI).
- Source radio (`Open Font` / `File`) in the dialog; the dialog now defaults
  to the frontmost open Glyphs document when one is available.
- `.glyphs` output format for the open-font source path.
- Hull-preview chips with per-axis color coding.
- Primary blue Generate button (Return key equivalent) and Escape-bound
  Cancel button in the bottom action bar.
- Build-script preflight that asserts `Info.plist` version, `pyproject.toml`
  version, and CHANGELOG header are in sync, and refuses to ship a zip with
  a self-referential symlink or > 50 entries.
- `[build-system]` and `[tool.mypy]` tables in `pyproject.toml`.

### Changed

- `build-zip.sh` now strips `__pycache__/`, `*.pyc`, `*.pyo`, editor swap
  files; uses `zip --symlinks` so symlinks are preserved instead of followed;
  verifies the resulting zip size and entry count.
- README documents the open-font path (Source radio, `.glyphs` output,
  required Glyphs 3.2+ APIs) and the new failure modes.

### Fixed

- Removed the self-referential `vf-clamp.glyphsPlugin/vf-clamp.glyphsPlugin`
  symlink that caused `zip -r` to recurse and produce a 2.9 MB bundle
  containing the bundle inside itself dozens of times.
- Removed the developer-machine `Contents/Resources/__pycache__/` directory
  from the shipped bundle; `.pyc` are regenerated by Glyphs on first import.
- `_on_generate_failure` no longer double-prefixes `Error:` when relaying a
  message through `_set_status(error=True)`.
- Reveal button is hidden + disabled on a subsequent generate failure so it
  cannot surface a stale prior output.

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
