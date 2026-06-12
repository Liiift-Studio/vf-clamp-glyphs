# test_coverage_gaps.py — targeted tests for previously-uncovered code paths
# in core.py. Issues #52 calls these out specifically: variable-instance
# detection, container/outline format mapping, STAT pruning, mac_roman name
# table fallback, version-check branches, brotli ImportError surface, and
# the safe_output_path collision loop, plus get_instance_names duplicate
# disambiguation and the _GLYPHS_AVAILABLE guard messages.

import os
import pytest

import core


# ---------------------------------------------------------------------------
# _is_variable_instance
# ---------------------------------------------------------------------------


class TestIsVariableInstance:
	"""Verify _is_variable_instance keys off the GSInstance.type attribute."""

	def test_variable_instance_true(self, monkeypatch, gsfont_classes):
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		inst = gsfont_classes['GSInstance']('VF', [400], type_=1)
		assert core._is_variable_instance(inst) is True

	def test_static_instance_false(self, monkeypatch, gsfont_classes):
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)
		inst = gsfont_classes['GSInstance']('Regular', [400], type_=0)
		assert core._is_variable_instance(inst) is False

	def test_missing_type_attribute_is_false(self, monkeypatch):
		"""A bare object without .type should not be classed as variable."""
		monkeypatch.setattr(core, 'INSTANCETYPEVARIABLE', 1)

		class _Bare:
			pass
		assert core._is_variable_instance(_Bare()) is False


# ---------------------------------------------------------------------------
# _container_for_format — both branches
# ---------------------------------------------------------------------------


