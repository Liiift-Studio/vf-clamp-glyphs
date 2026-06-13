# font_registration.py — wraps CTFontManager so the animated preview view
# can render glyph shapes from the user's *actual* source font.
#
# v1.2.2 used the system variable font as a stand-in. This module unlocks
# the real-source-font path:
#   • For File sources (.ttf/.otf/.woff/.woff2): register the file on disk,
#     return a font descriptor with a stable identifier.
#   • For Open Font sources (GSFont in Glyphs): export the source to a
#     temp variable TTF on a background thread, then register that.
#
# Cleanup is the caller's responsibility — call unregister_path() in the
# dialog's cancel handler so temp files don't leak across sessions.

import os
import shutil
import tempfile
import threading
import traceback
from typing import Callable, Optional, Tuple

try:
	from AppKit import NSFontManager, NSFont  # type: ignore
	from Foundation import NSURL  # type: ignore
	from CoreText import (  # type: ignore
		CTFontManagerRegisterFontsForURL,
		CTFontManagerUnregisterFontsForURL,
		CTFontManagerCreateFontDescriptorsFromURL,
		CTFontCreateWithFontDescriptor,
		CTFontCopyVariationAxes,
		kCTFontManagerScopeProcess,
	)
	_APPKIT_AVAILABLE = True
except Exception:  # noqa: BLE001 — CoreText/AppKit may be missing on CI
	_APPKIT_AVAILABLE = False


def is_available() -> bool:
	return _APPKIT_AVAILABLE


def _pick_variable_descriptor(descriptors):
	"""Pick the descriptor that exposes variation axes.

	``CTFontManagerCreateFontDescriptorsFromURL`` returns one descriptor per
	font in the file. For a variable font with named instances, that's the
	variable font itself PLUS one descriptor per named instance — and the
	instance descriptors are already collapsed (no axes). If we blindly
	pick descriptors[0] we may land on an instance, which is why preview
	animation appeared to do nothing even though the right glyphs rendered.

	Walk every descriptor, materialise a probe CTFont, ask for its axes,
	and return the first descriptor that reports a non-empty axis list.
	Falls back to descriptors[0] when nothing has axes (legitimately
	static font).
	"""
	if not descriptors:
		return None
	for desc in descriptors:
		try:
			probe = CTFontCreateWithFontDescriptor(desc, 12.0, None)
			if probe is None:
				continue
			axes = CTFontCopyVariationAxes(probe)
			if axes and len(axes) > 0:
				return desc
		except (AttributeError, RuntimeError, TypeError):
			continue
	return descriptors[0]


def register_font_at_path(path: str) -> Tuple[bool, Optional[object]]:
	"""Register a font file with the process-wide font namespace.

	Returns ``(ok, descriptor)``:
	  - ``ok`` is True if registration succeeded or the font was already
	    registered (CTFontManager returns False with a "duplicate" error in
	    that case, which we treat as success for preview purposes).
	  - ``descriptor`` is the variable font descriptor extracted from the
	    file (when available), or the first static one as fallback.

	Returns ``(False, None)`` on a hard error.
	"""
	if not _APPKIT_AVAILABLE or not path or not os.path.exists(path):
		return False, None
	try:
		url = NSURL.fileURLWithPath_(path)
		# Register at process scope so the font is available for NSFont lookup
		# but doesn't leak into other applications.
		ok, _err = CTFontManagerRegisterFontsForURL(
			url, kCTFontManagerScopeProcess, None,
		)
		# CTFontManager returns False with a "duplicate" error if the font
		# is already registered. That's fine for preview — we still extract
		# its descriptor below.
		descriptors = CTFontManagerCreateFontDescriptorsFromURL(url)
		picked = _pick_variable_descriptor(descriptors)
		if picked is not None:
			return True, picked
		return bool(ok), None
	except Exception:  # noqa: BLE001
		traceback.print_exc()
		return False, None


def unregister_path(path: str) -> bool:
	"""Unregister a previously-registered font file. Safe to call repeatedly."""
	if not _APPKIT_AVAILABLE or not path:
		return False
	try:
		url = NSURL.fileURLWithPath_(path)
		ok, _err = CTFontManagerUnregisterFontsForURL(
			url, kCTFontManagerScopeProcess, None,
		)
		return bool(ok)
	except Exception:  # noqa: BLE001
		return False


# ---------------------------------------------------------------------------
# GSFont → temp variable TTF — background-threaded so the dialog stays
# responsive while Glyphs compiles. Slow first hit (a few seconds for a
# large family), but only once per source font.
# ---------------------------------------------------------------------------

# Track temp paths globally so the dialog can ask us to clean up everything
# on close. Each entry is a path string.
_PREVIEW_TEMP_PATHS: list = []


def export_gsfont_to_temp_vf_async(
	gsfont,
	on_complete: Callable[[Optional[str], Optional[object]], None],
) -> None:
	"""Spawn a worker thread that compiles ``gsfont`` to a temp variable TTF.

	The callback runs **on the worker thread** with either
	``(temp_path, descriptor)`` on success or ``(None, None)`` on failure.
	Callers are responsible for marshalling the result to the main thread
	(use ``AppHelper.callAfter`` from the Glyphs plugin context).
	"""

	def _worker():
		try:
			path, desc = _export_gsfont_to_temp_vf_sync(gsfont)
			on_complete(path, desc)
		except Exception:  # noqa: BLE001
			traceback.print_exc()
			on_complete(None, None)

	t = threading.Thread(target=_worker, name='vfclamp-preview-export', daemon=True)
	t.start()


