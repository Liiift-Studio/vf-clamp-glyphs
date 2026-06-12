# core.py — framework-agnostic helpers for vf-clamp Glyphs plugin.
# Importable outside Glyphs.app for unit testing; depends only on fontTools + stdlib.

import contextlib
import copy
import os
import re
import shutil
import tempfile
import unicodedata

try:
	from fontTools.varLib import instancer
	from fontTools.ttLib import TTFont
	import fontTools
	_FONTTOOLS_AVAILABLE = True
	_FONTTOOLS_IMPORT_ERROR = None
except ImportError as _err:
	_FONTTOOLS_AVAILABLE = False
	_FONTTOOLS_IMPORT_ERROR = str(_err)
	fontTools = None  # type: ignore

# Minimum fontTools version required for instancer.AxisTriple
MIN_FONTTOOLS_VERSION = (4, 34, 0)

# Maximum bytes for a font we will parse (safeguard against crafted/oversize input)
MAX_FONT_BYTES = 64 * 1024 * 1024  # 64 MB

# Public capability helpers — preferred over reaching into the underscore-
# prefixed module-level flags. Subclasses/consumers that import these get a
# stable API surface even if the internal flag naming changes.
def is_fonttools_ready():
	"""Return True when fontTools is importable and new enough for instancer."""
	if not _FONTTOOLS_AVAILABLE:
		return False
	try:
		check_fonttools_version()
		return True
	except RuntimeError:
		return False


def fonttools_import_error():
	"""Return the fontTools import-error string, or None when importable."""
	return _FONTTOOLS_IMPORT_ERROR


def is_glyphs_app_available():
	"""Return True when running inside Glyphs.app with the GlyphsApp Python API."""
	return _GLYPHS_AVAILABLE


def open_font_safely(font_path):
	"""Open a TTFont after running size/sanity checks. Public alias of _safe_open_font."""
	return _safe_open_font(font_path)


# Precompiled regexes (avoid per-call recompilation)
_PS_NAME_RE = re.compile(r'[^A-Za-z0-9-]')
_FS_RESERVED_RE = re.compile(r'[/\\:*?"<>|]')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
_UNICODE_DIRECTIONAL_RE = re.compile(
	'[‪-‮⁦-⁩​-‏﻿]'
)
_TRIM_DOTS_SPACES_RE = re.compile(r'^[. ]+|[. ]+$')


def check_fonttools_version():
	"""Raise RuntimeError if fontTools is missing or too old for instancer.AxisTriple.

	Returns the parsed version tuple on success.
	"""
	if not _FONTTOOLS_AVAILABLE:
		raise RuntimeError(
			f'fontTools is not available: {_FONTTOOLS_IMPORT_ERROR}'
		)
	raw = getattr(fontTools, '__version__', '0.0.0')
	# Parse leading 'X.Y.Z' segments
	parts = re.match(r'(\d+)\.(\d+)\.?(\d+)?', raw)
	if not parts:
		return (0, 0, 0)
	version = (
		int(parts.group(1)),
		int(parts.group(2)),
		int(parts.group(3) or 0),
	)
	if version < MIN_FONTTOOLS_VERSION:
		raise RuntimeError(
			f'fontTools >= {".".join(str(v) for v in MIN_FONTTOOLS_VERSION)} '
			f'is required for axis instancing; found {raw}.'
		)
	return version


def sanitize_filename(name, fallback='font'):
	"""Sanitise a string for safe use as a filename across macOS and Windows.

	Strips path separators, Windows-reserved characters, control characters,
	Unicode directional overrides/joiners, and trailing dots and spaces.
	Returns ``fallback`` if the result is empty.
	"""
	if not name:
		return fallback
	# Normalise to remove combining oddities
	cleaned = unicodedata.normalize('NFC', name)
	cleaned = _FS_RESERVED_RE.sub('-', cleaned)
	cleaned = _CONTROL_RE.sub('', cleaned)
	cleaned = _UNICODE_DIRECTIONAL_RE.sub('', cleaned)
	cleaned = _TRIM_DOTS_SPACES_RE.sub('', cleaned)
	# Cap length (keep room for extension)
	cleaned = cleaned[:200].strip()
	return cleaned or fallback


def sanitize_ps_name(name, max_len=63):
	"""Sanitise a PostScript name (nameID 6).

	Restricts to ASCII alphanumerics and hyphen; collapses runs of hyphens;
	enforces the PostScript 63-byte length cap; strips leading/trailing
	hyphens. Returns 'Font' if the result is empty.
	"""
	if not name:
		return 'Font'
	collapsed = _PS_NAME_RE.sub('', name.replace(' ', '-'))
	collapsed = re.sub(r'-+', '-', collapsed).strip('-')
	collapsed = collapsed[:max_len]
	return collapsed or 'Font'


