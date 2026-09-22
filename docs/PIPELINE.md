# How the pipeline works, and why

Reference for the brochure pipeline: what each phase does, what it costs, which LLM calls
exist and which were deliberately removed, and what happens when something fails. Change
history is in [IMPROVEMENTS.md](IMPROVEMENTS.md); this file is the current picture.

## The shape of a run

`POST /api/recommend` starts a background job; the browser polls `GET /api/jobs/{id}` for
progress. Steps run in order through a small workflow runner
([`app/pipeline/workflow.py`](../ai-services-python/app/pipeline/workflow.py)) that handles
per-step retries and rollback; the steps themselves are in
[`app/pipeline/brochure.py`](../ai-services-python/app/pipeline/brochure.py).

| # | Step | LLM calls | Typical time | If it fails |
|---|---|---|---|---|
| 0 | **ingest** (only when a PDF is uploaded) | none (embeddings) | ~1 min per 100 blocks, once | Demo catalog, reported |
| 1 | **profile** – segment + budget tier | 0 (rules; opt-in LLM) | instant | n/a |
| 2 | **recommend** – retrieve → extract → rank | 2 | ~20 s | Demo catalog and/or rule-based scoring, reported |
| 3 | **plan** – outline the sections | 0 (opt-in) | instant | Default outline, reported |
| 4 | **copy** – write, review, revise | 2+ per draft | ~18 s | Claim-free generic copy, reported |
| 5 | **html** – Jinja2 template | none | ~30 ms | Job fails |
| 6 | **pdf** – headless Chromium | none | ~6 s | Job fails (no mock PDF) |

Measured on a generated 120-page catalog with `gemini-3.1-flash-lite`. **A run is ~5,400
tokens** (~4.0k in, ~1.4k out), so token spend is negligible: what a run costs is round-trip
latency, i.e. the *number* of LLM calls, not their size.

## Phase by phase

### Ingest (one-off per catalog)
Each page is split into **product-sized blocks** rather than indexed whole
([`app/rag/layout.py`](../ai-services-python/app/rag/layout.py)): words are read with their
coordinates, split at vertical gutters (the whitespace columns running down a page), then at
vertical gaps within each column. Sideways text (rotated nav tabs) is dropped, and lines
repeated across ≥40% of pages (nav bars, running headers) are stripped before embedding.

Each block is embedded separately, and the largest image **inside that block** is cropped out
of the rendered page and stored as that product's photo. Cropping the render, rather than
pulling the embedded image stream, both sidesteps encodings a browser cannot display and
guarantees the picture belongs to the product beside it.

Why: measured on a real 133-page catalogue, 71% of pages hold several products, and pages
carry ~19 images each. One chunk per page meant four products shared one index entry and one
hero image, while stream-order text merged adjacent columns
(`SOFTLOCK + MESH: 100% POLYESTERAvailable until 202880000274`). That catalogue yields 644
blocks from 122 pages (median 6 per page), and 207 blocks get their own product photo.

Pages pdfplumber cannot read fall back to one chunk per page, the previous behaviour. The catalog is remembered
(`storage/catalog.json`) and reused by later runs, including after a restart, until you upload
another or call `DELETE /api/rag/catalog`.

Embedding is the one thing that scales with catalog size: **one request per block**, and
free-tier quota is 100 requests/minute, so ingest is rate-limited (batches of 50, waiting and
retrying when the provider says to). The 133-page catalogue above is 644 blocks, i.e. ~7
minutes of ingest — once. Scanned/image-only PDFs index nothing: there is no OCR.

### 1. Profile
Turns age, income, family size, hobbies and location into a segment (Adventure / Executive /
Family / Standard) and a budget tier. **Rules by default**
([`app/ai/profile.py`](../ai-services-python/app/ai/profile.py)): outdoor hobbies → Adventure,
family size ≥ 3 → Family, and so on.

> **Why no LLM here:** the rules use the same handful of inputs, are deterministic, instant,
> and never invent a segment. The LLM version cost ~5–13 s and a call for the same answer. Set
> `USE_LLM_PROFILE=true` to compare.

### 2. Recommend
1. **Retrieve** – one vector search *per hobby* ("Gear and equipment for camping"), merged by
   block, best 6 kept. Per-hobby rather than one blended query, because a single query lets one
   interest dominate, and because catalog text describes products, not customers — putting
   income or family size in the query only adds noise.
2. **Extract** (LLM) – reads the matched pages and returns structured products. The prompt
   forbids inventing: unstated price → `0` (rendered "Price on request"), unstated specs
   omitted, pages without products skipped. This is the largest prompt (~1k tokens), but it
   scales with the 6 matched pages, not with catalog size.
   Results are **cached per chunk** against the catalog's content hash
   ([`app/rag/extraction_cache.py`](../ai-services-python/app/rag/extraction_cache.py)), so
   chunks that matched a previous run cost nothing. `copy_review.extraction_cache` reports
   hits and misses; forgetting the catalog drops its cache.
3. **Rank** (LLM) – scores candidates against the profile and returns reasons. Invented or
   duplicate product ids are dropped. Every reason and explanation is then fact-checked
   (below); unsupported ones are removed, since they are printed in the brochure.

