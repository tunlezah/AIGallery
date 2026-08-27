# Decisions

Unspecified details resolved during the build, with a one-line rationale each.
Measured values (columns, contrast) are at the end.

## Generator

- **Python version:** code targets 3.11+ (`tomllib`, `dataclasses`); CI pins
  `python:3.12-slim` per spec. No 3.12-only syntax, so local dev on 3.11 works.
- **Config loader lives in `build/config.py`:** keeps `generate.py`
  orchestration-only and the loader independently unit-testable.
- **Env override typing:** env vars are coerced to the TOML field's type
  (bool: `1/true/yes/on`; lists: comma-separated). A malformed value is fatal
  for core keys but a notice (keep default) for `topic_*` keys, honouring
  "never fail fast on anything under the topic block".
- **`src/index.html` is a template, not copied verbatim:** the inlined JSON
  block, the baked tile HTML (required for the no-JS rendering guarantee) and
  the hosted/standalone differences (CSP, inlined JS/CSS) vary per build, so
  the generator substitutes `{{PLACEHOLDER}}` slots. All other `src/` files
  are copied verbatim as specified.
- **Tiles are baked into the served HTML:** the spec requires tiles to render
  fully with JS disabled, so the generator emits section + tile markup
  statically; `app.js` only enhances (clamp, More, search, filters,
  settings). The inline JSON drives search/filter/sort, not rendering.
- **Drafts are validated before being excluded:** a malformed draft still
  errors — silent rot in drafts would surface only when someone flips
  `draft: false`.
- **`summary` > 200 chars:** warn and truncate (ellipsis) rather than hard
  error — length is a soft budget, not a safety property.
- **h1/h2 in item bodies are demoted to h3** (h6 → h5): card bodies sit under
  the page's h1 → h2 (section) → h3 (tile title) hierarchy, and h1/h2/h6 are
  not in the sanitiser allow-list anyway.
- **Raw HTML in Markdown is escaped to inert text** (parser `html=False`),
  matching CommonMark's defined behaviour; the sanitiser additionally strips
  live markup for any HTML that reaches it by other paths (topic manifests).
- **`rel`/`target` are added post-sanitise** by rewriting complete `<a>` open
  tags (quoted-attribute-aware regex over sanitiser output, which is
  canonically serialised) — nh3's `link_rel` would hit relative links too.