def compact_name(first, last):
	"""Strip shared word prefix/suffix from two style names and join with a hyphen.

	Canonical TypeScript implementation: @liiift-studio/vf-clamp src/core/utils.ts
	compactName(). Mirrors are kept in vf-clamp-robofont and vf-clamp-vscode.

	Examples:
	  'Encode Sans Light' + 'Encode Sans Bold'  -> 'Encode Sans Light-Bold'
	  'Light'                                    -> 'Light'   (single instance)
	  'Regular' + 'Regular'                      -> 'Regular'
	"""
	if first == last:
		return first
	fw = first.split()
	lw = last.split()

	prefix_len = 0
	while (
		prefix_len < len(fw)
		and prefix_len < len(lw)
		and fw[prefix_len] == lw[prefix_len]
	):
		prefix_len += 1

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


def compute_default_output_name(basename, first_style, last_style):
	"""Compute the auto-filled output name from a font basename and selected styles.

	Strips a trailing first-style slug (e.g. ``EncodeSans-Light`` + first=``Light``
	-> ``EncodeSans``) and joins with a compact style range.
	Returns the basename alone if no useful family base can be derived.
	"""
	if not basename:
		basename = ''
	style_compact = compact_name(first_style, last_style)
	style_slug = first_style.replace(' ', '')
	if basename.endswith(style_slug):
		family_base = basename[: -len(style_slug)].rstrip('-_')
	else:
		family_base = basename
	if family_base:
		return f'{family_base} {style_compact}'.strip()
	return style_compact


def extension_for_format(fmt):
	"""Return the file extension for a format label (TTF, OTF, WOFF, WOFF2, GLYPHS)."""
	return {
		'TTF': '.ttf',
		'OTF': '.otf',
		'WOFF': '.woff',
		'WOFF2': '.woff2',
		'GLYPHS': '.glyphs',
	}.get(str(fmt).upper(), '.ttf')


def flavor_for_format(fmt):
	"""Return the fontTools flavor string for WOFF/WOFF2, or None for raw sfnt."""
	f = str(fmt).upper()
	if f == 'WOFF':
		return 'woff'
	if f == 'WOFF2':
		return 'woff2'
	return None


def _disambiguated_instance_labels(name_table, fvar_instances):
	"""Yield (instance, disambiguated_label) using the same '#N' suffix logic as get_instance_names."""
	seen = {}
	for inst in fvar_instances:
		label = name_table.getDebugName(inst.subfamilyNameID)
		if not label:
			continue
		if label in seen:
			seen[label] += 1
			yield inst, f'{label} #{seen[label]}'
		else:
			seen[label] = 1
			yield inst, label


def get_axis_hull_from_instances(font, selected_names):
	"""Return per-axis ``(min, max)`` covering every selected named instance.

	Pure helper around a TTFont. Returns ``{axis_tag: [lo, hi]}``.
	Raises ValueError if there is no fvar table or no selected name matches.

	Applies the same ``' #N'`` disambiguation as :func:`get_instance_names` so
	that selecting a duplicated subfamily name through the UI matches the
	correct fvar instance rather than producing an empty hull.
	"""
	if 'fvar' not in font:
		raise ValueError('This font has no fvar table — it is not a variable font.')

	fvar = font['fvar']
	name_table = font['name']

	selected_set = set(selected_names)
	hull = {}
	matched = 0
	for inst, label in _disambiguated_instance_labels(name_table, fvar.instances):
		if label not in selected_set:
			continue
		matched += 1
		for tag, val in dict(inst.coordinates).items():
			if tag not in hull:
				hull[tag] = [val, val]
			else:
				hull[tag][0] = min(hull[tag][0], val)
				hull[tag][1] = max(hull[tag][1], val)
	if matched == 0:
		raise ValueError('No matching named instances found for the selected names.')
	return hull


