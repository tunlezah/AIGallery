"""Content discovery + validation tests (milestone 2)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from content import Diagnostics, discover, normalise_tags, slugify

FIXTURES = Path(__file__).parent / "fixtures"


class SlugTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Foo.Bar"), "foo-bar")
        self.assertEqual(slugify("  Héllo  World!! "), "hello-world")
        self.assertEqual(slugify("--a--b--"), "a-b")

    def test_tags_normalised(self):
        diags = Diagnostics()
        tags = normalise_tags(["  Local ", "Frontier  Model", "local"], diags, "x")
        self.assertEqual(tags, ["frontier-model", "local"])
        tags = normalise_tags("a, B ,a", diags, "x")
        self.assertEqual(tags, ["a", "b"])


class DiscoveryTests(unittest.TestCase):
    def _discover(self, name):
        diags = Diagnostics()
        sections = discover(FIXTURES / name, diags)
        return sections, diags

    def test_valid_fixture(self):
        sections, diags = self._discover("content-valid")
        self.assertEqual(diags.errors, [])
        by_slug = {s.slug: s for s in sections}
        self.assertEqual(set(by_slug), {"models", "tooling", "research"})
        tooling = {i.slug for i in by_slug["tooling"].items}
        self.assertEqual(tooling, {"ollama", "vllm", "huge-body", "broken-image"})
        # draft excluded
        self.assertNotIn("draft-item", tooling)
        # warnings: stray file, broken image, unknown key, deep nesting
        text = "\n".join(diags.warnings)
        self.assertIn("stray.txt", text)
        self.assertIn("missing.png", text)
        self.assertIn("unknown_key", text)
        self.assertIn("deeper than two levels", text)

    def test_no_url_and_no_image(self):
        sections, _ = self._discover("content-valid")
        items = {i.slug: i for s in sections for i in s.items}
        self.assertIsNone(items["vllm"].url)
        self.assertIsNone(items["llama"].image_src)
        self.assertIsNone(items["broken-image"].image_src)  # warned fallback

    def test_comma_string_tags(self):
        sections, _ = self._discover("content-valid")
        items = {i.slug: i for s in sections for i in s.items}
        self.assertEqual(items["llama"].tags, ["local", "open-weights"])

    def test_malformed_yaml_is_hard_error_naming_file(self):
        _, diags = self._discover("content-malformed")
        self.assertEqual(len(diags.errors), 1)
        self.assertIn("index.md", diags.errors[0])
        self.assertIn("malformed YAML", diags.errors[0])

    def test_duplicate_slug_is_hard_error_naming_both(self):
        _, diags = self._discover("content-dupslug")
        joined = "\n".join(diags.errors)
        self.assertIn("duplicate slug", joined)
        self.assertIn("Foo.Bar", joined)
        self.assertIn("foo-bar", joined)

    def test_section_metadata(self):
        sections, _ = self._discover("content-valid")
        models = next(s for s in sections if s.slug == "models")
        self.assertEqual(models.title, "Models")
        self.assertEqual(models.order, 10)


class FieldValidationTests(unittest.TestCase):
    def test_bad_url_scheme_is_error(self):
        import tempfile
        from content import parse_item
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "thing"
            item_dir.mkdir()
            (item_dir / "index.md").write_text(
                '---\ntitle: "X"\nurl: "javascript:alert(1)"\n---\nbody\n'
            )
            diags = Diagnostics()
            parse_item(item_dir / "index.md", "s", diags)
            self.assertTrue(any("scheme" in e for e in diags.errors))

    def test_image_traversal_is_error(self):
        import tempfile
        from content import parse_item
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "thing"
            item_dir.mkdir()
            (item_dir / "index.md").write_text(
                '---\ntitle: "X"\nimage: "../../etc/passwd"\n---\nbody\n'
            )
            diags = Diagnostics()
            parse_item(item_dir / "index.md", "s", diags)
            self.assertTrue(any("travers" in e or "escape" in e for e in diags.errors))

    def test_absolute_image_is_error(self):
        import tempfile
        from content import parse_item
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "thing"
            item_dir.mkdir()
            (item_dir / "index.md").write_text(
                '---\ntitle: "X"\nimage: "/etc/passwd"\n---\nbody\n'
            )
            diags = Diagnostics()
            parse_item(item_dir / "index.md", "s", diags)
            self.assertTrue(diags.errors)

    def test_missing_title_is_error(self):
        import tempfile
        from content import parse_item
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "thing"
            item_dir.mkdir()
            (item_dir / "index.md").write_text("---\nurl: \"https://x.com\"\n---\nbody\n")
            diags = Diagnostics()
            result = parse_item(item_dir / "index.md", "s", diags)
            self.assertIsNone(result)
            self.assertTrue(any("title" in e for e in diags.errors))

    def test_invalid_date_warns_and_drops(self):
        import tempfile
        from content import parse_item
        with tempfile.TemporaryDirectory() as tmp:
            item_dir = Path(tmp) / "thing"
            item_dir.mkdir()
            (item_dir / "index.md").write_text(
                '---\ntitle: "X"\nadded: "not-a-date"\n---\nbody\n'
            )
            diags = Diagnostics()
            item = parse_item(item_dir / "index.md", "s", diags)
            self.assertIsNotNone(item)
            self.assertIsNone(item.added)
            self.assertEqual(diags.errors, [])
            self.assertTrue(any("added" in w for w in diags.warnings))

    def test_control_characters_in_url_are_errors(self):
        # Browsers strip tab/newline anywhere and leading C0 controls before
        # parsing, so these would all resolve to javascript: in the browser.
        from content import validate_url
        for bad in (
            "java\tscript:alert(1)",
            "java\nscript:alert(1)",
            "java\rscript:alert(1)",
            "\x01javascript:alert(1)",
            "https://example.com/\x7f",
        ):
            diags = Diagnostics()
            self.assertIsNone(validate_url(bad, diags, "x"), repr(bad))
            self.assertTrue(any("control" in e for e in diags.errors), repr(bad))

    def test_url_scheme_detection_and_relative_paths(self):
        from content import validate_url
        for bad in ("JavaScript:alert(1)", "data:text/html,x", "vbscript:x", "//evil.example/x",
                    "\\\\evil.example/x", "/\\evil.example/x"):
            diags = Diagnostics()
            self.assertIsNone(validate_url(bad, diags, "x"), repr(bad))
            self.assertTrue(diags.errors, repr(bad))
        for good in ("https://ok.example/a?b=c", "http://ok.example", "../other/", "docs/page:1", "#top"):
            diags = Diagnostics()
            self.assertEqual(validate_url(good, diags, "x"), good)
            self.assertEqual(diags.errors, [])


if __name__ == "__main__":
    unittest.main()
