# core.py — framework-agnostic fontTools helpers for vf-clamp Glyphs plugin.
# Importable outside Glyphs.app for unit testing; depends only on fontTools + stdlib.
#
# Architectural note (issue #60): the GSFont-side clamping subsystem (operating
# on Glyphs source files via the GlyphsApp Python API) used to live below in
# this same file. It now lives in the sibling module ``gsfont_core``. The two
# subsystems have different exception policies, different abstraction layers,
# and different runtime prerequisites, so they earned their own modules.
#
# For backward compatibility the public GSFont names are re-exported from this
# module at the bottom of the file so existing callers (plugin.py, tests) keep
# working with ``from core import clamp_gsfont`` style imports.

import contextlib
import copy
import os
import re
import shutil
import tempfile
import unicodedata

# Central format registry — every dispatch by format label routes here so
# adding a new format only touches formats.py instead of five call-sites.
import formats as _formats

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
	"""Return True when running inside Glyphs.app with the GlyphsApp Python API.

	The actual flag lives on ``gsfont_core`` after the core/gsfont_core split
	(issue #60). We import lazily and read through ``gsfont_core`` rather than
	through the module-level __getattr__ proxy because the proxy is not yet
	installed when this function is defined at import time.
	"""
	import gsfont_core as _gc
	return _gc._GLYPHS_AVAILABLE


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


# Memoised parse of check_fonttools_version() keyed by ``fontTools.__version__``.
# The version string is fixed for the lifetime of a production process, so we
# cache the parsed tuple to avoid re-running the regex on every UI refresh and
# every produce_restricted_vf call. Keying on the raw string means test-time
# ``monkeypatch.setattr(fontTools, '__version__', …)`` automatically invalidates
# the cache without needing a bespoke reset hook.
_CHECKED_FONTTOOLS_RAW = None
_CHECKED_FONTTOOLS_VERSION = None


def check_fonttools_version():
	"""Raise RuntimeError if fontTools is missing or too old for instancer.AxisTriple.

	Returns the parsed version tuple on success. The result is memoised against
	``fontTools.__version__`` — repeated calls with the same underlying version
	just return the cached parse.
	"""
	global _CHECKED_FONTTOOLS_RAW, _CHECKED_FONTTOOLS_VERSION
	# Availability check always runs — it covers both genuine import failure
	# AND test-time monkeypatching of _FONTTOOLS_AVAILABLE.
	if not _FONTTOOLS_AVAILABLE:
		raise RuntimeError(
			f'fontTools is not available: {_FONTTOOLS_IMPORT_ERROR}'
		)
	raw = getattr(fontTools, '__version__', '0.0.0')
	if raw == _CHECKED_FONTTOOLS_RAW and _CHECKED_FONTTOOLS_VERSION is not None:
		return _CHECKED_FONTTOOLS_VERSION
	# Parse leading 'X.Y.Z' segments
	parts = re.match(r'(\d+)\.(\d+)\.?(\d+)?', raw)
	if not parts:
		# Unparseable versions are NOT cached — preserve fall-through behaviour.
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
	_CHECKED_FONTTOOLS_RAW = raw
	_CHECKED_FONTTOOLS_VERSION = version
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
	"""Return the file extension for a format label (TTF, OTF, WOFF, WOFF2, GLYPHS).

	Thin wrapper around :mod:`formats` so adding a new format only requires
	editing the registry — callers still see the same module-level alias.
	"""
	return _formats.extension_for(fmt)


def flavor_for_format(fmt):
	"""Return the fontTools flavor string for WOFF/WOFF2, or None for raw sfnt.

	Thin wrapper around :mod:`formats` so adding a new format only requires
	editing the registry — callers still see the same module-level alias.
	"""
	return _formats.flavor_for(fmt)


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


def _hull_bounds(constraint):
	"""Return (lo, hi) for a hull entry — either a scalar pin or an AxisTriple/tuple.

	``instancer.AxisTriple`` is a Sequence (not a tuple subclass), so we test
	indexability rather than ``isinstance(..., tuple)``. A bare numeric pin
	collapses to ``(value, value)`` so callers can treat both shapes uniformly.
	"""
	if hasattr(constraint, '__len__') and not isinstance(constraint, (int, float)):
		return constraint[0], constraint[-1]
	return constraint, constraint