If retrieval or extraction fails, the built-in demo catalog is used. If ranking fails, the
deterministic scorer in [`app/ai/ranker.py`](../ai-services-python/app/ai/ranker.py) (budget
fit, hobby/feature overlap, feature count) takes over. Both are reported.

### 3. Plan
Optional outline of brochure sections.

> **Why off by default:** the outline only ever feeds the writer, and the writer can structure
> copy itself. It cost ~6–25 s and a call. `USE_LLM_PLANNER=true` restores it.

### 4. Copy — the part that matters
The writer produces headline, subheadline, paragraphs and CTA from the **top product's specs**
plus the segment. Then the draft is reviewed:

1. **Deterministic checks first** (instant, always run):
   - **Grounding** ([`app/ai/grounding.py`](../ai-services-python/app/ai/grounding.py)) flags
     specific-looking terms absent from the specs: internal-capital names (SmartThings, ThinQ),
     acronyms (NFC, AI) and numbers. Comparison ignores case, spacing, punctuation and Unicode
     hyphens, so "WiFi" matches a spec's "Wi-Fi"; short terms must match a whole spec word,
     because "ai" is a substring of "stainless".
   - **Banned words**, whole-word (so "affordable" is not "ford").
2. **If those already reject the draft, the LLM reviews are skipped** — it is going back to the
   writer anyway, so their verdict would be wasted time and quota.
3. Otherwise the **spec critic** (LLM) judges the draft against the specs, and any unlisted
   feature, app, service or warranty is a failure even if plausible.
4. A rejection sends the feedback back to the **writer**, up to `MAX_REVISIONS = 2`. Still
   rejected, or the writer is unavailable → **claim-free generic copy**, reported as a warning.

> **Why the deterministic check exists at all:** in a live run the LLM critic approved copy
> promising a "SmartThings app" for a fridge whose specs mention no app. Critics catch
> contradictions; they miss plausible additions. The check also means copy is never completely
> unreviewed when the critic is down.

> **Why no tone evaluator by default:** it scored tone/readability, but its verdict never
> affected whether a draft was approved — only the banned-word scan and the spec checks do. It
> was the slowest reviewer (~11.5 s). `USE_LLM_TONE_EVALUATOR=true` brings it back, in parallel
> with the critic.

### 5–6. HTML and PDF
Jinja2 (autoescaped) renders the fixed-size A4 pages; local images are inlined as data URIs
because the renderer opens the file over `file://`. Headless Chromium prints the PDF via
Playwright, in a **child process** — on Windows, uvicorn's reload mode installs an event loop
Playwright cannot spawn a browser under, and a crashed browser cannot take the server with it.
There is no mock PDF: if rendering fails, the job fails.

## Cross-cutting design

**Every fallback is reported.** Each degradation appends to `warnings`, which the response
returns and the UI shows in a yellow box. No warnings = every AI step worked. This exists
because the previous (Go) version fell back silently at nearly every step — canned profile,
fake catalog, auto-passed critic, invalid PDF — while reporting success.

**Two providers.** Every call goes through
[`app/ai/llm.py`](../ai-services-python/app/ai/llm.py). A call that fails on the primary
provider (free tiers give 503/429 constantly) is retried once on a backup provider, normally a
different vendor. When the backup answers, the run says so.

**Deterministic fallback per step.** Profile, ranking and copy each have a non-LLM path, so a
provider outage degrades output instead of failing the request.

## Configuration that changes behaviour

| Setting | Default | Effect |
|---|---|---|
| `USE_LLM_PROFILE` | false | LLM segmentation instead of rules (+1 call) |
| `USE_LLM_PLANNER` | false | Separate outline call before the writer (+1 call) |
| `USE_LLM_TONE_EVALUATOR` | false | Tone score alongside the critic (+1 call, reported only) |
| `LLM_CRITIC_MODEL` | (same as `LLM_MODEL`) | Stronger model for fact-checking only |
| `MAX_UPLOAD_MB` | 50 | Largest catalog PDF accepted |
| `DISABLE_PDF` | false | Skip rendering; runs return `pdf_url: null` |

## Measuring a change

```bash
python -m scripts.make_sample_catalog --pages 120 --out storage/sample_catalog.pdf
python -m scripts.benchmark --label before --runs 3 --catalog storage/sample_catalog.pdf
# ... make the change ...
python -m scripts.benchmark --label after --runs 3 --catalog storage/sample_catalog.pdf
python -m scripts.benchmark --compare storage/benchmarks/before-*.json storage/benchmarks/after-*.json
```

The comparison prints time per step and per call, tokens per phase, and the quality signals a
speed change must not worsen: revisions, generic-copy fallbacks, other fallbacks, and
**unsupported terms in the copy that actually shipped** (must stay 0). Free-model latency is
noisy — compare medians over several runs, and re-run if one call was an outlier.

Logic that must not depend on a provider's mood is tested offline instead:
`tests/test_speed.py` uses fake LLMs that sleep, so "the reviewers run in parallel" and "the
LLM reviews are skipped" are proven without an API key.