def compute_hull(font, selected_names):
	"""Compute per-axis constraints suitable for ``instancer.instantiateVariableFont``.

	Returns a dict mapping axis tag -> scalar (pin) or ``instancer.AxisTriple``.
	When a range is needed, the default value is anchored to the source default
	if it lies inside the range, otherwise clamped to the nearest range edge
	(current fontTools requires a numeric default).
	Axes shared by every selected instance but not varied are pinned to the value.
	Note: fvar named instances always carry coordinates for every axis, so in
	practice every axis appears in the returned dict — selecting fewer instances
	does NOT leave axes unrestricted.
	"""
	check_fonttools_version()
	raw_hull = get_axis_hull_from_instances(font, selected_names)
	fvar = font['fvar']
	axis_defaults = {ax.axisTag: ax.defaultValue for ax in fvar.axes}

	result = {}
	for tag, (lo, hi) in raw_hull.items():
		if lo == hi:
			result[tag] = lo
		else:
			default = axis_defaults.get(tag, lo)
			anchored = max(lo, min(hi, default))
			result[tag] = instancer.AxisTriple(lo, anchored, hi)
	return result


def filter_fvar_instances(font, selected_names):
	"""Drop fvar.instances whose disambiguated subfamily name is not in ``selected_names``.

	No-op when ``fvar`` is absent. Uses the same ``' #N'`` disambiguation as
	:func:`get_instance_names` so duplicate-named instances are matched
	correctly.
	"""
	if 'fvar' not in font:
		return
	fvar = font['fvar']
	name_table = font['name']
	selected_set = set(selected_names)
	kept = []
	for inst, label in _disambiguated_instance_labels(name_table, fvar.instances):
		if label in selected_set:
			kept.append(inst)
	fvar.instances = kept


def prune_stat_axis_values(font, hull):
	"""Remove STAT AxisValue records that fall outside ``hull``.

	Only handles AxisValueFormat 1 (single value) and 3 (linked); other formats
	are left intact. No-op when STAT is absent.
	"""
	if 'STAT' not in font:
		return
	stat = font['STAT'].table
	axis_records = getattr(stat, 'DesignAxisRecord', None)
	if axis_records is None or not getattr(stat, 'AxisValueArray', None):
		return
	tag_for_index = [ax.AxisTag for ax in axis_records.Axis]

	kept = []
	for av in stat.AxisValueArray.AxisValue:
		fmt = getattr(av, 'Format', None)
		axis_idx = getattr(av, 'AxisIndex', None)
		if axis_idx is None or axis_idx >= len(tag_for_index):
			kept.append(av)
			continue
		tag = tag_for_index[axis_idx]
		constraint = hull.get(tag)
		if constraint is None:
			kept.append(av)
			continue
		if isinstance(constraint, tuple):
			lo, hi = constraint[0], constraint[-1]
		else:
			lo = hi = constraint
		if fmt in (1, 3):
			val = getattr(av, 'Value', None)
			if val is None or lo <= val <= hi:
				kept.append(av)
			continue
		# Unknown format — keep
		kept.append(av)
	stat.AxisValueArray.AxisValue = kept
	stat.AxisValueCount = len(kept)


def patch_name_table(font, family_name):
	"""Update name IDs 1, 4, 6, 16, 17, 25 to reflect the restricted VF.

	Both Windows (platformID=3, UTF-16-BE) and Mac (platformID=1, mac_roman)
	records for English (langID 0x0409 / 0) are updated. Non-English localised
	records for these IDs are removed to avoid stale name leakage.
	mac_roman records that cannot encode the family name are dropped.
	"""
	ps_name = sanitize_ps_name(family_name)
	# Heuristic subfamily fallback so Full Name (1+2) and Typo Full (16+17) stay coherent
	subfamily_fallback = 'Regular'

	name_table = font['name']
	existing_ids = {r.nameID for r in name_table.names}

	updates = {
		1: family_name,
		4: f'{family_name} {subfamily_fallback}'.strip(),
		6: ps_name,
	}
	if 16 in existing_ids:
		updates[16] = family_name
		# Pair nameID 17 with nameID 16 to satisfy Windows GDI requirements
		updates[17] = subfamily_fallback
	if 25 in existing_ids:
		# Variations PostScript Name Prefix recommends <=27 chars, no trailing '-'
		updates[25] = ps_name[:27].rstrip('-') or 'Font'

	english_lang_ids = {0, 0x0409}  # Mac English, Windows en-US

	# Filter out non-English records for the IDs we're rewriting
	pruned = []
	for record in name_table.names:
		if record.nameID in updates and record.langID not in english_lang_ids:
			continue
		pruned.append(record)
	name_table.names = pruned

	updated = set()
	for record in name_table.names:
		if record.nameID not in updates:
			continue
		value = updates[record.nameID]
		if record.platformID == 3:
			record.string = value.encode('utf-16-be')
			updated.add((record.nameID, 3))
		elif record.platformID == 1:
			try:
				record.string = value.encode('mac_roman')
				updated.add((record.nameID, 1))
			except (UnicodeEncodeError, LookupError):
				# Drop unencodable mac record rather than ship literal '?' chars
				pass
	# Strip mac records we couldn't re-encode
	name_table.names = [
		r for r in name_table.names
		if not (
			r.nameID in updates
			and r.platformID == 1
			and (r.nameID, 1) not in updated
		)
	]

	# Ensure at least one Windows record exists per updated nameID
	for name_id, value in updates.items():
		if (name_id, 3) not in updated:
			name_table.setName(value, name_id, 3, 1, 0x0409)


