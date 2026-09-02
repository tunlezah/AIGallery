"""fetch_topic.py contract tests: always exit 0, never echo the token,
never forward it across origins, soft-fail on unreachable/401/empty,
validate downloaded image bytes, and mark partial snapshots. Uses only
local in-process HTTP servers — no external network."""
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BUILD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD))

import fetch_topic  # noqa: E402

SECRET = "glpat-THIS-MUST-NEVER-APPEAR"


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


PNG = _png_bytes()
MANIFEST = (
    '---\ntitle: "Thing Pro"\nimage: "cover.png"\nfeatured: true\n---\n'
    "Manifest **body**.\n"
)


def run_fetch(tmp, env_extra, *extra_args):
    env = {
        **os.environ,
        "GALLERY_CONTENT_REPO_URL": "",
        "GALLERY_TOPICS_ENABLED": "true",
        "GALLERY_TOPIC_BUDGET_SECONDS": "10",
        "GALLERY_TOPIC_TIMEOUT_SECONDS": "3",
        **env_extra,
    }
    env.pop("CI_SERVER_URL", None)
    out_dir = Path(tmp) / ".topics"
    proc = subprocess.run(
        [sys.executable, str(BUILD / "fetch_topic.py"),
         "--config", str(Path(tmp) / "no-such-config.toml"),
         "--out", str(out_dir), *extra_args],
        capture_output=True, text=True, cwd=tmp, env=env, timeout=60,
    )
    snapshot_path = out_dir / "topics.json"
    snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.is_file() else None
    return proc, snapshot


