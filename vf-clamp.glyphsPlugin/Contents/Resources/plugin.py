# plugin.py — vf-clamp Glyphs.app plugin for generating restricted variable fonts

import objc
import os
import re
import threading
import warnings

from GlyphsApp import *
from GlyphsApp.plugins import *

import vanilla

# Guard fonttools import so we can surface a useful error inside Glyphs
# rather than crashing silently at startup.
try:
	from fontTools.varLib import instancer
	from fontTools.ttLib import TTFont
	_FONTTOOLS_AVAILABLE = True
except ImportError as _ft_import_error:
	_FONTTOOLS_AVAILABLE = False
	_ft_import_error_msg = str(_ft_import_error)


# ---------------------------------------------------------------------------
# Core fonttools helpers
# ---------------------------------------------------------------------------

def compute_hull(font, selected_names):
	"""Compute the per-axis hull (min/max) across the selected named instances.

	Returns a dict mapping axis tag → scalar (pin) or AxisTriple (range).
	Only axes that appear in the selected instances are included; any other
	fvar axis is left unrestricted (not present in the returned dict).
	"""
	if 'fvar' not in font:
		raise ValueError('This font has no fvar table — it is not a variable font.')

	fvar = font['fvar']
	name_table = font['name']

	# Build a dict of instance name → {axis_tag: value}
	all_insts = {}
	for inst in fvar.instances:
		label = name_table.getDebugName(inst.subfamilyNameID)
		if label:
			all_insts[label] = dict(inst.coordinates)

	hull = {}
	for name in selected_names:
		if name not in all_insts:
			continue
		for tag, val in all_insts[name].items():
			if tag not in hull:
				hull[tag] = [val, val]
			else:
				hull[tag][0] = min(hull[tag][0], val)
				hull[tag][1] = max(hull[tag][1], val)

	result = {}
	for tag, (lo, hi) in hull.items():
		if lo == hi:
			# Pin the axis to a single value (scalar)
			result[tag] = lo
		else:
			# Restrict axis to a range — use AxisTriple(min, None, max);
			# None for default lets fonttools derive it from the fvar default.
			# AxisRange() is deprecated and emits DeprecationWarning.
			result[tag] = instancer.AxisTriple(lo, None, hi)

	return result


def patch_name_table(font, family_name):
	"""Update name IDs 1, 4, 6, and optionally 16 and 25 to reflect the restricted VF.

	Handles both Windows (platformID=3, UTF-16-BE) and Mac (platformID=1, mac_roman)
	records.  PostScript name (nameID 6 and 25) is sanitised to ASCII alphanumeric + dash.
	nameID 2 (Subfamily) is intentionally left alone — the output is still variable.
	"""
	# PostScript name: only A-Z a-z 0-9 and hyphen, spaces replaced with hyphens
	ps_name = re.sub(r'[^A-Za-z0-9-]', '', family_name.replace(' ', '-'))

	name_table = font['name']
	existing_ids = {r.nameID for r in name_table.names}

	# Decide which name IDs to update
	updates = {1: family_name, 4: family_name, 6: ps_name}
	if 16 in existing_ids:
		updates[16] = family_name
	if 25 in existing_ids:
		updates[25] = ps_name

	# Track which (nameID, platformID, platEncID, langID) combos we have updated
	updated = set()

	for record in name_table.names:
		if record.nameID not in updates:
			continue
		value = updates[record.nameID]
		if record.platformID == 3:
			# Windows: encode as UTF-16-BE
			record.string = value.encode('utf-16-be')
		elif record.platformID == 1:
			# Mac: encode as mac_roman; fall back to ASCII with replacement
			try:
				record.string = value.encode('mac_roman')
			except (UnicodeEncodeError, LookupError):
				record.string = value.encode('ascii', errors='replace')
		updated.add((record.nameID, record.platformID))

	# Ensure at least a Windows (platformID=3) record exists for nameIDs we want to set.
	# Some fonts only have platformID=1 records; add Windows records when absent.
	for name_id, value in updates.items():
		if (name_id, 3) not in updated:
			name_table.setName(value, name_id, 3, 1, 0x0409)


