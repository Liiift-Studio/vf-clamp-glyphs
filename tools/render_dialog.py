#!/usr/bin/env python3
# render_dialog.py — full-dialog snapshot harness for vf-clamp Glyphs plugin.
#
# Sibling to render_preview.py. That one renders only the two custom NSViews
# (hull plot + animated specimen). This one paints the *entire* dialog frame
# — instance list, source picker, output options, action bar, log pane — and
# mounts the real HullPlotView and AnimatedPreviewView inside it, so we can
# review layout decisions (zone heights, column widths, widget alignment,
# action-bar clipping, log pane crowding) without launching Glyphs.
#
# Geometry is the SAME dimensions plugin.py uses: W=820, PAD=16, ZONE1_H=96,
# ZONE2_H=348, ZONE3_H=174, LOG_H=84, ACTION_BAR_H=64. The chrome widgets
# are drawn as plain NSBezierPath shapes (we don't need NSButton/etc. to be
# functional — we just need them to occupy the right space).

import argparse
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_RESOURCES = os.path.normpath(os.path.join(
	THIS_DIR, '..', 'vf-clamp.glyphsPlugin', 'Contents', 'Resources',
))
sys.path.insert(0, PLUGIN_RESOURCES)

from AppKit import (  # noqa: E402
	NSApplication, NSBitmapImageRep, NSBitmapImageFileTypePNG,
	NSGraphicsContext, NSColor, NSRectFill, NSWindow, NSBackingStoreBuffered,
	NSImage, NSCompositingOperationSourceOver, NSAppearance,
	NSBezierPath, NSFont, NSAttributedString, NSMutableParagraphStyle,
	NSTextAlignmentCenter, NSTextAlignmentLeft, NSTextAlignmentRight,
	NSForegroundColorAttributeName, NSFontAttributeName,
	NSParagraphStyleAttributeName, NSView,
)
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize  # noqa: E402

NSApplication.sharedApplication()

from hull_plot import make_hull_plot_view  # noqa: E402
from preview_view import make_preview_view, ANIM_PERIOD  # noqa: E402
from render_preview import (  # noqa: E402
	synthetic_fixture, hull_from, WEIGHTS, SIZES,
)


# Dialog geometry — verbatim from plugin.py (kept in sync by eye).
W = 820
PAD = 16
ZONE1_H = 96
ZONE2_H = 348
ZONE3_H = 174
LOG_H = 84
ACTION_BAR_H = 64
COL_GAP = 16
BOX_INSET = 12  # vanilla.Box interior padding


def total_height():
	"""Match VFClampDialog._compute_window_height()."""
	return (
		PAD + ZONE1_H + PAD + ZONE2_H + PAD + ZONE3_H + PAD
		+ LOG_H + PAD + ACTION_BAR_H + PAD
	)


# ---------------------------------------------------------------------------
# Drawing helpers — plain NSBezierPath shapes that look like the AppKit
# widgets they're standing in for. None of them are interactive; they only
# need to occupy the right space.
# ---------------------------------------------------------------------------

def _para(align):
	p = NSMutableParagraphStyle.alloc().init()
	p.setAlignment_(align)
	return p


def _text(s, point, size=12.0, color=None, align=NSTextAlignmentLeft, bold=False):
	"""Draw a one-line label at ``point``."""
	if color is None:
		color = NSColor.labelColor()
	font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
	attrs = {
		NSFontAttributeName: font,
		NSForegroundColorAttributeName: color,
		NSParagraphStyleAttributeName: _para(align),
	}
	NSAttributedString.alloc().initWithString_attributes_(s, attrs).drawAtPoint_(point)


def _text_in_rect(s, rect, size=12.0, color=None, align=NSTextAlignmentLeft, bold=False):
	"""Draw a label clipped to a rect, vertically centred."""
	if color is None:
		color = NSColor.labelColor()
	font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
	attrs = {
		NSFontAttributeName: font,
		NSForegroundColorAttributeName: color,
		NSParagraphStyleAttributeName: _para(align),
	}
	att = NSAttributedString.alloc().initWithString_attributes_(s, attrs)
	tsize = att.size()
	y = rect.origin.y + (rect.size.height - tsize.height) / 2.0
	if align == NSTextAlignmentCenter:
		x = rect.origin.x + (rect.size.width - tsize.width) / 2.0
	elif align == NSTextAlignmentRight:
		x = rect.origin.x + rect.size.width - tsize.width - 4
	else:
		x = rect.origin.x + 4
	att.drawAtPoint_(NSMakePoint(x, y))


