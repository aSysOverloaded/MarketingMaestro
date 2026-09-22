"""HTML -> A4 PDF via Playwright (headless Chromium).

render_pdf() runs the browser in a child process (`python -m app.render.pdf <html> <pdf>`)
instead of in the server process. On Windows, uvicorn with --reload installs the selector
event loop policy, under which Playwright cannot spawn the browser; a child process gets a
clean interpreter. It also keeps a hung or crashed browser from taking the server with it.

Browser choice: an installed Chrome or Edge is tried first (no download needed), then
Playwright's bundled Chromium (`playwright install chromium`).
"""
import subprocess
import sys
from pathlib import Path

from app.config import SERVICE_DIR

RENDER_TIMEOUT_SECONDS = 90
_CHANNELS = ("chrome", "msedge", None)  # None = Playwright's bundled Chromium


def render_pdf(html_path: Path, pdf_path: Path) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "app.render.pdf", str(html_path.resolve()), str(pdf_path.resolve())],
        cwd=SERVICE_DIR,
        capture_output=True,
        text=True,
        timeout=RENDER_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PDF render failed: {(proc.stderr or proc.stdout).strip()[-800:]}")
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise RuntimeError("PDF render produced no output file")


def _render_in_this_process(html_path: Path, pdf_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    errors = []
    with sync_playwright() as p:
        browser = None
        for channel in _CHANNELS:
            try:
                browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
                break
            except Exception as e:
                errors.append(f"{channel or 'bundled chromium'}: {str(e).splitlines()[0]}")
        if browser is None:
            raise RuntimeError(
                "No usable browser for PDF rendering. Install Chrome/Edge or run "
                "`python -m playwright install chromium`. Tried: " + "; ".join(errors)
            )
        try:
            page = browser.new_page()
            # networkidle waits for the Tailwind CDN stylesheet and web fonts to load.
            page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30_000)
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        finally:
            browser.close()


if __name__ == "__main__":
    _render_in_this_process(Path(sys.argv[1]), Path(sys.argv[2]))
