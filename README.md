# AI Gallery

A browsable, searchable catalogue of AI tools and resources, generated as a
fully static site and hosted on GitLab Pages. Content lives in a **separate
Git repository**; projects on the same GitLab instance can additionally opt
in by adding a **GitLab topic** to their project.

Design constraints (enforced, not aspirational):

- **Zero runtime network calls.** No CDNs, fonts, analytics or external
  images — every byte comes from the Pages origin. The page works with the
  network disabled after first load.
- **No runtime build step.** Plain static files; the search index is inlined
  into the HTML at build time.
- **Path-safe.** Relative URLs everywhere, so the same `public/` works at
  `https://<ns>.gitlab.io/<project>/` and at a domain root.
- **Two build targets** from one code path: hosted (`public/`) and a
  standalone `dist/standalone/index.html` you can double-click and use over
  `file://` with no feature reduction.
- **Deterministic builds** — same content commit + same topic snapshot in,
  byte-identical output out.
- **Untrusted input stays inert.** Every body is rendered through an
  allow-list sanitiser, every URL is validated, SVGs are checked for active
  content, and third-party images are accepted only when their bytes are a
  raster format.

## Adding an item (content repo)

The content repo's top-level directories are **sections**; each directory one
level below a section is a **gallery item** if it contains `index.md`:

```
tooling/                      # section
├── _section.md               # optional: title, order, description, icon
├── icon.png                  # optional section icon (shown in the heading + sidebar)
└── ollama/                   # item
    ├── index.md               # required
    └── cover.png              # optional image
```

`index.md` is YAML frontmatter + a Markdown body:

```markdown
---
title: "Ollama"                      # REQUIRED
url: "https://ollama.com"            # optional; http(s) or relative only
image: "cover.png"                   # optional; must live inside this directory
tags: ["local", "inference", "cli"]  # optional; list or comma-separated string
summary: "Run LLMs locally."         # optional; <= 200 chars
order: 10                            # optional sort weight, lower first
added: 2026-03-14                    # optional ISO date
updated: 2026-08-01                  # optional ISO date
featured: false                      # optional; true shows a Featured badge
draft: false                         # true = excluded from the build
---
Free Markdown body. Shown on the card, clamped, with a More/Less control.
```

- The body is CommonMark plus tables and strikethrough. Raw HTML is escaped
  to inert text; headings are demoted below the card title.
- `featured: true` adds a **Featured** badge to the card, and `is:featured`
  in the search box lists only featured items.
- `url` must be http(s) or a relative path. Any other scheme, a
  protocol-relative URL, or a URL containing control characters (tab,
  newline, C0) fails the build: browsers strip those characters before
  parsing, so `java<TAB>script:` would otherwise become `javascript:`.
- `image` may be PNG, JPEG, WebP, GIF or SVG. SVG files are validated:
  scripts, event handlers, `<foreignObject>`, `url()`/`@import` in styles,
  DOCTYPE/ENTITY declarations or external references fail the build with the
  file named. Raster images wider than `thumbnail_width` get a WebP
  thumbnail; the tile shows the thumbnail.
- `_section.md` accepts `title`, `order`, `description` and `icon` (a
  relative image path; rendered next to the section heading and in the
  sidebar, capped at 256 px wide).

An item with no `image` (and a topic project with no avatar) gets the
gallery's default image — `default_image` in `gallery.config.toml`, which
ships as `src/assets/AIGallery.png`. At build time the default image is
resized like any other cover into `media/gallery-default.<width>.webp`; the
full-size source is not shipped, so an imageless tile costs a few kilobytes
rather than the whole source file. Every card image is rendered inside a
fixed 4:3 media box (`object-fit: cover`), so any image size or aspect ratio
displays at the same standard tile size. The header's **?** button opens a
help dialog explaining both contribution paths and the search syntax; its
repo link, topic name and manifest path are baked from the config at build
time (credentials in the repo URL, if any, are stripped first).

Validation is strict where it matters: a missing `title`, a malformed YAML
block, a disallowed URL, an image path that escapes the item directory, an
SVG with active content, or a duplicate slug within a section **fails the
build** with the file named. Softer problems (missing image file, unknown
keys, invalid dates) warn and degrade; set `strict = true` to escalate
warnings to errors.

## Opting in via the GitLab topic

Any project on the same GitLab instance can appear in the gallery's
**Subscribed** section by adding the topic **`AI-Gallery`** (configurable via
`topic_name`) under *Settings → General → Topics*. No registration, no MR.
Discovery is additive: curated content always wins — a topic project whose
URL or slug duplicates a curated item is dropped with a notice.