def _zone_box(x, y, w, h, title):
	"""Stylise a vanilla.Box: faint rounded rect + uppercase title chip."""
	r = NSMakeRect(x, y, w, h)
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.04).set()
	NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6.0, 6.0).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10).set()
	border = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6.0, 6.0)
	border.setLineWidth_(1.0)
	border.stroke()
	_text(
		title.upper(),
		NSMakePoint(x + 10, y + h - 18),
		size=10.0,
		color=NSColor.tertiaryLabelColor(),
		bold=True,
	)


def _popup(x, y, w, h, text):
	"""Stylise an NSPopUpButton: rounded rect + label + chevron."""
	r = NSMakeRect(x, y, w, h)
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.08).set()
	NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 4.0, 4.0).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).set()
	b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 4.0, 4.0)
	b.setLineWidth_(1.0)
	b.stroke()
	_text_in_rect(text, NSMakeRect(x + 4, y, w - 22, h))
	# Chevron
	cx = x + w - 12
	cy = y + h / 2.0
	chev = NSBezierPath.bezierPath()
	chev.moveToPoint_(NSMakePoint(cx - 4, cy + 2))
	chev.lineToPoint_(NSMakePoint(cx, cy - 2))
	chev.lineToPoint_(NSMakePoint(cx + 4, cy + 2))
	NSColor.tertiaryLabelColor().set()
	chev.setLineWidth_(1.5)
	chev.stroke()


def _field(x, y, w, h, text, placeholder=False):
	"""Stylise an NSTextField with text or placeholder."""
	r = NSMakeRect(x, y, w, h)
	NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.30).set()
	NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 3.0, 3.0).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.16).set()
	b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 3.0, 3.0)
	b.setLineWidth_(1.0)
	b.stroke()
	color = NSColor.tertiaryLabelColor() if placeholder else NSColor.labelColor()
	_text_in_rect(text, NSMakeRect(x + 4, y, w - 8, h), color=color)


def _button(x, y, w, h, text, primary=False):
	"""Stylise an NSButton: filled accent if primary, outlined otherwise."""
	r = NSMakeRect(x, y, w, h)
	if primary:
		NSColor.controlAccentColor().set()
		NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6.0, 6.0).fill()
		_text_in_rect(text, r, color=NSColor.whiteColor(), align=NSTextAlignmentCenter, bold=True)
	else:
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10).set()
		NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6.0, 6.0).fill()
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).set()
		b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6.0, 6.0)
		b.setLineWidth_(1.0)
		b.stroke()
		_text_in_rect(text, r, align=NSTextAlignmentCenter)


def _checkbox(x, y, size, checked, label, color=None):
	"""Stylise a checkbox with optional adjacent label."""
	r = NSMakeRect(x, y, size, size)
	NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.30).set()
	NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 2.5, 2.5).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.30).set()
	b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 2.5, 2.5)
	b.setLineWidth_(1.0)
	b.stroke()
	if checked:
		NSColor.controlAccentColor().set()
		inner = NSMakeRect(x + 2.5, y + 2.5, size - 5, size - 5)
		NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inner, 1.5, 1.5).fill()
		# Checkmark
		check = NSBezierPath.bezierPath()
		check.moveToPoint_(NSMakePoint(x + 4, y + size / 2))
		check.lineToPoint_(NSMakePoint(x + size / 2 - 1, y + 4.5))
		check.lineToPoint_(NSMakePoint(x + size - 3.5, y + size - 4))
		NSColor.whiteColor().set()
		check.setLineWidth_(1.6)
		check.stroke()
	if label:
		_text_in_rect(
			label,
			NSMakeRect(x + size + 6, y - 2, 400, size + 4),
			color=color,
		)