class FontParseError(ValueError):
	"""Raised when fontTools cannot parse a candidate font file.

	Distinguished from ``OSError`` so callers narrowing on I/O can avoid
	swallowing parse failures (and vice versa). Inherits from ``ValueError``
	because parse-failure is fundamentally a bad-input condition.
	"""


def _safe_open_font(font_path):
	"""Open a TTFont after running basic safety checks (size cap, fvar required).

	Raises:
	  ``OSError`` for I/O failures (missing file, permission denied, oversize).
	  ``FontParseError`` for content failures (corrupt bytes, unknown sfnt
	  flavour, invalid table). ``FontParseError`` is a subclass of
	  ``ValueError`` for back-compat with callers narrowing on ``ValueError``.
	"""
	try:
		size = os.path.getsize(font_path)
	except OSError as e:
		raise OSError(f'Cannot stat font file: {e}') from e
	if size > MAX_FONT_BYTES:
		raise ValueError(
			f'Font file is too large to process safely '
			f'({size} bytes > {MAX_FONT_BYTES} byte cap).'
		)
	try:
		return TTFont(font_path)
	except (IOError, OSError) as e:
		raise OSError(f'Cannot open font file: {e}') from e
	except Exception as e:  # fontTools.ttLib.TTLibError, parsing errors
		# Raise a dedicated FontParseError so callers can distinguish a
		# corrupt font from a missing file. (Subclass of ValueError so
		# legacy `except ValueError` paths continue to catch it.)
		raise FontParseError(f'Failed to parse font file: {e}') from e


def get_instance_names(font_path):
	"""Return an ordered list of named-instance subfamily names from a font file."""
	check_fonttools_version()
	with contextlib.closing(_safe_open_font(font_path)) as font:
		if 'fvar' not in font:
			raise ValueError(
				'This font has no variable axes — select a variable font with an fvar table.'
			)
		name_table = font['name']
		names = []
		seen = {}
		for inst in font['fvar'].instances:
			label = name_table.getDebugName(inst.subfamilyNameID)
			if not label:
				continue
			if label in seen:
				# Disambiguate duplicate names with a #N suffix preserving original ordering
				seen[label] += 1
				names.append(f'{label} #{seen[label]}')
			else:
				seen[label] = 1
				names.append(label)
		return names


def safe_output_path(folder, family_name, ext):
	"""Return an output path that is guaranteed to live inside ``folder``.

	Sanitises ``family_name``, joins with ``folder``, resolves and rejects any
	path that would escape the chosen folder via traversal or absolute input.
	If the target exists, appends ``-1``, ``-2``, ... before the extension.

	Uses ``os.path.realpath`` rather than ``os.path.abspath`` so symlink
	resolution mismatches on macOS (``/var`` vs ``/private/var``) cannot
	smuggle a traversal past the ``commonpath`` containment check.
	"""
	safe = sanitize_filename(family_name)
	if not folder:
		folder = os.path.expanduser('~/Desktop')
	folder = os.path.realpath(folder)
	candidate = os.path.realpath(os.path.join(folder, safe + ext))
	# Refuse to escape the chosen folder. commonpath raises ValueError on
	# cross-drive paths on Windows; treat that as a containment failure too.
	try:
		contained = os.path.commonpath([candidate, folder]) == folder
	except ValueError:
		contained = False
	if not contained:
		candidate = os.path.join(folder, sanitize_filename('font') + ext)
	# Auto-suffix on collision
	if os.path.exists(candidate):
		base, e = os.path.splitext(candidate)
		i = 1
		while os.path.exists(f'{base}-{i}{e}'):
			i += 1
		candidate = f'{base}-{i}{e}'
	return candidate


def _unlink_quiet(path):
	"""Delete ``path`` if present; swallow OSError (best-effort cleanup)."""
	try:
		if path and os.path.exists(path):
			os.unlink(path)
	except OSError:
		pass