def prune_stat_axis_values(font, hull):
	"""Prune STAT AxisValue records and DesignAxisRecord entries to match ``hull``.

	AxisValue handling per format:

	* Format 1 (single value) — keep when ``Value`` lies inside the hull.
	* Format 2 (range) — keep when ``[RangeMinValue, RangeMaxValue]`` intersects
	  the hull. The surviving record's range is clamped to the hull so it does
	  not advertise a span the file can no longer reach; ``NominalValue`` is
	  re-anchored into the clamped range when it would otherwise fall outside.
	* Format 3 (linked) — keep when both ``Value`` and ``LinkedValue`` lie
	  inside the hull. A LinkedValue pointing outside the new range would lie
	  about a style link the restricted file cannot reach.
	* Format 4 (multi-axis) — keep only when every component ``AxisValueRecord``
	  references an axis the font still has and falls inside that axis's hull.

	DesignAxisRecord is pruned for axes the instancer removed from fvar
	(i.e. pinned axes whose tag no longer appears in ``font['fvar']``). The
	``AxisIndex`` on every surviving AxisValue is re-mapped to the new
	DesignAxisRecord positions, and the ``Format 4`` per-component records are
	re-mapped as well. ``DesignAxisCount`` is kept in sync. ``AxisValueCount``
	is updated to match the kept list.

	``ElidedFallbackNameID`` is left untouched: it points into the name table,
	not the AxisValue table, so STAT pruning does not invalidate it.

	No-op when STAT is absent.
	"""
	if 'STAT' not in font:
		return
	stat = font['STAT'].table
	axis_records = getattr(stat, 'DesignAxisRecord', None)
	if axis_records is None:
		return
	tag_for_index = [ax.AxisTag for ax in axis_records.Axis]
	# Tags still present in the (post-instancer) fvar — axes the instancer
	# pinned away are absent from fvar. We treat absence-but-in-hull as "the
	# instancer dropped this axis"; absence-but-not-in-hull as "the font never
	# had it" (e.g. a STAT axis that fvar didn't carry).
	fvar_tags = (
		{ax.axisTag for ax in font['fvar'].axes}
		if 'fvar' in font
		else set()
	)

	value_array = getattr(stat, 'AxisValueArray', None)
	if value_array is not None:
		kept = []
		for av in value_array.AxisValue:
			fmt = getattr(av, 'Format', None)
			if fmt == 4:
				# Multi-axis record: keep only when every component axis is
				# still present and every component value lies inside its hull.
				records = getattr(av, 'AxisValueRecord', None) or []
				ok = bool(records)
				for rec in records:
					rec_idx = getattr(rec, 'AxisIndex', None)
					if rec_idx is None or rec_idx >= len(tag_for_index):
						ok = False
						break
					rec_tag = tag_for_index[rec_idx]
					if rec_tag in hull and rec_tag not in fvar_tags:
						# Axis was pinned out by the instancer.
						ok = False
						break
					constraint = hull.get(rec_tag)
					if constraint is None:
						continue
					lo, hi = _hull_bounds(constraint)
					val = getattr(rec, 'Value', None)
					if val is None or not (lo <= val <= hi):
						ok = False
						break
				if ok:
					kept.append(av)
				continue

			axis_idx = getattr(av, 'AxisIndex', None)
			if axis_idx is None or axis_idx >= len(tag_for_index):
				kept.append(av)
				continue
			tag = tag_for_index[axis_idx]
			# Drop AxisValue records whose axis was pinned out of fvar.
			if tag in hull and tag not in fvar_tags:
				continue
			constraint = hull.get(tag)
			if constraint is None:
				kept.append(av)
				continue
			lo, hi = _hull_bounds(constraint)
			if fmt == 1:
				val = getattr(av, 'Value', None)
				if val is None or lo <= val <= hi:
					kept.append(av)
			elif fmt == 2:
				rmin = getattr(av, 'RangeMinValue', None)
				rmax = getattr(av, 'RangeMaxValue', None)
				nominal = getattr(av, 'NominalValue', None)
				if rmin is None or rmax is None:
					kept.append(av)
					continue
				# Keep only when the advertised range intersects the hull.
				if rmax < lo or rmin > hi:
					continue
				# Clamp the surviving record so it doesn't advertise a span
				# the restricted file no longer carries.
				av.RangeMinValue = max(rmin, lo)
				av.RangeMaxValue = min(rmax, hi)
				if nominal is not None and not (av.RangeMinValue <= nominal <= av.RangeMaxValue):
					av.NominalValue = av.RangeMinValue
				kept.append(av)
			elif fmt == 3:
				val = getattr(av, 'Value', None)
				linked = getattr(av, 'LinkedValue', None)
				if val is None or not (lo <= val <= hi):
					continue
				# A LinkedValue pointing outside the new hull would advertise a
				# style link that the restricted file cannot reach.
				if linked is not None and not (lo <= linked <= hi):
					continue
				kept.append(av)
			else:
				# Unknown format — keep to avoid silently dropping data we
				# do not understand.
				kept.append(av)
		value_array.AxisValue = kept
		stat.AxisValueCount = len(kept)

	# Prune DesignAxisRecord for axes the instancer pinned out, then re-map
	# AxisIndex on surviving AxisValue records (including Format 4 sub-records).
	axis_array = axis_records.Axis
	pinned_indices = {
		i for i, ax in enumerate(axis_array)
		if ax.AxisTag in hull and ax.AxisTag not in fvar_tags
	}
	if pinned_indices:
		old_to_new = {}
		new_axes = []
		for i, ax in enumerate(axis_array):
			if i in pinned_indices:
				continue
			old_to_new[i] = len(new_axes)
			new_axes.append(ax)
		axis_records.Axis = new_axes
		stat.DesignAxisCount = len(new_axes)
		if value_array is not None:
			for av in value_array.AxisValue:
				fmt = getattr(av, 'Format', None)
				if fmt == 4:
					records = getattr(av, 'AxisValueRecord', None) or []
					for rec in records:
						old = getattr(rec, 'AxisIndex', None)
						if old in old_to_new:
							rec.AxisIndex = old_to_new[old]
				else:
					old = getattr(av, 'AxisIndex', None)
					if old in old_to_new:
						av.AxisIndex = old_to_new[old]


