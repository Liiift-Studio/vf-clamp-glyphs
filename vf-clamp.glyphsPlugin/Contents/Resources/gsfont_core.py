# gsfont_core.py — GSFont-side clamping subsystem for vf-clamp Glyphs plugin.
# Operates on Glyphs.app source files (.glyphs); depends on the GlyphsApp Python API
# when run inside Glyphs, and falls back to copy.deepcopy for unit-test fakes.
#
# Split from core.py to keep the fontTools/binary subsystem distinct from the
# GSFont/source-file subsystem (per architectural review #60). The two subsystems
# share no operational code: different exception policies, different abstraction
# layers, different runtime requirements. core.py is framework-agnostic and runs
# anywhere fontTools imports; this module additionally requires GlyphsApp at full
# capability (graceful no-op otherwise).

import copy
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Re-use shared filename/format helpers from core so behaviour matches the
# fontTools-driven binary path exactly (no drift between the two writers).
from core import extension_for_format

# Central format registry — keeps every dispatch by format label in one place.
import formats as _formats

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


def is_glyphs_app_available() -> bool:
	"""Return True when running inside Glyphs.app with the GlyphsApp Python API."""
	return _GLYPHS_AVAILABLE


def glyphs_import_error() -> Optional[str]:
	"""Return the GlyphsApp import-error string, or None when importable."""
	return _GLYPHS_IMPORT_ERROR


def _is_variable_instance(inst: Any) -> bool:
	"""Return True for a Variable Font Setting instance (not a static named instance)."""
	return getattr(inst, 'type', 0) == INSTANCETYPEVARIABLE


def list_open_glyphs_fonts() -> List[Any]:
	"""Return the list of open ``GSFont`` documents, or [] when Glyphs is unavailable."""
	if not _GLYPHS_AVAILABLE:
		return []
	try:
		return list(Glyphs.fonts)
	except Exception:
		return []


def gsfont_label(gsfont: Any) -> str:
	"""Return a short, human-readable label for a GSFont (used in PopUp lists)."""
	family = getattr(gsfont, 'familyName', '') or 'Untitled'
	doc_path = ''
	try:
		doc_path = gsfont.filepath or ''
	except (AttributeError, OSError):
		# Glyphs may not have a filepath set, or filesystem access may fail.
		pass
	if doc_path:
		base = Path(doc_path).name
		return f'{family}  ({base})'
	return family


def gsfont_instance_names(gsfont: Any) -> List[str]:
	"""Return ordered names of *static* named instances in a GSFont (skips Variable Font Settings)."""
	out: List[str] = []
	for inst in gsfont.instances:
		if _is_variable_instance(inst):
			continue
		name = (inst.name or '').strip()
		if name:
			out.append(name)
	return out


def compute_gsfont_hull(gsfont: Any, selected_names: List[str]) -> Dict[str, Tuple[float, float]]:
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


def _deepcopy_gsfont(gsfont: Any) -> Any:
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
	tmp_dir = Path(tempfile.mkdtemp(prefix='vfclamp-clone-'))
	tmp_path = tmp_dir / 'clone.glyphs'
	try:
		# makeCopy=True writes a new file without changing the source's
		# tracked filepath or flipping its dirty flag.
		# str() coerces for the Glyphs API which expects an NSString-bridgeable
		# value (pathlib.Path passes through PyObjC fine, but explicit str is
		# robust across older Glyphs builds).
		gsfont.save(path=str(tmp_path), formatVersion=3, makeCopy=True)
		try:
			reopened = Glyphs.open(str(tmp_path), showInterface=False)
		except TypeError:
			reopened = Glyphs.open(str(tmp_path))
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


def clamp_gsfont(gsfont: Any, selected_instance_names: List[str], output_family_name: str) -> Any:
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


def save_gsfont_to_glyphs(gsfont: Any, output_path: str, format_version: Optional[int] = None) -> None:
	"""Save a (clamped) GSFont to a ``.glyphs`` file without affecting the open document set.

	When ``format_version`` is None the output inherits the source font's own
	``formatVersion`` (so a Glyphs 2 source produces a Glyphs 2 file rather than
	being silently upgraded to Glyphs 3 schema). Callers can pass an explicit
	integer to override.

	After the file is written we explicitly evict the cloned GSFont's tracked
	NSDocument from Glyphs' document controller. Reason: ``gsfont.copy()``
	(called upstream by ``clamp_gsfont``) silently registers the clone with
	NSDocumentController, naming it "clone" with an autosave path that was
	never actually written. When that document is later garbage-collected,
	Glyphs' autosave subsystem surfaces a modal alert:

	    The file "clone (Autosaved).glyphs" doesn't exist.

	Marking the doc as clean + removing it from the controller + closing it
	makes Glyphs forget the clone before the autosave check fires.
	"""
	if format_version is None:
		# Inherit the source font's format version; default to 3 if unknown.
		format_version = getattr(gsfont, 'formatVersion', None) or 3
	# makeCopy=True writes a new file without changing the font's tracked file path.
	gsfont.save(path=output_path, formatVersion=format_version, makeCopy=True)
	_evict_clone_tracking(gsfont)