> **Scope it on any instance you do not fully control.** With the default
> configuration, *every* project on the instance that carries the topic gets
> a card, including its title, description, avatar and links. On a shared or
> public instance set `topic_allow_namespaces` (globs over
> `path_with_namespace`, case-insensitive), for example
> `["ml-group/*", "platform/ai-*"]`; projects outside the list are skipped
> with one summary notice. `topic_exclude` remains available as a deny-list.

What the gallery uses, and how to look good:

| Project field | Becomes | Tip |
|---|---|---|
| name | card title | keep it short |
| description | summary + card body | first ~200 chars are shown |
| avatar | card image | uploaded avatars are localised at build time; raster only |
| topics | tag chips | the subscription topic itself is removed |
| web_url | card link | |
| created / last activity | added / updated dates | |

Optionally, commit a manifest at `.ai-gallery/index.md` (configurable via
`topic_manifest_path`) on your default branch — same frontmatter + Markdown
format as a gallery item. Its fields override the derived metadata:

| Manifest field | Effect |
|---|---|
| `title`, `summary`, `tags` | replace the derived values |
| `url` | must stay on the same GitLab instance |
| `image` | a PNG/JPEG/WebP/GIF in the manifest's directory (or below it), fetched from your default branch and capped by `topic_avatar_max_kb`; beats the avatar. SVG is never accepted from projects. |
| `order`, `added`, `updated`, `featured` | as for a curated item |
| `draft: true` | removes the project from the gallery |
| body | becomes the card body (sanitised like any other) |

The topic feed is fetched by the **only** network step of the build
(`build/fetch_topic.py`) and **can never fail the pipeline**: timeouts, auth
errors, rate limits or malformed responses degrade to "no topic items" with a
notice. Because that failure is silent by design, the site footer states the
feed's build-time state: how many projects were discovered, whether the list
was truncated, whether some images or manifests could not be fetched, or that
the feed was unavailable. New topic projects appear on the next scheduled or
triggered build.

Safety properties of the fetch step:

- The API token is sent only to the origin a request was addressed to.
  Redirects are followed, but a redirect to another origin (for example an
  object-storage host) is made **without** the token.
- Downloaded avatars and manifest images are kept only when their bytes are
  PNG, JPEG, WebP or GIF; a declared `image/*` type that disagrees with the
  bytes is refused. SVG is never accepted from third-party projects.
- Manifest image paths may not use schemes, absolute paths or `..`.

## Configuration

Everything lives in `gallery.config.toml`. Every key can be overridden by an
environment variable named `GALLERY_<KEY>` upper-cased (env wins), e.g.
`GALLERY_CONTENT_REPO_URL`, `GALLERY_TOPIC_NAME`, `GALLERY_STRICT=true`.
List keys take comma-separated values from the environment.

Pointing at a different content repo or topic:

```toml
content_repo_url = "https://gitlab.example.com/my-group/my-content.git"
content_repo_ref = "main"        # branch, tag or 40-char commit SHA
content_subdir   = "."           # subdirectory of the repo to treat as root
topic_name       = "AI-Gallery"  # or your own topic
topic_api_base   = ""            # blank = derive from CI_SERVER_URL, then
                                 # from content_repo_url's host
topic_allow_namespaces = ["my-group/*"]   # scope the feed on shared instances
```

Do not put credentials in `content_repo_url` or its environment override:
the value is baked into the help dialog. The pipeline splices the deploy
token into the clone URL separately, and the built-in check strips any
userinfo before publishing, but the variable should hold the plain URL.

## Running locally

```sh
pip install -r build/requirements.txt

# Point at a local clone of the content repo (or the test fixtures):
git clone <content-repo> .content

# Offline build (skips the topic stage entirely — deterministic):
python build/generate.py --no-topics

# Full build incl. standalone target:
python build/fetch_topic.py          # optional; writes .topics/ (network)
python build/generate.py --standalone

python -m http.server -d public 8000  # http://localhost:8000/
python -m unittest discover -s build/tests
```

Both scripts resolve paths against the repository root, whatever the current
directory. Generator flags: `--content-dir`, `--topics-snapshot`, `--out`,
`--dist`, `--no-topics`, `--standalone`, `--single-file` (inline images
< 200 KB as data URIs for a one-file gallery). Fetch flags: `--config`,
`--out` (snapshot directory).

### Browser smoke test (optional, developer machine)

