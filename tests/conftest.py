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


# ---------------------------------------------------------------------------
# Lightweight GSFont mock factory — keeps the GlyphsApp Python API out of CI.
# We mirror just enough of the structural surface that core.compute_gsfont_hull
# and the other GSFont helpers can be exercised without a Glyphs installation.
# ---------------------------------------------------------------------------


class _FakeAxis:
	"""Minimal stand-in for GSAxis exposing the axisTag attribute used by core."""

	def __init__(self, tag):
		self.axisTag = tag


class _FakeInstance:
	"""Minimal stand-in for GSInstance with name, axes, and type."""

	def __init__(self, name, axes, type_=0):
		self.name = name
		# core mutates inst.axes in place when collapsing axes — keep a list.
		self.axes = list(axes)
		self.type = type_


class _FakeMaster:
	"""Minimal stand-in for GSFontMaster with mutable axes coordinates."""

	def __init__(self, axes):
		self.axes = list(axes)


class _FakeGSFont:
	"""Minimal GSFont stand-in for unit testing the GSFont code path.

	Exposes ``axes``, ``masters``, ``instances``, ``familyName``, ``filepath``,
	and a ``copy()`` method that returns a deep clone (matching what
	clamp_gsfont actually needs to leave the source untouched).
	"""

	def __init__(self, family_name, axes, masters, instances, filepath=None):
		self.familyName = family_name
		self.axes = [_FakeAxis(getattr(a, 'axisTag', a)) for a in axes]
		self.masters = list(masters)
		self.instances = list(instances)
		self.filepath = filepath

	def copy(self):
		"""Deep clone — mirrors the contract clamp_gsfont relies on."""
		import copy as _copy
		return _copy.deepcopy(self)


@pytest.fixture
def fake_gsfont():
	"""Return a fresh GSFont stand-in with three masters and four instances.

	Axes: wght (100-900), wdth (75-100).
	Masters: at (100,75), (100,100), (900,100).
	Instances: Light/Regular/Bold/Condensed Light + a Variable Font Setting.
	"""
	axes = ['wght', 'wdth']
	masters = [
		_FakeMaster([100, 75]),
		_FakeMaster([100, 100]),
		_FakeMaster([900, 100]),
	]
	instances = [
		_FakeInstance('Light', [100, 100]),
		_FakeInstance('Regular', [400, 100]),
		_FakeInstance('Bold', [900, 100]),
		_FakeInstance('Condensed Light', [100, 75]),
		# Variable Font Setting — should be preserved by clamp_gsfont
		_FakeInstance('Variable', [400, 100], type_=1),
	]
	return _FakeGSFont('Demo Family', axes, masters, instances, filepath='/tmp/demo.glyphs')


@pytest.fixture
def gsfont_classes():
	"""Expose the fake GSFont class factories so tests can hand-craft variants."""
	return {
		'GSFont': _FakeGSFont,
		'GSInstance': _FakeInstance,
		'GSFontMaster': _FakeMaster,
		'GSAxis': _FakeAxis,
	}
