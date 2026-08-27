"""End-to-end generator tests: build against fixtures, determinism,
strict-mode behaviour and topic soft-failure at build level (milestone 5+)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BUILD = Path(__file__).resolve().parents[1]
ROOT = BUILD.parent
FIXTURES = Path(__file__).parent / "fixtures"

BASE_ENV = {
    **os.environ,
    "GALLERY_CONTENT_REPO_URL": "https://gitlab.example.com/group/content.git",
}


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
            # placeholder shipped, broken image warned
            self.assertTrue((out / "assets/placeholder.svg").is_file())
            self.assertIn("missing.png", proc.stderr)
            # draft excluded
            self.assertNotIn("draft-item", (out / "gallery.json").read_text())

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
            # baked HTML carries the badge + section
            html = (out / "index.html").read_text()
            self.assertIn("from topic", html)
            self.assertIn('id="section-subscribed"', html)

    def test_topic_empty_snapshot_no_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, snapshot=FIXTURES / "topics-empty.json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = (out / "index.html").read_text()
            self.assertNotIn('id="section-subscribed"', html)
            self.assertNotIn("Subscribed", html)

    def test_topic_absent_snapshot_no_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, snapshot=Path(tmp) / "nope.json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("Subscribed", (out / "index.html").read_text())

    def test_standalone_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "public"
            proc = run_build(out, snapshot=FIXTURES / "topics/topics.json", standalone=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            dist = Path(tmp) / "dist-standalone"
            html = (dist / "index.html").read_text()
            self.assertTrue((dist / "media").is_dir())
            # no CSP meta, no external script/style refs in standalone
            self.assertNotIn("Content-Security-Policy", html)
            self.assertNotIn('src="./assets/', html)
            self.assertNotIn('href="./assets/', html)
            # hosted keeps CSP + external classic scripts
            hosted = (out / "index.html").read_text()
            self.assertIn("Content-Security-Policy", hosted)
            self.assertIn('script src="./assets/app.js" defer', hosted.replace("<", ""))

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


if __name__ == "__main__":
    unittest.main()
