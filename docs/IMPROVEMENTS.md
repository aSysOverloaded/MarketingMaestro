# Improvements Log

A running record of fixes and structural changes to the Marketing Agent: what changed, why,
and how it was verified. Newest entries first. Each entry lists the files touched so a change
can be traced back to its commit.

Open items that have been identified but not yet done live in [Backlog](#backlog) at the bottom.

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

- **Decision taken 2026-09-22: migrate to a Python-only backend** (remove `backend-go/`). Several
  items below get simpler once there is one process.
- **Silent fallbacks everywhere.** LLM failures produce a canned profile, a fake catalog, canned
  copy with unverified claims, an auto-passed critic, and an invalid mock PDF — all reported as
  `success: true`. Record every fallback in the response and show it in the UI.
- **Critic retry re-rolls instead of revising.** A critic rejection re-runs the critic on the same
  copy until it passes. It should re-run the writer with the critic's feedback.
- **Critic and writer only see the top product** (`specs[0]`), while the brochure shows up to 4.
- **Shared global state in the sidecar.** One in-memory Qdrant collection, recreated on every
  upload, and globally named extracted images (`page_N_img_M`): concurrent users overwrite each
  other's catalogs and images.
- **Retry stacking and no cancellation.** LLM-call retries × orchestrator retries with sleeps that
  ignore the request context; a bad request can hold a connection for minutes.
- **Form defaults are silent.** Empty form fields become 32 / $120k / family of 4 / Seattle /
  trekking without telling the user.
- **Unverified template claims.** Placeholder spec cells render "Status: Available" and
  "Quality: Certified" when data is missing.
- **Security.** `/api/send-email` sends any job's PDF to any address (job IDs are timestamps);
  CORS `*`; all of `/storage` is public; SMTP on port 465 uses `InsecureSkipVerify`.
- **Dead code from the car-domain era.** Unused `ProductMatcher`, `config.yaml`, Postgres
  migrations, `vehicles` table, horsepower/seats fields, BMW/Navigator/Tesla branches,
  `ford`/`toyota` banned words, empty `frontend-nextjs/`.
- **Dependencies.** `google.generativeai` is only installed transitively and is deprecated;
  Qdrant `recreate_collection` / `search` are deprecated.
