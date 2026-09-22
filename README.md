# Marketing Agent

Generates a personalized product brochure (PDF) for a customer: profiles them from a few
demographics, retrieves matching products from an uploaded PDF catalog, ranks them, writes
marketing copy that is fact-checked against the product specs, and renders an A4 brochure.

Single Python service (FastAPI + LangChain). The code lives in [`ai-services-python/`](ai-services-python/).

## Run it

```bash
cd ai-services-python
python -m venv venv
venv/Scripts/pip install -r requirements-dev.txt     # venv/bin/pip on macOS/Linux
cp .env.example .env                                # then fill in LLM_API_KEY and GEMINI_API_KEY
venv/Scripts/python -m app.main                     # http://127.0.0.1:8000
```

PDF rendering uses an installed Chrome or Edge. If neither is present, run
`venv/Scripts/python -m playwright install chromium`, or set `DISABLE_PDF=true`.

Tests (offline, no API keys needed): `venv/Scripts/python -m pytest`

Benchmark (real API calls, ~4 min per run): `venv/Scripts/python -m scripts.benchmark --label NAME --runs 3`,
then `--compare storage/benchmarks/A.json storage/benchmarks/B.json` to compare speed and quality side by side.

## Using it

1. Start the server, open http://127.0.0.1:8000, fill in the customer's details.
2. **Upload the catalog PDF once.** It is indexed and reused by every later run, including
   after a restart. The line under the upload box says which catalog the next run will use;
   "forget it" there goes back to the built-in demo catalog. Re-uploading the same file is
   cheap (the content hash is compared, so it is not re-embedded), but leaving the box empty
   is faster.
3. **Watch the progress list** while it runs (~2 minutes): each step, its timing, and what it
   is doing ("Writing draft 2 (revising with reviewer feedback)").
4. **Read the yellow "This run used fallbacks" box**, if it appears. No box = every AI step
   worked. The box names the step and the reason, e.g. the AI writer being unavailable (so the
   copy is generic), or a step being answered by the backup provider.
5. Open the PDF, and use **Send** to email it (SMTP required; otherwise it is written to
   `storage/sent_emails/`).

### Getting good brochures

- **Catalog quality decides product quality.** Pages need selectable text - scanned/image-only
  PDFs index nothing (there is no OCR). Prices and specs are taken only from what a page
  states; nothing is invented, so an unpriced page renders "Price on request".
- **Hobbies drive retrieval.** One search runs per hobby, so "camping, cooking" searches the
  catalog twice and covers both. Vague hobbies retrieve vague pages.
- **Check Diagnostics** (bottom of the page) when results look wrong: `embeddings` must read
  `real` (`mock` means the Gemini key is not working and retrieval is meaningless), and "Last
  retrieval" shows which catalog pages matched, with scores.
- **Nothing reaches the brochure unchecked.** An instant check rejects branded names, acronyms
  and numbers that the specs do not contain, then the AI critic reviews the draft; rejected
  drafts are rewritten with that feedback (twice at most). Unsupported ranking reasons are
  dropped. If copy cannot be verified, claim-free generic copy is used and the run says so.

### Free-tier notes

Both providers are on free tiers, which are regularly overloaded or capped. A failed call is
retried once on the backup provider automatically, and the run reports when that happened.
If runs start showing fallback warnings constantly, switch `LLM_MODEL` (and/or the backup) to
another free model; `docs/IMPROVEMENTS.md` records which ones worked and when.

## How a request flows

Summary below; [docs/PIPELINE.md](docs/PIPELINE.md) explains each phase and the reasoning.

`POST /api/recommend` (form fields + optional catalog PDF) starts a background job and returns `202 {job_id, status_url}`; poll `GET /api/jobs/{job_id}` for per-step progress and, when `status` is `done`, the `result`. Steps:

1. **Ingest** the PDF: page text is embedded (Gemini) into an on-disk Qdrant index (`storage/qdrant`); page images are saved. Without a PDF, the previously indexed catalog is reused (it survives restarts); `DELETE /api/rag/catalog` forgets it. The app is single-user by design: one catalog at a time.
2. **profile** – deterministic rules assign a segment and budget tier (`USE_LLM_PROFILE=true` for an LLM call instead).
3. **recommend** – one retrieval query per hobby, LLM extracts products from the matched pages, LLM ranks them (rule-based scoring fallback). Ranking reasons citing terms absent from the specs and customer profile are removed.
4. **plan** – skipped by default; the writer structures the copy itself (`USE_LLM_PLANNER=true` for a separate outline call).
5. **copy** – LLM writes the copy; a deterministic spec-term check, the spec critic and the evaluator review it; rejected drafts are revised with the reviewer's feedback (up to 2 revisions), else claim-free generic copy is used.
6. **html** – Jinja2 template → `storage/temp_brochures/`.
7. **pdf** – headless Chromium via Playwright → `storage/generated_brochures/`.

Every fallback taken is returned in the response's `warnings` list and shown in the UI. Emails are
only sent when the user clicks **Send** (`POST /api/send-email`); without SMTP settings they are
logged to `storage/sent_emails/`.

## Layout

| Path | What |
|---|---|
| `app/main.py` | HTTP routes; serves the UI (`static/`) and generated files (`/storage`) |
| `app/pipeline/workflow.py` | Generic step runner: retries with backoff, compensation on failure |
| `app/pipeline/brochure.py` | The brochure steps and their fallbacks |
| `app/ai/` | LangChain chains (one per LLM task) and structured-output schemas |
| `app/rag/search.py` | PDF ingest, embeddings, vector search |
| `app/render/` | HTML compilation and PDF rendering |
| `app/delivery/email.py` | SMTP delivery |
| `templates/brochure.html` | The brochure template |

**How it works and why** - each phase, its cost, which LLM calls exist and which were
deliberately removed: [docs/PIPELINE.md](docs/PIPELINE.md).
Change history and the open backlog: [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md).
