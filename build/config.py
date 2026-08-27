"""Configuration loading for the AI Gallery generator.

Values come from gallery.config.toml, with every key overridable by an
environment variable of the same name upper-cased and prefixed GALLERY_
(e.g. GALLERY_CONTENT_REPO_URL). Environment wins over file.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    site_title: str = "AI Gallery"
    site_description: str = "A curated gallery of AI tools and resources."
    content_repo_url: str = ""
    content_repo_ref: str = "main"
    content_subdir: str = "."
    default_image: str = "assets/placeholder.svg"
    default_sort: str = "order"
    cards_per_page: int = 0
    generate_thumbnails: bool = True
    thumbnail_width: int = 640
    strict: bool = False
    # --- Topic subscription (optional; never fatal) ---
    topics_enabled: bool = True
    topic_name: str = "AI-Gallery"
    topic_api_base: str = ""
    topic_section_slug: str = "subscribed"
    topic_section_title: str = "Subscribed"
    topic_section_order: int = 900
    topic_section_description: str = (
        "Projects on this GitLab instance tagged with the gallery topic."
    )
    topic_max_projects: int = 200
    topic_include_archived: bool = False
    topic_visibility: str = ""
    topic_fetch_avatars: bool = True
    topic_avatar_max_kb: int = 512
    topic_manifest_path: str = ".ai-gallery/index.md"
    topic_exclude: list[str] = field(default_factory=list)
    topic_timeout_seconds: int = 20
    topic_budget_seconds: int = 120


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# Keys in the topic block: misconfiguration here must never fail the build.
_TOPIC_KEYS = {f.name for f in fields(Config) if f.name.startswith("topic")}


def _coerce(name: str, raw: str, target_type: type, problems: list[str]):
    """Coerce an env-var string to the type of the config field."""
    if target_type is bool:
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        problems.append(f"{name}: expected a boolean, got {raw!r}")
        return None
    if target_type is int:
        try:
            return int(raw.strip())
        except ValueError:
            problems.append(f"{name}: expected an integer, got {raw!r}")
            return None
    if target_type is list:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


def load_config(
    path: str | Path = "gallery.config.toml",
    *,
    require_content_repo: bool = True,
    environ: dict[str, str] | None = None,
) -> tuple[Config, list[str]]:
    """Load config from TOML + environment.

    Returns (config, notices). Raises SystemExit on fatal problems outside
    the topic block; topic-block problems are downgraded to notices and the
    field keeps its default.
    """
    env = os.environ if environ is None else environ
    cfg = Config()
    notices: list[str] = []
    fatal: list[str] = []
    field_types = {f.name: (bool if f.type == "bool" else int if f.type == "int" else list if f.type.startswith("list") else str) for f in fields(Config)}

    p = Path(path)
    if p.exists():
        try:
            with open(p, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"error: cannot parse {p}: {exc}")
        for key, value in sorted(data.items()):
            if key not in field_types:
                notices.append(f"config: unknown key {key!r} ignored")
                continue
            want = field_types[key]
            ok = isinstance(value, bool) if want is bool else (
                isinstance(value, int) and not isinstance(value, bool) if want is int
                else isinstance(value, list) if want is list
                else isinstance(value, str)
            )
            if not ok:
                msg = f"config: {key} has wrong type {type(value).__name__}"
                if key in _TOPIC_KEYS:
                    notices.append(f"{msg}; using default")
                    continue
                fatal.append(msg)
                continue
            if want is list and not all(isinstance(v, str) for v in value):
                notices.append(f"config: {key} must be a list of strings; using default")
                continue
            setattr(cfg, key, value)
    else:
        notices.append(f"config: {p} not found; using defaults + environment")

    for key, want in sorted(field_types.items()):
        env_name = "GALLERY_" + key.upper()
        if env_name in env:
            problems: list[str] = []
            value = _coerce(env_name, env[env_name], want, problems)
            for msg in problems:
                if key in _TOPIC_KEYS:
                    notices.append(f"config: {msg}; using default")
                else:
                    fatal.append(f"config: {msg}")
            if value is not None or (want is bool and not problems):
                if value is not None:
                    setattr(cfg, key, value)

    if cfg.default_sort not in ("order", "title", "added", "updated"):
        fatal.append(
            f"config: default_sort must be one of order|title|added|updated, got {cfg.default_sort!r}"
        )
    if cfg.topic_visibility not in ("", "public", "internal", "private"):
        notices.append(
            f"config: topic_visibility {cfg.topic_visibility!r} is not one of "
            "''|public|internal|private; ignoring it"
        )
        cfg.topic_visibility = ""

    if require_content_repo and not cfg.content_repo_url:
        fatal.append(
            "config: content_repo_url is not set. Set it in gallery.config.toml "
            "or via the GALLERY_CONTENT_REPO_URL environment variable."
        )

    if fatal:
        for msg in fatal:
            print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(1)

    return cfg, notices
