#!/usr/bin/env python3
# render_preview.py — headless snapshot harness for the vf-clamp dialog views.
#
# Imports the *exact* hull_plot.py and preview_view.py modules that ship in
# the .glyphsPlugin bundle and draws them into offscreen NSBitmapImageReps.
# Output is one PNG for the hull plot and one for the animated specimen, plus
# an optional --composite that stacks them like the dialog's right column.
#
# The point is a tight feedback loop while iterating on the UI: edit a draw
# method, rerun this script, eyeball the PNGs. No Glyphs restart, no .zip
# rebuild, no manual screenshots. The synthetic fixture mirrors the Daith
# Adv layout (4 opsz × 9 wght = 36 instances) from the v1.2.9 screenshot so
# clipping, label collisions, dot placement, and probe-ring tracking are
# faithful to the production case.

import argparse
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_RESOURCES = os.path.normpath(os.path.join(
	THIS_DIR, '..', 'vf-clamp.glyphsPlugin', 'Contents', 'Resources',
))
sys.path.insert(0, PLUGIN_RESOURCES)

# AppKit graphics need a shared NSApplication before any drawing happens or
# the bitmap rep's context comes back nil and writeToFile_ silently fails.
from AppKit import (  # noqa: E402
	NSApplication, NSBitmapImageRep, NSBitmapImageFileTypePNG,
	NSGraphicsContext, NSColor, NSRectFill, NSWindow, NSBackingStoreBuffered,
	NSImage, NSCompositingOperationSourceOver,
)
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize  # noqa: E402

NSApplication.sharedApplication()

from hull_plot import make_hull_plot_view  # noqa: E402
from preview_view import make_preview_view, ANIM_PERIOD  # noqa: E402


# Fake font for the right column of the dialog. 9 weights × 4 optical sizes
# matches the v1.2.9 Daith Adv screenshot (36 instances) and gives the hull
# plot enough density to see clipping/collision behaviour.
WEIGHTS = [
	('Thin', 190),
	('Extralight', 204),
	('Light', 219),
	('Italic', 235),
	('Medium', 253),
	('Semibold', 271),
	('Bold', 291),
	('Extrabold', 313),
	('Black', 336),
]
SIZES = [12, 24, 42, 60]


def synthetic_fixture():
	"""Return parallel (instances, names) for a 36-instance opsz×wght font."""
	instances = []
	names = []
	for size in SIZES:
		for label, wght in WEIGHTS:
			instances.append({'wght': float(wght), 'opsz': float(size)})
			names.append(f'{size} {label} Italic')
	return instances, names


def hull_from(instances, selected_indices):
	"""Compute the hull dict for the dialog's `_compute_current_hull` path."""
	out = {}
	for idx in selected_indices:
		for tag, val in instances[idx].items():
			if tag not in out:
				out[tag] = [val, val]
			else:
				out[tag][0] = min(out[tag][0], val)
				out[tag][1] = max(out[tag][1], val)
	return {tag: (lo, hi) for tag, (lo, hi) in out.items()}


def render_view_to_png(view, out_path, bg=(0.13, 0.13, 0.13, 1.0)):
	"""Draw ``view`` into an offscreen bitmap and write a PNG.

	The view is attached to a hidden NSWindow first so its backing store
	is valid — drawing a window-less view via cacheDisplayInRect_ silently
	produces a blank bitmap on modern AppKit.
	"""
	bounds = view.bounds()
	w = int(bounds.size.width)
	h = int(bounds.size.height)

	window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
		NSMakeRect(0, 0, w, h), 0, NSBackingStoreBuffered, False,
	)
	window.contentView().addSubview_(view)

	bitmap = view.bitmapImageRepForCachingDisplayInRect_(bounds)
	if bitmap is None:
		print(f'! bitmapImageRepForCachingDisplayInRect_ returned nil for {out_path}')
		return False

	# Paint the dialog-ish dark background first so transparent regions of
	# the view (the hull plot deliberately clears its bg) look like the real
	# Glyphs panel rather than checkerboard transparency.
	ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
	NSGraphicsContext.saveGraphicsState()
	NSGraphicsContext.setCurrentContext_(ctx)
	NSColor.colorWithCalibratedRed_green_blue_alpha_(*bg).set()
	NSRectFill(NSMakeRect(0, 0, w, h))
	NSGraphicsContext.restoreGraphicsState()

	view.cacheDisplayInRect_toBitmapImageRep_(bounds, bitmap)

	data = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
	if data is None:
		print(f'! PNG encode failed for {out_path}')
		return False
	ok = data.writeToFile_atomically_(out_path, True)
	if not ok:
		print(f'! writeToFile_ failed for {out_path}')
	return bool(ok)


