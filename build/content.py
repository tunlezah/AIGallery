"""Content discovery, frontmatter parsing and validation for the AI Gallery.

Walks the content root (top-level directories are sections; directories one
level below are items when they contain index.md), parses YAML frontmatter,
validates every field per the schema, and accumulates *all* diagnostics so
they can be reported together at the end of the build.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

class Diagnostics:
    """Aggregates errors, warnings and topic notices for end-of-build report.

    Severities:
      * error   -- always fails the build.
      * warning -- fails the build only when strict = true.
      * notice  -- topic-subscription diagnostics; NEVER fails the build,
                   even under strict = true.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notices: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def notice(self, msg: str) -> None:
        self.notices.append(msg)

    def report(self, out) -> None:
        for msg in self.errors:
            print(f"error: {msg}", file=out)
        for msg in self.warnings:
            print(f"warning: {msg}", file=out)
        for msg in self.notices:
            print(f"notice: {msg}", file=out)

    def failed(self, strict: bool) -> bool:
        return bool(self.errors) or (strict and bool(self.warnings))


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Item:
    slug: str
    section: str                       # section slug
    source: str                        # "content" | "topic"
    title: str
    path: str = ""                     # source path, for diagnostics
    url: str | None = None
    image_src: Path | None = None      # resolved source file on disk
    image: str | None = None           # public path, filled by images stage
    thumb: str | None = None
    image_w: int = 0
    image_h: int = 0
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    body_md: str = ""
    body_html: str = ""
    search: str = ""
    order: int = 1000
    added: str | None = None
    updated: str | None = None
    featured: bool = False


@dataclass
class Section:
    slug: str
    title: str
    order: int = 1000
    description: str = ""
    icon_src: Path | None = None
    icon: str | None = None           # public path, filled by images stage
    icon_w: int = 0
    icon_h: int = 0
    items: list[Item] = field(default_factory=list)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """lowercase, non-alphanumerics -> '-', collapse repeats, strip edges."""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _SLUG_RE.sub("-", folded.lower()).strip("-")


def normalise_tags(raw, diags: Diagnostics, where: str) -> list[str]:
    """Trim, lowercase, collapse whitespace to '-', de-duplicate, sort.

    Accepts a list of strings or a comma-separated string.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, list):
        parts = []
        for entry in raw:
            if isinstance(entry, (str, int, float)):
                parts.append(str(entry))
            else:
                diags.warn(f"{where}: tag entry {entry!r} is not a string; dropped")
    else:
        diags.warn(f"{where}: tags must be a list or comma-separated string; dropped")
        return []
    tags = set()
    for part in parts:
        tag = re.sub(r"\s+", "-", str(part).strip().lower())
        if tag:
            tags.add(tag)
    return sorted(tags)


_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def split_frontmatter(text: str, path: str, diags: Diagnostics):
    """Return (frontmatter-dict-or-None, body). Malformed YAML is a hard error."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f" (line {mark.line + 1})" if mark else ""
        diags.error(f"{path}: malformed YAML frontmatter{line}: {exc}")
        return None, ""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        diags.error(f"{path}: frontmatter must be a YAML mapping, got {type(data).__name__}")
        return None, ""
    return data, text[match.end():]