def compact_name(first, last):
	"""Strip shared word prefix/suffix and join differing parts with a hyphen.

	Canonical TypeScript implementation: @liiift-studio/vf-clamp src/core/utils.ts compactName()
	Duplicate also exists in vf-clamp-robofont controller.py and vf-clamp-vscode panel.ts webview.

	Examples:
	  'Encode Sans Light' + 'Encode Sans Bold'  → 'Encode Sans Light-Bold'
	  'Light'                                    → 'Light'   (single instance)
	  'Regular' + 'Regular'                      → 'Regular'
	"""
	if first == last:
		return first
	fw = first.split()
	lw = last.split()

	# Count shared prefix words
	prefix_len = 0
	while prefix_len < len(fw) and prefix_len < len(lw) and fw[prefix_len] == lw[prefix_len]:
		prefix_len += 1

	# Count shared suffix words (must not overlap with the prefix)
	suffix_len = 0
	while (
		suffix_len < len(fw) - prefix_len
		and suffix_len < len(lw) - prefix_len
		and fw[-1 - suffix_len] == lw[-1 - suffix_len]
	):
		suffix_len += 1

	prefix = ' '.join(fw[:prefix_len])
	a = ' '.join(fw[prefix_len: len(fw) - suffix_len if suffix_len else None])
	b = ' '.join(lw[prefix_len: len(lw) - suffix_len if suffix_len else None])
	suffix = ' '.join(fw[len(fw) - suffix_len:]) if suffix_len else ''

	middle = f'{a}-{b}' if (a and b) else (a or b)
	return ' '.join(filter(None, [prefix, middle, suffix]))


def produce_restricted_vf(font_path, selected_names, family_name, output_path):
	"""Load font_path, restrict axes to the hull of selected_names, patch names, and save.

	Raises ValueError for bad input (no fvar, no valid instances).
	Raises OSError for file I/O failures.
	"""
	if not _FONTTOOLS_AVAILABLE:
		raise RuntimeError(f'fonttools is not available: {_ft_import_error_msg}')

	try:
		font = TTFont(font_path)
	except Exception as e:
		raise OSError(f'Cannot open font file: {e}') from e

	if 'fvar' not in font:
		raise ValueError('This font has no variable axes — it is not a variable font.')

	hull = compute_hull(font, selected_names)
	if not hull:
		raise ValueError('No valid named instances found for the selected names.')

	# Warn if any axis default falls outside the restricted range — fonttools silently clamps it.
	fvar = font['fvar']
	for ax in fvar.axes:
		constraint = hull.get(ax.axisTag)
		if isinstance(constraint, tuple):
			lo, hi = constraint
			if not (lo <= ax.defaultValue <= hi):
				clamped = max(lo, min(hi, ax.defaultValue))
				print(
					f'Warning: {ax.axisTag} default ({ax.defaultValue}) is outside '
					f'restricted range [{lo}, {hi}]. Default will be clamped to {clamped}.'
				)

	try:
		partial = instancer.instantiateVariableFont(font, hull)
	except Exception as e:
		raise RuntimeError(f'instancer failed: {e}') from e

	patch_name_table(partial, family_name)

	# Ensure output directory exists
	output_dir = os.path.dirname(output_path)
	if output_dir:
		os.makedirs(output_dir, exist_ok=True)

	try:
		partial.save(output_path)
	except Exception as e:
		raise OSError(f'Failed to save output font: {e}') from e


def get_instance_names(font_path):
	"""Return an ordered list of named-instance subfamily names from a variable font file.

	Raises ValueError if the font has no fvar table.
	Raises OSError if the file cannot be opened.
	"""
	if not _FONTTOOLS_AVAILABLE:
		raise RuntimeError(f'fonttools is not available: {_ft_import_error_msg}')

	try:
		font = TTFont(font_path)
	except Exception as e:
		raise OSError(f'Cannot open font file: {e}') from e

	if 'fvar' not in font:
		raise ValueError('This font has no variable axes — select a variable font (.ttf/.otf with an fvar table).')

	name_table = font['name']
	names = []
	for inst in font['fvar'].instances:
		label = name_table.getDebugName(inst.subfamilyNameID)
		if label and label not in names:
			names.append(label)
	return names


def extension_for_format(fmt):
	"""Return the file extension string for a given format label (TTF, OTF, WOFF, WOFF2)."""
	return {
		'TTF': '.ttf',
		'OTF': '.otf',
		'WOFF': '.woff',
		'WOFF2': '.woff2',
	}.get(fmt.upper(), '.ttf')


# ---------------------------------------------------------------------------
# Glyphs Plugin
# ---------------------------------------------------------------------------

