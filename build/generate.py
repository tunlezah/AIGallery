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
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config, resolve_api_base  # noqa: E402
from content import Diagnostics, Item, Section, discover  # noqa: E402
from images import derive_default_image, process_image  # noqa: E402
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

FALLBACK_DEFAULT_IMAGE = "assets/placeholder.svg"
# --single-file inlines media files up to this size as data URIs.
INLINE_THRESHOLD_KB = 200
# Section icons are decorative and small; never ship them wider than this.
ICON_MAX_WIDTH = 256
# The inline JSON block carries only what app.js reads. Bodies, images and
# links are already baked into the tiles; gallery.json keeps the full record.
LEAN_ITEM_KEYS = (
    "id", "section", "source", "title", "tags", "search",
    "order", "added", "updated", "featured",
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


def _date_desc_key(value: str | None) -> int:
    """ISO dates sort numerically as YYYYMMDD; negated for newest-first while
    the title tie-break stays ascending (mirrors the comparator in app.js)."""
    digits = re.sub(r"\D", "", value or "")
    return -int(digits) if digits else 0


def sort_items(items: list[Item], mode: str) -> list[Item]:
    fold = fold_search_text
    if mode == "title":
        return sorted(items, key=lambda i: (fold(i.title), i.section, i.slug))
    if mode in ("added", "updated"):
        # newest first, then title A-Z; undated items last, by title
        with_date = sorted(
            (i for i in items if getattr(i, mode)),
            key=lambda i: (_date_desc_key(getattr(i, mode)), fold(i.title), i.slug),
        )
        without = sorted((i for i in items if not getattr(i, mode)),
                         key=lambda i: (fold(i.title), i.slug))
        return with_date + without
    return sorted(items, key=lambda i: (i.order, fold(i.title), i.slug))


def build_index(cfg, sections: list[Section], topic_meta: dict,
                ref: str | None, commit: str | None, default_image: str) -> dict:
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
        "site": {
            "title": cfg.site_title,
            "description": cfg.site_description,
            "default_image": default_image,
        },
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
    featured_attr = ' data-featured="true"' if item.featured else ""
    parts = [
        f'<article class="tile" data-id="{esc(item_id)}"{featured_attr}>',
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
    if item.featured:
        footer_bits.append('<span class="tile-badge tile-badge-featured">Featured</span>')
    if item.source == "topic":
        footer_bits.append(
            f'<span class="tile-badge">from topic<span class="visually-hidden">'
            f' — discovered via the GitLab topic {esc(topic_name)}</span></span>'
        )
    parts.append(f'<div class="tile-footer">{"".join(footer_bits)}</div>')
    parts.append("</div></article>")
    return "".join(parts)


def _icon_html(section: Section, css_class: str) -> str:
    """Decorative section icon (alt="": the section title is right beside it)."""
    if not section.icon:
        return ""
    dims = (
        f' width="{section.icon_w}" height="{section.icon_h}"'
        if section.icon_w and section.icon_h else ""
    )
    return f'<img class="{css_class}" src="{escape_html(section.icon)}"{dims} alt="" decoding="async">'


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
            f'<h2 id="section-h-{escape_html(section.slug)}">{_icon_html(section, "section-icon")}'
            f'{escape_html(section.title)}'
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
            f'aria-pressed="false"><span class="rail-label">{_icon_html(section, "rail-icon")}'
            f'{esc(section.title)}</span> '
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


def topic_status_html(cfg, topic_meta: dict) -> str:
    """Footer line that makes the topic feed's build-time state visible.

    The fetch step can never fail the pipeline, so without this a feed that
    has been broken for weeks would look exactly like a feed with no projects.
    """
    if not topic_meta.get("enabled"):
        return ""
    name = escape_html(cfg.topic_name)
    if not topic_meta.get("fetched"):
        return (
            '<p class="topic-status topic-status-warn">The GitLab topic feed for '
            f'<code>{name}</code> was unavailable when this build ran, so subscribed '
            "projects may be missing.</p>"
        )
    count = int(topic_meta.get("count") or 0)
    noun = "project" if count == 1 else "projects"
    text = (
        f"{count} subscribed {noun} discovered via the GitLab topic "
        f"<code>{name}</code> at build time."
    )
    degraded = bool(topic_meta.get("partial") or topic_meta.get("truncated"))
    if topic_meta.get("truncated"):
        text += " The list was cut off at the configured maximum."
    if topic_meta.get("partial"):
        text += " Some project images or manifests could not be fetched."
    css = "topic-status topic-status-warn" if degraded else "topic-status"
    return f'<p class="{css}">{text}</p>'


def public_repo_url(url: str) -> str:
    """Repository URL for display: credentials stripped, trailing .git removed.

    Someone may point GALLERY_CONTENT_REPO_URL at a token-bearing URL; the
    help dialog must never publish it.
    """
    url = (url or "").strip().removesuffix(".git")
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def topic_instance_host(cfg, topic_meta: dict, environ: dict | None = None) -> str:
    """Host (and port) of the GitLab instance the topic feed reads, for the
    help dialog; "" when nothing resolves.

    The snapshot's recorded instance wins because it is what the fetch step
    actually queried; without a snapshot the same resolution order the fetch
    step uses (topic_api_base -> CI_SERVER_URL -> content_repo_url) applies.
    """
    candidates = [topic_meta.get("instance"), resolve_api_base(cfg, environ)[0]]
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            parts = urlsplit(candidate.strip())
            port = parts.port
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and parts.hostname:
            host = parts.hostname
            if ":" in host:
                host = f"[{host}]"
            return f"{host}:{port}" if port else host
    return ""


def help_slots(cfg, topic_meta: dict, environ: dict | None = None) -> dict[str, str]:
    """Template slots for the help dialog, baked from configuration.

    Everything the help text states about *this* gallery (repo link, topic
    name, manifest path, section name, size caps, and the eligibility rules
    that follow from topic_visibility / topic_include_archived /
    topic_allow_namespaces) is derived here, so reconfiguring the gallery
    cannot leave the help stale. All values are HTML-escaped.
    """
    esc = escape_html
    host = topic_instance_host(cfg, topic_meta, environ)

    rules: list[str] = []
    if cfg.topic_visibility:
        vis = esc(cfg.topic_visibility)
        rules.append(
            f"<li>The project's visibility is <strong>{vis}</strong> &#8212; this gallery "
            f"lists only {vis} projects.</li>"
        )
    else:
        rules.append(
            "<li>The gallery's build can see the project: a <strong>public</strong> project "
            "always qualifies; an internal or private one only when the gallery's "
            "maintainers gave the build an API token that can read it.</li>"
        )
    if not cfg.topic_include_archived:
        rules.append("<li>The project is not archived.</li>")
    if cfg.topic_allow_namespaces:
        globs = ", ".join(f"<code>{esc(glob)}</code>" for glob in cfg.topic_allow_namespaces)
        rules.append(
            "<li>The project path is inside a namespace this gallery accepts: "
            f"{globs}.</li>"
        )

    return {
        "{{CONTENT_REPO_URL}}": esc(public_repo_url(cfg.content_repo_url)),
        "{{TOPIC_NAME}}": esc(cfg.topic_name),
        "{{TOPIC_MANIFEST_PATH}}": esc(cfg.topic_manifest_path),
        "{{TOPIC_SECTION_TITLE}}": esc(cfg.topic_section_title),
        "{{TOPIC_INSTANCE_HOST}}": esc(host) if host else "this GitLab instance",
        "{{TOPIC_AVATAR_MAX_KB}}": str(int(cfg.topic_avatar_max_kb)),
        "{{TOPIC_MAX_PROJECTS}}": str(int(cfg.topic_max_projects)),
        "{{TOPIC_ELIGIBILITY_RULES}}": "\n".join(rules),
    }


# <!--if:flag-->...<!--/if:flag--> keeps its body only when `flag` is on;
# <!--if:!flag-->...<!--/if:!flag--> only when it is off. Blocks may nest.
_COND_BLOCK_RE = re.compile(r"<!--if:(!?)([a-z_]+)-->(.*?)<!--/if:\1\2-->", re.DOTALL)


def apply_conditional_blocks(html: str, flags: dict[str, bool]) -> str:
    """Resolve the template's conditional comment blocks against `flags`.

    An unknown flag name is a template bug and raises, so a typo cannot
    silently ship half a sentence.
    """

    def _resolve(match: re.Match) -> str:
        negate, name, body = match.group(1), match.group(2), match.group(3)
        if name not in flags:
            raise KeyError(f"template uses unknown conditional flag {name!r}")
        return body if flags[name] != bool(negate) else ""

    while True:
        resolved = _COND_BLOCK_RE.sub(_resolve, html)
        if resolved == html:
            break
        html = resolved
    if "<!--if:" in html or "<!--/if:" in html:
        raise ValueError("template has an unbalanced conditional block")
    return html


def resolve_default_image(cfg, diags: Diagnostics) -> str:
    """Validate cfg.default_image as a path inside src/; fall back if broken.

    The returned value is a src/-relative POSIX path. The build derives a
    tile-sized copy of it under media/ (see derive_default_image), so the
    full-size source is never shipped.
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


def render_page(template: str, *, cfg, index: dict, sections, tag_counts, topic_meta: dict,
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

    runtime_placeholder = placeholder_url
    if standalone and single_file:
        # A one-file build has no media/ sibling: make the runtime image
        # fallback self-contained as well.
        candidate = out_dir / placeholder_url
        if candidate.is_file() and candidate.stat().st_size <= INLINE_THRESHOLD_KB * 1024:
            runtime_placeholder = data_uri(candidate)

    payload = {
        "schema": index["schema"],
        "generated_from": index["generated_from"],
        "runtime": {
            "placeholder": runtime_placeholder,
            "cards_per_page": cfg.cards_per_page,
            "default_sort": cfg.default_sort,
            "topic_name": cfg.topic_name,
        },
        "items": [{key: item[key] for key in LEAN_ITEM_KEYS} for item in index["items"]],
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
        "{{TOPIC_STATUS}}": topic_status_html(cfg, topic_meta),
        # Help dialog: baked from config so the text stays correct when the
        # content repo, topic name, manifest path or feed scope change.
        **help_slots(cfg, topic_meta),
    }
    # The help dialog describes the topic path only when the feature is on
    # (a --no-topics dev build still documents the deployed gallery), and
    # mentions strict mode only when warnings really do fail the build.
    html = apply_conditional_blocks(
        html, {"topics": bool(cfg.topics_enabled), "strict": bool(cfg.strict)}
    )
    for token, value in replacements.items():
        html = html.replace(token, value)

    if standalone and single_file:
        html = _inline_media(html, out_dir)
    return html


def _inline_media(html: str, out_dir: Path, threshold_kb: int = INLINE_THRESHOLD_KB) -> str:
    """--single-file: data-URI-inline media images under the size threshold."""

    def _sub(match: re.Match) -> str:
        rel = match.group(1)
        path = out_dir / rel
        if path.is_file() and path.stat().st_size <= threshold_kb * 1024:
            return f'src="{data_uri(path)}"'
        return match.group(0)

    return re.sub(r'src="(media/[^"]+)"', _sub, html)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def process_all_images(cfg, sections: list[Section], out_dir: Path, diags: Diagnostics,
                       counters: dict, pillow_logged: list[bool]) -> None:
    media_dir = out_dir / "media"
    icon_width = min(cfg.thumbnail_width, ICON_MAX_WIDTH)
    for section in sections:
        if section.icon_src is not None:
            result = process_image(
                section.icon_src, media_dir, section.slug, diags,
                generate_thumbnails=cfg.generate_thumbnails, thumbnail_width=icon_width,
                pillow_missing_logged=pillow_logged,
            )
            if result:
                section.icon = result["thumb"]
                if result["thumb"] != result["image"]:
                    section.icon_w = icon_width
                    section.icon_h = max(1, round(result["image_h"] * icon_width / result["image_w"]))
                else:
                    section.icon_w, section.icon_h = result["image_w"], result["image_h"]
        for item in section.items:
            if item.image_src is None:
                continue
            result = process_image(
                item.image_src, media_dir, f"{section.slug}/{item.slug}", diags,
                generate_thumbnails=cfg.generate_thumbnails,
                thumbnail_width=cfg.thumbnail_width,
                pillow_missing_logged=pillow_logged,
                allow_svg=item.source != "topic",
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


def copy_src(out_dir: Path, skip: set[Path] = frozenset()) -> None:
    """Mirror src/ into the output, minus the template and any `skip` files
    (the full-size default image ships under media/ at tile size instead)."""
    skip_resolved = {path.resolve() for path in skip}

    def _ignore(directory, names):
        return [name for name in names if (Path(directory) / name).resolve() in skip_resolved]

    for entry in sorted(SRC_DIR.iterdir(), key=lambda p: p.name):
        if entry.name == "index.html" or entry.resolve() in skip_resolved:
            continue  # template, rendered separately
        dest = out_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True, ignore=_ignore)
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
                  "fetched": False, "count": 0, "dropped_duplicates": 0,
                  "partial": False, "truncated": False}
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
    if diags.failed(cfg.strict):
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

    # 7. Images: items and section icons, then the default image at tile size.
    counters = {"images": 0, "thumbnails": 0}
    pillow_logged = [False]
    process_all_images(cfg, sections, out_dir, diags, counters, pillow_logged)
    default_public = derive_default_image(
        SRC_DIR / default_image_rel, out_dir / "media", diags,
        generate_thumbnails=cfg.generate_thumbnails, thumbnail_width=cfg.thumbnail_width,
        pillow_missing_logged=pillow_logged,
    )
    if default_public is None and not diags.errors:
        default_image_rel = FALLBACK_DEFAULT_IMAGE
        default_public = derive_default_image(
            SRC_DIR / FALLBACK_DEFAULT_IMAGE, out_dir / "media", diags,
            generate_thumbnails=False, thumbnail_width=cfg.thumbnail_width,
            pillow_missing_logged=pillow_logged,
        )
    if diags.failed(cfg.strict):
        diags.report(sys.stderr)
        print("\nbuild failed during image processing", file=sys.stderr)
        return 1
    default_public = default_public or FALLBACK_DEFAULT_IMAGE

    total_items = sum(len(s.items) for s in sections)
    if total_items > 3000 and cfg.cards_per_page == 0:
        diags.warn(
            f"{total_items} items is a lot for a single page; consider setting "
            "cards_per_page for incremental rendering"
        )

    # 8. Emit the index.
    ref = cfg.content_repo_ref
    commit = content_commit(content_dir)
    index = build_index(cfg, sections, topic_meta, ref, commit, default_public)
    (out_dir / "gallery.json").write_text(
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 9. Copy src/ (minus the template and the full-size default image) and
    #    render the page.
    skip = {SRC_DIR / default_image_rel} - {SRC_DIR / FALLBACK_DEFAULT_IMAGE}
    copy_src(out_dir, skip)
    template = (SRC_DIR / "index.html").read_text(encoding="utf-8")
    tag_counts = {t["name"]: t["count"] for t in index["tags"]}
    hosted_html = render_page(
        template, cfg=cfg, index=index, sections=sections, tag_counts=tag_counts,
        topic_meta=topic_meta, standalone=False, single_file=False, out_dir=out_dir,
        placeholder_url=default_public,
    )
    (out_dir / "index.html").write_text(hosted_html, encoding="utf-8")

    if args.standalone:
        dist_dir = Path(args.dist)
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
        dist_dir.mkdir(parents=True)
        # media/ (item images, icons and the tile-sized default image) is the
        # standalone file's one sibling.
        media_src = out_dir / "media"
        if media_src.is_dir():
            shutil.copytree(media_src, dist_dir / "media")
        standalone_html = render_page(
            template, cfg=cfg, index=index, sections=sections, tag_counts=tag_counts,
            topic_meta=topic_meta, standalone=True, single_file=args.single_file,
            out_dir=dist_dir, placeholder_url=default_public,
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
    print(f"default image: {default_public}")
    print(
        "topic: "
        + (
            f"instance={topic_meta.get('instance') or 'n/a'} fetched={topic_meta.get('fetched')} "
            f"count={topic_meta.get('count')} dropped_duplicates={topic_meta.get('dropped_duplicates')} "
            f"partial={topic_meta.get('partial')} truncated={topic_meta.get('truncated')}"
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
