# Improvements Log

A running record of fixes and structural changes to the Marketing Agent: what changed, why,
and how it was verified. Newest entries first. Each entry lists the files touched so a change
can be traced back to its commit.

Open items that have been identified but not yet done live in [Backlog](#backlog) at the bottom.

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

- **The free model is often overloaded.** OpenRouter's free Nvidia provider returned `503
  provider overloaded` for most calls in several runs, so the brochure fell back to generic
  copy. The fallbacks work and are reported, but in practice the product needs a paid or
  less contended `LLM_MODEL` (and optionally `LLM_CRITIC_MODEL`).
- **Generic invented claims still rely on the LLM critic.** The grounding check catches
  branded names, acronyms and numbers. Plain-language additions like "get alerts on your
  phone" still depend on the critic.
- **Critic and writer only see the top product**, while the brochure shows up to 4.
- **Shared global state.** One in-memory Qdrant collection, recreated on every upload, and
  globally named extracted images (`page_N_img_M`). Concurrent users overwrite each other's
  catalogs and images, including images in brochures already generated. Scope the collection
  and image names per job, or per catalog hash.
- **Slow on the free model.** The copy step is about 30 s per writer call, and each revision
  costs another writer + critic round. Consider a faster model for the writer, or streaming
  progress to the UI instead of one long request.
- **`/storage` is fully public.** Job ids are no longer guessable, but extracted images use
  predictable names. Serve generated files through a job-id-checked route instead.
- **No cancellation.** When the browser disconnects, the pipeline keeps running to completion.
- **Dependencies.** `google.generativeai` (embeddings) is deprecated; move to `google-genai`.
  Qdrant `recreate_collection` / `search` are deprecated.
- **Leftovers:** empty `frontend-nextjs/`. The service directory is still named
  `ai-services-python/` although it is now the whole backend.

**Done** (moved out of the backlog on 2026-09-22 by the Python migration): silent fallbacks,
critic re-rolling instead of revising, retry stacking, silent form defaults, unverified template
claims, timestamp job ids / CORS `*` / SMTP `InsecureSkipVerify`, and the dead car-era code
(`ProductMatcher` now drives the ranking fallback).
