# test_font_integration.py — round-trip tests that exercise core helpers
# against an in-memory variable font fixture (see conftest.py).

import os
import pytest

import core


class TestGetInstanceNames:
	"""Verify get_instance_names enumerates fvar.instances in order."""

	def test_returns_subfamily_names(self, vf_font_path):
		names = core.get_instance_names(vf_font_path)
		assert names == ['Light', 'Regular', 'Bold']

	def test_rejects_static_font(self, tmp_path, vf_font):
		# Strip fvar (and the dependent gvar) to simulate a static font
		del vf_font['fvar']
		if 'gvar' in vf_font:
			del vf_font['gvar']
		path = tmp_path / 'static.ttf'
		vf_font.save(str(path))
		with pytest.raises(ValueError, match='no variable axes'):
			core.get_instance_names(str(path))


class TestComputeHull:
	"""Verify compute_hull constructs the right AxisTriple / pin per axis."""

	def test_single_instance_pins(self, vf_font):
		hull = core.compute_hull(vf_font, ['Regular'])
		assert hull == {'wght': 400.0}

	def test_two_instances_range(self, vf_font):
		hull = core.compute_hull(vf_font, ['Light', 'Bold'])
		from fontTools.varLib import instancer
		assert isinstance(hull['wght'], instancer.AxisTriple)
		assert hull['wght'].minimum == 300.0
		assert hull['wght'].maximum == 700.0
		# Default should be anchored inside the range (400 is inside [300, 700])
		assert hull['wght'].default == 400.0

	def test_default_clamped_when_outside(self, vf_font):
		# Light + Regular -> [300, 400]; original default 400 still inside
		hull = core.compute_hull(vf_font, ['Light', 'Regular'])
		assert hull['wght'].default == 400.0


class TestProduceRestrictedVF:
	"""Verify produce_restricted_vf saves a valid restricted variable font."""

	def test_save_ttf_range(self, vf_font_path, tmp_path):
		out = str(tmp_path / 'restricted.ttf')
		core.produce_restricted_vf(
			vf_font_path, ['Light', 'Bold'], 'Restricted Family', out, fmt='TTF'
		)
		assert os.path.exists(out)
		from fontTools.ttLib import TTFont
		with TTFont(out) as result:
			# Should still be variable (range, not pin) and have an fvar table
			assert 'fvar' in result
			ax = result['fvar'].axes[0]
			assert ax.minValue == 300.0
			assert ax.maxValue == 700.0
			# Name table updated
			assert result['name'].getDebugName(1) == 'Restricted Family'

	def test_save_pin_makes_static(self, vf_font_path, tmp_path):
		out = str(tmp_path / 'static.ttf'.replace('static', 'pinned'))
		core.produce_restricted_vf(
			vf_font_path, ['Regular'], 'Pinned Family', out, fmt='TTF'
		)
		from fontTools.ttLib import TTFont
		with TTFont(out) as result:
			# fontTools removes fvar when every axis is pinned
			assert 'fvar' not in result
			assert result['name'].getDebugName(1) == 'Pinned Family'

	def test_woff_flavor_actually_writes_woff(self, vf_font_path, tmp_path):
		out = str(tmp_path / 'restricted.woff')
		core.produce_restricted_vf(
			vf_font_path, ['Light', 'Bold'], 'Restricted Family', out, fmt='WOFF'
		)
		# WOFF signature is 'wOFF'
		with open(out, 'rb') as fh:
			head = fh.read(4)
		assert head == b'wOFF'

	def test_fvar_instances_filtered(self, vf_font_path, tmp_path):
		out = str(tmp_path / 'restricted.ttf')
		core.produce_restricted_vf(
			vf_font_path, ['Light', 'Bold'], 'Restricted Family', out, fmt='TTF'
		)
		from fontTools.ttLib import TTFont
		with TTFont(out) as result:
			labels = [
				result['name'].getDebugName(i.subfamilyNameID)
				for i in result['fvar'].instances
			]
			# 'Regular' was not selected — should be filtered out
			assert 'Regular' not in labels
			assert set(labels) == {'Light', 'Bold'}


class TestSafeOpenFont:
	"""Verify _safe_open_font enforces size cap and reports clear errors."""

	def test_oversized_rejected(self, tmp_path, monkeypatch):
		path = tmp_path / 'big.ttf'
		path.write_bytes(b'\x00' * 10)
		monkeypatch.setattr(core, 'MAX_FONT_BYTES', 1)
		with pytest.raises(ValueError, match='too large'):
			core._safe_open_font(str(path))

	def test_missing_file(self, tmp_path):
		with pytest.raises(OSError):
			core._safe_open_font(str(tmp_path / 'nope.ttf'))

	def test_garbage_file(self, tmp_path):
		path = tmp_path / 'garbage.ttf'
		path.write_bytes(b'not a font')
		with pytest.raises(OSError):
			core._safe_open_font(str(path))