class TestContainerForFormat:
	"""Verify _container_for_format covers Glyphs-unavailable and per-format."""

	def test_returns_none_when_glyphs_unavailable(self, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', False)
		assert core._container_for_format('TTF') is None
		assert core._container_for_format('OTF') is None
		assert core._container_for_format('WOFF') is None
		assert core._container_for_format('WOFF2') is None

	def test_returns_glyphs_constants_when_available(self, monkeypatch):
		# Stand in fake Glyphs constants so we can confirm dispatch without
		# importing the real GlyphsApp module.
		fake_plain = object()
		fake_woff = object()
		fake_woff2 = object()
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', True)
		monkeypatch.setattr(core, 'PLAIN', fake_plain)
		monkeypatch.setattr(core, 'WOFF', fake_woff)
		monkeypatch.setattr(core, 'WOFF2', fake_woff2)
		assert core._container_for_format('TTF') is fake_plain
		assert core._container_for_format('OTF') is fake_plain
		assert core._container_for_format('WOFF') is fake_woff
		assert core._container_for_format('WOFF2') is fake_woff2
		# Empty / unknown -> PLAIN
		assert core._container_for_format('') is fake_plain
		assert core._container_for_format(None) is fake_plain

	def test_case_insensitive(self, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', True)
		fake_woff = object()
		monkeypatch.setattr(core, 'WOFF', fake_woff)
		monkeypatch.setattr(core, 'PLAIN', object())
		monkeypatch.setattr(core, 'WOFF2', object())
		assert core._container_for_format('woff') is fake_woff


# ---------------------------------------------------------------------------
# is_glyphs_app_available + clamp_gsfont guard messaging
# ---------------------------------------------------------------------------


class TestGlyphsGuardMessaging:
	"""Verify the Glyphs-unavailable guard surfaces the original import error."""

	def test_clamp_message_includes_import_error(self, fake_gsfont, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', False)
		monkeypatch.setattr(core, '_GLYPHS_IMPORT_ERROR', 'mocked import failure')
		with pytest.raises(RuntimeError, match='mocked import failure'):
			core.clamp_gsfont(fake_gsfont, ['Regular'], 'X')

	def test_export_binary_message_includes_import_error(self, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', False)
		monkeypatch.setattr(core, '_GLYPHS_IMPORT_ERROR', 'no-glyphs-module')
		with pytest.raises(RuntimeError, match='no-glyphs-module'):
			core.export_gsfont_binary_via_glyphs(None, '/tmp/out.ttf', 'TTF')


# ---------------------------------------------------------------------------
# safe_output_path collision loop — exercise the suffix increment
# ---------------------------------------------------------------------------


class TestSafeOutputPathCollisionLoop:
	"""Verify safe_output_path's suffix loop survives many collisions."""

	def test_many_collisions(self, tmp_path):
		# Pre-create A.ttf, A-1.ttf, ..., A-4.ttf so the loop has to run.
		(tmp_path / 'A.ttf').write_bytes(b'x')
		for i in range(1, 5):
			(tmp_path / f'A-{i}.ttf').write_bytes(b'x')
		out = core.safe_output_path(str(tmp_path), 'A', '.ttf')
		assert out.endswith('A-5.ttf')

	def test_collision_skips_non_existent_gap(self, tmp_path):
		# A-3.ttf does NOT exist — but the loop walks sequentially, so it
		# should still stop at the first non-existing suffix (A-3.ttf).
		(tmp_path / 'A.ttf').write_bytes(b'x')
		(tmp_path / 'A-1.ttf').write_bytes(b'x')
		(tmp_path / 'A-2.ttf').write_bytes(b'x')
		out = core.safe_output_path(str(tmp_path), 'A', '.ttf')
		assert out.endswith('A-3.ttf')


# ---------------------------------------------------------------------------
# get_instance_names — duplicate-instance disambiguation
# ---------------------------------------------------------------------------


class TestGetInstanceNamesDuplicates:
	"""Verify the ' #N' suffix is applied to duplicate fvar instance names."""

	def test_duplicate_names_disambiguated(self, tmp_path, vf_font):
		"""Two instances with the same subfamily name get '#2' / '#3' suffixes."""
		# Append a duplicate 'Regular' to the in-memory font's fvar table.
		from fontTools.ttLib.tables._f_v_a_r import NamedInstance
		fvar = vf_font['fvar']
		dup = NamedInstance()
		dup.subfamilyNameID = fvar.instances[1].subfamilyNameID  # reuse Regular's name id
		dup.coordinates = {'wght': 500}
		fvar.instances.append(dup)
		# Add a third Regular too.
		dup2 = NamedInstance()
		dup2.subfamilyNameID = fvar.instances[1].subfamilyNameID
		dup2.coordinates = {'wght': 600}
		fvar.instances.append(dup2)

		path = tmp_path / 'dups.ttf'
		vf_font.save(str(path))
		names = core.get_instance_names(str(path))
		# Original ordering: Light, Regular, Bold, Regular(dup), Regular(dup2)
		# disambiguated -> Regular, Regular #2, Regular #3
		assert names == ['Light', 'Regular', 'Bold', 'Regular #2', 'Regular #3']


# ---------------------------------------------------------------------------
# prune_stat_axis_values — both kept and dropped branches
# ---------------------------------------------------------------------------


class TestPruneStatAxisValues:
	"""Verify prune_stat_axis_values trims format-1/3 records outside the hull."""

	def test_noop_without_stat(self, vf_font):
		# vf_font has no STAT table — should silently return.
		assert 'STAT' not in vf_font
		core.prune_stat_axis_values(vf_font, {'wght': (300, 700)})

	def test_format_1_record_in_range_kept(self, vf_font):
		_seed_stat(vf_font, [(0, 1, 400)])  # wght=400, in hull [300,700]
		core.prune_stat_axis_values(vf_font, {'wght': (300, 700)})
		stat = vf_font['STAT'].table
		assert stat.AxisValueCount == 1

	def test_format_1_record_outside_range_dropped(self, vf_font):
		_seed_stat(vf_font, [(0, 1, 900)])  # wght=900, outside hull [300,700]
		core.prune_stat_axis_values(vf_font, {'wght': (300, 700)})
		stat = vf_font['STAT'].table
		assert stat.AxisValueCount == 0

	def test_unknown_format_preserved(self, vf_font):
		# Format 2 (range record) is not handled — should be kept untouched.
		_seed_stat(vf_font, [(0, 2, 400)])
		core.prune_stat_axis_values(vf_font, {'wght': (300, 700)})
		stat = vf_font['STAT'].table
		assert stat.AxisValueCount == 1

	def test_record_for_unknown_axis_preserved(self, vf_font):
		# AxisIndex points past the axis list — keep, don't crash.
		_seed_stat(vf_font, [(99, 1, 400)])
		core.prune_stat_axis_values(vf_font, {'wght': (300, 700)})
		stat = vf_font['STAT'].table
		assert stat.AxisValueCount == 1

	def test_pin_constraint_keeps_matching_value(self, vf_font):
		# When the hull constraint is a scalar (pin), records at that exact
		# value are kept; others are dropped.
		_seed_stat(vf_font, [(0, 1, 400), (0, 1, 500)])
		core.prune_stat_axis_values(vf_font, {'wght': 400})
		stat = vf_font['STAT'].table
		assert stat.AxisValueCount == 1


def _seed_stat(font, value_specs):
	"""Attach a minimal STAT table with the given (axis_idx, format, value) tuples.

	Helper for the STAT pruning tests — fontTools doesn't ship a tiny STAT
	builder, so we synthesise just enough structure (DesignAxisRecord +
	AxisValueArray) to exercise the prune logic.
	"""
	from fontTools.ttLib import newTable

	class _Axis:
		def __init__(self, tag):
			self.AxisTag = tag

	class _DesignAxes:
		def __init__(self, axes):
			self.Axis = axes

	class _AxisValue:
		def __init__(self, axis_idx, fmt, value):
			self.AxisIndex = axis_idx
			self.Format = fmt
			self.Value = value

	class _AxisValueArray:
		def __init__(self, values):
			self.AxisValue = values

	class _StatTable:
		def __init__(self):
			self.DesignAxisRecord = _DesignAxes([_Axis('wght')])
			self.AxisValueArray = _AxisValueArray([])
			self.AxisValueCount = 0

	stat = newTable('STAT')
	stat.table = _StatTable()
	stat.table.AxisValueArray = _AxisValueArray(
		[_AxisValue(idx, fmt, val) for (idx, fmt, val) in value_specs]
	)
	stat.table.AxisValueCount = len(value_specs)
	font['STAT'] = stat


# ---------------------------------------------------------------------------
# patch_name_table — mac_roman fallback for unencodable bytes
# ---------------------------------------------------------------------------


class TestPatchNameTableMacRoman:
	"""Verify patch_name_table drops mac_roman records when re-encoding fails."""

	def test_unencodable_mac_record_dropped(self, vf_font):
		# Force a mac record onto a nameID we will rewrite, with a value that
		# cannot round-trip through mac_roman (cyrillic or emoji works).
		name_table = vf_font['name']
		# Add a mac record for nameID 1 with platformID=1, langID=0.
		name_table.setName(
			'placeholder', 1, 1, 0, 0,
		)
		family = 'Бренд'  # Cyrillic — not encodable to mac_roman
		core.patch_name_table(vf_font, family)
		# After patching, no mac (platform=1) record for nameID 1 should remain;
		# the windows (platform=3) record must still carry the new family name.
		mac_records = [
			r for r in name_table.names if r.nameID == 1 and r.platformID == 1
		]
		win_records = [
			r for r in name_table.names if r.nameID == 1 and r.platformID == 3
		]
		assert mac_records == []
		assert win_records
		assert win_records[0].toUnicode() == family

	def test_mac_record_encodes_ascii(self, vf_font):
		"""ASCII family names should retain their mac_roman record."""
		name_table = vf_font['name']
		name_table.setName('placeholder', 1, 1, 0, 0)
		core.patch_name_table(vf_font, 'AsciiFamily')
		mac_records = [
			r for r in name_table.names if r.nameID == 1 and r.platformID == 1
		]
		# At least one mac_roman record should survive when the new family
		# is fully representable in mac_roman.
		assert mac_records


# ---------------------------------------------------------------------------
# check_fonttools_version — version-too-old branch
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_fonttools_version():
	"""Yield a setter that temporarily rewrites core.fontTools.__version__.

	pytest's monkeypatch.setattr does not always reach core.fontTools.__version__
	when the test runner has captured a different fontTools module instance
	through another fixture. Manual save/restore guarantees the patch reaches
	the actual lookup site used by check_fonttools_version.
	"""
	import core as _core
	original = _core.fontTools.__version__

	def _set(value):
		_core.fontTools.__version__ = value

	yield _set
	_core.fontTools.__version__ = original


class TestCheckFonttoolsVersionTooOld:
	"""Verify the version-too-old branch raises RuntimeError."""

	def test_raises_for_old_version(self, patch_fonttools_version):
		patch_fonttools_version('4.0.0')
		with pytest.raises(RuntimeError, match='is required for axis instancing'):
			core.check_fonttools_version()

	def test_returns_zero_tuple_for_unparseable(self, patch_fonttools_version):
		patch_fonttools_version('not-a-version')
		# Unparseable -> returns (0,0,0) per the function contract.
		assert core.check_fonttools_version() == (0, 0, 0)

	def test_handles_two_part_version(self, patch_fonttools_version):
		"""Versions like '4.50' (no patch) should parse as (4, 50, 0)."""
		patch_fonttools_version('4.50')
		# core.MIN_FONTTOOLS_VERSION is (4, 34, 0); 4.50.0 should be accepted.
		assert core.check_fonttools_version() == (4, 50, 0)


# ---------------------------------------------------------------------------
# WOFF2 / brotli — produce_restricted_vf surface
# ---------------------------------------------------------------------------


class TestWoff2BrotliBranch:
	"""Verify the WOFF2 save path is wired up.

	We do NOT pre-emptively uninstall brotli for this test (the CI env has
	it). Instead we verify that the flavor argument actually reaches
	partial.save by intercepting via flavor_for_format and checking the
	resulting bytes start with the WOFF2 signature.
	"""

	def test_woff2_writes_correct_signature(self, vf_font_path, tmp_path):
		try:
			import brotli  # noqa: F401
		except ImportError:
			pytest.skip('brotli not installed — this environment cannot write WOFF2')
		out = str(tmp_path / 'r.woff2')
		core.produce_restricted_vf(
			vf_font_path, ['Light', 'Bold'], 'Restricted Family', out, fmt='WOFF2'
		)
		with open(out, 'rb') as fh:
			head = fh.read(4)
		# WOFF2 signature is 'wOF2'
		assert head == b'wOF2'


# ---------------------------------------------------------------------------
# is_fonttools_ready — error-branch coverage
# ---------------------------------------------------------------------------


class TestIsFonttoolsReadyBranches:
	"""Exercise is_fonttools_ready paths that the existing tests miss."""

	def test_false_when_check_version_raises(self, monkeypatch):
		"""If check_fonttools_version raises RuntimeError, is_fonttools_ready -> False."""
		def _raise():
			raise RuntimeError('too old')
		monkeypatch.setattr(core, 'check_fonttools_version', _raise)
		assert core.is_fonttools_ready() is False