def _radio(x, y, size, checked, label):
	"""Stylise an NSRadio button + adjacent label."""
	r = NSMakeRect(x, y, size, size)
	NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.30).set()
	NSBezierPath.bezierPathWithOvalInRect_(r).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.30).set()
	b = NSBezierPath.bezierPathWithOvalInRect_(r)
	b.setLineWidth_(1.0)
	b.stroke()
	if checked:
		inner = NSMakeRect(x + 3, y + 3, size - 6, size - 6)
		NSColor.controlAccentColor().set()
		NSBezierPath.bezierPathWithOvalInRect_(inner).fill()
	_text_in_rect(label, NSMakeRect(x + size + 6, y - 2, 200, size + 4))


# ---------------------------------------------------------------------------
# Custom NSView that paints the full dialog frame + mounts the real
# HullPlotView and AnimatedPreviewView as subviews.
# ---------------------------------------------------------------------------

class DialogMockView(NSView):
	"""Draws the chrome (zones, list rows, popups, log) so the real custom
	views in zone 2 read in context. isFlipped=True so coords match the
	dialog's own top-left origin convention.
	"""

	def isFlipped(self):
		return True

	def setMockState_(self, state):
		self._state = state

	def drawRect_(self, rect):
		st = self._state
		bounds = self.bounds()
		w = bounds.size.width

		# Window background.
		NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.14, 0.14, 1.0).set()
		NSRectFill(bounds)

		# Title bar.
		NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.10, 0.10, 1.0).set()
		NSRectFill(NSMakeRect(0, 0, w, 28))
		_text_in_rect(
			'◇ vf-clamp — Generate Restricted VFs',
			NSMakeRect(0, 0, w, 28),
			size=12.5,
			color=NSColor.secondaryLabelColor(),
			align=NSTextAlignmentCenter,
		)
		# Traffic lights
		for i, rgb in enumerate([
			(0.97, 0.36, 0.34), (0.99, 0.74, 0.18), (0.20, 0.78, 0.35),
		]):
			NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, 1.0).set()
			NSBezierPath.bezierPathWithOvalInRect_(
				NSMakeRect(10 + i * 18, 9, 11, 11),
			).fill()

		# Window content starts below title bar.
		y = 28 + PAD
		# --- Zone 1: Source -------------------------------------------------
		_zone_box(PAD, y, w - 2 * PAD, ZONE1_H, 'Source')
		row1y = y + 30
		_radio(PAD + BOX_INSET, row1y, 14, st['source'] == 'open', 'Open Font')
		_radio(PAD + BOX_INSET + 110, row1y, 14, st['source'] == 'file', 'File')
		row2y = y + 56
		_popup(
			PAD + BOX_INSET, row2y,
			w - 2 * PAD - 2 * BOX_INSET, 24,
			st['font_label'],
		)
		y += ZONE1_H + PAD

		# --- Zone 2: Dashboard ----------------------------------------------
		_zone_box(PAD, y, w - 2 * PAD, ZONE2_H, '')
		inner_w = w - 2 * PAD - 2 * BOX_INSET
		col_w = (inner_w - COL_GAP) // 2
		left_x = PAD + BOX_INSET
		right_x = left_x + col_w + COL_GAP

		# Left column — instance list header + filter + select buttons + list
		_text(
			f'INSTANCES ({len(st["checked"])} OF {len(st["instances"])})',
			NSMakePoint(left_x, y + 6),
			size=10.5,
			color=NSColor.tertiaryLabelColor(),
			bold=True,
		)
		_field(left_x, y + 28, col_w, 22, st.get('filter', ''), placeholder=not st.get('filter'))
		# Select All / None / Invert + More
		btnw = 50
		bx = left_x
		for label in ('All', 'None', 'Invert'):
			_button(bx, y + 58, btnw, 22, label)
			bx += btnw + 4
		_popup(bx, y + 58, 70, 22, '▾ More')

		# Instance list rows
		list_y = y + 88
		list_h = ZONE2_H - (list_y - y) - 12
		# List border
		lr = NSMakeRect(left_x, list_y, col_w, list_h)
		NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.20).set()
		NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(lr, 4.0, 4.0).fill()
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.14).set()
		bp = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(lr, 4.0, 4.0)
		bp.setLineWidth_(1.0)
		bp.stroke()
		row_h = 18
		visible_rows = int(list_h / row_h)
		for i in range(min(visible_rows, len(st['instances']))):
			ry = list_y + 4 + i * row_h
			if i == st.get('focus_row', -1):
				NSColor.controlAccentColor().colorWithAlphaComponent_(0.55).set()
				NSRectFill(NSMakeRect(left_x + 2, ry - 1, col_w - 4, row_h))
			_checkbox(left_x + 8, ry + 2, 12, i in st['checked'], '')
			# Name + axis values
			name = st['names'][i]
			coords = st['instances'][i]
			vals = '  '.join(f'{k}={int(v)}' for k, v in coords.items())
			_text(name, NSMakePoint(left_x + 28, ry + 1), size=11.0)
			_text(
				vals, NSMakePoint(left_x + 28 + 130, ry + 1),
				size=11.0, color=NSColor.tertiaryLabelColor(),
			)

		# Right column — heading + size estimate + preview name
		_text(
			'OUTPUT PREVIEW',
			NSMakePoint(right_x, y + 6),
			size=10.5,
			color=NSColor.tertiaryLabelColor(),
			bold=True,
		)
		_text(
			st.get('preview_name', '(set output name)'),
			NSMakePoint(right_x, y + 26),
			size=11.0,
			color=NSColor.secondaryLabelColor(),
		)
		# Hull plot — same dimensions plugin.py uses (plot_y_box=60, plot_h=175).
		plot_y = y + 60
		plot_h = 175
		# (Actual HullPlotView is added as a subview by the caller — its
		# frame matches this rectangle. We just leave the rect blank here.)

		# Size estimate label + selection count — both sit between the hull
		# plot and the animated specimen so the user can see how many
		# instances are licensed and how big the output will be.
		_text(
			st.get('size_estimate', ''),
			NSMakePoint(right_x, plot_y + plot_h + 6),
			size=10.5,
			color=NSColor.secondaryLabelColor(),
		)
		_text(
			f'{len(st["checked"])} instances selected',
			NSMakePoint(right_x + col_w - 130, plot_y + plot_h + 6),
			size=10.5,
			color=NSColor.tertiaryLabelColor(),
		)

		# (AnimatedPreviewView added as subview below the size estimate.)

		y += ZONE2_H + PAD

		# --- Zone 3: Output -------------------------------------------------
		_zone_box(PAD, y, w - 2 * PAD, ZONE3_H, 'Output')
		LABEL_W = 90
		LABEL_X = PAD + BOX_INSET
		CTRL_X = LABEL_X + LABEL_W + 8
		ctrl_w = w - 2 * PAD - 2 * BOX_INSET - LABEL_W - 8

		def row(label, control, value, ry, ctrl_w_override=None):
			_text_in_rect(
				label,
				NSMakeRect(LABEL_X, ry, LABEL_W, 22),
				size=11.5, color=NSColor.secondaryLabelColor(),
				align=NSTextAlignmentRight,
			)
			cw = ctrl_w_override if ctrl_w_override is not None else ctrl_w
			if control == 'popup':
				_popup(CTRL_X, ry, 240, 22, value)
			elif control == 'field':
				_field(CTRL_X, ry, cw, 22, value, placeholder=False)
			elif control == 'compound':
				# Field + Browse + Recent popup
				field_w = cw - 110
				_field(CTRL_X, ry, field_w, 22, value, placeholder=True)
				_button(CTRL_X + field_w + 4, ry, 60, 22, 'Choose…')
				_popup(CTRL_X + field_w + 4 + 60 + 4, ry, 76, 22, '▾ Recent')

		row('Preset:', 'popup', st['preset'], y + 14)
		row('Output Name:', 'field', st['output_name'], y + 46)
		row('Format:', 'popup', st['format'], y + 78)
		# Format hint
		_text(
			st.get('format_hint', ''),
			NSMakePoint(CTRL_X + 248, y + 82),
			size=10.5,
			color=NSColor.tertiaryLabelColor(),
		)
		row('Folder:', 'compound', st.get('folder', ''), y + 110)
		# Checkbox
		_checkbox(
			CTRL_X, y + 142, 14, st.get('open_after', True),
			'Open output in Glyphs (or default app) after generating',
		)
		y += ZONE3_H + PAD

		# --- LOG pane -------------------------------------------------------
		_zone_box(PAD, y, w - 2 * PAD, LOG_H, '')
		_text(
			'LOG',
			NSMakePoint(PAD + 10, y + 6),
			size=10.5, color=NSColor.tertiaryLabelColor(), bold=True,
		)
		log_lines = st.get('log', [])
		for i, line in enumerate(log_lines[-3:]):
			_text(
				line, NSMakePoint(PAD + 12, y + 26 + i * 14),
				size=11.0, color=NSColor.secondaryLabelColor(),
			)
		y += LOG_H + PAD

		# --- Action bar -----------------------------------------------------
		# Left side: small toggle buttons that mirror the +A select-all chips.
		ab_y = y + 18
		left_chips = ['⌥A All', '⌥N None', '⌥I Invert', '↵ Generate']
		cx = PAD
		for chip in left_chips:
			_text(
				chip, NSMakePoint(cx, ab_y + 4),
				size=10.5, color=NSColor.secondaryLabelColor(),
			)
			cx += 70
		# Right side: Cancel + Generate
		right_x_btn = w - PAD - 140 - 8 - 80
		_button(right_x_btn, ab_y - 6, 80, 28, 'Cancel')
		_button(right_x_btn + 80 + 8, ab_y - 6, 140, 32, 'Generate', primary=True)


