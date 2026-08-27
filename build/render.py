"""Markdown -> sanitised HTML for the AI Gallery.

CommonMark rendering with raw HTML disabled at the parser level, followed by
an allow-list sanitiser (nh3/ammonia). Every body — curated or topic-derived —
passes through here; treat all input as untrusted.
"""

from __future__ import annotations

import html as html_mod
import re
import unicodedata

import nh3
from markdown_it import MarkdownIt

ALLOWED_TAGS = {
    "p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "blockquote",
    "a", "h3", "h4", "h5", "hr", "del", "kbd",
    "table", "thead", "tbody", "tr", "th", "td",
}
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}
ALLOWED_URL_SCHEMES = {"http", "https"}

# Raw HTML in Markdown is disabled at the parser level (html=False) as well
# as stripped by the sanitiser afterwards.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})

# h1/h2 would compete with the page's own heading hierarchy; demote to h3
# (h6 is not in the allow-list; clamp it to h5).
_DEMOTE = {"h1": "h3", "h2": "h3", "h6": "h5"}

# Sanitiser output serialises attributes as double-quoted strings, so this
# matches complete <a ...> open tags without being fooled by '>' in values.
_A_TAG_RE = re.compile(r'<a((?:\s+[a-zA-Z-]+="[^"]*")*)\s*>')
_HREF_RE = re.compile(r'\shref="([^"]*)"')


def _externalise_anchors(sanitised: str) -> str:
    """Force rel="noopener noreferrer" and target="_blank" on external anchors."""

    def _rewrite(match: re.Match) -> str:
        attrs = match.group(1)
        href = _HREF_RE.search(attrs)
        if href and href.group(1).lower().startswith(("http://", "https://")):
            return f'<a{attrs} target="_blank" rel="noopener noreferrer">'
        return match.group(0)

    return _A_TAG_RE.sub(_rewrite, sanitised)


def render_markdown(markdown: str) -> str:
    """CommonMark -> HTML -> allow-list sanitised HTML."""
    raw = _MD.render(markdown or "")
    for src, dst in _DEMOTE.items():
        raw = raw.replace(f"<{src}>", f"<{dst}>").replace(f"</{src}>", f"</{dst}>")
    clean = nh3.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel=None,
    )
    return _externalise_anchors(clean).strip()


def sanitise_html(fragment: str) -> str:
    """Sanitise an HTML fragment that did not come from our own renderer."""
    clean = nh3.clean(
        fragment or "",
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel=None,
    )
    return _externalise_anchors(clean).strip()


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(fragment: str) -> str:
    """Plain text from an HTML fragment (for summaries and the search field)."""
    text = _TAG_RE.sub(" ", fragment or "")
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fold_search_text(text: str) -> str:
    """Lowercase, diacritic-folded, whitespace-normalised text for search."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", folded.lower()).strip()


def derive_summary(body_html: str, limit: int = 160) -> str:
    """First ~`limit` chars of the body's plain text."""
    text = html_to_text(body_html)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "…"


def escape_html(text: str) -> str:
    return html_mod.escape(text or "", quote=True)