def _evict_clone_tracking(gsfont: Any) -> None:
	"""Detach the cloned GSFont from Glyphs' NSDocumentController + autosave.

	Best-effort: each step is guarded so partial AppKit availability across
	Glyphs versions doesn't crash the save flow. Safe to call when there's
	no parent doc (no-op).
	"""
	if not _GLYPHS_AVAILABLE:
		return
	try:
		parent = getattr(gsfont, 'parent', None)
	except Exception:  # noqa: BLE001
		parent = None
	if parent is None:
		return
	# 1. Clear the dirty flag so autosave doesn't try to write a new copy on
	#    teardown. NSChangeCleared == 0.
	try:
		parent.updateChangeCount_(0)
	except (AttributeError, RuntimeError):
		pass
	# 2. Remove from the shared document controller's tracked-document set.
	try:
		from AppKit import NSDocumentController  # type: ignore
		dc = NSDocumentController.sharedDocumentController()
		if dc is not None:
			dc.removeDocument_(parent)
	except (ImportError, AttributeError, RuntimeError):
		pass
	# 3. Close without prompting.
	try:
		parent.close()
	except (AttributeError, RuntimeError):
		pass


def _container_for_format(fmt: str) -> Any:
	"""Map a 'TTF'/'OTF'/'WOFF'/'WOFF2' string to a Glyphs export container constant.

	The symbolic container key comes from the central :mod:`formats` registry
	and is resolved against the live GlyphsApp constants here. Returns ``None``
	when GlyphsApp is not importable (CI/test).
	"""
	if not _GLYPHS_AVAILABLE:
		return None
	key = _formats.container_key_for(fmt)
	if key == 'WOFF':
		return WOFF
	if key == 'WOFF2':
		return WOFF2
	return PLAIN  # PLAIN is the default for TTF/OTF and unknown formats


def _outline_format_for(fmt: str) -> Any:
	"""Map a UI format string to the outline format Glyphs.generate() expects.

	Resolves through the :mod:`formats` registry — adding a new format only
	requires updating the registry entry.
	"""
	return _formats.outline_for(fmt)


def export_gsfont_binary_via_glyphs(clamped_font: Any, output_path: str, fmt: str) -> None:
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
	# pathlib path arithmetic on output_path: parent for the destination
	# directory; '.' fallback when output_path is a bare filename without dirs.
	output_path_obj = Path(output_path)
	output_dir = output_path_obj.parent if str(output_path_obj.parent) else Path('.')
	output_dir.mkdir(parents=True, exist_ok=True)

	tmp_dir = Path(tempfile.mkdtemp(prefix='vfclamp-'))
	tmp_glyphs_path = tmp_dir / 'vfclamp-source.glyphs'
	export_dir = tmp_dir / 'export'
	export_dir.mkdir(parents=True, exist_ok=True)

	temp_doc = None
	try:
		# str() coerces Path → str for the Glyphs API which expects an
		# NSString-bridgeable value across all Glyphs versions.
		save_gsfont_to_glyphs(clamped_font, str(tmp_glyphs_path))
		# Open headlessly when supported (Glyphs 3.2+). On older builds that
		# do not recognise the ``showInterface`` kwarg, Python raises TypeError;
		# PyObjC may also raise NSInvalidArgumentException via ValueError.
		# When we fall back to a visible open, we mark the doc for closure in
		# the finally block so we don't strand a phantom temp window.
		try:
			temp_doc = Glyphs.open(str(tmp_glyphs_path), showInterface=False)
		except (TypeError, ValueError):
			temp_doc = Glyphs.open(str(tmp_glyphs_path))
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
			fontPath=str(export_dir),
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
			p for p in export_dir.iterdir()
			if not p.name.startswith('.') and p.name.lower().endswith(expected_ext)
		]
		if not generated:
			# Fall back to any non-dotfile so the user gets a file even if the
			# extension is unexpected (older Glyphs may write .ttf for OTF).
			generated = [
				p for p in export_dir.iterdir()
				if not p.name.startswith('.')
			]
		if not generated:
			raise RuntimeError('Glyphs export wrote no file to the temp directory')
		# Pick the most recently modified file in case Glyphs wrote auxiliary
		# files alongside the main output.
		generated.sort(key=lambda p: p.stat().st_mtime, reverse=True)
		# shutil.move accepts Path objects on 3.9+; we still wrap in str()
		# for compatibility with the older Python bundled in some Glyphs builds.
		shutil.move(str(generated[0]), str(output_path_obj))
	finally:
		# Close the temp document without prompting to save. Use the same
		# eviction sequence as save_gsfont_to_glyphs so the binary export
		# path doesn't trip the "clone (Autosaved).glyphs doesn't exist"
		# alert when temp_doc is GC'd later.
		if temp_doc is not None:
			_evict_clone_tracking(temp_doc)
		# Also evict the upstream clamped_font's tracking — clamp_gsfont
		# returns a copy() that registered itself with NSDocumentController.
		_evict_clone_tracking(clamped_font)
		# Best-effort cleanup of the temp directory. ignore_errors=True already
		# swallows OSError; no need for a second except wrapper.
		shutil.rmtree(tmp_dir, ignore_errors=True)