# ---------------------------------------------------------------------------
# Compose + render
# ---------------------------------------------------------------------------

def fake_state(selected_indices, log_lines=None):
	instances, names = synthetic_fixture()
	checked = set(int(i) for i in selected_indices)
	hull = hull_from(instances, list(checked))
	selected = sorted(checked)
	def fmt_axis(tag, lo, hi):
		return f'{tag} {int(lo)}' if lo == hi else f'{tag} {int(lo)}–{int(hi)}'
	preview_name = (
		f'Daith Adv {int(min(SIZES))} {names[selected[0]].split(" ", 1)[1]} '
		f'{int(max(SIZES))} {names[selected[-1]].split(" ", 1)[1]}.glyphs'
		if selected else '(set output name)'
	)
	return {
		'source': 'open',
		'font_label': 'Daith Adv  (Daith-Italic Adv2 v2.glyphspackage)',
		'instances': instances,
		'names': names,
		'checked': checked,
		'focus_row': selected[-1] if selected else -1,
		'filter': '',
		'preview_name': preview_name,
		'size_estimate': f'≈ {18 + len(selected) * 4} KB',
		'preset': '(no preset)',
		'output_name': f'Daith Adv {int(min(SIZES))} Extralight-42 Light Italic',
		'format': '.glyphs',
		'format_hint': '.glyphs source file you can open in Glyphs',
		'folder': 'Default: same folder as source',
		'open_after': True,
		'log': log_lines or [
			'Ready. Pick instances and click Generate.',
			'Loading open Glyphs font…',
		],
		'hull': hull,
	}


