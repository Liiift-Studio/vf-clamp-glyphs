# vf-clamp-glyphs — Claude Code Configuration

## Inherited Context

This is a plugin submodule of `@liiift-studio/vf-clamp`. When working inside the
vf-clamp parent repo checkout, Claude Code will also load `vf-clamp/CLAUDE.md` which
defines the core purpose, API, name table patching approach, and shared conventions.

## What This Is

A Glyphs.app plugin that generates restricted variable fonts from any TTF/OTF variable
font file. Font engineers select named instances in a dialog, and the plugin produces
one restricted VF with the correct name table — matching the delivery behaviour of the
`@liiift-studio/vf-clamp` npm package.

## Tech Stack

- Python 3 (Glyphs.app built-in runtime)
- fonttools (bundled with Glyphs.app — `fontTools.varLib.instancer`, `fontTools.ttLib.TTFont`)
- vanilla (bundled with Glyphs.app, used for all UI)
- GlyphsApp Python API (`GeneralPlugin`, `Glyphs`, `NSOpenPanel`, etc.)

## Key Files

| File | Purpose |
|------|---------|
| `vf-clamp.glyphsPlugin/Contents/Resources/core.py` | Framework-agnostic fontTools helpers + optional GSFont helpers gated by GlyphsApp import |
| `vf-clamp.glyphsPlugin/Contents/Resources/plugin.py` | AppKit/PyObjC dialog + `LiiiftVFClampPlugin` GeneralPlugin shell |
| `vf-clamp.glyphsPlugin/Contents/Info.plist` | Bundle metadata — class name, bundle ID, version |
| `scripts/build-zip.sh` | Reproducible bundle zip with version + symlink + entry-count preflight |

## Plugin Architecture

- **`LiiiftVFClampPlugin`** — `GeneralPlugin` subclass; registers one Script-menu item. Vendor-namespaced to avoid Obj-C symbol collisions.
- **`VFClampDialog`** — `vanilla.Window` with a Source radio (Open Font / File), instance checkboxes, hull preview, output options, and Generate.
- Two source paths share the same dialog: (a) fontTools instancer against an exported binary, run in a worker thread; (b) GSFont clamp against an open Glyphs document, routed through `GSInstance.generate` on the main thread because Glyphs APIs require it.
- Core logic lives in `core.py` module-level functions (`compute_hull`, `patch_name_table`, `compact_name`, `produce_restricted_vf`, `clamp_gsfont`, `compute_gsfont_hull`, etc.) so the fontTools subset can be unit-tested independently.

## Installation

Double-click `vf-clamp.glyphsPlugin` in Finder, or drag it to:

```
~/Library/Application Support/Glyphs 3/Plugins/
```

Restart Glyphs. The menu item appears under **Script › vf-clamp › Generate Restricted VFs…**

## Usage Flow

1. Script › vf-clamp › Generate Restricted VFs…
2. Pick the **Source** — `Open Font` (a Glyphs document) or `File` (an exported binary).
3. Either pick an open Glyphs font from the popup, or click **Browse…** to pick a `.ttf`/`.otf`/`.woff`/`.woff2` from disk.
4. Tick the named instances you want to include in the restricted VF.
5. Adjust the **Output Name** if needed (auto-computed via `compact_name`).
6. Choose **Format** — `.glyphs` (Open Font source only), TTF, OTF, WOFF, or WOFF2 — and **Output Folder**.
7. Click **Generate**.

## Coding Standards

- Python 3 style throughout
- Tabs for indentation (not spaces)
- One-line summary comment at the top of each file
- Comment every function and class with a concise docstring
- `console.error` / `console.warn` equivalents: use `print` to stderr for non-fatal warnings

## Engineers to Contact If Stuck

- **Glyphs.app plugin API:** Georg Seifert and Rainer Scheichelbauer (@mekkablue) — glyphsapp.com/forum
- **fonttools / instancer:** Cosimo Lupo (@anthrotype), Behdad Esfahbod (@behdad)
- **vanilla UI toolkit:** documentation at `help(vanilla)` inside Glyphs, or robofab.org
