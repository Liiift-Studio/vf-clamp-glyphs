# Changelog

## [1.2.10] — 2026-06-13

- **Hull plot dots no longer clip at the edges**: the 2-axis plot now maps instance coordinates into a 6-px inset rectangle so a dot at the axis minimum/maximum sits fully inside the chart border instead of straddling it. New `DOT_INSET` constant in `hull_plot.py`.
- **Animation probe ring**: while the HOHO Anes specimen animates, a small hollow circle now tracks the live position inside the hull rectangle so you can see which point of the licensed design space the specimen is currently rendering at. Wired by `AnimatedPreviewView.setProbeTarget_(hullView)`; each `tick_` pushes `_current_variations()` into `HullPlotView.setProbeCoords_`. The ring clears automatically when animation stops.
- **HOHO Anes specimen bumped from 44 pt to 60 pt** so it fills more of the right column. The old 44 pt was a defensive overcorrection after a 64-pt overflow on a very-bold-axis font; 60 pt is the comfortable middle ground.
- **Hull plot now shows the full font axis range** in a muted second line below the selection range — `full: wght 100–900  opsz 12–42` — so the hull rectangle reads in context of the design space, not in isolation.

## [1.2.9] — 2026-06-13

- **Removed the redundant single-line status label** at the bottom of the action bar. The LOG pane already shows everything that used to appear there; every save was producing a duplicate "Saved: …" line below the buttons. `_set_status()` now writes only to the log.
- **Interactive hull plot**: clicking instance dots in the 2-axis plot toggles their selection. Each instance is drawn as a small dot at its design-space coordinates — filled accent for selected rows, outlined for unselected. New `HullPlotView.setInstances_selectedIndices_onClick_(...)` accepts the per-instance coords, the current selection mask, and a Python callback. `mouseDown_` does an 8-px-radius hit test against the most-recent draw to decide which instance to toggle, then invokes the callback. New helpers `_selected_instance_indices()` and `_toggle_instance_at_index()` in the dialog handle the round trip.
- 1-axis plot and 3+-axis chips view stay non-interactive for now — they're trivial enough that the instance list checkboxes are still the right surface.

## [1.2.8] — 2026-06-13

- **Fix: "The file 'clone (Autosaved).glyphs' doesn't exist." alert after every save**. `gsfont.copy()` (the first step of `clamp_gsfont`) silently registers the clone with Glyphs' shared `NSDocumentController` and gives it an autosave path that is never actually written. When the cloned GSFont was later garbage-collected, Glyphs' autosave subsystem looked for that phantom file and surfaced a modal alert. New helper `_evict_clone_tracking(gsfont)` in `gsfont_core.py` calls `parent.updateChangeCount_(0)` → `NSDocumentController.removeDocument_(parent)` → `parent.close()` to make Glyphs forget the clone before the autosave check fires. Wired into `save_gsfont_to_glyphs`, `export_gsfont_binary_via_glyphs`, and the preview-compile temp-doc cleanup in `font_registration.py`.

## [1.2.7] — 2026-06-13

- **Fix bottom-up Y inversion for raw NSView placement**. The hull-plot and HOHO-Anes preview views are mounted directly on `win._window.contentView().addSubview_()` — which uses macOS-default bottom-left coordinates — but everywhere else in the dialog we use vanilla's top-left convention. The two NSViews were therefore rendering at flipped Y positions. Symptoms: in v1.2.6's taller window, the hull plot rectangle drifted down into Zone 3 (overlapping Format/Folder), and "HOHO Anes" floated up into the preview-name slot. Fix: convert top-y → bottom-y (`window_h − top_y − view_h`) before constructing each raw NSView's frame.
- **Shrink HOHO Anes from 64 pt to 44 pt** so the specimen fits comfortably inside the ~370 px right column even when the wght/wdth sweep makes the text temporarily wider on heavy axis values.

## [1.2.6] — 2026-06-13

