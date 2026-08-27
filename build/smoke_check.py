"""Post-build smoke checks, run by CI after the generator.

Asserts: public/index.html + public/gallery.json exist, the JSON parses,
items is non-empty, every image/thumb referenced by the index exists on
disk, and no secret value leaked into public/ or dist/.

Usage: python build/smoke_check.py [public-dir] [dist-dir]
Secrets to scan for are read from the environment variable names given in
SECRET_ENV_VARS (never printed).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SECRET_ENV_VARS = ("GALLERY_TOPIC_TOKEN", "CONTENT_REPO_TOKEN")


def fail(msg: str) -> None:
    print(f"smoke check FAILED: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    public = Path(sys.argv[1] if len(sys.argv) > 1 else "public")
    dist = Path(sys.argv[2] if len(sys.argv) > 2 else "dist")

    index_html = public / "index.html"
    gallery_json = public / "gallery.json"
    if not index_html.is_file():
        fail(f"{index_html} does not exist")
    if not gallery_json.is_file():
        fail(f"{gallery_json} does not exist")

    try:
        index = json.loads(gallery_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{gallery_json} does not parse: {exc}")

    items = index.get("items")
    if not isinstance(items, list) or not items:
        fail("gallery.json items is empty — the gallery would render blank")

    missing = []
    for item in items:
        for key in ("image", "thumb"):
            rel = item.get(key)
            if rel and not (public / rel).is_file():
                missing.append(f"{item.get('id')}: {rel}")
    if missing:
        fail("referenced files missing from disk:\n  " + "\n  ".join(missing))

    secrets = [os.environ[name] for name in SECRET_ENV_VARS
               if os.environ.get(name) and len(os.environ[name]) >= 8]
    if secrets:
        scanned = 0
        for root in (public, dist):
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                blob = path.read_bytes()
                scanned += 1
                for secret in secrets:
                    if secret.encode("utf-8") in blob:
                        fail(f"a secret value appears in {path}")
        print(f"smoke check: scanned {scanned} output files for secret values — clean")

    print(f"smoke check OK: {len(items)} items, all referenced media present")


if __name__ == "__main__":
    main()