def produce_restricted_vf(font_path, selected_names, family_name, output_path, fmt='TTF'):
	"""Load ``font_path``, restrict axes to the hull of ``selected_names``,
	patch names, prune STAT/fvar instances, set the WOFF flavor if requested,
	and save to ``output_path``.

	Raises ValueError for bad input, OSError for I/O failures, RuntimeError for
	font-engine failures.
	"""
	check_fonttools_version()
	with contextlib.closing(_safe_open_font(font_path)) as font:
		if 'fvar' not in font:
			raise ValueError('This font has no variable axes — it is not a variable font.')

		hull = compute_hull(font, selected_names)
		if not hull:
			raise ValueError('No valid named instances found for the selected names.')

		# Run instancer (handles avar, HVAR/MVAR/VVAR, OS/2 fsSelection updates internally)
		# We catch Exception here because fontTools' instancer can raise many
		# different internal exception types (TTLibError, KeyError, ValueError
		# on bad axis math, etc.); they all map to the same user-facing condition.
		try:
			partial = instancer.instantiateVariableFont(font, hull)
		except Exception as e:
			raise RuntimeError(f'instancer failed: {e}') from e

		# Filter fvar named instances so the output advertises only the licensed range
		filter_fvar_instances(partial, selected_names)
		prune_stat_axis_values(partial, hull)
		patch_name_table(partial, family_name)

		flavor = flavor_for_format(fmt)
		if flavor is not None:
			# WOFF/WOFF2 require fontTools to write a compressed wrapper.
			# 'woff2' additionally requires the brotli package at runtime.
			partial.flavor = flavor

		output_dir = os.path.dirname(output_path)
		if output_dir:
			os.makedirs(output_dir, exist_ok=True)

		try:
			partial.save(output_path)
		except (IOError, OSError) as e:
			# Best-effort cleanup of a half-written file so the user doesn't
			# end up with a corrupt .ttf masquerading as a valid one.
			_unlink_quiet(output_path)
			raise OSError(f'Failed to save output font: {e}') from e
		except Exception as e:  # brotli missing for WOFF2, etc.
			_unlink_quiet(output_path)
			raise RuntimeError(f'Failed to save output font: {e}') from e


# ---------------------------------------------------------------------------
# GSFont-side clamping — operates on Glyphs.app source files (.glyphs).
# These helpers only resolve when the GlyphsApp Python module is importable,
# which is true inside Glyphs but not in standalone unit-test runs.
# ---------------------------------------------------------------------------

try:
	from GlyphsApp import (
		Glyphs,
		GSInstance,
		INSTANCETYPEVARIABLE,
		PLAIN,
		WOFF,
		WOFF2,
	)
	_GLYPHS_AVAILABLE = True
	_GLYPHS_IMPORT_ERROR = None
except ImportError as _gerr:
	_GLYPHS_AVAILABLE = False
	_GLYPHS_IMPORT_ERROR = str(_gerr)
	Glyphs = None  # type: ignore
	GSInstance = None  # type: ignore
	INSTANCETYPEVARIABLE = 1  # type: ignore  (fallback constant)
	PLAIN = WOFF = WOFF2 = None  # type: ignore


def _is_variable_instance(inst):
	"""Return True for a Variable Font Setting instance (not a static named instance)."""
	return getattr(inst, 'type', 0) == INSTANCETYPEVARIABLE


def list_open_glyphs_fonts():
	"""Return the list of open ``GSFont`` documents, or [] when Glyphs is unavailable."""
	if not _GLYPHS_AVAILABLE:
		return []
	try:
		return list(Glyphs.fonts)
	except Exception:
		return []


def gsfont_label(gsfont):
	"""Return a short, human-readable label for a GSFont (used in PopUp lists)."""
	family = getattr(gsfont, 'familyName', '') or 'Untitled'
	doc_path = ''
	try:
		doc_path = gsfont.filepath or ''
	except (AttributeError, OSError):
		# Glyphs may not have a filepath set, or filesystem access may fail.
		pass
	if doc_path:
		base = os.path.basename(doc_path)
		return f'{family}  ({base})'
	return family


def gsfont_instance_names(gsfont):
	"""Return ordered names of *static* named instances in a GSFont (skips Variable Font Settings)."""
	out = []
	for inst in gsfont.instances:
		if _is_variable_instance(inst):
			continue
		name = (inst.name or '').strip()
		if name:
			out.append(name)
	return out


