"""fetch_topic.py contract tests: always exit 0, never echo the token,
soft-fail on unreachable/401/empty. Uses only a local in-process HTTP
server — no external network."""
import json
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BUILD = Path(__file__).resolve().parents[1]

SECRET = "glpat-THIS-MUST-NEVER-APPEAR"


def run_fetch(tmp, env_extra):
    env = {
        **os.environ,
        "GALLERY_CONTENT_REPO_URL": "",
        "GALLERY_TOPICS_ENABLED": "true",
        "GALLERY_TOPIC_BUDGET_SECONDS": "10",
        "GALLERY_TOPIC_TIMEOUT_SECONDS": "3",
        **env_extra,
    }
    env.pop("CI_SERVER_URL", None)
    proc = subprocess.run(
        [sys.executable, str(BUILD / "fetch_topic.py")],
        capture_output=True, text=True, cwd=tmp, env=env, timeout=60,
    )
    snapshot_path = Path(tmp) / ".topics" / "topics.json"
    snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.is_file() else None
    return proc, snapshot


class Handler(http.server.BaseHTTPRequestHandler):
    behaviour = "ok"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.behaviour == "401":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"message":"401 Unauthorized"}')
            return
        if self.path.startswith("/api/v4/projects?"):
            body = json.dumps([
                {
                    "id": 7,
                    "path_with_namespace": "grp/thing",
                    "name": "Thing",
                    "name_with_namespace": "Grp / Thing",
                    "description": "A thing.",
                    "web_url": "http://127.0.0.1/grp/thing",
                    "topics": ["ai-gallery"],
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_activity_at": "2026-02-01T00:00:00Z",
                    "default_branch": "main",
                    "avatar_url": None,
                },
                {
                    "id": 8,
                    "path_with_namespace": "me/site",
                    "name": "The site repo itself",
                    "web_url": "http://127.0.0.1/me/site",
                    "topics": ["ai-gallery"],
                },
            ] if self.behaviour == "ok" else []).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        # manifest fetch -> 404 is the normal case
        self.send_response(404)
        self.end_headers()


class FetchTopicTests(unittest.TestCase):
    def _serve(self, behaviour):
        Handler.behaviour = behaviour
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
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

    def test_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, snapshot = run_fetch(tmp, {"GALLERY_TOPICS_ENABLED": "false"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(snapshot["fetched"])


if __name__ == "__main__":
    unittest.main()
