"""AI Gallery static site generator.

Reads the cloned content repo (.content/) and the topic snapshot (.topics/),
both written by CI beforehand, and emits:

  * public/          -- hosted target for GitLab Pages
  * dist/standalone/ -- file://-safe standalone target (--standalone)

The generator performs no network I/O. Builds are deterministic: everything
is sorted, and no wall-clock timestamps enter the output.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config  # noqa: E402
from content import Diagnostics, Item, Section, discover  # noqa: E402
from images import HAVE_PILLOW, process_image  # noqa: E402
from render import (  # noqa: E402
    derive_summary,
    escape_html,
    fold_search_text,
    html_to_text,
    render_markdown,
)
from topics import merge_topics  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
}

CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; base-uri 'none'; form-action 'none'\">"
)


# --------------------------------------------------------------------------
# Index assembly
# --------------------------------------------------------------------------

def content_commit(content_dir: Path) -> str | None:
    if not (content_dir / ".git").exists():
        return None  # not a clone root (e.g. fixtures) — no commit to report
    try:
        out = subprocess.run(
            ["git", "-C", str(content_dir), "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def sort_items(items: list[Item], mode: str) -> list[Item]:
    fold = fold_search_text
    if mode == "title":
        return sorted(items, key=lambda i: (fold(i.title), i.section, i.slug))
    if mode in ("added", "updated"):
        def key(i: Item):
            date = getattr(i, mode)
            return (date is None, "" if date is None else "", date or "", fold(i.title))
        # newest first, missing dates last
        with_date = sorted(
            (i for i in items if getattr(i, mode)),
            key=lambda i: (getattr(i, mode), fold(i.title)),
            reverse=True,
        )
        without = sorted((i for i in items if not getattr(i, mode)),
                         key=lambda i: (fold(i.title), i.slug))
        return with_date + without
    return sorted(items, key=lambda i: (i.order, fold(i.title), i.slug))


def build_index(cfg, sections: list[Section], topic_meta: dict,
                ref: str | None, commit: str | None) -> dict:
    all_items: list[Item] = [item for section in sections for item in section.items]
    tag_counts: dict[str, int] = {}
    for item in all_items:
        for tag in item.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    items_json = []
    for item in sort_items(all_items, cfg.default_sort):
        items_json.append({
            "id": f"{item.section}/{item.slug}",
            "section": item.section,
            "source": item.source,
            "title": item.title,
            "url": item.url,
            "image": item.image,
            "thumb": item.thumb,
            "image_w": item.image_w,
            "image_h": item.image_h,
            "tags": item.tags,
            "summary": item.summary,
            "body_html": item.body_html,
            "search": item.search,
            "order": item.order,
            "added": item.added,
            "updated": item.updated,
            "featured": item.featured,
        })

    sections_json = []
    for section in sorted(sections, key=lambda s: (s.order, fold_search_text(s.title), s.slug)):
        entry = {
            "slug": section.slug,
            "title": section.title,
            "description": section.description,
            "order": section.order,
            "count": len(section.items),
        }
        if section.icon:
            entry["icon"] = section.icon
        sections_json.append(entry)

    return {
        "schema": 1,
        "site": {"title": cfg.site_title, "description": cfg.site_description},
        "generated_from": {
            "ref": ref,
            "commit": commit,
            "topic": topic_meta,
        },
        "sections": sections_json,
        "tags": [{"name": name, "count": count} for name, count in sorted(tag_counts.items())],
        "items": items_json,
    }


# --------------------------------------------------------------------------
# HTML rendering (baked tiles: the page works fully with JS disabled)
# --------------------------------------------------------------------------

def render_tile(item: Item, placeholder_url: str, topic_name: str) -> str:
    esc = escape_html
    item_id = f"{item.section}/{item.slug}"
    body_id = "tb-" + item_id.replace("/", "-")
    img_src = item.thumb or item.image or placeholder_url
    dims = (
        f' width="{item.image_w}" height="{item.image_h}"'
        if item.image_w and item.image_h else ' width="800" height="600"'
    )
    parts = [
        f'<article class="tile" data-id="{esc(item_id)}">',
        '<div class="tile-media">',
        f'<img src="{esc(img_src)}"{dims} alt="" loading="lazy" decoding="async">',
        "</div>",
        '<div class="tile-inner">',
    ]
    title = esc(item.title)
    if item.url:
        parts.append(
            f'<h3 class="tile-title" title="{title}">'
            f'<a href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">{title}</a></h3>'
        )
    else:
        parts.append(f'<h3 class="tile-title" title="{title}">{title}</h3>')

    if item.tags:
        shown = item.tags[:3]
        chips = "".join(
            f'<button type="button" class="chip tile-chip" data-tag="{esc(tag)}" '
            f'aria-pressed="false">{esc(tag)}</button>'
            for tag in shown
        )
        if len(item.tags) > 3:
            extra = len(item.tags) - 3
            chips += (
                f'<span class="chip chip-more" title="{esc(", ".join(item.tags[3:]))}"'
                f'>+{extra}<span class="visually-hidden"> more tags: '
                f'{esc(", ".join(item.tags[3:]))}</span></span>'
            )
        parts.append(f'<div class="tile-tags">{chips}</div>')

    if item.body_html:
        parts.append(f'<div class="tile-body" id="{esc(body_id)}">{item.body_html}</div>')
    else:
        parts.append(f'<div class="tile-body tile-body-empty" id="{esc(body_id)}"></div>')

    footer_bits = []
    if item.url:
        host = urlsplit(item.url).hostname or item.url
        footer_bits.append(
            f'<a class="tile-host" href="{esc(item.url)}" target="_blank" '
            f'rel="noopener noreferrer">{esc(host)}</a>'
        )
    if item.updated:
        footer_bits.append(
            f'<time class="tile-updated" datetime="{esc(item.updated)}">{esc(item.updated)}</time>'
        )
    if item.source == "topic":
        footer_bits.append(
            f'<span class="tile-badge">from topic<span class="visually-hidden">'
            f' — discovered via the GitLab topic {esc(topic_name)}</span></span>'
        )
    parts.append(f'<div class="tile-footer">{"".join(footer_bits)}</div>')
    parts.append("</div></article>")
    return "".join(parts)


def render_content(cfg, sections: list[Section], placeholder_url: str) -> str:
    out: list[str] = []
    ordered = sorted(sections, key=lambda s: (s.order, fold_search_text(s.title), s.slug))
    for pos, section in enumerate(ordered):
        if pos > 0:
            out.append('<hr class="rainbow-divider" aria-hidden="true">')
        out.append(
            f'<section class="gallery-section" id="section-{escape_html(section.slug)}" '
            f'data-section="{escape_html(section.slug)}" '
            f'aria-labelledby="section-h-{escape_html(section.slug)}">'
        )
        out.append('<div class="section-head">')
        out.append(
            f'<h2 id="section-h-{escape_html(section.slug)}">{escape_html(section.title)}'
            f' <span class="section-count">({len(section.items)})</span></h2>'
        )
        if section.description:
            out.append(f'<p class="section-desc">{escape_html(section.description)}</p>')
        out.append("</div>")
        out.append('<div class="grid">')
        for item in sort_items(section.items, cfg.default_sort):
            out.append(render_tile(item, placeholder_url, cfg.topic_name))
        out.append("</div></section>")
    return "\n".join(out)


def render_rail(sections: list[Section], tag_counts: dict[str, int]) -> str:
    esc = escape_html
    out = ['<div class="rail-block"><h2 class="rail-heading" id="rail-sections-h">Sections</h2>',
           '<ul class="rail-list" aria-labelledby="rail-sections-h">']
    ordered = sorted(sections, key=lambda s: (s.order, fold_search_text(s.title), s.slug))
    for section in ordered:
        out.append(
            f'<li><button type="button" class="rail-section" data-section="{esc(section.slug)}" '
            f'aria-pressed="false">{esc(section.title)} '
            f'<span class="count">{len(section.items)}</span></button></li>'
        )
    out.append("</ul></div>")
    if tag_counts:
        out.append('<div class="rail-block"><h2 class="rail-heading" id="rail-tags-h">Tags</h2>')
        out.append('<div class="chip-row" aria-labelledby="rail-tags-h">')
        for tag, count in sorted(tag_counts.items()):
            out.append(
                f'<button type="button" class="chip" data-tag="{esc(tag)}" '
                f'aria-pressed="false">{esc(tag)} <span class="count">{count}</span></button>'
            )
        out.append("</div></div>")
    return "\n".join(out)


FALLBACK_DEFAULT_IMAGE = "assets/placeholder.svg"


def resolve_default_image(cfg, diags: Diagnostics) -> str:
    """Validate cfg.default_image as a path inside src/; fall back if broken.

    The returned value is a src/-relative POSIX path — usable directly as the
    hosted URL because copy_src() mirrors src/ into the output directory.
    """
    rel = (cfg.default_image or "").strip().replace("\\", "/")
    if not rel:
        return FALLBACK_DEFAULT_IMAGE
    candidate = (SRC_DIR / rel).resolve()
    inside_src = str(candidate).startswith(str(SRC_DIR.resolve()) + os.sep)
    if not inside_src or not candidate.is_file():
        diags.warn(
            f"config: default_image {cfg.default_image!r} is not a file under src/; "
            f"using {FALLBACK_DEFAULT_IMAGE}"
        )
        return FALLBACK_DEFAULT_IMAGE
    return rel


def data_uri(path: Path) -> str:
    mime = MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def inline_json(payload: dict) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    # `<` never appears unescaped, so `</script` and `<!--` cannot occur.
    return text.replace("<", "\\u003c")


def render_page(template: str, *, cfg, index: dict, sections, tag_counts,
                standalone: bool, single_file: bool, out_dir: Path,
                placeholder_url: str) -> str:
    if standalone:
        favicon = f'<link rel="icon" href="{data_uri(SRC_DIR / "favicon.svg")}" type="image/svg+xml">'
        css = (SRC_DIR / "assets" / "app.css").read_text(encoding="utf-8")
        styles = f"<style>\n{css}\n</style>"
        theme_init = "<script>\n" + (SRC_DIR / "assets" / "theme-init.js").read_text(encoding="utf-8") + "\n</script>"
        scripts = "<script>\n" + (SRC_DIR / "assets" / "app.js").read_text(encoding="utf-8") + "\n</script>"
        csp = ""
    else:
        favicon = '<link rel="icon" href="./favicon.svg" type="image/svg+xml">'
        styles = '<link rel="stylesheet" href="./assets/app.css">'
        theme_init = '<script src="./assets/theme-init.js"></script>'
        scripts = '<script src="./assets/app.js" defer></script>'
        csp = CSP_META

    payload = dict(index)
    payload["runtime"] = {
        "placeholder": placeholder_url,
        "cards_per_page": cfg.cards_per_page,
        "default_sort": cfg.default_sort,
        "topic_name": cfg.topic_name,
    }
    data_block = (
        '<script type="application/json" id="gallery-data">'
        + inline_json(payload)
        + "</script>"
    )

    html = template
    replacements = {
        "{{CSP}}": csp,
        "{{SITE_TITLE}}": escape_html(cfg.site_title),
        "{{SITE_DESCRIPTION}}": escape_html(cfg.site_description),
        "{{FAVICON}}": favicon,
        "{{STYLES}}": styles,
        "{{THEME_INIT}}": theme_init,
        "{{RAIL}}": render_rail(sections, tag_counts),
        "{{CONTENT}}": render_content(cfg, sections, placeholder_url),
        "{{DATA}}": data_block,
        "{{SCRIPTS}}": scripts,
        "{{ITEM_TOTAL}}": str(len(index["items"])),
        # Help dialog: baked from config so the text stays correct when the
        # content repo, topic name or manifest path are reconfigured.
        "{{TOPIC_NAME}}": escape_html(cfg.topic_name),
        "{{TOPIC_MANIFEST_PATH}}": escape_html(cfg.topic_manifest_path),
        "{{CONTENT_REPO_URL}}": escape_html(cfg.content_repo_url.removesuffix(".git")),
    }
    for token, value in replacements.items():
        html = html.replace(token, value)

    if standalone and single_file:
        html = _inline_media(html, out_dir)
    return html


def _inline_media(html: str, out_dir: Path, threshold_kb: int = 200) -> str:
    """--single-file: data-URI-inline media images under the size threshold."""
    import re as _re

    def _sub(match: _re.Match) -> str:
        rel = match.group(1)
        path = out_dir / rel
        if path.is_file() and path.stat().st_size <= threshold_kb * 1024:
            return f'src="{data_uri(path)}"'
        return match.group(0)

    return _re.sub(r'src="(media/[^"]+)"', _sub, html)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def process_all_images(cfg, sections: list[Section], out_dir: Path, diags: Diagnostics,
                       counters: dict) -> None:
    media_dir = out_dir / "media"
    pillow_logged = [False]
    for section in sections:
        if section.icon_src is not None:
            result = process_image(
                section.icon_src, media_dir, section.slug, diags,
                generate_thumbnails=False, thumbnail_width=cfg.thumbnail_width,
                pillow_missing_logged=pillow_logged,
            )
            if result:
                section.icon = result["image"]
        for item in section.items:
            if item.image_src is None:
                continue
            result = process_image(
                item.image_src, media_dir, f"{section.slug}/{item.slug}", diags,
                generate_thumbnails=cfg.generate_thumbnails,
                thumbnail_width=cfg.thumbnail_width,
                pillow_missing_logged=pillow_logged,
            )
            if result is None:
                item.image_src = None
                continue
            item.image = result["image"]
            item.thumb = result["thumb"]
            item.image_w = result["image_w"]
            item.image_h = result["image_h"]
            counters["images"] += 1
            if result["thumb"] != result["image"]:
                counters["thumbnails"] += 1


def finalise_items(cfg, sections: list[Section]) -> None:
    section_titles = {s.slug: s.title for s in sections}
    for section in sections:
        for item in section.items:
            if not item.body_html and item.body_md.strip():
                item.body_html = render_markdown(item.body_md)
            if not item.summary:
                item.summary = derive_summary(item.body_html)
            item.search = fold_search_text(" ".join([
                item.title,
                item.summary,
                html_to_text(item.body_html),
                " ".join(item.tags),
                section_titles.get(item.section, ""),
            ]))


def copy_src(out_dir: Path) -> None:
    for entry in sorted(SRC_DIR.iterdir(), key=lambda p: p.name):
        if entry.name == "index.html":
            continue  # template, rendered separately
        dest = out_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True)
        else:
            shutil.copyfile(entry, dest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Gallery static site generator")
    parser.add_argument("--config", default=str(REPO_ROOT / "gallery.config.toml"))
    parser.add_argument("--content-dir", default=str(REPO_ROOT / ".content"))
    parser.add_argument("--topics-snapshot", default=str(REPO_ROOT / ".topics" / "topics.json"))
    parser.add_argument("--out", default=str(REPO_ROOT / "public"))
    parser.add_argument("--dist", default=str(REPO_ROOT / "dist" / "standalone"))
    parser.add_argument("--no-topics", action="store_true",
                        help="skip the topic merge entirely (determinism/offline builds)")
    parser.add_argument("--standalone", action="store_true",
                        help="also emit the file://-safe standalone build")
    parser.add_argument("--single-file", action="store_true",
                        help="with --standalone: inline small images as data URIs")
    args = parser.parse_args(argv)

    cfg, config_notices = load_config(args.config)
    diags = Diagnostics()
    for msg in config_notices:
        diags.notice(msg)
    default_image_rel = resolve_default_image(cfg, diags)

    # 1-2. Acquire (done by CI) + discover.
    content_dir = Path(args.content_dir)
    content_root = (content_dir / cfg.content_subdir).resolve() \
        if cfg.content_subdir not in (".", "") else content_dir
    sections = discover(content_root, diags)

    # 3-5. Parse/validate happened during discovery; render markdown now.
    finalise_items(cfg, sections)

    # 6. Merge topic items.
    topic_meta = {"enabled": False, "name": cfg.topic_name, "instance": None,
                  "fetched": False, "count": 0, "dropped_duplicates": 0}
    if not args.no_topics:
        curated = [item for section in sections for item in section.items]
        topic_section, topic_meta = merge_topics(cfg, curated, Path(args.topics_snapshot), diags)
        if topic_section is not None:
            if any(s.slug == topic_section.slug for s in sections):
                diags.notice(
                    f"topic: curated section already uses slug {topic_section.slug!r}; "
                    "appending topic items to it"
                )
                for section in sections:
                    if section.slug == topic_section.slug:
                        section.items.extend(topic_section.items)
            else:
                sections.append(topic_section)
            finalise_items(cfg, sections)

    # Drop sections that ended up with zero items (incl. empty topic section).
    sections = [s for s in sections if s.items]

    # Bail out before writing anything if validation failed.
    if diags.errors or (cfg.strict and diags.warnings):
        diags.report(sys.stderr)
        summary = f"{len(diags.errors)} error(s), {len(diags.warnings)} warning(s)"
        if cfg.strict and diags.warnings and not diags.errors:
            summary += " [strict mode escalates warnings]"
        print(f"\nbuild failed: {summary}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # 7. Images.
    counters = {"images": 0, "thumbnails": 0}
    process_all_images(cfg, sections, out_dir, diags, counters)
    if diags.errors or (cfg.strict and diags.warnings):
        diags.report(sys.stderr)
        return 1

    total_items = sum(len(s.items) for s in sections)
    if total_items > 3000 and cfg.cards_per_page == 0:
        diags.warn(
            f"{total_items} items is a lot for a single page; consider setting "
            "cards_per_page for incremental rendering"
        )

    # 8. Emit the index.
    ref = cfg.content_repo_ref
    commit = content_commit(content_dir)
    index = build_index(cfg, sections, topic_meta, ref, commit)
    (out_dir / "gallery.json").write_text(
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 9. Copy src/ + render the page.
    copy_src(out_dir)
    template = (SRC_DIR / "index.html").read_text(encoding="utf-8")
    tag_counts = {t["name"]: t["count"] for t in index["tags"]}
    hosted_html = render_page(
        template, cfg=cfg, index=index, sections=sections, tag_counts=tag_counts,
        standalone=False, single_file=False, out_dir=out_dir,
        placeholder_url=default_image_rel,
    )
    (out_dir / "index.html").write_text(hosted_html, encoding="utf-8")

    if args.standalone:
        dist_dir = Path(args.dist)
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
        dist_dir.mkdir(parents=True)
        media_src = out_dir / "media"
        if media_src.is_dir():
            shutil.copytree(media_src, dist_dir / "media")
        # The default image ships as a media/ file rather than a data URI:
        # it may be a large raster, and a data URI would be repeated in every
        # imageless tile. media/ is already the standalone file's one sibling.
        default_src = SRC_DIR / default_image_rel
        standalone_placeholder = f"media/gallery-default{default_src.suffix.lower()}"
        (dist_dir / "media").mkdir(exist_ok=True)
        shutil.copyfile(default_src, dist_dir / standalone_placeholder)
        standalone_html = render_page(
            template, cfg=cfg, index=index, sections=sections, tag_counts=tag_counts,
            standalone=True, single_file=args.single_file, out_dir=dist_dir,
            placeholder_url=standalone_placeholder,
        )
        target = dist_dir / "index.html"
        target.write_text(standalone_html, encoding="utf-8")
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"standalone: {target} ({size_mb:.2f} MB)")
        if args.single_file and size_mb > 25:
            diags.warn(f"single-file standalone build is {size_mb:.1f} MB (> 25 MB)")

    # 10. Report.
    diags.report(sys.stderr)
    by_source = {"content": 0, "topic": 0}
    for section in sections:
        for item in section.items:
            by_source[item.source] = by_source.get(item.source, 0) + 1
    print(
        f"\nbuild ok: {len(sections)} sections, {total_items} items "
        f"({by_source.get('content', 0)} content + {by_source.get('topic', 0)} topic), "
        f"{counters['images']} images, {counters['thumbnails']} thumbnails, "
        f"{len(diags.warnings)} warnings, {len(diags.notices)} topic/config notices"
    )
    print(f"content: ref={ref} commit={commit or 'unknown'}")
    print(
        "topic: "
        + (
            f"instance={topic_meta.get('instance') or 'n/a'} fetched={topic_meta.get('fetched')} "
            f"count={topic_meta.get('count')} dropped_duplicates={topic_meta.get('dropped_duplicates')}"
            if topic_meta.get("enabled")
            else "disabled"
        )
    )
    if cfg.strict and diags.warnings:
        print("build failed: warnings escalated by strict = true", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
