# formats.py — central registry of output formats supported by vf-clamp.
# Every site that needs to dispatch on a format label imports from here so
# adding a new format only requires editing this one file.

# Sentinel mapping. Each entry pins down everything the rest of the code needs
# to know about a format: the file extension, the fontTools flavor string for
# WOFF/WOFF2 wrappers, the Glyphs container constant key, the outline format
# token Glyphs.generate() expects, and whether the format produces a Glyphs
# source file rather than a binary.
#
# The "container_key" is a symbolic name resolved against the live GlyphsApp
# constants at runtime (see core._container_for_format) — keeping it as a
# string here means this module stays importable in CI where GlyphsApp is not
# present.

_FORMATS = {
	'TTF': {
		'extension': '.ttf',
		'flavor': None,
		'container_key': 'PLAIN',
		'outline': 'TTF',
		'is_source': False,
	},
	'OTF': {
		'extension': '.otf',
		'flavor': None,
		'container_key': 'PLAIN',
		'outline': 'OTF',
		'is_source': False,
	},
	'WOFF': {
		'extension': '.woff',
		'flavor': 'woff',
		'container_key': 'WOFF',
		'outline': 'OTF',
		'is_source': False,
	},
	'WOFF2': {
		'extension': '.woff2',
		'flavor': 'woff2',
		'container_key': 'WOFF2',
		'outline': 'OTF',
		'is_source': False,
	},
	'GLYPHS': {
		'extension': '.glyphs',
		'flavor': None,
		'container_key': None,
		'outline': None,
		'is_source': True,
	},
}


def _entry(fmt):
	"""Return the registry entry for ``fmt`` (case-insensitive). None if unknown."""
	if fmt is None:
		return None
	return _FORMATS.get(str(fmt).upper())


def known_formats():
	"""Return a tuple of every registered format label (uppercase)."""
	return tuple(_FORMATS.keys())


def is_known_format(fmt):
	"""Return True when ``fmt`` is a registered format label."""
	return _entry(fmt) is not None


def extension_for(fmt):
	"""Return the file extension for ``fmt``. Falls back to '.ttf' for unknown."""
	entry = _entry(fmt)
	return entry['extension'] if entry else '.ttf'


def flavor_for(fmt):
	"""Return the fontTools flavor string for ``fmt`` (or None for sfnt/source)."""
	entry = _entry(fmt)
	return entry['flavor'] if entry else None


def container_key_for(fmt):
	"""Return the symbolic Glyphs container constant name for ``fmt``.

	The caller resolves the string to an actual GlyphsApp constant — keeping
	the mapping symbolic here means this module stays importable in CI.
	Falls back to ``'PLAIN'`` for unknown formats.
	"""
	entry = _entry(fmt)
	return entry['container_key'] if entry else 'PLAIN'


def outline_for(fmt):
	"""Return the outline-format token Glyphs.generate() expects for ``fmt``."""
	entry = _entry(fmt)
	if entry is None:
		return 'OTF'
	return entry['outline'] or 'OTF'


def is_source_format(fmt):
	"""Return True when ``fmt`` produces a Glyphs source file rather than a binary."""
	entry = _entry(fmt)
	return bool(entry and entry['is_source'])
