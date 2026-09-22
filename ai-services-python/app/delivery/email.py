"""Brochure delivery over SMTP, or a local log file when SMTP is not configured."""
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from app.config import settings

logger = logging.getLogger("delivery.email")

SUBJECT = "Your Personalized Product Brochure & Recommendations"
BODY = (
    "Dear Customer,\n\n"
    "Please find attached your custom-compiled product recommendation brochure.\n\n"
    "Warm regards,\nSales & Marketing Team"
)


def send_brochure(to_email: str, pdf_path: Path, log_dir: Path) -> dict:
    """Returns {"sent": bool, "message": str}. sent is True only for a real SMTP delivery."""
    if not settings.has_smtp:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"email_{pdf_path.stem}.log"
        log_path.write_text(
            f"Timestamp: {datetime.now(timezone.utc).isoformat()}\nTo: {to_email}\nSubject: {SUBJECT}\n"
            f"Attachment: {pdf_path} ({pdf_path.stat().st_size} bytes)\n\n{BODY}\n",
            encoding="utf-8",
        )
        logger.info(f"SMTP not configured; logged email to {log_path}")
        return {
            "sent": False,
            "message": "Demo mode: SMTP is not configured, so the email was logged locally instead of sent. "
                       "Set SMTP_HOST / SMTP_USER / SMTP_PASS to send real emails.",
        }

    msg = EmailMessage()
    msg["From"] = f"Marketing Agent <{settings.smtp_user}>"
    msg["To"] = to_email
    msg["Subject"] = SUBJECT
    msg.set_content(BODY)
    msg.add_attachment(pdf_path.read_bytes(), maintype="application", subtype="pdf", filename=pdf_path.name)

    # Certificates are verified on both paths (the old Go client skipped verification on 465).
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=30) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_pass)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.smtp_user, settings.smtp_pass)
            smtp.send_message(msg)

    logger.info(f"sent brochure {pdf_path.name} to {to_email} via {settings.smtp_host}")
    return {"sent": True, "message": f"Email sent successfully to {to_email}!"}
