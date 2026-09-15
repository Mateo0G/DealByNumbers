"""Email a generated one-pager PDF to a fixed recipient.

Used by the web app's background worker: after a deck is analyzed and rendered,
a copy of the PDF is mailed so a record of every generated document lands in an
inbox.

Two transports are supported, chosen from the environment:

  * Resend (HTTPS API, port 443) -- used when ``RESEND_API_KEY`` is set. This is
    the reliable choice on hosts like Railway that block outbound SMTP ports
    (a plain SMTP connect there fails with "Network is unreachable").
  * SMTP (STARTTLS) -- used otherwise, when SMTP_HOST/USER/PASSWORD are set.
    Fine for local runs or hosts that allow outbound SMTP.

Sending is best-effort from the caller's point of view: the PDF has already been
written to the results store by the time we get here, so a mail failure is
recorded on the job rather than losing the document. Callers decide how loud to
be about ``MailError``.
"""

from __future__ import annotations

import base64
import json
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

# Where generated documents are always copied. Overridable via $MAIL_TO for
# testing or a different destination, but defaults to the address the feature
# was built for.
DEFAULT_RECIPIENT = "info@tencapital.group"

# Resend's shared onboarding sender. It works with no domain verification, but
# Resend then only allows delivery to the account owner's own email address on
# the Resend account. Override with $RESEND_FROM once a domain is verified
# (e.g. "One-Pagers <noreply@yourdomain.com>") to send to any recipient.
DEFAULT_RESEND_FROM = "onboarding@resend.dev"

RESEND_ENDPOINT = "https://api.resend.com/emails"


class MailError(Exception):
    """Raised when the PDF could not be emailed."""


def _smtp_configured() -> bool:
    return bool(
        os.getenv("SMTP_HOST")
        and os.getenv("SMTP_USER")
        and os.getenv("SMTP_PASSWORD")
    )


def mail_enabled() -> bool:
    """True when at least one transport is configured enough to attempt a send."""
    return bool(os.getenv("RESEND_API_KEY")) or _smtp_configured()


def email_document(
    pdf_bytes: bytes,
    filename: str,
    *,
    company_name: str = "the company",
    recipient: str | None = None,
) -> str:
    """Email ``pdf_bytes`` as an attachment to ``recipient``.

    Transport selection:
        RESEND_API_KEY set -> send via the Resend HTTPS API (recommended on
                              Railway and other SMTP-blocked hosts).
        else               -> send via SMTP (SMTP_HOST/PORT/USER/PASSWORD).

    Common vars:
        MAIL_TO        optional recipient override, defaults to DEFAULT_RECIPIENT

    Resend vars:
        RESEND_API_KEY required to use Resend
        RESEND_FROM    optional From, defaults to onboarding@resend.dev

    SMTP vars:
        SMTP_HOST      required, e.g. ``smtp.gmail.com``
        SMTP_PORT      optional, default 587 (STARTTLS)
        SMTP_USER      required, the authenticating account / From address
        SMTP_PASSWORD  required, app password or SMTP password
        SMTP_FROM      optional From override, defaults to SMTP_USER

    Returns:
        The address the document was sent to.

    Raises:
        MailError: no transport is configured, or the send failed.
    """
    to_addr = recipient or os.getenv("MAIL_TO") or DEFAULT_RECIPIENT
    subject = f"Investor one-pager: {company_name}"
    body = (
        f"Attached is the generated one-page investor summary for {company_name}.\n\n"
        f"File: {filename}\n\n"
        "— deal-by-numbers"
    )

    if os.getenv("RESEND_API_KEY"):
        return _send_via_resend(pdf_bytes, filename, subject, body, to_addr)
    if _smtp_configured():
        return _send_via_smtp(pdf_bytes, filename, subject, body, to_addr)

    raise MailError(
        "Email is not configured. Set RESEND_API_KEY (recommended on Railway) "
        "or SMTP_HOST/SMTP_USER/SMTP_PASSWORD (see .env.example)."
    )


def _send_via_resend(
    pdf_bytes: bytes, filename: str, subject: str, body: str, to_addr: str
) -> str:
    """Send over the Resend HTTPS API (port 443)."""
    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("RESEND_FROM") or DEFAULT_RESEND_FROM

    payload = {
        "from": sender,
        "to": [to_addr],
        "subject": subject,
        "text": body,
        "attachments": [
            {
                "filename": filename,
                "content": base64.b64encode(pdf_bytes).decode("ascii"),
            }
        ],
    }
    req = urllib.request.Request(
        RESEND_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # api.resend.com is behind Cloudflare, which bans urllib's default
            # "Python-urllib/x.y" User-Agent (Cloudflare error 1010). Send a
            # normal UA so the request isn't flagged as a bad bot signature.
            "User-Agent": "deal-by-numbers/1.0 (+https://github.com/Mateo0G/DealByNumbers)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise MailError(f"Resend API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MailError(f"Resend request failed: {exc.reason}") from exc

    return to_addr


def _send_via_smtp(
    pdf_bytes: bytes, filename: str, subject: str, body: str, to_addr: str
) -> str:
    """Send over SMTP with STARTTLS."""
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    port = int(os.getenv("SMTP_PORT", "587"))
    sender = os.getenv("SMTP_FROM") or user

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=filename,
    )

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP send failed: {exc}") from exc

    return to_addr