class Handler(http.server.BaseHTTPRequestHandler):
    behaviour = "ok"

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype=None):
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def _base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _projects(self):
        b = self.behaviour
        if b == "empty":
            return []
        thing = {
            "id": 7,
            "path_with_namespace": "grp/thing",
            "name": "Thing",
            "name_with_namespace": "Grp / Thing",
            "description": "A thing.",
            "web_url": f"{self._base()}/grp/thing",
            "topics": ["ai-gallery"],
            "created_at": "2026-01-01T00:00:00Z",
            "last_activity_at": "2026-02-01T00:00:00Z",
            "default_branch": "main",
            "avatar_url": None,
        }
        if b == "manifest":
            thing["avatar_url"] = f"{self._base()}/uploads/avatar.png"
        elif b == "badavatar":
            thing["avatar_url"] = f"{self._base()}/uploads/evil.png"
        elif b == "partial":
            thing["avatar_url"] = f"{self._base()}/uploads/forbidden.png"
        site = {
            "id": 8,
            "path_with_namespace": "me/site",
            "name": "The site repo itself",
            "web_url": f"{self._base()}/me/site",
            "topics": ["ai-gallery"],
        }
        return [thing, site]

    def do_GET(self):
        if self.behaviour == "401":
            return self._send(401, b'{"message":"401 Unauthorized"}')
        if self.path.startswith("/api/v4/projects?"):
            return self._send(200, json.dumps(self._projects()).encode(), "application/json")
        if self.path == "/uploads/avatar.png":
            return self._send(200, PNG, "image/png")
        if self.path == "/uploads/evil.png":
            return self._send(200, b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>", "image/png")
        if self.path == "/uploads/forbidden.png":
            return self._send(403, b"forbidden")
        if self.behaviour == "manifest":
            if self.path == "/api/v4/projects/7/repository/files/.ai-gallery%2Findex.md/raw?ref=main":
                return self._send(200, MANIFEST.encode(), "text/plain; charset=utf-8")
            if self.path == "/api/v4/projects/7/repository/files/.ai-gallery%2Fcover.png/raw?ref=main":
                return self._send(200, PNG, "text/plain")  # GitLab raw endpoint: bytes decide
        # manifest fetch -> 404 is the normal case
        self._send(404)


class FetchTopicTests(unittest.TestCase):
    def _serve(self, behaviour):
        Handler.behaviour = behaviour
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_unreachable_base_soft_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, snapshot = run_fetch(tmp, {
                "GALLERY_TOPIC_API_BASE": "http://127.0.0.1:1",
                "GALLERY_TOPIC_TOKEN": SECRET,
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(snapshot["fetched"])
            self.assertEqual(snapshot["projects"], [])
            self.assertNotIn(SECRET, proc.stdout + proc.stderr)
            self.assertNotIn(SECRET, json.dumps(snapshot))

    def test_401_soft_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._serve("401")
            proc, snapshot = run_fetch(tmp, {
                "GALLERY_TOPIC_API_BASE": base,
                "GALLERY_TOPIC_TOKEN": SECRET,
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(snapshot["fetched"])
            self.assertNotIn(SECRET, proc.stdout + proc.stderr)

    def test_zero_matching_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._serve("empty")
            proc, snapshot = run_fetch(tmp, {"GALLERY_TOPIC_API_BASE": base})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(snapshot["fetched"])
            self.assertEqual(snapshot["projects"], [])
            self.assertFalse(snapshot["partial"])

    def test_no_base_resolvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, snapshot = run_fetch(tmp, {})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(snapshot["fetched"])
            self.assertIn("no API base resolvable", proc.stdout)

    def test_success_and_self_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._serve("ok")
            proc, snapshot = run_fetch(tmp, {
                "GALLERY_TOPIC_API_BASE": base,
                "CI_PROJECT_PATH": "me/site",
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(snapshot["fetched"])
            paths = [p["path_with_namespace"] for p in snapshot["projects"]]
            self.assertEqual(paths, ["grp/thing"])  # site repo excluded
            self.assertIsNone(snapshot["projects"][0]["manifest"])  # 404 silent
            self.assertIsNone(snapshot["projects"][0]["image_file"])
            self.assertFalse(snapshot["partial"])
            self.assertIn("topic_allow_namespaces is empty", proc.stdout)

    def test_allow_list_filters_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._serve("ok")
            proc, snapshot = run_fetch(tmp, {
                "GALLERY_TOPIC_API_BASE": base,
                "GALLERY_TOPIC_ALLOW_NAMESPACES": "ml-group/*, platform/ai-*",
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(snapshot["fetched"])
            self.assertEqual(snapshot["projects"], [])
            self.assertIn("not matched by topic_allow_namespaces", proc.stdout)
            proc, snapshot = run_fetch(tmp, {
                "GALLERY_TOPIC_API_BASE": base,
                "GALLERY_TOPIC_ALLOW_NAMESPACES": "GRP/*",   # case-insensitive
                "CI_PROJECT_PATH": "me/site",
            })
            self.assertEqual([p["path_with_namespace"] for p in snapshot["projects"]], ["grp/thing"])

    def test_manifest_image_and_avatar_localised(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._serve("manifest")
            proc, snapshot = run_fetch(tmp, {"GALLERY_TOPIC_API_BASE": base, "CI_PROJECT_PATH": "me/site"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            project = snapshot["projects"][0]
            self.assertEqual(project["avatar_file"], "media/7/avatar.png")
            self.assertEqual(project["image_file"], "media/7/image.png")
            self.assertIn("Thing Pro", project["manifest"])
            for rel in (project["avatar_file"], project["image_file"]):
                self.assertEqual((Path(tmp) / ".topics" / rel).read_bytes(), PNG)
            self.assertFalse(snapshot["partial"])

    def test_image_bytes_must_match_declared_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._serve("badavatar")
            proc, snapshot = run_fetch(tmp, {"GALLERY_TOPIC_API_BASE": base, "CI_PROJECT_PATH": "me/site"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIsNone(snapshot["projects"][0]["avatar_file"])
            self.assertIn("not a PNG/JPEG/WebP/GIF image", proc.stdout)
            self.assertEqual(list((Path(tmp) / ".topics").rglob("*.png")), [])

    def test_failed_download_marks_snapshot_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._serve("partial")
            proc, snapshot = run_fetch(tmp, {"GALLERY_TOPIC_API_BASE": base, "CI_PROJECT_PATH": "me/site"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(snapshot["fetched"])
            self.assertTrue(snapshot["partial"])
            self.assertIsNone(snapshot["projects"][0]["avatar_file"])
            self.assertIn("marked partial", proc.stdout)

    def test_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, snapshot = run_fetch(tmp, {"GALLERY_TOPICS_ENABLED": "false"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(snapshot["fetched"])

    def test_bad_arguments_still_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(BUILD / "fetch_topic.py"), "--definitely-not-a-flag"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "GALLERY_TOPICS_ENABLED": "false"},
        )
        self.assertEqual(proc.returncode, 0)


class _RedirectTarget(http.server.BaseHTTPRequestHandler):
    seen = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        _RedirectTarget.seen = {k.lower(): v for k, v in self.headers.items()}
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(PNG)


class _Redirector(http.server.BaseHTTPRequestHandler):
    location = ""
    seen = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/same-origin-target":
            _Redirector.seen = {k.lower(): v for k, v in self.headers.items()}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(302)
        self.send_header("Location", _Redirector.location or "/same-origin-target")
        self.end_headers()


class RedirectTokenTests(unittest.TestCase):
    def _serve(self, handler):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_token_not_forwarded_across_origins(self):
        target_port = self._serve(_RedirectTarget)
        _Redirector.location = f"http://127.0.0.1:{target_port}/elsewhere/avatar.png"
        redirector_port = self._serve(_Redirector)
        _RedirectTarget.seen = {}
        result = fetch_topic.http_get(
            f"http://127.0.0.1:{redirector_port}/uploads/avatar.png", SECRET, 5,
            fetch_topic.Budget(10), max_bytes=1 << 20,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], PNG)  # the redirect itself is still followed
        self.assertIn("user-agent", _RedirectTarget.seen)
        self.assertNotIn("private-token", _RedirectTarget.seen)

    def test_token_kept_on_same_origin_redirect(self):
        _Redirector.location = ""
        port = self._serve(_Redirector)
        _Redirector.seen = {}
        result = fetch_topic.http_get(
            f"http://127.0.0.1:{port}/redirect-me", SECRET, 5, fetch_topic.Budget(10),
        )
        self.assertEqual(result[0], b"ok")
        self.assertEqual(_Redirector.seen.get("private-token"), SECRET)


class ManifestImagePathTests(unittest.TestCase):
    def test_paths(self):
        f = fetch_topic.manifest_image_repo_path
        self.assertEqual(f('---\nimage: "cover.png"\n---\n', ".ai-gallery/index.md"), ".ai-gallery/cover.png")
        self.assertEqual(f('---\nimage: "./art/cover.JPG"\n---\n', ".ai-gallery/index.md"), ".ai-gallery/art/cover.JPG")
        self.assertEqual(f('---\nimage: "cover.webp"\n---\n', "index.md"), "cover.webp")
        for bad in ('image: "../logo.png"', 'image: "/etc/passwd.png"', 'image: "https://x/y.png"',
                    'image: "logo.svg"', 'image: "notes.txt"', 'image: 42'):
            self.assertIsNone(f(f"---\n{bad}\n---\n", ".ai-gallery/index.md"), bad)
        self.assertIsNone(f("no frontmatter", ".ai-gallery/index.md"))


if __name__ == "__main__":
    unittest.main()
