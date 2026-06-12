# test_core_helpers.py — unit tests for the framework-agnostic helpers in core.py.

import os

import pytest

import core


# ---------------------------------------------------------------------------
# compact_name
# ---------------------------------------------------------------------------

class TestCompactName:
	"""Verify compact_name strips shared prefixes/suffixes and joins differences."""

	def test_identical(self):
		assert core.compact_name('Light', 'Light') == 'Light'

	def test_simple_two(self):
		assert core.compact_name('Light', 'Bold') == 'Light-Bold'

	def test_shared_prefix(self):
		assert core.compact_name('Encode Sans Light', 'Encode Sans Bold') == 'Encode Sans Light-Bold'

	def test_shared_prefix_and_suffix(self):
		assert (
			core.compact_name('Encode Sans Light Italic', 'Encode Sans Bold Italic')
			== 'Encode Sans Light-Bold Italic'
		)

	def test_only_one_word_differs(self):
		assert core.compact_name('Regular', 'Regular') == 'Regular'


# ---------------------------------------------------------------------------
# compute_default_output_name
# ---------------------------------------------------------------------------

class TestComputeDefaultOutputName:
	"""Verify default output-name derivation strips trailing style slugs."""

	def test_strips_trailing_style(self):
		assert core.compute_default_output_name('EncodeSans-Light', 'Light', 'Bold') == 'EncodeSans Light-Bold'

	def test_no_trailing_style(self):
		# basename does not end with the style; fallback to basename + compact range
		assert core.compute_default_output_name('EncodeSans', 'Light', 'Bold') == 'EncodeSans Light-Bold'

	def test_empty_basename(self):
		assert core.compute_default_output_name('', 'Light', 'Bold') == 'Light-Bold'

	def test_single_selection(self):
		assert core.compute_default_output_name('EncodeSans', 'Regular', 'Regular') == 'EncodeSans Regular'


# ---------------------------------------------------------------------------
# extension_for_format / flavor_for_format
# ---------------------------------------------------------------------------

class TestFormatHelpers:
	"""Verify format string -> extension and flavor mappings."""

	@pytest.mark.parametrize('fmt,ext', [
		('TTF', '.ttf'),
		('OTF', '.otf'),
		('WOFF', '.woff'),
		('WOFF2', '.woff2'),
		('woff', '.woff'),
		('weird', '.ttf'),
	])
	def test_extension_for_format(self, fmt, ext):
		assert core.extension_for_format(fmt) == ext

	@pytest.mark.parametrize('fmt,flavor', [
		('TTF', None),
		('OTF', None),
		('WOFF', 'woff'),
		('WOFF2', 'woff2'),
	])
	def test_flavor_for_format(self, fmt, flavor):
		assert core.flavor_for_format(fmt) == flavor


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
	"""Verify sanitize_filename rejects unsafe characters across OSes."""

	def test_strips_path_separators(self):
		assert '/' not in core.sanitize_filename('a/b/c')
		assert '\\' not in core.sanitize_filename(r'a\b\c')

	def test_strips_windows_reserved(self):
		out = core.sanitize_filename('a:b*c?d"e<f>g|h')
		for ch in ':*?"<>|':
			assert ch not in out

	def test_strips_control_chars(self):
		out = core.sanitize_filename('a\x00b\x01c\x1fd\x7fe')
		assert out == 'abcde'

	def test_strips_unicode_directional(self):
		# U+202E RIGHT-TO-LEFT OVERRIDE must be removed
		assert '‮' not in core.sanitize_filename('a‮b')

	def test_strips_trailing_dots_and_spaces(self):
		assert core.sanitize_filename('myfile...   ') == 'myfile'
		assert core.sanitize_filename('   .myfile') == 'myfile'

	def test_empty_falls_back(self):
		assert core.sanitize_filename('') == 'font'
		assert core.sanitize_filename(None) == 'font'

	def test_length_cap(self):
		long = 'x' * 500
		out = core.sanitize_filename(long)
		assert len(out) <= 200


# ---------------------------------------------------------------------------
# sanitize_ps_name
# ---------------------------------------------------------------------------

