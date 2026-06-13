# preview_view.py — animated NSView rendering a variable-font specimen.
#
# Specimen text is "HOHO Anes" — chosen because the H/O/N/e shapes expose
# weight/width/optical-size changes prominently:
#   • H — straight vertical strokes (weight visible)
#   • O — bowl shapes (weight + optical sizing visible)
#   • A — apex + diagonal strokes (weight + width visible)
#   • n — x-height shape (width + opsz visible)
#   • e — bowl + counter (opsz visible)
#   • s — curve + thinning (weight + opsz visible)
#
# This view animates the selected hull range over a ~2-second loop so the
# user sees what a customer would see if they instantiated the clamped
# font at any point in the licensed design space. It falls back to a
# system variable font when no source font is available (e.g. when the
# source is an open Glyphs document — that path needs CTFontManager
# registration of a temp binary which is a v1.2.3 follow-up).

import math
from typing import Dict, Optional, Tuple

try:
	import objc  # type: ignore
	from AppKit import (  # type: ignore
		NSView,
		NSColor,
		NSFont,
		NSFontDescriptor,
		NSFontVariationAttribute,
		NSAttributedString,
		NSForegroundColorAttributeName,
		NSFontAttributeName,
		NSParagraphStyleAttributeName,
		NSMutableParagraphStyle,
		NSTextAlignmentCenter,
		NSTimer,
	)
	from Foundation import NSMakeRect, NSMakePoint, NSMakeSize  # type: ignore
	_APPKIT_AVAILABLE = True
except Exception:  # noqa: BLE001 — AppKit missing on CI
	_APPKIT_AVAILABLE = False


SPECIMEN_TEXT = 'HOHO Anes'

# Animation period in seconds — one full back-and-forth cycle.
ANIM_PERIOD = 2.4

# Frame interval — 30 fps is plenty for a smooth axis sweep without
# burning CPU; the user is watching, not measuring latency.
FRAME_INTERVAL = 1.0 / 30.0


def is_available() -> bool:
	"""Return True iff AppKit is importable on this runtime."""
	return _APPKIT_AVAILABLE


def _axis_tag_to_int(tag: str) -> int:
	"""Pack a 4-byte axis tag (e.g. 'wght') into the 32-bit int that
	NSFontVariationAttribute keys expect. NSFontDescriptor uses the
	OpenType axis tag as a big-endian uint32 identifier."""
	if not tag or len(tag) > 4:
		return 0
	pad = tag.ljust(4)
	return (ord(pad[0]) << 24) | (ord(pad[1]) << 16) | (ord(pad[2]) << 8) | ord(pad[3])


