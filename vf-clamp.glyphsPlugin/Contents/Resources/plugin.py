# plugin.py — vf-clamp Glyphs.app plugin shell around core.py.
# Pure UI/registration concerns; all fonttools work lives in core.py.

import os
import sys
import threading
import traceback

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
# golden-angle hue system but tuned for legibility against macOS dark mode
# panels. Values are sRGB 0..1 floats.
# ---------------------------------------------------------------------------

AXIS_COLORS = {
	'wght': (0.40, 0.66, 0.96),   # blue
	'wdth': (0.42, 0.78, 0.52),   # green
	'opsz': (0.66, 0.55, 0.92),   # purple
	'slnt': (0.96, 0.66, 0.42),   # orange
	'ital': (0.96, 0.55, 0.76),   # pink
	'GRAD': (0.95, 0.86, 0.45),   # yellow
}
DEFAULT_AXIS_COLOR = (0.60, 0.60, 0.60)


def _nscolor_for_axis(tag):
	"""Return an NSColor for the small chip that sits next to ``tag`` in the hull preview."""
	rgb = AXIS_COLORS.get(tag, DEFAULT_AXIS_COLOR)
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
		self._instance_names = []   # ordered list from fvar OR gsfont
		self._checks = []           # list of (attr_name, inner_group) tuples
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
		if self._gsfont_options:
			self._set_source_mode_ui(self.SOURCE_GSFONT)
			self._auto_select_frontmost_gsfont()
		else:
			self._set_source_mode_ui(self.SOURCE_FILE)

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
		# Accessibility: VoiceOver reads short labels like 'All' as ambiguous
		# without context. Set explicit accessibility labels that name the
		# scope ("instances") so screen-reader users know what these affect.
		for btn_widget, ax_label in (
			(win.allBtn, 'Select all instances'),
			(win.noneBtn, 'Deselect all instances'),
			(win.invertBtn, 'Invert instance selection'),
		):
			try:
				btn_widget._nsObject.setAccessibilityLabel_(ax_label)
			except (AttributeError, RuntimeError):
				pass
		y += LABEL_H + 6

		# --- Row 4: Instance scroll area (in the control column) -------
		win.instancePlaceholder = vanilla.TextBox(
			(CONTROL_X, y, -PAD, LABEL_H),
			'Open a variable font to see its named instances.',
			sizeStyle='small',
		)
		self._scroll_top_y = y
		self._scroll_height = LABEL_H
		# Seed the ScrollView with an empty placeholder Group; the document
		# view is replaced by _populate_instance_checks when a font loads.
		self._scroll_placeholder_group = vanilla.Group((0, 0, 1, 1))
		win.instanceScroll = vanilla.ScrollView(
			(CONTROL_X, y, -PAD, LABEL_H),
			self._scroll_placeholder_group._nsObject,
			hasHorizontalScroller=False,
			hasVerticalScroller=True,
			autohidesScrollers=True,
		)
		win.instanceScroll.show(False)
		y += LABEL_H + 8

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
		y += ROW + 4

		# --- Row 7: Format ---------------------------------------------
		win.formatLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Format:')
		win.formatPopup = vanilla.PopUpButton(
			(CONTROL_X, y, 160, FIELD_H + 2),
			list(self.BINARY_FORMATS),
		)
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

		win.revealButton = vanilla.Button(
			(-PAD - GEN_W - GAP - CAN_W - GAP - REV_W, y, REV_W, BTN_H),
			'Reveal',
			callback=self._on_reveal,
			sizeStyle='small',
		)
		win.revealButton.enable(False)
		win.revealButton.show(False)  # appears only after a successful generate
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
		y += BTN_H + PAD
		self._static_sections_height = y

	def _compute_window_height(self, n_instances):
		"""Return total window height for ``n_instances`` checkboxes in the scroll area."""
		PAD = self.PAD
		LABEL_H = self.LABEL_H
		ROW = self.ROW
		BTN_H = self.BTN_H
		CHECK_H = self.CHECK_H
		CHECK_GAP = self.CHECK_GAP
		HULL_H = self.HULL_H

		visible_rows = max(1, min(n_instances, 8))
		scroll_h = visible_rows * (CHECK_H + CHECK_GAP) + 8

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
	def _refresh_gsfont_popup(self):
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

	def _on_gsfont_chosen(self, sender):
		"""Handle a user selection from the open-Glyphs-font popup."""
		idx = self.w.gsfontPopup.get()
		# Index 0 is the sentinel "(no open Glyphs fonts)" / "—" entry.
		if idx <= 0 or idx - 1 >= len(self._gsfont_options):
			return
		gsfont = self._gsfont_options[idx - 1]
		self._load_gsfont(gsfont)

	@objc.python_method
	def _set_source_mode_ui(self, mode):
		"""Show the file row OR the gsfont popup, depending on ``mode``.

		Both widget groups occupy the same Y position; only one is visible.
		Also updates the leading "Source-input" label and refreshes the
		format popup so .glyphs appears/disappears appropriately.

		AppKit attribute errors are tolerated here so a partially-built window
		(e.g. during construction before _build_window finishes) cannot crash
		mode transitions.
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
				self._set_source_mode_ui(self.SOURCE_GSFONT)
				# Clear any prior gsfont selection — there's nothing to clamp.
				self._gsfont = None
				return
			self._set_source_mode_ui(self.SOURCE_GSFONT)
			# Auto-select the most likely target so the user doesn't have to
			# poke the popup as a second action.
			self._auto_select_frontmost_gsfont()
		else:  # File
			self._set_source_mode_ui(self.SOURCE_FILE)
			self._gsfont = None
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
	def _auto_select_frontmost_gsfont(self):
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
	def _refresh_format_popup(self):
		"""Adjust the Format popup items to match the active source mode."""
		if self._source_mode == self.SOURCE_GSFONT:
			items = list(self.GSFONT_FORMATS)
			default_idx = 0  # .glyphs is the headline output for an open font
		else:
			items = list(self.BINARY_FORMATS)
			default_idx = 0  # TTF
		# Try to preserve the user's existing selection if still valid.
		try:
			current_idx = self.w.formatPopup.get()
			current_items = self.w.formatPopup.getItems()
			current_label = current_items[current_idx] if 0 <= current_idx < len(current_items) else None
		except Exception:
			current_label = None
		try:
			self.w.formatPopup.setItems(items)
			if current_label in items:
				self.w.formatPopup.set(items.index(current_label))
			else:
				self.w.formatPopup.set(default_idx)
		except Exception:
			pass

	@objc.python_method
	def _load_gsfont(self, gsfont):
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

		self._gsfont = gsfont
		# Don't clear self._font_path — the user may switch back to file mode via Browse.
		self._instance_names = names
		# Setter handles both _source_mode AND widget visibility + format popup.
		self._set_source_mode_ui(self.SOURCE_GSFONT)
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
		"""Update output name, axis preview, and Generate button state on toggle."""
		self._refresh_name()
		self._refresh_axis_preview()
		self._refresh_generate_button()

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

	def _on_generate(self, sender):
		"""Read UI state, then dispatch to file- or gsfont-source generator."""
		selected = self._selected_instance_names()
		if not selected:
			self._set_status('Select at least one named instance.', error=True)
			return

		family_name = self.w.nameField.get().strip()
		if not family_name:
			self._set_status('Enter an output name.', error=True)
			return

		fmt_items = self.w.formatPopup.getItems()
		fmt_index = self.w.formatPopup.get()
		fmt_label = fmt_items[fmt_index] if fmt_items else 'TTF'
		# Normalise the display label '.glyphs' to the internal token 'GLYPHS'.
		fmt = 'GLYPHS' if fmt_label == self.GSFONT_FORMAT_LABEL else fmt_label
		ext = extension_for_format(fmt)

		# Cross-source sanity check: .glyphs output requires an open-font source.
		if fmt == 'GLYPHS' and self._source_mode != self.SOURCE_GSFONT:
			self._set_status(
				'.glyphs output requires using an open Glyphs font as the source.',
				error=True,
			)
			return

		if flavor_for_format(fmt) == 'woff2':
			# brotli is required for both the fontTools path (we call it
			# directly via partial.save) AND defensively for the Glyphs-native
			# pipeline on builds where Glyphs forwards to fontTools internally.
			# Probing here surfaces a targeted hint instead of a deep traceback.
			try:
				import brotli  # noqa: F401
			except ImportError:
				self._set_status(
					'WOFF2 output requires the brotli package; install it or pick WOFF/TTF/OTF.',
					error=True,
				)
				return

		folder = self.w.folderField.get().strip()
		if not folder:
			folder = self._default_output_folder()

		output_path = safe_output_path(folder, family_name, ext)

		# Dispatch to the appropriate generator.
		if self._source_mode == self.SOURCE_GSFONT:
			self._generate_from_gsfont(selected, family_name, fmt, output_path)
		else:
			self._generate_from_file(selected, family_name, fmt, output_path)

	@objc.python_method
	def _default_output_folder(self):
		"""Return a sensible default output folder when the user has not chosen one."""
		if self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None:
			try:
				fp = self._gsfont.filepath
			except Exception:
				fp = None
			if fp:
				return os.path.dirname(fp)
		if self._font_path:
			return os.path.dirname(self._font_path)
		return os.path.expanduser('~/Desktop')

	@objc.python_method
	def _generate_from_file(self, selected, family_name, fmt, output_path):
		"""File-source path — uses fontTools instancer in a worker thread (unchanged)."""
		font_path = self._font_path
		self._set_status('Generating…')
		self.w.generateButton.enable(False)
		self.w.revealButton.enable(False)
		try:
			self.w.spinner.start()
		except Exception:
			pass

		# Reset the cancellation flag at the start of each generate so a
		# prior Cancel click doesn't short-circuit this one.
		self._cancelled = False
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
						if os.path.exists(output_path):
							os.unlink(output_path)
					except OSError:
						pass
					return
				AppHelper.callAfter(dialog_ref._on_generate_success, output_path)

		threading.Thread(target=_run, daemon=True).start()

	@objc.python_method
	def _generate_from_gsfont(self, selected, family_name, fmt, output_path):
		"""Open-font-source path — runs on the main thread (Glyphs APIs require it).

		For ``.glyphs`` output we just clamp + save. For binary outputs we route
		through Glyphs' native export pipeline (Variable Font Setting + generate).

		We defer the actual heavy work to the next runloop tick via
		``AppHelper.callAfter`` so the spinner.start() / Generating… status
		updates can paint before the main thread blocks inside Glyphs.open and
		GSInstance.generate.
		"""
		gsfont = self._gsfont
		if gsfont is None:
			self._set_status('No open Glyphs font selected.', error=True)
			return

		self._set_status('Generating…')
		self.w.generateButton.enable(False)
		self.w.revealButton.enable(False)
		try:
			self.w.spinner.start()
		except (AttributeError, RuntimeError):
			pass

		# Defer the long Glyphs.open / generate sequence so the spinner and
		# status label paint before the main thread blocks. Without this,
		# the user sees a hung dialog with no feedback for several seconds.
		AppHelper.callAfter(
			self._run_gsfont_generate, gsfont, selected, family_name, fmt, output_path
		)

	@objc.python_method
	def _run_gsfont_generate(self, gsfont, selected, family_name, fmt, output_path):
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

		self._font_path = path
		self._instance_names = names
		# Switch back to file source mode and undo any prior gsfont selection.
		self._gsfont = None
		try:
			self.w.gsfontPopup.set(0)  # reset to sentinel
		except Exception:
			pass
		# Setter handles both _source_mode AND widget visibility + format popup.
		self._set_source_mode_ui(self.SOURCE_FILE)
		self.w.fontPathField.set(path)
		self._set_status('')
		self._populate_instance_checks(names)

		if not self.w.folderField.get().strip():
			self.w.folderField.set(os.path.dirname(path))

		self._name_overridden = False
		self._refresh_name()
		self._refresh_axis_preview()
		self._refresh_generate_button()

	@objc.python_method
	def _populate_instance_checks(self, names):
		"""Build one CheckBox per named instance inside a scrollable Group."""
		self.w.instancePlaceholder.show(False)

		n = len(names)
		check_row = self.CHECK_H + self.CHECK_GAP
		inner_h = n * check_row + self.CHECK_GAP
		visible_rows = max(1, min(n, 8))
		scroll_h = visible_rows * check_row + self.CHECK_GAP

		# 0-width sentinel '-0' previously left the group zero-wide; explicit
		# width (W - 2*PAD - scrollbar) keeps children visible.
		group_width = self.W - 2 * self.PAD - 18
		inner_group = vanilla.Group((0, 0, group_width, inner_h))
		self._checks = []
		for idx, name in enumerate(names):
			y = self.CHECK_GAP + idx * check_row
			attr = f'_cb_{idx}'
			cb = vanilla.CheckBox(
				(8, y, group_width - 16, self.CHECK_H),
				name,
				value=False,
				callback=self._on_check_toggled,
			)
			setattr(inner_group, attr, cb)
			self._checks.append((attr, inner_group))

		self._inner_group = inner_group

		# Rebuild the ScrollView with the inner group as its document view —
		# the previous setDocumentView_ via munged selector was unreliable.
		try:
			self.w.instanceScroll._nsObject.setDocumentView_(inner_group._nsObject)
		except Exception:
			# Last-resort: drop and recreate the ScrollView
			pass

		self.w.instanceScroll.setPosSize(
			(self.PAD, self._scroll_top_y, -self.PAD, scroll_h)
		)
		self.w.instanceScroll.show(True)

		new_h = self._compute_window_height(n)
		self.w.resize(self.W, new_h)

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _iter_checks(self):
		"""Yield every CheckBox widget bound to the inner group."""
		if not hasattr(self, '_inner_group'):
			return
		for attr, group in self._checks:
			cb = getattr(group, attr, None)
			if cb is not None:
				yield cb

	def _set_all_checks(self, value):
		"""Set every checkbox to ``value`` and refresh dependent UI."""
		for cb in self._iter_checks():
			cb.set(bool(value))
		self._on_check_toggled(None)

	def _selected_instance_names(self):
		"""Return names of instances whose checkbox is ticked."""
		if not hasattr(self, '_inner_group'):
			return []
		selected = []
		for idx, name in enumerate(self._instance_names):
			attr = f'_cb_{idx}'
			cb = getattr(self._inner_group, attr, None)
			if cb and cb.get():
				selected.append(name)
		return selected

	@objc.python_method
	def _refresh_name(self):
		"""Auto-compute the output name unless the user has overridden it."""
		if self._name_overridden:
			return
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
	def _refresh_axis_preview(self):
		"""Render the axis hull as colored ■-prefixed chips, one axis per line."""
		selected = self._selected_instance_names()
		if not selected:
			self._set_hull_text('(select instances to preview)')
			return

		hull = {}
		try:
			if self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None:
				hull = compute_gsfont_hull(self._gsfont, selected)
			elif self._font_path:
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

	def _refresh_generate_button(self):
		"""Enable Generate only when a source is loaded and >=1 instance selected."""
		source_loaded = (
			self._font_path is not None
			or (self._source_mode == self.SOURCE_GSFONT and self._gsfont is not None)
		)
		enabled = bool(source_loaded and self._selected_instance_names())
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
