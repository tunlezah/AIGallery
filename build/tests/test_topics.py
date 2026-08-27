"""Topic merge + soft-failure tests (milestone 6). No network anywhere."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config
from content import Diagnostics, Item
from topics import merge_topics, normalise_url_for_dedupe

FIXTURES = Path(__file__).parent / "fixtures"


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
        import tempfile
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


if __name__ == "__main__":
    unittest.main()
