"""Offline topic-snapshot merge for the AI Gallery.

Reads .topics/topics.json (written by the CI-only fetch step) and maps the
discovered projects onto gallery items. Never touches the network. Every
failure here is soft: a malformed snapshot or record degrades to "no topic
items" with a notice, and notices never fail the build (even under strict).

All topic-derived strings are untrusted third-party input; everything that
ends up in HTML goes through the same allow-list sanitiser as curated
content, and only raster images are accepted from the snapshot.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from urllib.parse import urlsplit

from content import (
    CONTROL_CHARS_RE,
    Diagnostics,
    Item,
    Section,
    collapse_ws,
    normalise_tags,
    parse_date,
    slugify,
    split_frontmatter,
)
from render import escape_html, render_markdown


def normalise_url_for_dedupe(url: str) -> str:
    """host + path, case-folded, trailing slash stripped."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/").lower()
    return f"{host}{path}"


def _date_part(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def load_snapshot(snapshot_path: Path, diags: Diagnostics) -> dict | None:
    """Load .topics/topics.json; any problem is a notice, never an error."""
    if not snapshot_path.is_file():
        return None
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        diags.notice(f"topic snapshot {snapshot_path} unreadable ({exc}); continuing without topic items")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        diags.notice(f"topic snapshot {snapshot_path} has unexpected shape; continuing without topic items")
        return None
    return data


def _snapshot_media(label: str, value, snapshot_dir: Path, diags: Diagnostics,
                    what: str) -> Path | None:
    """Resolve a media path recorded in the snapshot, or None with a notice.

    The snapshot is an interface, not a trusted artifact: paths must stay
    inside the snapshot directory, the file must exist, and SVG is refused
    because a third party's SVG would be served from the site's origin.
    """
    if not isinstance(value, str) or not value:
        return None
    rel = value.replace("\\", "/")
    if ".." in rel.split("/") or rel.startswith("/"):
        diags.notice(f"topic: {label} {what} path {rel!r} is unsafe; ignored")
        return None
    if rel.lower().endswith(".svg"):
        diags.notice(f"topic: {label} {what} is an SVG, which is not accepted from third-party projects; ignored")
        return None
    candidate = snapshot_dir / rel
    if not candidate.is_file():
        diags.notice(f"topic: {label} {what} file {rel!r} missing from snapshot; using the default image")
        return None
    return candidate


def _apply_manifest(record_name: str, manifest_text: str, instance_host: str,
                    diags: Diagnostics) -> dict:
    """Parse a project's optional .ai-gallery manifest.

    Returns overrides: a subset of {title, tags, summary, url, order, added,
    updated, featured, draft, body_html}. Same frontmatter + Markdown +
    sanitiser pipeline as index.md; failures are notices only. (The
    manifest's `image` is fetched by fetch_topic.py and arrives as the
    record's image_file.)
    """
    overrides: dict = {}
    scratch = Diagnostics()
    where = f"manifest of {record_name}"
    front, body = split_frontmatter(manifest_text, where, scratch)
    if front is None:
        front = {}
    title = front.get("title")
    if isinstance(title, str) and title.strip():
        overrides["title"] = collapse_ws(title)
    if "tags" in front:
        overrides["tags"] = normalise_tags(front.get("tags"), scratch, where)
    summary = front.get("summary")
    if isinstance(summary, str) and summary.strip():
        overrides["summary"] = collapse_ws(summary)[:200]
    url = front.get("url")
    if isinstance(url, str) and url.strip():
        candidate = url.strip()
        try:
            host = (urlsplit(candidate).hostname or "").lower()
        except ValueError:
            host = ""
        if (
            host and host == instance_host
            and candidate.lower().startswith(("http://", "https://"))
            and not CONTROL_CHARS_RE.search(candidate)
        ):
            overrides["url"] = candidate
        else:
            diags.notice(f"topic: {where} sets url off the resolved instance host; ignored")
    order = front.get("order")
    if isinstance(order, int) and not isinstance(order, bool):
        overrides["order"] = order
    elif order is not None:
        scratch.warn(f"{where}: order must be an integer; ignored")
    for fieldname in ("added", "updated"):
        if fieldname in front:
            parsed = parse_date(front.get(fieldname), scratch, where, fieldname)
            if parsed:
                overrides[fieldname] = parsed
    featured = front.get("featured")
    if isinstance(featured, bool):
        overrides["featured"] = featured
    elif featured is not None:
        scratch.warn(f"{where}: featured must be a boolean; ignored")
    if front.get("draft") is True:
        overrides["draft"] = True
    if body.strip():
        overrides["body_html"] = render_markdown(body)
    for msg in scratch.errors + scratch.warnings:
        diags.notice(f"topic: {msg}")
    return overrides


def merge_topics(
    cfg,
    curated_items: list[Item],
    snapshot_path: Path,
    diags: Diagnostics,
) -> tuple[Section | None, dict]:
    """Map the snapshot to items, dedupe against curated content, and build
    the synthetic section (only if at least one topic item survives).

    Returns (section_or_None, topic_meta_for_gallery_json).
    """
    meta = {
        "enabled": bool(cfg.topics_enabled),
        "name": cfg.topic_name,
        "instance": None,
        "fetched": False,
        "count": 0,
        "dropped_duplicates": 0,
        "partial": False,
        "truncated": False,
    }
    if not cfg.topics_enabled:
        return None, meta

    snapshot = load_snapshot(snapshot_path, diags)
    if snapshot is None:
        diags.notice("topic: no snapshot present; building with curated content only")
        return None, meta

    instance = snapshot.get("instance") if isinstance(snapshot.get("instance"), str) else None
    meta["instance"] = instance
    meta["fetched"] = bool(snapshot.get("fetched"))
    meta["partial"] = bool(snapshot.get("partial"))
    meta["truncated"] = bool(snapshot.get("truncated"))
    instance_host = (urlsplit(instance).hostname or "").lower() if instance else ""

    records = snapshot["projects"]
    if not snapshot.get("fetched") or not records:
        diags.notice("topic: snapshot has no projects; building with curated content only")
        return None, meta
    if meta["partial"]:
        diags.notice("topic: snapshot is marked partial (some avatar/manifest/image requests failed); "
                     "affected projects fall back to the default image or derived metadata")
    if meta["truncated"]:
        diags.notice("topic: snapshot is marked truncated (project list cut off at the configured maximum)")

    # Deterministic ordering + defensive cap.
    def _sort_key(rec):
        pwn = rec.get("path_with_namespace") if isinstance(rec, dict) else None
        return (pwn is None, pwn if isinstance(pwn, str) else "")

    records = sorted((r for r in records if isinstance(r, dict)), key=_sort_key)
    if len(records) > cfg.topic_max_projects:
        diags.notice(
            f"topic: snapshot has {len(records)} projects, capped at "
            f"topic_max_projects={cfg.topic_max_projects} (deterministic truncation)"
        )
        records = records[: cfg.topic_max_projects]
        meta["truncated"] = True

    curated_urls = {
        normalise_url_for_dedupe(item.url) for item in curated_items if item.url
    }
    curated_slugs = {item.slug for item in curated_items}
    topic_tag = normalise_tags(cfg.topic_name, Diagnostics(), "topic_name")
    topic_tag = topic_tag[0] if topic_tag else ""

    snapshot_dir = snapshot_path.parent
    items: list[Item] = []
    dropped = 0
    for record in records:
        pwn = record.get("path_with_namespace")
        name = record.get("name")
        web_url = record.get("web_url")
        if (
            not isinstance(record.get("id"), int)
            or not isinstance(pwn, str) or not pwn.strip()
            or not isinstance(name, str) or not name.strip()
            or not isinstance(web_url, str)
            or not web_url.lower().startswith(("http://", "https://"))
            or CONTROL_CHARS_RE.search(web_url)
        ):
            label = pwn if isinstance(pwn, str) and pwn else record.get("id", "<unknown>")
            diags.notice(f"topic: malformed project record {label!r} skipped")
            continue
        if record.get("archived") and not cfg.topic_include_archived:
            diags.notice(f"topic: archived project {pwn} skipped")
            continue

        slug = slugify(pwn)
        if not slug:
            diags.notice(f"topic: project {pwn} slugifies to nothing; skipped")
            continue
        if normalise_url_for_dedupe(web_url) in curated_urls:
            diags.notice(f"topic: {pwn} duplicates a curated item's url; curated item wins")
            dropped += 1
            continue
        if slug in curated_slugs:
            diags.notice(f"topic: {pwn} duplicates a curated item's slug ({slug}); curated item wins")
            dropped += 1
            continue

        scratch = Diagnostics()
        tags = normalise_tags(record.get("topics"), scratch, pwn)
        tags = [t for t in tags if t != topic_tag]

        description = record.get("description")
        summary = ""
        if isinstance(description, str):
            summary = collapse_ws(description)[:200]

        # A manifest image beats the project avatar; both must be raster
        # files that actually exist inside the snapshot directory.
        image_src = _snapshot_media(pwn, record.get("image_file"), snapshot_dir, diags, "manifest image")
        if image_src is None:
            image_src = _snapshot_media(pwn, record.get("avatar_file"), snapshot_dir, diags, "avatar")

        item = Item(
            slug=slug,
            section=cfg.topic_section_slug,
            source="topic",
            title=collapse_ws(name),
            path=pwn,
            url=web_url,
            image_src=image_src,
            tags=tags,
            summary=summary,
            body_md="",
            body_html=f"<p>{escape_html(summary)}</p>" if summary else "",
            order=1000,
            added=_date_part(record.get("created_at")),
            updated=_date_part(record.get("last_activity_at")),
        )

        manifest = record.get("manifest")
        if isinstance(manifest, str) and manifest.strip():
            overrides = _apply_manifest(pwn, manifest, instance_host, diags)
            if overrides.pop("draft", False):
                diags.notice(f"topic: {pwn} manifest sets draft: true; skipped")
                continue
            for key_, value in overrides.items():
                setattr(item, key_, value)

        # A record could still collide with an earlier topic item's slug.
        if any(existing.slug == item.slug for existing in items):
            diags.notice(f"topic: duplicate topic slug {item.slug!r} from {pwn}; skipped")
            continue
        items.append(item)

    # Title collision within the synthetic section -> use name_with_namespace.
    titles: dict[str, list[Item]] = {}
    by_slug = {slugify(r.get("path_with_namespace", "")): r for r in records if isinstance(r.get("path_with_namespace"), str)}
    for item in items:
        titles.setdefault(item.title.lower(), []).append(item)
    for _title, group in sorted(titles.items()):
        if len(group) > 1:
            for item in group:
                record = by_slug.get(item.slug, {})
                nwn = record.get("name_with_namespace")
                if isinstance(nwn, str) and nwn.strip():
                    item.title = collapse_ws(nwn)

    meta["dropped_duplicates"] = dropped
    meta["count"] = len(items)
    if not items:
        diags.notice("topic: no topic items survived mapping/dedupe; no Subscribed section emitted")
        return None, meta

    items.sort(key=lambda item: item.path)  # path_with_namespace, for stability
    section = Section(
        slug=cfg.topic_section_slug,
        title=cfg.topic_section_title,
        order=cfg.topic_section_order,
        description=cfg.topic_section_description,
        items=items,
    )
    return section, meta
