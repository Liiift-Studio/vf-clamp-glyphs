# plugin.py — vf-clamp Glyphs.app plugin shell around core.py.
# Pure UI/registration concerns; all fonttools work lives in core.py.

import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, List, Optional

import objc
from PyObjCTools import AppHelper

# Explicit AppKit imports — do not rely on `from GlyphsApp import *` re-exports
from AppKit import (
	NSMenuItem,
	NSOpenPanel,
	NSModalResponseOK,
	NSWorkspace,
)

from GlyphsApp import Glyphs, Message, SCRIPT_MENU
from GlyphsApp.plugins import GeneralPlugin

import vanilla

# AppKit primitives used for the colored axis chips + key-equivalent buttons.
from AppKit import (
	NSColor,
	NSAttributedString,
	NSMutableAttributedString,
	NSForegroundColorAttributeName,
	NSFontAttributeName,
	NSFont,
	NSTextAlignmentRight,
)


# ---------------------------------------------------------------------------
# Visual palette — small accent colors per axis, mirroring the website's
# golden-angle hue system. Two variants: dark-mode (lighter, less saturated to
# read against dark translucent panels) and light-mode (deeper to retain WCAG
# contrast against off-white). Selected at render time via NSAppearance.
# Values are sRGB 0..1 floats.
# ---------------------------------------------------------------------------

AXIS_COLORS_DARK = {
	'wght': (0.40, 0.66, 0.96),   # blue
	'wdth': (0.42, 0.78, 0.52),   # green
	'opsz': (0.66, 0.55, 0.92),   # purple
	'slnt': (0.96, 0.66, 0.42),   # orange
	'ital': (0.96, 0.55, 0.76),   # pink
	'GRAD': (0.95, 0.86, 0.45),   # yellow
}
AXIS_COLORS_LIGHT = {
	'wght': (0.10, 0.36, 0.78),   # blue
	'wdth': (0.10, 0.50, 0.22),   # green
	'opsz': (0.36, 0.18, 0.72),   # purple
	'slnt': (0.78, 0.36, 0.06),   # orange
	'ital': (0.74, 0.20, 0.46),   # pink
	'GRAD': (0.62, 0.46, 0.04),   # yellow
}
# Back-compat alias — older docstrings reference AXIS_COLORS.
AXIS_COLORS = AXIS_COLORS_DARK
DEFAULT_AXIS_COLOR_DARK = (0.60, 0.60, 0.60)
DEFAULT_AXIS_COLOR_LIGHT = (0.30, 0.30, 0.30)
DEFAULT_AXIS_COLOR = DEFAULT_AXIS_COLOR_DARK


def _is_dark_appearance():
	"""Return True when the current effective appearance is a Dark Aqua variant."""
	try:
		from AppKit import NSApp, NSAppearanceNameDarkAqua
		appearance = NSApp().effectiveAppearance() if NSApp() is not None else None
		if appearance is None:
			return True
		match = appearance.bestMatchFromAppearancesWithNames_([NSAppearanceNameDarkAqua])
		return match == NSAppearanceNameDarkAqua
	except (AttributeError, ImportError, RuntimeError):
		# Default to dark — historical Glyphs default and least-bad fallback.
		return True


def _nscolor_for_axis(tag):
	"""Return an NSColor for the small chip that sits next to ``tag`` in the hull preview.

	Picks the dark- or light-mode palette to maintain contrast in both
	system appearances. Falls back to the dark palette if appearance lookup
	fails (matches Glyphs' historical default).
	"""
	if _is_dark_appearance():
		palette = AXIS_COLORS_DARK
		default = DEFAULT_AXIS_COLOR_DARK
	else:
		palette = AXIS_COLORS_LIGHT
		default = DEFAULT_AXIS_COLOR_LIGHT
	rgb = palette.get(tag, default)
	return NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], 1.0)

# Make Resources/ importable so this file can pull in its sibling core.py
_RESOURCES_DIR = os.path.dirname(os.path.abspath(__file__))
if _RESOURCES_DIR not in sys.path:
	sys.path.insert(0, _RESOURCES_DIR)

from core import (  # noqa: E402  (deferred import after sys.path mutation)
	compact_name,
	compute_default_output_name,
	check_fonttools_version,
	extension_for_format,
	flavor_for_format,
	get_axis_hull_from_instances,
	get_instance_names,
	produce_restricted_vf,
	safe_output_path,
	# Public capability helpers (replace earlier reaches into _FONTTOOLS_*).
	is_fonttools_ready,
	fonttools_import_error,
	is_glyphs_app_available,
	open_font_safely,
	# Glyphs-source path (Phase 1+2: clamp open GSFont → .glyphs or binary)
	list_open_glyphs_fonts,
	gsfont_label,
	gsfont_instance_names,
	compute_gsfont_hull,
	clamp_gsfont,
	save_gsfont_to_glyphs,
	export_gsfont_binary_via_glyphs,
)


# ---------------------------------------------------------------------------
# Glyphs Plugin
# ---------------------------------------------------------------------------

class LiiiftVFClampPlugin(GeneralPlugin):
	"""Glyphs.app GeneralPlugin that adds 'Generate Restricted VFs…' under Script.

	Class name is vendor-namespaced to avoid Obj-C symbol collisions with any
	other plugin shipping a `VFClampPlugin` class.
	"""

	@objc.python_method
	def settings(self):
		"""Declare plugin display name and submenu label."""
		self.name = 'vf-clamp'
		# GeneralPlugin uses self.menuName (NOT self.name) for the submenu label
		self.menuName = 'vf-clamp'

	@objc.python_method
	def start(self):
		"""Register the menu item under Script > vf-clamp."""
		try:
			newMenuItem = NSMenuItem.new()
			newMenuItem.setTitle_('Generate Restricted VFs…')
			# AppKit target/action expects a SEL, not a bound Python method
			newMenuItem.setAction_('showDialog:')
			newMenuItem.setTarget_(self)

			# Glyphs.menu[SCRIPT_MENU] is an NSMenuItem; the actual NSMenu is
			# its submenu(). addItem_ is the only correct way to extend it.
			script_menu = Glyphs.menu[SCRIPT_MENU]
			submenu = script_menu.submenu() if hasattr(script_menu, 'submenu') else None
			if submenu is not None:
				submenu.addItem_(newMenuItem)
			else:
				# Last-resort fallback for unusual Glyphs versions
				try:
					script_menu.append(newMenuItem)
				except Exception:
					pass
		except Exception:
			# Never let registration errors crash plugin loading silently:
			# log to the Macro Panel via stderr so the user can see them.
			traceback.print_exc()

	# Bare ObjC selector — must NOT carry @objc.python_method
	def showDialog_(self, sender):
		"""Open the vf-clamp dialog window."""
		if fonttools_import_error() is not None:
			Message(
				f'vf-clamp requires fontTools, which could not be imported:\n\n{fonttools_import_error()}',
				'vf-clamp — Missing Dependency',
			)
			return
		try:
			check_fonttools_version()
		except RuntimeError as e:
			Message(str(e), 'vf-clamp — Incompatible fontTools')
			return
		# If a prior dialog is still on screen, bring it forward instead of
		# spawning a second one. This avoids the zombie-window pattern where
		# self.dialog gets re-assigned and the previous dialog's worker
		# thread starts firing callbacks at a dropped Python object.
		existing = getattr(self, 'dialog', None)
		if existing is not None and existing._alive():
			existing.show()
			return
		self.dialog = VFClampDialog()
		self.dialog.show()

	@objc.python_method
	def __file__(self):
		"""Return the .glyphsPlugin bundle path (not the inner module path)."""
		# Resources/plugin.py -> Resources/ -> Contents/ -> bundle root
		return os.path.dirname(os.path.dirname(_RESOURCES_DIR))


