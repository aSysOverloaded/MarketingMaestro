"""In-memory registry of brochure jobs, so a run can happen in the background while the UI
polls GET /api/jobs/{id} for real step-by-step progress. Single-user by design: a short
history is kept in process memory and lost on restart.
"""
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional

MAX_JOBS = 20

STEP_LABELS = {
    "ingest": "Indexing the uploaded catalog",
    "profile": "Profiling the customer",
    "recommend": "Finding and ranking products",
    "plan": "Planning the brochure",
    "copy": "Writing and fact-checking the copy",
    "product_copy": "Writing copy for each product",
    "html": "Building the brochure",
    "pdf": "Rendering the PDF",
}


class Job:
    def __init__(self, job_id: str, step_names: List[str]):
        self.id = job_id
        self.status = "running"  # running | done | failed
        self.steps = [{"name": n, "label": STEP_LABELS.get(n, n), "status": "pending", "ms": None} for n in step_names]
        self.note: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def step_update(self, name: str, status: str, ms: Optional[int]) -> None:
        with self._lock:
            for step in self.steps:
                if step["name"] == name:
                    step["status"], step["ms"] = status, ms
            self.note = None  # notes belong to the step that was running

    def set_note(self, note: str) -> None:
        with self._lock:
            self.note = note

    def finish(self, result: Dict[str, Any]) -> None:
        with self._lock:
            self.status, self.result = "done", result

    def fail(self, error: Dict[str, Any]) -> None:
        with self._lock:
            self.status, self.error = "failed", error

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.id,
                "status": self.status,
                "steps": [dict(s) for s in self.steps],
                "note": self.note,
                "result": self.result,
                "error": self.error,
            }


_jobs: "OrderedDict[str, Job]" = OrderedDict()
_registry_lock = threading.Lock()


def create(job_id: str, step_names: List[str]) -> Job:
    job = Job(job_id, step_names)
    with _registry_lock:
        _jobs[job_id] = job
        while len(_jobs) > MAX_JOBS:
            _jobs.popitem(last=False)
    return job


def get(job_id: str) -> Optional[Job]:
    with _registry_lock:
        return _jobs.get(job_id)
