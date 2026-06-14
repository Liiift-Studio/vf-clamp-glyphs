# plugin.py — vf-clamp Glyphs.app plugin shell around core.py.
# Pure UI/registration concerns; all fonttools work lives in core.py.

import os
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
	NSFontWeightSemibold,
	NSTextAlignmentRight,
	NSEvent,
	NSEventMaskKeyDown,
	NSCommandKeyMask,
	NSView,
	NSDragOperationCopy,
	NSDragOperationNone,
	NSFilenamesPboardType,
	NSPasteboardTypeFileURL,
	NSRectFill,
	NSMakeRect,
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


# ---------------------------------------------------------------------------
# Format metadata — short human-readable descriptions surface beside the popup
# so the user knows what they're picking before they click Generate. WOFF2
# carries an explicit brotli warning because the dependency isn't always
# bundled with Glyphs and the failure mode is otherwise opaque.
# ---------------------------------------------------------------------------

FORMAT_DESCRIPTIONS = {
	'TTF': 'TrueType binary variable font',
	'OTF': 'OpenType (CFF2) binary variable font',
	'WOFF': 'Web Open Font Format wrapper',
	'WOFF2': 'WOFF2 compressed web font (requires brotli)',
	'GLYPHS': '.glyphs source file you can open in Glyphs',
}


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


def _rgb_for_axis(tag):
	"""Return the raw (r, g, b) tuple for ``tag`` using the active palette."""
	if _is_dark_appearance():
		palette = AXIS_COLORS_DARK
		default = DEFAULT_AXIS_COLOR_DARK
	else:
		palette = AXIS_COLORS_LIGHT
		default = DEFAULT_AXIS_COLOR_LIGHT
	return palette.get(tag, default)


def _nscolor_for_axis(tag):
	"""Return an NSColor for the small chip that sits next to ``tag`` in the hull preview.

	Picks the dark- or light-mode palette to maintain contrast in both
	system appearances. Falls back to the dark palette if appearance lookup
	fails (matches Glyphs' historical default).
	"""
	rgb = _rgb_for_axis(tag)
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

# Local sibling modules — small enough that we import them eagerly.
from presets import (  # noqa: E402
	load_presets,
	save_presets,
	make_preset,
	load_recent_folders,
	save_recent_folders,
	push_recent_folder,
	validate_output_name,
	RECENT_FOLDERS_MAX,
)
from hull_plot import make_hull_plot_view, is_available as hull_plot_available  # noqa: E402
from preview_view import make_preview_view, is_available as preview_view_available  # noqa: E402
from font_registration import (  # noqa: E402
	register_font_at_path,
	export_gsfont_to_temp_vf_async,
	cleanup_all_temp_paths,
	is_available as font_registration_available,
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
# Drag-drop NSView — accepts font files dropped onto the file-source path
# field and routes them through the dialog's _load_font helper.
# ---------------------------------------------------------------------------

class _LogActivityStripe(NSView):
	"""Thin accent-coloured strip pinned to the left edge of the LOG pane.

	Flashes for ~0.8 seconds each time ``flash()`` is called (which the
	dialog wires into ``_log_append``), giving users a peripheral cue that
	new content has landed in the log without stealing focus. Addresses the
	Interaction Designer's "log lacks read/unread affordance" finding.
	"""

	def init(self):
		self = objc.super(_LogActivityStripe, self).init()
		if self is None:
			return None
		self._alpha = 0.0
		self._fade_timer = None
		return self

	def isFlipped(self):
		return True

	def isOpaque(self):
		return False

	def acceptsFirstResponder(self):
		return False

	def flash(self):
		"""Re-trigger the strip's fade to maximum brightness."""
		from AppKit import NSTimer
		self._alpha = 1.0
		t = self._fade_timer
		if t is not None:
			try:
				t.invalidate()
			except (AttributeError, RuntimeError):
				pass
		try:
			self._fade_timer = (
				NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
					1.0 / 30.0, self, 'tickFade:', None, True,
				)
			)
		except (AttributeError, RuntimeError):
			self._fade_timer = None
		try:
			self.setNeedsDisplay_(True)
		except Exception:
			pass

	def tickFade_(self, _timer):
		"""NSTimer callback — decay alpha + invalidate when faded out."""
		# 24 frames at 30 fps == 0.8 s total fade duration.
		self._alpha = max(0.0, self._alpha - 1.0 / 24.0)
		try:
			self.setNeedsDisplay_(True)
		except Exception:
			pass
		if self._alpha <= 0.0:
			t = self._fade_timer
			self._fade_timer = None
			if t is not None:
				try:
					t.invalidate()
				except (AttributeError, RuntimeError):
					pass

	def drawRect_(self, _rect):
		if self._alpha <= 0.0:
			return
		try:
			NSColor.controlAccentColor().colorWithAlphaComponent_(self._alpha).set()
			NSRectFill(self.bounds())
		except Exception:
			pass


