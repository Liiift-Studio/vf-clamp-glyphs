# core.py — framework-agnostic helpers for vf-clamp Glyphs plugin.
# Importable outside Glyphs.app for unit testing; depends only on fontTools + stdlib.

import contextlib
import os
import re
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
	"""Return the file extension for a format label (TTF, OTF, WOFF, WOFF2)."""
	return {
		'TTF': '.ttf',
		'OTF': '.otf',
		'WOFF': '.woff',
		'WOFF2': '.woff2',
	}.get(str(fmt).upper(), '.ttf')


def flavor_for_format(fmt):
	"""Return the fontTools flavor string for WOFF/WOFF2, or None for raw sfnt."""
	f = str(fmt).upper()
	if f == 'WOFF':
		return 'woff'
	if f == 'WOFF2':
		return 'woff2'
	return None


def get_axis_hull_from_instances(font, selected_names):
	"""Return per-axis ``(min, max)`` covering every selected named instance.

	Pure helper around a TTFont. Returns ``{axis_tag: [lo, hi]}``.
	Raises ValueError if there is no fvar table or no selected name matches.
	"""
	if 'fvar' not in font:
		raise ValueError('This font has no fvar table — it is not a variable font.')

	fvar = font['fvar']
	name_table = font['name']

	# Order instances by index so duplicates can be disambiguated by position
	indexed = []
	for idx, inst in enumerate(fvar.instances):
		label = name_table.getDebugName(inst.subfamilyNameID)
		if label:
			indexed.append((idx, label, dict(inst.coordinates)))

	hull = {}
	matched = 0
	for _, label, coords in indexed:
		if label not in selected_names:
			continue
		matched += 1
		for tag, val in coords.items():
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
	Axes shared by every selected instance but not varied are pinned to the value;
	axes not present in any selected instance are left unrestricted (absent from
	the returned dict).
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
	"""Drop fvar.instances whose subfamily name is not in ``selected_names``.

	No-op when ``fvar`` is absent.
	"""
	if 'fvar' not in font:
		return
	fvar = font['fvar']
	name_table = font['name']
	kept = []
	for inst in fvar.instances:
		label = name_table.getDebugName(inst.subfamilyNameID)
		if label and label in selected_names:
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


def _safe_open_font(font_path):
	"""Open a TTFont after running basic safety checks (size cap, fvar required)."""
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
		raise OSError(f'Failed to parse font file: {e}') from e


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
	"""
	safe = sanitize_filename(family_name)
	if not folder:
		folder = os.path.expanduser('~/Desktop')
	folder = os.path.abspath(folder)
	candidate = os.path.abspath(os.path.join(folder, safe + ext))
	# Refuse to escape the chosen folder
	if os.path.commonpath([candidate, folder]) != folder:
		candidate = os.path.join(folder, sanitize_filename('font') + ext)
	# Auto-suffix on collision
	if os.path.exists(candidate):
		base, e = os.path.splitext(candidate)
		i = 1
		while os.path.exists(f'{base}-{i}{e}'):
			i += 1
		candidate = f'{base}-{i}{e}'
	return candidate


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
			raise OSError(f'Failed to save output font: {e}') from e
		except Exception as e:  # brotli missing for WOFF2, etc.
			raise RuntimeError(f'Failed to save output font: {e}') from e