if _APPKIT_AVAILABLE:

	class AnimatedPreviewView(NSView):
		"""Custom NSView that draws SPECIMEN_TEXT and cycles axis variations.

		Public methods (call from Python):
		    setHull_(hull_dict)              — set the axis ranges to animate over
		    setFontDescriptor_(font_desc)    — set the underlying font (optional)
		    startAnimating()                 — begin the timer loop
		    stopAnimating()                  — stop the timer loop
		"""

		def init(self):  # type: ignore[override]
			self = objc.super(AnimatedPreviewView, self).init()
			if self is None:
				return None
			self._hull = {}  # type: Dict[str, Tuple[float, float]]
			self._font_size = 64.0
			self._t0 = 0.0
			self._timer = None
			self._base_descriptor = None
			self._anim_progress = 0.0
			return self

		# --------- public PyObjC entry points (Python-callable) ----------

		def setHull_(self, hull):
			"""hull: dict[axis_tag, (lo, hi)] — pinned axes contribute lo==hi."""
			self._hull = dict(hull) if hull else {}
			self.setNeedsDisplay_(True)

		def setFontDescriptor_(self, descriptor):
			"""Optional override of the underlying font descriptor.

			When None (the default), the view falls back to the system
			variable font for animation. When the caller registers a real
			variable font via CTFontManagerRegisterFontsForURL_, pass the
			descriptor from that font here.
			"""
			self._base_descriptor = descriptor
			self.setNeedsDisplay_(True)

		def setFontSize_(self, size):
			try:
				self._font_size = float(size)
			except (TypeError, ValueError):
				pass
			self.setNeedsDisplay_(True)

		def startAnimating(self):
			"""Begin (or restart) the animation timer."""
			self.stopAnimating()
			try:
				self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
					FRAME_INTERVAL, self, 'tick:', None, True,
				)
			except (AttributeError, RuntimeError):
				self._timer = None

		def stopAnimating(self):
			"""Stop the timer if running."""
			t = self._timer
			self._timer = None
			if t is not None:
				try:
					t.invalidate()
				except (AttributeError, RuntimeError):
					pass

		# --------- ObjC selectors --------------------------------------

		def tick_(self, _timer):
			"""NSTimer callback — advance the animation phase and redraw."""
			self._anim_progress = (self._anim_progress + FRAME_INTERVAL) % ANIM_PERIOD
			self.setNeedsDisplay_(True)

		def isOpaque(self):
			return False

		def drawRect_(self, dirtyRect):
			try:
				self._draw()
			except Exception:
				# Never let a draw error tear down the timer or the host
				# vanilla widget hierarchy.
				pass

		# --------- private rendering -----------------------------------

		def _draw(self):
			bounds = self.bounds()
			bg = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.18)
			bg.set()
			from AppKit import NSRectFill  # local import — Foundation is shared
			NSRectFill(bounds)

			# Compute current variation settings from hull + animation phase.
			variations = self._current_variations()
			font = self._build_font(variations)
			if font is None:
				return

			# Build the attributed specimen string, centred in the view.
			fg = NSColor.labelColor()
			para = NSMutableParagraphStyle.alloc().init()
			para.setAlignment_(NSTextAlignmentCenter)
			attrs = {
				NSFontAttributeName: font,
				NSForegroundColorAttributeName: fg,
				NSParagraphStyleAttributeName: para,
			}
			specimen = NSAttributedString.alloc().initWithString_attributes_(
				SPECIMEN_TEXT, attrs,
			)

			# Centre vertically in the view's bounds.
			text_size = specimen.size()
			origin_x = (bounds.size.width - text_size.width) / 2.0
			origin_y = (bounds.size.height - text_size.height) / 2.0
			specimen.drawAtPoint_(NSMakePoint(origin_x, origin_y))

			# Caption row beneath: show the live variation values so the
			# user can see what's being animated.
			caption = self._caption_text(variations)
			caption_font = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize())
			caption_attrs = {
				NSFontAttributeName: caption_font,
				NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
				NSParagraphStyleAttributeName: para,
			}
			caption_str = NSAttributedString.alloc().initWithString_attributes_(
				caption, caption_attrs,
			)
			cap_size = caption_str.size()
			caption_str.drawAtPoint_(NSMakePoint(
				(bounds.size.width - cap_size.width) / 2.0,
				6,  # bottom margin
			))

		# --------- helpers ---------------------------------------------

		def _current_variations(self):
			"""Compute axis values for the current animation phase.

			Each axis sweeps lo → hi → lo over ANIM_PERIOD. Pinned axes
			(lo == hi) emit a constant value. Axes are phase-offset by tag
			so wght and wdth don't sweep in lockstep — gives a richer feel.
			"""
			out = {}
			phase = self._anim_progress / ANIM_PERIOD  # 0 .. 1
			i = 0
			for tag, rng in self._hull.items():
				try:
					lo, hi = rng
				except (TypeError, ValueError):
					continue
				if lo == hi:
					out[tag] = float(lo)
					continue
				# Sine wave back-and-forth, with an axis-dependent phase.
				offset = (i * 0.17) % 1.0
				p = (phase + offset) % 1.0
				# 0 .. 1 .. 0 mapped to lo .. hi .. lo via cosine
				k = (1.0 - math.cos(2.0 * math.pi * p)) / 2.0  # 0..1..0
				out[tag] = float(lo) + (float(hi) - float(lo)) * k
				i += 1
			return out

		def _build_font(self, variations):
			"""Build an NSFont with the given variation settings.

			Returns None if neither a custom descriptor nor a system
			variable font can be resolved.
			"""
			try:
				size = self._font_size
				if self._base_descriptor is not None:
					base = self._base_descriptor
				else:
					# Fall back to the system variable font. macOS .AppleSystemUIFont
					# supports `wght` and `opsz` axes natively.
					base = NSFont.systemFontOfSize_(size).fontDescriptor()

				if variations:
					axis_dict = {
						_axis_tag_to_int(tag): value
						for tag, value in variations.items()
						if _axis_tag_to_int(tag) != 0
					}
					if axis_dict:
						base = base.fontDescriptorByAddingAttributes_({
							NSFontVariationAttribute: axis_dict,
						})

				font = NSFont.fontWithDescriptor_size_(base, size)
				return font
			except (AttributeError, RuntimeError, ValueError):
				return None

		def _caption_text(self, variations):
			if not variations:
				return '(select instances to preview)'
			parts = []
			for tag, val in variations.items():
				if val == int(val):
					parts.append(f'{tag} {int(val)}')
				else:
					parts.append(f'{tag} {val:.1f}')
			return '  ·  '.join(parts)


def make_preview_view(frame: Tuple[float, float, float, float]):
	"""Return a configured AnimatedPreviewView, or None on a non-AppKit runtime.

	Caller is responsible for mounting the view into the window content view
	at the given frame and calling startAnimating() / stopAnimating()
	at lifecycle points.
	"""
	if not _APPKIT_AVAILABLE:
		return None
	try:
		view = AnimatedPreviewView.alloc().init()
		view.setFrame_(NSMakeRect(*frame))
		return view
	except (AttributeError, RuntimeError):
		return None