- **"Open after generating" checkbox** in the Output zone. When ticked, successful saves trigger `Glyphs.open(path)` for `.glyphs` outputs and `NSWorkspace.openFile_` for binary outputs.
- **Dedicated scrollable LOG pane** between the Output zone and the action bar. Replaces the previously-truncated 74-px status sliver. NSTextView wrapped in an NSScrollView, monospaced, semi-transparent dark background. All status updates flow through `_log_append()` which auto-scrolls to the bottom and trims to ~5 KB so a long session doesn't bloat the editor.
- Status label retained for the brief one-line summary in the action bar; the log is now the authoritative surface for anything longer.
- Action area shape: Zone 3 height bumped 144 → 174 to fit the new checkbox row; new constant `LOG_H = 84` reserves the log pane; total window height grows by ~94 px.

## [1.2.5] — 2026-06-13

- **Real fix for invisible wght animation**: `CTFontManagerCreateFontDescriptorsFromURL` returns one descriptor per font face in the file — for a variable font with named instances, that's the variable font itself **plus** one descriptor per named instance with axes already collapsed. v1.2.3 / v1.2.4 took `descriptors[0]` blindly, which often landed on an instance descriptor with no variable axes left to animate. v1.2.5 walks every descriptor, probes each with `CTFontCreateWithFontDescriptor`, queries `CTFontCopyVariationAxes`, and returns the first one that exposes a non-empty axis list (falls back to `descriptors[0]` for legitimately static fonts).
- **Readable error messages**: status label was only ~74 px wide so any error longer than "Error: …" got clipped. Moved to a full-width row above the bottom edge. Long error tooltips now also attach for hover. Full Python traceback is unconditionally logged to stderr so the Glyphs Macro Panel always carries the complete error, even when the status label truncates.
- **Action bar height** bumped 56 → 64 to fit the new status row without re-clipping the buttons.

## [1.2.4] — 2026-06-13

- **Fix: axis animation now visible for source fonts with non-standard axis identifiers**. v1.2.3 computed identifiers from OpenType tags (`'wght' → 0x77676874`) but the compiled font may report different identifiers in its `fvar` table. Now `setFontDescriptor_` queries the real axes via `CTFontCopyVariationAxes` and builds a `tag → identifier` map so `NSFontVariationAttribute` receives the keys the font actually understands. Falls back to the computed identifier when the CoreText query fails.
- **Hide the specimen at 10% opacity when no instances are selected**. Previously "HOHO Anes" stayed at full opacity with stale variation values even when the hull was empty — visually loud but conveying nothing. Now the no-selection state dims to 10% with a hint caption.

## [1.2.3] — 2026-06-13

- **Real source-font preview**: the animated `HOHO Anes` specimen now renders with the user's actual font instead of the macOS system fallback. New module `font_registration.py` wraps `CTFontManagerRegisterFontsForURL` to register the source with the process-scope font namespace and extract its `NSFontDescriptor`, which `AnimatedPreviewView.setFontDescriptor_()` consumes.
- **File source mode**: synchronous register on `_load_font(path)`. Effectively instant.
- **Open Font source mode**: spawns a background worker (`export_gsfont_to_temp_vf_async`) that saves a copy of the open `.glyphs` to a temp directory, opens it headlessly via `Glyphs.open(showInterface=False)`, finds-or-creates a Variable Font Setting, calls `vf_inst.generate(format='OTF', containers=[PLAIN])` to compile a temp variable TTF, and registers it. Slow first hit (a few seconds) but only once per source font. Stale-callback token guards against the user changing sources mid-export.
- **Cleanup**: `_on_cancel` invokes `cleanup_all_temp_paths()` to unregister every temp font + delete every temp directory so registered fonts don't leak across sessions.

## [1.2.2] — 2026-06-13

- **Fix**: Cancel + Generate buttons no longer clipped by the bottom edge of the dialog. The 36-px action bar was too tight for a 32-px Generate, 24-px Cancel/Reveal, and 18-px shortcut hints — bumped to 56 px with hints anchored to a stable Y offset.
- **New**: Animated `HOHO Anes` specimen preview in the dashboard zone. Renders the specimen text and cycles `font-variation-settings` over the **selected hull range** (not the full source axis range) so what you see matches what a licensed customer would see in the clamped output. Each axis sweeps lo → hi → lo over a 2.4-second cosine loop, phase-offset per axis so multi-axis fonts don't move in lockstep. Live caption beneath the specimen shows the current axis values.
- v1.2.2 uses the system variable font for animation (proves the pipeline + axis math). v1.2.3 will register the actual source font via `CTFontManagerRegisterFontsForURL` so the preview shows real glyph shapes from the user's font.