class VFClampPlugin(GeneralPlugin):
	"""Glyphs.app GeneralPlugin that adds 'Generate Restricted VFs…' to the Script menu."""

	@objc.python_method
	def settings(self):
		"""Declare the plugin name as it appears in menus."""
		self.name = 'vf-clamp'

	@objc.python_method
	def start(self):
		"""Register the menu item under Script › vf-clamp."""
		newMenuItem = NSMenuItem.new()
		newMenuItem.setTitle_('Generate Restricted VFs…')
		# showDialog_ must NOT carry @objc.python_method so AppKit can call it
		# as an Objective-C selector when the menu item is activated.
		newMenuItem.setAction_(self.showDialog_)
		newMenuItem.setTarget_(self)
		Glyphs.menu[SCRIPT_MENU].append(newMenuItem)

	# NOTE: no @objc.python_method here — AppKit calls this as an ObjC action selector.
	def showDialog_(self, sender):
		"""Open the vf-clamp dialog window."""
		if not _FONTTOOLS_AVAILABLE:
			Message(
				f'vf-clamp requires fonttools, which could not be imported:\n\n{_ft_import_error_msg}',
				'vf-clamp — Missing Dependency',
			)
			return
		self.dialog = VFClampDialog()
		self.dialog.show()

	@objc.python_method
	def __file__(self):
		"""Return the plugin file path (required by Glyphs.app)."""
		return __file__


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

# Pixel metrics — all layout is top-down with fixed row heights
_W = 500          # window width
_PAD = 16         # outer padding
_LABEL_H = 20     # standard label height
_FIELD_H = 22     # standard input field height
_BTN_H = 24       # button height
_ROW = 28         # vertical rhythm per row
_CHECK_H = 20     # checkbox row height
_CHECK_GAP = 4    # gap between checkboxes