def compute_gsfont_hull(gsfont, selected_names):
	"""Compute the per-axis hull from selected GSInstance coordinates.

	Returns ``dict[axis_tag, (lo, hi)]`` in design units. Axes that are absent
	from the font are skipped. Variable Font Setting instances are ignored.
	"""
	selected_set = set(selected_names)
	axis_tags = [getattr(ax, 'axisTag', '') or '' for ax in gsfont.axes]
	hull = {}
	for inst in gsfont.instances:
		if _is_variable_instance(inst):
			continue
		if (inst.name or '') not in selected_set:
			continue
		coords = list(inst.axes)
		for tag, val in zip(axis_tags, coords):
			if not tag:
				continue
			if tag not in hull:
				hull[tag] = [val, val]
			else:
				hull[tag][0] = min(hull[tag][0], val)
				hull[tag][1] = max(hull[tag][1], val)
	return {tag: (lo, hi) for tag, (lo, hi) in hull.items()}


def _deepcopy_gsfont(gsfont):
	"""Return a true deep clone of ``gsfont`` that does not share state with the source.

	Glyphs 3's ``GSFont.copy()`` inherits NSObject's NSCopying, which is a
	shallow copy — masters/instances/glyphs collections still reference the
	same Obj-C objects as the source. Mutating ``new_font.masters`` then bleeds
	into the user's open document. The reliable Glyphs-side deep-clone path is
	save-to-temp-file then re-open: this serialises the entire state and re-
	parses it into fresh Obj-C objects.

	In CI (where ``_GLYPHS_AVAILABLE`` is False) the input is a Python fake
	that supports ``copy.deepcopy`` directly — we use that as the fallback.
	"""
	# When Glyphs is not available or the input is a Python fake (no .save
	# method bound to the GSFont scripting API), fall back to copy.deepcopy.
	if not _GLYPHS_AVAILABLE or not callable(getattr(gsfont, 'save', None)) or Glyphs is None:
		return copy.deepcopy(gsfont)
	# Try the save-and-reopen round-trip first. This is what Glyphs' own
	# scripting cookbook recommends for a true deep clone.
	tmp_dir = tempfile.mkdtemp(prefix='vfclamp-clone-')
	tmp_path = os.path.join(tmp_dir, 'clone.glyphs')
	try:
		# makeCopy=True writes a new file without changing the source's
		# tracked filepath or flipping its dirty flag.
		gsfont.save(path=tmp_path, formatVersion=3, makeCopy=True)
		try:
			reopened = Glyphs.open(tmp_path, showInterface=False)
		except TypeError:
			reopened = Glyphs.open(tmp_path)
		if reopened is None:
			raise RuntimeError('Could not reopen temp clone of GSFont')
		return reopened
	finally:
		# Best-effort cleanup — Glyphs may still hold the file handle until
		# the reopened document is closed by the caller, but the temp file
		# is no longer needed for the clone itself.
		try:
			shutil.rmtree(tmp_dir, ignore_errors=True)
		except OSError:
			pass


