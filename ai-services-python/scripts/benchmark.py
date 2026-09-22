"""Benchmark the real pipeline: speed per step and per LLM call, plus quality signals, so a
speed change can be judged on both.

Runs the full workflow against the configured model (real API calls, real cost) for a fixed
set of customer profiles on the built-in demo catalog, so every run sees the same input.

    python -m scripts.benchmark --label baseline --runs 3
    python -m scripts.benchmark --compare storage/benchmarks/baseline-*.json storage/benchmarks/fast-*.json

Free-model latency is noisy, so compare medians over several runs rather than single runs.
Quality must not regress: "ungrounded in final copy" should stay 0, and the fallback count
should not rise.
"""
import argparse
import json
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from app import diagnostics
from app.ai.grounding import find_ungrounded_terms
from app.catalog import CustomerInput
from app.config import settings
from app.pipeline import brochure

PROFILES = [
    CustomerInput(age=38, income=95000, family_size=3, hobbies=["camping", "cooking"], location="Portland, OR"),
    CustomerInput(age=29, income=60000, family_size=1, hobbies=["gaming", "fitness"], location="Austin, TX"),
    CustomerInput(age=52, income=240000, family_size=5, hobbies=["hosting dinners", "gardening"], location="Boston, MA"),
]


def run_once(customer: CustomerInput, use_catalog: bool = False) -> dict:
    diagnostics.reset_tokens()
    ctx = brochure.JobContext(job_id=f"job_{uuid.uuid4().hex}", trace_id="trace_bench", customer=customer,
                              catalog_indexed=use_catalog)
    steps = {}
    start = time.monotonic()
    error = None
    try:
        brochure.build_workflow().run(ctx, on_step=lambda name, status, ms: steps.__setitem__(name, ms) if ms is not None else None)
    except Exception as e:
        error = str(e)
    total_ms = int((time.monotonic() - start) * 1000)

    product = ctx.selected_products[0].model_dump(exclude={"hero_image", "page_number"}) if ctx.selected_products else {}
    final_copy = ctx.copy or {}
    return {
        "customer": customer.model_dump(),
        "total_ms": total_ms,
        "steps_ms": steps,
        "calls": ctx.review.get("calls", []),
        "tokens": diagnostics.get_tokens(),
        "revisions": ctx.review.get("revisions"),
        "critic_passed": (ctx.review.get("critic") or {}).get("passed"),
        "generic_copy": final_copy == brochure.fallback_copy(ctx.profile.segment) if ctx.profile else None,
        # Re-check the copy that actually shipped: this must stay empty.
        "ungrounded_in_final": find_ungrounded_terms(final_copy, product) if final_copy and product else [],
        "fallback_warnings": [w for w in ctx.warnings if w["step"] not in ("recommend", "pdf", "input") or "unavailable" in w["message"]],
        "warnings": ctx.warnings,
        "error": error,
    }


def summarize(label: str, runs: list) -> dict:
    def med(values):
        values = [v for v in values if v is not None]
        return int(statistics.median(values)) if values else None

    step_names = sorted({s for r in runs for s in r["steps_ms"]}, key=["profile", "recommend", "plan", "copy", "html", "pdf"].index)
    call_names = ["writer", "critic", "evaluator"]
    return {
        "label": label,
        "runs": len(runs),
        "total_ms": med([r["total_ms"] for r in runs]),
        "steps_ms": {s: med([r["steps_ms"].get(s) for r in runs]) for s in step_names},
        "calls_ms": {c: med([x["ms"] for r in runs for x in r["calls"] if x["call"] == c]) for c in call_names},
        "calls_per_run": {c: round(sum(1 for r in runs for x in r["calls"] if x["call"] == c) / len(runs), 1) for c in call_names},
        "tokens": {
            purpose: {
                "calls": round(statistics.mean([r["tokens"].get(purpose, {}).get("calls", 0) for r in runs]), 1),
                "input": int(statistics.median([r["tokens"].get(purpose, {}).get("input", 0) for r in runs])),
                "output": int(statistics.median([r["tokens"].get(purpose, {}).get("output", 0) for r in runs])),
            }
            for purpose in sorted({p for r in runs for p in r["tokens"]})
        },
        "avg_revisions": round(statistics.mean([r["revisions"] or 0 for r in runs]), 2),
        "runs_with_generic_copy": sum(1 for r in runs if r["generic_copy"]),
        "runs_with_fallbacks": sum(1 for r in runs if r["fallback_warnings"]),
        "runs_with_ungrounded_final_copy": sum(1 for r in runs if r["ungrounded_in_final"]),
        "errors": sum(1 for r in runs if r["error"]),
    }