def stack_pngs(top_path, bottom_path, out_path, gap=12):
	"""Stack two PNGs vertically into a single composite PNG."""
	top = NSImage.alloc().initWithContentsOfFile_(top_path)
	bottom = NSImage.alloc().initWithContentsOfFile_(bottom_path)
	if top is None or bottom is None:
		print('! stack_pngs: could not read one of the input PNGs')
		return False
	ts = top.size()
	bs = bottom.size()
	w = int(max(ts.width, bs.width))
	h = int(ts.height + bs.height + gap)

	bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
		None, w, h, 8, 4, True, False, 'NSCalibratedRGBColorSpace', 0, 0,
	)
	ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
	NSGraphicsContext.saveGraphicsState()
	NSGraphicsContext.setCurrentContext_(ctx)
	NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.13, 0.13, 1.0).set()
	NSRectFill(NSMakeRect(0, 0, w, h))
	# AppKit origin is bottom-left here. Bottom view goes on first.
	bottom.drawAtPoint_fromRect_operation_fraction_(
		NSMakePoint((w - bs.width) / 2.0, 0),
		NSMakeRect(0, 0, bs.width, bs.height),
		NSCompositingOperationSourceOver, 1.0,
	)
	top.drawAtPoint_fromRect_operation_fraction_(
		NSMakePoint((w - ts.width) / 2.0, bs.height + gap),
		NSMakeRect(0, 0, ts.width, ts.height),
		NSCompositingOperationSourceOver, 1.0,
	)
	NSGraphicsContext.restoreGraphicsState()

	data = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
	return bool(data.writeToFile_atomically_(out_path, True))


def main():
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument('--out-dir', default=os.path.join(THIS_DIR, 'snapshots'))
	p.add_argument('--width', type=int, default=370,
		help='Width of each view in points (matches dialog right column).')
	p.add_argument('--plot-height', type=int, default=210)
	p.add_argument('--preview-height', type=int, default=180)
	p.add_argument('--selected', default='1,2,8,18,19',
		help='Comma-separated indices into the synthetic fixture.')
	p.add_argument('--anim', type=float, default=0.4,
		help='Animation phase 0..1 used for the probe ring + specimen.')
	p.add_argument('--font', default=None,
		help='Optional .ttf/.otf path to register for the specimen view.')
	p.add_argument('--composite', action='store_true',
		help='Also write a stacked plot-over-preview PNG.')
	args = p.parse_args()

	os.makedirs(args.out_dir, exist_ok=True)
	plot_png = os.path.join(args.out_dir, 'plot.png')
	preview_png = os.path.join(args.out_dir, 'preview.png')
	composite_png = os.path.join(args.out_dir, 'composite.png')

	instances, names = synthetic_fixture()
	selected = [int(x) for x in args.selected.split(',') if x.strip()]
	hull = hull_from(instances, selected)

	# Compute axis_ranges from the full fixture so the plot shows the hull
	# in context of the design space — same source of truth the dialog uses.
	axis_ranges = {
		'wght': (float(min(w for _, w in WEIGHTS)),
				 float(WEIGHTS[len(WEIGHTS) // 2][1]),
				 float(max(w for _, w in WEIGHTS))),
		'opsz': (float(min(SIZES)), float(SIZES[0]), float(max(SIZES))),
	}
	axis_colors = {
		'wght': (0.46, 0.74, 1.00),
		'opsz': (1.00, 0.68, 0.42),
	}

	print(f'Selected indices : {selected}')
	print(f'Selected names   : {[names[i] for i in selected]}')
	print(f'Hull             : {hull}')

	# ---- Hull plot ----
	plot = make_hull_plot_view((0, 0, args.width, args.plot_height))
	if plot is None:
		print('! make_hull_plot_view returned None — AppKit missing?')
		return 1
	plot.setHull_axisRanges_axisColors_(hull, axis_ranges, axis_colors)
	plot.setInstances_selectedIndices_onClick_(instances, selected, None)
	if 'wght' in hull and 'opsz' in hull:
		wlo, whi = hull['wght']
		olo, ohi = hull['opsz']
		plot.setProbeCoords_({
			'wght': wlo + (whi - wlo) * args.anim,
			'opsz': olo + (ohi - olo) * (1.0 - args.anim),
		})

	render_view_to_png(plot, plot_png)
	print(f'  → {plot_png}')

	# ---- Animated specimen ----
	preview = make_preview_view((0, 0, args.width, args.preview_height))
	if preview is None:
		print('! make_preview_view returned None — AppKit missing?')
		return 1
	preview.setFontSize_(60.0)
	preview.setHull_(hull)
	# Manually advance the animation phase since no NSTimer is running.
	preview._anim_progress = args.anim * ANIM_PERIOD

	if args.font:
		try:
			from font_registration import register_font_at_path
			ok, descriptor = register_font_at_path(args.font)
			if ok and descriptor is not None:
				preview.setFontDescriptor_(descriptor)
				print(f'  + registered {args.font} for specimen')
			else:
				print(f'  ! font registration failed for {args.font}')
		except Exception as e:  # noqa: BLE001
			print(f'  ! font registration raised: {e}')

	render_view_to_png(preview, preview_png)
	print(f'  → {preview_png}')

	if args.composite:
		stack_pngs(plot_png, preview_png, composite_png)
		print(f'  → {composite_png}')

	return 0


if __name__ == '__main__':
	sys.exit(main())
