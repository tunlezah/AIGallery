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

## Adding an item (content repo)

The content repo's top-level directories are **sections**; each directory one
level below a section is a **gallery item** if it contains `index.md`:

```
tooling/                      # section
├── _section.md               # optional: title, order, description, icon
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
featured: false                      # optional
draft: false                         # true = excluded from the build
---
Free Markdown body. Shown on the card, clamped, with a More/Less control.
```

An item with no `image` (and a topic project with no avatar) gets the
gallery's default image — `default_image` in `gallery.config.toml`, which
ships as `src/assets/AIGallery.png`. Every card image is rendered inside a
fixed 4:3 media box (`object-fit: cover`), so any image size or aspect ratio
displays at the same standard tile size. The header's **?** button opens a
help dialog explaining both contribution paths (content repo MR, or the
GitLab topic); its repo link, topic name and manifest path are baked from
the config at build time.

Validation is strict where it matters: a missing `title`, a malformed YAML
block, a `javascript:` URL, an image path that escapes the item directory,
or a duplicate slug within a section **fails the build** with the file named.
Softer problems (missing image file, unknown keys, invalid dates) warn and
degrade; set `strict = true` to escalate warnings to errors.

## Opting in via the GitLab topic

Any project on the same GitLab instance can appear in the gallery's
**Subscribed** section by adding the topic **`AI-Gallery`** (configurable via
`topic_name`) under *Settings → General → Topics*. No registration, no MR.
Discovery is additive: curated content always wins — a topic project whose
URL or slug duplicates a curated item is dropped with a notice.

What the gallery uses, and how to look good:

| Project field | Becomes | Tip |
|---|---|---|
| name | card title | keep it short |
| description | summary + card body | first ~200 chars are shown |
| avatar | card image | uploaded avatars are localised at build time |
| topics | tag chips | the subscription topic itself is removed |
| web_url | card link | |
| created / last activity | added / updated dates | |

Optionally, commit a manifest at `.ai-gallery/index.md` (configurable via
`topic_manifest_path`) on your default branch — same frontmatter + Markdown
format as a gallery item. Its fields override the derived metadata (`url`
must stay on the same GitLab instance), and its body becomes the card body.

The topic feed is fetched by the **only** network step of the build
(`build/fetch_topic.py`) and **can never fail the pipeline**: timeouts, auth
errors, rate limits or malformed responses degrade to "no topic items" with a
notice. New topic projects appear on the next scheduled or triggered build.

## Configuration

Everything lives in `gallery.config.toml`. Every key can be overridden by an
environment variable named `GALLERY_<KEY>` upper-cased (env wins), e.g.
`GALLERY_CONTENT_REPO_URL`, `GALLERY_TOPIC_NAME`, `GALLERY_STRICT=true`.

Pointing at a different content repo or topic:

```toml
content_repo_url = "https://gitlab.example.com/my-group/my-content.git"
content_repo_ref = "main"        # branch, tag or 40-char commit SHA
content_subdir   = "."           # subdirectory of the repo to treat as root
topic_name       = "AI-Gallery"  # or your own topic
topic_api_base   = ""            # blank = derive from CI_SERVER_URL, then
                                 # from content_repo_url's host
```

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

Useful flags: `--content-dir`, `--topics-snapshot`, `--out`, `--dist`,
`--no-topics`, `--standalone`, `--single-file` (inline images < 200 KB as
data URIs for a one-file gallery).

## The standalone build

CI attaches `dist/standalone/` as an artifact. Download it, keep
`index.html` next to its `media/` folder, and double-click the HTML — the
full gallery (search, filters, More/Less, help, settings, all four themes) works
over `file://` with zero console errors. This works because the build:

- inlines the JSON index into the HTML (no runtime fetching anywhere),
- uses only classic scripts (ES modules are CORS-blocked on `file://`),
- keeps URL state in `location.hash` (the History API throws on `file://`),
- omits the CSP `<meta>` (`'self'` is unreliable against an opaque origin;
  build-time sanitisation is the actual XSS defence in both targets),
- guards all storage access with an in-memory fallback.

## CI, tokens and rebuilds

The single `pages` job (`.gitlab-ci.yml`) clones the content repo, runs the
topic fetch (`|| true` — it cannot block), builds both targets, smoke-checks
the output (index parses, items non-empty, referenced media exist, **no
token value appears in any output**), and publishes `public/`.

**Private content repo:** create a deploy token or project access token on
the content repo with **`read_repository` scope only**, and set masked CI
variables `CONTENT_REPO_USER` / `CONTENT_REPO_TOKEN` on this repo. Public
content repos need no token.

**Topic feed auth:** unauthenticated fetch sees public projects. To include
internal/private projects, set a masked `GALLERY_TOPIC_TOKEN` with
**`read_api` scope only**. Do not use `CI_JOB_TOKEN` — it does not cover the
projects-list API. Tokens are never echoed and never written to output.

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

## Repository layout

```
build/      generator (offline) + fetch_topic.py (the one network step) + tests
src/        front-end source: index.html template, app.css, app.js, theme-init.js
public/     hosted build output (gitignored)
dist/       standalone build output (gitignored)
.content/   content repo clone, made by CI (gitignored)
.topics/    topic snapshot + localised avatars, made by CI (gitignored)
```

See `DECISIONS.md` for every judgement call, the measured tile-grid column
counts, and the per-theme contrast ratios.
