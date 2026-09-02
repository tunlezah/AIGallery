"""Config loader tests: TOML values, env overrides + coercion, and the rule
that topic-block problems are notices while core problems are fatal."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(tmp, toml=None, environ=None, require=False):
    path = Path(tmp) / "gallery.config.toml"
    if toml is not None:
        path.write_text(toml, encoding="utf-8")
    return load_config(path, require_content_repo=require, environ=environ or {})


class ConfigTests(unittest.TestCase):
    def test_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, notices = load(tmp)
            self.assertEqual(cfg.topic_name, "AI-Gallery")
            self.assertEqual(cfg.thumbnail_width, 640)
            self.assertEqual(cfg.topic_allow_namespaces, [])
            self.assertTrue(any("not found" in n for n in notices))

    def test_repo_config_loads_cleanly(self):
        cfg, notices = load_config(REPO_ROOT / "gallery.config.toml", environ={})
        self.assertEqual(cfg.default_image, "assets/AIGallery.png")
        self.assertEqual(notices, [])

    def test_toml_values_and_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, notices = load(tmp, 'site_title = "Zoo"\ncards_per_page = 24\nbogus_key = 1\n'
                                     'topic_allow_namespaces = ["ml/*"]\n')
            self.assertEqual(cfg.site_title, "Zoo")
            self.assertEqual(cfg.cards_per_page, 24)
            self.assertEqual(cfg.topic_allow_namespaces, ["ml/*"])
            self.assertTrue(any("bogus_key" in n for n in notices))

    def test_env_overrides_and_coercion(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = load(tmp, 'strict = false\n', environ={
                "GALLERY_STRICT": "yes",
                "GALLERY_CARDS_PER_PAGE": " 12 ",
                "GALLERY_TOPIC_EXCLUDE": "a/*, b/*",
                "GALLERY_SITE_TITLE": "From env",
                "GALLERY_TOPICS_ENABLED": "off",
            })
            self.assertTrue(cfg.strict)
            self.assertEqual(cfg.cards_per_page, 12)
            self.assertEqual(cfg.topic_exclude, ["a/*", "b/*"])
            self.assertEqual(cfg.site_title, "From env")
            self.assertFalse(cfg.topics_enabled)

    def test_bad_core_env_value_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                load(tmp, environ={"GALLERY_CARDS_PER_PAGE": "lots"})
            with self.assertRaises(SystemExit):
                load(tmp, environ={"GALLERY_STRICT": "maybe"})

    def test_bad_topic_env_value_is_a_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, notices = load(tmp, environ={"GALLERY_TOPIC_MAX_PROJECTS": "lots"})
            self.assertEqual(cfg.topic_max_projects, 200)
            self.assertTrue(any("TOPIC_MAX_PROJECTS" in n for n in notices))

    def test_wrong_toml_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, notices = load(tmp, 'topic_timeout_seconds = "20"\n')
            self.assertEqual(cfg.topic_timeout_seconds, 20)  # default kept, notice only
            self.assertTrue(any("topic_timeout_seconds" in n for n in notices))
            cfg, notices = load(tmp, 'topic_exclude = ["ok", 3]\n')
            self.assertEqual(cfg.topic_exclude, [])
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    load(tmp, 'cards_per_page = "x"\n')
                with self.assertRaises(SystemExit):
                    load(tmp, 'generate_thumbnails = 1\n')  # ints are not booleans

    def test_enumerations_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    load(tmp, 'default_sort = "random"\n')
            cfg, notices = load(tmp, 'topic_visibility = "secret"\n')
            self.assertEqual(cfg.topic_visibility, "")
            self.assertTrue(any("topic_visibility" in n for n in notices))

    def test_content_repo_required_for_the_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    load(tmp, "", require=True)
            cfg, _ = load(tmp, "", environ={"GALLERY_CONTENT_REPO_URL": "https://h/x.git"}, require=True)
            self.assertEqual(cfg.content_repo_url, "https://h/x.git")

    def test_malformed_toml_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                load(tmp, "this is = not [ toml\n")


if __name__ == "__main__":
    unittest.main()
