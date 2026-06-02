# vf-clamp-glyphs

A [Glyphs.app](https://glyphsapp.com) plugin that generates restricted variable
fonts from any exported TTF/OTF variable font file. Select the named instances
a customer has licensed, click Generate, and receive a micro-VF that spans
exactly that range — with the name table updated to match.

This is the Glyphs.app companion to the
[`@liiift-studio/vf-clamp`](https://vfclamp.com) npm package, which does the
same thing server-side for per-purchase delivery. Both implementations share
the same `compact_name` algorithm; behavioural parity is tracked in
[CHANGELOG.md](CHANGELOG.md) and enforced by the test suite.

---

## Requirements

| Requirement                | Version            |
| -------------------------- | ------------------ |
| Glyphs.app                 | 3.x                |
| Python (bundled by Glyphs) | 3.8+               |
| fontTools                  | >= 4.34.0          |
| vanilla                    | bundled by Glyphs  |
| brotli (only for WOFF2)    | >= 1.0.9 optional  |

The newer fontTools requirement comes from `instancer.AxisTriple`. Older
versions raise `AttributeError` at startup; the plugin surfaces a clear
message in that case.

---

## Installation

1. Download `vf-clamp-glyphs.zip` from
   [Releases](https://github.com/Liiift-Studio/vf-clamp-glyphs/releases) and
   verify the checksum:
   ```bash
   shasum -a 256 vf-clamp-glyphs.zip
   # compare against vf-clamp-glyphs.zip.sha256
   ```
2. Unzip and double-click `vf-clamp.glyphsPlugin`. Glyphs installs it under:
   ```
   ~/Library/Application Support/Glyphs 3/Plugins/
   ```
   (create that folder if it does not exist).
3. If macOS Gatekeeper blocks the unsigned bundle, clear the quarantine bit:
   ```bash
   xattr -d com.apple.quarantine ~/Library/Application\ Support/Glyphs\ 3/Plugins/vf-clamp.glyphsPlugin
   ```
   Or right-click the bundle in Finder and choose **Open** once.
4. Restart Glyphs 3. The plugin appears under
   **Script › vf-clamp › Generate Restricted VFs…**.

---

## Usage

> **Important:** this plugin works on an already-exported variable font file
> (`.ttf` or `.otf`), not on a `.glyphs` source. Export your variable font from
> Glyphs first, then run this plugin on the exported file.

1. Open Glyphs 3 (no document needs to be open).
2. Go to **Script › vf-clamp › Generate Restricted VFs…**.
3. In the dialog:
   - **Browse…** — pick any exported `.ttf`, `.otf`, `.woff`, or `.woff2`
     variable font from disk.
   - **Named Instances** — tick each instance the customer has licensed.
     Use **All / None / Invert** for bulk selection.
   - **Axis Ranges** — preview the computed hull (e.g. `wght 300-700`).
   - **Output Name** — auto-filled via `compact_name()` (e.g. *Encode Sans
     Light-Bold*); edit freely.
   - **Format** — choose TTF, OTF, WOFF, or WOFF2. WOFF and WOFF2 outputs are
     now properly compressed (not mislabelled sfnt bytes).
   - **Output Folder** — defaults to the same folder as the source font,
     falling back to `~/Desktop` if no font has been loaded yet.
4. Click **Generate**. The button label, spinner, and status line update
   live; the worker runs off the main thread so Glyphs stays responsive.
5. Click **Reveal** to surface the saved file in Finder.

If the target file already exists, the plugin appends `-1`, `-2`, ... rather
than silently overwriting.

---

## Screenshot

A screenshot will be added once the next public Glyphs release is captured.

---

## How It Works

Under the hood the plugin calls
[`fontTools.varLib.instancer`](https://fonttools.readthedocs.io/en/latest/varLib/instancer.html):

1. **`compute_hull`** — finds the bounding box (min/max per axis) across all
   selected named instances. Axes shared by all instances at a single value
   are pinned; axes with variation become a restricted range whose default is
   anchored to a numeric value inside that range.
2. **`instancer.instantiateVariableFont`** — clamps each axis to that range,
   producing a partial instance (still variable) or a pinned static font (if
   only one instance is selected).
3. **`filter_fvar_instances`** — drops named instances the customer did not
   license so the output advertises only the restricted range.
4. **`prune_stat_axis_values`** — removes STAT AxisValue records that fall
   outside the new hull, so OS font menus do not surface unlicensed names.
5. **`patch_name_table`** — updates name IDs 1, 4, 6, 16, 17, 25 across both
   Windows and Mac records (English only; non-English localised records for
   the same IDs are dropped to avoid stale name leakage).
6. **WOFF/WOFF2 flavor** — set on the font before save so the writer produces
   a real WOFF wrapper instead of raw sfnt bytes with a `.woff` extension.

---

## Output Name Logic

`compact_name(first, last)` strips the shared prefix and suffix of the first
and last selected instance names, then joins the differing parts with a
hyphen:

| Selected                                                  | Output                          |
| --------------------------------------------------------- | ------------------------------- |
| Light only                                                | Light                           |
| Light + Bold                                              | Light-Bold                      |
| Encode Sans Light + Encode Sans Bold                      | Encode Sans Light-Bold          |
| Encode Sans Condensed Light + Encode Sans Condensed Bold  | Encode Sans Condensed Light-Bold|

---

## Development

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run the test suite (47 tests, ~0.1s)
pytest

# Rebuild the distributable zip
./scripts/build-zip.sh
```

The `core.py` module is framework-agnostic and imports nothing from Glyphs.app
or AppKit. The Glyphs/AppKit shell lives in `plugin.py`. Tests live in
`tests/` and use `fontTools.fontBuilder` to construct an in-memory variable
font fixture (no external font files needed).

---

## Troubleshooting

**The plugin does not appear under Script › vf-clamp**

- Confirm the plugin is in `~/Library/Application Support/Glyphs 3/Plugins/`
  (not `Glyphs 2/`).
- Restart Glyphs after installing.
- Check **Window › Macro Panel** for Python errors printed at startup.

**"fontTools is not available" / AttributeError on AxisTriple**

`AxisTriple` requires fontTools >= 4.34. fontTools is bundled with Glyphs 3
but older Glyphs builds may carry an older fontTools. Upgrade Glyphs, or
install a newer fontTools into the Python environment Glyphs uses.

**"This font has no variable axes"**

The selected file is a static font, not a variable font. Export a variable
font from your Glyphs source first (`File › Export…`, choose the Variable
Font exporter).

**"WOFF2 output requires the brotli package"**

WOFF2 compression requires the optional `brotli` package. Install it into the
Python environment used by Glyphs, or pick TTF / OTF / WOFF instead.

**Output font looks wrong / instancer raises an error**

Check the Macro Panel for a full traceback. The most common cause is selecting
instances whose axis coordinates push the instancer outside the fvar default
range.

**No named instances appear in the list**

The font's `fvar` table has no named instances, or all instance name IDs are
missing from the `name` table. This can happen with fonts exported from
certain tools. Check your export settings.

---

## Related

- **[vf-clamp npm package](https://vfclamp.com)** — server-side per-purchase
  restricted VF delivery.
- **[@liiift-studio/vf-clamp on npm](https://www.npmjs.com/package/@liiift-studio/vf-clamp)** —
  Vercel function + Sanity integration.

---

## License

MIT — see [LICENSE](LICENSE) — © Liiift Studio