`build/tests/browser_smoke.js` builds the fixtures, serves the hosted target
under a Pages-style sub-path, and drives both targets in headless Chromium:
no console errors, tiles and images render, search/filters/sort/dialogs
work, and no link resolves to a `javascript:` URL. It needs Node and
Playwright, which the pipeline runner does not have, so it is not part of CI:

```sh
npm install -g playwright && npx playwright install chromium
NODE_PATH="$(npm root -g)" node build/tests/browser_smoke.js
```

## The standalone build

CI attaches `dist/standalone/` as an artifact. Download it, keep
`index.html` next to its `media/` folder, and double-click the HTML — the
full gallery (search, filters, More/Less, help, settings, all four themes) works
over `file://` with zero console errors. With `--single-file`, images under
200 KB (the tile-sized default image included) are inlined as data URIs, so
the HTML alone is enough unless an item ships a larger cover. This works
because the build:

- inlines the JSON index into the HTML (no runtime fetching anywhere),
- uses only classic scripts (ES modules are CORS-blocked on `file://`),
- keeps URL state in `location.hash` (the History API throws on `file://`),
- omits the CSP `<meta>` (`'self'` is unreliable against an opaque origin;
  build-time sanitisation is the actual XSS defence in both targets),
- guards all storage access with an in-memory fallback.

## CI, tokens and rebuilds

The single `pages` job (`.gitlab-ci.yml`) clones the content repo, runs the
topic fetch (`|| true` — it cannot block), builds both targets, smoke-checks
the output (index parses, items non-empty, referenced media and the default
image exist, the standalone index exists, **no token value appears in any
output**), and publishes `public/`.

**Private content repo:** create a deploy token or project access token on
the content repo with **`read_repository` scope only**, and set masked CI
variables `CONTENT_REPO_USER` / `CONTENT_REPO_TOKEN` on this repo. Public
content repos need no token.

**Topic feed auth:** unauthenticated fetch sees public projects. To include
internal/private projects, set a masked `GALLERY_TOPIC_TOKEN` with
**`read_api` scope only**. Do not use `CI_JOB_TOKEN` — it does not cover the
projects-list API. Tokens are never echoed, never written to output, and
never sent to an origin other than the API's.

**Rebuild on content change** (this repo cannot see content pushes):

1. *Pipeline trigger token* — on this repo: Settings → CI/CD → Pipeline
   trigger tokens → add one. In the **content repo's** `.gitlab-ci.yml`:

   ```yaml
   notify-gallery:
     stage: deploy
     rules: [{ if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH' }]
     script:
       - curl -sS -X POST --fail
         -F "token=$GALLERY_TRIGGER_TOKEN"
         -F "ref=main"
         "$CI_API_V4_URL/projects/<site-project-id>/trigger/pipeline"
   ```

   with `GALLERY_TRIGGER_TOKEN` stored as a masked variable on the content repo.

2. *Scheduled pipeline* — Settings → CI/CD → Pipeline schedules, e.g. nightly.
   This is also what picks up newly topic-tagged projects.

## Dependencies

`build/requirements.in` lists exact versions of every direct and transitive
dependency. `build/requirements.txt` is **generated** from it by
`build/pin_requirements.py` and carries sha256 hashes for every file PyPI
publishes for those versions, so `pip install -r build/requirements.txt`
runs in hash-checking mode and refuses any file that does not match. To bump
a version, edit `requirements.in`, run the script on a machine that can
reach pypi.org, and commit both files.

On a disconnected GitLab instance, point pip at a mirror that serves the
unmodified PyPI files (a caching proxy such as Nexus or Artifactory does);
re-packaged or locally rebuilt wheels will not match the hashes and are
refused, which is the point. The base image `python:3.12-slim` and the
`git`/`ca-certificates` apt packages are the remaining unpinned inputs; pin
the image by digest (`python:3.12-slim@sha256:...`) if your registry mirror
preserves digests.

## Repository layout

```
build/      generator (offline) + fetch_topic.py (the one network step) + tests
            requirements.in -> pin_requirements.py -> requirements.txt (hashed)
            tests/browser_smoke.js: optional Playwright check for developers
src/        front-end source: index.html template, app.css, app.js, theme-init.js
public/     hosted build output (gitignored)
dist/       standalone build output (gitignored)
.content/   content repo clone, made by CI (gitignored)
.topics/    topic snapshot + localised avatars/images, made by CI (gitignored)
```

See `DECISIONS.md` for every judgement call, the measured tile-grid column
counts, and the per-theme contrast ratios.