- **Intrinsic image dimensions are probed with stdlib header parsers**
  (PNG/GIF/JPEG/WebP/SVG), so `width`/`height` are recorded even without
  Pillow; Pillow is only needed for thumbnails. Unknown dimensions warn and
  fall back to 800×600 (4:3, the tile's media box ratio).
- **Thumbnails:** only when the source is raster, non-GIF (animation would be
  lost) and wider than `thumbnail_width`; otherwise `thumb` = the original.
  WebP quality 80 / method 4, deterministic for a fixed Pillow version
  (pinned in requirements.txt).
- **Sort semantics:** `order` = (order, folded title); `title` = folded
  title; `added`/`updated` = newest first with undated items last — date
  sorts are only useful newest-first in a gallery.
- **`generated_from.commit` is only reported when the content dir is itself
  a clone root** (`.git` present) — otherwise `git` would resolve upward to
  an unrelated enclosing repo and break cross-commit determinism.
- **The inline JSON block escapes every `<` as `<`**, which covers both
  `</script` and `<!--` in one rule and keeps the payload valid JSON.
- **Item with no image:** `image`/`thumb` are `null` in the index; the baked
  HTML references the placeholder directly (hosted: `assets/placeholder.svg`;
  standalone: a data URI, so `media/` stays the only sibling the standalone
  file needs).
- **`cards_per_page > 0` is implemented as incremental reveal** (first N tiles
  shown, IntersectionObserver sentinel reveals the rest chunk-by-chunk) rather
  than discrete pages: real pagination would fight hash-based filter state,
  and the spec's Scale section asks for exactly this batching behaviour. Any
  active filter reveals everything first so counts stay truthful. The same
  mechanism engages automatically above 800 items; >3000 items logs the
  recommended-pagination build warning.
- **Topic snapshot stores the optional manifest inline** (string field in
  `topics.json`) instead of as side files: one snapshot file, trivially
  deterministic, and the fetch step caps manifests at 256 KB.
- **Defensive re-checks in the offline merge:** the merge re-drops archived
  and malformed records even though the fetch step already filters them —
  the snapshot is an interface, not a trusted artifact.
- **Topic title collisions** use `name_with_namespace` when available, per
  the mapping table; if two records still collide, later ones are skipped
  with a notice.
- **fetch_topic exits 0 even on config-loader failure** (wrapped at top
  level) and writes an empty snapshot, so a broken TOML cannot make the
  *topic step* the thing that fails the pipeline (the generator will still
  fail the build properly).

## Front end

- **Settings-button toggle under a modal dialog:** with `showModal()` the
  backdrop covers the header, so a click aimed at the settings button lands
  on the backdrop — which closes the panel. The user-visible behaviour
  ("click the gear again to close") therefore works; keyboard users have
  Escape and the close button. Verified in the browser pass.
- **Chip overflow (`+N`)** is baked at build time (first 3 chips + indicator
  with the full list in `title` and visually-hidden text); all tags remain in
  the search index and the rail.
- **ResizeObserver observes `#main`** (one shared observer) and re-checks
  clamp overflow in rAF-batched chunks of 200 — cheap, and O(visible tiles)
  work is skipped for hidden tiles.
- **Escape semantics:** Escape clears the search only while the search field
  is focused (global Escape would fight the dialog's native Escape-to-close).
- **GeoCities starfield is pure CSS radial-gradients** (tiled 140×140
  background), not an SVG data URI — same self-contained effect, no `xmlns`
  string in the stylesheet.
- **The "hit counter" is a pure restyle** of the existing live result count
  (LED green-on-black, letter-spaced) — changing its text per theme would be
  a behavioural difference, which the GeoCities theme must not introduce.
- **`xmlns="http://www.w3.org/2000/svg"` appears in the two standalone SVG
  assets** (favicon, placeholder): it is the XML namespace identifier
  required for an SVG *document* to parse, not a network request. Inline SVGs
  in the HTML omit it. These are the only non-content `http` strings in the
  built output.
- **Non-modal `<dialog>` fallback** (no `showModal`): open attribute is set
  manually and close-button/trigger-toggle still work; Escape/backdrop are
  native-modal features. No current browser needs this path.

## Measured tile-grid values

Final value: **`--tile-min: 13.25rem`** (212 px) with `--tile-gap: 1rem`;
density presets: comfortable 15rem / compact 11.5rem; below 900 px (rail
collapsed) a `--tile-floor: 15rem` takes over so phones/tablets don't get
cramped columns. Measured in Chromium at default density, fixture content:

| Viewport | Spec target | Measured |
|---|---|---|
| 1600 px | 5 (6 ok) | **5** |
| 1440 px | 4–5 | **5** |
| 1200 px | 4 | **4** |
| 1024 px | 3 | **3** |
| 768 px | 2 | **2** |
| 480 px | 1 | **1** |
| 360 px | 1 | **1** |

Tiles in a row measure equal heights (flex column + `align-items: stretch`);
expanding a tile grows its own row only and the column count is unchanged
(verified 5 → 5 during expansion at 1440 px).

## Measured contrast ratios (WCAG 2.2 AA)

Text pairs need ≥ 4.5:1; the focus indicator (non-text) needs ≥ 3:1.

| Pair | Light | Dark | GeoCities |
|---|---|---|---|
| body text / tile surface | 16.70 | 13.69 | 20.43 |
| body text / page bg | 15.58 | 15.31 | 20.04 (white on starfield navy) |
| muted text / tile surface | 6.36 | 7.28 | 10.47 |
| link / tile surface | 6.39 | 7.83 | 9.14 (`#0000EE`), visited 10.71 (`#551A8B`) |
| chip text / chip bg | 9.76 | 9.46 | 11.54 |
| topic badge text / badge bg | 6.88 | 6.93 | 6.70 (black on `#ff00ff`) |
| section heading / page bg | 15.58 | 15.31 | 18.66 (`#ffff00` on `#000033`) |
| hit counter | n/a | n/a | 15.30 (`#00ff00` on black) |
| focus ring vs surface (≥3:1) | 6.39 | 7.83 | 3.89 on tiles / 5.01 on page (`#ff0000` dotted) |

GeoCities compromise noted per spec: the period-authentic focus treatment
(yellow dotted everywhere) fails 3:1 on the pale-yellow tiles, so the focus
ring is **red** (3.89:1 on tiles, 5.01:1 on the starfield) and chunky dotted
for flavour. axe-core reports zero violations in all four themes.
