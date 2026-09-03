"""Image copying, dimension probing, thumbnails, sniffing and SVG safety tests."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from content import Diagnostics
from images import (
    derive_default_image,
    extension_matches,
    probe_dimensions,
    process_image,
    sniff_image_type,
    svg_unsafe_reason,
)

FIXTURES = Path(__file__).parent / "fixtures"
SRC = Path(__file__).resolve().parents[2] / "src"
SVG_NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


def _process(src, tmp, rel="s/i", **kw):
    diags = Diagnostics()
    kw.setdefault("generate_thumbnails", True)
    kw.setdefault("thumbnail_width", 640)
    result = process_image(src, Path(tmp), rel, diags, pillow_missing_logged=[False], **kw)
    return result, diags


class ProbeTests(unittest.TestCase):
    def test_png(self):
        self.assertEqual(
            probe_dimensions(FIXTURES / "content-valid/models/claude/cover.png"),
            (1200, 630),
        )

    def test_webp(self):
        self.assertEqual(
            probe_dimensions(FIXTURES / "content-valid/tooling/ollama/cover.webp"),
            (800, 600),
        )

    def test_svg(self):
        self.assertEqual(probe_dimensions(SRC / "assets/placeholder.svg"), (800, 600))


class SniffTests(unittest.TestCase):
    def test_magic_bytes(self):
        png = (FIXTURES / "content-valid/models/claude/cover.png").read_bytes()[:16]
        webp = (FIXTURES / "content-valid/tooling/ollama/cover.webp").read_bytes()[:16]
        self.assertEqual(sniff_image_type(png), ".png")
        self.assertEqual(sniff_image_type(webp), ".webp")
        self.assertEqual(sniff_image_type(b"GIF89a" + b"\0" * 10), ".gif")
        self.assertEqual(sniff_image_type(b"\xff\xd8\xff\xe0" + b"\0" * 12), ".jpg")
        self.assertIsNone(sniff_image_type(b"<svg xmlns='http://www.w3.org/2000/svg'/>"))
        self.assertIsNone(sniff_image_type(b"MZ"))

    def test_extension_matches(self):
        self.assertTrue(extension_matches(".jpeg", ".jpg"))
        self.assertTrue(extension_matches(".PNG", ".png"))
        self.assertFalse(extension_matches(".png", ".gif"))
        self.assertFalse(extension_matches(".png", None))


class SvgSafetyTests(unittest.TestCase):
    def _svg(self, tmp, body):
        path = Path(tmp) / "x.svg"
        path.write_text(body, encoding="utf-8")
        return path

    def test_clean_svgs_pass(self):
        self.assertIsNone(svg_unsafe_reason(SRC / "assets/placeholder.svg"))
        self.assertIsNone(svg_unsafe_reason(SRC / "favicon.svg"))
        self.assertIsNone(svg_unsafe_reason(FIXTURES / "content-valid/research/notes/cover.svg"))

    def test_script_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            reason = svg_unsafe_reason(self._svg(tmp, f"<svg {SVG_NS}><script>alert(1)</script></svg>"))
            self.assertIn("script", reason)

    def test_event_handler_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            reason = svg_unsafe_reason(self._svg(tmp, f'<svg {SVG_NS} onload="alert(1)"><rect/></svg>'))
            self.assertIn("event handler", reason)

    def test_javascript_href_with_tab_entity_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = f'<svg {SVG_NS}><a xlink:href="java&#x9;script:alert(1)"><text>x</text></a></svg>'
            self.assertIn("active URL", svg_unsafe_reason(self._svg(tmp, svg)))

    def test_animation_target_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = f'<svg {SVG_NS}><a href="#a"><set attributeName="href" to="javascript:alert(1)"/><text>x</text></a></svg>'
            self.assertIn("active URL", svg_unsafe_reason(self._svg(tmp, svg)))

    def test_external_reference_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = f'<svg {SVG_NS}><image href="https://evil.example/x.png"/></svg>'
            self.assertIn("external reference", svg_unsafe_reason(self._svg(tmp, svg)))

    def test_foreign_object_and_style_url_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = f'<svg {SVG_NS}><foreignObject><div>x</div></foreignObject></svg>'
            self.assertIn("foreignobject", svg_unsafe_reason(self._svg(tmp, svg)))
            svg = f'<svg {SVG_NS}><style>rect {{ fill: url(https://evil.example/p.svg#g) }}</style><rect/></svg>'
            self.assertIn("url()", svg_unsafe_reason(self._svg(tmp, svg)))

    def test_entities_and_malformed_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = f'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]><svg {SVG_NS}>&x;</svg>'
            self.assertIn("ENTITY", svg_unsafe_reason(self._svg(tmp, svg)))
            self.assertIn("well-formed", svg_unsafe_reason(self._svg(tmp, "<svg><rect></svg>")))

    def test_process_image_fails_build_on_unsafe_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = FIXTURES / "content-unsafe-svg/models/evil/cover.svg"
            result, diags = _process(src, Path(tmp) / "out")
            self.assertIsNone(result)
            self.assertTrue(any("SVG rejected" in e for e in diags.errors))

    def test_process_image_refuses_svg_from_third_parties(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, diags = _process(SRC / "assets/placeholder.svg", Path(tmp) / "out", allow_svg=False)
            self.assertIsNone(result)
            self.assertEqual(diags.errors, [])
            self.assertTrue(any("SVG" in w for w in diags.warnings))

    def test_process_image_copies_safe_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, diags = _process(FIXTURES / "content-valid/research/notes/cover.svg", tmp, "research/notes")
            self.assertEqual(result["image"], "media/research/notes/cover.svg")
            self.assertEqual(result["thumb"], result["image"])  # SVGs are never thumbnailed
            self.assertEqual(diags.errors, [])


class ProcessTests(unittest.TestCase):
    def test_copy_and_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, diags = _process(FIXTURES / "content-valid/models/claude/cover.png", tmp, "models/claude")
            self.assertEqual(result["image"], "media/models/claude/cover.png")
            self.assertEqual(result["thumb"], "media/models/claude/cover.640.webp")
            self.assertEqual((result["image_w"], result["image_h"]), (1200, 630))
            self.assertTrue((Path(tmp) / "models/claude/cover.png").is_file())
            self.assertTrue((Path(tmp) / "models/claude/cover.640.webp").is_file())

    def test_no_thumbnail_when_narrower_than_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _process(FIXTURES / "topics/media/42/avatar.png", tmp, "subscribed/x")
            self.assertEqual(result["thumb"], result["image"])
            self.assertEqual((result["image_w"], result["image_h"]), (128, 128))

    def test_unsupported_extension_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "evil.exe"
            bad.write_bytes(b"MZ")
            result, diags = _process(bad, Path(tmp) / "out", generate_thumbnails=False)
            self.assertIsNone(result)
            self.assertTrue(diags.warnings)


class DefaultImageTests(unittest.TestCase):
    def _derive(self, src, tmp, **kw):
        diags = Diagnostics()
        kw.setdefault("generate_thumbnails", True)
        kw.setdefault("thumbnail_width", 640)
        result = derive_default_image(src, Path(tmp) / "media", diags, pillow_missing_logged=[False], **kw)
        return result, diags

    def test_large_raster_becomes_tile_sized_webp(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, diags = self._derive(FIXTURES / "content-valid/models/claude/cover.png", tmp)
            self.assertEqual(result, "media/gallery-default.640.webp")
            self.assertTrue((Path(tmp) / result).is_file())
            self.assertFalse((Path(tmp) / "media/gallery-default.png").exists())  # original not shipped
            self.assertEqual(diags.errors, [])

    def test_small_raster_and_svg_are_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._derive(FIXTURES / "topics/media/42/avatar.png", tmp)
            self.assertEqual(result, "media/gallery-default.png")
            result, _ = self._derive(SRC / "assets/placeholder.svg", tmp)
            self.assertEqual(result, "media/gallery-default.svg")

    def test_thumbnails_disabled_copies_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._derive(FIXTURES / "content-valid/models/claude/cover.png", tmp,
                                     generate_thumbnails=False)
            self.assertEqual(result, "media/gallery-default.png")

    def test_unsafe_svg_default_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, diags = self._derive(FIXTURES / "content-unsafe-svg/models/evil/cover.svg", tmp)
            self.assertIsNone(result)
            self.assertTrue(diags.errors)


if __name__ == "__main__":
    unittest.main()
