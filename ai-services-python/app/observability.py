"""Shared structured-logging helper.

Format: [job=<id>] [<stage>] <message>

This exact format is what lets a job's logs be grepped consistently across
this service and correlated with the Go backend's [JobID: ...] tagged logs
for the same request. Do not change the format without checking anywhere
that greps or parses these lines.
"""
import logging


def log_stage(logger: logging.Logger, job_id: str, stage: str, message: str, level: str = "info") -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(f"[job={job_id}] [{stage}] {message}")