def parse_date(value, diags: Diagnostics, where: str, fieldname: str) -> str | None:
    """Accept ISO 8601 date/datetime (or YAML-parsed date objects); warn+drop otherwise."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            pass
    diags.warn(f"{where}: invalid {fieldname} date {value!r}; field dropped")
    return None


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

# ASCII control characters. Browsers strip tab/newline anywhere in a URL and
# leading C0 controls before parsing, so "java\tscript:" or "\x01javascript:"
# would reach the browser as javascript: even though no scheme regex sees one.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def validate_url(value, diags: Diagnostics, where: str) -> str | None:
    """http(s) or relative path only. Other schemes are a hard error.

    The scheme is detected with urllib.parse rather than a regex over the raw
    string, and any ASCII control character is refused outright, so the
    browser's URL parser can never resolve a different scheme than the one
    validated here.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        diags.error(f"{where}: url must be a non-empty string")
        return None
    url = value.strip()
    if CONTROL_CHARS_RE.search(url):
        diags.error(
            f"{where}: url contains ASCII control characters (tab, newline or C0), "
            "which browsers strip before parsing; refused"
        )
        return None
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError as exc:
        diags.error(f"{where}: url is not parseable: {exc}")
        return None
    if scheme:
        if scheme in ("http", "https"):
            return url
        diags.error(f"{where}: url scheme {scheme!r} is not allowed (http(s) or relative path only)")
        return None
    # Browsers treat backslashes like slashes here, so "\\host" is also
    # protocol-relative.
    if len(url) >= 2 and url[0] in "/\\" and url[1] in "/\\":
        diags.error(f"{where}: protocol-relative urls are not allowed")
        return None
    return url


