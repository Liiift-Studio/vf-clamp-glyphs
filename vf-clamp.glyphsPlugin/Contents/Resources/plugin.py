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
	get_instance_names,
	produce_restricted_vf,
	safe_output_path,
	_FONTTOOLS_AVAILABLE,
	_FONTTOOLS_IMPORT_ERROR,
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
		if not _FONTTOOLS_AVAILABLE:
			Message(
				f'vf-clamp requires fontTools, which could not be imported:\n\n{_FONTTOOLS_IMPORT_ERROR}',
				'vf-clamp — Missing Dependency',
			)
			return
		try:
			check_fonttools_version()
		except RuntimeError as e:
			Message(str(e), 'vf-clamp — Incompatible fontTools')
			return
		self.dialog = VFClampDialog()
		self.dialog.show()

	@objc.python_method
	def __file__(self):
		"""Return the .glyphsPlugin bundle path (not the inner module path)."""
		# Resources/plugin.py -> Resources/ -> Contents/ -> bundle root
		return os.path.dirname(os.path.dirname(_RESOURCES_DIR))


# Backwards-compat alias so a pre-existing Info.plist NSPrincipalClass entry
# of `VFClampPlugin` still resolves while we migrate users to the namespaced name.
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

	def __init__(self):
		"""Initialise dialog state. Window is shown by show()."""
		self._font_path = None
		self._instance_names = []   # ordered list from fvar
		self._checks = []           # list of (attr_name, inner_group) tuples
		self._name_overridden = False
		self._build_window()

	# ------------------------------------------------------------------
	# Window construction
	# ------------------------------------------------------------------

	def _build_window(self):
		"""Build the full dialog layout with vanilla widgets."""
		w = self.W
		h = self._compute_window_height(0)

		# vanilla.Window (not FloatingWindow) so the panel does not hover over
		# Finder windows the user opens to inspect output.
		self.w = vanilla.Window(
			(w, h),
			'vf-clamp — Generate Restricted VFs',
			minSize=(w, 320),
			maxSize=(w, 1200),
		)
		win = self.w
		PAD = self.PAD
		LABEL_H = self.LABEL_H
		FIELD_H = self.FIELD_H
		BTN_H = self.BTN_H
		ROW = self.ROW

		y = PAD

		# --- Variable Font File ---
		win.fontLabel = vanilla.TextBox((PAD, y, -PAD, LABEL_H), 'Variable Font File:')
		y += LABEL_H + 4

		win.fontPathField = vanilla.EditText(
			(PAD, y, -90, FIELD_H),
			placeholder='Select a .ttf or .otf variable font…',
			readOnly=True,
		)
		win.browseButton = vanilla.Button(
			(-86, y - 1, -PAD, BTN_H),
			'Browse…',
			callback=self._on_browse,
		)
		y += ROW + 4

		win.divider1 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Named Instances (scroll area) + bulk-select buttons ---
		win.instanceLabel = vanilla.TextBox((PAD, y, 200, LABEL_H), 'Named Instances:')

		# Bulk-selection helpers on the right of the label row
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
		y += LABEL_H + 6

		win.instancePlaceholder = vanilla.TextBox(
			(PAD, y, -PAD, LABEL_H),
			'Open a variable font to see its named instances.',
			sizeStyle='small',
		)
		self._scroll_top_y = y
		self._scroll_height = LABEL_H

		win.instanceScroll = vanilla.ScrollView(
			(PAD, y, -PAD, LABEL_H),
			hasHorizontalScroller=False,
			hasVerticalScroller=True,
			autohidesScrollers=True,
		)
		win.instanceScroll.show(False)

		y += LABEL_H + 8

		# --- Axis Ranges preview ---
		win.axisLabel = vanilla.TextBox((PAD, y, -PAD, LABEL_H), 'Axis Ranges:')
		y += LABEL_H + 4
		win.axisPreview = vanilla.TextBox(
			(PAD, y, -PAD, LABEL_H),
			'(select instances to preview)',
			sizeStyle='small',
		)
		y += LABEL_H + 8

		win.divider2 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Output Name ---
		win.nameLabel = vanilla.TextBox((PAD, y, -PAD, LABEL_H), 'Output Name:')
		y += LABEL_H + 4
		win.nameField = vanilla.EditText(
			(PAD, y, -PAD, FIELD_H),
			placeholder='e.g. MyFont Light-Bold',
			callback=self._on_name_edited,
		)
		y += ROW + 4

		# --- Format (full row, consistent vertical rhythm) ---
		win.formatLabel = vanilla.TextBox((PAD, y, -PAD, LABEL_H), 'Format:')
		y += LABEL_H + 4
		win.formatPopup = vanilla.PopUpButton(
			(PAD, y, 160, FIELD_H + 2),
			['TTF', 'OTF', 'WOFF', 'WOFF2'],
		)
		y += ROW + 4

		# --- Output Folder ---
		win.folderLabel = vanilla.TextBox((PAD, y, -PAD, LABEL_H), 'Output Folder:')
		y += LABEL_H + 4
		win.folderField = vanilla.EditText(
			(PAD, y, -90, FIELD_H),
			placeholder='Default: same folder as font file',
			readOnly=True,
		)
		win.folderButton = vanilla.Button(
			(-86, y - 1, -PAD, BTN_H),
			'Choose…',
			callback=self._on_choose_folder,
		)
		y += ROW + 8

		win.divider3 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Generate button + status + reveal ---
		win.generateButton = vanilla.Button(
			(-PAD - 120, y - 1, -PAD, BTN_H),
			'Generate',
			callback=self._on_generate,
		)
		win.generateButton.enable(False)
		# Make Generate the default Return-key action
		try:
			win.generateButton._nsObject.setKeyEquivalent_('\r')
		except Exception:
			pass

		win.revealButton = vanilla.Button(
			(-PAD - 200, y - 1, 76, BTN_H),
			'Reveal',
			callback=self._on_reveal,
			sizeStyle='small',
		)
		win.revealButton.enable(False)
		self._last_output_path = None

		win.statusLabel = vanilla.TextBox(
			(PAD, y + 4, -PAD - 210, LABEL_H * 2),
			'',
			sizeStyle='small',
			selectable=True,
		)
		win.spinner = vanilla.ProgressSpinner((-PAD - 226, y, 18, 18), displayWhenStopped=False)
		try:
			win.spinner.stop()
		except Exception:
			pass
		y += ROW + PAD

		self._static_sections_height = y

	def _compute_window_height(self, n_instances):
		"""Return total window height for n_instances checkboxes in the scroll area."""
		LABEL_H = self.LABEL_H
		FIELD_H = self.FIELD_H
		ROW = self.ROW
		PAD = self.PAD
		CHECK_H = self.CHECK_H
		CHECK_GAP = self.CHECK_GAP

		top = PAD + LABEL_H + 4 + ROW + 4 + 12 + LABEL_H + 6
		visible_rows = max(1, min(n_instances, 8))
		scroll_h = visible_rows * (CHECK_H + CHECK_GAP) + 8
		# axis preview block + dividers + name + format + folder + generate
		bottom = (
			8 + LABEL_H + 4 + LABEL_H + 8 + 12 +
			LABEL_H + 4 + FIELD_H + 4 +
			LABEL_H + 4 + (FIELD_H + 2) + 4 +
			LABEL_H + 4 + FIELD_H + 8 + 12 +
			ROW + PAD
		)
		return top + scroll_h + bottom

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
		"""Read UI state, then run produce_restricted_vf on a worker thread."""
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
		fmt = fmt_items[fmt_index] if fmt_items else 'TTF'
		ext = extension_for_format(fmt)

		if flavor_for_format(fmt) == 'woff2':
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
			folder = (
				os.path.dirname(self._font_path)
				if self._font_path
				else os.path.expanduser('~/Desktop')
			)

		output_path = safe_output_path(folder, family_name, ext)
		font_path = self._font_path

		self._set_status('Generating…')
		self.w.generateButton.enable(False)
		self.w.revealButton.enable(False)
		try:
			self.w.spinner.start()
		except Exception:
			pass

		# Retain self for the thread by capturing as a local
		dialog_ref = self

		def _run():
			"""Blocking font generation — runs off the AppKit main thread."""
			try:
				produce_restricted_vf(font_path, selected, family_name, output_path, fmt=fmt)
			except (ValueError, RuntimeError, OSError) as e:
				msg = str(e)
				AppHelper.callAfter(dialog_ref._on_generate_failure, msg)
			except Exception as e:
				# Unexpected error class — log full traceback to Macro Panel
				traceback.print_exc()
				msg = str(e)
				AppHelper.callAfter(dialog_ref._on_generate_failure, msg)
			else:
				AppHelper.callAfter(dialog_ref._on_generate_success, output_path)

		threading.Thread(target=_run, daemon=True).start()

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
		scrubbed = message.replace(os.path.expanduser('~'), '~')
		self._set_status(f'Error: {scrubbed}', error=True)
		self.w.generateButton.enable(True)
		try:
			self.w.spinner.stop()
		except Exception:
			pass

	def _alive(self):
		"""Return True if the dialog's underlying NSWindow is still around."""
		try:
			return self.w._window is not None and self.w._window.isVisible()
		except Exception:
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
		"""Render a one-line summary of the axis hull for the current selection."""
		try:
			from core import _safe_open_font, get_axis_hull_from_instances  # local import
		except Exception:
			self.w.axisPreview.set('(unable to compute)')
			return

		selected = self._selected_instance_names()
		if not selected or not self._font_path:
			self.w.axisPreview.set('(select instances to preview)')
			return
		try:
			import contextlib as _cl
			with _cl.closing(_safe_open_font(self._font_path)) as f:
				hull = get_axis_hull_from_instances(f, selected)
		except Exception as e:
			self.w.axisPreview.set(f'(unavailable: {e})')
			return
		parts = []
		for tag, (lo, hi) in hull.items():
			parts.append(f'{tag} {lo}' if lo == hi else f'{tag} {lo}-{hi}')
		self.w.axisPreview.set('  ·  '.join(parts) if parts else '(no axes)')

	def _refresh_generate_button(self):
		"""Enable Generate only when a font is loaded and >=1 instance selected."""
		enabled = bool(self._font_path and self._selected_instance_names())
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
