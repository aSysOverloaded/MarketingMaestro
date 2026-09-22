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

## How a request flows

`POST /api/recommend` (form fields + optional catalog PDF) starts a background job and returns `202 {job_id, status_url}`; poll `GET /api/jobs/{job_id}` for per-step progress and, when `status` is `done`, the `result`. Steps:

1. **Ingest** the PDF: page text is embedded (Gemini) into an on-disk Qdrant index (`storage/qdrant`); page images are saved. Without a PDF, the previously indexed catalog is reused (it survives restarts); `DELETE /api/rag/catalog` forgets it. The app is single-user by design: one catalog at a time.
2. **profile** – LLM assigns a segment and budget tier (rule-based fallback).
3. **recommend** – one retrieval query per hobby, LLM extracts products from the matched pages, LLM ranks them (rule-based scoring fallback). Ranking reasons citing terms absent from the specs and customer profile are removed.
4. **plan** – LLM outlines the brochure sections.
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

Change history and the open backlog: [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md).