def print_table(summaries: list) -> None:
    def fmt_ms(ms):
        return "-" if ms is None else f"{ms / 1000:.1f}s"

    rows = [("total", [fmt_ms(s["total_ms"]) for s in summaries])]
    for step in summaries[0]["steps_ms"]:
        rows.append((f"  step {step}", [fmt_ms(s["steps_ms"].get(step)) for s in summaries]))
    for call in summaries[0]["calls_ms"]:
        rows.append((f"  call {call} (median)", [fmt_ms(s["calls_ms"].get(call)) for s in summaries]))
        rows.append((f"  call {call} (per run)", [str(s["calls_per_run"].get(call)) for s in summaries]))
    for purpose, t in summaries[0].get("tokens", {}).items():
        rows.append((f"  tokens {purpose} (in/out)", [
            f"{s.get('tokens', {}).get(purpose, {}).get('input', 0)}/{s.get('tokens', {}).get(purpose, {}).get('output', 0)}"
            for s in summaries]))
    for key in ["avg_revisions", "runs_with_generic_copy", "runs_with_fallbacks", "runs_with_ungrounded_final_copy", "errors"]:
        rows.append((key, [str(s[key]) for s in summaries]))

    header = ["", *[f"{s['label']} (n={s['runs']})" for s in summaries]]
    widths = [max(len(r[0]) for r in rows + [(header[0], [])])] + [max(len(h), 10) for h in header[1:]]
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)))
    for name, values in rows:
        print("  ".join([name.ljust(widths[0]), *[v.ljust(w) for v, w in zip(values, widths[1:])]]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="run")
    parser.add_argument("--runs", type=int, default=3, help="runs in total, cycling through the fixed profiles")
    parser.add_argument("--no-pdf", action="store_true", help="skip PDF rendering (it is not an LLM cost)")
    parser.add_argument("--catalog", metavar="PDF", help="ingest this catalog first and run against it (default: built-in demo catalog)")
    parser.add_argument("--compare", nargs="+", metavar="RESULT_JSON", help="print saved results side by side")
    args = parser.parse_args()

    if args.compare:
        print_table([json.loads(Path(p).read_text(encoding="utf-8"))["summary"] for p in args.compare])
        return

    if args.no_pdf:
        settings.disable_pdf = True
    print(f"model={settings.llm_model} critic_model={settings.llm_critic_model or '(same)'} runs={args.runs}", flush=True)

    use_catalog = False
    if args.catalog:
        from app.rag.search import ingest_pdf
        pdf_bytes = Path(args.catalog).read_bytes()
        start = time.monotonic()
        result = ingest_pdf(pdf_bytes, job_id="job_bench", filename=Path(args.catalog).name)
        use_catalog = result["indexed_pages"] > 0
        print(f"ingested {args.catalog}: {result['indexed_pages']} pages, reused={result.get('reused')}, "
              f"{time.monotonic() - start:.1f}s, embeddings={diagnostics.get_status('embeddings')['mode']}", flush=True)

    runs = []
    for i in range(args.runs):
        customer = PROFILES[i % len(PROFILES)]
        result = run_once(customer, use_catalog=use_catalog)
        runs.append(result)
        print(f"run {i + 1}/{args.runs}: {result['total_ms'] / 1000:.1f}s revisions={result['revisions']} "
              f"generic_copy={result['generic_copy']} ungrounded={result['ungrounded_in_final']} error={result['error']}", flush=True)

    summary = summarize(args.label, runs)
    summary["catalog"] = Path(args.catalog).name if args.catalog else "demo"
    out_dir = settings.storage_dir / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"summary": summary, "model": settings.llm_model, "runs": runs}, indent=2), encoding="utf-8")
    print()
    print_table([summary])
    print(f"\nsaved {out}")


if __name__ == "__main__":
    sys.exit(main())