def render_dialog(state, anim_phase, out_path, font_path=None):
	"""Render the full dialog mock + real custom views to PNG."""
	h = total_height()
	root = DialogMockView.alloc().initWithFrame_(NSMakeRect(0, 0, W, h))
	root.setMockState_(state)

	# Mount real HullPlotView + AnimatedPreviewView at the right column
	# coordinates. The mock view's drawRect_ leaves those rects empty.
	zone2_y = 28 + PAD + ZONE1_H + PAD
	right_x = PAD + BOX_INSET + ((W - 2 * PAD - 2 * BOX_INSET - COL_GAP) // 2) + COL_GAP
	col_w = (W - 2 * PAD - 2 * BOX_INSET - COL_GAP) // 2

	plot_y = zone2_y + 60
	plot_h = 175
	plot = make_hull_plot_view((right_x, plot_y, col_w, plot_h))
	# axis_ranges wider than fixture so the hull rect sits as a subset.
	axis_ranges = {
		'wght': (100.0, 400.0, 900.0),
		'opsz': (8.0, 12.0, 72.0),
	}
	axis_colors = {
		'wght': (0.46, 0.74, 1.00),
		'opsz': (1.00, 0.68, 0.42),
	}
	plot.setHull_axisRanges_axisColors_(state['hull'], axis_ranges, axis_colors)
	plot.setInstances_selectedIndices_onClick_(
		state['instances'], sorted(state['checked']), None,
	)
	# Snapshot the live-toggle highlight at a chosen point in the animation
	# so we can review what the ring looks like mid-pulse. age must be < 0.4
	# (HIGHLIGHT_DURATION); higher values produce no visible ring.
	if state.get('highlight_idx') is not None:
		plot._toggle_highlight_idx = int(state['highlight_idx'])
		plot._toggle_highlight_age = float(state.get('highlight_age', 0.15))
	if 'wght' in state['hull'] and 'opsz' in state['hull']:
		wlo, whi = state['hull']['wght']
		olo, ohi = state['hull']['opsz']
		plot.setProbeCoords_({
			'wght': wlo + (whi - wlo) * anim_phase,
			'opsz': olo + (ohi - olo) * (1.0 - anim_phase),
		})
	root.addSubview_(plot)

	# Same math as plugin.py (v1.2.12): preview sits 22 px below plot bottom,
	# specimen at 54 pt to fit the tighter preview region.
	preview_y = plot_y + plot_h + 22
	preview_h = ZONE2_H - (preview_y - zone2_y) - 10
	preview = make_preview_view((right_x, preview_y, col_w, preview_h))
	preview.setFontSize_(32.0)
	preview.setHull_(state['hull'])
	preview._anim_progress = anim_phase * ANIM_PERIOD
	if font_path:
		from font_registration import register_font_at_path
		ok, descriptor = register_font_at_path(font_path)
		if ok and descriptor is not None:
			preview.setFontDescriptor_(descriptor)
	root.addSubview_(preview)

	# Render through the same dark-bg composite path as render_preview.
	window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
		NSMakeRect(0, 0, W, h), 0, NSBackingStoreBuffered, False,
	)
	try:
		window.setAppearance_(NSAppearance.appearanceNamed_('NSAppearanceNameDarkAqua'))
	except (AttributeError, RuntimeError):
		pass
	window.contentView().addSubview_(root)
	try:
		root.setAppearance_(NSAppearance.appearanceNamed_('NSAppearanceNameDarkAqua'))
	except (AttributeError, RuntimeError):
		pass

	transp = root.bitmapImageRepForCachingDisplayInRect_(root.bounds())
	root.cacheDisplayInRect_toBitmapImageRep_(root.bounds(), transp)

	dark = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
		None, W, h, 8, 4, True, False, 'NSCalibratedRGBColorSpace', 0, 0,
	)
	ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(dark)
	NSGraphicsContext.saveGraphicsState()
	NSGraphicsContext.setCurrentContext_(ctx)
	NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.14, 0.14, 1.0).set()
	NSRectFill(NSMakeRect(0, 0, W, h))
	img = NSImage.alloc().initWithSize_(NSMakeSize(W, h))
	img.addRepresentation_(transp)
	img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
		NSMakeRect(0, 0, W, h),
		NSMakeRect(0, 0, W, h),
		NSCompositingOperationSourceOver,
		1.0,
		True,
		None,
	)
	NSGraphicsContext.restoreGraphicsState()

	data = dark.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
	return bool(data.writeToFile_atomically_(out_path, True))


def main():
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument('--out', default=os.path.join(THIS_DIR, 'snapshots', 'dialog.png'))
	p.add_argument('--selected', default='1,2,8,18,19')
	p.add_argument('--anim', type=float, default=0.4)
	p.add_argument('--font', default=None)
	p.add_argument('--highlight-idx', type=int, default=None,
		help='Instance index to freeze mid-toggle-highlight.')
	p.add_argument('--highlight-age', type=float, default=0.15,
		help='Age of the highlight in seconds (0..0.4).')
	args = p.parse_args()

	os.makedirs(os.path.dirname(args.out), exist_ok=True)
	state = fake_state([int(x) for x in args.selected.split(',') if x.strip()])
	if args.highlight_idx is not None:
		state['highlight_idx'] = args.highlight_idx
		state['highlight_age'] = args.highlight_age
	ok = render_dialog(state, args.anim, args.out, font_path=args.font)
	if ok:
		print(f'→ {args.out}  ({W}×{total_height()})')
	else:
		print(f'! render failed for {args.out}')
		return 1
	return 0


if __name__ == '__main__':
	sys.exit(main())
