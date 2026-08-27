"""Image copying, dimension probing and thumbnail tests (milestones 4/15)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from content import Diagnostics
from images import probe_dimensions, process_image

FIXTURES = Path(__file__).parent / "fixtures"


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
        src = Path(__file__).resolve().parents[2] / "src/assets/placeholder.svg"
        self.assertEqual(probe_dimensions(src), (800, 600))


class ProcessTests(unittest.TestCase):
    def test_copy_and_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            diags = Diagnostics()
            result = process_image(
                FIXTURES / "content-valid/models/claude/cover.png",
                Path(tmp), "models/claude", diags,
                generate_thumbnails=True, thumbnail_width=640,
                pillow_missing_logged=[False],
            )
            self.assertEqual(result["image"], "media/models/claude/cover.png")
            self.assertEqual(result["thumb"], "media/models/claude/cover.640.webp")
            self.assertEqual((result["image_w"], result["image_h"]), (1200, 630))
            self.assertTrue((Path(tmp) / "models/claude/cover.png").is_file())
            self.assertTrue((Path(tmp) / "models/claude/cover.640.webp").is_file())

    def test_no_thumbnail_when_narrower_than_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            diags = Diagnostics()
            result = process_image(
                FIXTURES / "topics/media/42/avatar.png",
                Path(tmp), "subscribed/x", diags,
                generate_thumbnails=True, thumbnail_width=640,
                pillow_missing_logged=[False],
            )
            self.assertEqual(result["thumb"], result["image"])
            self.assertEqual((result["image_w"], result["image_h"]), (128, 128))

    def test_unsupported_extension_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "evil.exe"
            bad.write_bytes(b"MZ")
            diags = Diagnostics()
            result = process_image(
                bad, Path(tmp) / "out", "s/i", diags,
                generate_thumbnails=False, thumbnail_width=640,
                pillow_missing_logged=[False],
            )
            self.assertIsNone(result)
            self.assertTrue(diags.warnings)


if __name__ == "__main__":
    unittest.main()
