# presets.py — JSON persistence for vf-clamp presets and recent-folders MRU.
# Pure-Python; no AppKit or fonttools imports so the helpers are unit-testable
# without a Glyphs runtime.

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# Default on-disk locations. We keep presets + the recent-folders MRU in a
# vendor-namespaced Application Support folder of our own rather than inside
# Glyphs' Plugins folder. The Plugins folder is reserved by Glyphs for plugin
# bundles only — writing plain data files there is unexpected and was reported
# upstream (issue #84). The bundle ID namespace keeps our data out of the way
# while still persisting across plugin reinstalls and staying easy to wipe.
DEFAULT_SUPPORT_DIR = Path(
	'~/Library/Application Support/studio.liiift.vf-clamp'
).expanduser()
DEFAULT_PRESETS_PATH = DEFAULT_SUPPORT_DIR / 'presets.json'
DEFAULT_RECENTS_PATH = DEFAULT_SUPPORT_DIR / 'recent.json'

# Pre-#84 location: data used to live inside Glyphs' Plugins folder. We migrate
# any files found here to DEFAULT_SUPPORT_DIR on first access so existing users
# keep their presets and recent folders after upgrading.
LEGACY_SUPPORT_DIR = Path(
	'~/Library/Application Support/Glyphs 3/Plugins/vf-clamp'
).expanduser()


def migrate_legacy_support_dir(
	legacy_dir: Path = LEGACY_SUPPORT_DIR,
	new_dir: Path = DEFAULT_SUPPORT_DIR,
) -> None:
	"""Move pre-#84 presets/recent files into the new support dir if needed.

	Best-effort and idempotent: only copies a legacy file when the new
	location does not already have it, and never raises — a failed migration
	must not break plugin startup. Leaves the legacy files in place so a
	downgrade still finds them.
	"""
	try:
		if not legacy_dir.is_dir():
			return
		new_dir.mkdir(parents=True, exist_ok=True)
		for filename in ('presets.json', 'recent.json'):
			src = legacy_dir / filename
			dst = new_dir / filename
			if src.is_file() and not dst.exists():
				dst.write_bytes(src.read_bytes())
	except OSError:
		# Migration is opportunistic — silently give up on any I/O error.
		return

# Cap on how many recent folders we keep. Five matches Finder's "Recent Places"
# menu density and keeps the popup short enough to read at a glance.
RECENT_FOLDERS_MAX = 5


def _atomic_write_json(path: Path, payload: Any) -> None:
	"""Write ``payload`` as JSON to ``path`` atomically.

	Uses a sibling tmp file + ``os.replace`` so a crash mid-write cannot leave
	an empty/corrupt presets.json behind. Quietly creates the parent dir.
	"""
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
	except OSError:
		return
	tmp = path.with_suffix(path.suffix + '.tmp')
	try:
		with open(tmp, 'w', encoding='utf-8') as f:
			json.dump(payload, f, indent=2, ensure_ascii=False)
		os.replace(tmp, path)
	except OSError:
		# Best-effort persistence — a failed write should never break the UI.
		try:
			if tmp.exists():
				tmp.unlink()
		except OSError:
			pass


def _read_json(path: Path) -> Optional[Any]:
	"""Return parsed JSON from ``path`` or None on missing/invalid file."""
	try:
		with open(path, 'r', encoding='utf-8') as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def _validate_preset(payload: Any) -> Optional[Dict[str, Any]]:
	"""Return a sanitised preset dict or None if the payload is invalid.

	A valid preset has at least ``name`` (str), ``instances`` (list of str),
	and ``format`` (str). Extra keys are preserved untouched so newer plugin
	versions can extend the schema without losing old fields on round-trip.
	"""
	if not isinstance(payload, dict):
		return None
	name = payload.get('name')
	instances = payload.get('instances')
	fmt = payload.get('format')
	if not isinstance(name, str) or not name.strip():
		return None
	if not isinstance(instances, list) or not all(isinstance(s, str) for s in instances):
		return None
	if not isinstance(fmt, str):
		return None
	return payload


