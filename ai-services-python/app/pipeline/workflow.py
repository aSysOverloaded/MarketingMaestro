"""Minimal sequential workflow runner: ordered steps, per-step retries with backoff, and
compensation (rollback) of completed steps in reverse order when a later step fails.

Ported from the former Go orchestrator. Deliberately generic - it knows nothing about
brochures; the steps and their shared context live in app/pipeline/brochure.py.
"""
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from app.observability import log_stage

logger = logging.getLogger("pipeline.workflow")


@dataclass
class Step:
    name: str
    run: Callable[[Any], None]
    # Extra attempts after the first. Most steps are 0: they handle LLM failure with their
    # own fallback, and the chat model already retries transport errors - retrying here on
    # top of that is what used to stack into multi-minute requests.
    retries: int = 0
    backoff_seconds: float = 1.0  # exponential: backoff * 2^(attempt-1)
    compensate: Optional[Callable[[Any], None]] = None


class WorkflowError(Exception):
    def __init__(self, step: str, cause: BaseException):
        super().__init__(f"step {step} failed: {cause}")
        self.step = step
        self.cause = cause


class Workflow:
    def __init__(self, name: str, steps: List[Step], sleep: Callable[[float], None] = time.sleep):
        self.name = name
        self.steps = steps
        self._sleep = sleep  # injectable so tests don't wait on backoff

    def run(self, ctx: Any) -> None:
        job_id = getattr(ctx, "job_id", "unknown")
        completed: List[Step] = []
        log_stage(logger, job_id, "workflow", f"starting {self.name}")

        for step in self.steps:
            start = time.monotonic()
            attempt = 0
            while True:
                try:
                    step.run(ctx)
                    break
                except Exception as e:
                    attempt += 1
                    if attempt > step.retries:
                        log_stage(logger, job_id, step.name, f"failed after {attempt} attempt(s): {e}", level="error")
                        self._compensate(ctx, completed)
                        raise WorkflowError(step.name, e) from e
                    delay = step.backoff_seconds * (2 ** (attempt - 1))
                    log_stage(logger, job_id, step.name, f"attempt {attempt} failed: {e}; retrying in {delay:.1f}s", level="warning")
                    self._sleep(delay)

            completed.append(step)
            log_stage(logger, job_id, step.name, f"ok ({int((time.monotonic() - start) * 1000)} ms)")

        log_stage(logger, job_id, "workflow", f"{self.name} completed")

    def _compensate(self, ctx: Any, completed: List[Step]) -> None:
        job_id = getattr(ctx, "job_id", "unknown")
        for step in reversed(completed):
            if step.compensate is None:
                continue
            try:
                step.compensate(ctx)
                log_stage(logger, job_id, step.name, "compensated")
            except Exception as e:
                # Keep rolling back the remaining steps even if one compensation fails.
                log_stage(logger, job_id, step.name, f"compensation failed: {e}", level="warning")
