"""Email and PDF rendering: the two paths that reach the outside world, and the two whose
failures land in front of a customer (no brochure, or no email) rather than in a test run."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.delivery import email as delivery
from app.render import pdf as pdf_render


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


@pytest.fixture
def smtp_configured(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_user", "sales@example.com")
    monkeypatch.setattr(settings, "smtp_pass", "secret")
    FakeSMTP.instances = []


def _pdf_file(tmp_path) -> Path:
    path = tmp_path / "brochure_job_x.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


def test_without_smtp_the_email_is_logged_not_sent(tmp_path, isolated_storage):
    result = delivery.send_brochure("a@b.co", _pdf_file(tmp_path), tmp_path / "sent")

    assert result["sent"] is False and "not configured" in result["message"]
    logged = next((tmp_path / "sent").glob("*.log")).read_text(encoding="utf-8")
    assert "a@b.co" in logged


def test_starttls_is_used_on_the_default_port(tmp_path, smtp_configured, monkeypatch):
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(delivery.smtplib, "SMTP", FakeSMTP)

    result = delivery.send_brochure("customer@example.com", _pdf_file(tmp_path), tmp_path / "sent")

    smtp = FakeSMTP.instances[0]
    assert result["sent"] is True
    assert smtp.started_tls and smtp.logged_in == ("sales@example.com", "secret")
    assert smtp.sent["To"] == "customer@example.com"
    attachment = list(smtp.sent.iter_attachments())[0]
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_content() == b"%PDF-1.4 fake"


def test_port_465_uses_implicit_tls_with_certificate_checking(tmp_path, smtp_configured, monkeypatch):
    monkeypatch.setattr(settings, "smtp_port", 465)
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", FakeSMTP)

    delivery.send_brochure("customer@example.com", _pdf_file(tmp_path), tmp_path / "sent")

    smtp = FakeSMTP.instances[0]
    assert smtp.port == 465 and not smtp.started_tls
    assert smtp.context is not None  # certificates are verified; the old Go client skipped that


def test_a_failing_smtp_server_raises_rather_than_reporting_success(tmp_path, smtp_configured, monkeypatch):
    class Refusing(FakeSMTP):
        def login(self, user, password):
            raise RuntimeError("authentication failed")

    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(delivery.smtplib, "SMTP", Refusing)

    with pytest.raises(RuntimeError, match="authentication failed"):
        delivery.send_brochure("customer@example.com", _pdf_file(tmp_path), tmp_path / "sent")


def test_the_pdf_renderer_runs_a_child_process_and_checks_its_output(tmp_path, monkeypatch):
    html, pdf = tmp_path / "in.html", tmp_path / "out.pdf"
    html.write_text("<html></html>", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        pdf.write_bytes(b"%PDF-1.4 rendered")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_render.subprocess, "run", fake_run)
    pdf_render.render_pdf(html, pdf)

    cmd, kwargs = calls[0]
    assert cmd[1:3] == ["-m", "app.render.pdf"]  # a child process, not this one
    assert kwargs["timeout"] == pdf_render.RENDER_TIMEOUT_SECONDS


def test_a_failed_render_raises_with_the_child_error(tmp_path, monkeypatch):
    html, pdf = tmp_path / "in.html", tmp_path / "out.pdf"
    html.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(pdf_render.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no browser found"))

    with pytest.raises(RuntimeError, match="no browser found"):
        pdf_render.render_pdf(html, pdf)


def test_an_empty_pdf_is_treated_as_a_failure(tmp_path, monkeypatch):
    html, pdf = tmp_path / "in.html", tmp_path / "out.pdf"
    html.write_text("<html></html>", encoding="utf-8")

    def empty_run(cmd, **kwargs):
        pdf.write_bytes(b"")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_render.subprocess, "run", empty_run)
    with pytest.raises(RuntimeError, match="no output"):
        pdf_render.render_pdf(html, pdf)


def test_an_installed_browser_is_preferred_and_the_bundled_one_is_last_resort():
    tried = []

    class FakeChromium:
        def launch(self, channel=None):
            tried.append(channel)
            if channel in ("chrome", "msedge"):
                raise RuntimeError(f"{channel} is not installed")
            return "bundled-browser"

    assert pdf_render.launch_browser(SimpleNamespace(chromium=FakeChromium())) == "bundled-browser"
    assert tried == ["chrome", "msedge", None]


def test_no_browser_at_all_explains_how_to_fix_it():
    class NoBrowsers:
        def launch(self, channel=None):
            raise RuntimeError("Executable does not exist")

    with pytest.raises(RuntimeError, match="playwright install chromium"):
        pdf_render.launch_browser(SimpleNamespace(chromium=NoBrowsers()))