def clamp_gsfont(gsfont, selected_instance_names, output_family_name):
	"""Return a clamped *copy* of ``gsfont``.

	The source font is never mutated. The returned ``GSFont`` has:

	* every instance not in ``selected_instance_names`` removed (Variable Font
	  Setting entries are retained — they describe how to export a VF);
	* every master whose coordinates fall outside the hull of the selected
	  instances on any axis removed;
	* any axis whose hull collapses to a single value removed entirely (so a
	  single-instance selection produces a static single-master font);
	* ``familyName`` rewritten to ``output_family_name``.
	"""
	if not _GLYPHS_AVAILABLE:
		raise RuntimeError(
			f'GlyphsApp Python API not available: {_GLYPHS_IMPORT_ERROR}'
		)

	selected_set = set(selected_instance_names)
	if not selected_set:
		raise ValueError('No instances selected for clamp')

	hull = compute_gsfont_hull(gsfont, selected_instance_names)
	if not hull:
		raise ValueError('Selected instances yielded an empty axis hull')

	# Deep-clone the source font so mutations never leak back into the user's
	# open document. GSFont's NSCopying conformance is a shallow copy in
	# Glyphs 3 (inherited from NSObject — the Python wrapper does not override
	# it to deep-copy masters/instances/glyphs), so calling gsfont.copy()
	# would still share collection elements with the source. We round-trip
	# via the canonical Glyphs save-to-temp-file-and-reopen path when
	# available, falling back to copy.deepcopy() for the unit-test fake.
	new_font = _deepcopy_gsfont(gsfont)
	axis_tags = [getattr(ax, 'axisTag', '') or '' for ax in new_font.axes]

	# 1. Drop unselected named instances (keep Variable Font Settings).
	new_font.instances = [
		inst for inst in new_font.instances
		if _is_variable_instance(inst) or (inst.name or '') in selected_set
	]

	# 2. Drop masters whose coords lie outside the hull on any axis. For pure
	# pin selections (hull fully collapses to a point) we tolerate a small
	# floating-point delta — a master at (399.9999) vs an instance at (400) is
	# still the correct master to keep.
	_PIN_EPSILON = 1e-6
	all_collapsed = all(lo == hi for (lo, hi) in hull.values())
	surviving = []
	for master in list(new_font.masters):
		coords = list(master.axes)
		inside = True
		for tag, val in zip(axis_tags, coords):
			rng = hull.get(tag)
			if rng is None:
				continue
			lo, hi = rng
			if val < lo - _PIN_EPSILON or val > hi + _PIN_EPSILON:
				inside = False
				break
		if inside:
			surviving.append(master)
	if not surviving:
		# Tailor the message to whether the user picked a single pin or a
		# multi-instance range — the recovery is different in each case.
		if all_collapsed:
			raise RuntimeError(
				'The selected instance does not coincide with any existing master. '
				'vf-clamp does not interpolate new masters — add a master at the '
				'selected instance coordinates in Glyphs, or pick an instance '
				'whose coordinates already match a master.'
			)
		raise RuntimeError(
			'No masters fall within the hull of the selected instances. '
			'vf-clamp cannot reconstruct the design space from instances alone — '
			'pick at least two instances whose coordinates span existing masters.'
		)
	new_font.masters = surviving

	# 3. Drop axes that collapsed to a single coordinate.
	collapsed = {tag for tag, (lo, hi) in hull.items() if lo == hi}
	if collapsed:
		keep_idx = [i for i, tag in enumerate(axis_tags) if tag not in collapsed]
		# Trim the axis-coordinate arrays on EVERY instance — including
		# Variable Font Setting entries — so they stay structurally parallel
		# to new_font.axes. Skipping VF Settings would leave them with N
		# entries while the font advertises N-1 axes, desyncing Glyphs'
		# export pipeline (Variable Font Setting referencing a phantom axis).
		for master in new_font.masters:
			master.axes = [master.axes[i] for i in keep_idx]
		for inst in new_font.instances:
			inst.axes = [inst.axes[i] for i in keep_idx]
		new_font.axes = [ax for i, ax in enumerate(new_font.axes) if i in keep_idx]

	# 4. Rewrite the family name (analogue of the OpenType name-table patch).
	new_font.familyName = output_family_name

	return new_font


def save_gsfont_to_glyphs(gsfont, output_path, format_version=None):
	"""Save a (clamped) GSFont to a ``.glyphs`` file without affecting the open document set.

	When ``format_version`` is None the output inherits the source font's own
	``formatVersion`` (so a Glyphs 2 source produces a Glyphs 2 file rather than
	being silently upgraded to Glyphs 3 schema). Callers can pass an explicit
	integer to override.
	"""
	if format_version is None:
		# Inherit the source font's format version; default to 3 if unknown.
		format_version = getattr(gsfont, 'formatVersion', None) or 3
	# makeCopy=True writes a new file without changing the font's tracked file path.
	gsfont.save(path=output_path, formatVersion=format_version, makeCopy=True)


def _container_for_format(fmt):
	"""Map a 'TTF'/'OTF'/'WOFF'/'WOFF2' string to a Glyphs export container constant."""
	if not _GLYPHS_AVAILABLE:
		return None
	f = (fmt or '').upper()
	if f == 'WOFF':
		return WOFF
	if f == 'WOFF2':
		return WOFF2
	return PLAIN  # TTF and OTF both use the PLAIN container


def _outline_format_for(fmt):
	"""Map a UI format string to the outline format Glyphs.generate() expects."""
	f = (fmt or '').upper()
	if f == 'TTF':
		return 'TTF'
	# OTF / WOFF / WOFF2 — Glyphs always writes the OTF outline; the WOFF
	# wrappers are applied via the containers parameter.
	return 'OTF'


