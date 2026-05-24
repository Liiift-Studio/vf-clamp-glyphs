# plugin.py — vf-clamp Glyphs.app plugin for generating restricted variable fonts

import objc
from GlyphsApp import *
from GlyphsApp.plugins import *
import vanilla
import os
import re
from fontTools.varLib import instancer
from fontTools.ttLib import TTFont


# ---------------------------------------------------------------------------
# Core fonttools helpers
# ---------------------------------------------------------------------------

def compute_hull(font, selected_names):
	"""Compute the axis hull (min/max per axis) across the selected named instances."""
	fvar = font['fvar']
	name_table = font['name']
	all_insts = {
		name_table.getDebugName(inst.subfamilyNameID): dict(inst.coordinates)
		for inst in fvar.instances
	}
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
		# Pinned axis: pass a scalar; ranged axis: pass AxisRange
		result[tag] = lo if lo == hi else instancer.AxisRange(lo, hi)
	return result


def patch_name_table(font, family_name):
	"""Update name IDs 1, 4, 6, and optionally 16 and 25 to reflect the restricted VF."""
	ps_name = re.sub(r'[^A-Za-z0-9-]', '', family_name.replace(' ', '-'))
	name_table = font['name']
	existing_ids = {r.nameID for r in name_table.names}
	updates = {1: family_name, 4: family_name, 6: ps_name}
	if 16 in existing_ids:
		updates[16] = family_name
	if 25 in existing_ids:
		updates[25] = ps_name
	for record in name_table.names:
		if record.nameID not in updates:
			continue
		value = updates[record.nameID]
		if record.platformID == 3:
			record.string = value.encode('utf-16-be')
		elif record.platformID == 1:
			try:
				record.string = value.encode('mac_roman')
			except Exception:
				record.string = value.encode('ascii', errors='replace')


def compact_name(first, last):
	"""Strip shared word prefix/suffix — e.g. 'Encode Sans Light' + 'Encode Sans Bold' → 'Encode Sans Light-Bold'."""
	if first == last:
		return first
	fw = first.split()
	lw = last.split()
	# Count shared prefix words
	prefix_len = 0
	while prefix_len < len(fw) and prefix_len < len(lw) and fw[prefix_len] == lw[prefix_len]:
		prefix_len += 1
	# Count shared suffix words (not overlapping prefix)
	suffix_len = 0
	while (suffix_len < len(fw) - prefix_len and
		   suffix_len < len(lw) - prefix_len and
		   fw[-1 - suffix_len] == lw[-1 - suffix_len]):
		suffix_len += 1
	prefix = ' '.join(fw[:prefix_len])
	a = ' '.join(fw[prefix_len: len(fw) - suffix_len if suffix_len else None])
	b = ' '.join(lw[prefix_len: len(lw) - suffix_len if suffix_len else None])
	suffix = ' '.join(fw[len(fw) - suffix_len:]) if suffix_len else ''
	middle = f'{a}-{b}' if a and b else (a or b)
	return ' '.join(filter(None, [prefix, middle, suffix]))


def produce_restricted_vf(font_path, selected_names, family_name, output_path):
	"""Load font_path, restrict axes to the hull of selected_names, patch names, and save to output_path."""
	font = TTFont(font_path)
	hull = compute_hull(font, selected_names)
	if not hull:
		raise ValueError('No valid instances found for the selected names.')
	partial = instancer.instantiateVariableFont(font, hull)
	patch_name_table(partial, family_name)
	partial.save(output_path)


def get_instance_names(font_path):
	"""Return an ordered list of named-instance subfamily names from a variable font file."""
	font = TTFont(font_path)
	if 'fvar' not in font:
		raise ValueError('This font does not contain an fvar table — it is not a variable font.')
	name_table = font['name']
	names = []
	for inst in font['fvar'].instances:
		label = name_table.getDebugName(inst.subfamilyNameID)
		if label and label not in names:
			names.append(label)
	return names


