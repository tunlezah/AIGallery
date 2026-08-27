"""CI-only topic fetch: GitLab topic feed -> .topics/ snapshot.

This is the ONLY component of the build permitted to touch the network.
It is strictly separate from generate.py so the generator stays offline
and testable.

Contract:
  * ALWAYS exits 0. Timeout, DNS failure, TLS error, non-2xx, rate limit,
    malformed JSON, exceeded budget -> write an empty snapshot and exit 0.
  * Retries at most 3 times with backoff on 429/5xx only.
  * Never echoes GALLERY_TOPIC_TOKEN, never writes it anywhere.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config  # noqa: E402

SNAPSHOT_DIR = Path(".topics")
USER_AGENT = "ai-gallery-build (topic subscription fetch)"
AVATAR_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}
MANIFEST_MAX_BYTES = 256 * 1024


def notice(msg: str) -> None:
    print(f"notice: topic fetch: {msg}", flush=True)


def write_snapshot(topic: str, instance: str | None, fetched: bool,
                   truncated: bool, projects: list[dict]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "topic": topic,
        "instance": instance,
        "fetched": fetched,
        "truncated": truncated,
        "projects": sorted(projects, key=lambda p: p.get("path_with_namespace", "")),
    }
    path = SNAPSHOT_DIR / "topics.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    notice(f"wrote {path} (fetched={fetched}, projects={len(projects)})")


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


class Budget:
    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def exhausted(self) -> bool:
        return self.remaining() <= 0


def http_get(url: str, token: str | None, timeout: float, budget: Budget,
             *, max_bytes: int | None = None):
    """GET with 3 retries (backoff) on 429/5xx. Returns (bytes, headers) or None."""
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["PRIVATE-TOKEN"] = token
    for attempt in range(3):
        if budget.exhausted():
            notice("wall-clock budget exhausted")
            return None
        request = urllib.request.Request(url, headers=headers)
        try:
            effective_timeout = max(1.0, min(timeout, budget.remaining()))
            with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                if max_bytes is not None:
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        return None, None
                else:
                    body = response.read()
                return body, dict(response.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, {"status": 404}
            if exc.code in (429,) or 500 <= exc.code < 600:
                if attempt < 2:
                    time.sleep(min(2 ** attempt, max(0.0, budget.remaining())))
                    continue
            notice(f"HTTP {exc.code} from {url.split('?')[0]}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            notice(f"request failed for {url.split('?')[0]}: {reason}")
            return None
    return None


def fetch_avatar(project: dict, base_host: str, token: str | None, cfg,
                 budget: Budget) -> str | None:
    avatar_url = project.get("avatar_url")
    if not isinstance(avatar_url, str) or not avatar_url:
        return None
    try:
        parts = urllib.parse.urlsplit(avatar_url)
    except ValueError:
        return None
    if (parts.hostname or "").lower() != base_host:
        notice(f"{project.get('path_with_namespace')}: avatar not on the instance host; skipped")
        return None
    max_bytes = cfg.topic_avatar_max_kb * 1024
    result = http_get(avatar_url, token, cfg.topic_timeout_seconds, budget, max_bytes=max_bytes)
    if not result or result[0] is None:
        notice(f"{project.get('path_with_namespace')}: avatar unavailable or too large; skipped")
        return None
    body, headers = result
    content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = AVATAR_TYPES.get(content_type)
    if ext is None:
        notice(f"{project.get('path_with_namespace')}: avatar content-type {content_type!r} not allowed; skipped")
        return None
    rel = f"media/{project['id']}/avatar{ext}"
    dest = SNAPSHOT_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return rel


def fetch_manifest(project: dict, base: str, token: str | None, cfg,
                   budget: Budget) -> str | None:
    if not cfg.topic_manifest_path.strip():
        return None
    ref = project.get("default_branch")
    if not isinstance(ref, str) or not ref:
        return None
    encoded = urllib.parse.quote(cfg.topic_manifest_path, safe="")
    url = (
        f"{base}/api/v4/projects/{project['id']}/repository/files/{encoded}/raw"
        f"?ref={urllib.parse.quote(ref, safe='')}"
    )
    result = http_get(url, token, cfg.topic_timeout_seconds, budget,
                      max_bytes=MANIFEST_MAX_BYTES)
    if not result:
        return None
    body, headers = result
    if body is None:
        return None  # 404 is the normal, silent case; oversize also lands here
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        notice(f"{project.get('path_with_namespace')}: manifest is not valid UTF-8; ignored")
        return None


def normalise_repo_path(url: str) -> str:
    """owner/repo path from a git URL, for content-repo self-exclusion."""
    try:
        parts = urllib.parse.urlsplit(url)
        path = parts.path
    except ValueError:
        path = url
    return path.strip("/").removesuffix(".git").lower()


def main() -> int:
    cfg, config_notices = load_config(require_content_repo=False)
    for msg in config_notices:
        notice(msg)

    if not cfg.topics_enabled:
        notice("topics_enabled = false; writing empty snapshot")
        write_snapshot(cfg.topic_name, None, False, False, [])
        return 0

    base, source = resolve_api_base(cfg)
    if base is None:
        notice("no API base resolvable (topic_api_base, CI_SERVER_URL and content_repo_url all unset); skipping topic feed")
        write_snapshot(cfg.topic_name, None, False, False, [])
        return 0
    notice(f"API base {base} (resolved from {source})")
    base_host = (urllib.parse.urlsplit(base).hostname or "").lower()

    token = os.environ.get("GALLERY_TOPIC_TOKEN") or None
    notice("authenticating with GALLERY_TOPIC_TOKEN" if token else "no token; public projects only")

    budget = Budget(cfg.topic_budget_seconds)
    self_path = os.environ.get("CI_PROJECT_PATH", "").strip().lower()
    content_path = normalise_repo_path(cfg.content_repo_url) if cfg.content_repo_url else ""

    projects: list[dict] = []
    truncated = False
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
            write_snapshot(cfg.topic_name, base, False, False, [])
            return 0
        body, headers = result
        try:
            batch = json.loads(body)
        except json.JSONDecodeError:
            notice("projects response is not valid JSON; writing empty snapshot")
            write_snapshot(cfg.topic_name, base, False, False, [])
            return 0
        if not isinstance(batch, list):
            notice("projects response is not a list; writing empty snapshot")
            write_snapshot(cfg.topic_name, base, False, False, [])
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
            if any(fnmatch.fnmatchcase(pwn, pattern) for pattern in cfg.topic_exclude):
                notice(f"{pwn} excluded by topic_exclude")
                continue
            if len(projects) >= cfg.topic_max_projects:
                truncated = True
                break
            projects.append(raw)
        if truncated:
            notice(f"reached topic_max_projects={cfg.topic_max_projects}; truncating deterministically")
            break
        next_page = ""
        for key, value in (headers or {}).items():
            if key.lower() == "x-next-page":
                next_page = (value or "").strip()
        page = next_page

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
            "manifest": None,
        }
        if cfg.topic_fetch_avatars and isinstance(raw.get("id"), int):
            entry["avatar_file"] = fetch_avatar(raw, base_host, token, cfg, budget)
        if isinstance(raw.get("id"), int):
            entry["manifest"] = fetch_manifest(raw, base, token, cfg, budget)
        slim.append(entry)

    write_snapshot(cfg.topic_name, base, True, truncated, slim)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as exc:
        # Even a config-loader exit must not fail the pipeline from here.
        if exc.code not in (0, None):
            notice("configuration problem; writing empty snapshot and exiting 0")
            try:
                write_snapshot("", None, False, False, [])
            except OSError:
                pass
        sys.exit(0)
    except BaseException as exc:  # noqa: BLE001 - failure is always soft
        notice(f"unexpected failure ({exc.__class__.__name__}); writing empty snapshot and exiting 0")
        try:
            write_snapshot("", None, False, False, [])
        except OSError:
            pass
        sys.exit(0)
