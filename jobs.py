"""Disk-backed background job store for deck -> one-pager generation.

Why disk and not an in-process dict: Railway runs this app under gunicorn with
multiple workers. The worker that accepts ``POST /generate`` and starts the
background thread is often NOT the worker that later serves ``GET /status/<id>``
or ``GET /download/<id>``. A per-process dict would be invisible to sibling
workers, so a poll could hit a worker that has never heard of the job. The
filesystem is shared by every worker on the instance, so we key job state on it:

    <STORE_DIR>/<job_id>.json   -- status record (processing | ready | error)
    <STORE_DIR>/<job_id>.pdf    -- the rendered document, once ready

The heavy work (extract -> analyze via Claude -> render -> email) runs in a
daemon thread. ``POST /generate`` returns the job id immediately, so the HTTP
request is never held open long enough for Railway's edge proxy to time it out.
The browser then polls the fast ``/status`` endpoint until the job is ready.

Results are ephemeral: they live on the instance's local disk and are swept
after a TTL (and are lost on redeploy/restart). That is fine here -- downloads
happen within seconds/minutes of generation, and every document is also emailed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from analyze import AnalysisError, analyze
from extract import UnsupportedFileError, extract
from mailer import MailError, email_document, mail_enabled
from render import render

# Where job status files and rendered PDFs are stored. Overridable via env so a
# deployment can point at a mounted volume if it wants durability.
STORE_DIR = Path(os.environ.get("JOB_STORE_DIR", Path(tempfile.gettempdir()) / "dbn_jobs"))

# Sweep job artifacts older than this many seconds. Downloads are expected to
# happen shortly after generation; anything older is stale and safe to drop.
TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", str(6 * 60 * 60)))

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_ERROR = "error"


def _status_path(job_id: str) -> Path:
    return STORE_DIR / f"{job_id}.json"


def _pdf_path(job_id: str) -> Path:
    return STORE_DIR / f"{job_id}.pdf"


def _write_status(job_id: str, record: dict) -> None:
    """Atomically write a job's status record.

    Written to a temp file then renamed so a concurrent ``/status`` poll never
    reads a half-written JSON file. On Windows, ``os.replace`` can transiently
    fail with a PermissionError if a reader holds the target file open at that
    instant, so retry briefly before giving up.
    """
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_DIR / f"{job_id}.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(record), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, _status_path(job_id))
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def read_status(job_id: str) -> dict | None:
    """Return the job's status record, or None if the job id is unknown."""
    path = _status_path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A transient read during the atomic replace, or a corrupt file. The
        # caller should treat this as "still working" and poll again.
        return None


def pdf_bytes(job_id: str) -> bytes | None:
    """Return the rendered PDF bytes for a ready job, or None if unavailable."""
    path = _pdf_path(job_id)
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _sweep_expired() -> None:
    """Delete status/PDF artifacts older than the TTL. Best-effort."""
    if not STORE_DIR.is_dir():
        return
    cutoff = time.time() - TTL_SECONDS
    for entry in STORE_DIR.iterdir():
        try:
            if entry.suffix in (".json", ".pdf") and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            pass


def _run_job(job_id: str, in_path: str, stem: str, base_record: dict) -> None:
    """Background worker: extract -> analyze -> render -> store -> email."""
    record = dict(base_record)

    def fail(message: str) -> None:
        record.update(status=STATUS_ERROR, error=message, finished=time.time())
        _write_status(job_id, record)

    try:
        try:
            deck_text = extract(in_path)
        except UnsupportedFileError as exc:
            return fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - any parser error
            return fail(f"Could not read the file: {exc}")

        if not deck_text.strip():
            return fail(
                "No readable text found in the file. Is the deck image-only or empty?"
            )

        try:
            data = analyze(deck_text)
        except AnalysisError as exc:
            return fail(str(exc))

        company_name = (data.get("company_name") or "the company").strip() or "the company"
        download_name = f"{stem}_onepager.pdf"
        out_path = _pdf_path(job_id)

        try:
            render(data, str(out_path))
        except Exception as exc:  # noqa: BLE001 - reportlab can raise various errors
            return fail(f"Failed to render the PDF: {exc}")

        # The document is now downloadable. Mark it ready BEFORE attempting the
        # (slower, best-effort) email so the user can grab the file immediately.
        record.update(
            status=STATUS_READY,
            company_name=company_name,
            download_name=download_name,
            finished=time.time(),
        )
        _write_status(job_id, record)

        # Email a copy. A mail failure must not undo the ready state; record it
        # on the job for diagnostics only.
        if mail_enabled():
            try:
                sent_to = email_document(
                    out_path.read_bytes(),
                    download_name,
                    company_name=company_name,
                )
                record["mail_status"] = "sent"
                record["mail_to"] = sent_to
                print(f"[job {job_id}] emailed one-pager to {sent_to}", flush=True)
            except MailError as exc:
                record["mail_status"] = "failed"
                record["mail_error"] = str(exc)
                # Surface the real SMTP error in the server (Railway) logs so the
                # cause is diagnosable — the browser only sees a generic message.
                print(f"[job {job_id}] email failed: {exc}", file=sys.stderr, flush=True)
        else:
            record["mail_status"] = "skipped"
            print(
                f"[job {job_id}] email skipped: SMTP not configured "
                "(need SMTP_HOST, SMTP_USER, SMTP_PASSWORD)",
                file=sys.stderr,
                flush=True,
            )
        _write_status(job_id, record)
    finally:
        try:
            os.remove(in_path)
        except OSError:
            pass
        try:
            os.rmdir(os.path.dirname(in_path))
        except OSError:
            pass


def start_job(upload_bytes: bytes, ext: str, stem: str) -> str:
    """Persist the upload, kick off the background worker, and return a job id.

    Returns immediately; the caller responds to the browser with the id while
    the heavy pipeline runs in a daemon thread.
    """
    _sweep_expired()

    job_id = uuid.uuid4().hex
    STORE_DIR.mkdir(parents=True, exist_ok=True)

    # Stage the uploaded bytes to a private temp dir the worker owns and cleans
    # up. Kept out of STORE_DIR so the sweep only ever touches .json/.pdf.
    work_dir = tempfile.mkdtemp(prefix=f"dbn_{job_id}_")
    in_path = os.path.join(work_dir, "input" + ext)
    with open(in_path, "wb") as fh:
        fh.write(upload_bytes)

    base_record = {
        "job_id": job_id,
        "status": STATUS_PROCESSING,
        "stem": stem,
        "created": time.time(),
    }
    _write_status(job_id, base_record)

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, in_path, stem, base_record),
        name=f"dbn-job-{job_id}",
        daemon=True,
    )
    thread.start()

    return job_id