def export_gsfont_binary_via_glyphs(clamped_font, output_path, fmt):
	"""Export a clamped GSFont as a Variable Font binary using Glyphs' own compiler.

	Strategy: save the clamped font to a temp ``.glyphs`` file, open it
	headlessly in Glyphs, add a Variable Font Setting if none exists, call
	``GSInstance.generate(...)`` to produce the binary, then close and clean up.

	The output is moved/renamed to ``output_path``.
	"""
	if not _GLYPHS_AVAILABLE:
		raise RuntimeError(
			f'GlyphsApp Python API not available: {_GLYPHS_IMPORT_ERROR}'
		)

	container = _container_for_format(fmt)
	outline_fmt = _outline_format_for(fmt)
	output_dir = os.path.dirname(output_path) or '.'
	os.makedirs(output_dir, exist_ok=True)

	tmp_dir = tempfile.mkdtemp(prefix='vfclamp-')
	tmp_glyphs_path = os.path.join(tmp_dir, 'vfclamp-source.glyphs')
	export_dir = os.path.join(tmp_dir, 'export')
	os.makedirs(export_dir, exist_ok=True)

	temp_doc = None
	try:
		save_gsfont_to_glyphs(clamped_font, tmp_glyphs_path)
		# Open headlessly when supported (Glyphs 3.2+). On older builds that
		# do not recognise the ``showInterface`` kwarg, Python raises TypeError;
		# PyObjC may also raise NSInvalidArgumentException via ValueError.
		# When we fall back to a visible open, we mark the doc for closure in
		# the finally block so we don't strand a phantom temp window.
		try:
			temp_doc = Glyphs.open(tmp_glyphs_path, showInterface=False)
		except (TypeError, ValueError):
			temp_doc = Glyphs.open(tmp_glyphs_path)
		if temp_doc is None:
			raise RuntimeError(
				'Glyphs.open() returned no document for temp source file. '
				'Glyphs 3.2+ is recommended for the open-Glyphs-font path.'
			)

		# Find or create a Variable Font Setting.
		vf_inst = next((i for i in temp_doc.instances if _is_variable_instance(i)), None)
		if vf_inst is None:
			vf_inst = GSInstance()
			vf_inst.type = INSTANCETYPEVARIABLE
			vf_inst.name = clamped_font.familyName or 'Variable'
			temp_doc.instances.append(vf_inst)

		# Build the kwarg dict so we can fall back gracefully on Glyphs <3.2
		# where ``containers`` was not yet a recognised parameter.
		generate_kwargs = dict(
			format=outline_fmt,
			fontPath=export_dir,
			autoHint=True,
			useProductionNames=True,
		)
		if container is not None:
			generate_kwargs['containers'] = [container]
		try:
			ok = vf_inst.generate(**generate_kwargs)
		except TypeError:
			# Older Glyphs builds reject the ``containers`` kwarg.
			generate_kwargs.pop('containers', None)
			ok = vf_inst.generate(**generate_kwargs)
		# Glyphs returns True (older builds) or a list of generated paths
		# (newer builds) on success; a string on failure; falsy values
		# (False, None) and NSError on hard failure.
		if isinstance(ok, str):
			raise RuntimeError(f'Glyphs export failed: {ok}')
		if ok is False or ok is None:
			raise RuntimeError(
				'Glyphs export failed: GSInstance.generate returned no result. '
				'Check the Glyphs Macro Panel for details.'
			)

		# Locate the freshly generated file. Glyphs writes one file with an
		# extension matching the container/outline combination. We filter to
		# the expected extension first so a stray .fea/.designspace/log can't
		# masquerade as the output.
		expected_ext = extension_for_format(fmt).lower()
		generated = [
			os.path.join(export_dir, name)
			for name in os.listdir(export_dir)
			if not name.startswith('.') and name.lower().endswith(expected_ext)
		]
		if not generated:
			# Fall back to any non-dotfile so the user gets a file even if the
			# extension is unexpected (older Glyphs may write .ttf for OTF).
			generated = [
				os.path.join(export_dir, name)
				for name in os.listdir(export_dir)
				if not name.startswith('.')
			]
		if not generated:
			raise RuntimeError('Glyphs export wrote no file to the temp directory')
		# Pick the most recently modified file in case Glyphs wrote auxiliary
		# files alongside the main output.
		generated.sort(key=lambda p: os.path.getmtime(p), reverse=True)
		shutil.move(generated[0], output_path)
	finally:
		# Close the temp document without prompting to save.
		if temp_doc is not None:
			try:
				if hasattr(temp_doc, 'parent') and temp_doc.parent is not None:
					temp_doc.parent.close()
				elif hasattr(temp_doc, 'close'):
					temp_doc.close()
			except (AttributeError, RuntimeError):
				pass
		# Best-effort cleanup of the temp directory. ignore_errors=True already
		# swallows OSError; no need for a second except wrapper.
		shutil.rmtree(tmp_dir, ignore_errors=True)
