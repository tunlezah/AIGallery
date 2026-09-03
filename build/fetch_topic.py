"""CI-only topic fetch: GitLab topic feed -> .topics/ snapshot.

This is the ONLY component of the build permitted to touch the network.
It is strictly separate from generate.py so the generator stays offline
and testable.

Contract:
  * ALWAYS exits 0. Timeout, DNS failure, TLS error, non-2xx, rate limit,
    malformed JSON, exceeded budget -> write an empty snapshot and exit 0.
  * Retries at most 3 times with backoff on 429/5xx only.
  * Never echoes GALLERY_TOPIC_TOKEN, never writes it anywhere, and never
    sends it to any origin other than the one a request was addressed to
    (redirects included).
  * Downloaded images are kept only when their bytes are PNG/JPEG/WebP/GIF;
    SVG is never accepted from third-party projects.
  * Paths default to the repository root, like generate.py, whatever the
    current working directory; --config and --out override them.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config  # noqa: E402
from content import Diagnostics, split_frontmatter  # noqa: E402
from images import sniff_image_type  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "gallery.config.toml"
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / ".topics"
USER_AGENT = "ai-gallery-build (topic subscription fetch)"
IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MANIFEST_MAX_BYTES = 256 * 1024
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def notice(msg: str) -> None:
    print(f"notice: topic fetch: {msg}", flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


class _TokenScopedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the API token to another origin.

    urllib's default handler copies every request header onto the redirected
    request, which would hand PRIVATE-TOKEN to, for example, the object
    storage host that GitLab redirects upload downloads to. Redirects to
    non-web schemes are refused.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if _origin(new.full_url)[0] not in ("http", "https"):
            return None  # surfaces as an HTTPError to the caller
        if _origin(new.full_url) != _origin(req.full_url):
            new.remove_header("Private-token")  # urllib capitalises header names
        return new


_OPENER = urllib.request.build_opener(_TokenScopedRedirectHandler())


class Budget:
    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def exhausted(self) -> bool:
        return self.remaining() <= 0


def http_get(url: str, token: str | None, timeout: float, budget: Budget,
             *, max_bytes: int | None = None, stats: dict | None = None):
    """GET with 3 retries (backoff) on 429/5xx.

    Returns (bytes, headers) on success with header names lower-cased,
    (None, {"status": 404}) on 404, (None, None) when the body exceeds
    `max_bytes`, or None on any failure. Failures (not 404s) increment
    stats["failed"] when `stats` is given, so the snapshot can be marked
    partial.
    """
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["PRIVATE-TOKEN"] = token

    def _fail():
        if stats is not None:
            stats["failed"] += 1
        return None

    for attempt in range(3):
        if budget.exhausted():
            notice("wall-clock budget exhausted")
            return _fail()
        request = urllib.request.Request(url, headers=headers)
        try:
            effective_timeout = max(1.0, min(timeout, budget.remaining()))
            with _OPENER.open(request, timeout=effective_timeout) as response:
                if max_bytes is not None:
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        return None, None
                else:
                    body = response.read()
                return body, {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, {"status": 404}
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt < 2:
                    time.sleep(min(2 ** attempt, max(0.0, budget.remaining())))
                    continue
            notice(f"HTTP {exc.code} from {url.split('?')[0]}")
            return _fail()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            notice(f"request failed for {url.split('?')[0]}: {reason}")
            return _fail()
    return _fail()


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------

def write_snapshot(snapshot_dir: Path, topic: str, instance: str | None, fetched: bool,
                   truncated: bool, partial: bool, projects: list[dict]) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "topic": topic,
        "instance": instance,
        "fetched": fetched,
        "truncated": truncated,   # project list cut off at topic_max_projects/budget
        "partial": partial,       # some avatar/manifest/image requests failed
        "projects": sorted(projects, key=lambda p: p.get("path_with_namespace", "")),
    }
    path = snapshot_dir / "topics.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    notice(f"wrote {path} (fetched={fetched}, projects={len(projects)}, partial={partial})")


def resolve_api_base(cfg) -> tuple[str | None, str]:
    """Resolution order: topic_api_base -> CI_SERVER_URL -> content_repo_url host."""
    if cfg.topic_api_base.strip():
        return cfg.topic_api_base.strip().rstrip("/"), "topic_api_base"
    ci_server = os.environ.get("CI_SERVER_URL", "").strip()
    if ci_server:
        return ci_server.rstrip("/"), "CI_SERVER_URL"
    if cfg.content_repo_url.strip():
        parts = urllib.parse.urlsplit(cfg.content_repo_url.strip())
        if parts.scheme in ("http", "https") and parts.hostname:
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme}://{parts.hostname}{port}", "content_repo_url"
    return None, "unresolvable"


# --------------------------------------------------------------------------
# Per-project downloads
# --------------------------------------------------------------------------

def _store_image(project: dict, body: bytes, headers: dict, snapshot_dir: Path,
                 stem: str) -> str | None:
    """Keep downloaded bytes only when they are an allowed raster image.

    The bytes decide the type; a declared image/* content-type that
    disagrees with them is treated as suspicious and refused.
    """
    label = project.get("path_with_namespace")
    sniffed = sniff_image_type(body[:16])
    declared = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if sniffed is None:
        notice(f"{label}: {stem} is not a PNG/JPEG/WebP/GIF image; skipped")
        return None
    if declared.startswith("image/") and IMAGE_TYPES.get(declared) != sniffed:
        notice(f"{label}: {stem} content-type {declared!r} does not match its bytes; skipped")
        return None
    rel = f"media/{project['id']}/{stem}{sniffed}"
    dest = snapshot_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return rel


def fetch_avatar(project: dict, base_host: str, token: str | None, cfg,
                 budget: Budget, snapshot_dir: Path, stats: dict) -> str | None:
    avatar_url = project.get("avatar_url")
    if not isinstance(avatar_url, str) or not avatar_url:
        return None
    try:
        parts = urllib.parse.urlsplit(avatar_url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or (parts.hostname or "").lower() != base_host:
        notice(f"{project.get('path_with_namespace')}: avatar not on the instance host; skipped")
        return None
    max_bytes = cfg.topic_avatar_max_kb * 1024
    result = http_get(avatar_url, token, cfg.topic_timeout_seconds, budget,
                      max_bytes=max_bytes, stats=stats)
    if not result or result[0] is None:
        notice(f"{project.get('path_with_namespace')}: avatar unavailable or too large; skipped")
        return None
    body, headers = result
    return _store_image(project, body, headers, snapshot_dir, "avatar")


def fetch_repo_file(project: dict, repo_path: str, base: str, token: str | None, cfg,
                    budget: Budget, *, max_bytes: int, stats: dict):
    """Raw file from the project's default branch; same return shape as http_get."""
    ref = project.get("default_branch")
    if not isinstance(ref, str) or not ref:
        return None
    url = (
        f"{base}/api/v4/projects/{project['id']}/repository/files/"
        f"{urllib.parse.quote(repo_path, safe='')}/raw?ref={urllib.parse.quote(ref, safe='')}"
    )
    return http_get(url, token, cfg.topic_timeout_seconds, budget, max_bytes=max_bytes, stats=stats)


def fetch_manifest(project: dict, base: str, token: str | None, cfg,
                   budget: Budget, stats: dict) -> str | None:
    if not cfg.topic_manifest_path.strip():
        return None
    result = fetch_repo_file(project, cfg.topic_manifest_path, base, token, cfg, budget,
                             max_bytes=MANIFEST_MAX_BYTES, stats=stats)
    if not result:
        return None
    body, _headers = result
    if body is None:
        return None  # 404 is the normal, silent case; oversize also lands here
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        notice(f"{project.get('path_with_namespace')}: manifest is not valid UTF-8; ignored")
        return None


def manifest_image_repo_path(manifest_text: str, manifest_path: str) -> str | None:
    """Repository path of the image a manifest names, or None when absent/unsafe.

    The image must be a raster file in the manifest's own directory (or
    below it): no schemes, no absolute paths, no traversal, no SVG.
    """
    front, _body = split_frontmatter(manifest_text, "manifest", Diagnostics())
    if not isinstance(front, dict):
        return None
    image = front.get("image")
    if not isinstance(image, str) or not image.strip():
        return None
    rel = image.strip().replace("\\", "/")
    if _SCHEME_RE.match(rel) or rel.startswith("/"):
        return None
    pure = PurePosixPath(rel)
    if ".." in pure.parts or pure.suffix.lower() not in IMAGE_EXTS:
        return None
    manifest_dir = PurePosixPath(manifest_path).parent
    if str(manifest_dir) in ("", "."):
        return str(pure)
    return str(manifest_dir / pure)


def fetch_manifest_image(project: dict, manifest_text: str, base: str, token: str | None,
                         cfg, budget: Budget, snapshot_dir: Path, stats: dict) -> str | None:
    repo_path = manifest_image_repo_path(manifest_text, cfg.topic_manifest_path)
    if repo_path is None:
        return None
    result = fetch_repo_file(project, repo_path, base, token, cfg, budget,
                             max_bytes=cfg.topic_avatar_max_kb * 1024, stats=stats)
    if not result or result[0] is None:
        notice(f"{project.get('path_with_namespace')}: manifest image {repo_path!r} unavailable or too large; skipped")
        return None
    body, headers = result
    return _store_image(project, body, headers, snapshot_dir, "image")


def normalise_repo_path(url: str) -> str:
    """owner/repo path from a git URL, for content-repo self-exclusion."""
    try:
        parts = urllib.parse.urlsplit(url)
        path = parts.path
    except ValueError:
        path = url
    return path.strip("/").removesuffix(".git").lower()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Gallery topic feed fetch (the build's only network step)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="gallery.config.toml to read (default: the repository's)")
    parser.add_argument("--out", default=str(DEFAULT_SNAPSHOT_DIR),
                        help="snapshot directory to write (default: <repo>/.topics)")
    return parser.parse_args(argv)


def main(args: argparse.Namespace) -> int:
    snapshot_dir = Path(args.out)
    cfg, config_notices = load_config(args.config, require_content_repo=False)
    for msg in config_notices:
        notice(msg)

    if not cfg.topics_enabled:
        notice("topics_enabled = false; writing empty snapshot")
        write_snapshot(snapshot_dir, cfg.topic_name, None, False, False, False, [])
        return 0

    base, source = resolve_api_base(cfg)
    if base is None:
        notice("no API base resolvable (topic_api_base, CI_SERVER_URL and content_repo_url all unset); skipping topic feed")
        write_snapshot(snapshot_dir, cfg.topic_name, None, False, False, False, [])
        return 0
    notice(f"API base {base} (resolved from {source})")
    base_host = (urllib.parse.urlsplit(base).hostname or "").lower()

    token = os.environ.get("GALLERY_TOPIC_TOKEN") or None
    notice("authenticating with GALLERY_TOPIC_TOKEN" if token else "no token; public projects only")
    if not cfg.topic_allow_namespaces:
        notice("topic_allow_namespaces is empty: every project on the instance that "
               "carries the topic will be listed")

    budget = Budget(cfg.topic_budget_seconds)
    stats = {"failed": 0}
    self_path = os.environ.get("CI_PROJECT_PATH", "").strip().lower()
    content_path = normalise_repo_path(cfg.content_repo_url) if cfg.content_repo_url else ""
    allow = [pattern.lower() for pattern in cfg.topic_allow_namespaces]
    exclude = [pattern.lower() for pattern in cfg.topic_exclude]

    projects: list[dict] = []
    truncated = False
    not_allowed = 0
    page = "1"
    while page:
        if budget.exhausted():
            notice("budget exhausted during pagination; keeping what we have")
            truncated = True
            break
        query = {
            "topic": cfg.topic_name,
            "per_page": "100",
            "order_by": "id",
            "sort": "asc",
            "page": page,
        }
        if not cfg.topic_include_archived:
            query["archived"] = "false"
        if cfg.topic_visibility:
            query["visibility"] = cfg.topic_visibility
        url = f"{base}/api/v4/projects?{urllib.parse.urlencode(query)}"
        result = http_get(url, token, cfg.topic_timeout_seconds, budget)
        if not result or result[0] is None:
            notice("projects request failed; writing empty snapshot")
            write_snapshot(snapshot_dir, cfg.topic_name, base, False, False, False, [])
            return 0
        body, headers = result
        try:
            batch = json.loads(body)
        except json.JSONDecodeError:
            notice("projects response is not valid JSON; writing empty snapshot")
            write_snapshot(snapshot_dir, cfg.topic_name, base, False, False, False, [])
            return 0
        if not isinstance(batch, list):
            notice("projects response is not a list; writing empty snapshot")
            write_snapshot(snapshot_dir, cfg.topic_name, base, False, False, False, [])
            return 0

        for raw in batch:
            if not isinstance(raw, dict):
                continue
            pwn = raw.get("path_with_namespace")
            if not isinstance(pwn, str):
                continue
            low = pwn.lower()
            if self_path and low == self_path:
                continue  # the site repo itself
            if content_path and low == content_path:
                continue  # the content repo
            if allow and not any(fnmatch.fnmatchcase(low, pattern) for pattern in allow):
                not_allowed += 1
                continue
            if any(fnmatch.fnmatchcase(low, pattern) for pattern in exclude):
                notice(f"{pwn} excluded by topic_exclude")
                continue
            if len(projects) >= cfg.topic_max_projects:
                truncated = True
                break
            projects.append(raw)
        if truncated:
            notice(f"reached topic_max_projects={cfg.topic_max_projects}; truncating deterministically")
            break
        page = (headers or {}).get("x-next-page", "").strip()

    if not_allowed:
        notice(f"{not_allowed} tagged project(s) skipped: not matched by topic_allow_namespaces")

    slim: list[dict] = []
    for raw in projects:
        entry = {
            "id": raw.get("id"),
            "path_with_namespace": raw.get("path_with_namespace"),
            "name": raw.get("name"),
            "name_with_namespace": raw.get("name_with_namespace"),
            "description": raw.get("description"),
            "web_url": raw.get("web_url"),
            "topics": raw.get("topics") or raw.get("tag_list") or [],
            "created_at": raw.get("created_at"),
            "last_activity_at": raw.get("last_activity_at"),
            "avatar_file": None,
            "image_file": None,
            "manifest": None,
        }
        if isinstance(raw.get("id"), int):
            if cfg.topic_fetch_avatars:
                entry["avatar_file"] = fetch_avatar(raw, base_host, token, cfg, budget, snapshot_dir, stats)
            entry["manifest"] = fetch_manifest(raw, base, token, cfg, budget, stats)
            if entry["manifest"] and cfg.topic_fetch_avatars:
                entry["image_file"] = fetch_manifest_image(
                    raw, entry["manifest"], base, token, cfg, budget, snapshot_dir, stats,
                )
        slim.append(entry)

    partial = stats["failed"] > 0
    if partial:
        notice(f"{stats['failed']} avatar/manifest/image request(s) failed; snapshot marked partial")
    write_snapshot(snapshot_dir, cfg.topic_name, base, True, truncated, partial, slim)
    return 0


def _emergency_snapshot(snapshot_dir: Path) -> None:
    try:
        write_snapshot(snapshot_dir, "", None, False, False, False, [])
    except OSError:
        pass


if __name__ == "__main__":
    try:
        args = parse_args()
    except SystemExit as exc:
        if exc.code not in (0, None):
            notice("bad command-line arguments; writing empty snapshot to the default location and exiting 0")
            _emergency_snapshot(DEFAULT_SNAPSHOT_DIR)
        sys.exit(0)
    try:
        sys.exit(main(args))
    except SystemExit as exc:
        # Even a config-loader exit must not fail the pipeline from here.
        if exc.code not in (0, None):
            notice("configuration problem; writing empty snapshot and exiting 0")
            _emergency_snapshot(Path(args.out))
        sys.exit(0)
    except BaseException as exc:  # noqa: BLE001 - failure is always soft
        notice(f"unexpected failure ({exc.__class__.__name__}); writing empty snapshot and exiting 0")
        _emergency_snapshot(Path(args.out))
        sys.exit(0)