class _FontDropView(NSView):
	"""Transparent NSView overlay that accepts dragged font files.

	Sits on top of the read-only fontPathField and converts file drops into
	calls back into the dialog. We use an overlay rather than subclassing
	NSTextField because the latter triggers a cascade of focus-ring and
	first-responder bugs in vanilla.EditText.
	"""

	def initWithFrame_dialog_(self, frame, dialog):
		"""Stash a weak-ish ref to the dialog so callbacks can find it."""
		self = objc.super(_FontDropView, self).initWithFrame_(frame)
		if self is None:
			return None
		self._dialog = dialog
		# Register for the modern (NSPasteboardTypeFileURL) and legacy
		# (NSFilenamesPboardType) drop types. Glyphs is currently shipped
		# with PyObjC that exposes both.
		try:
			self.registerForDraggedTypes_([
				NSPasteboardTypeFileURL,
				NSFilenamesPboardType,
			])
		except Exception:
			pass
		return self

	def acceptsFirstMouse_(self, event):
		"""Let clicks pass through to the EditText below."""
		return False

	def _extract_path(self, sender):
		"""Pull a single .ttf/.otf/.woff/.woff2 path off the drag pasteboard."""
		try:
			pb = sender.draggingPasteboard()
		except Exception:
			return None
		paths = []
		try:
			files = pb.propertyListForType_(NSFilenamesPboardType)
			if files:
				paths.extend(files)
		except Exception:
			pass
		if not paths:
			try:
				urls = pb.readObjectsForClasses_options_([], None) or []
				for u in urls:
					p = getattr(u, 'path', None)
					if callable(p):
						paths.append(p())
			except Exception:
				pass
		for p in paths:
			ext = os.path.splitext(p)[1].lower().lstrip('.')
			if ext in ('ttf', 'otf', 'woff', 'woff2'):
				return p
		return None

	def draggingEntered_(self, sender):
		"""Light up the drop ring when a font file enters the field."""
		return NSDragOperationCopy if self._extract_path(sender) else NSDragOperationNone

	def prepareForDragOperation_(self, sender):
		"""Final accept gate — must mirror draggingEntered_."""
		return self._extract_path(sender) is not None

	def performDragOperation_(self, sender):
		"""Route the dropped file into the dialog's load pipeline."""
		path = self._extract_path(sender)
		if not path:
			return False
		try:
			# Toggle to file mode first so the user sees the load happen in
			# context. The dialog's own helper handles status messaging.
			self._dialog._transition_source_mode(self._dialog.SOURCE_FILE)
			self._dialog._load_font(path)
		except Exception:
			traceback.print_exc()
			return False
		return True


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class VFClampDialog:
	"""Dialog for selecting named instances and generating a restricted VF.

	v1.2.0 introduces a three-zone layout:

	  Zone 1 — Source picker (radio + file path / open-font popup)
	  Zone 2 — Dashboard: instance list (left) + output preview (right)
	  Zone 3 — Output options (preset, name, format, folder)

	The bottom action bar carries shortcut hints on the left and a large
	primary-blue Generate button on the right.
	"""

	# Pixel metrics — kept as class attrs so future dialogs cannot shadow them.
	# v1.2.0 widens the dialog to fit the two-column dashboard.
	W = 820
	PAD = 16
	LABEL_H = 20
	FIELD_H = 22
	BTN_H = 24
	ROW = 28
	# Zone heights are fixed so the action bar always sits at the same Y.
	ZONE1_H = 96
	ZONE2_H = 348
	ZONE3_H = 174  # bumped to fit the new "Open after generating" checkbox row
	# Scrollable error/status log between zone 3 and the action bar.
	LOG_H = 84
	# Dashboard internals
	COL_GAP = 16
	# Bottom action bar button widths — Generate is larger and primary blue.
	GENERATE_W = 140
	GENERATE_H = 32
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

	# Preset popup sentinels — must not collide with user preset names.
	_PRESET_NONE_LABEL = '(no preset)'
	_PRESET_SAVE_LABEL = '— Save Current… —'
	_PRESET_MANAGE_LABEL = '— Manage… —'

	# Recent-folders popup leader label.
	_RECENT_HEADER_LABEL = '▾ Recent'

	# Smart-select More popup options.
	_MORE_SELECT_HEADER = '▾ More'
	_MORE_SELECT_ITALIC = 'Select All Italic'
	_MORE_SELECT_ROMAN = 'Select All Roman'

	def __init__(self):
		"""Initialise dialog state. Window is shown by show()."""
		self._font_path = None
		# Parsed TTFont cache populated by _load_font and reused by
		# _refresh_preview so list edits don't re-parse the disk file.
		self._cached_font = None

		# Instance model — replaces the old list-of-CheckBox widgets.
		# All three lists are parallel and the source of truth for selection.
		self._instance_names: List[str] = []          # ordered subfamily labels
		self._instance_coords: List[Dict[str, float]] = []   # per-instance axis coord dicts
		self._instance_checked: List[bool] = []       # parallel bool list
		self._instance_filter: str = ''               # current SearchBox text
		self._visible_to_full: List[int] = []         # row index -> _instance_names index
		# Re-entry guard so a bulk model update doesn't fire N edit callbacks.
		self._suspend_list_edit_cb: bool = False
		# Axis ranges for the hull plot (full design space, not the hull).
		self._fvar_axis_ranges: Dict[str, Tuple[float, float, float]] = {}

		self._name_overridden = False
		# Phase 1+2: source can be a file on disk or a currently-open GSFont.
		self._source_mode = self.SOURCE_FILE
		self._gsfont = None         # GSFont when _source_mode == SOURCE_GSFONT
		self._gsfont_options = []   # list of currently-open GSFonts mirroring popup order
		# Cancellation flag — set by _on_cancel, read by worker thread to short-circuit
		# the file-source pipeline and skip writing partial output.
		self._cancelled = False
		# Output measurements — when known, drive the size-estimate line.
		self._source_size_bytes: Optional[int] = None
		# v1.2.6: "Open output after generating" checkbox state — defaulted to
		# False so existing automation isn't surprised by a sudden tab open.
		self._open_after_save = False

		# Persistent state — presets + recents. Failures fall back to empty.
		try:
			self._presets = load_presets()
		except Exception:
			self._presets = {}
		try:
			self._recent_folders = load_recent_folders()
		except Exception:
			self._recent_folders = []

		# Keyboard-shortcut local event monitor handle; removed on close().
		self._shortcut_monitor = None
		# Drag-drop overlay — kept around so AppKit retains it.
		self._drop_view = None
		# Hull plot custom NSView (None when AppKit is unavailable).
		self._hull_plot_view = None
		# Last generated path for the Reveal button.
		self._last_output_path = None

		self._build_window()
		# Populate the open-Glyphs-font popup once the window exists.
		self._refresh_gsfont_popup()
		self._refresh_presets_popup()
		self._refresh_recents_popup()
		# Default to "Open Font" mode when at least one Glyphs document is
		# open; falls back to "File" when no Glyphs documents are open.
		if self._gsfont_options:
			self._transition_source_mode(self.SOURCE_GSFONT, clear_inactive=False)
			self._auto_select_frontmost_gsfont()
		else:
			self._transition_source_mode(self.SOURCE_FILE, clear_inactive=False)

		# Wire keyboard shortcuts after the window is fully built so the
		# monitor closure can safely call back into self.w.
		self._install_shortcut_monitor()

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

	@objc.python_method
	def _semibold_label(self, posSize, text):
		"""Build a small semibold TextBox used as a section header inside a zone."""
		box = vanilla.TextBox(posSize, text)
		try:
			cell = box._nsObject.cell()
			cell.setFont_(
				NSFont.systemFontOfSize_weight_(NSFont.smallSystemFontSize(), NSFontWeightSemibold)
			)
		except Exception:
			pass
		return box

	def _build_window(self):
		"""Build the three-zone dialog layout."""
		w = self.W
		h = self._compute_window_height()

		self.w = vanilla.Window(
			(w, h),
			'◆ vf-clamp — Generate Restricted VFs',
			minSize=(w, h),
			maxSize=(w, h),
		)
		# Persist window position across launches.
		try:
			nswin = self.w.getNSWindow() if hasattr(self.w, 'getNSWindow') else self.w._window
			if nswin is not None:
				nswin.setFrameAutosaveName_('com.liiift.vf-clamp.dialog')
		except (AttributeError, RuntimeError):
			pass

		win = self.w
		PAD = self.PAD
		y = PAD
		y = self._build_zone_source(y)
		y = self._build_zone_dashboard(y)
		y = self._build_zone_output(y)
		y = self._build_log_pane(y)
		y = self._build_action_bar(y)
		self._static_sections_height = y

	# ------------------------------------------------------------------
	# Zone builders
	# ------------------------------------------------------------------

	def _build_zone_source(self, y):
		"""Zone 1 — Source picker. Returns the new y after the zone.

		IMPORTANT: vanilla.Box's re-parenting via ``box.attr = win.widget`` is
		unreliable across Glyphs builds — widgets stay attached to the window
		root regardless. So we create the Box as a decorative frame only and
		place every child widget at window-relative coordinates by adding
		``PAD`` to X and ``y`` to Y for inputs that the previous (broken)
		layout treated as box-relative.
		"""
		win = self.w
		PAD = self.PAD
		win.zone1 = vanilla.Box((PAD, y, -PAD, self.ZONE1_H))

		# Window-relative coordinate origin for widgets that should appear
		# inside this zone's visual frame.
		left = PAD + 12
		right_inset = PAD + 12

		# Title row — small caps label at the top of the box's interior.
		win.zone1Title = self._semibold_label(
			(left, y + 6, -right_inset, 18), 'SOURCE',
		)

		# Radio row
		win.sourceRadio = vanilla.RadioGroup(
			(left, y + 30, 220, 22),
			['Open Font', 'File'],
			isVertical=False,
			callback=self._on_source_radio_changed,
		)
		win.sourceRadio.set(1)
		try:
			win.sourceRadio._nsObject.setAccessibilityLabel_('Font source')
		except (AttributeError, RuntimeError):
			pass

		# Input row — both widgets share Y; visibility flips with mode.
		# File mode
		win.fontPathField = vanilla.EditText(
			(left, y + 60, -(right_inset + 100), 22),
			placeholder='Drag a .ttf/.otf/.woff/.woff2 here or click Browse…',
			readOnly=True,
		)
		win.browseButton = vanilla.Button(
			(-(right_inset + 90), y + 59, 88, 24),
			'Browse…',
			callback=self._on_browse,
		)
		# Open-Font mode
		win.gsfontPopup = vanilla.PopUpButton(
			(left, y + 60, -right_inset, 24),
			[self._GSFONT_POPUP_EMPTY],
			callback=self._on_gsfont_chosen,
		)
		for widget, ax_label in (
			(win.fontPathField, 'Selected font file path'),
			(win.browseButton, 'Browse for a variable font file'),
			(win.gsfontPopup, 'Open Glyphs font'),
		):
			try:
				widget._nsObject.setAccessibilityLabel_(ax_label)
			except (AttributeError, RuntimeError):
				pass

		# Attach drag-drop overlay over the file path field. Created after the
		# field exists so the overlay's frame matches the EditText.
		self._install_drop_handler()

		return y + self.ZONE1_H + PAD

	def _build_zone_dashboard(self, y):
		"""Zone 2 — instance picker (left) + output preview (right).

		See _build_zone_source for why all widgets use window-relative coords
		instead of being nested inside the Box.
		"""
		win = self.w
		PAD = self.PAD
		ZONE_H = self.ZONE2_H
		win.zone2 = vanilla.Box((PAD, y, -PAD, ZONE_H))

		# Compute the two halves in window-relative coords. The Box draws a
		# decorative frame at (PAD, y, -PAD, ZONE_H); widgets are placed at
		# window-X = PAD + box-interior-X, window-Y = y + box-interior-Y.
		inset = 12
		inner_w = self.W - 2 * PAD - 2 * inset
		col_w = (inner_w - self.COL_GAP) // 2
		left_x = PAD + inset
		right_x = PAD + inset + col_w + self.COL_GAP
		# Shorthand: convert a box-interior Y to a window-relative Y.
		def Y(box_y):
			return y + box_y

		# ---------- LEFT HALF — Instance picker ----------
		win.instanceHeader = self._semibold_label(
			(left_x, Y(6), col_w, 18), 'INSTANCES (0 OF 0)',
		)
		# Filter SearchBox — falls back to EditText on older vanilla builds.
		try:
			win.filterBox = vanilla.SearchBox(
				(left_x, Y(30), col_w, 22),
				placeholder='Filter…',
				callback=self._on_filter_changed,
			)
		except AttributeError:
			win.filterBox = vanilla.EditText(
				(left_x, Y(30), col_w, 22),
				placeholder='Filter…',
				callback=self._on_filter_changed,
			)

		# Bulk select buttons row
		btn_y = Y(60)
		win.allBtn = vanilla.Button(
			(left_x, btn_y, 56, 22), 'All',
			callback=self._on_select_all, sizeStyle='small',
		)
		win.noneBtn = vanilla.Button(
			(left_x + 60, btn_y, 56, 22), 'None',
			callback=self._on_select_none, sizeStyle='small',
		)
		win.invertBtn = vanilla.Button(
			(left_x + 120, btn_y, 64, 22), 'Invert',
			callback=self._on_select_invert, sizeStyle='small',
		)
		win.moreSelectBtn = vanilla.PopUpButton(
			(left_x + 188, btn_y, 110, 22),
			[self._MORE_SELECT_HEADER, self._MORE_SELECT_ITALIC, self._MORE_SELECT_ROMAN],
			callback=self._on_more_select,
		)
		for btn_widget, ax_label in (
			(win.allBtn, 'Select all instances'),
			(win.noneBtn, 'Deselect all instances'),
			(win.invertBtn, 'Invert instance selection'),
			(win.moreSelectBtn, 'More selection options'),
		):
			try:
				btn_widget._nsObject.setAccessibilityLabel_(ax_label)
			except (AttributeError, RuntimeError):
				pass
			try:
				btn_widget._nsObject.setToolTip_(ax_label)
			except (AttributeError, RuntimeError):
				pass

		# vanilla.List — replaces the prior ScrollView-of-CheckBox layout.
		list_y = btn_y + 30
		list_h = ZONE_H - (list_y - y) - 10
		try:
			win.instanceList = vanilla.List(
				(left_x, list_y, col_w, list_h),
				items=[],
				columnDescriptions=[
					dict(
						title='', key='checked',
						cell=vanilla.CheckBoxListCell(), width=22, editable=True,
					),
					dict(title='Instance', key='display', editable=False),
				],
				allowsMultipleSelection=True,
				allowsEmptySelection=True,
				editCallback=self._on_list_edit,
				selectionCallback=self._on_list_selection,
				showColumnTitles=False,
			)
			self._using_vanilla_list = True
		except (AttributeError, TypeError, RuntimeError):
			# Fallback: vanilla.List with CheckBoxListCell is unavailable on
			# this Glyphs build — surface a placeholder so the dialog still
			# opens. (Smart-select / filter are no-ops in the fallback.)
			win.instanceList = vanilla.TextBox(  # type: ignore[assignment]
				(left_x, list_y, col_w, list_h),
				'(instance list unavailable on this Glyphs build)',
				sizeStyle='small',
			)
			self._using_vanilla_list = False
		try:
			win.instanceList._nsObject.setAccessibilityLabel_('Named instances')
		except (AttributeError, RuntimeError):
			pass

		# v1.2.12: install a monospaced font on the "Instance" column so
		# the ljust-padded display strings from _build_list_items actually
		# render at consistent column widths. Without this the proportional
		# system font would still leave the wght/opsz values ragged.
		try:
			tv = None
			try:
				tv = win.instanceList.getNSTableView()
			except (AttributeError, RuntimeError):
				tv = getattr(win.instanceList, '_tableView', None)
			if tv is not None:
				cols = tv.tableColumns()
				if cols and len(cols) >= 2:
					mono = NSFont.monospacedSystemFontOfSize_weight_(
						NSFont.smallSystemFontSize(), 0.0,
					)
					cols[1].dataCell().setFont_(mono)
		except (AttributeError, RuntimeError):
			pass

		# Bulk-select controls and filter hidden until a font is loaded so
		# dead affordances don't surface a stale "click All" with nothing to
		# select. Same pattern as v1.1.x.
		for widget in (
			win.allBtn, win.noneBtn, win.invertBtn,
			win.moreSelectBtn, win.filterBox,
		):
			try:
				widget.show(False)
			except (AttributeError, RuntimeError):
				pass

		# ---------- RIGHT HALF — Output preview ----------
		win.previewHeader = self._semibold_label(
			(right_x, Y(6), col_w, 18), 'OUTPUT PREVIEW',
		)
		win.previewName = vanilla.TextBox(
			(right_x, Y(30), col_w, 22),
			'',
			selectable=True,
		)
		try:
			cell = win.previewName._nsObject.cell()
			cell.setFont_(NSFont.systemFontOfSize_(14.0))
		except Exception:
			pass

		# Hull plot — custom NSView when available, else a chips text box.
		# CRITICAL: addSubview_ on an unflipped NSView uses bottom-left origin
		# (macOS default), but our Y values throughout this dialog are top-
		# left. Convert top-y → bottom-y before constructing the frame.
		# Vanilla widgets auto-flip; raw NSView addSubview_ does not.
		plot_y_box = 60
		# v1.2.14 pulled back from 175 → 150 after the second designer
		# review flagged Zone 2's vertical balance as compromised — the
		# 175-px plot starved the specimen area below. 150 is still a
		# clear win over the original 140 (more breathing room for the
		# 4 dot rows + bigger label gap) but leaves the lower half of
		# the right column workable for the two-up specimen.
		plot_h = 150
		plot_y = Y(plot_y_box)
		window_h = self._compute_window_height()
		plot_y_flipped = window_h - plot_y - plot_h
		view = make_hull_plot_view((right_x, plot_y_flipped, col_w, plot_h))
		if view is not None:
			try:
				win._window.contentView().addSubview_(view)
				self._hull_plot_view = view
			except (AttributeError, RuntimeError):
				self._hull_plot_view = None

		# Chips fallback — always exists. When the custom view is present
		# and showing <=2 axes, the chips view is hidden.
		win.hullChips = vanilla.TextBox(
			(right_x, plot_y, col_w, plot_h),
			'(select instances to preview)',
			sizeStyle='small',
			selectable=True,
		)
		try:
			cell = win.hullChips._nsObject.cell()
			cell.setUsesSingleLineMode_(False)
			cell.setWraps_(True)
		except (AttributeError, RuntimeError):
			pass

		# Size estimate
		win.sizeEstimate = self._right_label(
			(right_x, plot_y + plot_h + 8, col_w, 18), '',
		)
		try:
			cell = win.sizeEstimate._nsObject.cell()
			cell.setAlignment_(0)  # NSTextAlignmentLeft
			cell.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
			cell.setTextColor_(NSColor.secondaryLabelColor())
		except Exception:
			pass

		# --- Animated VF specimen preview ("HOHO Anes") -----------------
		# Same top→bottom Y conversion as the hull plot above.
		# Gap tightened from 32 → 22 (v1.2.12). Bottom margin restored
		# from 10 → 16 (v1.2.14) so the specimen-to-Zone3 spacing matches
		# the rest of the 16 px PAD rhythm — Visual Designer flagged 10
		# as a one-off break in the dialog's vertical baseline.
		preview_top_y = plot_y + plot_h + 22
		preview_h = ZONE_H - (preview_top_y - y) - 16
		preview_y_flipped = window_h - preview_top_y - preview_h
		self._preview_view = None
		if preview_view_available() and preview_h >= 60:
			pv = make_preview_view((right_x, preview_y_flipped, col_w, preview_h))
			if pv is not None:
				try:
					win._window.contentView().addSubview_(pv)
					self._preview_view = pv
					# Specimen size — v1.2.17 back up to 48 pt now that the
					# preview is a single animated specimen again (no longer
					# two-up). 48 pt fills the right column well even at the
					# heaviest weight + sits comfortably in the ~100-px
					# preview height left after the 150-px plot above.
					try:
						pv.setFontSize_(48.0)
					except (AttributeError, RuntimeError):
						pass
					# v1.2.10 animation probe: feed the specimen's tick into
					# the hull plot so the user sees a live ring tracking the
					# current variation values inside the hull rectangle.
					try:
						if self._hull_plot_view is not None:
							pv.setProbeTarget_(self._hull_plot_view)
					except (AttributeError, RuntimeError):
						pass
				except (AttributeError, RuntimeError):
					self._preview_view = None

		return y + ZONE_H + PAD

	def _build_zone_output(self, y):
		"""Zone 3 — preset, output name, format, folder.

		See _build_zone_source for why the Box is decorative-only and all
		child widgets use window-relative coords.
		"""
		win = self.w
		PAD = self.PAD
		ZONE_H = self.ZONE3_H
		win.zone3 = vanilla.Box((PAD, y, -PAD, ZONE_H))

		# Layout grid — right-aligned label column + control column.
		# Window-relative X coords: PAD + box-interior X.
		LABEL_W = 90
		LABEL_X = PAD + 12
		CTRL_X = PAD + 12 + LABEL_W + 8
		# Right inset for widgets anchored to the right edge.
		R_INSET = PAD + 12

		def Y(box_y):
			return y + box_y

		row_y = 8
		# --- Preset
		win.presetLabel = self._right_label(
			(LABEL_X, Y(row_y + 2), LABEL_W, 20), 'Preset:',
		)
		win.presetPopup = vanilla.PopUpButton(
			(CTRL_X, Y(row_y), 240, 22),
			[self._PRESET_NONE_LABEL, self._PRESET_SAVE_LABEL, self._PRESET_MANAGE_LABEL],
			callback=self._on_preset_chosen,
		)
		try:
			win.presetPopup._nsObject.setAccessibilityLabel_('Preset')
		except (AttributeError, RuntimeError):
			pass
		row_y += 30

		# --- Output name
		win.nameLabel = self._right_label(
			(LABEL_X, Y(row_y + 2), LABEL_W, 20), 'Output Name:',
		)
		win.nameField = vanilla.EditText(
			(CTRL_X, Y(row_y), -R_INSET, 22),
			placeholder='e.g. MyFont Light-Bold',
			callback=self._on_name_edited,
		)
		try:
			win.nameField._nsObject.setAccessibilityLabel_('Output family name')
		except (AttributeError, RuntimeError):
			pass
		row_y += 30

		# --- Format + description
		win.formatLabel = self._right_label(
			(LABEL_X, Y(row_y + 2), LABEL_W, 20), 'Format:',
		)
		win.formatPopup = vanilla.PopUpButton(
			(CTRL_X, Y(row_y), 120, 22),
			list(self.BINARY_FORMATS),
			callback=self._on_format_changed,
		)
		try:
			win.formatPopup._nsObject.setAccessibilityLabel_('Output font format')
		except (AttributeError, RuntimeError):
			pass
		win.formatDescription = vanilla.TextBox(
			(CTRL_X + 130, Y(row_y + 2), -R_INSET, 18),
			'',
			sizeStyle='small',
			selectable=True,
		)
		try:
			cell = win.formatDescription._nsObject.cell()
			cell.setTextColor_(NSColor.secondaryLabelColor())
		except Exception:
			pass
		row_y += 30

		# --- Folder
		win.folderLabel = self._right_label(
			(LABEL_X, Y(row_y + 2), LABEL_W, 20), 'Folder:',
		)
		# Layout: [folderField][Choose…][▾ Recent]
		win.folderField = vanilla.EditText(
			(CTRL_X, Y(row_y), -(R_INSET + 206), 22),
			placeholder='Default: same folder as source',
			readOnly=True,
		)
		win.folderButton = vanilla.Button(
			(-(R_INSET + 200), Y(row_y - 1), 90, 24),
			'Choose…',
			callback=self._on_choose_folder,
		)
		win.recentFoldersPopup = vanilla.PopUpButton(
			(-(R_INSET + 100), Y(row_y - 1), 100, 24),
			[self._RECENT_HEADER_LABEL],
			callback=self._on_recent_folder_chosen,
		)
		for widget, ax_label in (
			(win.folderField, 'Output folder path'),
			(win.folderButton, 'Choose the output folder'),
			(win.recentFoldersPopup, 'Recent folders'),
		):
			try:
				widget._nsObject.setAccessibilityLabel_(ax_label)
			except (AttributeError, RuntimeError):
				pass
		row_y += 30

		# --- "Open after generating" checkbox ------------------------
		win.openAfterSave = vanilla.CheckBox(
			(CTRL_X, Y(row_y), -R_INSET, 22),
			'Open output in Glyphs (or default app) after generating',
			value=getattr(self, '_open_after_save', False),
			callback=self._on_open_after_save_changed,
		)
		try:
			win.openAfterSave._nsObject.setAccessibilityLabel_(
				'Open the saved file after generating'
			)
		except (AttributeError, RuntimeError):
			pass

		return y + ZONE_H + PAD

	def _build_log_pane(self, y):
		"""Scrollable read-only log pane for errors + status messages.

		Sits between zone 3 and the action bar so error output has room to
		breathe instead of being squeezed into a 74-px sliver next to the
		Generate button. Multi-line, selectable, monospaced so tracebacks
		stay readable.
		"""
		win = self.w
		PAD = self.PAD
		LOG_H = self.LOG_H
		win.logHeader = self._semibold_label(
			(PAD + 12, y, 200, 16), 'LOG',
		)
		# vanilla.TextEditor wraps NSTextView in an NSScrollView with native
		# vertical scrolling for free.
		win.logEditor = vanilla.TextEditor(
			(PAD, y + 18, -PAD, LOG_H - 18),
			text='',
			readOnly=True,
			callback=None,
		)
		try:
			ed = win.logEditor._nsObject  # NSScrollView
			tv = ed.documentView() if hasattr(ed, 'documentView') else None
			if tv is not None:
				tv.setFont_(NSFont.userFixedPitchFontOfSize_(11.0))
				tv.setEditable_(False)
				tv.setSelectable_(True)
				tv.setBackgroundColor_(
					NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.18),
				)
				tv.setTextColor_(NSColor.labelColor())
		except (AttributeError, RuntimeError):
			pass
		try:
			win.logEditor._nsObject.setAccessibilityLabel_(
				'Log of recent status messages and errors',
			)
		except (AttributeError, RuntimeError):
			pass
		# v1.2.15 activity stripe — thin accent bar pinned to the LOG's
		# left edge. Flashes for ~0.8 s each time _log_append adds a line
		# so users get a peripheral signal that new content arrived without
		# stealing focus. Mounted directly on the window's contentView so
		# it sits ABOVE the NSScrollView and doesn't get clipped.
		try:
			stripe_w = 3
			stripe_y_top = y + 18
			stripe_h = LOG_H - 18
			window_h = self._compute_window_height()
			stripe_y_flipped = window_h - stripe_y_top - stripe_h
			stripe = _LogActivityStripe.alloc().init()
			stripe.setFrame_(NSMakeRect(
				PAD, stripe_y_flipped, stripe_w, stripe_h,
			))
			win._window.contentView().addSubview_(stripe)
			self._log_activity_stripe = stripe
		except (AttributeError, RuntimeError):
			self._log_activity_stripe = None
		# Hint text on first launch so the empty pane doesn't look broken.
		self._log_append('Ready. Pick instances and click Generate.')
		return y + LOG_H + PAD

	def _build_action_bar(self, y):
		"""Bottom row — shortcut hints, spinner+status, Reveal/Cancel/Generate."""
		win = self.w
		PAD = self.PAD

		# Shortcut hints on the left, vertically aligned with the buttons.
		# Action bar interior: buttons sit at y+12 with the Generate button
		# vertically centred against the 32-px footprint.
		# v1.2.15: expanded the hint string with ⇥ (Tab) navigation and ␣
		# (Space) toggle after the Accessibility Engineer flagged that
		# keyboard discovery was undocumented — the bulk-select chords
		# were listed but the everyday Tab/Space pattern was not.
		win.shortcutHints = vanilla.TextBox(
			(PAD, y + 18, 540, 18),
			'⌘A All   ⌘D None   ⌘I Invert   ⇥ Navigate   ␣ Toggle   ⏎ Generate',
			sizeStyle='small',
			selectable=False,
		)
		try:
			cell = win.shortcutHints._nsObject.cell()
			# v1.2.12: bumped from tertiaryLabelColor → secondaryLabelColor
			# after a multi-designer review flagged the chips as effectively
			# invisible against the dark Glyphs panel. Secondary still reads
			# as subordinate to the primary Generate button but the hints
			# are now actually discoverable.
			cell.setTextColor_(NSColor.secondaryLabelColor())
		except Exception:
			pass

		# Spinner + status — pushed right to clear the wider v1.2.15 hint string.
		win.spinner = vanilla.ProgressSpinner((PAD + 550, y + 20, 16, 16), displayWhenStopped=False)
		try:
			win.spinner.stop()
		except Exception:
			pass

		# Right-anchored buttons
		gen_w = self.GENERATE_W
		gen_h = self.GENERATE_H
		can_w = self.CANCEL_W
		rev_w = self.REVEAL_W
		gap = self.ACTION_GAP

		# Generate — large + primary blue via the system key-equivalent.
		# Vertically centred against the Cancel/Reveal baseline (which sits at
		# y+12 with 24-px height) so the 32-px Generate is at y+8 and shares
		# the same visual centre line.
		gen_y = y + 8
		win.generateButton = vanilla.Button(
			(-PAD - gen_w, gen_y, gen_w, gen_h),
			'Generate',
			callback=self._on_generate,
		)
		win.generateButton.enable(False)
		try:
			win.generateButton._nsObject.setKeyEquivalent_('\r')
		except Exception:
			pass
		try:
			win.generateButton._nsObject.setAccessibilityLabel_(
				'Generate the restricted variable font'
			)
		except (AttributeError, RuntimeError):
			pass

		win.cancelButton = vanilla.Button(
			(-PAD - gen_w - gap - can_w, y + 12, can_w, 24),
			'Cancel',
			callback=self._on_cancel,
		)
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
			(-PAD - gen_w - gap - can_w - gap - rev_w, y + 12, rev_w, 24),
			'Reveal',
			callback=self._on_reveal,
			sizeStyle='small',
		)
		win.revealButton.enable(False)
		win.revealButton.show(False)
		for widget, ax_label in (
			(win.revealButton, 'Reveal the generated font in Finder'),
		):
			try:
				widget._nsObject.setAccessibilityLabel_(ax_label)
			except (AttributeError, RuntimeError):
				pass
			try:
				widget._nsObject.setToolTip_(ax_label)
			except (AttributeError, RuntimeError):
				pass

		# v1.2.9: the redundant status label was removed — the LOG pane above
		# the action bar already shows everything that used to appear here.
		# `_set_status` now writes only to the log.

		return y + 36 + PAD

	# Reserved height of the bottom action bar. Must fit:
	# - the 32-px-tall primary Generate button at y + 8
	# - the 24-px-tall Cancel + Reveal buttons at y + 12
	# - the shortcut-hints text at y + 18
	# - the full-width status label at y + 38 (height 14)
	# 64 leaves a clean bottom margin and keeps error messages on-screen.
	ACTION_BAR_H = 64

	def _compute_window_height(self):
		"""Return the total window height. v1.2.0 fixes the layout — no instance-count math."""
		PAD = self.PAD
		# PAD + zone1 + PAD + zone2 + PAD + zone3 + PAD + log + PAD + action_bar + PAD
		return (
			PAD
			+ self.ZONE1_H + PAD
			+ self.ZONE2_H + PAD
			+ self.ZONE3_H + PAD
			+ self.LOG_H + PAD
			+ self.ACTION_BAR_H + PAD
		)

	# ------------------------------------------------------------------
	# Drag-drop installation
	# ------------------------------------------------------------------

	@objc.python_method
	def _install_drop_handler(self):
		"""Attach a _FontDropView overlay above the file path field."""
		try:
			field = self.w.fontPathField._nsObject
		except (AttributeError, RuntimeError):
			return
		try:
			frame = field.frame()
			view = _FontDropView.alloc().initWithFrame_dialog_(frame, self)
			if view is None:
				return
			superview = field.superview()
			if superview is None:
				return
			superview.addSubview_positioned_relativeTo_(view, 1, field)  # NSWindowAbove = 1
			self._drop_view = view
		except (AttributeError, RuntimeError):
			pass

	# ------------------------------------------------------------------
	# Keyboard shortcut monitor
	# ------------------------------------------------------------------

	@objc.python_method
	def _install_shortcut_monitor(self):
		"""Register an NSEvent local key-down monitor for ⌘A / ⌘D / ⌘I.

		Return is already wired through generateButton's key-equivalent; Escape
		is wired through cancelButton's. We add the bulk-select shortcuts here
		because vanilla doesn't expose a key-equivalent API for small buttons.
		"""
		dialog_ref = self

		def _handler(event):
			try:
				# Ignore events not targeting our window.
				nswin = (
					dialog_ref.w.getNSWindow()
					if hasattr(dialog_ref.w, 'getNSWindow') else dialog_ref.w._window
				)
				if nswin is None or event.window() is not nswin:
					return event
				if not (event.modifierFlags() & NSCommandKeyMask):
					return event
				chars = event.charactersIgnoringModifiers()
				if not chars:
					return event
				ch = chars.lower()
				if ch == 'a':
					dialog_ref._on_select_all(None)
					return None
				if ch == 'd':
					dialog_ref._on_select_none(None)
					return None
				if ch == 'i':
					dialog_ref._on_select_invert(None)
					return None
			except Exception:
				pass
			return event

		try:
			self._shortcut_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
				NSEventMaskKeyDown, _handler,
			)
		except (AttributeError, RuntimeError):
			self._shortcut_monitor = None

	@objc.python_method
	def _remove_shortcut_monitor(self):
		"""Tear down the key-down monitor so the dialog can be GC'd."""
		mon = self._shortcut_monitor
		self._shortcut_monitor = None
		if mon is None:
			return
		try:
			NSEvent.removeMonitor_(mon)
		except (AttributeError, RuntimeError):
			pass

	# ------------------------------------------------------------------
	# Event handlers — source picker
	# ------------------------------------------------------------------

	def _on_browse(self, sender):
		"""Open a file-picker for the user to select a variable font."""
		panel = NSOpenPanel.openPanel()
		panel.setCanChooseFiles_(True)
		panel.setCanChooseDirectories_(False)
		panel.setAllowsMultipleSelection_(False)
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
			panel.setAllowedFileTypes_(['ttf', 'otf', 'woff', 'woff2'])
		panel.setTitle_('Select a Variable Font')
		panel.setPrompt_('Select')
		result = panel.runModal()
		if result == NSModalResponseOK:
			path = panel.URL().path()
			self._load_font(path)

	@objc.python_method
	def _refresh_gsfont_popup(self) -> None:
		"""Repopulate the open-font popup from the current Glyphs.fonts list."""
		fonts = list_open_glyphs_fonts() if is_glyphs_app_available() else []
		self._gsfont_options = fonts
		if not fonts:
			items = [self._GSFONT_POPUP_EMPTY]
		else:
			items = [self._GSFONT_POPUP_EMPTY] + [gsfont_label(f) for f in fonts]
		try:
			self.w.gsfontPopup.setItems(items)
			self.w.gsfontPopup.set(0)
		except Exception:
			pass

	def _on_gsfont_chosen(self, sender: Any) -> None:
		"""Handle a user selection from the open-Glyphs-font popup."""
		idx = self.w.gsfontPopup.get()
		if idx <= 0 or idx - 1 >= len(self._gsfont_options):
			return
		gsfont = self._gsfont_options[idx - 1]
		self._load_gsfont(gsfont)

	@objc.python_method
	def _set_source_mode_ui(self, mode: str) -> None:
		"""Show the file row OR the gsfont popup, depending on ``mode``."""
		is_file = (mode == self.SOURCE_FILE)
		try:
			self.w.fontPathField.show(is_file)
			self.w.browseButton.show(is_file)
			self.w.gsfontPopup.show(not is_file)
			# Keep drop overlay in sync with the path field visibility.
			if self._drop_view is not None:
				try:
					self._drop_view.setHidden_(not is_file)
				except Exception:
					pass
			self.w.sourceRadio.set(1 if is_file else 0)
		except (AttributeError, RuntimeError):
			pass
		self._source_mode = mode
		self._refresh_format_popup()

	@objc.python_method
	def _transition_source_mode(
		self, mode: str, *, clear_inactive: bool = True, reset_folder: bool = False,
	) -> None:
		"""Switch ``_source_mode`` to ``mode`` and clear stale cross-source state."""
		self._set_source_mode_ui(mode)
		if clear_inactive:
			if mode == self.SOURCE_FILE:
				self._gsfont = None
				try:
					self.w.gsfontPopup.set(0)
				except (AttributeError, RuntimeError):
					pass
			else:
				self._font_path = None
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
			self._refresh_gsfont_popup()
			if not self._gsfont_options:
				self._set_status(
					'No Glyphs fonts are currently open. Switch back to File or open one.',
				)
				self._transition_source_mode(self.SOURCE_GSFONT)
				return
			self._transition_source_mode(self.SOURCE_GSFONT)
			self._auto_select_frontmost_gsfont()
		else:
			self._transition_source_mode(self.SOURCE_FILE)
			self._set_status('')

	def _on_cancel(self, sender):
		"""Signal cancellation to any in-flight worker, then close the dialog."""
		self._cancelled = True
		self._remove_shortcut_monitor()
		# Stop the animated preview timer so it doesn't keep a dead-view ref.
		pv = getattr(self, '_preview_view', None)
		if pv is not None:
			try:
				pv.stopAnimating()
			except (AttributeError, RuntimeError):
				pass
		# Unregister + delete any temp preview fonts so they don't leak
		# into other AppKit processes' font namespaces.
		try:
			cleanup_all_temp_paths()
		except Exception:  # noqa: BLE001
			pass
		try:
			self.w.close()
		except (AttributeError, RuntimeError):
			pass

	@objc.python_method
	def _auto_select_frontmost_gsfont(self) -> None:
		"""Default to the frontmost open Glyphs font when the dialog opens."""
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
			idx = self._gsfont_options.index(target) + 1
			self.w.gsfontPopup.set(idx)
		except (ValueError, AttributeError, RuntimeError):
			pass
		self._load_gsfont(target)

	@objc.python_method
	def _refresh_format_popup(self) -> None:
		"""Set the Format popup items + canonical default for the active source mode."""
		if self._source_mode == self.SOURCE_GSFONT:
			items = list(self.GSFONT_FORMATS)
			default_idx = 0
		else:
			items = list(self.BINARY_FORMATS)
			default_idx = 0
		try:
			self.w.formatPopup.setItems(items)
			self.w.formatPopup.set(default_idx)
		except Exception:
			pass
		self._refresh_format_description()

	@objc.python_method
	def _refresh_format_description(self):
		"""Update the format description text beside the popup."""
		try:
			fmt = self._resolve_selected_format()
			self.w.formatDescription.set(FORMAT_DESCRIPTIONS.get(fmt, ''))
		except (AttributeError, RuntimeError):
			pass

	def _on_format_changed(self, sender):
		"""User picked a new format — refresh description and preview name."""
		self._refresh_format_description()
		# Preview name shows the extension, so refresh too.
		self._refresh_preview()

	@objc.python_method
	def _load_gsfont(self, gsfont: Any) -> None:
		"""Switch the dialog to GSFont source mode and populate instance list."""
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

		self._transition_source_mode(self.SOURCE_GSFONT)
		self._gsfont = gsfont
		coords = self._extract_gsfont_instance_coords(gsfont, names)
		axis_ranges = self._extract_gsfont_axis_ranges(gsfont)
		self._fvar_axis_ranges = axis_ranges
		self._source_size_bytes = None  # unknown for open Glyphs font
		self._load_instances(names, coords)
		self._name_overridden = False
		self._refresh_name()
		self._refresh_preview()
		self._refresh_generate_button()
		self._set_status('')
		# Kick off a background compile so the animated preview eventually
		# switches from the system font fallback to the user's real glyphs.
		self._setup_preview_font_for_gsfont_source(gsfont)

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
			folder = panel.URL().path()
			self.w.folderField.set(folder)
			self._push_recent_folder(folder)

	def _on_name_edited(self, sender):
		"""Validate the edited name and stop auto-fill once the user touches it."""
		current = self.w.nameField.get()
		if current.strip():
			self._name_overridden = True
		err = validate_output_name(current)
		# Surface inline via a tooltip on the field; success clears any prior tip.
		try:
			ns = self.w.nameField._nsObject
			if err:
				ns.setToolTip_(err)
			else:
				ns.setToolTip_('')
		except (AttributeError, RuntimeError):
			pass
		# Show validation errors in the status bar so they're never missed.
		if err and current.strip():
			self._set_status(err, error=True)
		else:
			# v1.2.9: status label removed; the log pane keeps the full
			# history so there's nothing to selectively clear here.
			pass
		# Mirror live into the preview text.
		self._refresh_preview()

	# ------------------------------------------------------------------
	# Instance list — vanilla.List edit/selection handlers
	# ------------------------------------------------------------------

	def _on_list_edit(self, sender):
		"""vanilla.List editCallback — sync model with the user's checkbox edit."""
		if self._suspend_list_edit_cb:
			return
		try:
			items = sender.get()
		except (AttributeError, RuntimeError):
			items = []
		# Each row in items maps via _visible_to_full to a full-list index.
		# Track which rows changed so we can fire the v1.2.13 highlight on
		# the matching dot in the hull plot.
		changed_full_idx = None
		for row_idx, item in enumerate(items):
			if row_idx >= len(self._visible_to_full):
				continue
			full_idx = self._visible_to_full[row_idx]
			try:
				new = bool(item.get('checked'))
				if self._instance_checked[full_idx] != new:
					self._instance_checked[full_idx] = new
					changed_full_idx = full_idx
			except (IndexError, AttributeError):
				pass
		selected = self._selected_instance_names()
		self._refresh_name(selected=selected)
		self._refresh_preview(selected=selected)
		self._refresh_generate_button(selected=selected)
		self._refresh_selection_count(selected=selected)
		# v1.2.13: live-toggle highlight on the changed dot.
		if changed_full_idx is not None:
			try:
				if self._hull_plot_view is not None:
					self._hull_plot_view.setRecentlyToggled_(changed_full_idx)
			except (AttributeError, RuntimeError):
				pass

	def _on_list_selection(self, sender):
		"""vanilla.List selectionCallback — currently unused but reserved."""
		# Adjacent-select would consume sender.getSelection() here. Left as a
		# stub so the wiring is in place when the feature lands.
		return

	def _on_filter_changed(self, sender):
		"""SearchBox / EditText callback — update filter and rebuild the list."""
		try:
			self._instance_filter = (sender.get() or '').strip()
		except (AttributeError, RuntimeError):
			self._instance_filter = ''
		self._refresh_list()

	def _on_more_select(self, sender):
		"""More-select dropdown — fan out to Italic / Roman helpers."""
		try:
			idx = sender.get()
		except (AttributeError, RuntimeError):
			idx = 0
		if idx == 1:
			self._select_indices(self._italic_indices())
		elif idx == 2:
			self._select_indices(self._roman_indices())
		# Always reset back to the header label so the popup keeps its "menu"
		# affordance — the picked option is a one-shot action.
		try:
			sender.set(0)
		except (AttributeError, RuntimeError):
			pass

	# ------------------------------------------------------------------
	# Instance model
	# ------------------------------------------------------------------

	@objc.python_method
	def _load_instances(self, names: List[str], coords: List[Dict[str, float]]) -> None:
		"""Populate the model + list view from a freshly loaded source."""
		self._instance_names = list(names)
		# coords may be shorter than names if extraction partially failed — pad
		# with empty dicts so all parallel lookups stay safe.
		coords_padded = list(coords) + [{} for _ in range(len(names) - len(coords))]
		self._instance_coords = coords_padded[:len(names)]
		self._instance_checked = [False] * len(names)
		self._instance_filter = ''
		# Surface the bulk-select helpers + filter now that we have data.
		for widget in (
			self.w.allBtn, self.w.noneBtn, self.w.invertBtn,
			self.w.moreSelectBtn, self.w.filterBox,
		):
			try:
				widget.show(True)
			except (AttributeError, RuntimeError):
				pass
		# Clear filter UI back to empty when reloading a new source.
		try:
			self.w.filterBox.set('')
		except (AttributeError, RuntimeError):
			pass
		self._refresh_list()
		self._refresh_selection_count()

	@objc.python_method
	def _build_list_items(self) -> List[dict]:
		"""Return the row dicts visible to the user (after filtering).

		v1.2.12 alignment fix: the display string pads the instance name out
		to the widest name in the *full* font (so filtered subsets still
		line up against the rest) before appending the axis values. Paired
		with the monospaced font installed on the display column in
		_build_zone_dashboard, this makes the wght/opsz values land on a
		consistent x-coordinate so a font engineer can scan a column of
		weights instead of zigzagging across ragged row endings.
		"""
		needle = (self._instance_filter or '').lower()
		self._visible_to_full = []
		# Compute pad width from the *full* set so filtered rows still
		# align against the source-of-truth.
		max_name_len = max(
			(len(n) for n in self._instance_names), default=0,
		)
		pad_to = max_name_len + 2  # two-space gutter before values
		items = []
		for full_idx, name in enumerate(self._instance_names):
			if needle and needle not in name.lower():
				continue
			self._visible_to_full.append(full_idx)
			coord = self._instance_coords[full_idx] if full_idx < len(self._instance_coords) else {}
			coord_summary = '  '.join(f'{tag}={val:g}' for tag, val in coord.items())
			if coord_summary:
				display = f'{name.ljust(pad_to)}{coord_summary}'
			else:
				display = name
			items.append({
				'checked': self._instance_checked[full_idx],
				'display': display,
			})
		return items

	@objc.python_method
	def _refresh_list(self) -> None:
		"""Rebuild the vanilla.List item set, preserving the suspend guard."""
		if not getattr(self, '_using_vanilla_list', False):
			return
		items = self._build_list_items()
		self._suspend_list_edit_cb = True
		try:
			self.w.instanceList.set(items)
		except (AttributeError, RuntimeError):
			pass
		finally:
			self._suspend_list_edit_cb = False

	@objc.python_method
	def _selected_instance_names(self) -> List[str]:
		"""Return names of instances whose row is checked."""
		out = []
		for name, checked in zip(self._instance_names, self._instance_checked):
			if checked:
				out.append(name)
		return out

	@objc.python_method
	def _selected_instance_indices(self) -> List[int]:
		"""Return full-list indices of every currently-checked instance."""
		return [i for i, v in enumerate(self._instance_checked) if v]

	@objc.python_method
	def _toggle_instance_at_index(self, idx: int) -> None:
		"""Flip the checked state at ``idx`` and refresh dependent UI.

		Used as the click callback from the interactive hull plot — when
		the user clicks an instance dot in the plot, this is the route the
		click takes back into the dialog's selection state. Safe to call
		with out-of-range indices (no-op).
		"""
		try:
			idx = int(idx)
		except (TypeError, ValueError):
			return
		if not (0 <= idx < len(self._instance_checked)):
			return
		self._instance_checked[idx] = not self._instance_checked[idx]
		self._refresh_list()
		selected = self._selected_instance_names()
		self._refresh_name(selected=selected)
		self._refresh_preview(selected=selected)
		self._refresh_generate_button(selected=selected)
		self._refresh_selection_count(selected=selected)
		# v1.2.13: fire the live-toggle highlight on the hull plot so the
		# user gets a brief visual confirmation that the click landed.
		try:
			if self._hull_plot_view is not None:
				self._hull_plot_view.setRecentlyToggled_(idx)
		except (AttributeError, RuntimeError):
			pass

	@objc.python_method
	def _set_all_checks(self, value: bool) -> None:
		"""Tick or untick every named instance (visible or not) and refresh."""
		self._instance_checked = [bool(value)] * len(self._instance_names)
		self._refresh_list()
		selected = self._selected_instance_names()
		self._refresh_name(selected=selected)
		self._refresh_preview(selected=selected)
		self._refresh_generate_button(selected=selected)
		self._refresh_selection_count(selected=selected)

	def _on_select_all(self, sender):
		"""Tick every named-instance row."""
		self._set_all_checks(True)

	def _on_select_none(self, sender):
		"""Untick every named-instance row."""
		self._set_all_checks(False)

	def _on_select_invert(self, sender):
		"""Invert every named-instance row's check state."""
		self._instance_checked = [not v for v in self._instance_checked]
		self._refresh_list()
		selected = self._selected_instance_names()
		self._refresh_name(selected=selected)
		self._refresh_preview(selected=selected)
		self._refresh_generate_button(selected=selected)
		self._refresh_selection_count(selected=selected)

	@objc.python_method
	def _select_indices(self, indices: List[int]) -> None:
		"""Tick exactly the given full-list indices and refresh."""
		self._instance_checked = [False] * len(self._instance_names)
		for i in indices:
			if 0 <= i < len(self._instance_checked):
				self._instance_checked[i] = True
		self._refresh_list()
		selected = self._selected_instance_names()
		self._refresh_name(selected=selected)
		self._refresh_preview(selected=selected)
		self._refresh_generate_button(selected=selected)
		self._refresh_selection_count(selected=selected)

	@objc.python_method
	def _italic_indices(self) -> List[int]:
		"""Indices of instances that look italic (by name OR coord)."""
		out = []
		pattern = re.compile(r'\b(italic|oblique|slanted)\b', re.IGNORECASE)
		for i, name in enumerate(self._instance_names):
			coord = self._instance_coords[i] if i < len(self._instance_coords) else {}
			slnt = coord.get('slnt', 0)
			ital = coord.get('ital', 0)
			if pattern.search(name) or slnt < 0 or ital >= 0.5:
				out.append(i)
		return out

	@objc.python_method
	def _roman_indices(self) -> List[int]:
		"""Complement of italic — indices of upright/roman instances."""
		italics = set(self._italic_indices())
		return [i for i in range(len(self._instance_names)) if i not in italics]

	@objc.python_method
	def _refresh_selection_count(self, selected=None):
		"""Update the instance header with an 'N of M' count of selected rows."""
		if not self._instance_names:
			try:
				self.w.instanceHeader.set('INSTANCES (0 OF 0)')
			except (AttributeError, RuntimeError):
				pass
			return
		if selected is None:
			selected = self._selected_instance_names()
		count = len(selected)
		total = len(self._instance_names)
		try:
			self.w.instanceHeader.set(f'INSTANCES ({count} OF {total})')
		except (AttributeError, RuntimeError):
			pass

	# ------------------------------------------------------------------
	# Reveal / format / status
	# ------------------------------------------------------------------

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
		"""Read the Format popup and normalise the label to an internal token."""
		try:
			fmt_items = self.w.formatPopup.getItems()
			fmt_index = self.w.formatPopup.get()
		except (AttributeError, RuntimeError):
			return 'TTF'
		fmt_label = fmt_items[fmt_index] if fmt_items else 'TTF'
		return 'GLYPHS' if fmt_label == self.GSFONT_FORMAT_LABEL else fmt_label

	@objc.python_method
	def _validate_brotli_for_format(self, fmt):
		"""Return an error message for WOFF2 output without brotli, else None."""
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

		TODO(v1.3.0): multi-output. The npm package supports an
		``outputs: [{name, instances}, …]`` array. Bringing that to the
		Glyphs UI requires duplicating Zone 2 per output, which is larger
		than every other v1.2.0 change combined. v1.2.0 ships the
		zone restructure so v1.3.0 can swap the dashboard for a tabbed
		variant without redoing zones 1 or 3. See the redesign plan.
		"""
		selected = self._selected_instance_names()
		if not selected:
			return None, 'Select at least one named instance.'

		family_name = self.w.nameField.get().strip()
		if not family_name:
			return None, 'Enter an output name.'
		name_err = validate_output_name(family_name)
		if name_err is not None:
			return None, name_err

		fmt = self._resolve_selected_format()
		ext = extension_for_format(fmt)

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
		"""Validate dialog input, then dispatch to the right source-path generator."""
		params, err = self._collect_generate_inputs()
		if err is not None:
			self._set_status(err, error=True)
			return
		# Bump the output folder into the MRU as soon as we know we'll write to it.
		try:
			folder = str(Path(params['output_path']).parent)
			self._push_recent_folder(folder)
		except Exception:
			pass

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
		"""Return a sensible default output folder when the user has not chosen one."""
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
		"""Spin up the shared 'Generate is in flight' UI state."""
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
		"""File-source path — uses fontTools instancer in a worker thread."""
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
		"""Open-font-source path — runs on the main thread (Glyphs APIs require it)."""
		gsfont = self._gsfont
		if gsfont is None:
			self._set_status('No open Glyphs font selected.', error=True)
			return

		self._begin_generate_ui()
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
		if not self._alive():
			return
		self._last_output_path = path
		self.w.revealButton.enable(True)
		try:
			self.w.revealButton.show(True)
		except Exception:
			pass
		display = path.replace(os.path.expanduser('~'), '~', 1)
		self._set_status(f'Saved: {display}')
		self.w.generateButton.enable(True)
		try:
			self.w.spinner.stop()
		except Exception:
			pass
		# Honour the "Open after generating" checkbox.
		if getattr(self, '_open_after_save', False):
			self._open_path_after_save(path)

	@objc.python_method
	def _on_generate_failure(self, message):
		"""Main-thread handler invoked after a generate failure."""
		if not self._alive():
			return
		# Always log the full traceback to stderr so it reaches the Glyphs
		# Macro Panel — the in-dialog status row truncates long messages.
		try:
			print(f'[vf-clamp generate failure] {message}', file=sys.stderr)
			traceback.print_exc(file=sys.stderr)
		except Exception:  # noqa: BLE001
			pass
		scrubbed = message.replace(os.path.expanduser('~'), '~')
		scrubbed = scrubbed.replace('/var/folders/', '/<tmp>/').replace('/private/var/folders/', '/<tmp>/')
		# v1.2.9: status label is gone; the log pane shows the full
		# message and is selectable so users can copy it directly.
		self._set_status(scrubbed, error=True)
		self.w.generateButton.enable(True)
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
		"""Return True if the dialog's underlying NSWindow is still around."""
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
		"""Parse the font at path, populate the list, and refresh UI."""
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

		self._transition_source_mode(self.SOURCE_FILE)
		self._font_path = path

		# Source file size — drives the size-estimate heuristic.
		try:
			self._source_size_bytes = os.path.getsize(path)
		except OSError:
			self._source_size_bytes = None

		self._close_cached_font()
		try:
			self._cached_font = open_font_safely(path)
		except Exception:
			self._cached_font = None
		self.w.fontPathField.set(path)
		self._set_status('')

		# Extract per-instance coords + axis ranges for the dashboard.
		coords = self._extract_ttfont_instance_coords(self._cached_font, names)
		self._fvar_axis_ranges = self._extract_ttfont_axis_ranges(self._cached_font)
		self._load_instances(names, coords)

		if not self.w.folderField.get().strip():
			self.w.folderField.set(str(Path(path).parent))

		self._name_overridden = False
		self._refresh_name()
		self._refresh_preview()
		self._refresh_generate_button()
		# Register the file with CTFontManager so the animated preview
		# renders with the actual source font's glyphs instead of the
		# system fallback.
		self._setup_preview_font_for_file_source(path)

	@objc.python_method
	def _close_cached_font(self):
		"""Release the cached TTFont so the next ``_load_font`` reads fresh."""
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

	# ------------------------------------------------------------------
	# Axis / instance coord extraction
	# ------------------------------------------------------------------

	@objc.python_method
	def _extract_ttfont_instance_coords(self, font, names):
		"""Return parallel list of per-instance coord dicts for fvar instances."""
		coords = [{} for _ in names]
		if font is None:
			return coords
		try:
			fvar = font['fvar']
			name_table = font['name']
		except Exception:
			return coords
		# Re-use core's disambiguation so labels match.
		try:
			from core import _disambiguated_instance_labels
		except Exception:
			return coords
		name_to_coord = {}
		try:
			for inst, label in _disambiguated_instance_labels(name_table, fvar.instances):
				name_to_coord[label] = dict(inst.coordinates)
		except Exception:
			return coords
		for i, n in enumerate(names):
			coords[i] = name_to_coord.get(n, {})
		return coords

	@objc.python_method
	def _extract_ttfont_axis_ranges(self, font):
		"""Return ``{tag: (min, default, max)}`` from an fvar table, or {}."""
		if font is None:
			return {}
		try:
			fvar = font['fvar']
		except Exception:
			return {}
		out = {}
		for ax in fvar.axes:
			try:
				out[ax.axisTag] = (
					float(ax.minValue), float(ax.defaultValue), float(ax.maxValue),
				)
			except (AttributeError, ValueError):
				continue
		return out

	@objc.python_method
	def _extract_gsfont_instance_coords(self, gsfont, names):
		"""Return parallel list of per-instance coord dicts for GSInstances."""
		coords = [{} for _ in names]
		try:
			axis_tags = [getattr(ax, 'axisTag', '') or '' for ax in gsfont.axes]
		except Exception:
			return coords
		name_to_coord = {}
		try:
			for inst in gsfont.instances:
				# Skip Variable Font Settings — they aren't picker rows.
				try:
					from gsfont_core import _is_variable_instance
					if _is_variable_instance(inst):
						continue
				except Exception:
					pass
				name = (inst.name or '').strip()
				if not name:
					continue
				try:
					vals = list(inst.axes)
				except Exception:
					vals = []
				d = {}
				for tag, val in zip(axis_tags, vals):
					if tag:
						try:
							d[tag] = float(val)
						except (TypeError, ValueError):
							continue
				name_to_coord[name] = d
		except Exception:
			return coords
		for i, n in enumerate(names):
			coords[i] = name_to_coord.get(n, {})
		return coords

	@objc.python_method
	def _extract_gsfont_axis_ranges(self, gsfont):
		"""Return ``{tag: (min, default, max)}`` derived from GSInstance coords.

		GSFont's axes don't expose min/max directly the way fvar does — we
		approximate the design space from the min/max of static instance
		coordinates on each axis. This is good enough for the hull plot
		because the hull is always a subset of that range.
		"""
		out = {}
		try:
			axis_tags = [getattr(ax, 'axisTag', '') or '' for ax in gsfont.axes]
		except Exception:
			return out
		try:
			from gsfont_core import _is_variable_instance
		except Exception:
			_is_variable_instance = lambda inst: False  # noqa: E731
		bounds = {}
		try:
			for inst in gsfont.instances:
				if _is_variable_instance(inst):
					continue
				try:
					vals = list(inst.axes)
				except Exception:
					continue
				for tag, val in zip(axis_tags, vals):
					if not tag:
						continue
					try:
						v = float(val)
					except (TypeError, ValueError):
						continue
					if tag not in bounds:
						bounds[tag] = [v, v]
					else:
						bounds[tag][0] = min(bounds[tag][0], v)
						bounds[tag][1] = max(bounds[tag][1], v)
		except Exception:
			return out
		for tag, (lo, hi) in bounds.items():
			default = lo if lo == hi else (lo + hi) / 2
			out[tag] = (lo, default, hi)
		return out

	# ------------------------------------------------------------------
	# Output preview helpers (preview name, hull plot, size estimate)
	# ------------------------------------------------------------------

	@objc.python_method
	def _refresh_name(self, selected=None):
		"""Auto-compute the output name unless the user has overridden it."""
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
		# Fall back to the open GSFont's family name when there's no file path.
		if not base and self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None:
			try:
				base = self._gsfont.familyName or ''
			except Exception:
				base = ''
		computed = compute_default_output_name(base, selected[0], selected[-1])
		self.w.nameField.set(computed.strip())

	@objc.python_method
	def _refresh_preview(self, selected=None):
		"""Update the preview name, hull plot, and size estimate."""
		# Preview name — show the live nameField value with the active extension.
		try:
			current_name = self.w.nameField.get().strip()
		except (AttributeError, RuntimeError):
			current_name = ''
		try:
			fmt = self._resolve_selected_format()
			ext = extension_for_format(fmt)
		except Exception:
			ext = 'ttf'
		display = f'{current_name}.{ext}' if current_name else '(set output name)'
		try:
			self.w.previewName.set(display)
		except (AttributeError, RuntimeError):
			pass

		if selected is None:
			selected = self._selected_instance_names()
		hull = self._compute_current_hull(selected)
		self._render_hull(hull)
		self._refresh_size_estimate(selected)
		self._refresh_animated_preview(hull)

	@objc.python_method
	def _refresh_animated_preview(self, hull):
		"""Drive the HOHO Anes specimen view: feed it the hull + animate."""
		view = getattr(self, '_preview_view', None)
		if view is None:
			return
		try:
			if hull:
				view.setHull_(hull)
				view.startAnimating()
			else:
				view.setHull_({})
				view.stopAnimating()
		except (AttributeError, RuntimeError):
			pass

	# ------------------------------------------------------------------
	# Preview font registration — swaps the system fallback for the real
	# source font so "HOHO Anes" renders with the user's actual glyphs.
	# ------------------------------------------------------------------

	@objc.python_method
	def _setup_preview_font_for_file_source(self, path):
		"""Register a file-source TTF/OTF and push its descriptor to the preview."""
		view = getattr(self, '_preview_view', None)
		if view is None or not font_registration_available() or not path:
			return
		try:
			ok, descriptor = register_font_at_path(path)
			if ok and descriptor is not None:
				try:
					view.setFontDescriptor_(descriptor)
				except (AttributeError, RuntimeError):
					pass
		except Exception:  # noqa: BLE001
			traceback.print_exc()

	@objc.python_method
	def _setup_preview_font_for_gsfont_source(self, gsfont):
		"""Kick off a background export + registration for an Open Font source.

		Compiles a temp variable TTF, registers it, and pushes its descriptor
		to the preview view when ready. Uses a monotonic token so that if the
		user changes source fonts during the export, the stale callback is
		ignored instead of overwriting the new font's preview.
		"""
		view = getattr(self, '_preview_view', None)
		if view is None or not font_registration_available() or gsfont is None:
			return

		# Bump the token — any in-flight export from a previous source is now stale.
		self._preview_token = getattr(self, '_preview_token', 0) + 1
		token = self._preview_token

		# Quick visual signal that something is happening; the system font
		# keeps animating in the meantime.
		try:
			view.setNeedsDisplay_(True)
		except (AttributeError, RuntimeError):
			pass

		dialog_ref = self

		def _on_complete(temp_path, descriptor):
			"""Worker-thread callback — marshalled back to the main thread."""
			def _apply():
				# Stale callback (user already moved on) — drop on the floor.
				if getattr(dialog_ref, '_preview_token', 0) != token:
					return
				v = getattr(dialog_ref, '_preview_view', None)
				if v is None or descriptor is None:
					return
				try:
					v.setFontDescriptor_(descriptor)
				except (AttributeError, RuntimeError):
					pass
			try:
				AppHelper.callAfter(_apply)
			except (AttributeError, RuntimeError):
				# Last resort: try in-thread (will probably succeed since
				# AppKit calls from background threads sometimes work for
				# simple state mutations).
				_apply()

		export_gsfont_to_temp_vf_async(gsfont, _on_complete)

	@objc.python_method
	def _compute_current_hull(self, selected):
		"""Return the per-axis (lo, hi) hull for the current selection."""
		if not selected:
			return {}
		try:
			if self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None:
				return compute_gsfont_hull(self._gsfont, selected)
			if self._font_path:
				cached = getattr(self, '_cached_font', None)
				if cached is not None:
					return get_axis_hull_from_instances(cached, selected)
				import contextlib as _cl
				with _cl.closing(open_font_safely(self._font_path)) as f:
					return get_axis_hull_from_instances(f, selected)
		except Exception:
			return {}
		return {}

	@objc.python_method
	def _render_hull(self, hull):
		"""Show the custom plot for 1-2 axes; fall back to chips otherwise."""
		# Default: chips visible, plot hidden.
		use_plot = (
			self._hull_plot_view is not None
			and hull
			and 1 <= len(hull) <= 2
		)
		if use_plot:
			try:
				self.w.hullChips.show(False)
			except (AttributeError, RuntimeError):
				pass
			axis_colors = {tag: _rgb_for_axis(tag) for tag in hull.keys()}
			try:
				self._hull_plot_view.setHull_axisRanges_axisColors_(
					hull, self._fvar_axis_ranges, axis_colors,
				)
				# v1.2.9 interactive plot: feed it the per-instance coords +
				# current selection mask + a callback that toggles whichever
				# instance the user clicks.
				try:
					selected_indices = set(self._selected_instance_indices())
				except Exception:
					selected_indices = set()
				try:
					self._hull_plot_view.setInstances_selectedIndices_onClick_(
						self._instance_coords,
						selected_indices,
						self._toggle_instance_at_index,
					)
				except (AttributeError, RuntimeError):
					pass
				self._hull_plot_view.setHidden_(False)
			except Exception:
				# Plot failed for some reason — fall back to chips below.
				use_plot = False

		if not use_plot:
			try:
				if self._hull_plot_view is not None:
					self._hull_plot_view.setHidden_(True)
			except Exception:
				pass
			try:
				self.w.hullChips.show(True)
			except (AttributeError, RuntimeError):
				pass
			self._set_chips_text(hull)

	@objc.python_method
	def _set_chips_text(self, hull):
		"""Render the axis hull as colored ■-prefixed chips in win.hullChips."""
		if not hull:
			self._set_chips_placeholder('(select instances to preview)')
			return

		attr = NSMutableAttributedString.alloc().init()
		small_font = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize())
		mono_font = NSFont.monospacedDigitSystemFontOfSize_weight_(
			NSFont.smallSystemFontSize(),
			0.0,
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
			attr.appendAttributedString_(
				NSAttributedString.alloc().initWithString_attributes_(
					'■  ',
					{
						NSForegroundColorAttributeName: _nscolor_for_axis(tag),
						NSFontAttributeName: small_font,
					},
				)
			)
			attr.appendAttributedString_(
				NSAttributedString.alloc().initWithString_attributes_(
					f'{tag}',
					{
						NSForegroundColorAttributeName: label,
						NSFontAttributeName: small_font,
					},
				)
			)
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
			self.w.hullChips._nsObject.setAttributedStringValue_(attr)
		except Exception:
			parts = []
			for tag, (lo, hi) in hull.items():
				a = f'{lo:g}'
				b = f'{hi:g}'
				parts.append(f'{tag} {a}' if a == b else f'{tag} {a}–{b}')
			self.w.hullChips.set('  ·  '.join(parts))

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
			self.w.hullChips._nsObject.setAccessibilityLabel_('Axis ranges')
			self.w.hullChips._nsObject.setAccessibilityValue_(ax_summary)
		except (AttributeError, RuntimeError):
			pass

	@objc.python_method
	def _set_chips_placeholder(self, text):
		"""Render the muted placeholder text inside the chips area."""
		try:
			attr = NSAttributedString.alloc().initWithString_attributes_(
				text,
				{
					NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
					NSFontAttributeName: NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()),
				},
			)
			self.w.hullChips._nsObject.setAttributedStringValue_(attr)
		except Exception:
			self.w.hullChips.set(text)

	@objc.python_method
	def _refresh_size_estimate(self, selected):
		"""Update the size-estimate line beside the hull plot.

		v1.2.17 enriches the line with the structural counts that actually
		drive file size: number of masters retained in the clamp, number of
		axes in the output design space, and how many of those axes pinned
		to a single value (no longer variable). Without this, "≈ 38 KB"
		looked like a hand-wave; with it, the user can see that the size
		falls because masters are dropped and axes are pinned.
		"""
		if not selected:
			text = ''
		else:
			n = len(selected)
			total = max(1, len(self._instance_names))
			parts = [f'{n} instance{"s" if n != 1 else ""}']

			# Size heuristic — only applies for File sources where we have
			# a source byte count to scale.
			if self._source_size_bytes is not None:
				ratio = max(0.3, min(1.0, n / total))
				size_kb = int(self._source_size_bytes * ratio / 1024)
				parts.insert(0, f'~{size_kb:,} KB')

			# Structural counts: surviving masters + axis count + pinned.
			masters, axes, pinned = self._count_structural(selected)
			if masters is not None:
				parts.append(f'{masters} master{"s" if masters != 1 else ""}')
			if axes is not None and axes > 0:
				if pinned:
					parts.append(f'{axes} ax · {pinned} pinned')
				else:
					parts.append(f'{axes} ax')

			text = '  ·  '.join(parts)
		try:
			self.w.sizeEstimate.set(text)
		except (AttributeError, RuntimeError):
			pass

	@objc.python_method
	def _count_structural(self, selected):
		"""Return (surviving_masters, axis_count, pinned_axes) for the hull.

		Best-effort — returns ``(None, None, None)`` if the source isn't
		loaded yet or the counts can't be computed. The dialog formats
		whatever is available, so a partial answer is still useful.
		"""
		try:
			hull = self._compute_current_hull(selected)
		except Exception:
			hull = {}
		if not hull:
			return (None, None, None)

		axes = len(hull)
		pinned = sum(1 for lo, hi in hull.values() if lo == hi)

		# Surviving masters — count masters whose coords fall inside the
		# hull range on every axis. Only computable for GSFont sources;
		# File sources don't expose master geometry through fonttools at
		# this level of abstraction (varLib stores variation deltas, not
		# named masters).
		masters = None
		try:
			if self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None:
				axis_tags = [
					getattr(ax, 'axisTag', '') or '' for ax in self._gsfont.axes
				]
				count = 0
				for master in self._gsfont.masters:
					try:
						coords = list(master.axes) if hasattr(master, 'axes') else []
					except (AttributeError, RuntimeError):
						coords = []
					ok = True
					for tag, val in zip(axis_tags, coords):
						if tag not in hull:
							continue
						lo, hi = hull[tag]
						if not (lo <= float(val) <= hi):
							ok = False
							break
					if ok:
						count += 1
				masters = count
		except Exception:
			masters = None

		return (masters, axes, pinned)

	def _refresh_generate_button(self, selected=None):
		"""Enable Generate only when a source is loaded and >=1 instance selected."""
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
		"""Append a status message to the log pane.

		v1.2.9 dropped the duplicate single-line status label that used to
		sit below the action bar. The LOG pane is now the single source of
		truth for status output; everything else (success, failure, hints)
		flows through here.
		"""
		text = f'Error: {message}' if error else message
		# Don't double-log blanks (e.g. status-clears during normal flow).
		if message:
			self._log_append(text)

	@objc.python_method
	def _log_append(self, message):
		"""Append a line to the scrollable log pane and scroll to the bottom.

		Truncates the log when it exceeds ~5 KB so the pane never grows
		without bound across a long debugging session.

		v1.2.15 also flashes the log activity stripe on the left edge so the
		user gets a peripheral signal that new content arrived.
		"""
		if not message:
			return
		editor = getattr(self.w, 'logEditor', None)
		if editor is None:
			return
		stripe = getattr(self, '_log_activity_stripe', None)
		if stripe is not None:
			try:
				stripe.flash()
			except (AttributeError, RuntimeError):
				pass
		try:
			existing = editor.get() or ''
			if existing and not existing.endswith('\n'):
				existing += '\n'
			combined = (existing + str(message)).rstrip() + '\n'
			# Keep only the trailing ~5 KB so the editor stays snappy.
			if len(combined) > 5120:
				combined = combined[-5120:]
				combined = combined[combined.find('\n') + 1:] if '\n' in combined else combined
			editor.set(combined)
			# Scroll to the bottom via the underlying NSTextView.
			try:
				tv = editor._nsObject.documentView()
				if tv is not None and hasattr(tv, 'scrollRangeToVisible_'):
					from Foundation import NSMakeRange  # type: ignore
					length = len(combined)
					tv.scrollRangeToVisible_(NSMakeRange(length, 0))
			except (AttributeError, RuntimeError, ImportError):
				pass
		except (AttributeError, RuntimeError):
			pass

	def _on_open_after_save_changed(self, sender):
		"""Persist the checkbox state so it survives within the dialog session."""
		try:
			self._open_after_save = bool(sender.get())
		except (AttributeError, RuntimeError):
			self._open_after_save = False

	@objc.python_method
	def _open_path_after_save(self, path):
		"""Open the saved output in Glyphs (for .glyphs) or the default app."""
		if not path or not os.path.exists(path):
			return
		try:
			ext = os.path.splitext(path)[1].lower()
			if ext in ('.glyphs', '.glyphspackage'):
				# Open in Glyphs as a real document tab.
				try:
					from GlyphsApp import Glyphs  # type: ignore
					Glyphs.open(path)
					self._log_append(f'Opened in Glyphs: {path}')
					return
				except (ImportError, AttributeError, RuntimeError):
					pass
			# Fallback: macOS default opener (open command via LaunchServices).
			try:
				from AppKit import NSWorkspace  # type: ignore
				NSWorkspace.sharedWorkspace().openFile_(path)
				self._log_append(f'Opened: {path}')
			except (ImportError, AttributeError, RuntimeError):
				import subprocess
				subprocess.Popen(['/usr/bin/open', path])
				self._log_append(f'Opened via /usr/bin/open: {path}')
		except Exception as e:  # noqa: BLE001
			self._log_append(f'Could not open {path}: {e}')

	# ------------------------------------------------------------------
	# Presets
	# ------------------------------------------------------------------

	@objc.python_method
	def _refresh_presets_popup(self):
		"""Repopulate the preset popup from the in-memory preset store."""
		names = sorted(self._presets.keys(), key=lambda s: s.lower())
		items = [self._PRESET_NONE_LABEL, self._PRESET_SAVE_LABEL, self._PRESET_MANAGE_LABEL]
		items.extend(names)
		try:
			self.w.presetPopup.setItems(items)
			self.w.presetPopup.set(0)
		except (AttributeError, RuntimeError):
			pass

	def _on_preset_chosen(self, sender):
		"""Dispatch the preset popup to Save / Manage / Apply."""
		try:
			idx = sender.get()
			items = sender.getItems()
		except (AttributeError, RuntimeError):
			return
		if idx == 0:
			return
		if idx == 1:
			self._open_save_preset_dialog()
			# Reset to (no preset) so picking 'Save…' again next time works.
			try:
				sender.set(0)
			except (AttributeError, RuntimeError):
				pass
			return
		if idx == 2:
			self._open_manage_presets_dialog()
			try:
				sender.set(0)
			except (AttributeError, RuntimeError):
				pass
			return
		# User-defined preset
		if idx < len(items):
			self._apply_preset(items[idx])

	@objc.python_method
	def _apply_preset(self, name):
		"""Apply a preset by name — select instances, set output name + format."""
		preset = self._presets.get(name)
		if preset is None:
			return
		wanted = set(preset.get('instances', []))
		self._instance_checked = [n in wanted for n in self._instance_names]
		self._refresh_list()
		# Output name: only auto-apply if the user hasn't typed their own.
		fmt = preset.get('format', '')
		if fmt:
			try:
				items = self.w.formatPopup.getItems()
				if fmt in items:
					self.w.formatPopup.set(items.index(fmt))
			except (AttributeError, RuntimeError):
				pass
		# Refresh chain
		selected = self._selected_instance_names()
		self._refresh_name(selected=selected)
		self._refresh_format_description()
		self._refresh_preview(selected=selected)
		self._refresh_generate_button(selected=selected)
		self._refresh_selection_count(selected=selected)

	def _open_save_preset_dialog(self):
		"""Prompt for a preset name and persist the current selection/format."""
		selected = self._selected_instance_names()
		if not selected:
			self._set_status('Select at least one instance before saving a preset.', error=True)
			return
		try:
			fmt = self._resolve_selected_format()
		except Exception:
			fmt = 'TTF'
		# Cheap-and-cheerful prompt — vanilla doesn't expose AskString in every
		# Glyphs build, so we use AppKit's Message + saved-name fallback.
		try:
			from vanilla.dialogs import askString
			name = askString('Save Preset', 'Name this preset:', '')
		except Exception:
			name = None
		if not name:
			return
		name = name.strip()
		if not name:
			return
		self._presets[name] = make_preset(name, selected, fmt)
		try:
			save_presets(self._presets)
		except Exception:
			pass
		self._refresh_presets_popup()
		self._set_status(f'Saved preset "{name}".')

	def _open_manage_presets_dialog(self):
		"""Surface a small manage sheet — delete-only for v1.2.0."""
		if not self._presets:
			self._set_status('No presets saved yet.')
			return
		# Without a full sheet UI, fall back to a quick "delete by name" prompt.
		try:
			from vanilla.dialogs import askString
			name = askString(
				'Manage Presets',
				'Type a preset name to delete (cancel to abort):',
				'',
			)
		except Exception:
			name = None
		if not name:
			return
		name = name.strip()
		if name in self._presets:
			del self._presets[name]
			try:
				save_presets(self._presets)
			except Exception:
				pass
			self._refresh_presets_popup()
			self._set_status(f'Deleted preset "{name}".')
		else:
			self._set_status(f'No preset named "{name}".', error=True)

	# ------------------------------------------------------------------
	# Recent folders
	# ------------------------------------------------------------------

	@objc.python_method
	def _refresh_recents_popup(self):
		"""Repopulate the recent folders popup."""
		home = os.path.expanduser('~')
		items = [self._RECENT_HEADER_LABEL]
		for folder in self._recent_folders[:RECENT_FOLDERS_MAX]:
			# Shorten with ~ for readability.
			short = folder.replace(home, '~', 1) if folder.startswith(home) else folder
			items.append(short)
		try:
			self.w.recentFoldersPopup.setItems(items)
			self.w.recentFoldersPopup.set(0)
		except (AttributeError, RuntimeError):
			pass

	def _on_recent_folder_chosen(self, sender):
		"""Apply a recent-folder pick to the folder field."""
		try:
			idx = sender.get()
		except (AttributeError, RuntimeError):
			return
		if idx <= 0 or idx > len(self._recent_folders):
			return
		folder = self._recent_folders[idx - 1]
		try:
			self.w.folderField.set(folder)
		except (AttributeError, RuntimeError):
			pass
		# Bump to front of MRU.
		self._push_recent_folder(folder)
		try:
			sender.set(0)
		except (AttributeError, RuntimeError):
			pass

	@objc.python_method
	def _push_recent_folder(self, folder):
		"""Move ``folder`` to the front of the MRU and persist."""
		if not folder:
			return
		self._recent_folders = push_recent_folder(self._recent_folders, folder)
		try:
			save_recent_folders(self._recent_folders)
		except Exception:
			pass
		self._refresh_recents_popup()

	# ------------------------------------------------------------------
	# Public
	# ------------------------------------------------------------------

	def show(self):
		"""Bring the dialog window to the front."""
		# Sheet modality would attach via NSWindow.beginSheet_completionHandler_
		# only when the source GSFont has a visible parent window. Falls back to
		# a normal panel so headless gsfonts and the file-source path keep
		# working. v1.2.0 ships the standalone panel; sheet modality is a
		# follow-up. (See plan §6.)
		self.w.open()
