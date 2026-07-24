"""Email a generated one-pager PDF to a fixed recipient over SMTP.

Used by the web app's background worker: after a deck is analyzed and rendered,
a copy of the PDF is mailed so a record of every generated document lands in an
inbox. Transport is plain SMTP (STARTTLS), configured from environment variables
so it works with a Gmail app password or any SMTP provider.

Sending is best-effort from the caller's point of view: the PDF has already been
written to the results store by the time we get here, so a mail failure is
recorded on the job rather than losing the document. Callers decide how loud to
be about ``MailError``.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

# Where generated documents are always copied. Overridable via $MAIL_TO for
# testing or a different destination, but defaults to the address the feature
# was built for.
DEFAULT_RECIPIENT = "mateo.ghercioiu@gmail.com"


class MailError(Exception):
    """Raised when the PDF could not be emailed."""


def mail_enabled() -> bool:
    """True when SMTP is configured enough to attempt a send."""
    return bool(
        os.getenv("SMTP_HOST")
        and os.getenv("SMTP_USER")
        and os.getenv("SMTP_PASSWORD")
    )


def email_document(
    pdf_bytes: bytes,
    filename: str,
    *,
    company_name: str = "the company",
    recipient: str | None = None,
) -> str:
    """Email ``pdf_bytes`` as an attachment to ``recipient``.

    Reads SMTP settings from the environment:

        SMTP_HOST      required, e.g. ``smtp.gmail.com``
        SMTP_PORT      optional, default 587 (STARTTLS)
        SMTP_USER      required, the authenticating account / From address
        SMTP_PASSWORD  required, app password or SMTP password
        SMTP_FROM      optional From override, defaults to SMTP_USER
        MAIL_TO        optional recipient override, defaults to DEFAULT_RECIPIENT

    Args:
        pdf_bytes:    the rendered PDF content to attach.
        filename:     attachment file name, e.g. ``acme_onepager.pdf``.
        company_name: used in the subject/body for context.
        recipient:    explicit recipient; falls back to $MAIL_TO then
                      DEFAULT_RECIPIENT.

    Returns:
        The address the document was sent to.

    Raises:
        MailError: configuration is missing or the SMTP conversation failed.
    """
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    if not (host and user and password):
        raise MailError(
            "SMTP is not configured. Set SMTP_HOST, SMTP_USER and SMTP_PASSWORD "
            "(see .env.example) to email generated documents."
        )

    port = int(os.getenv("SMTP_PORT", "587"))
    sender = os.getenv("SMTP_FROM", user)
    to_addr = recipient or os.getenv("MAIL_TO") or DEFAULT_RECIPIENT

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = f"Investor one-pager: {company_name}"
    msg.set_content(
        f"Attached is the generated one-page investor summary for {company_name}.\n\n"
        f"File: {filename}\n\n"
        "— deal-by-numbers"
    )
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
