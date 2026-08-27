"""Offline topic-snapshot merge for the AI Gallery.

Reads .topics/topics.json (written by the CI-only fetch step) and maps the
discovered projects onto gallery items. Never touches the network. Every
failure here is soft: a malformed snapshot or record degrades to "no topic
items" with a notice, and notices never fail the build (even under strict).

All topic-derived strings are untrusted third-party input; everything that
ends up in HTML goes through the same allow-list sanitiser as curated
content.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from content import Diagnostics, Item, Section, normalise_tags, slugify, collapse_ws, split_frontmatter
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


def _apply_manifest(record_name: str, manifest_text: str, instance_host: str,
                    diags: Diagnostics) -> dict:
    """Parse a project's optional .ai-gallery manifest.

    Returns overrides: subset of {title, tags, summary, url, body_html}.
    Same frontmatter + Markdown + sanitiser pipeline as index.md; failures
    are notices only.
    """
    overrides: dict = {}
    scratch = Diagnostics()
    front, body = split_frontmatter(manifest_text, f"manifest of {record_name}", scratch)
    for msg in scratch.errors + scratch.warnings:
        diags.notice(f"topic: {msg}")
    if front is None:
        front = {}
    title = front.get("title")
    if isinstance(title, str) and title.strip():
        overrides["title"] = collapse_ws(title)
    if "tags" in front:
        overrides["tags"] = normalise_tags(front.get("tags"), scratch, f"manifest of {record_name}")
    summary = front.get("summary")
    if isinstance(summary, str) and summary.strip():
        overrides["summary"] = collapse_ws(summary)[:200]
    url = front.get("url")
    if isinstance(url, str) and url.strip():
        try:
            host = (urlsplit(url.strip()).hostname or "").lower()
        except ValueError:
            host = ""
        if host and host == instance_host and url.strip().lower().startswith(("http://", "https://")):
            overrides["url"] = url.strip()
        else:
            diags.notice(
                f"topic: manifest of {record_name} sets url off the resolved instance host; ignored"
            )
    if body.strip():
        overrides["body_html"] = render_markdown(body)
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
    instance_host = (urlsplit(instance).hostname or "").lower() if instance else ""

    records = snapshot["projects"]
    if not snapshot.get("fetched") or not records:
        diags.notice("topic: snapshot has no projects; building with curated content only")
        return None, meta

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

        avatar_src = None
        avatar_file = record.get("avatar_file")
        if isinstance(avatar_file, str) and avatar_file:
            rel = avatar_file.replace("\\", "/")
            if ".." in rel.split("/") or rel.startswith("/"):
                diags.notice(f"topic: {pwn} avatar path {rel!r} is unsafe; ignored")
            else:
                candidate = snapshot_dir / rel
                if candidate.is_file():
                    avatar_src = candidate
                else:
                    diags.notice(f"topic: {pwn} avatar file {rel!r} missing from snapshot; using placeholder")

        item = Item(
            slug=slug,
            section=cfg.topic_section_slug,
            source="topic",
            title=collapse_ws(name),
            path=pwn,
            url=web_url,
            image_src=avatar_src,
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