def resolve_image(value, item_dir: Path, diags: Diagnostics, where: str) -> Path | None:
    """Image must resolve inside the item directory; missing file warns."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        diags.warn(f"{where}: image must be a non-empty string; using placeholder")
        return None
    rel = value.strip().replace("\\", "/")
    if _SCHEME_RE.match(rel) or rel.startswith(("/", "//")):
        diags.error(f"{where}: image must be a relative path inside the item directory, got {rel!r}")
        return None
    pure = PurePosixPath(rel)
    if ".." in pure.parts:
        diags.error(f"{where}: image path {rel!r} traverses outside the item directory")
        return None
    candidate = (item_dir / rel).resolve()
    try:
        candidate.relative_to(item_dir.resolve())
    except ValueError:
        diags.error(f"{where}: image path {rel!r} escapes the item directory")
        return None
    if not candidate.is_file():
        diags.warn(f"{where}: image file {rel!r} not found; falling back to placeholder")
        return None
    return candidate


_WS_RE = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


ITEM_KNOWN_KEYS = {
    "title", "url", "image", "tags", "summary", "order",
    "added", "updated", "featured", "draft",
}
SECTION_KNOWN_KEYS = {"title", "order", "description", "icon"}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_item(index_md: Path, section_slug: str, diags: Diagnostics) -> Item | None:
    """Parse and validate one index.md. Returns None when invalid or a draft."""
    where = str(index_md)
    try:
        text = index_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        diags.error(f"{where}: cannot read file: {exc}")
        return None

    front, body = split_frontmatter(text, where, diags)
    if front is None and not body:
        return None  # malformed YAML already reported
    if front is None:
        diags.error(f"{where}: missing frontmatter (line 1); 'title' is required")
        return None

    for key in sorted(set(front) - ITEM_KNOWN_KEYS):
        diags.warn(f"{where}: unknown frontmatter key {key!r} ignored")

    title = front.get("title")
    if not isinstance(title, str) or not title.strip():
        diags.error(f"{where}: 'title' is required and must be a non-empty string (line 1)")
        return None
    title = collapse_ws(title)

    draft = front.get("draft", False)
    if not isinstance(draft, bool):
        diags.warn(f"{where}: draft must be a boolean; treating as false")
        draft = False
    if draft:
        return None

    featured = front.get("featured", False)
    if not isinstance(featured, bool):
        diags.warn(f"{where}: featured must be a boolean; treating as false")
        featured = False

    order = front.get("order", 1000)
    if isinstance(order, bool) or not isinstance(order, int):
        diags.warn(f"{where}: order must be an integer; using default 1000")
        order = 1000

    summary = front.get("summary")
    if summary is not None and not isinstance(summary, str):
        diags.warn(f"{where}: summary must be a string; ignored")
        summary = None
    if summary is not None:
        summary = collapse_ws(summary)
        if len(summary) > 200:
            diags.warn(f"{where}: summary exceeds 200 characters; truncated")
            summary = summary[:199].rstrip() + "…"

    item = Item(
        slug=slugify(index_md.parent.name),
        section=section_slug,
        source="content",
        title=title,
        path=where,
        url=validate_url(front.get("url"), diags, where),
        image_src=resolve_image(front.get("image"), index_md.parent, diags, where),
        tags=normalise_tags(front.get("tags"), diags, where),
        summary=summary or "",
        body_md=body,
        order=order,
        added=parse_date(front.get("added"), diags, where, "added"),
        updated=parse_date(front.get("updated"), diags, where, "updated"),
        featured=featured,
    )
    if not item.slug:
        diags.error(f"{where}: directory name {index_md.parent.name!r} slugifies to nothing")
        return None
    return item


def parse_section_meta(section_dir: Path, slug: str, diags: Diagnostics) -> Section:
    section = Section(slug=slug, title=section_dir.name.replace("-", " ").replace("_", " ").title())
    meta_path = section_dir / "_section.md"
    if not meta_path.is_file():
        return section
    where = str(meta_path)
    try:
        text = meta_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        diags.error(f"{where}: cannot read file: {exc}")
        return section
    front, _body = split_frontmatter(text, where, diags)
    if front is None:
        return section
    for key in sorted(set(front) - SECTION_KNOWN_KEYS):
        diags.warn(f"{where}: unknown frontmatter key {key!r} ignored")
    title = front.get("title")
    if isinstance(title, str) and title.strip():
        section.title = collapse_ws(title)
    elif title is not None:
        diags.warn(f"{where}: title must be a non-empty string; using directory name")
    order = front.get("order", 1000)
    if isinstance(order, bool) or not isinstance(order, int):
        diags.warn(f"{where}: order must be an integer; using default 1000")
        order = 1000
    section.order = order
    desc = front.get("description", "")
    if isinstance(desc, str):
        section.description = collapse_ws(desc)
    elif desc is not None:
        diags.warn(f"{where}: description must be a string; ignored")
    section.icon_src = resolve_image(front.get("icon"), section_dir, diags, where)
    return section


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _hidden(name: str) -> bool:
    return name.startswith(("_", "."))


def discover(content_root: Path, diags: Diagnostics) -> list[Section]:
    """Walk the content root and build the section -> item tree."""
    if not content_root.is_dir():
        diags.error(f"content root {content_root} does not exist or is not a directory")
        return []

    sections: list[Section] = []
    for entry in sorted(content_root.iterdir(), key=lambda p: p.name):
        if _hidden(entry.name):
            continue
        if entry.is_file():
            diags.warn(f"{entry}: stray file at content root ignored (top-level entries must be section directories)")
            continue
        if not entry.is_dir():
            continue
        slug = slugify(entry.name)
        if not slug:
            diags.error(f"{entry}: section directory name slugifies to nothing")
            continue
        section = parse_section_meta(entry, slug, diags)
        seen_slugs: dict[str, str] = {}
        for child in sorted(entry.iterdir(), key=lambda p: p.name):
            if _hidden(child.name):
                continue
            if child.is_file():
                continue  # loose asset files inside a section are fine
            if not child.is_dir():
                continue
            index_md = child / "index.md"
            if not index_md.is_file():
                diags.warn(f"{child}: directory has no index.md; not a gallery item, skipped")
                continue
            for grandchild in sorted(child.iterdir(), key=lambda p: p.name):
                if grandchild.is_dir() and not _hidden(grandchild.name):
                    diags.warn(
                        f"{grandchild}: nesting deeper than two levels is out of scope; "
                        "directory ignored (items are exactly one level below a section)"
                    )
            item = parse_item(index_md, section.slug, diags)
            if item is None:
                continue
            if item.slug in seen_slugs:
                diags.error(
                    f"duplicate slug '{section.slug}/{item.slug}': "
                    f"{seen_slugs[item.slug]} and {child}"
                )
                continue
            seen_slugs[item.slug] = str(child)
            section.items.append(item)
        sections.append(section)

    slug_counts: dict[str, list[str]] = {}
    for section in sections:
        slug_counts.setdefault(section.slug, []).append(section.title)
    for slug, titles in sorted(slug_counts.items()):
        if len(titles) > 1:
            diags.error(f"duplicate section slug {slug!r} from directories {titles}")

    return sections