def patch_name_table(font, family_name, subfamily=None):
	"""Update name IDs 1, 2, 4, 6, 16, 17, 25 to reflect the restricted VF.

	``subfamily`` controls nameID 2 (subfamily) and nameID 17 (typographic
	subfamily). When omitted it defaults to ``'Regular'`` — appropriate for a
	ranged output where the file represents the family-at-default-location.
	For a single-instance pin, callers should pass the picked instance's
	subfamily so nameID 2 (and the Full Name 1+2 pairing) stay coherent with
	what the file actually represents (e.g. pinning the ``Bold`` instance
	should yield nameID 2 = ``Bold`` and nameID 4 = ``{family} Bold``).

	Both Windows (platformID=3, UTF-16-BE) and Mac (platformID=1, mac_roman)
	records for English (langID 0x0409 / 0) are updated. Non-English localised
	records for these IDs are removed to avoid stale name leakage.
	mac_roman records that cannot encode the family name are dropped.
	"""
	ps_name = sanitize_ps_name(family_name)
	# Subfamily name keeps Full Name (1+2) and Typo Full (16+17) coherent.
	# For a pin we expect the picked instance's subfamily (e.g. 'Bold');
	# for a range we fall back to 'Regular' (the family-at-default convention).
	subfamily_value = (subfamily or 'Regular').strip() or 'Regular'

	name_table = font['name']
	existing_ids = {r.nameID for r in name_table.names}

	updates = {
		1: family_name,
		2: subfamily_value,
		4: f'{family_name} {subfamily_value}'.strip(),
		6: ps_name,
	}
	if 16 in existing_ids:
		updates[16] = family_name
		# Pair nameID 17 with nameID 16 to satisfy Windows GDI requirements
		updates[17] = subfamily_value
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


# ---------------------------------------------------------------------------
# OS/2 + head.macStyle synchronisation
# ---------------------------------------------------------------------------

# Standard usWidthClass mapping. Width values are wdth-axis percentages
# (50..200) and the OS/2 class is 1..9 per the OpenType spec.
_WIDTH_CLASS_BREAKPOINTS = (
	(62.5, 1),   # Ultra-condensed
	(75.0, 2),   # Extra-condensed
	(87.5, 3),   # Condensed
	(100.0, 4),  # Semi-condensed
	(112.5, 5),  # Medium / Normal
	(125.0, 6),  # Semi-expanded
	(150.0, 7),  # Expanded
	(200.0, 8),  # Extra-expanded
)


def _width_class_for_wdth(value):
	"""Map a wdth-axis value (50..200) to an OS/2.usWidthClass integer (1..9)."""
	for boundary, cls in _WIDTH_CLASS_BREAKPOINTS:
		if value < boundary:
			return cls
	return 9


# OS/2.fsSelection bits we touch.
_FS_ITALIC = 0x01
_FS_BOLD = 0x20
_FS_REGULAR = 0x40
_FS_OBLIQUE = 0x200