class VFClampDialog:
	"""Vanilla FloatingWindow dialog for selecting named instances and generating a restricted VF."""

	def __init__(self):
		"""Initialise dialog state. Window is not shown until show() is called."""
		self._font_path = None
		self._instance_names = []   # ordered list from fvar
		self._checks = []           # vanilla.CheckBox widgets, one per instance
		self._name_overridden = False
		self._build_window()

	# ------------------------------------------------------------------
	# Window construction
	# ------------------------------------------------------------------

	def _build_window(self):
		"""Build the full dialog layout with vanilla widgets."""
		w = _W
		# Calculate initial height for static sections only
		# (instance section expands when a font is loaded)
		h = self._compute_window_height(0)

		self.w = vanilla.FloatingWindow(
			(w, h),
			'vf-clamp — Generate Restricted VFs',
			minSize=(w, 300),
			maxSize=(w, 1000),
		)
		win = self.w

		y = _PAD

		# --- Variable Font File ---
		win.fontLabel = vanilla.TextBox((_PAD, y, -_PAD, _LABEL_H), 'Variable Font File:')
		y += _LABEL_H + 4

		win.fontPathField = vanilla.EditText(
			(_PAD, y, -90, _FIELD_H),
			placeholder='Select a .ttf or .otf variable font…',
			readOnly=True,
		)
		win.browseButton = vanilla.Button(
			(-86, y - 1, -_PAD, _BTN_H),
			'Browse…',
			callback=self._on_browse,
		)
		y += _ROW + 4

		win.divider1 = vanilla.HorizontalLine((_PAD, y, -_PAD, 1))
		y += 12

		# --- Named Instances (scroll area) ---
		win.instanceLabel = vanilla.TextBox((_PAD, y, -_PAD, _LABEL_H), 'Named Instances:')
		y += _LABEL_H + 6

		# Placeholder shown before a font is loaded
		win.instancePlaceholder = vanilla.TextBox(
			(_PAD, y, -_PAD, _LABEL_H),
			'Open a variable font to see its named instances.',
			sizeStyle='small',
		)
		# Scrollable group that holds the dynamic checkboxes
		self._scroll_top_y = y
		self._scroll_height = _LABEL_H  # updated after font load

		win.instanceScroll = vanilla.ScrollView(
			(_PAD, y, -_PAD, _LABEL_H),
			hasHorizontalScroller=False,
			hasVerticalScroller=True,
			autohidesScrollers=True,
		)
		win.instanceScroll.show(False)  # hidden until font loaded

		y += _LABEL_H + 8
		self._post_scroll_y = y  # everything below is repositioned after font load

		win.divider2 = vanilla.HorizontalLine((_PAD, y, -_PAD, 1))
		y += 12

		# --- Output Name ---
		win.nameLabel = vanilla.TextBox((_PAD, y, -_PAD, _LABEL_H), 'Output Name:')
		y += _LABEL_H + 4

		win.nameField = vanilla.EditText(
			(_PAD, y, -_PAD, _FIELD_H),
			placeholder='e.g. MyFont Light-Bold',
			callback=self._on_name_edited,
		)
		y += _ROW + 4

		# --- Format ---
		win.formatLabel = vanilla.TextBox((_PAD, y, 120, _LABEL_H), 'Format:')
		win.formatPopup = vanilla.PopUpButton(
			(_PAD + 120, y - 2, 120, _FIELD_H + 2),
			['TTF', 'OTF', 'WOFF', 'WOFF2'],
		)
		y += _ROW + 4

		# --- Output Folder ---
		win.folderLabel = vanilla.TextBox((_PAD, y, -_PAD, _LABEL_H), 'Output Folder:')
		y += _LABEL_H + 4

		win.folderField = vanilla.EditText(
			(_PAD, y, -90, _FIELD_H),
			placeholder='Default: same folder as font file',
			readOnly=True,
		)
		win.folderButton = vanilla.Button(
			(-86, y - 1, -_PAD, _BTN_H),
			'Choose…',
			callback=self._on_choose_folder,
		)
		y += _ROW + 8

		win.divider3 = vanilla.HorizontalLine((_PAD, y, -_PAD, 1))
		y += 12

		# --- Generate button + status label ---
		win.generateButton = vanilla.Button(
			(-_PAD - 120, y - 1, -_PAD, _BTN_H),
			'Generate',
			callback=self._on_generate,
		)
		win.generateButton.enable(False)

		win.statusLabel = vanilla.TextBox(
			(_PAD, y + 4, -140, _LABEL_H),
			'',
			sizeStyle='small',
		)
		y += _ROW + _PAD

		self._static_sections_height = y

	def _compute_window_height(self, n_instances):
		"""Return total window height for n_instances checkboxes in the scroll area."""
		# Static top section: font label + field + divider + instance label
		top = _PAD + _LABEL_H + 4 + _ROW + 4 + 12 + _LABEL_H + 6
		# Scroll area height: at least 1 row, capped at 8 visible rows
		visible_rows = max(1, min(n_instances, 8))
		scroll_h = visible_rows * (_CHECK_H + _CHECK_GAP) + 8
		# Static bottom sections: divider + name + format + folder + generate
		bottom = 8 + 12 + _LABEL_H + 4 + _FIELD_H + 4 + _ROW + 4 + _ROW + 4 + _LABEL_H + 4 + _FIELD_H + 8 + 12 + _ROW + _PAD
		return top + scroll_h + bottom

	# ------------------------------------------------------------------
	# Event handlers
	# ------------------------------------------------------------------

	def _on_browse(self, sender):
		"""Open a file-picker panel for the user to select a variable font."""
		panel = NSOpenPanel.openPanel()
		panel.setCanChooseFiles_(True)
		panel.setCanChooseDirectories_(False)
		panel.setAllowsMultipleSelection_(False)
		panel.setAllowedFileTypes_(['ttf', 'otf', 'TTF', 'OTF'])
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
		"""Track whether the user has manually set the output name."""
		self._name_overridden = bool(self.w.nameField.get().strip())

	def _on_check_toggled(self, sender):
		"""Update output name and Generate button state when an instance is toggled."""
		self._refresh_name()
		self._refresh_generate_button()

	def _on_generate(self, sender):
		"""Read UI state on main thread, then run produce_restricted_vf on a background thread."""
		selected = self._selected_instance_names()
		if not selected:
			self._set_status('Select at least one named instance.', error=True)
			return

		family_name = self.w.nameField.get().strip()
		if not family_name:
			self._set_status('Enter an output name.', error=True)
			return

		# Resolve format string from PopUpButton (get() returns an index)
		fmt_items = self.w.formatPopup.getItems()
		fmt_index = self.w.formatPopup.get()
		fmt = fmt_items[fmt_index] if fmt_items else 'TTF'
		ext = extension_for_format(fmt)

		# Determine output folder
		folder = self.w.folderField.get().strip()
		if not folder:
			folder = os.path.dirname(self._font_path) if self._font_path else os.path.expanduser('~/Desktop')

		# Sanitise family name for use as a filename (no path separators)
		safe_name = re.sub(r'[/\\:*?"<>|]', '-', family_name)
		output_path = os.path.join(folder, safe_name + ext)

		# Capture before entering thread — thread must not read self.w.*
		font_path = self._font_path

		# Update UI synchronously before handing off
		self._set_status('Generating…')
		self.w.generateButton.enable(False)

		def _run():
			"""Blocking font generation — runs off the AppKit main thread."""
			try:
				produce_restricted_vf(font_path, selected, family_name, output_path)
				msg = f'Saved: {output_path}'
				objc.callOnMainThread(lambda: self._set_status(msg))
			except Exception as e:
				err_msg = f'Error: {e}'
				objc.callOnMainThread(lambda: self._set_status(err_msg, error=True))
			finally:
				objc.callOnMainThread(lambda: self.w.generateButton.enable(True))

		threading.Thread(target=_run, daemon=True).start()

	# ------------------------------------------------------------------
	# Font loading
	# ------------------------------------------------------------------

	def _load_font(self, path):
		"""Parse the font at path, populate the instance checkbox list, and refresh UI."""
		self._set_status('Loading…')
		try:
			names = get_instance_names(path)
		except ValueError as e:
			self._set_status(f'{e}', error=True)
			return
		except OSError as e:
			self._set_status(f'Error loading font: {e}', error=True)
			return
		except Exception as e:
			self._set_status(f'Unexpected error: {e}', error=True)
			return

		self._font_path = path
		self._instance_names = names
		self.w.fontPathField.set(path)
		self._set_status('')
		self._populate_instance_checks(names)

		# Auto-fill output folder to font's directory (if not already chosen)
		if not self.w.folderField.get().strip():
			self.w.folderField.set(os.path.dirname(path))

		self._name_overridden = False
		self._refresh_name()
		self._refresh_generate_button()

	def _populate_instance_checks(self, names):
		"""Build one CheckBox per named instance inside a scrollable Group.

		Replaces any previous set of checkboxes entirely.
		"""
		# Hide placeholder text
		self.w.instancePlaceholder.show(False)

		n = len(names)
		check_row = _CHECK_H + _CHECK_GAP

		# Total height of the inner group (all checkboxes stacked)
		inner_h = n * check_row + _CHECK_GAP

		# Visible scroll area height: show up to 8 rows, minimum 1
		visible_rows = max(1, min(n, 8))
		scroll_h = visible_rows * check_row + _CHECK_GAP

		# Build the inner Group that holds all checkboxes
		inner_group = vanilla.Group((0, 0, -0, inner_h))
		self._checks = []
		for idx, name in enumerate(names):
			y = _CHECK_GAP + idx * check_row
			attr = f'_cb_{idx}'
			cb = vanilla.CheckBox(
				(8, y, -8, _CHECK_H),
				name,
				value=False,
				callback=self._on_check_toggled,
			)
			setattr(inner_group, attr, cb)
			self._checks.append((attr, inner_group))

		# Store reference to inner group so we can read checkboxes later
		self._inner_group = inner_group

		# Resize and show the scroll view
		self.w.instanceScroll.setPosSize((_PAD, self._scroll_top_y, -_PAD, scroll_h))
		self.w.instanceScroll.setDocumentView_(inner_group._nsObject)
		self.w.instanceScroll.show(True)

		# Resize window to fit the new scroll area
		new_h = self._compute_window_height(n)
		self.w.resize(_W, new_h)

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

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

	def _refresh_name(self):
		"""Auto-compute output name from first + last selected instance (unless overridden)."""
		if self._name_overridden:
			return
		selected = self._selected_instance_names()
		if not selected:
			self.w.nameField.set('')
			return

		# Derive base family name by stripping the instance style token from the file basename
		base = os.path.splitext(os.path.basename(self._font_path))[0] if self._font_path else ''
		first_style = selected[0]
		last_style = selected[-1]
		style_compact = compact_name(first_style, last_style)

		# Attempt to strip the first_style suffix from the file basename
		style_slug = first_style.replace(' ', '')
		if base.endswith(style_slug):
			family_base = base[: -len(style_slug)].rstrip('-_')
			computed = f'{family_base} {style_compact}' if family_base else style_compact
		else:
			computed = f'{base} {style_compact}' if base else style_compact

		self.w.nameField.set(computed.strip())

	def _refresh_generate_button(self):
		"""Enable Generate only when a font is loaded and at least one instance is selected."""
		enabled = bool(self._font_path and self._selected_instance_names())
		self.w.generateButton.enable(enabled)

	def _set_status(self, message, error=False):
		"""Update the status label. Errors are prefixed with a warning symbol."""
		text = f'⚠️  {message}' if error else message
		self.w.statusLabel.set(text)

	# ------------------------------------------------------------------
	# Public
	# ------------------------------------------------------------------

	def show(self):
		"""Bring the dialog window to the front."""
		self.w.open()
