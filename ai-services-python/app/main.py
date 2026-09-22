import logging
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import jobs
from app.catalog import CustomerInput
from app.config import SERVICE_DIR, log_startup_config, settings
from app.delivery.email import send_brochure
from app.observability import log_stage
from app.pipeline.brochure import JobContext, build_workflow, pdf_path_for
from app.pipeline.workflow import WorkflowError
from app.rag.search import clear_catalog, get_catalog, get_stats, ingest_pdf, search_catalog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("main")

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
# uuid4 hex, not a timestamp: job ids gate access to the generated PDF (download and
# /api/send-email), so they must not be guessable.
JOB_ID_PATTERN = re.compile(r"^job_[0-9a-f]{32}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEFAULTS = {"age": 32, "income": 120000.0, "family_size": 4, "location": "Seattle, WA", "hobbies": ["trekking", "camping"]}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log_startup_config()
    yield


app = FastAPI(title="Marketing Agent", lifespan=lifespan)
settings.storage_dir.mkdir(parents=True, exist_ok=True)


@app.post("/api/recommend", status_code=202)
def recommend(
    age: Optional[int] = Form(None),
    income: Optional[float] = Form(None),
    family_size: Optional[int] = Form(None),
    location: str = Form(""),
    hobbies: str = Form(""),
    brochure: Optional[UploadFile] = File(None),
):
    """Start a brochure job in the background and return its id right away. Poll
    GET /api/jobs/{job_id} for step-by-step progress and, once done, the result."""
    job_id = f"job_{uuid.uuid4().hex}"
    trace_id = f"trace_{uuid.uuid4().hex[:16]}"

    submitted = {
        "age": age or None,
        "income": income or None,
        "family_size": family_size or None,
        "location": location.strip() or None,
        "hobbies": [h.strip() for h in hobbies.split(",") if h.strip()] or None,
    }
    customer = CustomerInput(**{k: (DEFAULTS[k] if v is None else v) for k, v in submitted.items()})
    ctx = JobContext(job_id=job_id, trace_id=trace_id, customer=customer)
    defaulted = [k for k, v in submitted.items() if v is None]
    if defaulted:
        # Previously silent: empty fields quietly became a 32-year-old in Seattle who likes trekking.
        ctx.warnings.append({"step": "input", "message": f"Used default values for: {', '.join(defaulted)}."})

    # Read the upload inside the request - the file handle is closed once the response is sent.
    upload = None
    if brochure is not None and brochure.filename:
        pdf_bytes = brochure.file.read(MAX_UPLOAD_BYTES + 1)
        if len(pdf_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Brochure PDF exceeds the 15 MB limit.")
        upload = (pdf_bytes, brochure.filename)

    workflow = build_workflow()
    job = jobs.create(job_id, (["ingest"] if upload else []) + [step.name for step in workflow.steps])
    ctx.on_note = job.set_note
    threading.Thread(target=_run_job, args=(job, ctx, workflow, upload), daemon=True).start()
    return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job (jobs are kept in memory and lost on restart).")
    return job.to_dict()


def _run_job(job: jobs.Job, ctx: JobContext, workflow, upload) -> None:
    try:
        catalog_source = _prepare_catalog(job, ctx, upload)
        workflow.run(ctx, on_step=job.step_update)
        job.finish(_result(ctx, catalog_source))
    except WorkflowError as e:
        job.fail({"failed_step": e.step, "error": str(e.cause), "warnings": ctx.warnings})
    except Exception as e:  # never leave a job stuck in "running"
        log_stage(logger, ctx.job_id, "job", f"crashed: {e}", level="error")
        job.fail({"failed_step": None, "error": str(e), "warnings": ctx.warnings})


def _prepare_catalog(job: jobs.Job, ctx: JobContext, upload) -> Optional[str]:
    """Index the uploaded PDF, or reuse the already-indexed catalog. Returns the catalog source."""
    if upload is None:
        if get_catalog():
            # No file this time: keep using the catalog already indexed (it persists across
            # runs and restarts). Previously this silently fell back to the demo catalog.
            ctx.catalog_indexed = True
            return "reused"
        return None

    pdf_bytes, filename = upload
    job.step_update("ingest", "running", None)
    start = time.monotonic()
    try:
        result = ingest_pdf(pdf_bytes, job_id=ctx.job_id, filename=filename)
        ctx.catalog_indexed = result["indexed_pages"] > 0
        if not ctx.catalog_indexed:
            ctx.warn("ingest", "The uploaded PDF has no extractable text (scanned images only?).")
    except Exception as e:
        ctx.warn("ingest", f"Failed to read the uploaded PDF ({e}).")
    job.step_update("ingest", "done", int((time.monotonic() - start) * 1000))
    return "uploaded" if ctx.catalog_indexed else None


def _result(ctx: JobContext, catalog_source: Optional[str]) -> dict:
    products = {p.id: p for p in ctx.selected_products}
    return {
        "success": True,
        "job_id": ctx.job_id,
        "trace_id": ctx.trace_id,
        "segment": ctx.profile.segment,
        "budget_tier": ctx.profile.budget_tier,
        "recommendations": [{**r.model_dump(), "model": products[r.product_id].model} for r in ctx.recommendations],
        "pdf_url": f"/storage/generated_brochures/{ctx.pdf_path.name}" if ctx.pdf_path else None,
        "catalog": {**get_catalog(), "source": catalog_source} if catalog_source else None,
        "copy_review": ctx.review,
        "rag_debug": ctx.rag_debug,
        "warnings": ctx.warnings,
    }


@app.post("/api/send-email")
def send_email(job_id: str = Form(...), email: str = Form(...)):
    if not JOB_ID_PATTERN.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id.")
    if not EMAIL_PATTERN.match(email.strip()):
        raise HTTPException(status_code=400, detail="Invalid email address.")

    pdf_path = pdf_path_for(job_id)
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="PDF brochure not found. Please regenerate the recommendation first.")

    try:
        result = send_brochure(email.strip(), pdf_path, settings.storage_dir / "sent_emails")
    except Exception as e:
        log_stage(logger, job_id, "email", f"SMTP send failed: {e}", level="error")
        raise HTTPException(status_code=502, detail=f"Failed to send email: {e}")
    return {"success": True, **result}


@app.get("/api/rag/stats")
def stats_endpoint():
    try:
        return {"reachable": True, **get_stats()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/rag/catalog")
def forget_catalog():
    """Drop the indexed catalog so the next run uses the built-in demo catalog."""
    clear_catalog()
    return {"success": True}


class SearchRequest(BaseModel):
    query: str
    limit: int = 3


@app.post("/api/rag/search")
def search_endpoint(payload: SearchRequest):
    # Debugging aid for retrieval quality; the pipeline calls search_catalog directly.
    return {"matches": search_catalog(payload.query, payload.limit, job_id="debug")}


# Mounted last so the /api routes above take precedence.
app.mount("/storage", StaticFiles(directory=settings.storage_dir), name="storage")
app.mount("/", StaticFiles(directory=SERVICE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
