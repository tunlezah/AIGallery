"""Image handling: copy item images into public/media, probe intrinsic
dimensions (stdlib parsers, so width/height work even without Pillow), and
optionally derive width-constrained WebP thumbnails via Pillow.
"""

from __future__ import annotations

import re
import shutil
import struct
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
        data = path.read_bytes() if path.suffix.lower() in (".jpg", ".jpeg") \
            else path.open("rb").read(4096)
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
) -> dict | None:
    """Copy `src` to public media, probe dimensions, maybe emit a thumbnail.

    Returns {"image", "thumb", "image_w", "image_h"} with public-relative
    paths (media/...), or None when the file type is unsupported.
    """
    ext = src.suffix.lower()
    if ext not in ALLOWED_EXTS:
        diags.warn(f"{src}: unsupported image type {ext!r}; falling back to placeholder")
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
    if (
        generate_thumbnails
        and ext in RASTER_EXTS
        and ext != ".gif"                 # animation would be lost
        and width > thumbnail_width
    ):
        if not HAVE_PILLOW:
            if not pillow_missing_logged[0]:
                diags.warn("Pillow is not installed; skipping thumbnail generation and using originals")
                pillow_missing_logged[0] = True
        else:
            try:
                with Image.open(src) as img:
                    ratio = thumbnail_width / img.width
                    new_size = (thumbnail_width, max(1, round(img.height * ratio)))
                    resized = img.convert("RGB").resize(new_size, Image.LANCZOS)
                    thumb_name = f"{dest.stem}.{thumbnail_width}.webp"
                    resized.save(dest_dir / thumb_name, "WEBP", quality=80, method=4)
                    thumb_rel = f"media/{rel_dir}/{thumb_name}"
            except OSError as exc:
                diags.warn(f"{src}: thumbnail generation failed ({exc}); using original")

    return {
        "image": public_rel,
        "thumb": thumb_rel,
        "image_w": width,
        "image_h": height,
    }
