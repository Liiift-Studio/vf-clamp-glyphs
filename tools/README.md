# tools/

## render_preview.py

Headless snapshot harness for iterating on the dialog UI without restarting Glyphs.

Imports the **exact** `hull_plot.py` and `preview_view.py` modules that ship in the `.glyphsPlugin` bundle and draws them into offscreen PNGs.

### Setup (one-time)

```bash
~/.pyenv/shims/python3 -m pip install --user pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-CoreText
```

(Or any Python with PyObjC. Glyphs itself runs on the system framework Python; this harness uses pyenv so it doesn't pollute Glyphs' runtime.)

### Run

```bash
cd plugins/glyphs
~/.pyenv/shims/python3 tools/render_preview.py --composite
```

Output lands in `tools/snapshots/`:

- `plot.png` — the hull-plot NSView
- `preview.png` — the animated HOHO Anes specimen
- `composite.png` — both stacked, matching the dialog's right column

### Flags

| Flag | Meaning | Default |
|---|---|---|
| `--selected I,J,K` | Indices into the 36-instance fixture | `1,2,8,18,19` |
| `--anim 0..1` | Animation phase (drives probe ring + specimen variation) | `0.4` |
| `--width` / `--plot-height` / `--preview-height` | View dimensions | `370 / 210 / 180` |
| `--font path.ttf` | Register a real font so the specimen uses its glyphs | system fallback |
| `--composite` | Also write the stacked PNG | off |
| `--out-dir` | Where snapshots land | `tools/snapshots/` |

### Workflow

1. Edit `hull_plot.py` or `preview_view.py`.
2. Run the harness.
3. Read the PNG.
4. Iterate.

Only when something looks right do we bump `Info.plist` + `pyproject.toml` and run `scripts/build-zip.sh` for real Glyphs verification.
