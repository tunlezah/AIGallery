"""Topic merge + soft-failure tests. No network anywhere."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config
from content import Diagnostics, Item
from topics import merge_topics, normalise_url_for_dedupe

FIXTURES = Path(__file__).parent / "fixtures"
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\0" * 24


def make_cfg(**overrides):
    cfg = Config()
    cfg.content_repo_url = "https://gitlab.example.com/group/content.git"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def curated_fixture_items():
    return [
        Item(slug="ollama", section="tooling", source="content",
             title="Ollama", url="https://ollama.com"),
        Item(slug="claude", section="models", source="content",
             title="Claude", url="https://claude.com"),
    ]


def record(**overrides):
    base = {
        "id": 1,
        "path_with_namespace": "grp/proj",
        "name": "Proj",
        "name_with_namespace": "Grp / Proj",
        "description": "A project.",
        "web_url": "https://gitlab.example.com/grp/proj",
        "topics": ["ai-gallery", "tooling"],
        "created_at": "2026-01-01T00:00:00Z",
        "last_activity_at": "2026-02-01T00:00:00Z",
        "avatar_file": None,
        "image_file": None,
        "manifest": None,
    }
    base.update(overrides)
    return base


def write_snapshot(tmp, projects, **extra):
    data = {
        "schema": 1, "topic": "AI-Gallery", "instance": "https://gitlab.example.com",
        "fetched": True, "truncated": False, "partial": False, "projects": projects,
    }
    data.update(extra)
    path = Path(tmp) / "topics.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class DedupeNormalisationTests(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(normalise_url_for_dedupe("https://Ollama.com/"), "ollama.com")
        self.assertEqual(
            normalise_url_for_dedupe("http://a.b/Path/"),
            normalise_url_for_dedupe("https://A.B/path"),
        )


class MergeTests(unittest.TestCase):
    def test_success_path(self):
        diags = Diagnostics()
        section, meta = merge_topics(
            make_cfg(), curated_fixture_items(), FIXTURES / "topics/topics.json", diags,
        )
        self.assertEqual(diags.errors, [])
        self.assertEqual(diags.warnings, [])
        self.assertIsNotNone(section)
        self.assertEqual(section.slug, "subscribed")
        slugs = [item.slug for item in section.items]
        # sorted by path_with_namespace; archived, malformed and duplicate dropped
        self.assertEqual(
            slugs,
            ["infra-quiet-runner", "ml-group-eval-harness", "ml-group-prompt-forge"],
        )
        self.assertEqual(meta["count"], 3)
        self.assertEqual(meta["dropped_duplicates"], 1)
        self.assertFalse(meta["partial"])
        self.assertFalse(meta["truncated"])
        notices = "\n".join(diags.notices)
        self.assertIn("duplicates a curated item's url", notices)
        self.assertIn("archived", notices)
        self.assertIn("malformed project record", notices)

    def test_topic_tag_removed_and_tags_normalised(self):
        diags = Diagnostics()
        section, _ = merge_topics(
            make_cfg(), curated_fixture_items(), FIXTURES / "topics/topics.json", diags,
        )
        forge = next(i for i in section.items if i.slug == "ml-group-prompt-forge")
        self.assertEqual(forge.tags, ["dev-tools", "prompts"])
        self.assertNotIn("ai-gallery", forge.tags)

    def test_manifest_enrichment_and_sanitisation(self):
        diags = Diagnostics()
        section, _ = merge_topics(
            make_cfg(), curated_fixture_items(), FIXTURES / "topics/topics.json", diags,
        )
        harness = next(i for i in section.items if i.slug == "ml-group-eval-harness")
        self.assertEqual(harness.title, "Eval Harness Pro")
        self.assertEqual(harness.tags, ["benchmarks", "evals"])
        self.assertIn("Manifest-enriched", harness.summary)
        # same-instance url override accepted
        self.assertIn("/-/wikis/home", harness.url)
        # body sanitised
        self.assertIn("<strong>", harness.body_html)
        # raw HTML in the manifest is escaped to inert text, never live markup
        self.assertNotIn("<script", harness.body_html)

    def test_description_dates_avatar(self):
        diags = Diagnostics()
        section, _ = merge_topics(
            make_cfg(), curated_fixture_items(), FIXTURES / "topics/topics.json", diags,
        )
        forge = next(i for i in section.items if i.slug == "ml-group-prompt-forge")
        self.assertEqual(forge.summary, "A workbench for iterating on prompts. Second line collapses.")
        self.assertEqual(forge.added, "2026-02-01")
        self.assertEqual(forge.updated, "2026-08-12")
        self.assertIsNotNone(forge.image_src)
        runner = next(i for i in section.items if i.slug == "infra-quiet-runner")
        self.assertIsNone(runner.image_src)

    def test_missing_snapshot_is_soft(self):
        diags = Diagnostics()
        section, meta = merge_topics(
            make_cfg(), [], FIXTURES / "does-not-exist/topics.json", diags,
        )
        self.assertIsNone(section)
        self.assertFalse(meta["fetched"])
        self.assertEqual(diags.errors, [])
        self.assertTrue(diags.notices)

    def test_empty_snapshot_fetched_false_is_soft(self):
        diags = Diagnostics()
        section, meta = merge_topics(
            make_cfg(), [], FIXTURES / "topics-empty.json", diags,
        )
        self.assertIsNone(section)
        self.assertEqual(meta["count"], 0)
        self.assertEqual(diags.errors, [])

    def test_garbage_snapshot_is_soft(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "topics.json"
            bad.write_text("{not json", encoding="utf-8")
            diags = Diagnostics()
            section, _ = merge_topics(make_cfg(), [], bad, diags)
            self.assertIsNone(section)
            self.assertEqual(diags.errors, [])
            self.assertTrue(any("unreadable" in n for n in diags.notices))

    def test_disabled_topics(self):
        diags = Diagnostics()
        section, meta = merge_topics(
            make_cfg(topics_enabled=False), [], FIXTURES / "topics/topics.json", diags,
        )
        self.assertIsNone(section)
        self.assertFalse(meta["enabled"])
        self.assertEqual(diags.notices, [])


class ManifestFieldTests(unittest.TestCase):
    def _merge(self, tmp, projects, **extra):
        diags = Diagnostics()
        section, meta = merge_topics(make_cfg(), [], write_snapshot(tmp, projects, **extra), diags)
        self.assertEqual(diags.errors, [])
        self.assertEqual(diags.warnings, [])
        return section, meta, diags

    def test_draft_manifest_removes_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            section, meta, diags = self._merge(tmp, [record(manifest="---\ndraft: true\n---\nGone.\n")])
            self.assertIsNone(section)
            self.assertEqual(meta["count"], 0)
            self.assertTrue(any("draft" in n for n in diags.notices))

    def test_metadata_fields_override_derived_values(self):
        manifest = (
            "---\norder: 5\nadded: 2026-03-03\nupdated: \"2026-04-04\"\nfeatured: true\n---\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            section, _, _ = self._merge(tmp, [record(manifest=manifest)])
            item = section.items[0]
            self.assertEqual(item.order, 5)
            self.assertEqual(item.added, "2026-03-03")
            self.assertEqual(item.updated, "2026-04-04")
            self.assertTrue(item.featured)
            self.assertEqual(item.summary, "A project.")  # untouched fields keep derived values

    def test_bad_manifest_field_types_are_notices(self):
        manifest = "---\norder: \"ten\"\nfeatured: \"yes\"\nadded: \"not-a-date\"\n---\n"
        with tempfile.TemporaryDirectory() as tmp:
            section, _, diags = self._merge(tmp, [record(manifest=manifest)])
            item = section.items[0]
            self.assertEqual(item.order, 1000)
            self.assertFalse(item.featured)
            self.assertEqual(item.added, "2026-01-01")
            self.assertTrue(any("order" in n for n in diags.notices))
            self.assertTrue(any("featured" in n for n in diags.notices))
            self.assertTrue(any("added" in n for n in diags.notices))

    def test_manifest_image_beats_avatar(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "media" / "1"
            media.mkdir(parents=True)
            (media / "avatar.png").write_bytes(PNG_HEAD)
            (media / "image.png").write_bytes(PNG_HEAD)
            section, _, _ = self._merge(tmp, [record(avatar_file="media/1/avatar.png", image_file="media/1/image.png")])
            self.assertEqual(section.items[0].image_src.name, "image.png")

    def test_svg_and_unsafe_media_paths_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "media" / "1"
            media.mkdir(parents=True)
            (media / "avatar.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
            section, _, diags = self._merge(tmp, [
                record(avatar_file="media/1/avatar.svg"),
                record(id=2, path_with_namespace="grp/other", web_url="https://gitlab.example.com/grp/other",
                       avatar_file="../../etc/passwd.png"),
            ])
            for item in section.items:
                self.assertIsNone(item.image_src)
            notices = "\n".join(diags.notices)
            self.assertIn("SVG", notices)
            self.assertIn("unsafe", notices)

    def test_partial_and_truncated_flags_surface_in_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            section, meta, diags = self._merge(tmp, [record()], partial=True, truncated=True)
            self.assertIsNotNone(section)
            self.assertTrue(meta["partial"])
            self.assertTrue(meta["truncated"])
            self.assertTrue(any("partial" in n for n in diags.notices))
            self.assertTrue(any("truncated" in n for n in diags.notices))

    def test_control_characters_in_web_url_are_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            section, meta, diags = self._merge(tmp, [record(web_url="https://gitlab.example.com/x\ny")])
            self.assertIsNone(section)
            self.assertTrue(any("malformed" in n for n in diags.notices))


if __name__ == "__main__":
    unittest.main()
