"""End-to-end generator tests: build against fixtures, determinism,
strict-mode behaviour, topic soft-failure at build level, rendering of
featured/icons/footer status, and the lean inline index."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from content import Item  # noqa: E402
from config import Config  # noqa: E402
from generate import (  # noqa: E402
    apply_conditional_blocks,
    help_slots,
    public_repo_url,
    sort_items,
    topic_instance_host,
)

BUILD = Path(__file__).resolve().parents[1]
ROOT = BUILD.parent
FIXTURES = Path(__file__).parent / "fixtures"

BASE_ENV = {
    **os.environ,
    "GALLERY_CONTENT_REPO_URL": "https://gitlab.example.com/group/content.git",
}

DEFAULT_WEBP = "media/gallery-default.640.webp"


def run_build(out_dir, *extra, content="content-valid",
              snapshot=None, env_extra=None, standalone=False):
    env = {**BASE_ENV, **(env_extra or {})}
    cmd = [
        sys.executable, str(BUILD / "generate.py"),
        "--content-dir", str(FIXTURES / content),
        "--out", str(out_dir),
    ]
    if snapshot is not None:
        cmd += ["--topics-snapshot", str(snapshot)]
    else:
        cmd += ["--no-topics"]
    if standalone:
        cmd += ["--standalone", "--dist", str(Path(out_dir).parent / "dist-standalone")]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def tree_bytes(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


def inline_payload(html: str) -> dict:
    start = html.index('id="gallery-data">') + len('id="gallery-data">')
    end = html.index("</script>", start)
    return json.loads(html[start:end])


class GenerateTests(unittest.TestCase):
    def test_valid_build_succeeds_with_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertTrue((out / "index.html").is_file())
            self.assertTrue((out / "gallery.json").is_file())
            index = json.loads((out / "gallery.json").read_text())
            self.assertGreater(len(index["items"]), 0)
            # every image/thumb referenced exists on disk
            for item in index["items"]:
                for key in ("image", "thumb"):
                    if item[key]:
                        self.assertTrue((out / item[key]).is_file(), item[key])
            # the default image ships tile-sized under media/, not as the
            # 1.5 MB source; the built-in placeholder still travels with src/
            self.assertEqual(index["site"]["default_image"], DEFAULT_WEBP)
            self.assertTrue((out / DEFAULT_WEBP).is_file())
            self.assertLess((out / DEFAULT_WEBP).stat().st_size, 60 * 1024)
            self.assertFalse((out / "assets/AIGallery.png").exists())
            self.assertTrue((out / "assets/placeholder.svg").is_file())
            self.assertIn("missing.png", proc.stderr)
            html = (out / "index.html").read_text()
            # imageless/broken-image tiles are baked with the default image
            self.assertIn(f'src="{DEFAULT_WEBP}"', html)
            # help dialog baked with config-derived values
            self.assertIn('id="help-dialog"', html)
            self.assertIn("<code>AI-Gallery</code>", html)
            self.assertIn("<code>.ai-gallery/index.md</code>", html)
            self.assertIn("is:featured", html)
            self.assertNotIn("{{", html)  # no template slot left unreplaced
            self.assertNotIn("<!--if:", html)  # every conditional block resolved
            # draft excluded
            self.assertNotIn("draft-item", (out / "gallery.json").read_text())
            # no topic footer line when topics are skipped
            self.assertNotIn('class="topic-status', html)

    def test_featured_badge_and_section_icon_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (out / "index.html").read_text()
            self.assertIn('data-id="models/claude" data-featured="true"', html)
            self.assertIn('class="tile-badge tile-badge-featured">Featured<', html)
            self.assertEqual(html.count("tile-badge-featured"), 1)
            # section icon: copied, referenced in heading + rail, in the index
            self.assertTrue((out / "media/models/icon.png").is_file())
            self.assertIn('<img class="section-icon" src="media/models/icon.png" width="64" height="64"', html)
            self.assertIn('<img class="rail-icon" src="media/models/icon.png"', html)
            index = json.loads((out / "gallery.json").read_text())
            models = next(s for s in index["sections"] if s["slug"] == "models")
            self.assertEqual(models["icon"], "media/models/icon.png")
            # the research item's safe SVG cover passed the validator
            self.assertTrue((out / "media/research/notes/cover.svg").is_file())
            # markdown table from the fixture body rendered as a table
            self.assertIn("<table>", html)
            self.assertIn("<del>RNN</del>", html)

    def test_inline_index_is_lean(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            run_build(out)
            html = (out / "index.html").read_text()
            payload = inline_payload(html)
            self.assertEqual(payload["runtime"]["placeholder"], DEFAULT_WEBP)
            item = payload["items"][0]
            for key in ("id", "section", "source", "title", "tags", "search", "order", "added", "updated", "featured"):
                self.assertIn(key, item)
            for key in ("body_html", "summary", "image", "thumb", "image_w", "image_h", "url"):
                self.assertNotIn(key, item)
            self.assertNotIn("sections", payload)
            self.assertNotIn("tags", payload)

    def test_determinism_no_topics(self):
        with tempfile.TemporaryDirectory() as tmp:
            out1, out2 = Path(tmp) / "a", Path(tmp) / "b"
            p1 = run_build(out1)
            p2 = run_build(out2)
            self.assertEqual(p1.returncode, 0, p1.stderr)
            self.assertEqual(p2.returncode, 0, p2.stderr)
            self.assertEqual(tree_bytes(out1), tree_bytes(out2))

    def test_malformed_frontmatter_fails_naming_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_build(Path(tmp) / "public", content="content-malformed")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("index.md", proc.stderr)

    def test_duplicate_slug_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_build(Path(tmp) / "public", content="content-dupslug")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("duplicate slug", proc.stderr)

    def test_unsafe_svg_fails_naming_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, content="content-unsafe-svg")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("cover.svg", proc.stderr)
            self.assertIn("SVG rejected", proc.stderr)
            self.assertFalse((out / "index.html").exists())

    def test_strict_escalates_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_build(Path(tmp) / "public", env_extra={"GALLERY_STRICT": "true"})
            self.assertNotEqual(proc.returncode, 0)  # fixture has warnings

    def test_strict_does_not_escalate_topic_notices(self):
        # A clean content tree + topic problems must pass under strict.
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content" / "models" / "clean"
            content.mkdir(parents=True)
            (content / "index.md").write_text('---\ntitle: "Clean"\n---\nBody.\n')
            proc = subprocess.run(
                [sys.executable, str(BUILD / "generate.py"),
                 "--content-dir", str(Path(tmp) / "content"),
                 "--out", str(Path(tmp) / "public"),
                 "--topics-snapshot", str(Path(tmp) / "absent.json")],
                capture_output=True, text=True,
                env={**BASE_ENV, "GALLERY_STRICT": "true"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("notice:", proc.stderr)

    def test_topic_success_path_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, snapshot=FIXTURES / "topics/topics.json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            index = json.loads((out / "gallery.json").read_text())
            topic_items = [i for i in index["items"] if i["source"] == "topic"]
            self.assertEqual(len(topic_items), 3)
            slugs = {s["slug"] for s in index["sections"]}
            self.assertIn("subscribed", slugs)
            # avatars localised into public media
            forge = next(i for i in topic_items if "prompt-forge" in i["id"])
            self.assertTrue(forge["image"].startswith("media/subscribed/"))
            self.assertTrue((out / forge["image"]).is_file())
            # topic tag itself absent from tags
            for item in topic_items:
                self.assertNotIn("ai-gallery", item["tags"])
            # duplicate dropped in favour of curated
            self.assertNotIn("ollama-mirror", (out / "gallery.json").read_text())
            self.assertEqual(index["generated_from"]["topic"]["dropped_duplicates"], 1)
            # baked HTML carries the badge + section + footer status
            html = (out / "index.html").read_text()
            self.assertIn("from topic", html)
            self.assertIn('id="section-subscribed"', html)
            self.assertIn('<p class="topic-status">3 subscribed projects discovered via the GitLab topic <code>AI-Gallery</code>', html)

    def test_topic_empty_snapshot_no_section_but_footer_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, snapshot=FIXTURES / "topics-empty.json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (out / "index.html").read_text()
            # no section, no rail entry (the help dialog may still name it)
            self.assertNotIn('id="section-subscribed"', html)
            self.assertNotIn('data-section="subscribed"', html)
            self.assertIn("topic-status-warn", html)
            self.assertIn("was unavailable when this build ran", html)

    def test_topic_absent_snapshot_no_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, snapshot=Path(tmp) / "nope.json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (out / "index.html").read_text()
            self.assertNotIn('data-section="subscribed"', html)
            self.assertIn("topic-status-warn", html)

    def test_standalone_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, snapshot=FIXTURES / "topics/topics.json", standalone=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            dist = Path(tmp) / "dist-standalone"
            html = (dist / "index.html").read_text()
            self.assertTrue((dist / "media").is_dir())
            # default image travels tile-sized in media/ (not a repeated data URI)
            self.assertTrue((dist / DEFAULT_WEBP).is_file())
            self.assertFalse((dist / "media" / "gallery-default.png").exists())
            self.assertIn(f'src="{DEFAULT_WEBP}"', html)
            # no CSP meta, no external script/style refs in standalone
            self.assertNotIn("Content-Security-Policy", html)
            self.assertNotIn('src="./assets/', html)
            self.assertNotIn('href="./assets/', html)
            # hosted keeps CSP + external classic scripts
            hosted = (out / "index.html").read_text()
            self.assertIn("Content-Security-Policy", hosted)
            self.assertIn('script src="./assets/app.js" defer', hosted.replace("<", ""))

    def test_single_file_inlines_default_image_and_runtime_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, "--single-file", standalone=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (Path(tmp) / "dist-standalone" / "index.html").read_text()
            self.assertNotIn(f'src="{DEFAULT_WEBP}"', html)
            self.assertIn('src="data:image/webp;base64,', html)
            self.assertTrue(inline_payload(html)["runtime"]["placeholder"].startswith("data:image/webp;base64,"))

    def test_no_file_scheme_hazards_in_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            run_build(out, snapshot=FIXTURES / "topics/topics.json", standalone=True)
            dist = Path(tmp) / "dist-standalone"
            for path in [out / "index.html", dist / "index.html",
                         out / "assets/app.js", out / "assets/theme-init.js"]:
                text = path.read_text()
                for hazard in ("fetch(", "XMLHttpRequest", 'type="module"',
                               "import ", "pushState", "replaceState"):
                    self.assertNotIn(hazard, text, f"{hazard} in {path.name}")

    def test_data_block_escaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            run_build(out)
            html = (out / "index.html").read_text()
            start = html.index('id="gallery-data">') + len('id="gallery-data">')
            end = html.index("</script>", start)
            blob = html[start:end]
            self.assertNotIn("<", blob)
            json.loads(blob)  # still valid JSON

    def test_help_link_never_publishes_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, env_extra={
                "GALLERY_CONTENT_REPO_URL": "https://deploy:sekrit-value-123@gitlab.example.com/g/content.git",
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for name in ("index.html", "gallery.json"):
                text = (out / name).read_text()
                self.assertNotIn("sekrit-value-123", text, name)
                self.assertNotIn("deploy:", text, name)
            self.assertIn('href="https://gitlab.example.com/g/content"', (out / "index.html").read_text())

    def test_help_dialog_is_tabbed_and_baked_from_config(self):
        # A --no-topics build still documents the *deployed* gallery, where
        # topics are enabled, so the topic tab is present.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (out / "index.html").read_text()
            for needle in (
                'role="tablist"', 'id="help-tab-mr"', 'id="help-tab-topic"', 'id="help-tab-search"',
                'id="help-panel-mr"', 'id="help-panel-topic"', 'id="help-panel-search"',
                'class="help-req">required<', 'class="help-opt">optional<',
                "<strong>Subscribed</strong> section", "up to 512 KB", "cut off at 200 projects",
                "<li>The project is not archived.</li>", 'href="#help"',
            ):
                self.assertIn(needle, html, needle)
            # defaults: no namespace restriction, no visibility filter, strict off
            self.assertNotIn("namespace this gallery accepts", html)
            self.assertIn("an internal or private one only when", html)
            self.assertNotIn("strict mode", html)
            # no snapshot: the instance falls back to the content repo's host
            self.assertIn("Any project on <strong>gitlab.example.com</strong>", html)

    def test_help_dialog_reflects_topic_scope_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, env_extra={
                "GALLERY_TOPIC_ALLOW_NAMESPACES": "ml-group/*, platform/ai-*",
                "GALLERY_TOPIC_VISIBILITY": "public",
                "GALLERY_TOPIC_INCLUDE_ARCHIVED": "true",
                "GALLERY_TOPIC_AVATAR_MAX_KB": "64",
                "GALLERY_TOPIC_SECTION_TITLE": "Community <picks>",
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (out / "index.html").read_text()
            self.assertIn("<code>ml-group/*</code>, <code>platform/ai-*</code>", html)
            self.assertIn("lists only public projects", html)
            self.assertNotIn("an internal or private one only when", html)
            self.assertNotIn("<li>The project is not archived.</li>", html)
            self.assertIn("up to 64 KB", html)
            self.assertIn("<strong>Community &lt;picks&gt;</strong> section", html)
            self.assertNotIn("Community <picks>", html)

    def test_help_dialog_omits_topic_path_when_topics_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, env_extra={"GALLERY_TOPICS_ENABLED": "false"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (out / "index.html").read_text()
            for absent in ('id="help-tab-topic"', 'id="help-panel-topic"', "Two ways in",
                           "source:topic", "#help=topic", "<!--if:", "<!--/if:"):
                self.assertNotIn(absent, html, absent)
            for present in ('id="help-tab-mr"', 'id="help-tab-search"', 'id="help-panel-mr"',
                            "Entries are added to the content repository by merge request"):
                self.assertIn(present, html, present)

    def test_help_dialog_mentions_strict_mode_only_when_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content" / "models" / "clean"
            content.mkdir(parents=True)
            (content / "index.md").write_text('---\ntitle: "Clean"\n---\nBody.\n')
            for strict, expected in (("true", True), ("false", False)):
                out = Path(tmp) / f"public-{strict}"
                proc = subprocess.run(
                    [sys.executable, str(BUILD / "generate.py"),
                     "--content-dir", str(Path(tmp) / "content"),
                     "--out", str(out), "--no-topics"],
                    capture_output=True, text=True,
                    env={**BASE_ENV, "GALLERY_STRICT": strict},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                html = (out / "index.html").read_text()
                self.assertEqual("so warnings fail the build too" in html, expected, strict)

    def test_help_dialog_names_instance_from_snapshot(self):
        # The snapshot records the instance the fetch step actually queried;
        # it beats the content repo's host.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, snapshot=FIXTURES / "topics/topics.json", env_extra={
                "GALLERY_CONTENT_REPO_URL": "https://code.example.org/g/content.git",
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (out / "index.html").read_text()
            self.assertIn("Any project on <strong>gitlab.example.com</strong>", html)
            self.assertNotIn("<strong>code.example.org</strong>", html)
            self.assertIn('href="https://code.example.org/g/content"', html)


class HelpSlotsTests(unittest.TestCase):
    def test_instance_host_prefers_snapshot_then_fetch_step_order(self):
        cfg = Config(content_repo_url="https://gitlab.example.com:8443/g/c.git")
        self.assertEqual(topic_instance_host(cfg, {"instance": "https://feed.example.net"}, environ={}),
                         "feed.example.net")
        self.assertEqual(topic_instance_host(cfg, {}, environ={"CI_SERVER_URL": "https://ci.example.org/"}),
                         "ci.example.org")
        self.assertEqual(topic_instance_host(cfg, {"instance": None}, environ={}), "gitlab.example.com:8443")
        cfg.topic_api_base = "https://api.example.com/"
        self.assertEqual(topic_instance_host(cfg, {}, environ={"CI_SERVER_URL": "https://ci.example.org"}),
                         "api.example.com")
        self.assertEqual(topic_instance_host(Config(content_repo_url="git@host:g/c.git"), {}, environ={}), "")
        # a malformed snapshot value is skipped, not fatal
        self.assertEqual(topic_instance_host(Config(), {"instance": "https://[bad"}, environ={}), "")

    def test_slots_are_escaped_and_credential_free(self):
        cfg = Config(
            content_repo_url="https://deploy:sekrit@gitlab.example.com/g/c.git",
            topic_name="AI <Gallery>", topic_allow_namespaces=["a/*", "<b>/*"],
        )
        slots = help_slots(cfg, {}, environ={})
        self.assertEqual(slots["{{CONTENT_REPO_URL}}"], "https://gitlab.example.com/g/c")
        self.assertEqual(slots["{{TOPIC_NAME}}"], "AI &lt;Gallery&gt;")
        self.assertIn("<code>a/*</code>, <code>&lt;b&gt;/*</code>", slots["{{TOPIC_ELIGIBILITY_RULES}}"])
        joined = " ".join(slots.values())
        self.assertNotIn("sekrit", joined)
        self.assertNotIn("{{", joined)

    def test_generic_wording_when_no_instance_resolves(self):
        slots = help_slots(Config(content_repo_url=""), {}, environ={})
        self.assertEqual(slots["{{TOPIC_INSTANCE_HOST}}"], "this GitLab instance")

    def test_eligibility_rules_follow_config(self):
        generic = help_slots(Config(), {}, environ={})["{{TOPIC_ELIGIBILITY_RULES}}"]
        self.assertIn("<strong>public</strong> project always qualifies", generic)
        self.assertIn("<li>The project is not archived.</li>", generic)
        self.assertNotIn("namespace", generic)
        scoped = help_slots(
            Config(topic_visibility="internal", topic_include_archived=True, topic_allow_namespaces=["ml/*"]),
            {}, environ={},
        )["{{TOPIC_ELIGIBILITY_RULES}}"]
        self.assertIn("lists only internal projects", scoped)
        self.assertNotIn("archived", scoped)
        self.assertIn("<code>ml/*</code>", scoped)


class ConditionalBlockTests(unittest.TestCase):
    TEMPLATE = "a<!--if:x-->X<!--if:y-->Y<!--/if:y--><!--/if:x-->b<!--if:!x-->N<!--/if:!x-->"

    def test_keeps_drops_negates_and_nests(self):
        self.assertEqual(apply_conditional_blocks(self.TEMPLATE, {"x": True, "y": False}), "aXb")
        self.assertEqual(apply_conditional_blocks(self.TEMPLATE, {"x": True, "y": True}), "aXYb")
        self.assertEqual(apply_conditional_blocks(self.TEMPLATE, {"x": False, "y": True}), "abN")

    def test_unknown_flag_and_unbalanced_block_raise(self):
        with self.assertRaises(KeyError):
            apply_conditional_blocks("<!--if:nope-->x<!--/if:nope-->", {"x": True})
        with self.assertRaises(ValueError):
            apply_conditional_blocks("<!--if:x-->x", {"x": True})
        with self.assertRaises(ValueError):
            apply_conditional_blocks("<!--if:x-->x<!--/if:y-->", {"x": True, "y": True})


class SortTests(unittest.TestCase):
    def _items(self):
        return [
            Item(slug="a", section="s", source="content", title="Alpha", added="2026-01-01", order=3),
            Item(slug="b", section="s", source="content", title="Beta", added="2026-01-01", order=2),
            Item(slug="c", section="s", source="content", title="Gamma", added="2026-02-01", order=1),
            Item(slug="d", section="s", source="content", title="Delta", added=None, order=1),
        ]

    def test_date_sort_newest_first_then_title_ascending(self):
        # Same-date items must tie-break A-Z, matching the comparator in app.js.
        titles = [i.title for i in sort_items(self._items(), "added")]
        self.assertEqual(titles, ["Gamma", "Alpha", "Beta", "Delta"])

    def test_order_and_title_sorts(self):
        self.assertEqual([i.title for i in sort_items(self._items(), "order")],
                         ["Delta", "Gamma", "Beta", "Alpha"])
        self.assertEqual([i.title for i in sort_items(self._items(), "title")],
                         ["Alpha", "Beta", "Delta", "Gamma"])


class PublicRepoUrlTests(unittest.TestCase):
    def test_strips_credentials_and_git_suffix(self):
        self.assertEqual(public_repo_url("https://u:p@gitlab.example.com/g/r.git"),
                         "https://gitlab.example.com/g/r")
        self.assertEqual(public_repo_url("https://token@gitlab.example.com:8443/g/r"),
                         "https://gitlab.example.com:8443/g/r")
        self.assertEqual(public_repo_url("https://gitlab.example.com/g/r.git"),
                         "https://gitlab.example.com/g/r")
        self.assertEqual(public_repo_url(""), "")


if __name__ == "__main__":
    unittest.main()