# head.macStyle bits we touch.
_MAC_BOLD = 0x01
_MAC_ITALIC = 0x02


def _default_location_after_instancing(font, hull):
	"""Return the effective default location after instancing.

	For axes still in fvar, the instancer has already chosen a default
	(typically the anchored midpoint). For axes the instancer pinned out, the
	pinned value lives only in the hull we passed in. We merge the two so the
	OS/2 update works for fully-pinned, fully-ranged, and mixed cases.
	"""
	location = {}
	if 'fvar' in font:
		for ax in font['fvar'].axes:
			location[ax.axisTag] = ax.defaultValue
	for tag, constraint in hull.items():
		if tag in location:
			continue
		# Axis pinned out of fvar — the hull is the pin value (scalar) or a
		# pinned range (use the lo edge). Note: instancer.AxisTriple is a
		# Sequence but NOT a tuple subclass, so we test indexability.
		lo, _ = _hull_bounds(constraint)
		location[tag] = lo
	return location


def update_os2_and_macstyle_from_fvar(font, hull=None):
	"""Recompute OS/2 weight/width/fsSelection and head.macStyle from fvar.

	For each axis tag we look at the *default location* still advertised by the
	(post-instancer) fvar table — plus any axis the instancer pinned out of
	fvar entirely (those values come from ``hull``):

	* ``wght`` -> ``OS/2.usWeightClass`` (rounded, clamped to 1..1000) and the
	  ``BOLD`` bits in fsSelection / macStyle when the default >= 600.
	* ``wdth`` -> ``OS/2.usWidthClass`` via the standard 50..200 -> 1..9 map.
	* ``ital`` (>= 0.5) or ``slnt`` (negative) -> ``ITALIC`` bits in fsSelection
	  and macStyle. A negative slnt without an ital axis is conventionally
	  recorded as oblique as well.
	* ``REGULAR`` is set iff none of BOLD / ITALIC / OBLIQUE end up set.

	``hull`` is optional. When passed, axes the instancer pinned out of fvar
	(so their default no longer appears in ``font['fvar']``) are recovered
	from the hull entry. When omitted, only axes still in fvar contribute.
	"""
	if 'OS/2' not in font:
		return
	if hull is None:
		hull = {}
	defaults = _default_location_after_instancing(font, hull)
	if not defaults:
		return
	os2 = font['OS/2']
	head = font['head'] if 'head' in font else None

	wght = defaults.get('wght')
	wdth = defaults.get('wdth')
	ital = defaults.get('ital')
	slnt = defaults.get('slnt')

	if wght is not None:
		os2.usWeightClass = max(1, min(1000, int(round(wght))))
	if wdth is not None:
		os2.usWidthClass = _width_class_for_wdth(wdth)

	# Resolve italic / oblique. ital is a boolean-style axis (0 = upright,
	# 1 = italic); slnt is signed degrees, with negative values forward-leaning
	# (conventionally italic / oblique).
	is_italic = (ital is not None and ital >= 0.5) or (slnt is not None and slnt < 0)
	is_oblique = slnt is not None and slnt < 0 and (ital is None or ital < 0.5)
	is_bold = wght is not None and wght >= 600

	fs = getattr(os2, 'fsSelection', 0) or 0
	# Clear the bits we own then re-set the ones the new default warrants.
	fs &= ~(_FS_ITALIC | _FS_BOLD | _FS_REGULAR | _FS_OBLIQUE)
	if is_italic:
		fs |= _FS_ITALIC
	if is_oblique:
		fs |= _FS_OBLIQUE
	if is_bold:
		fs |= _FS_BOLD
	if not (is_italic or is_oblique or is_bold):
		fs |= _FS_REGULAR
	os2.fsSelection = fs

	if head is not None:
		mac = getattr(head, 'macStyle', 0) or 0
		mac &= ~(_MAC_BOLD | _MAC_ITALIC)
		if is_bold:
			mac |= _MAC_BOLD
		if is_italic or is_oblique:
			mac |= _MAC_ITALIC
		head.macStyle = mac


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
		# Derive the subfamily for nameID 2/17: for a single-instance pin we use
		# the picked subfamily name verbatim (sans any '#N' disambiguation
		# suffix) so Full Name (1+2) reflects what the file actually contains.
		# For multi-instance ranges we leave it None so patch_name_table falls
		# back to 'Regular' — the standard family-at-default convention for VFs.
		subfamily_for_name = None
		if len(selected_names) == 1:
			only = selected_names[0]
			# Strip the ' #N' disambiguation suffix used by get_instance_names
			only = re.sub(r' #\d+$', '', only)
			subfamily_for_name = only or None
		patch_name_table(partial, family_name, subfamily=subfamily_for_name)
		# Update OS/2 + head.macStyle to reflect the new fvar default (or the
		# pinned location). fontTools' instancer is incomplete here — slant/
		# italic-derived bits in fsSelection/macStyle are never set, and older
		# fontTools versions skip the weight/width recompute for ranged outputs.
		update_os2_and_macstyle_from_fvar(partial, hull)

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
# GSFont-side clamping — moved to gsfont_core.py (issue #60: subsystem split).
# These names are re-exported here so existing callers
# (plugin.py, tests/test_gsfont_helpers.py) continue to work with their current
# `from core import …` style. New call sites should prefer importing directly
# from ``gsfont_core``.
# ---------------------------------------------------------------------------

