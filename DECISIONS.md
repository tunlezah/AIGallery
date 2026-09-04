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
- **Sort semantics:** `order` = (order, folded title, slug); `title` = folded
  title; `added`/`updated` = newest first with undated items last — date
  sorts are only useful newest-first in a gallery. Within one date the
  tie-break is title A→Z (the date key is negated rather than the whole sort
  reversed), so the baked order and the `app.js` comparator agree and
  switching sort modes in the settings never reshuffles same-date items.
- **`generated_from.commit` is only reported when the content dir is itself
  a clone root** (`.git` present) — otherwise `git` would resolve upward to
  an unrelated enclosing repo and break cross-commit determinism.
- **The inline JSON block escapes every `<` as `<`**, which covers both
  `</script` and `<!--` in one rule and keeps the payload valid JSON. It
  carries **only what `app.js` reads** (id, section, source, title, tags,
  search text, order, dates, featured): bodies, summaries, image paths and
  links are already baked into the tiles, and `gallery.json` keeps the full
  record. On large galleries this is the difference between shipping one
  copy of every body and two.
- **Item with no image:** `image`/`thumb` are `null` in the index; the baked
  HTML references the default image, which the build derives into
  `media/gallery-default.<thumbnail_width>.webp` through the same resize
  path as item covers (a raster narrower than the thumbnail width, or an
  SVG, is copied as `media/gallery-default.<ext>`). The full-size source is
  excluded from the `src/` mirror: every imageless tile loads this file, so
  its weight matters more than any other asset's (1.5 MB → ~15 KB for the
  shipped PNG). One file under `media/` also keeps the standalone target's
  single sibling folder. A `default_image` that is missing or escapes `src/`
  warns and falls back to `assets/placeholder.svg`, which also stays as the
  JS-side last resort; an unsafe SVG default is an error. With
  `--single-file` the default image is inlined like any other small image,
  and `runtime.placeholder` becomes a data URI too, so the one file really
  is one file.
- **URL validation refuses control characters and detects the scheme with
  `urlsplit`.** The WHATWG URL parser strips tab/newline anywhere and leading
  C0 controls before it looks at the scheme, so a regex over the raw string
  saw `java\tscript:` as relative while the browser saw `javascript:`.
  Control characters have no place in a gallery link, so they are a hard
  error; `\\host` is refused like `//host` because browsers treat the
  backslash as a slash in that position. The hosted CSP and the
  `noopener` targets already blocked execution, but the documented
  guarantee has to hold on its own.
- **SVGs are validated, not sanitised.** A served SVG is inert inside `<img>`
  but runs scripts on the site's origin when opened directly, and `url()`
  or `href` references make network requests. Curated content is reviewed,
  so an SVG with a script, event handler, `<foreignObject>`, DOCTYPE/ENTITY
  declaration, `url()`/`@import` in styles or an external reference fails
  the build with the file named (a validator cannot silently emit something
  the author did not write). Third-party sources never get SVG at all: the
  fetch step recognises only raster magic bytes, and the merge refuses a
  `.svg` path in the snapshot defensively.
- **The topic token is scoped to the origin it was addressed to.** urllib's
  redirect handler copies every header onto the redirected request, so an
  avatar URL that GitLab redirects to object storage would have carried
  `PRIVATE-TOKEN` to that host. A custom handler follows the redirect
  without the token when the origin (scheme, host, port) changes and refuses
  non-web schemes; same-origin redirects keep it.
- **Downloaded images are trusted by bytes, not by header.** GitLab's raw
  file endpoint often labels files `text/plain`, so the content-type cannot
  be the gate; magic bytes decide (PNG/JPEG/WebP/GIF only), and a declared
  `image/*` type that disagrees with the bytes is refused as suspicious.
- **Manifest `image` is fetched in the network step**, never resolved at
  merge time: it must be a raster file in the manifest's own directory (or
  below), capped by `topic_avatar_max_kb`, and it beats the avatar. The
  other manifest fields (`order`, `added`, `updated`, `featured`, `draft`)
  are applied at merge time with the same coercion rules as `index.md`;
  `draft: true` removes the project, which is the manifest owner's way out
  short of untagging.
- **`topic_allow_namespaces` is an allow-list on `path_with_namespace`
  globs** (case-insensitive; GitLab paths are case-insensitive in practice)
  applied client-side in the fetch step, before the project cap, so excluded
  projects never consume budget or slots. Skips are summarised in one notice
  rather than one per project, and an empty list prints a reminder that the
  whole instance is being listed. The group-projects endpoint would scope
  the query server-side but relies on a `topic` parameter that cannot be
  verified against a disconnected instance from here.
- **The footer states the topic feed's build-time state.** The fetch step's
  "never fail" contract means a feed broken for weeks is indistinguishable
  from a feed with no projects unless something says so; the footer line
  (count, truncated, partial, or unavailable) is baked from the snapshot
  metadata and styled as a warning when degraded.
- **Snapshots record `partial`** (some avatar/manifest/image request failed,
  404s excluded) alongside `truncated` (project list cut off), and the merge
  surfaces both as notices and in `generated_from.topic`. Without it two
  builds of the same content could differ with network speed and nobody
  would know why.
- **`fetch_topic.py` resolves paths against the repository root** like
  `generate.py` (`--config`, `--out` override), so the fetch step run from
  another directory no longer silently writes a snapshot the generator never
  reads.
- **The help dialog's repo link strips userinfo** before baking; the smoke
  check only knows the token values it is told about, so a token pasted into
  `GALLERY_CONTENT_REPO_URL` would otherwise have been published verbatim.