def _export_gsfont_to_temp_vf_sync(gsfont) -> Tuple[Optional[str], Optional[object]]:
	"""Synchronously export ``gsfont`` to a temp variable TTF and register it.

	Returns ``(path, descriptor)`` on success.
	"""
	# Defer the GlyphsApp import so this module remains importable on a
	# headless CI box. The plugin only calls this from inside Glyphs.
	try:
		from GlyphsApp import GSInstance, INSTANCETYPEVARIABLE, PLAIN, Glyphs  # type: ignore
	except ImportError:
		return None, None

	if gsfont is None:
		return None, None

	tmp_dir = tempfile.mkdtemp(prefix='vfclamp-preview-')
	_PREVIEW_TEMP_PATHS.append(tmp_dir)

	export_dir = os.path.join(tmp_dir, 'export')
	os.makedirs(export_dir, exist_ok=True)

	temp_doc = None
	source_path = os.path.join(tmp_dir, 'preview-source.glyphs')
	try:
		# Save a copy of the open font as a temp .glyphs so we can open it
		# headlessly and mutate it without touching the user's open document.
		try:
			copy = gsfont.copy()
		except (AttributeError, RuntimeError):
			copy = gsfont
		try:
			copy.save(path=source_path, formatVersion=3, makeCopy=True)
		except (AttributeError, TypeError):
			# Older Glyphs builds may not accept formatVersion kwarg.
			copy.save(source_path)

		# Open headlessly when supported (Glyphs 3.2+), fall back to a visible
		# document for older builds (the user briefly sees a tab appear).
		try:
			temp_doc = Glyphs.open(source_path, showInterface=False)
		except TypeError:
			temp_doc = Glyphs.open(source_path)
		if temp_doc is None:
			raise RuntimeError('Glyphs.open() returned no document for preview source')

		# Find or create a Variable Font Setting instance.
		vf_inst = None
		for inst in temp_doc.instances:
			try:
				if getattr(inst, 'type', 0) == INSTANCETYPEVARIABLE:
					vf_inst = inst
					break
			except (AttributeError, RuntimeError):
				continue
		if vf_inst is None:
			vf_inst = GSInstance()
			vf_inst.type = INSTANCETYPEVARIABLE
			vf_inst.name = (
				getattr(temp_doc, 'familyName', None) or 'Preview Variable'
			)
			temp_doc.instances.append(vf_inst)

		# Generate.
		try:
			vf_inst.generate(
				format='OTF',  # OTF outline + PLAIN container → .ttf wrapper
				fontPath=export_dir,
				containers=[PLAIN],
				autoHint=False,        # speed > hinting for preview
				useProductionNames=True,
			)
		except (AttributeError, TypeError) as e:
			raise RuntimeError(f'Glyphs preview export failed: {e}')

		# Find the resulting file. Glyphs writes a .ttf (or similar).
		generated = [
			os.path.join(export_dir, name)
			for name in os.listdir(export_dir)
			if not name.startswith('.')
		]
		if not generated:
			return None, None
		generated.sort(key=lambda p: os.path.getmtime(p), reverse=True)
		font_path = generated[0]

		# Register the temp font + extract a descriptor.
		ok, descriptor = register_font_at_path(font_path)
		if not ok:
			return None, None

		# Track for later cleanup.
		_PREVIEW_TEMP_PATHS.append(font_path)
		return font_path, descriptor

	finally:
		# Close the temp document; never let preview prep crash the dialog.
		# Run the full NSDocumentController eviction so Glyphs' autosave
		# subsystem doesn't surface "clone (Autosaved).glyphs doesn't exist"
		# after the preview compile finishes.
		if temp_doc is not None:
			try:
				# Defer to gsfont_core's helper when importable; otherwise
				# fall back to a local minimal close. The helper does the
				# updateChangeCount_ + removeDocument_ + close sequence.
				try:
					from gsfont_core import _evict_clone_tracking  # type: ignore
					_evict_clone_tracking(temp_doc)
				except Exception:  # noqa: BLE001
					if hasattr(temp_doc, 'parent') and temp_doc.parent is not None:
						temp_doc.parent.close()
					elif hasattr(temp_doc, 'close'):
						temp_doc.close()
			except Exception:  # noqa: BLE001
				pass


def cleanup_all_temp_paths() -> None:
	"""Unregister every temp font and remove every temp directory.

	Called by the dialog on cancel/close so preview work doesn't leak
	registered fonts across sessions.
	"""
	for path in list(_PREVIEW_TEMP_PATHS):
		try:
			unregister_path(path)
		except Exception:  # noqa: BLE001
			pass
		try:
			if os.path.isdir(path):
				shutil.rmtree(path, ignore_errors=True)
			elif os.path.isfile(path):
				os.remove(path)
		except Exception:  # noqa: BLE001
			pass
	_PREVIEW_TEMP_PATHS.clear()
