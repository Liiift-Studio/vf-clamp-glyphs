# test_gsfont_helpers.py — unit tests for the GSFont helpers in core.py.
# These use the FakeGSFont factory in conftest.py so they run on CI without Glyphs.

import pytest

import core


# ---------------------------------------------------------------------------
# list_open_glyphs_fonts / gsfont_label / gsfont_instance_names
# ---------------------------------------------------------------------------


class TestListOpenGlyphsFonts:
	"""Verify list_open_glyphs_fonts handles the missing-Glyphs case."""

	def test_returns_empty_when_glyphs_unavailable(self, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', False)
		assert core.list_open_glyphs_fonts() == []


class TestGsfontLabel:
	"""Verify gsfont_label combines familyName and basename safely."""

	def test_with_filepath(self, fake_gsfont):
		fake_gsfont.filepath = '/Users/x/Desktop/MyFont.glyphs'
		label = core.gsfont_label(fake_gsfont)
		assert 'Demo Family' in label
		assert 'MyFont.glyphs' in label

	def test_without_filepath(self, fake_gsfont):
		fake_gsfont.filepath = None
		assert core.gsfont_label(fake_gsfont) == 'Demo Family'

	def test_blank_family_falls_back(self, fake_gsfont):
		fake_gsfont.familyName = ''
		fake_gsfont.filepath = None
		assert core.gsfont_label(fake_gsfont) == 'Untitled'


class TestGsfontInstanceNames:
	"""Verify gsfont_instance_names returns ordered static-instance names."""

	def test_skips_variable_setting(self, fake_gsfont, monkeypatch):
		# core treats inst.type == INSTANCETYPEVARIABLE as a Variable Font Setting
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		names = core.gsfont_instance_names(fake_gsfont)
		assert names == ['Light', 'Regular', 'Bold', 'Condensed Light']

	def test_strips_blank_names(self, fake_gsfont, gsfont_classes, monkeypatch):
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		fake_gsfont.instances.append(gsfont_classes['GSInstance']('   ', [400, 100]))
		fake_gsfont.instances.append(gsfont_classes['GSInstance']('', [400, 100]))
		names = core.gsfont_instance_names(fake_gsfont)
		assert '' not in names


# ---------------------------------------------------------------------------
# compute_gsfont_hull
# ---------------------------------------------------------------------------


class TestComputeGsfontHull:
	"""Verify compute_gsfont_hull builds the right per-axis (lo, hi) dict."""

	def test_single_instance_pins(self, fake_gsfont, monkeypatch):
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		hull = core.compute_gsfont_hull(fake_gsfont, ['Regular'])
		assert hull == {'wght': (400, 400), 'wdth': (100, 100)}

	def test_two_instances_range(self, fake_gsfont, monkeypatch):
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		hull = core.compute_gsfont_hull(fake_gsfont, ['Light', 'Bold'])
		assert hull['wght'] == (100, 900)
		assert hull['wdth'] == (100, 100)

	def test_skips_variable_setting(self, fake_gsfont, monkeypatch):
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		# Variable Font Setting should never contribute to the hull
		hull = core.compute_gsfont_hull(fake_gsfont, ['Variable'])
		assert hull == {}

	def test_skips_empty_axis_tag(self, fake_gsfont, gsfont_classes, monkeypatch):
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		# Replace the wdth axis with a blank-tag axis to verify it is skipped
		fake_gsfont.axes[1].axisTag = ''
		hull = core.compute_gsfont_hull(fake_gsfont, ['Light', 'Bold'])
		assert 'wght' in hull
		assert '' not in hull

	def test_empty_selection_returns_empty(self, fake_gsfont, monkeypatch):
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		assert core.compute_gsfont_hull(fake_gsfont, []) == {}


# ---------------------------------------------------------------------------
# clamp_gsfont
# ---------------------------------------------------------------------------


class TestClampGsfont:
	"""Verify clamp_gsfont returns a new font with the expected pruning."""

	def test_raises_when_glyphs_unavailable(self, fake_gsfont, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', False)
		with pytest.raises(RuntimeError, match='GlyphsApp Python API not available'):
			core.clamp_gsfont(fake_gsfont, ['Regular'], 'Restricted Family')

	def test_raises_on_empty_selection(self, fake_gsfont, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', True)
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		with pytest.raises(ValueError, match='No instances selected'):
			core.clamp_gsfont(fake_gsfont, [], 'Restricted Family')

	def test_raises_when_no_masters_in_hull(self, fake_gsfont, monkeypatch):
		"""When the hull excludes every master, raise a friendly RuntimeError."""
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', True)
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		# Regular sits at (400, 100); no master is at wght=400, so the hull
		# {wght:(400,400), wdth:(100,100)} excludes all three masters. The
		# pin-collapse branch fires a tailored, single-instance message.
		with pytest.raises(RuntimeError, match='does not coincide with any existing master'):
			core.clamp_gsfont(fake_gsfont, ['Regular'], 'Restricted Family')

	def test_source_is_not_mutated(self, fake_gsfont, monkeypatch):
		"""clamp_gsfont must not mutate the input GSFont."""
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', True)
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		original_family = fake_gsfont.familyName
		original_instance_count = len(fake_gsfont.instances)
		original_master_count = len(fake_gsfont.masters)
		try:
			core.clamp_gsfont(fake_gsfont, ['Light', 'Bold'], 'Restricted Family')
		except RuntimeError:
			pass
		assert fake_gsfont.familyName == original_family
		assert len(fake_gsfont.instances) == original_instance_count
		assert len(fake_gsfont.masters) == original_master_count

	def test_preserves_variable_setting(self, fake_gsfont, monkeypatch):
		"""Variable Font Setting instances must survive clamping."""
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', True)
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		clamped = core.clamp_gsfont(fake_gsfont, ['Light', 'Bold'], 'Restricted Family')
		instance_names = [i.name for i in clamped.instances]
		assert 'Variable' in instance_names
		# Bold and Light selected — Regular and Condensed Light should be dropped
		assert 'Regular' not in instance_names
		assert 'Condensed Light' not in instance_names

	def test_rewrites_family_name(self, fake_gsfont, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', True)
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		clamped = core.clamp_gsfont(fake_gsfont, ['Light', 'Bold'], 'Restricted Family')
		assert clamped.familyName == 'Restricted Family'


# ---------------------------------------------------------------------------
# _container_for_format / _outline_format_for
# ---------------------------------------------------------------------------


class TestFormatMappers:
	"""Verify the Glyphs-export format mappers."""

	def test_outline_format_for(self):
		assert core._outline_format_for('TTF') == 'TTF'
		assert core._outline_format_for('OTF') == 'OTF'
		assert core._outline_format_for('WOFF') == 'OTF'
		assert core._outline_format_for('WOFF2') == 'OTF'
		# Unknown / case-insensitive
		assert core._outline_format_for('ttf') == 'TTF'
		assert core._outline_format_for('weird') == 'OTF'
		assert core._outline_format_for('') == 'OTF'

	def test_container_for_format_when_glyphs_unavailable(self, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', False)
		assert core._container_for_format('TTF') is None
		assert core._container_for_format('WOFF2') is None
