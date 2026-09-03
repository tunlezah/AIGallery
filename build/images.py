"""Image handling: copy item images into public/media, probe intrinsic
dimensions (stdlib parsers, so width/height work even without Pillow),
optionally derive width-constrained WebP thumbnails via Pillow, sniff the
real type of downloaded bytes, and refuse SVG files that carry active
content.
"""

from __future__ import annotations

import re
import shutil
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from PIL import Image
    HAVE_PILLOW = True
except ImportError:  # pragma: no cover - environment dependent
    Image = None
    HAVE_PILLOW = False

RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_EXTS = RASTER_EXTS | {".svg"}


# --------------------------------------------------------------------------
# Dimension probing (stdlib, deterministic)
# --------------------------------------------------------------------------

def _png_size(head: bytes):
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        w, h = struct.unpack(">II", head[16:24])
        return w, h
    return None


def _gif_size(head: bytes):
    if head[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", head[6:10])
        return w, h
    return None


def _jpeg_size(data: bytes):
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


def _webp_size(head: bytes):
    if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    fourcc = head[12:16]
    if fourcc == b"VP8 ":
        if head[23:26] != b"\x9d\x01\x2a":
            return None
        w, h = struct.unpack("<HH", head[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if fourcc == b"VP8L":
        if head[20] != 0x2F:
            return None
        b0, b1, b2, b3 = head[21:25]
        w = ((b1 & 0x3F) << 8 | b0) + 1
        h = ((b3 & 0x0F) << 10 | b2 << 2 | b1 >> 6) + 1
        return w, h
    if fourcc == b"VP8X":
        w = int.from_bytes(head[24:27], "little") + 1
        h = int.from_bytes(head[27:30], "little") + 1
        return w, h
    return None


_SVG_WH_RE = re.compile(
    rb'<svg[^>]*?\swidth="([0-9.]+)(?:px)?"[^>]*?\sheight="([0-9.]+)(?:px)?"',
    re.DOTALL,
)
_SVG_VB_RE = re.compile(
    rb'<svg[^>]*?\sviewBox="[0-9.+-]+[ ,]+[0-9.+-]+[ ,]+([0-9.]+)[ ,]+([0-9.]+)"',
    re.DOTALL,
)


def _svg_size(head: bytes):
    match = _SVG_WH_RE.search(head) or _SVG_VB_RE.search(head)
    if match:
        return round(float(match.group(1))), round(float(match.group(2)))
    return None


def probe_dimensions(path: Path) -> tuple[int, int] | None:
    """Intrinsic (width, height) via stdlib header parsing; None if unknown."""
    try:
        if path.suffix.lower() in (".jpg", ".jpeg"):
            data = path.read_bytes()
        else:
            with path.open("rb") as fh:
                data = fh.read(4096)
    except OSError:
        return None
    ext = path.suffix.lower()
    result = None
    if ext == ".png":
        result = _png_size(data)
    elif ext == ".gif":
        result = _gif_size(data)
    elif ext in (".jpg", ".jpeg"):
        result = _jpeg_size(data)
    elif ext == ".webp":
        result = _webp_size(data)
    elif ext == ".svg":
        result = _svg_size(data)
    if result is None and HAVE_PILLOW and ext in RASTER_EXTS:
        try:
            with Image.open(path) as img:
                result = img.size
        except OSError:
            result = None
    if result and result[0] > 0 and result[1] > 0:
        return int(result[0]), int(result[1])
    return None


# --------------------------------------------------------------------------
# Content sniffing (for bytes fetched from third parties)
# --------------------------------------------------------------------------

def sniff_image_type(head: bytes) -> str | None:
    """Extension implied by the magic bytes of a raster image, or None.

    Only PNG, GIF, JPEG and WebP are recognised; SVG is deliberately not,
    because SVG is never accepted from third-party sources.
    """
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def extension_matches(ext: str, sniffed: str | None) -> bool:
    """True when a file extension agrees with a sniff_image_type() result."""
    if sniffed is None:
        return False
    ext = ext.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    return ext == sniffed


# --------------------------------------------------------------------------
# SVG safety
# --------------------------------------------------------------------------

# Elements that run script, embed foreign documents or fetch resources.
_SVG_FORBIDDEN_ELEMENTS = {
    "script", "foreignobject", "iframe", "object", "embed", "handler",
    "audio", "video", "meta", "base", "link",
}
# Attributes that reference resources (plus SMIL animation targets, which
# can rewrite href at runtime).
_SVG_REF_ATTRS = {"href", "src", "from", "to", "values", "by"}
_SVG_ACTIVE_PREFIXES = (
    "javascript:", "vbscript:", "data:text", "data:application", "data:image/svg",
)
_SVG_EXTERNAL_PREFIXES = ("http:", "https:", "//", "ftp:", "file:")
_SVG_CSS_FETCH_RE = re.compile(r"url\s*\(|@import|expression\s*\(", re.IGNORECASE)
# Browsers strip tab/newline (and leading controls/spaces) before parsing a
# URL; fold them away before looking at the prefix.
_SVG_URL_FOLD_RE = re.compile(r"[\x00-\x20\x7f]")


def _svg_local(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def svg_unsafe_reason(path: Path) -> str | None:
    """Why an SVG must not be served, or None when it looks inert.

    SVGs are copied into the site and served from its origin. Inside <img>
    they are inert, but opened directly a browser runs their scripts on the
    site's origin, and url()/href references would make network requests.
    This is a validator, not a sanitiser: anything active is refused.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "not readable as UTF-8 text"
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:
        return "DOCTYPE/ENTITY declarations are not allowed"
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return f"not well-formed XML ({exc})"
    if not isinstance(root.tag, str) or _svg_local(root.tag) != "svg":
        return "root element is not <svg>"
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        local = _svg_local(element.tag)
        if local in _SVG_FORBIDDEN_ELEMENTS:
            return f"<{local}> element"
        if local == "style" and element.text and _SVG_CSS_FETCH_RE.search(element.text):
            return "<style> with url()/@import"
        for name, value in element.attrib.items():
            lname = _svg_local(name)
            folded = _SVG_URL_FOLD_RE.sub("", value).lower()
            if lname.startswith("on"):
                return f"event handler attribute {lname!r}"
            if folded.startswith(_SVG_ACTIVE_PREFIXES):
                return f"active URL in attribute {lname!r}"
            if (lname in _SVG_REF_ATTRS or lname.endswith("href")) and \
                    folded.startswith(_SVG_EXTERNAL_PREFIXES):
                return f"external reference in attribute {lname!r}"
            if lname == "style" and _SVG_CSS_FETCH_RE.search(value):
                return "style attribute with url()/expression()"
    return None


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------

def _thumbnail_mode(img) -> str:
    has_alpha = "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info)
    return "RGBA" if has_alpha else "RGB"


def write_thumbnail(src: Path, dest: Path, width: int) -> None:
    """Resize `src` to `width` px wide and save it as WebP at `dest`.

    Pillow is required. Quality 80 / method 4; deterministic for a fixed
    Pillow version (pinned in requirements.txt). Alpha is preserved.
    """
    with Image.open(src) as img:
        ratio = width / img.width
        new_size = (width, max(1, round(img.height * ratio)))
        resized = img.convert(_thumbnail_mode(img)).resize(new_size, Image.LANCZOS)
        resized.save(dest, "WEBP", quality=80, method=4)


def _wants_thumbnail(ext: str, width: int, generate_thumbnails: bool, thumbnail_width: int) -> bool:
    # GIFs are excluded because animation would be lost.
    return bool(generate_thumbnails) and ext in RASTER_EXTS and ext != ".gif" and width > thumbnail_width


def _note_pillow_missing(diags, pillow_missing_logged: list[bool]) -> None:
    if not pillow_missing_logged[0]:
        diags.warn("Pillow is not installed; skipping thumbnail generation and using originals")
        pillow_missing_logged[0] = True


# --------------------------------------------------------------------------
# Copy + thumbnail
# --------------------------------------------------------------------------

def process_image(
    src: Path,
    media_dir: Path,
    rel_dir: str,
    diags,
    *,
    generate_thumbnails: bool,
    thumbnail_width: int,
    pillow_missing_logged: list[bool],
    allow_svg: bool = True,
) -> dict | None:
    """Copy `src` to public media, probe dimensions, maybe emit a thumbnail.

    Returns {"image", "thumb", "image_w", "image_h"} with public-relative
    paths (media/...), or None when the file is unsupported or refused. An
    SVG with active content is a hard error (curated content is reviewed;
    an unsafe file should fail the build with its name), and SVG is refused
    outright with a warning when `allow_svg` is False (third-party sources).
    """
    ext = src.suffix.lower()
    if ext not in ALLOWED_EXTS:
        diags.warn(f"{src}: unsupported image type {ext!r}; falling back to the default image")
        return None
    if ext == ".svg":
        if not allow_svg:
            diags.warn(f"{src}: SVG images are not accepted from this source; falling back to the default image")
            return None
        reason = svg_unsafe_reason(src)
        if reason is not None:
            diags.error(
                f"{src}: SVG rejected ({reason}); SVG images must not contain scripts, "
                "event handlers, DOCTYPE/ENTITY declarations or external references"
            )
            return None

    dest_dir = media_dir / rel_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name.lower()
    shutil.copyfile(src, dest)
    public_rel = f"media/{rel_dir}/{dest.name}"

    dims = probe_dimensions(src)
    if dims is None:
        diags.warn(f"{src}: could not determine intrinsic dimensions; assuming 4:3 (800x600)")
        dims = (800, 600)
    width, height = dims

    thumb_rel = public_rel
    if _wants_thumbnail(ext, width, generate_thumbnails, thumbnail_width):
        if not HAVE_PILLOW:
            _note_pillow_missing(diags, pillow_missing_logged)
        else:
            thumb_name = f"{dest.stem}.{thumbnail_width}.webp"
            try:
                write_thumbnail(src, dest_dir / thumb_name, thumbnail_width)
                thumb_rel = f"media/{rel_dir}/{thumb_name}"
            except Exception as exc:  # noqa: BLE001 - a bad image must not crash the build
                diags.warn(f"{src}: thumbnail generation failed ({exc}); using original")

    return {
        "image": public_rel,
        "thumb": thumb_rel,
        "image_w": width,
        "image_h": height,
    }


def derive_default_image(
    src: Path,
    media_dir: Path,
    diags,
    *,
    generate_thumbnails: bool,
    thumbnail_width: int,
    pillow_missing_logged: list[bool],
) -> str | None:
    """Ship the configured default image under media/ at tile size.

    A raster wider than `thumbnail_width` becomes media/gallery-default.<w>.webp
    (Pillow permitting); anything else is copied as media/gallery-default<ext>.
    Every imageless tile references this file, so its size matters more than
    any other asset's. Returns the public-relative path, or None when the
    source is unusable (the caller falls back to the built-in placeholder).
    """
    ext = src.suffix.lower()
    if ext not in ALLOWED_EXTS or not src.is_file():
        diags.warn(f"{src}: default image is missing or has unsupported type {ext!r}")
        return None
    if ext == ".svg":
        reason = svg_unsafe_reason(src)
        if reason is not None:
            diags.error(f"{src}: default image SVG rejected ({reason})")
            return None
    media_dir.mkdir(parents=True, exist_ok=True)
    dims = probe_dimensions(src)
    width = dims[0] if dims else 0
    if _wants_thumbnail(ext, width, generate_thumbnails, thumbnail_width):
        if HAVE_PILLOW:
            name = f"gallery-default.{thumbnail_width}.webp"
            try:
                write_thumbnail(src, media_dir / name, thumbnail_width)
                return f"media/{name}"
            except Exception as exc:  # noqa: BLE001
                diags.warn(f"{src}: default image thumbnail failed ({exc}); shipping the original")
        else:
            _note_pillow_missing(diags, pillow_missing_logged)
    dest = media_dir / f"gallery-default{ext}"
    shutil.copyfile(src, dest)
    return f"media/gallery-default{ext}"
