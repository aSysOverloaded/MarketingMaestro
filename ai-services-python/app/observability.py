"""Shared structured-logging helper.

Format: [job=<id>] [<stage>] <message>

This exact format is what lets every log line for one request - workflow
runner, each step, RAG, LLM calls - be grepped by job id. Do not change the
format without checking anywhere that greps or parses these lines.
"""
import logging


def log_stage(logger: logging.Logger, job_id: str, stage: str, message: str, level: str = "info") -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(f"[job={job_id}] [{stage}] {message}")
