# conftest.py — pytest bootstrap that makes the in-bundle core.py importable
# and builds a tiny variable-font fixture for the test session.

import os
import sys
import pytest

# Resources/ holds core.py; expose it on sys.path so tests can `import core`.
RESOURCES_DIR = os.path.abspath(
	os.path.join(
		os.path.dirname(__file__),
		'..',
		'vf-clamp.glyphsPlugin',
		'Contents',
		'Resources',
	)
)
if RESOURCES_DIR not in sys.path:
	sys.path.insert(0, RESOURCES_DIR)


def _build_minimal_vf():
	"""Construct a tiny variable TTFont with one wght axis and three instances.

	Uses fontTools.fontBuilder for the boilerplate so the fixture stays
	robust across fontTools versions.
	"""
	from fontTools.fontBuilder import FontBuilder
	from fontTools.pens.ttGlyphPen import TTGlyphPen

	fb = FontBuilder(1000, isTTF=True)
	glyph_order = ['.notdef', 'A']
	fb.setupGlyphOrder(glyph_order)
	fb.setupCharacterMap({ord('A'): 'A'})

	# Build two simple glyphs (empty .notdef and a tiny square 'A')
	pen_notdef = TTGlyphPen(None)
	notdef_glyph = pen_notdef.glyph()

	pen_a = TTGlyphPen(None)
	pen_a.moveTo((0, 0))
	pen_a.lineTo((500, 0))
	pen_a.lineTo((500, 700))
	pen_a.lineTo((0, 700))
	pen_a.closePath()
	a_glyph = pen_a.glyph()
	fb.setupGlyf({'.notdef': notdef_glyph, 'A': a_glyph})

	fb.setupHorizontalMetrics({'.notdef': (500, 0), 'A': (520, 10)})
	fb.setupHorizontalHeader(ascent=800, descent=-200)
	fb.setupNameTable({
		'familyName': 'Test Family',
		'styleName': 'Regular',
	})
	fb.setupOS2(sTypoAscender=800, usWinAscent=800, usWinDescent=200)
	fb.setupPost()

	# Set up fvar with one wght axis + three named instances
	axes = [
		('wght', 100, 400, 900, 'Weight'),
	]
	instances = [
		{'location': {'wght': 300}, 'stylename': 'Light'},
		{'location': {'wght': 400}, 'stylename': 'Regular'},
		{'location': {'wght': 700}, 'stylename': 'Bold'},
	]
	fb.setupFvar(axes, instances)

	# Add a gvar table with one variation per axis so instancer has work to do.
	from fontTools.ttLib.tables.TupleVariation import TupleVariation
	# Two deltas — one for each end of the wght axis. Glyph 'A' has 4 contour
	# points plus 4 phantom points (lsb/rsb/tsb/bsb) = 8 deltas.
	def _deltas(scale):
		return [
			(int(0 * scale), 0),
			(int(50 * scale), 0),
			(int(50 * scale), int(50 * scale)),
			(int(0 * scale), int(50 * scale)),
			(0, 0), (0, 0), (0, 0), (0, 0),
		]
	gv_min = TupleVariation({'wght': (-1.0, -1.0, 0.0)}, _deltas(-1.0))
	gv_max = TupleVariation({'wght': (0.0, 1.0, 1.0)}, _deltas(2.0))
	fb.setupGvar({'A': [gv_min, gv_max], '.notdef': []})

	return fb.font


@pytest.fixture
def vf_font():
	"""Return a fresh in-memory variable TTFont for each test."""
	return _build_minimal_vf()


@pytest.fixture
def vf_font_path(tmp_path, vf_font):
	"""Return a filesystem path to a freshly-serialised variable font."""
	path = tmp_path / 'test.ttf'
	vf_font.save(str(path))
	return str(path)