## [1.2.1] — 2026-06-13

- **Fix critical layout bug in v1.2.0**: zone-build methods used box-relative coordinates while widgets were attached to the window root, causing every widget in zones 1/2/3 to render stacked at the top-left of the window with massive overlap. Cause: `vanilla.Box` re-parenting via `box.attr = win.widget` is unreliable across Glyphs builds; widgets stay attached to whatever container they were originally assigned to. Fix: keep the Boxes as decorative frames only, place every child widget at window-relative coordinates (add `PAD` to X, add `y` to Y), drop the broken `box.title = win.title` re-assignments, and mount the hull plot NSView on `win._window.contentView()` instead of `box._nsObject`.

## [1.2.0] — 2026-06-12

Major UX release. The dialog is restructured into a clear three-zone
layout (Source / Instances + Hull / Output) with a new instance list,
a graphical hull preview, persistent presets, recent-folders MRU, and
drag-and-drop font loading. Build-zip and version-parity tooling were
tightened in lockstep.

### Added

- **Three-zone dialog restructure.** The window is reorganised into
  Source, Instances + Hull preview, and Output zones with consistent
  spacing, semibold zone headers, and accessibility roles so screen
  readers announce the new grouping correctly.
- **`vanilla.List`-based instance picker.** Replaces the stack of
  individual `CheckBox` widgets. The list supports multi-select,
  keyboard navigation, type-ahead, and a `Cmd-A` / `Cmd-Shift-A`
  select-all / deselect-all binding. Scroll height no longer needs to
  be pre-reserved because the `List` is its own NSScrollView.
- **Graphical hull plot (`hull_plot.py`).** New custom `NSView`
  renders a 1-axis bar or 2-axis rectangle showing the full design
  space vs. the selected sub-hull. Three-or-more-axis fonts fall back
  to the existing chip preview. The view degrades gracefully when
  AppKit is unavailable so the module is safe to import on CI.
- **Presets (`presets.py`).** Save a named selection (instances +
  output format + name pattern) and recall it from a popup. Atomic
  JSON persistence under
  `~/Library/Application Support/Glyphs 3/Plugins/vf-clamp/presets.json`
  with schema validation and graceful corruption handling.
- **Recent-folders MRU.** The output-folder popup remembers the last
  5 folders used, persisted alongside presets in `recent.json`. The
  list is capped at `RECENT_FOLDERS_MAX = 5` to match Finder's
  "Recent Places" density.
- **Drag-and-drop font loading.** The Source zone accepts dropped
  `.ttf`, `.otf`, `.woff`, `.woff2` files via `NSPasteboardTypeFileURL`
  / `NSFilenamesPboardType`. The drop target is the entire Source
  zone, with a highlight ring during the drag.
- **Format descriptions.** Each format in the popup now has a one-line
  description beside it (e.g. "WOFF2 compressed web font (requires
  brotli)") so the trade-off is visible before clicking Generate.
- **Cmd-key shortcuts.** `Cmd-A` selects all instances, `Cmd-Shift-A`
  deselects all, `Cmd-S` saves a preset, `Cmd-,` opens the preset
  popup. Implemented via a local `NSEventMaskKeyDown` monitor.

### Changed

- **Plugin shell rewritten** to host the new three-zone layout while
  keeping the existing `LiiiftVFClampPlugin` GeneralPlugin contract,
  the file-source worker thread, and the GSFont main-thread path
  untouched. Approximately 1.3k of the 2.0k-line `plugin.py` diff is
  layout / wiring; the underlying `core.py`, `gsfont_core.py`, and
  `formats.py` modules are unchanged.
- **Axis colour palette factored** into `_rgb_for_axis` so both the
  chip preview and the new hull plot share a single source of truth
  for light/dark appearance handling.
- **Status label promoted** to the Output zone footer with a clearer
  separation between the error styling and the Reveal-in-Finder
  affordance.

### Fixed

- Validation harness for headless smoke tests now uses an
  attribute-tolerant `StubModule` subclass so `from AppKit import ...`
  and `from GlyphsApp import ...` succeed under bare `python3 -c`
  invocations without needing the real Glyphs runtime. No production
  code change — the plugin's imports are correct.

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
