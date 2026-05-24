# vf-clamp-glyphs

A [Glyphs.app](https://glyphsapp.com) plugin that generates restricted variable fonts from any TTF/OTF variable font file. Select the named instances a customer has licensed, click Generate, and receive a micro-VF that spans exactly that range — with the name table updated to match.

This is the Glyphs.app companion to the [`@liiift-studio/vf-clamp`](https://vfclamp.com) npm package, which does the same thing server-side for per-purchase delivery.

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Glyphs.app | 3.x |
| Python | 3.x (bundled with Glyphs 3) |
| fonttools | Any recent version (bundled with Glyphs 3) |
| vanilla | Any version (bundled with Glyphs 3) |

No Node.js, npm, or external dependencies required.

---

## Installation

**Option A — double-click**

Double-click `vf-clamp.glyphsPlugin` in Finder. Glyphs installs it automatically.

**Option B — manual drag**

Copy `vf-clamp.glyphsPlugin` to:

```
~/Library/Application Support/Glyphs 3/Plugins/
```

Restart Glyphs 3. The plugin appears under **Script › vf-clamp › Generate Restricted VFs…**

---

## Usage

1. Open Glyphs 3 (no document needs to be open).
2. Go to **Script › vf-clamp › Generate Restricted VFs…**
3. In the dialog:
   - **Browse…** — pick any `.ttf` or `.otf` variable font from disk.
   - **Named Instances** — tick each instance the customer has licensed.
   - **Output Name** — auto-filled via `compact_name()` (e.g. *Encode Sans Light-Bold*); edit freely.
   - **Format** — choose TTF, OTF, WOFF, or WOFF2.
   - **Output Folder** — defaults to the same folder as the source font.
4. Click **Generate**.

The restricted VF is saved to the output folder with the chosen name and extension.

---

## Screenshot

_Screenshot coming soon._

---

## How It Works

Under the hood the plugin calls [`fontTools.varLib.instancer`](https://fonttools.readthedocs.io/en/latest/varLib/instancer.html):

1. **`compute_hull`** — finds the bounding box (min/max per axis) across all selected named instances.
2. **`instancer.instantiateVariableFont`** — clamps each axis to that range, producing a partial instance.
3. **`patch_name_table`** — updates name IDs 1, 4, 6 (and optionally 16 and 25) so the font reports the correct family name and PostScript name.
4. The result is saved as TTF/OTF/WOFF/WOFF2 using fonttools' built-in writer.

Axes that collapse to a single value (because all selected instances share that coordinate) are pinned — the axis is removed from the output font entirely.

---

## Output Name Logic

`compact_name(first, last)` strips the shared prefix and suffix of the first and last selected instance names, then joins the differing parts with a hyphen:

| Selected | Output |
|----------|--------|
| Light only | Light |
| Light + Bold | Light-Bold |
| Encode Sans Light + Encode Sans Bold | Encode Sans Light-Bold |
| Encode Sans Condensed Light + Encode Sans Condensed Bold | Encode Sans Condensed Light-Bold |

---

## Related

- **[vf-clamp npm package](https://vfclamp.com)** — server-side per-purchase restricted VF delivery
- **[@liiift-studio/vf-clamp on npm](https://www.npmjs.com/package/@liiift-studio/vf-clamp)** — Vercel function + Sanity integration

---

## License

MIT — © Liiift Studio
