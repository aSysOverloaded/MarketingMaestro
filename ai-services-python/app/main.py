import os
import logging
from fastapi import FastAPI, HTTPException, UploadFile, File, Header
from pydantic import BaseModel
from typing import List, Dict, Any, Optional

from app.ai.planner import generate_plan
from app.ai.writer import generate_copy
from app.ai.critic import audit_copy
from app.ai.evaluator import evaluate_copy
from app.rag.search import ingest_pdf, search_catalog, get_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

app = FastAPI(title="AI Services Python Sidecar")

class PlanRequest(BaseModel):
    segment: str
    recommendation: Dict[str, Any]

class PlanResponse(BaseModel):
    sections: List[Dict[str, Any]]

class WriteRequest(BaseModel):
    segment: str
    sections: List[Dict[str, Any]]
    candidate: Dict[str, Any]

class WriteResponse(BaseModel):
    headline: str
    subheadline: str
    paragraphs: List[str]
    cta: str

class CriticRequest(BaseModel):
    copy: Dict[str, Any]
    candidate: Dict[str, Any]

class CriticResponse(BaseModel):
    passed: bool
    feedback: str

class EvaluateRequest(BaseModel):
    copy: Dict[str, Any]

class EvaluateResponse(BaseModel):
    passed: bool
    banned_words_found: List[str]
    tone_assessment: str
    score: int


@app.post("/api/plan", response_model=PlanResponse)
def plan_endpoint(payload: PlanRequest):
    try:
        sections = generate_plan(payload.segment, payload.recommendation)
        return PlanResponse(sections=sections)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/write", response_model=WriteResponse)
def write_endpoint(payload: WriteRequest):
    try:
        copy_data = generate_copy(payload.segment, payload.sections, payload.candidate)
        return WriteResponse(**copy_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/critic", response_model=CriticResponse)
def critic_endpoint(payload: CriticRequest):
    try:
        result = audit_copy(payload.copy, payload.candidate)
        return CriticResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/evaluate", response_model=EvaluateResponse)
def evaluate_endpoint(payload: EvaluateRequest):
    try:
        result = evaluate_copy(payload.copy)
        return EvaluateResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class SearchRequest(BaseModel):
    query: str
    limit: int = 3

@app.post("/api/rag/ingest")
async def ingest_endpoint(file: UploadFile = File(...), x_job_id: Optional[str] = Header(default="unknown")):
    try:
        pdf_bytes = await file.read()
        res = ingest_pdf(pdf_bytes, job_id=x_job_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rag/search")
def search_endpoint(payload: SearchRequest, x_job_id: Optional[str] = Header(default="unknown")):
    try:
        matches = search_catalog(payload.query, payload.limit, job_id=x_job_id)
        return {"matches": matches}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/rag/stats")
def stats_endpoint():
    try:
        return get_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