- **The help dialog's config-dependent facts are rendered in Python
  (`help_slots`), not hand-written in the template.** Whether an internal
  project can appear, whether archived projects are skipped and which
  namespaces are accepted all follow from `topic_visibility`,
  `topic_include_archived` and `topic_allow_namespaces`; a sentence in the
  template about them would be true for exactly one configuration. The
  allow-list is published (it tells a contributor whether they qualify); the
  `topic_exclude` deny-list is not (it is the maintainers' call, and naming
  excluded projects serves nobody).
- **Template conditionals are HTML comments** (`<!--if:flag-->` …
  `<!--/if:flag-->`, negatable, nestable, resolved by
  `apply_conditional_blocks`). The alternative — attribute slots that
  inject ` hidden` — would have shipped the topic help to galleries that
  have topics switched off, merely invisible. An unknown flag name raises,
  so a typo cannot silently drop or duplicate a block. The `topics` flag
  follows `topics_enabled` in the config, not the `--no-topics` build
  switch: a developer's offline build still documents the deployed gallery.
- **The help names the GitLab instance from the topic snapshot first**, then
  via the same `resolve_api_base` the fetch step uses (moved into
  `config.py` so both read one implementation). The snapshot records what
  was actually queried, which is the only truth a contributor needs; when
  nothing resolves the text falls back to "this GitLab instance" rather
  than guessing.
- **Tables and strikethrough are enabled in the Markdown parser.** The
  sanitiser allow-list admitted `table`/`del` from the start while the
  CommonMark preset never produced them, so a table in an item body came
  out as a paragraph of pipes. Alignment styles are stripped with every
  other `style` attribute (the CSP has no `unsafe-inline`).
- **`featured` means a badge and a filter**, not a sort change: `order` is
  the curator's explicit ordering and stays authoritative; `is:featured` in
  the search box lists featured items, and the article carries
  `data-featured` for styling.
- **Section icons render in the heading and the sidebar** as decorative
  images (`alt=""`, the title is adjacent), thumbnailed to at most 256 px
  because an icon is never displayed larger than a line of text.
- **Dependencies are pinned by hash.** `requirements.in` holds the exact
  versions (transitive `mdurl` included); `pin_requirements.py` expands them
  into `requirements.txt` with every published file's sha256, so pip's
  hash-checking mode works on any platform while refusing tampered or
  re-packaged files. The base image and apt packages stay tag-pinned; a
  digest pin is documented as the operator's call because disconnected
  registries do not always preserve digests.
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
- **The help dialog shares the settings dialog's pattern** (`.app-dialog`
  base class, one `wireDialog()` helper for both): same toggle-under-modal
  behaviour, backdrop close and focus return. Its content-repo link, topic
  name and manifest path are `{{...}}` slots substituted from config at
  build time, so reconfiguring the gallery cannot leave the help text stale.
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
- **`[hidden] { display: none !important }` is part of the base styles.**
  The filter code hides tiles by setting the `hidden` attribute, but the
  tile's own `display: flex` rule (author origin) beats the UA stylesheet's
  `[hidden]` rule, so filtered-out tiles inside a visible section stayed on
  screen while the result count said otherwise; only whole sections
  disappeared. The browser smoke test now asserts that a tag filter leaves
  exactly the matching tiles visible and that every `[hidden]` tile computes
  to `display: none`.
- **The explicit Light theme sets `color-scheme: light`.** The page-level
  `<meta name="color-scheme" content="light dark">` lets a dark OS pick dark
  form controls and scrollbars; Dark and GeoCities already pinned theirs, so
  Light was the one theme whose radios and dialog chrome could come out
  dark on a light surface. System deliberately sets nothing and follows
  the OS.
- **Search tokens are documented in the help dialog** (`tag:`, `section:`,
  `source:`, `is:featured`, `/`, Escape) because a filter nobody can
  discover is a filter nobody uses.
- **The help dialog is a tabbed page, not a scroll of paragraphs.** The
  question a visitor arrives with is "which way in?", so the intro answers
  it in two sentences and the tabs (Merge request / GitLab topic / Search &
  filters) let them read one path in full. Tabs follow the WAI-ARIA pattern
  with automatic activation and roving tabindex (Left/Right/Home/End, wrap
  around), inactive panels carry `hidden`, and the head (title, intro,
  tabs) stays put while the panel scrolls (`display:flex` scoped to
  `.help-dialog[open]` so the closed dialog keeps the UA's `display:none`).
  The dialog opens with focus on the selected tab rather than the close
  button. Required/optional is shown twice on purpose: as a badge next to
  every field in the tables (scan) and as an inline comment in the
  templates (copy-paste).
- **`#help` / `#help=<tab>` is read from the hash but never written to it.**
  The footer link and shared URLs open the dialog on a tab; the filter
  state then normalises the hash on its next write, exactly as any unknown
  key was already treated, so the History API stays untouched (it throws
  on `file://`) and the filter model gains no dialog state.
- **Selected-tab colour is `--link`, not `--accent`.** GeoCities' magenta
  accent measures 3.1:1 as text on the pale-yellow surface; its blue link
  colour measures 9.1:1. Light and Dark use the same value for both tokens,
  so nothing changes there.
- **The footer gained a plain "How to get a project listed" link.** A
  gallery that solicits contributions should say so where a visitor
  finishes scrolling, not only behind a `?` icon; on GeoCities the default
  link blues vanish against the navy starfield, so footer links are cyan
  there (16:1).
- **The browser smoke test is a developer script, not a pipeline job.** It
  needs Node and a Chromium download, which the disconnected runner cannot
  make; `build/tests/browser_smoke.js` builds the fixtures and asserts the
  front-end guarantees (no console errors, filters, sort, dialogs, images,
  no `javascript:` links) on both targets, so the "verified in the browser
  pass" claims are repeatable on any developer machine.

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