# Backwards-compat alias so a pre-existing Info.plist NSPrincipalClass entry
# of `VFClampPlugin` still resolves. This alias is kept indefinitely — removing
# it would break previously-installed bundles whose Info.plist still references
# the un-namespaced class name. New bundles should use `LiiiftVFClampPlugin`.
VFClampPlugin = LiiiftVFClampPlugin


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class VFClampDialog:
	"""Dialog for selecting named instances and generating a restricted VF."""

	# Pixel metrics — kept as class attrs so future dialogs cannot shadow them
	W = 540
	PAD = 16
	LABEL_H = 20
	FIELD_H = 22
	BTN_H = 24
	ROW = 28
	CHECK_H = 20
	CHECK_GAP = 4
	# Always reserve scroll-area space for this many visible rows at build time.
	# Setting this once at the layout level (rather than min(n, 8) at populate
	# time) keeps the widgets below the scroll area at stable Y positions —
	# vanilla widgets with positive Y don't auto-reflow, so a growing scroll
	# would otherwise overlap the Hull / Output Name / Format / Folder rows.
	MAX_VISIBLE_INSTANCES = 8
	# Filter-style layout: right-aligned label column + control column
	LABEL_COL_W = 110
	LABEL_GAP = 12
	CONTROL_X = PAD + LABEL_COL_W + LABEL_GAP  # 138
	HULL_H = 60  # space for up to ~3 axis chips
	# Bottom action bar button widths
	GENERATE_W = 100
	CANCEL_W = 80
	REVEAL_W = 70
	ACTION_GAP = 8

	# Source mode constants — mirrors current vs. last interaction.
	SOURCE_FILE = 'file'
	SOURCE_GSFONT = 'gsfont'

	# Format options shown in the Format popup.
	BINARY_FORMATS = ['TTF', 'OTF', 'WOFF', 'WOFF2']
	GSFONT_FORMAT_LABEL = '.glyphs'
	# When source is an open Glyphs font, the popup adds the .glyphs option on top.
	GSFONT_FORMATS = [GSFONT_FORMAT_LABEL] + BINARY_FORMATS

	# Sentinel item shown when there are zero open Glyphs documents.
	_GSFONT_POPUP_EMPTY = '(no open Glyphs fonts)'

	def __init__(self):
		"""Initialise dialog state. Window is shown by show()."""
		self._font_path = None
		# Parsed TTFont cache populated by _load_font and reused by
		# _refresh_axis_preview so checkbox toggles don't re-parse the disk file.
		self._cached_font = None
		self._instance_names = []   # ordered list from fvar OR gsfont
		self._checks = []           # list of CheckBox widgets (parallel to _instance_names)
		self._name_overridden = False
		# Phase 1+2: source can be a file on disk or a currently-open GSFont.
		self._source_mode = self.SOURCE_FILE
		self._gsfont = None         # GSFont when _source_mode == SOURCE_GSFONT
		self._gsfont_options = []   # list of currently-open GSFonts mirroring popup order
		# Cancellation flag — set by _on_cancel, read by worker thread to short-circuit
		# the file-source pipeline and skip writing partial output.
		self._cancelled = False
		self._build_window()
		# Populate the open-Glyphs-font popup once the window exists.
		self._refresh_gsfont_popup()
		# Default to "Open Font" mode when at least one Glyphs document is
		# open; this matches the canonical workflow of "I have my font open,
		# clamp it" without an extra mode-toggle click. Falls back to "File"
		# when no Glyphs documents are open.
		# Initial transition does not clear inactive state — nothing is loaded
		# yet, so there is nothing to clear; passing clear_inactive=False also
		# avoids a spurious gsfontPopup.set(0) before the popup has the right
		# items in place. (Issue #44.)
		if self._gsfont_options:
			self._transition_source_mode(self.SOURCE_GSFONT, clear_inactive=False)
			self._auto_select_frontmost_gsfont()
		else:
			self._transition_source_mode(self.SOURCE_FILE, clear_inactive=False)

	# ------------------------------------------------------------------
	# Window construction
	# ------------------------------------------------------------------

	@objc.python_method
	def _right_label(self, posSize, text):
		"""Build a right-aligned TextBox label that sits in the left column."""
		box = vanilla.TextBox(posSize, text)
		try:
			box._nsObject.cell().setAlignment_(NSTextAlignmentRight)
		except Exception:
			pass
		return box

	def _build_window(self):
		"""Build the dialog layout — Glyphs filter-style with right-aligned labels."""
		w = self.W
		h = self._compute_window_height(0)

		# vanilla.Window picks up the user's macOS appearance (dark mode → dark
		# translucent panel), which matches the native Glyphs filter look.
		self.w = vanilla.Window(
			(w, h),
			'vf-clamp — Generate Restricted VFs',
			minSize=(w, 360),
			maxSize=(w, 1200),
		)
		# Persist window position across launches. setFrameAutosaveName_ is
		# AppKit's documented mechanism for restoring user-positioned windows;
		# vanilla.Window doesn't accept it as a kwarg in older builds, so we
		# apply it to the underlying NSWindow.
		try:
			nswin = self.w.getNSWindow() if hasattr(self.w, 'getNSWindow') else self.w._window
			if nswin is not None:
				nswin.setFrameAutosaveName_('com.liiift.vf-clamp.dialog')
		except (AttributeError, RuntimeError):
			pass
		win = self.w
		PAD = self.PAD
		LABEL_H = self.LABEL_H
		FIELD_H = self.FIELD_H
		BTN_H = self.BTN_H
		ROW = self.ROW
		LABEL_COL_W = self.LABEL_COL_W
		CONTROL_X = self.CONTROL_X
		HULL_H = self.HULL_H

		y = PAD

		# --- Row 1: Source selector (RadioGroup) -----------------------
		win.sourceLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Source:')
		win.sourceRadio = vanilla.RadioGroup(
			(CONTROL_X, y, -PAD, ROW),
			['Open Font', 'File'],
			isVertical=False,
			callback=self._on_source_radio_changed,
		)
		win.sourceRadio.set(1)  # default to File; __init__ flips to Open Font if any are present
		# Accessibility: VoiceOver should announce the source-selector role
		# rather than reading the two radio cells as anonymous toggles.
		try:
			win.sourceRadio._nsObject.setAccessibilityLabel_(
				'Font source — Open Font or File'
			)
		except (AttributeError, RuntimeError):
			pass
		y += ROW + 6

		# --- Row 2: Source-specific input (file row OR gsfont popup, same Y)
		# Both widgets share the row; only the one matching the active mode is shown.
		win.sourceInputLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'File:')

		# File-mode widgets
		win.fontPathField = vanilla.EditText(
			(CONTROL_X, y, -94, FIELD_H),
			placeholder='Select a .ttf or .otf variable font…',
			readOnly=True,
		)
		win.browseButton = vanilla.Button(
			(-90, y - 1, -PAD, BTN_H),
			'Browse…',
			callback=self._on_browse,
		)

		# GSFont-mode widget (occupies the same Y, full control width)
		win.gsfontPopup = vanilla.PopUpButton(
			(CONTROL_X, y, -PAD, FIELD_H + 2),
			[self._GSFONT_POPUP_EMPTY],
			callback=self._on_gsfont_chosen,
		)
		# Accessibility: name file path field and gsfont popup explicitly so
		# VoiceOver reads them in context rather than as anonymous controls.
		for widget, ax_label in (
			(win.fontPathField, 'Selected font file path'),
			(win.browseButton, 'Browse for a variable font file'),
			(win.gsfontPopup, 'Open Glyphs font'),
		):
			try:
				widget._nsObject.setAccessibilityLabel_(ax_label)
			except (AttributeError, RuntimeError):
				pass
		# Initial visibility is set by _set_source_mode_ui after the build.
		y += ROW + 8

		# --- Divider ----------------------------------------------------
		win.divider1 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Row 3: Instances label + bulk select buttons --------------
		win.instanceLabel = self._right_label((PAD, y, LABEL_COL_W, LABEL_H), 'Instances:')
		# Bulk-selection helpers on the right
		win.allBtn = vanilla.Button(
			(-PAD - 180, y - 2, 56, BTN_H), 'All',
			callback=self._on_select_all, sizeStyle='small',
		)
		win.noneBtn = vanilla.Button(
			(-PAD - 122, y - 2, 56, BTN_H), 'None',
			callback=self._on_select_none, sizeStyle='small',
		)
		win.invertBtn = vanilla.Button(
			(-PAD - 64, y - 2, 64, BTN_H), 'Invert',
			callback=self._on_select_invert, sizeStyle='small',
		)
		# Accessibility + tooltips: VoiceOver reads short labels like 'All'
		# as ambiguous without context; sighted users also benefit from a
		# tooltip explaining the abbreviated scope.
		for btn_widget, ax_label in (
			(win.allBtn, 'Select all instances'),
			(win.noneBtn, 'Deselect all instances'),
			(win.invertBtn, 'Invert instance selection'),
		):
			try:
				btn_widget._nsObject.setAccessibilityLabel_(ax_label)
			except (AttributeError, RuntimeError):
				pass
			try:
				btn_widget._nsObject.setToolTip_(ax_label)
			except (AttributeError, RuntimeError):
				pass
		# Bulk-select buttons are meaningless without an instance list — keep
		# them hidden until _populate_instance_checks runs. Avoids dead
		# affordances that surface a stale "click All" before any font is
		# loaded.
		for btn_widget in (win.allBtn, win.noneBtn, win.invertBtn):
			try:
				btn_widget.show(False)
			except (AttributeError, RuntimeError):
				pass
		y += LABEL_H + 6

		# --- Row 4: Instance scroll area (in the control column) -------
		# Reserve MAX_VISIBLE_INSTANCES rows of vertical space up front so the
		# Hull/Output/Format/Folder rows below sit at fixed Y positions and
		# the scroll widget never has to resize at populate time.
		reserved_scroll_h = (
			self.MAX_VISIBLE_INSTANCES * (self.CHECK_H + self.CHECK_GAP) + 8
		)
		win.instancePlaceholder = vanilla.TextBox(
			(CONTROL_X, y, -PAD, LABEL_H),
			'Open a variable font to see its named instances.',
			sizeStyle='small',
		)
		self._scroll_top_y = y
		self._scroll_height = reserved_scroll_h
		# Seed the ScrollView with an empty placeholder Group; the document
		# view is replaced by _populate_instance_checks when a font loads.
		self._scroll_placeholder_group = vanilla.Group((0, 0, 1, 1))
		win.instanceScroll = vanilla.ScrollView(
			(CONTROL_X, y, -PAD, reserved_scroll_h),
			self._scroll_placeholder_group._nsObject,
			hasHorizontalScroller=False,
			hasVerticalScroller=True,
			autohidesScrollers=True,
		)
		win.instanceScroll.show(False)
		# Accessibility: name the scroll region so VoiceOver announces
		# "Named instances, scroll area" instead of an anonymous scroll view.
		try:
			win.instanceScroll._nsObject.setAccessibilityLabel_(
				'Named instances'
			)
		except (AttributeError, RuntimeError):
			pass
		y += reserved_scroll_h + 8

		# --- Row 5: Hull preview (colored axis chips) ------------------
		win.hullLabel = self._right_label((PAD, y + 2, LABEL_COL_W, LABEL_H), 'Hull:')
		win.axisPreview = vanilla.TextBox(
			(CONTROL_X, y, -PAD, HULL_H),
			'(select instances to preview)',
			sizeStyle='small',
			selectable=True,
		)
		# vanilla.TextBox wraps NSTextField in single-line mode by default;
		# the hull preview puts one axis per line so we need to turn wrapping
		# back on or the '\n' chars render as glyph-not-found boxes. The
		# attribute access is wrapped because older PyObjC builds may not
		# expose every setter; failure is non-fatal (falls back to plain text).
		try:
			cell = win.axisPreview._nsObject.cell()
			cell.setUsesSingleLineMode_(False)
			cell.setWraps_(True)
		except (AttributeError, RuntimeError):
			pass
		y += HULL_H + 8

		# --- Divider ----------------------------------------------------
		win.divider2 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Row 6: Output Name ----------------------------------------
		win.nameLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Output Name:')
		win.nameField = vanilla.EditText(
			(CONTROL_X, y, -PAD, FIELD_H),
			placeholder='e.g. MyFont Light-Bold',
			callback=self._on_name_edited,
		)
		try:
			win.nameField._nsObject.setAccessibilityLabel_(
				'Output family name'
			)
		except (AttributeError, RuntimeError):
			pass
		y += ROW + 4

		# --- Row 7: Format ---------------------------------------------
		win.formatLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Format:')
		win.formatPopup = vanilla.PopUpButton(
			(CONTROL_X, y, 160, FIELD_H + 2),
			list(self.BINARY_FORMATS),
		)
		try:
			win.formatPopup._nsObject.setAccessibilityLabel_(
				'Output font format'
			)
		except (AttributeError, RuntimeError):
			pass
		y += ROW + 4

		# --- Row 8: Output Folder --------------------------------------
		win.folderLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Folder:')
		win.folderField = vanilla.EditText(
			(CONTROL_X, y, -94, FIELD_H),
			placeholder='Default: same folder as source',
			readOnly=True,
		)
		win.folderButton = vanilla.Button(
			(-90, y - 1, -PAD, BTN_H),
			'Choose…',
			callback=self._on_choose_folder,
		)
		try:
			win.folderField._nsObject.setAccessibilityLabel_(
				'Output folder path'
			)
		except (AttributeError, RuntimeError):
			pass
		y += ROW + 8

		# --- Divider ----------------------------------------------------
		win.divider3 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Bottom action bar -----------------------------------------
		# Layout: [spinner] [statusLabel] ... [Reveal] [Cancel] [Generate]
		GEN_W = self.GENERATE_W
		CAN_W = self.CANCEL_W
		REV_W = self.REVEAL_W
		GAP = self.ACTION_GAP

		win.generateButton = vanilla.Button(
			(-PAD - GEN_W, y, GEN_W, BTN_H),
			'Generate',
			callback=self._on_generate,
		)
		win.generateButton.enable(False)
		# Return key → default action; on macOS this also paints the button
		# with the system accent colour, giving us the "primary blue" look.
		try:
			win.generateButton._nsObject.setKeyEquivalent_('\r')
		except Exception:
			pass
		# Accessibility: VoiceOver should announce the verbose action label
		# for the primary button (Return-key equivalent is the default).
		try:
			win.generateButton._nsObject.setAccessibilityLabel_(
				'Generate the restricted variable font'
			)
		except (AttributeError, RuntimeError):
			pass

		win.cancelButton = vanilla.Button(
			(-PAD - GEN_W - GAP - CAN_W, y, CAN_W, BTN_H),
			'Cancel',
			callback=self._on_cancel,
		)
		# Escape closes the dialog.
		try:
			win.cancelButton._nsObject.setKeyEquivalent_('\x1b')
		except Exception:
			pass
		try:
			win.cancelButton._nsObject.setAccessibilityLabel_(
				'Cancel and close the dialog'
			)
		except (AttributeError, RuntimeError):
			pass

		win.revealButton = vanilla.Button(
			(-PAD - GEN_W - GAP - CAN_W - GAP - REV_W, y, REV_W, BTN_H),
			'Reveal',
			callback=self._on_reveal,
			sizeStyle='small',
		)
		win.revealButton.enable(False)
		win.revealButton.show(False)  # appears only after a successful generate
		# Tooltip + accessibility label so 'Reveal' isn't ambiguous in
		# isolation (reveal what, where?).
		for widget, ax_label in (
			(win.revealButton, 'Reveal the generated font in Finder'),
			(win.folderButton, 'Choose the output folder'),
		):
			try:
				widget._nsObject.setAccessibilityLabel_(ax_label)
			except (AttributeError, RuntimeError):
				pass
			try:
				widget._nsObject.setToolTip_(ax_label)
			except (AttributeError, RuntimeError):
				pass
		self._last_output_path = None

		# Spinner + status on the left
		win.spinner = vanilla.ProgressSpinner((PAD, y + 4, 16, 16), displayWhenStopped=False)
		try:
			win.spinner.stop()
		except Exception:
			pass
		status_right_offset = PAD + GEN_W + GAP + CAN_W + GAP + REV_W + GAP
		win.statusLabel = vanilla.TextBox(
			(PAD + 22, y + 5, -status_right_offset, LABEL_H),
			'',
			sizeStyle='small',
			selectable=True,
		)
		# Accessibility: spinner + status text combine into an aria-live-like
		# announcement region. VoiceOver should treat status as a live region
		# so Generating… / Saved: / Error: messages are spoken automatically.
		try:
			win.statusLabel._nsObject.setAccessibilityLabel_('Status')
		except (AttributeError, RuntimeError):
			pass
		try:
			win.spinner._nsObject.setAccessibilityLabel_(
				'Working — generating font'
			)
		except (AttributeError, RuntimeError):
			pass
		y += BTN_H + PAD
		self._static_sections_height = y

	def _compute_window_height(self, n_instances):
		"""Return total window height — always reserves space for MAX_VISIBLE_INSTANCES rows.

		``n_instances`` is accepted for backward compatibility but no longer
		affects the result. We reserve the maximum scroll area upfront so the
		widgets below it (Hull / Output Name / Format / Folder / action bar)
		stay at fixed Y positions regardless of font instance count. Trade-off:
		a font with one or two instances renders some empty scroll space, but
		nothing overlaps and the window doesn't jump on font load.
		"""
		PAD = self.PAD
		LABEL_H = self.LABEL_H
		ROW = self.ROW
		BTN_H = self.BTN_H
		CHECK_H = self.CHECK_H
		CHECK_GAP = self.CHECK_GAP
		HULL_H = self.HULL_H

		# Reserve max scroll height once at the layout level.
		scroll_h = self.MAX_VISIBLE_INSTANCES * (CHECK_H + CHECK_GAP) + 8

		# Mirrors every `y +=` in _build_window so changes stay in sync.
		return (
			PAD +
			ROW + 6 +           # source radio
			ROW + 8 +           # source-input row
			12 +                # divider 1
			LABEL_H + 6 +       # instances label
			scroll_h + 8 +      # instances scroll
			HULL_H + 8 +        # hull preview
			12 +                # divider 2
			ROW + 4 +           # output name
			ROW + 4 +           # format
			ROW + 8 +           # output folder
			12 +                # divider 3
			BTN_H + PAD         # action bar
		)

	# ------------------------------------------------------------------
	# Event handlers
	# ------------------------------------------------------------------

	def _on_browse(self, sender):
		"""Open a file-picker for the user to select a variable font."""
		panel = NSOpenPanel.openPanel()
		panel.setCanChooseFiles_(True)
		panel.setCanChooseDirectories_(False)
		panel.setAllowsMultipleSelection_(False)
		# Modern API: setAllowedContentTypes_ via UTType where available.
		applied_modern = False
		try:
			from UniformTypeIdentifiers import UTType
			types = []
			for ext in ('ttf', 'otf', 'woff', 'woff2'):
				ut = UTType.typeWithFilenameExtension_(ext)
				if ut is not None:
					types.append(ut)
			if types:
				panel.setAllowedContentTypes_(types)
				applied_modern = True
		except Exception:
			pass
		if not applied_modern:
			# Fallback for older macOS — extensions are matched case-insensitively
			panel.setAllowedFileTypes_(['ttf', 'otf', 'woff', 'woff2'])
		panel.setTitle_('Select a Variable Font')
		panel.setPrompt_('Select')
		result = panel.runModal()
		if result == NSModalResponseOK:
			path = panel.URL().path()
			self._load_font(path)

	# ------------------------------------------------------------------
	# Open-Glyphs-font source path (Phase 1+2)
	# ------------------------------------------------------------------

	@objc.python_method
	def _refresh_gsfont_popup(self) -> None:
		"""Repopulate the open-font popup from the current Glyphs.fonts list."""
		fonts = list_open_glyphs_fonts() if is_glyphs_app_available() else []
		self._gsfont_options = fonts
		if not fonts:
			items = [self._GSFONT_POPUP_EMPTY]
		else:
			# Always include a leading sentinel so opening the popup is the
			# explicit action that switches source mode — picking the first
			# real entry shouldn't fire on mere refresh.
			items = [self._GSFONT_POPUP_EMPTY] + [gsfont_label(f) for f in fonts]
		try:
			self.w.gsfontPopup.setItems(items)
			self.w.gsfontPopup.set(0)
		except Exception:
			pass

	def _on_gsfont_chosen(self, sender: Any) -> None:
		"""Handle a user selection from the open-Glyphs-font popup."""
		idx = self.w.gsfontPopup.get()
		# Index 0 is the sentinel "(no open Glyphs fonts)" / "—" entry.
		if idx <= 0 or idx - 1 >= len(self._gsfont_options):
			return
		gsfont = self._gsfont_options[idx - 1]
		self._load_gsfont(gsfont)

	@objc.python_method
	def _set_source_mode_ui(self, mode: str) -> None:
		"""Show the file row OR the gsfont popup, depending on ``mode``.

		Both widget groups occupy the same Y position; only one is visible.
		Also updates the leading "Source-input" label and refreshes the
		format popup so .glyphs appears/disappears appropriately.

		AppKit attribute errors are tolerated here so a partially-built window
		(e.g. during construction before _build_window finishes) cannot crash
		mode transitions.

		This method is the *visibility-only* leaf. For a full source-mode
		transition (clearing the inactive source's path/gsfont, resetting
		the default output folder, etc.) call ``_transition_source_mode``
		instead — it routes back through here after handling cross-source
		state. See issue #44.
		"""
		is_file = (mode == self.SOURCE_FILE)
		try:
			self.w.fontPathField.show(is_file)
			self.w.browseButton.show(is_file)
			self.w.gsfontPopup.show(not is_file)
			self.w.sourceInputLabel.set('File:' if is_file else 'Open Font:')
			# Reflect mode on the radio without firing the callback.
			self.w.sourceRadio.set(1 if is_file else 0)
		except (AttributeError, RuntimeError):
			pass
		self._source_mode = mode
		self._refresh_format_popup()

	@objc.python_method
	def _transition_source_mode(self, mode: str, *, clear_inactive: bool = True, reset_folder: bool = False) -> None:
		"""Switch ``_source_mode`` to ``mode`` and clear stale cross-source state.

		Centralised transition (issue #44) — every place that needs to change
		the active source funnels through here so checkbox state, file paths,
		gsfont references, hull preview, and the default output folder cannot
		drift out of sync with the visible widget set.

		Arguments:

		``clear_inactive`` (default True)
			Wipe the *other* source's pointer (``_gsfont`` when switching to
			file mode, ``_font_path`` when switching to gsfont mode) so a
			subsequent generate call cannot accidentally use a stale source
			the user has visually navigated away from. Set False when the
			caller is itself about to populate the inactive source (e.g. the
			__init__ default).

		``reset_folder`` (default False)
			Blank the output-folder field so the next load picks a default
			rooted in the *new* source's folder rather than carrying the
			previous source's path. Callers that want to preserve a user's
			explicit folder pick set this False.
		"""
		# Always flip the visibility + format popup first so a downstream
		# refresh sees the correct widget set.
		self._set_source_mode_ui(mode)

		if clear_inactive:
			if mode == self.SOURCE_FILE:
				# Switching INTO file mode → drop the gsfont reference. The
				# gsfont popup is also reset to its sentinel so the visible
				# selection matches the cleared state.
				self._gsfont = None
				try:
					self.w.gsfontPopup.set(0)
				except (AttributeError, RuntimeError):
					pass
			else:
				# Switching INTO gsfont mode → drop the file-path reference
				# and blank the field so the user sees the source has changed.
				self._font_path = None
				# Release the cached TTFont from the previous file-mode session
				# — keeping it would leak the parsed tables and pin the disk
				# file open until the dialog is closed.
				try:
					self._close_cached_font()
				except AttributeError:
					pass
				try:
					self.w.fontPathField.set('')
				except (AttributeError, RuntimeError):
					pass

		if reset_folder:
			try:
				self.w.folderField.set('')
			except (AttributeError, RuntimeError):
				pass

	def _on_source_radio_changed(self, sender):
		"""User toggled the Source: Open Font / File radio."""
		idx = self.w.sourceRadio.get()
		if idx == 0:  # Open Font
			# Refresh the popup in case the user opened/closed Glyphs documents
			# between dialog launch and now.
			self._refresh_gsfont_popup()
			if not self._gsfont_options:
				self._set_status(
					'No Glyphs fonts are currently open. Switch back to File or open one.',
				)
				# Even with no candidates we still transition so the UI matches
				# the radio click. The transition clears any stale file path so
				# the user's next move starts from a clean slate.
				self._transition_source_mode(self.SOURCE_GSFONT)
				return
			self._transition_source_mode(self.SOURCE_GSFONT)
			# Auto-select the most likely target so the user doesn't have to
			# poke the popup as a second action.
			self._auto_select_frontmost_gsfont()
		else:  # File
			self._transition_source_mode(self.SOURCE_FILE)
			self._set_status('')

	def _on_cancel(self, sender):
		"""Signal cancellation to any in-flight worker, then close the dialog."""
		# Set the cancellation flag first so the worker thread can short-circuit
		# before we tear down the window. The worker itself is daemonized so it
		# won't outlive the process, but we don't want it to write a partial
		# output file after the user has clicked Cancel.
		self._cancelled = True
		try:
			self.w.close()
		except (AttributeError, RuntimeError):
			pass

	@objc.python_method
	def _auto_select_frontmost_gsfont(self) -> None:
		"""Default to the frontmost open Glyphs font when the dialog opens.

		When a font is open in Glyphs, the canonical entry point is "clamp this
		open font as a new .glyphs file" — so we select it automatically and
		default the Format popup to .glyphs. The user can still pick another
		open font from the popup or click Browse to switch to a disk-file source.

		Uses object identity (``is``) rather than ``in`` for the frontmost
		match because GSFont's ``__eq__`` falls back to NSObject pointer
		comparison; PyObjC can reissue wrappers for the same underlying Obj-C
		object, so an ``in`` check may falsely report "not found" and silently
		pick the wrong default. Falls back to the first listed font when there
		is no identity match.
		"""
		if not self._gsfont_options:
			return
		frontmost = None
		try:
			frontmost = Glyphs.font
		except (AttributeError, RuntimeError):
			frontmost = None
		target = self._gsfont_options[0]
		if frontmost is not None:
			for candidate in self._gsfont_options:
				if candidate is frontmost:
					target = candidate
					break
		try:
			# +1 for the sentinel row at popup index 0
			idx = self._gsfont_options.index(target) + 1
			self.w.gsfontPopup.set(idx)
		except (ValueError, AttributeError, RuntimeError):
			pass
		self._load_gsfont(target)

	@objc.python_method
	def _refresh_format_popup(self) -> None:
		"""Set the Format popup items + canonical default for the active source mode.

		Always resets to the per-mode default. The previous "preserve user's
		selection" logic surfaced a real bug: switching from file mode (default
		TTF) into Open-Font mode kept TTF selected because TTF appears in both
		format sets, so users never saw `.glyphs` as the headline option. Reset-
		on-mode-change is the obvious UX and matches the mode-radio click pattern.
		"""
		if self._source_mode == self.SOURCE_GSFONT:
			items = list(self.GSFONT_FORMATS)
			default_idx = 0  # .glyphs is the headline output for an open font
		else:
			items = list(self.BINARY_FORMATS)
			default_idx = 0  # TTF
		try:
			self.w.formatPopup.setItems(items)
			self.w.formatPopup.set(default_idx)
		except Exception:
			pass

	@objc.python_method
	def _load_gsfont(self, gsfont: Any) -> None:
		"""Switch the dialog to GSFont source mode and populate instance checkboxes."""
		self._set_status('Loading open Glyphs font…')
		try:
			names = gsfont_instance_names(gsfont)
		except Exception as e:
			traceback.print_exc()
			self._set_status(f'Could not read open font: {e}', error=True)
			return
		if not names:
			self._set_status(
				'That Glyphs font has no exportable named instances.',
				error=True,
			)
			return

		# Route through the centralised transition (issue #44) so the file-side
		# source is cleared and the visible widget set switches in one step.
		# We then assign _gsfont AFTER the transition because _transition_source_mode
		# clears the inactive source's pointer; setting _gsfont last anchors the
		# just-loaded reference. _font_path is cleared by the transition so a
		# stale file path can't survive into the next generate dispatch.
		self._transition_source_mode(self.SOURCE_GSFONT)
		self._gsfont = gsfont
		self._instance_names = names
		self._populate_instance_checks(names)
		self._name_overridden = False
		self._refresh_name()
		self._refresh_axis_preview()
		self._refresh_generate_button()
		self._set_status('')

	def _on_choose_folder(self, sender):
		"""Open a panel for the user to select an output folder."""
		panel = NSOpenPanel.openPanel()
		panel.setCanChooseFiles_(False)
		panel.setCanChooseDirectories_(True)
		panel.setAllowsMultipleSelection_(False)
		panel.setTitle_('Select Output Folder')
		panel.setPrompt_('Choose')
		result = panel.runModal()
		if result == NSModalResponseOK:
			self.w.folderField.set(panel.URL().path())

	def _on_name_edited(self, sender):
		"""Once the user touches the name field, stop auto-filling for the session."""
		# First non-trivial edit "claims" the field; subsequent clears do not flip back
		if self.w.nameField.get().strip():
			self._name_overridden = True

	def _on_check_toggled(self, sender):
		"""Update output name, axis preview, and Generate button state on toggle.

		Walks ``self._checks`` ONCE per toggle and threads the resulting selection
		list through every downstream refresh — without this, a single click
		incurs four O(N) bridge walks through the checkbox list (one per
		refresh helper) plus a font re-parse in the axis preview.
		"""
		selected = self._selected_instance_names()
		self._refresh_name(selected=selected)
		self._refresh_axis_preview(selected=selected)
		self._refresh_generate_button(selected=selected)
		self._refresh_selection_count(selected=selected)

	@objc.python_method
	def _refresh_selection_count(self, selected=None):
		"""Update the Instances label with an 'N of M' count of selected rows.

		Keeps the label terse — 'Instances (3/8):' rather than a separate
		status line — so the bottom status label remains free for transient
		messages (Generating…, Saved:…, Error:…). Callers that have already
		computed the selection list can pass it via ``selected`` to avoid a
		second walk through the checkbox list.
		"""
		if not self._instance_names:
			# Reset to bare label when there are no instances yet
			try:
				self.w.instanceLabel.set('Instances:')
			except (AttributeError, RuntimeError):
				pass
			return
		if selected is None:
			selected = self._selected_instance_names()
		count = len(selected)
		total = len(self._instance_names)
		try:
			self.w.instanceLabel.set(f'Instances ({count}/{total}):')
		except (AttributeError, RuntimeError):
			pass

	def _on_select_all(self, sender):
		"""Tick every named-instance checkbox."""
		self._set_all_checks(True)

	def _on_select_none(self, sender):
		"""Untick every named-instance checkbox."""
		self._set_all_checks(False)

	def _on_select_invert(self, sender):
		"""Invert every named-instance checkbox."""
		for cb in self._iter_checks():
			cb.set(not cb.get())
		self._on_check_toggled(sender)

	def _on_reveal(self, sender):
		"""Reveal the most recently saved font in Finder."""
		if not self._last_output_path:
			return
		try:
			NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(
				self._last_output_path, ''
			)
		except Exception:
			pass

	@objc.python_method
	def _resolve_selected_format(self):
		"""Read the Format popup and normalise the label to an internal token.

		Returns one of the internal format tokens ('TTF', 'OTF', 'WOFF',
		'WOFF2', 'GLYPHS'). The popup displays '.glyphs' for source output;
		this helper maps the user-facing label to the internal 'GLYPHS' token
		so downstream code only ever sees uppercase tokens.
		"""
		try:
			fmt_items = self.w.formatPopup.getItems()
			fmt_index = self.w.formatPopup.get()
		except (AttributeError, RuntimeError):
			return 'TTF'
		fmt_label = fmt_items[fmt_index] if fmt_items else 'TTF'
		return 'GLYPHS' if fmt_label == self.GSFONT_FORMAT_LABEL else fmt_label

	@objc.python_method
	def _validate_brotli_for_format(self, fmt):
		"""Return an error message for WOFF2 output without brotli, else None.

		Extracted (issue #59) so the WOFF2/brotli check stops entangling the
		generate dispatcher with backend capability detection. Callers ask
		"is this format viable right now?" and decide what to do with the
		answer (status message, popup change, etc.).
		"""
		if flavor_for_format(fmt) != 'woff2':
			return None
		try:
			import brotli  # noqa: F401
		except ImportError:
			return (
				'WOFF2 output requires the brotli package; install it or '
				'pick WOFF/TTF/OTF.'
			)
		return None

	@objc.python_method
	def _collect_generate_inputs(self):
		"""Read every dialog input needed by generate; return params or an error.

		Returns a tuple ``(params, error_message)`` where exactly one element
		is non-None. ``params`` is a dict with keys ``selected``, ``family_name``,
		``fmt``, ``output_path`` ready to hand to a generator. ``error_message``
		is a single string the caller surfaces verbatim via _set_status.

		Extracted (issue #59) so _on_generate stops being a god-method that
		also owns validation, normalisation, brotli probing, and folder
		fallback logic.
		"""
		selected = self._selected_instance_names()
		if not selected:
			return None, 'Select at least one named instance.'

		family_name = self.w.nameField.get().strip()
		if not family_name:
			return None, 'Enter an output name.'

		fmt = self._resolve_selected_format()
		ext = extension_for_format(fmt)

		# Cross-source sanity check: .glyphs output requires an open-font source.
		if fmt == 'GLYPHS' and self._source_mode != self.SOURCE_GSFONT:
			return None, '.glyphs output requires using an open Glyphs font as the source.'

		brotli_err = self._validate_brotli_for_format(fmt)
		if brotli_err is not None:
			return None, brotli_err

		folder = self.w.folderField.get().strip()
		if not folder:
			folder = self._default_output_folder()
		output_path = safe_output_path(folder, family_name, ext)

		return {
			'selected': selected,
			'family_name': family_name,
			'fmt': fmt,
			'output_path': output_path,
		}, None

	def _on_generate(self, sender):
		"""Validate dialog input, then dispatch to the right source-path generator.

		Reduced (issue #59) to two responsibilities: collect-or-error, and
		dispatch. Each previously-inlined concern (selection check, name
		check, format normalisation, brotli probe, folder fallback,
		output-path safety) lives in its own helper above.
		"""
		params, err = self._collect_generate_inputs()
		if err is not None:
			self._set_status(err, error=True)
			return

		# Dispatch to the appropriate generator. Both paths share the same
		# UI lifecycle via _begin_generate_ui (issue #58).
		if self._source_mode == self.SOURCE_GSFONT:
			self._generate_from_gsfont(
				params['selected'], params['family_name'], params['fmt'], params['output_path'],
			)
		else:
			self._generate_from_file(
				params['selected'], params['family_name'], params['fmt'], params['output_path'],
			)

	@objc.python_method
	def _default_output_folder(self) -> str:
		"""Return a sensible default output folder when the user has not chosen one.

		Uses ``pathlib.Path`` for the gsfont- and file-source folder derivations
		(issue #79 partial migration). The return type is still ``str`` because
		downstream consumers (vanilla EditText, safe_output_path) all expect a
		plain string path.
		"""
		if self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None:
			try:
				fp = self._gsfont.filepath
			except Exception:
				fp = None
			if fp:
				return str(Path(fp).parent)
		if self._font_path:
			return str(Path(self._font_path).parent)
		return str(Path.home() / 'Desktop')

	@objc.python_method
	def _begin_generate_ui(self):
		"""Spin up the shared 'Generate is in flight' UI state.

		Extracted (issue #58) so the two source-path dispatchers don't
		each copy the same five-line spinner/Generate/Reveal/disable block.
		The two paths still use different concurrency models — the file path
		runs in a worker thread, the gsfont path stays on the main thread
		because Glyphs APIs require it — but the *UI* lifecycle is identical
		and now lives in one place.

		Resets the cancellation flag too so a prior Cancel click doesn't
		short-circuit this generate.
		"""
		self._cancelled = False
		self._set_status('Generating…')
		try:
			self.w.generateButton.enable(False)
			self.w.revealButton.enable(False)
		except (AttributeError, RuntimeError):
			pass
		try:
			self.w.spinner.start()
		except (AttributeError, RuntimeError):
			pass

	@objc.python_method
	def _generate_from_file(self, selected, family_name, fmt, output_path):
		"""File-source path — uses fontTools instancer in a worker thread.

		Concurrency note (issue #58): we deliberately run fontTools in a
		background thread here. The fontTools instancer is pure-Python /
		fontTools and does not require the main runloop, so blocking the
		dialog would be a UX regression. The companion gsfont path stays on
		the main thread because Glyphs' Python API is main-thread-only.
		Both paths share the same _begin_generate_ui / _on_generate_success
		/ _on_generate_failure UI lifecycle.
		"""
		font_path = self._font_path
		self._begin_generate_ui()
		dialog_ref = self

		def _run():
			try:
				produce_restricted_vf(font_path, selected, family_name, output_path, fmt=fmt)
			except (ValueError, RuntimeError, OSError) as e:
				if dialog_ref._cancelled:
					return
				AppHelper.callAfter(dialog_ref._on_generate_failure, str(e))
			except Exception as e:
				traceback.print_exc()
				if dialog_ref._cancelled:
					return
				AppHelper.callAfter(dialog_ref._on_generate_failure, str(e))
			else:
				if dialog_ref._cancelled:
					# Best-effort cleanup of a partial output file the user
					# no longer wants. Quiet failure is intentional — the user
					# already saw the Cancel they asked for.
					try:
						out_path_obj = Path(output_path)
						if out_path_obj.exists():
							out_path_obj.unlink()
					except OSError:
						pass
					return
				AppHelper.callAfter(dialog_ref._on_generate_success, output_path)

		threading.Thread(target=_run, daemon=True).start()

	@objc.python_method
	def _generate_from_gsfont(self, selected: List[str], family_name: str, fmt: str, output_path: str) -> None:
		"""Open-font-source path — runs on the main thread (Glyphs APIs require it).

		For ``.glyphs`` output we just clamp + save. For binary outputs we route
		through Glyphs' native export pipeline (Variable Font Setting + generate).

		Concurrency note (issue #58): we deliberately stay on the main thread
		here. Glyphs' Python scripting API is not thread-safe — calling
		``Glyphs.open`` or ``GSInstance.generate`` from a background thread
		races with NSDocument internals and crashes the app. We defer the
		heavy work to the next runloop tick via ``AppHelper.callAfter`` so
		the spinner and status label paint before the main thread blocks.

		The companion file path runs in a worker thread because fontTools is
		main-thread-agnostic. Both paths share the same _begin_generate_ui /
		_on_generate_success / _on_generate_failure UI lifecycle.
		"""
		gsfont = self._gsfont
		if gsfont is None:
			self._set_status('No open Glyphs font selected.', error=True)
			return

		self._begin_generate_ui()

		# Defer the long Glyphs.open / generate sequence so the spinner and
		# status label paint before the main thread blocks. Without this,
		# the user sees a hung dialog with no feedback for several seconds.
		AppHelper.callAfter(
			self._run_gsfont_generate, gsfont, selected, family_name, fmt, output_path
		)

	@objc.python_method
	def _run_gsfont_generate(self, gsfont: Any, selected: List[str], family_name: str, fmt: str, output_path: str) -> None:
		"""Actually do the GSFont clamp + save/export. Deferred onto the runloop."""
		if self._cancelled or not self._alive():
			return
		try:
			clamped = clamp_gsfont(gsfont, selected, family_name)
			if fmt == 'GLYPHS':
				save_gsfont_to_glyphs(clamped, output_path)
			else:
				export_gsfont_binary_via_glyphs(clamped, output_path, fmt)
		except (ValueError, RuntimeError, OSError) as e:
			self._on_generate_failure(str(e))
			return
		except Exception as e:
			traceback.print_exc()
			self._on_generate_failure(str(e))
			return

		if self._cancelled:
			return
		self._on_generate_success(output_path)

	# ------------------------------------------------------------------
	# Worker-thread callbacks (always invoked on main thread via AppHelper)
	# ------------------------------------------------------------------

	@objc.python_method
	def _on_generate_success(self, path):
		"""Main-thread handler invoked after a successful generate."""
		# Bail if the dialog window was already closed
		if not self._alive():
			return
		self._last_output_path = path
		self.w.revealButton.enable(True)
		try:
			self.w.revealButton.show(True)
		except Exception:
			pass
		# Show a path scrubbed to ~ for readability and minor info-leak hygiene
		display = path.replace(os.path.expanduser('~'), '~', 1)
		self._set_status(f'Saved: {display}')
		self.w.generateButton.enable(True)
		try:
			self.w.spinner.stop()
		except Exception:
			pass

	@objc.python_method
	def _on_generate_failure(self, message):
		"""Main-thread handler invoked after a generate failure."""
		if not self._alive():
			return
		# Scrub the user's home dir and the temp-dir prefix so we don't leak
		# absolute paths into the dialog or Macro Panel log.
		scrubbed = message.replace(os.path.expanduser('~'), '~')
		scrubbed = scrubbed.replace('/var/folders/', '/<tmp>/').replace('/private/var/folders/', '/<tmp>/')
		# _set_status(error=True) already prepends "Error:" — don't double it.
		self._set_status(scrubbed, error=True)
		self.w.generateButton.enable(True)
		# Hide the Reveal button after a failure so it can't surface a stale
		# prior output that no longer corresponds to the current selection.
		try:
			self.w.revealButton.enable(False)
			self.w.revealButton.show(False)
		except (AttributeError, RuntimeError):
			pass
		try:
			self.w.spinner.stop()
		except (AttributeError, RuntimeError):
			pass

	def _alive(self):
		"""Return True if the dialog's underlying NSWindow is still around.

		Uses vanilla's public ``getNSWindow()`` accessor where available and
		falls back to ``_window`` only if the public accessor is missing.
		"""
		try:
			win = self.w.getNSWindow() if hasattr(self.w, 'getNSWindow') else self.w._window
			return win is not None and bool(win.isVisible())
		except (AttributeError, RuntimeError):
			return False

	# ------------------------------------------------------------------
	# Font loading
	# ------------------------------------------------------------------

	@objc.python_method
	def _load_font(self, path):
		"""Parse the font at path, populate the checkbox list, and refresh UI."""
		self._set_status('Loading…')
		try:
			names = get_instance_names(path)
		except ValueError as e:
			self._set_status(str(e), error=True)
			return
		except OSError as e:
			self._set_status(f'Error loading font: {e}', error=True)
			return
		except RuntimeError as e:
			self._set_status(str(e), error=True)
			return
		except Exception as e:
			traceback.print_exc()
			self._set_status(f'Unexpected error: {e}', error=True)
			return

		# Route through the centralised transition (issue #44). It clears the
		# gsfont reference, resets the gsfont popup to its sentinel, and flips
		# widget visibility — replacing the ad-hoc setattr+setter pair this
		# function used to do inline.
		self._transition_source_mode(self.SOURCE_FILE)
		self._font_path = path
		self._instance_names = names
		# Cache the parsed TTFont so subsequent checkbox toggles can read fvar
		# axis hulls without re-opening the disk file and re-parsing fontTools
		# tables. The cache is keyed by path; switching to a different font
		# clears it via _close_cached_font.
		self._close_cached_font()
		try:
			self._cached_font = open_font_safely(path)
		except Exception:
			# Fall through silently: _refresh_axis_preview re-opens on demand
			# when the cache is None.
			self._cached_font = None
		self.w.fontPathField.set(path)
		self._set_status('')
		self._populate_instance_checks(names)

		if not self.w.folderField.get().strip():
			# pathlib for the file-source folder default (issue #79 partial).
			self.w.folderField.set(str(Path(path).parent))

		self._name_overridden = False
		self._refresh_name()
		self._refresh_axis_preview()
		self._refresh_generate_button()

	@objc.python_method
	def _close_cached_font(self):
		"""Release the cached TTFont so the next ``_load_font`` reads fresh.

		Best-effort — fontTools' TTFont does not require explicit close, but
		dropping the reference lets the garbage collector reclaim the parsed
		tables immediately when the user switches fonts.
		"""
		cached = getattr(self, '_cached_font', None)
		if cached is None:
			return
		try:
			close = getattr(cached, 'close', None)
			if callable(close):
				close()
		except (AttributeError, OSError, RuntimeError):
			pass
		self._cached_font = None

	@objc.python_method
	def _populate_instance_checks(self, names):
		"""Build one CheckBox per named instance inside a scrollable Group.

		Holds the CheckBox widgets in ``self._checks`` (a flat list parallel to
		``self._instance_names``). Vanilla still requires the widgets to be
		attached to their parent Group as named attributes so that AppKit retains
		them and the document view's autorelease pool sees them, but the names
		are an internal implementation detail — public iteration goes through
		the list, never ``getattr``.
		"""
		self.w.instancePlaceholder.show(False)
		# Surface the bulk-select helpers now that we have something to select.
		for btn_widget in (self.w.allBtn, self.w.noneBtn, self.w.invertBtn):
			try:
				btn_widget.show(True)
			except (AttributeError, RuntimeError):
				pass

		n = len(names)
		check_row = self.CHECK_H + self.CHECK_GAP
		inner_h = n * check_row + self.CHECK_GAP
		visible_rows = max(1, min(n, 8))
		scroll_h = visible_rows * check_row + self.CHECK_GAP

		# Scroll widget was built at its maximum size in _build_window — no
		# resize needed here. This is the key change that prevents the scroll
		# area from growing downward and overlapping the rows below it.

		# Compute the document view width from the now-correct clip view size,
		# minus a scrollbar reserve so content + scrollbar fit together.
		# Window width 540 - CONTROL_X (138) - PAD (16) = 386 visible clip width.
		# Subtract 18 px scrollbar reserve = 368 px usable.
		group_width = self.W - self.CONTROL_X - self.PAD - 18
		inner_group = vanilla.Group((0, 0, group_width, inner_h))
		self._checks = []
		for idx, name in enumerate(names):
			y = self.CHECK_GAP + idx * check_row
			cb = vanilla.CheckBox(
				(8, y, group_width - 16, self.CHECK_H),
				name,
				value=False,
				callback=self._on_check_toggled,
			)
			# Attach to the parent group so AppKit retains the widget. The
			# attribute name is local to this build only — never read elsewhere.
			setattr(inner_group, f'_cb_{idx}', cb)
			self._checks.append(cb)

		# Accessibility: VoiceOver reads checkbox labels but the cell is wrapped
		# in a custom NSScrollView document view, so the row group itself benefits
		# from an explicit role description.
		for cb, name in zip(self._checks, names):
			try:
				cb._nsObject.setAccessibilityLabel_(
					f'Include {name} instance'
				)
			except (AttributeError, RuntimeError):
				pass

		self._inner_group = inner_group

		# Swap the document view. Explicitly set the inner_group's frame in
		# clip-view coordinates first, then ask the scroll view to relayout via
		# tile() + reflectScrolledClipView_ — without these, NSScrollView can
		# leave the old placeholder's scroll range in place and the new content
		# renders but is invisible behind the empty scroll area.
		try:
			scroll_ns = self.w.instanceScroll._nsObject
			old_doc = scroll_ns.documentView()
			if old_doc is not None and old_doc is not inner_group._nsObject:
				old_doc.removeFromSuperview()
			try:
				from Foundation import NSMakeRect, NSMakePoint  # type: ignore
				inner_group._nsObject.setFrame_(NSMakeRect(0, 0, group_width, inner_h))
			except (ImportError, AttributeError):
				pass
			scroll_ns.setDocumentView_(inner_group._nsObject)
			# Force NSScrollView to recompute its scroll range and redisplay.
			try:
				scroll_ns.tile()
				scroll_ns.reflectScrolledClipView_(scroll_ns.contentView())
				scroll_ns.setNeedsDisplay_(True)
				inner_group._nsObject.setNeedsDisplay_(True)
			except AttributeError:
				pass
		except Exception:
			# Last-resort: leave previous doc view in place rather than crash.
			pass

		self.w.instanceScroll.show(True)
		# Surface the initial 0/N count.
		self._refresh_selection_count()

		new_h = self._compute_window_height(n)
		# Only call resize when the height actually changes — vanilla.resize
		# triggers a real AppKit layout pass even when the size is unchanged.
		try:
			current_size = self.w.getPosSize()
			current_h = current_size[3] if len(current_size) >= 4 else None
		except (AttributeError, RuntimeError):
			current_h = None
		if current_h != new_h:
			self.w.resize(self.W, new_h)

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _iter_checks(self):
		"""Yield every CheckBox widget."""
		for cb in self._checks:
			yield cb

	def _set_all_checks(self, value):
		"""Set every checkbox to ``value`` and refresh dependent UI."""
		for cb in self._iter_checks():
			cb.set(bool(value))
		self._on_check_toggled(None)

	def _selected_instance_names(self):
		"""Return names of instances whose checkbox is ticked."""
		if not self._checks:
			return []
		selected = []
		for name, cb in zip(self._instance_names, self._checks):
			if cb and cb.get():
				selected.append(name)
		return selected

	@objc.python_method
	def _refresh_name(self, selected=None):
		"""Auto-compute the output name unless the user has overridden it.

		Callers that already walked the checkbox list can pass ``selected`` to
		avoid an extra O(N) bridge walk.
		"""
		if self._name_overridden:
			return
		if selected is None:
			selected = self._selected_instance_names()
		if not selected:
			self.w.nameField.set('')
			return

		base = (
			os.path.splitext(os.path.basename(self._font_path))[0]
			if self._font_path else ''
		)
		computed = compute_default_output_name(base, selected[0], selected[-1])
		self.w.nameField.set(computed.strip())

	@objc.python_method
	def _refresh_axis_preview(self, selected=None):
		"""Render the axis hull as colored ■-prefixed chips, one axis per line.

		Uses the parsed TTFont cached by ``_load_font`` rather than re-parsing
		the disk file on every checkbox toggle. Callers that already computed
		the selection list can pass it via ``selected``.
		"""
		if selected is None:
			selected = self._selected_instance_names()
		if not selected:
			self._set_hull_text('(select instances to preview)')
			return

		hull = {}
		try:
			if self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None:
				hull = compute_gsfont_hull(self._gsfont, selected)
			elif self._font_path:
				cached = getattr(self, '_cached_font', None)
				if cached is not None:
					hull = get_axis_hull_from_instances(cached, selected)
				else:
					# Fallback for callers that bypass _load_font's cache populate.
					import contextlib as _cl
					with _cl.closing(open_font_safely(self._font_path)) as f:
						hull = get_axis_hull_from_instances(f, selected)
			else:
				self._set_hull_text('(load a font to preview)')
				return
		except Exception as e:
			self._set_hull_text(f'(unavailable: {e})')
			return

		if not hull:
			self._set_hull_text('(no axes)')
			return

		# Build an attributed string: per-axis line is "■  TAG  lo–hi" where the
		# leading ■ is colored by the per-axis palette, the tag stays uppercase
		# and bold-ish, and the range trails in muted text.
		attr = NSMutableAttributedString.alloc().init()
		small_font = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize())
		mono_font = NSFont.monospacedDigitSystemFontOfSize_weight_(
			NSFont.smallSystemFontSize(),
			0.0,  # NSFontWeightRegular
		)
		muted = NSColor.secondaryLabelColor()
		label = NSColor.labelColor()

		first = True
		for tag, (lo, hi) in hull.items():
			if not first:
				attr.appendAttributedString_(
					NSAttributedString.alloc().initWithString_attributes_('\n', {})
				)
			first = False
			# Colored square
			attr.appendAttributedString_(
				NSAttributedString.alloc().initWithString_attributes_(
					'■  ',
					{
						NSForegroundColorAttributeName: _nscolor_for_axis(tag),
						NSFontAttributeName: small_font,
					},
				)
			)
			# Tag name in primary text color
			attr.appendAttributedString_(
				NSAttributedString.alloc().initWithString_attributes_(
					f'{tag}',
					{
						NSForegroundColorAttributeName: label,
						NSFontAttributeName: small_font,
					},
				)
			)
			# Range trailing in muted color, monospaced digits for alignment
			a = f'{lo:g}'
			b = f'{hi:g}'
			range_text = f'  pinned at {a}' if a == b else f'  {a} – {b}'
			attr.appendAttributedString_(
				NSAttributedString.alloc().initWithString_attributes_(
					range_text,
					{
						NSForegroundColorAttributeName: muted,
						NSFontAttributeName: mono_font,
					},
				)
			)

		try:
			self.w.axisPreview._nsObject.setAttributedStringValue_(attr)
		except Exception:
			# Fall back to plain text if attributed rendering is unavailable
			# on this Glyphs build.
			parts = []
			for tag, (lo, hi) in hull.items():
				a = f'{lo:g}'
				b = f'{hi:g}'
				parts.append(f'{tag} {a}' if a == b else f'{tag} {a}–{b}')
			self.w.axisPreview.set('  ·  '.join(parts))

		# Accessibility: VoiceOver would otherwise read the leading "■"
		# as "black square" for every axis. Expose a clean axis-by-axis
		# summary as the accessibility value instead (the colored chip is
		# a redundant visual cue — WCAG 1.4.1 prohibits color-only encoding).
		ax_parts = []
		for tag, (lo, hi) in hull.items():
			a = f'{lo:g}'
			b = f'{hi:g}'
			if a == b:
				ax_parts.append(f'{tag} pinned at {a}')
			else:
				ax_parts.append(f'{tag} {a} to {b}')
		ax_summary = '; '.join(ax_parts) if ax_parts else 'No axes'
		try:
			self.w.axisPreview._nsObject.setAccessibilityLabel_('Axis hull')
			self.w.axisPreview._nsObject.setAccessibilityValue_(ax_summary)
		except (AttributeError, RuntimeError):
			pass

	@objc.python_method
	def _set_hull_text(self, text):
		"""Set the hull preview to plain placeholder/error text in muted style."""
		try:
			attr = NSAttributedString.alloc().initWithString_attributes_(
				text,
				{
					NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
					NSFontAttributeName: NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()),
				},
			)
			self.w.axisPreview._nsObject.setAttributedStringValue_(attr)
		except Exception:
			self.w.axisPreview.set(text)

	def _refresh_generate_button(self, selected=None):
		"""Enable Generate only when a source is loaded and >=1 instance selected.

		Callers that already walked the checkbox list can pass ``selected`` to
		avoid an extra O(N) bridge walk.
		"""
		source_loaded = (
			self._font_path is not None
			or (self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None)
		)
		if selected is None:
			selected = self._selected_instance_names()
		enabled = bool(source_loaded and selected)
		self.w.generateButton.enable(enabled)

	@objc.python_method
	def _set_status(self, message, error=False):
		"""Update the status label. Errors are prefixed with 'Error:'."""
		# Prefer text-only prefix over emoji; emoji renders as .notdef on older macOS.
		text = f'Error: {message}' if error else message
		self.w.statusLabel.set(text)

	# ------------------------------------------------------------------
	# Public
	# ------------------------------------------------------------------

	def show(self):
		"""Bring the dialog window to the front."""
		self.w.open()