def load_presets(path: Path = DEFAULT_PRESETS_PATH) -> Dict[str, Dict[str, Any]]:
	"""Read all presets from disk. Returns ``{preset_name: preset_dict}``.

	Invalid entries are silently dropped so a corrupt file never crashes the
	UI. The on-disk format is ``{name: payload}`` for fast lookup by name.
	"""
	raw = _read_json(path)
	if not isinstance(raw, dict):
		return {}
	out: Dict[str, Dict[str, Any]] = {}
	for key, value in raw.items():
		if not isinstance(key, str):
			continue
		validated = _validate_preset(value)
		if validated is not None:
			out[key] = validated
	return out


def save_presets(presets: Dict[str, Dict[str, Any]], path: Path = DEFAULT_PRESETS_PATH) -> None:
	"""Persist ``presets`` atomically."""
	_atomic_write_json(path, presets)


def make_preset(name: str, instances: List[str], fmt: str) -> Dict[str, Any]:
	"""Return a new preset dict ready to drop into the presets store."""
	return {
		'name': name,
		'instances': list(instances),
		'format': fmt,
	}


# ---------------------------------------------------------------------------
# Recent folders MRU
# ---------------------------------------------------------------------------

def load_recent_folders(
	path: Path = DEFAULT_RECENTS_PATH,
	*,
	verify_exists: bool = True,
) -> List[str]:
	"""Read the recent-folders list from disk.

	Entries that no longer exist on disk are filtered out when
	``verify_exists`` is True (the default). Set to False in tests so the
	helper can be exercised without touching the filesystem.
	"""
	raw = _read_json(path)
	if not isinstance(raw, list):
		return []
	out: List[str] = []
	for entry in raw:
		if not isinstance(entry, str):
			continue
		if verify_exists and not Path(entry).is_dir():
			continue
		out.append(entry)
	return out[:RECENT_FOLDERS_MAX]


def save_recent_folders(folders: List[str], path: Path = DEFAULT_RECENTS_PATH) -> None:
	"""Persist the recent-folders MRU atomically (capped to RECENT_FOLDERS_MAX)."""
	_atomic_write_json(path, list(folders)[:RECENT_FOLDERS_MAX])


def push_recent_folder(
	folders: List[str], folder: str, *, max_entries: int = RECENT_FOLDERS_MAX
) -> List[str]:
	"""Return a new MRU list with ``folder`` moved to the front and deduped.

	Pure function — caller is responsible for persisting the result via
	``save_recent_folders``. ``max_entries`` defaults to RECENT_FOLDERS_MAX but
	is parameterised so the tests can probe the cap edge.
	"""
	if not folder:
		return list(folders)[:max_entries]
	# Dedupe by string equality. Folder paths are case-sensitive on macOS
	# default (APFS case-insensitive), but we leave casing alone so the
	# popup preserves what the user actually picked.
	deduped = [folder] + [f for f in folders if f != folder]
	return deduped[:max_entries]


# ---------------------------------------------------------------------------
# Output name validation
# ---------------------------------------------------------------------------

# Characters that cannot appear in a sane filename on macOS / cross-platform.
INVALID_NAME_CHARS = '/\\:*?"<>|'


def validate_output_name(name: str) -> Optional[str]:
	"""Return an error message if ``name`` is not usable, else None.

	Catches the three cases that produce confusing fontTools / OSError
	failures at save time: empty/whitespace-only names, names containing
	filesystem-illegal characters, and names that start with a dot (hidden
	files don't show in Finder reveal).
	"""
	if not isinstance(name, str):
		return 'Name must be a string.'
	stripped = name.strip()
	if not stripped:
		return 'Enter an output name.'
	for ch in stripped:
		if ch in INVALID_NAME_CHARS:
			return f'Name contains an invalid character: {ch!r}'
	if stripped.startswith('.'):
		return 'Name cannot start with a period.'
	return None
