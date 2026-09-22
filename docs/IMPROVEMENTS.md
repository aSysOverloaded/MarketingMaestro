# Improvements Log

A running record of fixes and structural changes to the Marketing Agent: what changed, why,
and how it was verified. Newest entries first. Each entry lists the files touched so a change
can be traced back to its commit.

Open items that have been identified but not yet done live in [Backlog](#backlog) at the bottom.

---

## 2026-09-23 — Ingest quality, diagnosed on a real 133-page catalogue

New `scripts/inspect_catalog.py` reports what a PDF will actually give the pipeline (text per
page, empty pages, dense pages, images, ingest cost) and recommends a strategy, before any
embedding quota is spent. Run on a real 38 MB sports catalogue:

| | |
|---|---|
| pages | 133 |
| empty pages | **0** - text extraction works everywhere, so **no OCR needed** |
| text per page | median 2106 chars, max 23306 |
| dense pages (multi-product) | **95 (71%)** |
| images | **2821, median 19 per page** |
| prices | **none** - wholesale catalogue, product codes only |

That contradicted the assumption that OCR was the missing piece. The real problems are
granularity (one page holds several products), image choice (19 candidates per page) and
flattened columns.

### Fixed now
- **Hero image is the largest image on the page again.** The batch-capping added earlier kept
  the *first* 2 images over 4 KB, which on a 19-image page is a logo or banner. Every image is
  now measured first and only the largest 2 are written (`select_page_images`, testable).
- **Repeated page furniture is stripped before embedding.** Lines appearing on ≥40% of pages
  (navigation bars, "Sports 2025", size charts) are removed from the indexed text: they add
  nothing to retrieval and pull every page's embedding towards the same centre. On the real
  catalogue it found 4 such lines, and page 54 now starts at the product name "GOLEM" instead
  of the navigation bar.
- **Files:** `app/rag/search.py`, `scripts/inspect_catalog.py` (new),
  `tests/test_ingest_quality.py` (new)
- **Verified:** 51 tests, 4 new (boilerplate detection and stripping, the short-document and
  rare-line cases, largest-image selection with only the winners written, and the undecodable
  image fallback). Boilerplate detection also checked against the real catalogue.

### Still open for this catalogue (see Backlog)
- **One page = one chunk** although 71% of pages hold several products, so products share an
  entry and a hero image.
- **Columns are flattened**: `pypdf` turns three columns into
  `SOFTLOCK + MESH: 100% POLYESTERAvailable until 202880000274`. Layout-aware extraction
  (pdfplumber/PyMuPDF word coordinates) would keep them apart.

---

## 2026-09-23 — Fewer LLM calls per run (3 optional calls off by default)

Measurement showed a run is ~5.4k tokens but 5+ round trips, so **the cost of a run is the
number of calls, not their size**. Three calls were earning little:

| Removed from the default path | Why | Restore with |
|---|---|---|
| **profile** (segment + budget tier) | Rules in `app/ai/profile.py` use the same inputs (hobbies, income, family size), are instant and deterministic, and cannot invent a segment | `USE_LLM_PROFILE=true` |
| **planner** (section outline) | The outline only ever fed the writer, which can structure copy itself | `USE_LLM_PLANNER=true` |
| **tone evaluator** | Its verdict never affected approve/revise - only the banned-word scan and the spec checks do. It was also the slowest reviewer (~11.5 s) | `USE_LLM_TONE_EVALUATOR=true` |

They are **switches, not deletions**, so each decision can be re-tested with the benchmark.
Reasoning is documented at the point of use: `docs/PIPELINE.md`, `app/config.py` and
`.env.example`.

**Per run now:** extractor (with a catalog) + ranker + writer + critic per draft = 3-4 calls,
down from 7 in the original Go pipeline and 5 before this change.

### Benchmark: honest result
Deterministic gains (120-page catalog, per run):

| | before | after |
|---|---|---|
| LLM calls (writer/critic pair + extractor + ranker) | 5 | **2 + extractor/ranker** |
| input tokens | 4,020 | **2,581** |
| profile + planner + evaluator tokens | 891 | **0** |
| generic copy / unsupported claims | 0 / 0 | **0 / 0** |

**Wall-clock is NOT a clean comparison and should not be quoted as one.** Medians came out
worse (54.6 s → 130.4 s) for reasons unrelated to the change: the earlier runs used *mock*
embeddings (the quota had failed), these used real ones; n=2; and one run spent ~2 minutes
waiting on embedding rate limits during retrieval. Individual clean run: 53.6 s with 2 LLM
calls. A fair speed comparison needs a re-run on fresh quota.

### Query embeddings no longer stall a run
The ingest rate-limit retry (previous entry) also applied to *query* embeddings, so a search
could block a live run for up to 60 s. Queries now retry once, capped at 10 s, then degrade to
a mock vector (reported as usual); ingest keeps the long waits, because it is a one-off cost
and mock pages stay wrong until the catalog is re-uploaded.

- **Files:** `app/config.py`, `app/pipeline/brochure.py`, `app/ai/writer.py`,
  `app/rag/search.py`, `.env.example`, `README.md`, `docs/PIPELINE.md` (new), tests
- **Verified:** 47 tests. New: optional calls really are absent by default and produce no
  warnings; banned words still enforced without the tone evaluator; query embeddings give up
  quickly. Live: a 120-page ingest hit the rate limit, waited 29 s then 60 s, and completed
  with `embeddings=real` - previously those pages silently held mock vectors.

---

## 2026-09-23 — Large catalogs: batching, rate-limit retries, token accounting

### Large PDFs were quietly unusable
- **Problem:** A 38 MB / several-hundred-page catalog hit three separate walls. The upload
  limit was hard-coded at 15 MB. Every page was embedded in **one** API request, which fails
  outright for a large catalog and dropped the whole index to mock vectors. And every image of
  every page was written to disk, though only one hero image per page is ever used.
- **Fix:**
  - `MAX_UPLOAD_MB` setting, default 50 (was a hard-coded 15 MB).
  - Pages are embedded in batches of 50, each page trimmed to 8000 chars. A failing batch
    degrades only itself.
  - **Rate-limit retries.** Gemini's free embedding quota is 100 requests per minute and counts
    one request per text, so a large catalog *will* hit it mid-ingest. A rate-limited batch now
    waits (using the provider's own `retry_delay` when given, capped at 60 s) and retries up to
    4 times, instead of silently filling those pages with mock vectors - which would make
    retrieval return near-random pages for the rest of the catalog's life.
  - Image extraction keeps at most 2 images per page and skips anything under 4 KB.
- **Files:** `app/rag/search.py`, `app/config.py`, `app/main.py`, `static/index.html`,
  `tests/test_catalog.py`
- **Verified:** 44 tests, 3 new: batch sizes and per-page trimming over 120 pages, a
  non-retryable failure degrading only its own batch, and a rate-limited batch waiting the
  provider's `retry_delay` then succeeding with real vectors.

### Token accounting, and where the time and tokens actually go
- `invoke_structured` records input/output tokens per purpose (`diagnostics.add_tokens`), and
  the benchmark reports them per phase. New `scripts/make_sample_catalog.py` generates a
  realistic multi-page catalog PDF, so ingest/retrieval/**extraction** can be benchmarked
  without a real (confidential) catalog. `--catalog PDF` runs the benchmark against it.
- Measured on a generated 120-page catalog (2 runs, median), `gemini-3.1-flash-lite`:

| phase | time | tokens in / out |
|---|---|---|
| recommend (search + extract + rank) | 19.5 s | extractor 1035/403, ranker 854/293 |
| copy (write + review) | 18.4 s | writer 673/256, critic 567/78, evaluator 413/48 |
| plan | 6.2 s | 270/277 |
| profile | 4.6 s | 208/25 |
| pdf | 5.9 s | - |
| **total** | **54.6 s** | **~4.0k in / 1.4k out per run** |

  **Tokens are not the constraint - latency is.** A whole run is ~5.4k tokens; the cost of a
  run is round-trip time, not token spend. Extraction is the largest prompt but scales with the
  number of *matched* pages (6), not catalog size. The evaluator was the slowest reviewer here
  (11.5 s vs the critic's 6.5 s), and its score never affects the approve/revise decision.

---

## 2026-09-23 — Backup LLM provider; Gemini as primary; 27% faster overall

### One call path, with an automatic backup provider
- **Problem:** Free tiers fail constantly - `503 overloaded` and `429 daily cap` - and each
  failed call costs a step its AI output, so runs silently degraded to generic copy. Switching
  to another free model on the same vendor does not help: the daily cap is per account.
- **Fix:**
  - `planner`, `writer`, `critic` and `evaluator` now go through the same
    `llm.invoke_structured` as the newer chains, instead of each repeating the call/parse/
    diagnostics logic. One call path for every AI step.
  - A call that fails on the primary provider is retried once on a **backup provider**
    (`LLM_FALLBACK_API_URL` / `_API_KEY` / `_MODEL`), normally a different vendor, so both
    rarely fail at once. Parsing failures also fail over, since another model may parse fine.
    Both failing = the step's existing deterministic fallback, as before.
  - When the backup answers, the run says so in `warnings` ("answered by the fallback
    provider"), so a run never silently depends on the backup. Diagnostics record
    `real` / `fallback` / `failed` per purpose.
- **Config now:** primary is **Gemini** `gemini-3.1-flash-lite` via its OpenAI-compatible
  endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`), backup is OpenRouter
  `nex-agi/nex-n2.5-pro:free`. Both free. The Gemini key already existed for embeddings.
- **Files:** `app/ai/llm.py`, `app/ai/{planner,writer,critic,evaluator}.py`,
  `app/pipeline/brochure.py`, `app/config.py`, `.env.example`, `tests/test_llm_fallback.py`
  (new), `tests/conftest.py`

### Benchmark: baseline vs now (3 runs each, median)

| | baseline (OpenRouter, sequential review) | now (Gemini + parallel review) |
|---|---|---|
| total | 165.8 s | **120.9 s** |
| copy step | 100.7 s | **31.7 s** |
| writer call | 32.4 s | 15.9 s |
| writer calls per run | 1.7 | 1.0 |
| critic / evaluator call | 16.7 / 14.4 s | 8.2 / 8.6 s |
| runs with generic copy | 1 of 3 | **0** |
| runs with fallbacks | 1 of 3 | **0** |
| runs with unsupported claims in final copy | 0 | **0** |

Copy step −69%, total −27%, and quality held: every run produced real AI copy, approved on the
first draft, with nothing unsupported reaching the brochure. Remaining variance is single slow
provider calls (one critic call 86 s, one ranking call 69 s in otherwise normal runs) - exactly
what the backup provider exists for.

- **Verified:** 41 tests (6 new for the fallback: backup untouched when the primary works,
  retried when it fails, both-failed recorded and raised, no backup configured, per-provider
  url/key/model, and the pipeline warning when the backup answered). The `no_llm` fixture now
  patches one call point instead of five modules.

---

## 2026-09-22 — Benchmark harness; faster review in the copy step

### How speed is measured
- New `scripts/benchmark.py` runs the real pipeline N times against the configured model, on
  fixed customer profiles and the demo catalog, so every run sees the same input. It records
  per-step times and per-call times inside the copy step (writer / critic / evaluator, via
  the new `ctx.review["calls"]`). It also records the quality signals a speed change must not
  worsen: revisions, generic-copy fallbacks, other fallbacks, and **unsupported terms in the
  copy that actually shipped** (must stay 0). Results are saved to
  `storage/benchmarks/<label>-<time>.json`, and `--compare a.json b.json` prints them side
  by side. Free-model latency is noisy, so compare medians over several runs.
- The logic of speed changes is tested offline in `tests/test_speed.py` with fake LLMs that
  sleep, so it doesn't depend on API quota or provider load.

### Baseline (before the changes below), `nex-agi/nex-n2.5-pro:free`
Only 2 of 3 runs are valid. The 3rd hit OpenRouter's **free-models-per-day limit** (`429`) and
fell back to generic copy.

| | Run 1 | Run 2 |
|---|---|---|
| Total | 254 s | 166 s |
| Copy step | 202 s | 101 s |
| Draft 1: writer / critic / evaluator | 45 / 48 / 22 s | 23 / 13 / 4 s |
| Draft 2: writer / critic / evaluator | 49 / 5 / 31 s | 32 / 19 / 6 s |

The critic and evaluator ran sequentially, and the writer is the single biggest cost.

### Changes
- **Critic and evaluator run in parallel** (they are independent), so a review costs
  max(critic, evaluator) instead of the sum. Projected from the baseline timings: about −27 s
  on run 1 and −10 s on run 2.
- **LLM reviews are skipped when the instant checks already rejected the draft.** Unsupported
  spec terms or banned words mean the draft goes back to the writer either way, so an LLM
  verdict on it is wasted time and quota. The banned-word scan (`evaluator.banned_words_in`,
  formerly private) now runs first alongside the grounding check. Skips are counted in
  `copy_review.skipped_llm_reviews`.
- **Files:** `app/pipeline/brochure.py`, `app/ai/evaluator.py`, `scripts/benchmark.py` (new),
  `tests/test_speed.py` (new), `tests/test_rules.py`
- **Verified:** 35 tests. New ones cover a grounded draft reaching both reviewers, review wall
  time being ~1× (not 2×) the LLM delay, LLM reviews skipped for a draft with an invented
  "SmartThings app" and run for its grounded revision, and a banned word triggering a revision
  with no LLM calls. **Live "after" benchmark pending:** the key hit the free daily request
  limit.

---

## 2026-09-22 — Real progress in the UI; working free model

### Real step-by-step progress
- **Problem:** The loading card changed text on fixed `setTimeout` timers (3.5 s, 7.5 s) that
  had nothing to do with what the pipeline was doing, while real runs take 2–3 minutes.
- **Fix:**
  - `POST /api/recommend` now starts a background job and returns `202 {job_id, status_url}`
    right away.
  - New `GET /api/jobs/{job_id}` returns each step's status (`pending` / `running` / `done` /
    `failed`) and duration, plus a live note from inside the step ("Ranking 3 products",
    "Writing draft 2 (revising with reviewer feedback)", "Fact-checking draft 2"), and the
    result or the error once finished.
  - Plumbing: `Workflow.run(ctx, on_step=...)` reports each step, `JobContext.note()` carries
    the sub-step notes, and `app/jobs.py` is an in-memory registry that keeps the last 20 jobs
    (single-user). Catalog indexing shows up as its own "ingest" step when a PDF is uploaded.
  - The UI polls every second and renders the step list with timings. The fake timers are
    gone. A failed job shows which step failed and still shows its warnings.
- **Breaking API change:** `/api/recommend` no longer returns the result directly. The same
  result object is now at `GET /api/jobs/{id}` → `result`. The only client is `static/index.html`.
- **Also:** the upload hint said "Max 10MB" while the limit is 15 MB.
- **Files:** `app/jobs.py` (new), `app/main.py`, `app/pipeline/workflow.py`,
  `app/pipeline/brochure.py`, `static/index.html`, tests
- **Verified:** 31 tests. The API tests now submit and poll, and a new test covers a failing
  step (status `failed`, the failed step named, warnings kept). I drove the real UI headlessly
  against the live server and model: steps and notes updated live, with no page errors.
  Timeline: profile 5 s, recommend 14 s, plan 25 s, draft 1 written in ~60 s and fact-checked
  in ~35 s, draft 2 in ~25 s + ~35 s, PDF ~9 s, 210 s total. **The copy step is ~80% of the
  run time.**

### Model
- New OpenRouter key installed in `.env`. The old default `nvidia/nemotron-3-super-120b-a12b:free`
  was persistently `503 overloaded`. `nex-agi/nex-n2.5-pro:free` passed structured-output
  checks repeatedly and is now the default in `config.py` and `.env.example`. Also working at
  the time: `nvidia/nemotron-3-ultra-550b-a55b:free` (but ~97 s for one small call) and,
  intermittently, `qwen/qwen3.8-27b:free` (sometimes `429`). With it, the first run with **no
  AI fallbacks at all** completed: grounded copy after one revision, sensible varied ranking
  (82/76/63), and ranker reasons passing the new grounding check with nothing removed.

---

## 2026-09-22 — Catalog persists and is reused; ranker reasons are fact-checked

### The uploaded catalog was forgotten on the next run
- **Problem:** The pipeline used the indexed catalog only when that request included the PDF.
  A second run without re-uploading silently used the demo catalog, even though the index
  still held yours. The index was also in-memory, so every restart meant uploading and
  embedding again.
- **Fix:**
  - Qdrant now runs in local on-disk mode (`storage/qdrant`). The client is created lazily,
    because `python -m app.main` runs uvicorn with reload, which imports the module in two
    processes, and local Qdrant allows one process per folder.
  - `storage/catalog.json` records the indexed catalog: filename, sha256, pages, time, and
    whether the embeddings were real.
  - A run without a file reuses the indexed catalog. The response gains
    `catalog: {filename, ..., source: "uploaded" | "reused"}`, or `null` for the demo catalog.
  - Re-uploading byte-identical content skips re-embedding, as before, but now also across
    restarts. The exception is a catalog indexed with mock embeddings (no working
    `GEMINI_API_KEY`), which is re-embedded.
  - New `DELETE /api/rag/catalog` to go back to the demo catalog.
  - UI: a line under the upload box says which catalog the next run will use, with a
    "forget it" link. The result card shows which catalog a run used. The status is also
    flagged when the catalog was indexed with mock embeddings.
- **Files:** `app/rag/search.py`, `app/main.py`, `static/index.html`, `tests/conftest.py`,
  `tests/test_catalog.py` (new)
- **Verified:** 3 new tests using a generated one-page PDF: the catalog survives a simulated
  restart, mock-embedded catalogs are re-embedded while real ones are reused, and the API goes
  upload → reused on the next run → forgotten → demo catalog. Started the real server with
  `python -m app.main` (reload mode): no Qdrant folder-lock conflict, and the stats and
  forget routes respond. The test fixture now also blanks `GEMINI_API_KEY`, so tests never
  call the real embeddings API.

### Ranker explanations are fact-checked
- **Problem:** Only the cover copy went through the grounding check and critic. The
  per-product "Why this option fits your profile" explanation and matched rules came
  straight from the ranking LLM and were printed in the brochure.
- **Fix:** After ranking, each explanation and matched rule is run through the grounding check
  (`find_ungrounded_in_text`, split out of `find_ungrounded_terms`). The sources are the
  product's specs **plus the customer profile**, since "fits your family of 3" is a legitimate
  reason. The ranker has no revise loop, so a flagged rule is dropped and a flagged
  explanation is replaced with a safe customer-facing sentence. Removals are listed in
  `copy_review.ranker_removed` and summarized in one warning.
- **Files:** `app/ai/grounding.py`, `app/pipeline/brochure.py`, `tests/test_pipeline.py`
- **Verified:** A new test covers an invented app, an acronym and a leaked "match score 92"
  (removed) against customer facts like family size and budget (kept). The offline
  end-to-end test asserts the rule-based fallback's own reasons are never flagged. 30 tests
  total. Not yet seen on real ranker output: the ranker got `503` in every live run so far.

---

## 2026-09-22 — Deterministic check for invented features in the copy

- **Problem:** The LLM critic passed copy that promised a "SmartThings app" for a fridge whose
  specs mention no app. It happened in both drafts of the live run, and the second draft
  shipped in the brochure. The critic handles contradictions ("30 cu. ft." vs 26.5) but misses
  plausible additions.
- **Fix, in three layers:**
  1. New `app/ai/grounding.py::find_ungrounded_terms`: flags terms in the copy that don't
     appear in the product specs. It checks internal-capital names (SmartThings, ThinQ,
     iPhone), acronyms (NFC, OLED, AI) and numbers. Comparison ignores case, spacing,
     punctuation and Unicode hyphens ("WiFi" matches "Wi-Fi"). Short terms must match a whole
     spec word, because "ai" is a substring of "stainless". It runs first in every review,
     and flagged terms go to the writer as revision feedback. It is deterministic, so it still
     runs when the LLM critic is down; the warning now says "only the deterministic spec-term
     check ran" instead of "NOT fact-checked".
  2. The critic prompt now treats any unlisted feature, app, service, integration,
     certification, warranty or capability as a failure, even when plausible.
  3. New optional `LLM_CRITIC_MODEL` points only the critic at a stronger model (same endpoint
     and key). Fact-checking is where a weak model hurts most, and it's one call per draft.
- **Deliberate limits:** ordinary and Title Case words are left to the LLM critic, since
  checking them deterministically flags every marketing heading. Single-digit integers are
  skipped because they are usually counts ("3 options"), so an invented "5-year warranty" can
  still get through to the critic.
- **Files:** `app/ai/grounding.py` (new), `app/pipeline/brochure.py`, `app/ai/critic.py`,
  `app/ai/llm.py`, `app/config.py`, `.env.example`
- **Verified:** Run on both real drafts from the live run, it flags exactly `SmartThings` + the
  leaked `90` match score (draft 1) and `SmartThings` (draft 2), with no false positives. The
  LLM critic had approved draft 2. 6 new tests (26 total): the live-run draft, acronyms and
  numbers, spelling and hyphen variants, short-term whole-word matching, the critic-model
  override, and a pipeline test showing the grounding check forces a revision even when the
  critic passes. A fresh live run wasn't possible: the free provider returned
  `503 provider overloaded` on the writer in two attempts. That was reported correctly as a
  fallback to generic copy.

---

## 2026-09-22 — Migrated to a single Python service; Go backend removed

**Why:** The Go backend was mostly glue. Four of its nine steps were HTTP clients that
forwarded to the Python sidecar, and much of the rest was LLM plumbing (response cleanup,
retries, fake-data fallbacks) that LangChain structured output already covers. Having two
processes caused real bugs: API keys had to be set in two `.env` files, Pydantic schemas were
hand-synced with Go structs that silently zero-filled mismatched fields, and every sidecar
hiccup triggered a silent Go-side bypass. Every request is bound by LLM latency, so Go's
speed bought nothing.

**What replaced what:**

| Go (removed) | Python (new) |
|---|---|
| `main.go` HTTP handlers | `app/main.py` (FastAPI; also serves the UI and `/storage`) |
| `workflow/orchestrator.go` | `app/pipeline/workflow.py` — same ideas: ordered steps, per-step retries with exponential backoff, compensation in reverse order |
| `workflow/gemini.go` (LLM calls, JSON cleanup, `simulateFallback`) | `app/ai/llm.py::invoke_structured` + new chains `app/ai/profile.py`, `extractor.py`, `ranker.py` |
| `steps/planner.go`, `writer.go`, `critic.go`, `evaluator.go` | Deleted — the pipeline calls the existing Python chains directly |
| `steps/profile.go`, `recommend.go` | `app/pipeline/brochure.py` steps `profile`, `recommend` |
| `steps/compile_html.go` + `templates/brochure_template.html` | `app/render/brochure.py` + `templates/brochure.html` (Jinja2, autoescaped) |
| `steps/render_pdf.go` (chromedp) | `app/render/pdf.py` (Playwright, in a child process) |
| `steps/email.go` | `app/delivery/email.py` (`smtplib`) |
| `recommendation/product_matcher.go` (unused) | Its budget/hobby scoring now powers the ranking fallback `ranker.score_products` |
| `config.yaml`, `migrations/` (both unused) | Deleted |

**Behaviour changes made during the port** (cheaper to fix than to port as-is):

- **Fallbacks are reported, not hidden.** Every fallback appends to a `warnings` list returned
  by `/api/recommend` and shown in a new UI panel. Fallbacks now use the customer's input: a
  rule-based profile instead of a canned "Adventure / Premium", and rule-based scoring instead
  of fixed scores of 95/88.
- **Critic rejection now revises the copy.** The writer gets the critic's and evaluator's
  feedback and tries again, up to `MAX_REVISIONS = 2`. Before, the critic was simply re-run on
  the same copy until the model happened to pass it. If the copy is still rejected, or the
  writer is down, claim-free generic copy is used; the old fallback copy asserted unverified
  features like "whisper-quiet operation".
- **No fake PDF.** If rendering fails the request fails; `DISABLE_PDF=true` skips it with a
  warning and `pdf_url: null`. The old fallback wrote an invalid 40-byte "PDF".
- **Fixed pages spilling over in the PDF (older bug).** The on-screen preview padding pushed each
  297 mm page past A4, so every page's footer landed on an extra page: a 3-product brochure
  printed as 8 pages instead of 4. Fixed with print-only CSS. Verified on the Go output too.
- **No invented spec cells.** The template no longer prints "Status: Available",
  "Quality: Certified" or a horsepower cell when the catalog doesn't state them. It shows
  Category, Capacity and Power only when present.
- **Security:** job ids are `uuid4` instead of timestamps (they gate PDF download and
  `/api/send-email`); `job_id` and email are validated; CORS `*` removed (same origin now);
  SMTP verifies TLS certificates on both 465 and STARTTLS; dev server binds `127.0.0.1`
  instead of `0.0.0.0`.
- **Input defaults are reported.** Empty form fields still get defaults, but the defaulted
  fields are listed in `warnings`.
- **Bounded latency:** chat calls have a 60 s timeout and 2 retries (`LLM_TIMEOUT_SECONDS`,
  `LLM_MAX_RETRIES`), and there are no orchestrator retries on LLM steps. The old stack was
  5 HTTP retries with sleeps up to 30 s, times up to 3 orchestrator retries.
- UI: shows product names instead of mangled ids, escapes LLM-generated text, hides the PDF
  link when there is no PDF, and shows the failed step on errors.
- Dropped car-era leftovers: BMW/Navigator/Tesla branding branches, horsepower/seats fields,
  and `ford`/`toyota` banned words.
- Removed the Go-only endpoints `/api/plan`, `/api/write`, `/api/critic`, `/api/evaluate` and
  `/api/rag/ingest`. `/api/rag/search` stays as a debugging aid.

**Config:** one `ai-services-python/.env`. New optional keys: `SMTP_*`, `DISABLE_PDF` (was
`DISABLE_CHROME_PDF`), `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `STORAGE_DIR`. Generated files
now live in `ai-services-python/storage/`. Old Go output in `backend-go/storage/` was left on
disk, untracked and gitignored.

**Verified:**
- `pytest`: 20 tests, all offline (LLM stubbed). They cover the workflow runner (order, retry
  and backoff, reverse compensation), banned words, branding, rule-based profile and scoring,
  that the ranker drops invented ids, an end-to-end run where every LLM step falls back, the
  revise-with-feedback loop, rendering only stated specs plus the paragraph cap and
  autoescaping, storage path containment, and the API routes.
- Real PDF render through the installed Edge/Chrome: valid 4-page A4 PDF, visually checked.
- Live run against the configured OpenRouter model. The critic rejected draft 1 (it quoted the
  internal match score to the customer), the writer revised it, and the critic passed revision 1.
  Two calls got `503 provider overloaded`; both fell back and were reported. Total 124 s:
  106 s was the copy step (about 30 s per writer call on this free model), PDF 6.7 s.

---

## 2026-09-22 — Quick fixes (tier 1)

Found during a full read-through of the pipeline. Each of these was a confirmed bug, not a
style issue.

### Banned-word check rejected "affordable"
- **Problem:** The evaluator's banned-word scan used substring matching, so `"ford"` matched
  inside "affordable". Go treats any banned word as a hard error with no retry, so any
  budget-focused copy failed the whole request with a 500.
- **Fix:** Whole-word regex match (`\bword\b`).
- **Files:** `ai-services-python/app/ai/evaluator.py`
- **Verified:** "An affordable choice" → no match; "Not cheap" → `cheap`; "any Ford" → `ford`.

### Writer body copy and CTA were never rendered
- **Problem:** The writer generates headline, subheadline, paragraphs and a CTA. The brochure
  template only used the headline and subheadline. The paragraphs (the most expensive LLM
  output, audited by both critic and evaluator) and the CTA were silently discarded.
- **Fix:** Template now renders `Copy.Paragraphs` (falling back to the old static intro when
  empty) and a CTA block on the cover page. Replaced the repeated anonymous copy struct with a
  named `copyBlock` type and removed the dead `CopywriterStep` branch (no step produces it).
- **Overflow guard:** The cover is a fixed-height A4 page with `overflow: hidden`, so long copy
  would be clipped without warning. The writer schema now asks for 2–3 paragraphs under 60 words
  each, and Go hard-caps at `maxCoverParagraphs = 3`.
- **Files:** `backend-go/templates/brochure_template.html`, `backend-go/internal/steps/compile_html.go`,
  `ai-services-python/app/ai/schemas.py`

### Wrong brand on dishwashers
- **Problem:** Brand detection treated any model containing `lg`, `wash` or `dryer` as LG, so
  "Bosch 800 Series Dishwasher" got LG's red branding.
- **Fix:** Match `lg` only as a standalone word; product categories are no longer brand signals.
- **Files:** `backend-go/internal/steps/compile_html.go`
- **Verified:** New `compile_html_test.go` covers Bosch dishwasher, generic washer, LG, Samsung,
  and a model containing "lg" mid-word.

### Extraction prompt told the model to invent prices and products
- **Problem:** The catalog-extraction prompt said *"If pricing or specific specs are not listed,
  make a highly accurate estimate"* and *"Never return an empty array"*. Invented prices were
  printed in the customer-facing brochure, and since the critic audits copy against these
  extracted specs, invented specs also became the "ground truth" the audit checked against.
- **Fix:** Prompt now requires extracting only stated values: unknown price → `base_price: 0`,
  unstated specs omitted, pages with no product skipped, empty result allowed. The ranking prompt
  is told that price 0 means unknown, not free. The template renders price 0 as
  "Price on request" / "On request" instead of "$0.00".
- **Files:** `backend-go/internal/steps/recommend.go`, `backend-go/internal/steps/compile_html.go`,
  `backend-go/templates/brochure_template.html`

### Email was sent automatically, and SendGrid "sends" were fake
- **Problem 1:** `EmailDispatchStep` ran inside every `/api/recommend` workflow. With SMTP
  configured, every generated brochure was emailed — including to the form's default
  `customer@example.com` — and then sent again when the user clicked Send.
- **Problem 2:** With `SENDGRID_API_KEY` set, the step logged a live send and reported success
  without calling any API. SMTP failures were also swallowed and reported as `Sent: true`.
- **Fix:** Removed the step from the web workflow; delivery is only via `/api/send-email`
  (the UI's Send button). Deleted the fake SendGrid branch. `Sent` now reflects whether a live
  send actually succeeded.
- **Files:** `backend-go/main.go`, `backend-go/internal/steps/email.go`

### Recommender ignored the user's age and location
- **Problem:** The ranking prompt's user profile hardcoded `Age: 32` and `Location: "Seattle, WA"`
  regardless of what was submitted.
- **Fix:** Read both from the profile step's attributes.
- **Files:** `backend-go/internal/steps/recommend.go`

### Integration test deleted real generated brochures
- **Problem:** `TestFullMVPWorkflowExecution` pointed its output dirs at the real
  `backend-go/storage/*` and ran `os.RemoveAll` on them.
- **Fix:** Outputs go to `t.TempDir()`. Also untracked a generated PDF that had been committed
  despite `storage/` being gitignored.
- **Files:** `backend-go/internal/steps/integration_test.go`,
  `backend-go/storage/generated_brochures/brochure_job_job_integration_1.pdf` (removed from git)
- **Verified:** Full `go test ./...` passes offline (LLM keys unset, `DISABLE_CHROME_PDF=true`);
  the 46 existing brochures in `storage/` survived the run.

---

## Backlog

Identified but not yet done. Ordered roughly by priority.

> **Scope (decided 2026-09-22): single user.** The app is used by one person at a time, and
> concurrent use is not supported. Shared in-process state (one Qdrant collection, globally
> named extracted images, publicly served `/storage`) is acceptable under this assumption, so
> multi-user items are out of scope rather than backlog.

- **PDF extraction: one page = one chunk.** Measured on a real catalogue, 71% of pages hold
  several products, so they share one index entry and one hero image, and the extractor gets a
  blob. Split pages into product blocks (a product code like `80000274` plus a name line is a
  reliable boundary in that catalogue) and embed per block.
- **PDF extraction: columns are flattened.** `pypdf` gives text in stream order, so adjacent
  columns merge (`POLYESTERAvailable until 202880000274`). pdfplumber/PyMuPDF expose word
  coordinates; clustering by x-position would keep product blocks apart and also let images be
  matched to the product beside them.
- **Extraction repeats every run** for the same matched pages; caching extracted products per
  page (in the Qdrant payload) makes repeat runs skip it.
- **Retrieval is vector-only.** No keyword/BM25 hybrid, so exact model codes match poorly.
- **OCR** is still absent, but measured as *not* the bottleneck for this catalogue (0 empty
  pages). Needed only for scanned catalogues.
- **Generic invented claims still rely on the LLM critic.** The grounding check catches
  branded names, acronyms and numbers. Plain-language additions like "get alerts on your
  phone" still depend on the critic.
- **Critic and writer only see the top product**, while the brochure shows up to 4.
- **Re-run the speed benchmark on fresh quota.** The last comparison was confounded by mock
  vs real embeddings and rate-limit waits; the call-count and token reductions are solid but
  the wall-clock gain is unmeasured.
- **Free-tier daily request cap.** OpenRouter's free models allow a limited number of requests
  per day unless the account has credits. Now mitigated rather than solved: it is the backup
  provider, and Gemini's free tier is primary.
- **UI text sizes are wrong in places.** `static/index.html` uses Tailwind arbitrary-size
  classes (`text-[10px]` and similar) that the Tailwind 2.2 CDN build doesn't support, so those
  labels render at the default size.
- **Branding only knows Samsung and LG.** Any uploaded catalog gets the "Premium Home" default,
  and the cover uses the top product's brand even when the options have mixed brands. Have the
  extractor return the brand, and use neutral cover branding for mixed selections.
- **PDF rendering depends on the network.** Tailwind (CDN), Google Fonts and the Unsplash
  fallback images load at render time: slow (`networkidle`) and broken offline. Ship compiled
  CSS and fonts locally.
- **No quality measurement.** There is no fixed set of sample catalogs and profiles for
  measuring fallback rate, grounding violations or critic rejections across prompt and model
  changes. Every quality claim so far comes from one-off manual runs.
- **Thin personalisation.** The brochure says "Prepared for: Valued Customer", because the form
  collects no name.
- **Scanned PDFs yield nothing.** No OCR, so image-only catalogs just produce a warning.
- **Ingesting a large catalog is slow on free embeddings** (100 requests/minute, one per page),
  so ~100 pages per minute of waiting. One-time per catalog, but a 400-page catalog is a ~4
  minute ingest. A paid tier or a local embedding model removes this.
- **Dependencies.** `google.generativeai` (embeddings) is deprecated; move to `google-genai`.
  Qdrant `recreate_collection` / `search` are deprecated.
- **Leftovers:** empty `frontend-nextjs/`. The service directory is still named
  `ai-services-python/` although it is now the whole backend.

**Done:** catalog reuse and persistence, fact-checking ranker reasons, and real UI progress
(2026-09-22, see above). Also, by the Python migration: silent fallbacks,
critic re-rolling instead of revising, retry stacking, silent form defaults, unverified template
claims, timestamp job ids / CORS `*` / SMTP `InsecureSkipVerify`, and the dead car-era code
(`ProductMatcher` now drives the ranking fallback).