class TestSanitizePsName:
	"""Verify the PostScript-name sanitiser respects spec restrictions."""

	def test_replaces_spaces_with_hyphens(self):
		assert core.sanitize_ps_name('Encode Sans Light') == 'Encode-Sans-Light'

	def test_strips_non_ascii(self):
		assert core.sanitize_ps_name('日本語Font') == 'Font'

	def test_collapses_hyphens(self):
		assert core.sanitize_ps_name('a---b') == 'a-b'

	def test_strips_leading_trailing_hyphens(self):
		assert core.sanitize_ps_name('---abc---') == 'abc'

	def test_empty_falls_back(self):
		assert core.sanitize_ps_name('') == 'Font'
		assert core.sanitize_ps_name('---') == 'Font'

	def test_length_cap(self):
		out = core.sanitize_ps_name('a' * 200)
		assert len(out) <= 63


# ---------------------------------------------------------------------------
# safe_output_path
# ---------------------------------------------------------------------------

class TestSafeOutputPath:
	"""Verify safe_output_path stays inside the chosen folder and auto-suffixes."""

	def test_basic(self, tmp_path):
		out = core.safe_output_path(str(tmp_path), 'MyFont', '.ttf')
		import os as _os
		assert out.startswith(_os.path.realpath(str(tmp_path)))
		assert out.endswith('MyFont.ttf')

	def test_path_traversal_blocked(self, tmp_path):
		# A '/etc/passwd' family_name should not escape the folder
		import os as _os
		out = core.safe_output_path(str(tmp_path), '/etc/passwd', '.ttf')
		assert out.startswith(_os.path.realpath(str(tmp_path)))

	def test_collision_suffix(self, tmp_path):
		first = tmp_path / 'A.ttf'
		first.write_bytes(b'x')
		out = core.safe_output_path(str(tmp_path), 'A', '.ttf')
		assert out.endswith('A-1.ttf')
		(tmp_path / 'A-1.ttf').write_bytes(b'x')
		out2 = core.safe_output_path(str(tmp_path), 'A', '.ttf')
		assert out2.endswith('A-2.ttf')


# ---------------------------------------------------------------------------
# check_fonttools_version
# ---------------------------------------------------------------------------


class TestCheckFonttoolsVersion:
	"""Verify check_fonttools_version returns a tuple and rejects too-old."""

	def test_returns_tuple_when_ok(self):
		ver = core.check_fonttools_version()
		assert isinstance(ver, tuple)
		assert len(ver) == 3

	def test_raises_when_unavailable(self, monkeypatch):
		monkeypatch.setattr(core, '_FONTTOOLS_AVAILABLE', False)
		monkeypatch.setattr(core, '_FONTTOOLS_IMPORT_ERROR', 'mocked')
		with pytest.raises(RuntimeError, match='not available'):
			core.check_fonttools_version()


# ---------------------------------------------------------------------------
# Public capability helpers
# ---------------------------------------------------------------------------


class TestPublicCapabilityHelpers:
	"""Verify the public is_*/fonttools_import_error helpers."""

	def test_is_fonttools_ready_true(self):
		assert core.is_fonttools_ready() is True

	def test_is_fonttools_ready_false_when_unavailable(self, monkeypatch):
		monkeypatch.setattr(core, '_FONTTOOLS_AVAILABLE', False)
		assert core.is_fonttools_ready() is False

	def test_is_glyphs_app_available_matches_flag(self, monkeypatch):
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', True)
		assert core.is_glyphs_app_available() is True
		monkeypatch.setattr(core, '_GLYPHS_AVAILABLE', False)
		assert core.is_glyphs_app_available() is False

	def test_fonttools_import_error_when_available(self):
		# When fontTools imported fine the error is None.
		assert core.fonttools_import_error() is None


# ---------------------------------------------------------------------------
# extension_for_format / flavor_for_format edge cases
# ---------------------------------------------------------------------------


class TestFormatEdgeCases:
	"""Edge cases for the format mappers — GLYPHS sentinel and flavor None."""

	def test_extension_for_glyphs(self):
		assert core.extension_for_format('GLYPHS') == '.glyphs'
		assert core.extension_for_format('glyphs') == '.glyphs'

	def test_flavor_for_glyphs_returns_none(self):
		# GLYPHS is not a fontTools flavor — should not pretend to be one.
		assert core.flavor_for_format('GLYPHS') is None