def extension_for_format(fmt):
	"""Return the file extension for a given format label (TTF, OTF, WOFF, WOFF2)."""
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
		"""Register the menu item under Script > vf-clamp."""
		newMenuItem = NSMenuItem.new()
		newMenuItem.setTitle_('Generate Restricted VFs…')
		newMenuItem.setAction_(self.showDialog_)
		newMenuItem.setTarget_(self)
		Glyphs.menu[SCRIPT_MENU].append(newMenuItem)

	@objc.python_method
	def showDialog_(self, sender):
		"""Open the vf-clamp dialog window."""
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
_W = 480          # window width
_PAD = 16         # outer padding
_LABEL_H = 20     # standard label height
_FIELD_H = 22     # standard input field height
_BTN_H = 24       # button height
_ROW = 28         # vertical rhythm per row


class VFClampDialog:
	"""Vanilla dialog for selecting named instances and generating a restricted VF."""

	def __init__(self):
		"""Initialise state; the window is not shown until show() is called."""
		self._font_path = None
		self._instance_names = []  # ordered list from fvar
		self._checks = []          # vanilla.CheckBox widgets, one per instance

		# Start y position (top-down inside vanilla's coordinate system = bottom-up in AppKit,
		# so vanilla auto-sizes; we track a running y for *posSize* top-offsets)
		self._build_window()

	# ------------------------------------------------------------------
	# Window construction
	# ------------------------------------------------------------------

	def _build_window(self):
		"""Build the full dialog layout with vanilla widgets."""
		# We calculate a generous fixed height; the window is not resizable.
		# We'll add instance checkboxes dynamically after font load.
		w = _W
		h = 560  # initial estimate; resized after font load

		self.w = vanilla.FloatingWindow(
			(w, h),
			'vf-clamp — Generate Restricted VFs',
			minSize=(w, 300),
			maxSize=(w, 900),
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

		# Divider (drawn as a Box with height=1)
		win.divider1 = vanilla.HorizontalLine((_PAD, y, -_PAD, 1))
		y += 12

		# --- Instance list (populated after font load) ---
		win.instanceLabel = vanilla.TextBox((_PAD, y, -_PAD, _LABEL_H), 'Named Instances:')
		y += _LABEL_H + 6

		# Placeholder message shown before a font is loaded
		win.instancePlaceholder = vanilla.TextBox(
			(_PAD, y, -_PAD, _LABEL_H * 2),
			'Open a variable font to see its named instances.',
			sizeStyle='small',
		)
		# Store start-y for dynamic checkbox area
		self._checks_top_y = y
		self._checks_area_height = 0  # updated after font load
		y += _LABEL_H * 2 + 8

		win.divider2 = vanilla.HorizontalLine((_PAD, y, -_PAD, 1))
		y += 12
		self._controls_top_y = y  # everything below moves down after font load

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

		# --- Generate button ---
		win.generateButton = vanilla.Button(
			(-_PAD - 120, y - 1, -_PAD, _BTN_H),
			'Generate',
			callback=self._on_generate,
		)
		win.generateButton.enable(False)

		# --- Status label ---
		win.statusLabel = vanilla.TextBox(
			(_PAD, y + 4, -140, _LABEL_H),
			'',
			sizeStyle='small',
		)
		y += _ROW

		# Resize window to fit content
		self._static_bottom_height = y + _PAD
		win.resize(w, self._static_bottom_height)

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
		"""Track whether the user has manually overridden the auto-computed output name."""
		self._name_overridden = bool(self.w.nameField.get().strip())

	def _on_check_toggled(self, sender):
		"""Update output name and Generate button state whenever an instance is toggled."""
		self._refresh_name()
		self._refresh_generate_button()

	def _on_generate(self, sender):
		"""Read UI values and call produce_restricted_vf; report success or failure."""
		selected = self._selected_instance_names()
		if not selected:
			self._set_status('Select at least one named instance.', error=True)
			return

		family_name = self.w.nameField.get().strip()
		if not family_name:
			self._set_status('Enter an output name.', error=True)
			return

		fmt = self.w.formatPopup.getItem()
		ext = extension_for_format(fmt)

		# Determine output folder
		folder = self.w.folderField.get().strip()
		if not folder:
			folder = os.path.dirname(self._font_path) if self._font_path else os.path.expanduser('~/Desktop')

		# Build output filename: sanitize family_name for filesystem
		safe_name = re.sub(r'[/\\:*?"<>|]', '-', family_name)
		output_path = os.path.join(folder, safe_name + ext)

		self._set_status('Generating…')
		try:
			produce_restricted_vf(self._font_path, selected, family_name, output_path)
			self._set_status(f'Saved: {output_path}')
		except Exception as e:
			self._set_status(f'Error: {e}', error=True)

	# ------------------------------------------------------------------
	# Font loading
	# ------------------------------------------------------------------

	def _load_font(self, path):
		"""Parse the font at path, populate the instance checkbox list, and refresh UI."""
		self._set_status('Loading…')
		try:
			names = get_instance_names(path)
		except Exception as e:
			self._set_status(f'Error loading font: {e}', error=True)
			return

		self._font_path = path
		self._instance_names = names
		self.w.fontPathField.set(path)
		self._set_status('')
		self._populate_instance_checks(names)

		# Auto-fill output folder to font's directory
		if not self.w.folderField.get().strip():
			self.w.folderField.set(os.path.dirname(path))

		self._refresh_name()
		self._refresh_generate_button()

	def _populate_instance_checks(self, names):
		"""Dynamically add one CheckBox per named instance, replacing the placeholder."""
		# Remove old checkboxes if any
		for cb in self._checks:
			try:
				delattr(self.w, cb._name_attr)
			except Exception:
				pass
		self._checks = []

		# Hide placeholder
		self.w.instancePlaceholder.set('')

		check_h = _LABEL_H + 2
		area_height = len(names) * (check_h + 4) + 8

		for idx, name in enumerate(names):
			y = self._checks_top_y + idx * (check_h + 4)
			attr_name = f'_check_{idx}'
			cb = vanilla.CheckBox(
				(_PAD + 8, y, -_PAD, check_h),
				name,
				value=False,
				callback=self._on_check_toggled,
			)
			# Store the attribute name so we can remove it later
			cb._name_attr = attr_name
			setattr(self.w, attr_name, cb)
			self._checks.append(cb)

		self._checks_area_height = area_height

		# Resize window to accommodate instance list
		new_h = self._checks_top_y + area_height + (self._static_bottom_height - self._checks_top_y - _LABEL_H * 2 - 8)
		self.w.resize(_W, max(400, new_h))

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _selected_instance_names(self):
		"""Return a list of instance name strings whose checkbox is ticked."""
		selected = []
		for idx, name in enumerate(self._instance_names):
			cb = getattr(self.w, f'_check_{idx}', None)
			if cb and cb.get():
				selected.append(name)
		return selected

	def _refresh_name(self):
		"""Auto-compute output name from first and last selected instance (unless overridden)."""
		if getattr(self, '_name_overridden', False):
			return
		selected = self._selected_instance_names()
		if not selected:
			self.w.nameField.set('')
			return
		# Derive base family name by stripping the first instance style from the font basename
		base = os.path.splitext(os.path.basename(self._font_path))[0] if self._font_path else ''
		# Use compact_name on the style tokens, prepend the shared base
		first_style = selected[0]
		last_style = selected[-1]
		style_compact = compact_name(first_style, last_style)
		# Try to build "FamilyName StyleCompact" by stripping the first_style suffix from base
		if base.endswith(first_style.replace(' ', '')):
			family_base = base[: -len(first_style.replace(' ', ''))].rstrip('-_')
			computed = f'{family_base} {style_compact}' if family_base else style_compact
		else:
			computed = f'{base} {style_compact}' if base else style_compact
		self.w.nameField.set(computed.strip())

	def _refresh_generate_button(self):
		"""Enable the Generate button only when a font is loaded and ≥1 instance is selected."""
		enabled = bool(self._font_path and self._selected_instance_names())
		self.w.generateButton.enable(enabled)

	def _set_status(self, message, error=False):
		"""Update the status label; errors are prefixed with '⚠ '."""
		if error:
			self.w.statusLabel.set(f'⚠️  {message}')
		else:
			self.w.statusLabel.set(message)

	# ------------------------------------------------------------------
	# Public
	# ------------------------------------------------------------------

	def show(self):
		"""Bring the dialog window to the front."""
		self._name_overridden = False
		self.w.open()
