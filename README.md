# vf-clamp-glyphs

A [Glyphs.app](https://glyphsapp.com) plugin that generates restricted variable fonts from any exported TTF/OTF variable font file. Select the named instances a customer has licensed, click Generate, and receive a micro-VF that spans exactly that range — with the name table updated to match.

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

> **Important:** this plugin works on an already-exported variable font file (`.ttf` or `.otf`), not on a `.glyphs` source. Export your variable font from Glyphs first, then run this plugin on the exported file.

1. Open Glyphs 3 (no document needs to be open).
2. Go to **Script › vf-clamp › Generate Restricted VFs…**
3. In the dialog:
   - **Browse…** — pick any exported `.ttf` or `.otf` variable font from disk.
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

1. **`compute_hull`** — finds the bounding box (min/max per axis) across all selected named instances. Axes shared by all instances at a single value are pinned; axes with variation become a restricted range.
2. **`instancer.instantiateVariableFont`** — clamps each axis to that range, producing a partial instance (still variable) or a pinned static font (if only one instance is selected).
3. **`patch_name_table`** — updates name IDs 1, 4, 6 (and optionally 16 and 25) so the font reports the correct family name and PostScript name.
4. The result is saved as TTF/OTF/WOFF/WOFF2 using fonttools' built-in writer.

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

## Troubleshooting

**The plugin does not appear under Script › vf-clamp**

- Confirm the plugin is in `~/Library/Application Support/Glyphs 3/Plugins/` (not `Glyphs 2/`).
- Restart Glyphs after installing.
- Check **Window › Macro Panel** for Python errors printed at startup.

**"fonttools is not available" error**

fonttools is bundled with Glyphs 3. If you see this error, your Glyphs installation may be damaged. Try reinstalling Glyphs from [glyphsapp.com](https://glyphsapp.com).

**"This font has no variable axes"**

The selected file is a static font, not a variable font. Export a variable font from your Glyphs source first (`File › Export…`, choose the Variable Font exporter).

**Output font looks wrong / instancer raises an error**

Some variable fonts have unusual axis configurations that trip up the instancer. Check the Macro Panel for a full traceback. The most common cause is selecting instances whose axis coordinates push the instancer outside the fvar default range.

**No named instances appear in the list**

The font's `fvar` table has no named instances, or all instance name IDs are missing from the `name` table. This can happen with fonts exported from certain tools. Check your export settings.

---

## Related

- **[vf-clamp npm package](https://vfclamp.com)** — server-side per-purchase restricted VF delivery
- **[@liiift-studio/vf-clamp on npm](https://www.npmjs.com/package/@liiift-studio/vf-clamp)** — Vercel function + Sanity integration

---

## License

MIT — see [LICENSE](LICENSE) — © Liiift Studio