import gsfont_core as _gsfont_core
from gsfont_core import (  # noqa: F401  (re-export by name)
	list_open_glyphs_fonts,
	gsfont_label,
	gsfont_instance_names,
	compute_gsfont_hull,
	clamp_gsfont,
	save_gsfont_to_glyphs,
	export_gsfont_binary_via_glyphs,
)
# Private helpers exposed for the existing unit tests (test_gsfont_helpers
# pokes at ``core._outline_format_for`` and ``core._container_for_format``).
from gsfont_core import (  # noqa: F401
	_is_variable_instance,
	_deepcopy_gsfont,
	_container_for_format,
	_outline_format_for,
)


# ---------------------------------------------------------------------------
# Naming-alignment deprecation aliases (issue #46)
#
# The file-source (fontTools) public API uses verb-first names without a
# subsystem prefix (``get_instance_names``, ``compute_hull``); the GSFont-
# source API uses a ``gsfont_`` prefix on the same verbs
# (``gsfont_instance_names``, ``compute_gsfont_hull``). The two were
# introduced at different times and the prefix-style is the canonical one for
# the newer GSFont subsystem — a full rename across the older fontTools
# helpers would be a public API break for downstream consumers (the npm
# wrapper, the CLI plugin, third-party Glyphs scripts that import from
# ``core``).
#
# As an intermediate step we add prefix-aligned aliases pointing at the
# existing fontTools helpers so callers who prefer the symmetrical
# ``<subsystem>_<verb>`` shape (e.g. ``fttools_instance_names``) can use it
# today. The original names remain the supported public API.
#
# Full rename pass deferred to v2.0 to coordinate with the npm + CLI release.
# ---------------------------------------------------------------------------

# Prefix-aligned aliases for the fontTools-side helpers. Pointing at the
# verb-first originals keeps a single source of truth; updating the original
# automatically updates the alias.
fttools_instance_names = get_instance_names
fttools_compute_hull = compute_hull
fttools_axis_hull_from_instances = get_axis_hull_from_instances
fttools_produce_restricted_vf = produce_restricted_vf


# The capability flag and the INSTANCETYPEVARIABLE sentinel live on
# ``gsfont_core`` but several tests monkeypatch them on ``core``. To keep the
# legacy ``monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', …)`` pattern working
# we forward those names through a module-level __getattr__ / __setattr__
# pair: reading or writing ``core._GLYPHS_AVAILABLE`` proxies to the canonical
# attribute on ``gsfont_core`` so a single monkeypatched flag drives behaviour
# in both modules.

_PROXIED_GSFONT_ATTRS = {
	'_GLYPHS_AVAILABLE',
	'_GLYPHS_IMPORT_ERROR',
	'INSTANCETYPEVARIABLE',
	'Glyphs',
	'GSInstance',
	'PLAIN',
	'WOFF',
	'WOFF2',
}


def __getattr__(name):
	"""Proxy a small set of GSFont-side names through from gsfont_core.

	Keeps legacy ``core._GLYPHS_AVAILABLE`` reads / monkeypatches working after
	the core/gsfont_core split.
	"""
	if name in _PROXIED_GSFONT_ATTRS:
		return getattr(_gsfont_core, name)
	raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


import sys as _sys

class _CoreModuleProxy(_sys.modules[__name__].__class__):
	"""Module subclass that forwards GSFont attr writes to gsfont_core.

	Needed because ``monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', False)`` in
	the legacy test suite must actually flip the flag the runtime code reads,
	which now lives on gsfont_core.
	"""

	def __setattr__(self, name, value):
		if name in _PROXIED_GSFONT_ATTRS:
			setattr(_gsfont_core, name, value)
			return
		super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _CoreModuleProxy
