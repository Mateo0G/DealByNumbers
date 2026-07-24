"""Web app — Pitch Deck -> Investor One-Pager PDF (async / job-based).

Why this is not a single request/response: analyzing a deck with Claude takes
~20-40s. Railway's edge proxy will cut an HTTP connection that stays open that
long, so a synchronous "upload -> analyze -> respond" request would appear to
hang forever and never deliver a file. Instead the work is decoupled:

    POST /generate         accepts the upload, starts a background job, and
                           returns a job id immediately (fast response).
    GET  /status/<job_id>  a small, fast poll the page hits every few seconds.
    GET  /download/<job_id> streams the finished PDF once the job is ready.

The background job (see jobs.py) extracts, analyzes, renders, stores the PDF on
disk for later download, AND emails a copy to info@tencapital.group. None of
those steps run inside the request that the browser is waiting on, so nothing is
vulnerable to the proxy timeout.

Deployment (Railway): set ANTHROPIC_API_KEY, and the SMTP_* variables (see
.env.example) to enable the emailed copy. The process binds to $PORT.
"""

from __future__ import annotations

import io
import os

from flask import (
    Flask,
    Response,
    jsonify,
    render_template_string,
    request,
    send_file,
    url_for,
)

import jobs
from mailer import DEFAULT_RECIPIENT, mail_enabled

MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pptx", ".pdf", ".docx"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
# Used only to flash transient error messages; not security-sensitive.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pitch Deck → Investor One-Pager</title>
  <style>
    :root { color-scheme: light dark; }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: #0f172a; color: #e2e8f0; padding: 2rem;
    }
    .card {
      width: 100%; max-width: 560px; background: #1e293b; border: 1px solid #334155;
      border-radius: 16px; padding: 2.25rem; box-shadow: 0 20px 50px rgba(0,0,0,.35);
    }
    h1 { margin: 0 0 .35rem; font-size: 1.5rem; color: #f8fafc; }
    p.sub { margin: 0 0 1.75rem; color: #94a3b8; font-size: .95rem; }
    label.file {
      display: block; border: 2px dashed #475569; border-radius: 12px; padding: 2rem 1rem;
      text-align: center; cursor: pointer; transition: border-color .15s, background .15s;
      background: #0f172a;
    }
    label.file:hover { border-color: #3b82f6; background: #14203a; }
    label.file .hint { color: #64748b; font-size: .85rem; margin-top: .4rem; }
    #fname { color: #93c5fd; font-weight: 600; margin-top: .6rem; min-height: 1.1em; }
    input[type=file] { display: none; }
    button {
      margin-top: 1.5rem; width: 100%; padding: .85rem 1rem; border: 0; border-radius: 10px;
      background: #2563eb; color: white; font-size: 1rem; font-weight: 600; cursor: pointer;
      transition: background .15s;
    }
    button:hover { background: #1d4ed8; }
    button:disabled { background: #475569; cursor: progress; }
    .flash { background: #7f1d1d; border: 1px solid #b91c1c; color: #fecaca;
             padding: .75rem 1rem; border-radius: 10px; margin-bottom: 1.25rem; font-size: .9rem; }
    .ok { background: #14532d; border: 1px solid #16a34a; color: #bbf7d0;
          padding: .75rem 1rem; border-radius: 10px; margin-bottom: 1.25rem; font-size: .9rem; }
    .status { display: none; align-items: center; gap: .7rem; margin-top: 1.5rem;
              color: #cbd5e1; font-size: .95rem; }
    .status .spinner {
      width: 18px; height: 18px; border: 3px solid #334155; border-top-color: #3b82f6;
      border-radius: 50%; animation: spin .8s linear infinite; flex: none;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    a.download {
      display: none; margin-top: 1rem; text-align: center; padding: .85rem 1rem;
      border-radius: 10px; background: #16a34a; color: white; font-weight: 600;
      text-decoration: none;
    }
    a.download:hover { background: #15803d; }
    .meta { margin-top: 1.5rem; color: #64748b; font-size: .8rem; line-height: 1.5; }
    code { background: #0f172a; padding: .1rem .35rem; border-radius: 5px; color: #cbd5e1; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Pitch Deck → Investor One-Pager</h1>
    <p class="sub">Upload a pitch deck and get a polished single-page investor PDF,
      analyzed by Claude.</p>

    <div id="flash" class="flash" style="display:none"></div>
    <div id="ok" class="ok" style="display:none"></div>

    <form id="form" method="post" action="{{ url_for('generate') }}" enctype="multipart/form-data">
      <label class="file">
        <input id="deck" type="file" name="deck" accept=".pptx,.pdf,.docx" required
               onchange="document.getElementById('fname').textContent = this.files[0]?.name || '';">
        <div>📄 Click to choose a deck</div>
        <div class="hint">.pptx, .pdf, or .docx · up to {{ max_mb }} MB</div>
        <div id="fname"></div>
      </label>
      <button id="go" type="submit">Generate one-pager PDF</button>
    </form>

    <div id="status" class="status">
      <div class="spinner"></div>
      <div id="statusText">Working…</div>
    </div>

    <a id="download" class="download" href="#">⬇ Download your PDF</a>

    <div class="meta">
      The uploaded file is processed on the server and a copy of every generated
      one-pager is emailed to <code>{{ recipient }}</code>.
      {% if not key_set %}<br><strong>⚠ Server has no ANTHROPIC_API_KEY set.</strong>{% endif %}
      {% if not mail_ready %}<br><strong>⚠ SMTP is not configured; the emailed copy will be skipped.</strong>{% endif %}
    </div>
  </div>

  <script>
    const form    = document.getElementById('form');
    const go       = document.getElementById('go');
    const flash    = document.getElementById('flash');
    const okMsg    = document.getElementById('ok');
    const deck     = document.getElementById('deck');
    const fname    = document.getElementById('fname');
    const statusEl = document.getElementById('status');
    const statusTx = document.getElementById('statusText');
    const download = document.getElementById('download');
    const LABEL    = 'Generate one-pager PDF';
    const POLL_MS  = 3000;

    let pollTimer = null;

    function showError(msg) {
      flash.textContent = msg;
      flash.style.display = 'block';
    }

    function resetUi() {
      go.disabled = false;
      go.textContent = LABEL;
      statusEl.style.display = 'none';
    }

    async function poll(jobId) {
      try {
        const res = await fetch('/status/' + jobId, { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const j = await res.json();

        if (j.status === 'ready') {
          statusEl.style.display = 'none';
          download.href = j.download_url;
          download.style.display = 'block';
          okMsg.textContent = '✅ Your one-pager is ready — a copy was ' +
            (j.mail_status === 'sent' ? 'emailed to the team.' :
             j.mail_status === 'skipped' ? 'not emailed (SMTP off).' :
             'not emailed (send failed) — you can still download it.');
          okMsg.style.display = 'block';
          if (j.mail_status === 'failed' && j.mail_error) {
            showError('Email error: ' + j.mail_error);
          }
          // Auto-trigger the download, and leave the button for a manual retry.
          window.location.href = j.download_url;
          resetUi();
          form.reset();
          fname.textContent = '';
          return;
        }

        if (j.status === 'error') {
          showError(j.error || 'Generation failed.');
          resetUi();
          return;
        }

        // still processing -> poll again
        pollTimer = setTimeout(() => poll(jobId), POLL_MS);
      } catch (err) {
        // A transient network/poll hiccup shouldn't kill the whole job; retry.
        pollTimer = setTimeout(() => poll(jobId), POLL_MS);
      }
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      flash.style.display = 'none';
      okMsg.style.display = 'none';
      download.style.display = 'none';
      if (pollTimer) clearTimeout(pollTimer);
      go.disabled = true;
      go.textContent = 'Uploading…';

      try {
        const res = await fetch(form.action, { method: 'POST', body: new FormData(form) });

        if (!res.ok) {
          let msg = 'Something went wrong (HTTP ' + res.status + ').';
          try { const j = await res.json(); if (j && j.error) msg = j.error; }
          catch (_) { /* non-JSON error body */ }
          showError(msg);
          resetUi();
          return;
        }

        const j = await res.json();
        statusEl.style.display = 'flex';
        statusTx.textContent = 'Analyzing with Claude… (this can take ~20–40s). You can keep this tab open.';
        go.textContent = 'Working…';
        poll(j.job_id);
      } catch (err) {
        showError('Network error: ' + err.message);
        resetUi();
      }
    });
  </script>
</body>
</html>
"""


@app.get("/")
def index() -> str:
    return render_template_string(
        PAGE,
        max_mb=MAX_UPLOAD_MB,
        key_set=bool(os.environ.get("ANTHROPIC_API_KEY")),
        mail_ready=mail_enabled(),
        recipient=os.environ.get("MAIL_TO", DEFAULT_RECIPIENT),
    )


@app.get("/healthz")
def healthz() -> Response:
    """Lightweight health check for Railway."""
    return Response("ok", mimetype="text/plain")


def _err(message: str, status: int = 400) -> Response:
    """Return a JSON error the front-end fetch handler can display inline."""
    return jsonify({"error": message}), status


@app.post("/generate")
def generate():
    """Accept an upload, start the background job, and return its id at once."""
    file = request.files.get("deck")
    if file is None or not file.filename:
        return _err("Please choose a file to upload.")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return _err(f"Unsupported file type '{ext}'. Use .pptx, .pdf, or .docx.")

    stem = os.path.splitext(os.path.basename(file.filename))[0] or "deck"

    upload_bytes = file.read()
    if not upload_bytes:
        return _err("The uploaded file is empty.")

    job_id = jobs.start_job(upload_bytes, ext, stem)
    return jsonify({"job_id": job_id, "status": jobs.STATUS_PROCESSING}), 202


@app.get("/status/<job_id>")
def status(job_id: str):
    """Fast poll: report a job's state, plus a download link once ready."""
    record = jobs.read_status(job_id)
    if record is None:
        return _err("Unknown or expired job id.", status=404)

    payload = {"status": record.get("status")}
    if record.get("status") == jobs.STATUS_READY:
        payload["download_url"] = url_for("download", job_id=job_id)
        payload["download_name"] = record.get("download_name")
        payload["company_name"] = record.get("company_name")
        payload["mail_status"] = record.get("mail_status")
        if record.get("mail_error"):
            payload["mail_error"] = record.get("mail_error")
    elif record.get("status") == jobs.STATUS_ERROR:
        payload["error"] = record.get("error")
    return jsonify(payload)


@app.get("/download/<job_id>")
def download(job_id: str):
    """Stream the finished PDF for a ready job."""
    record = jobs.read_status(job_id)
    if record is None:
        return _err("Unknown or expired job id.", status=404)
    if record.get("status") != jobs.STATUS_READY:
        return _err("This document is not ready yet.", status=409)

    data = jobs.pdf_bytes(job_id)
    if data is None:
        return _err("The generated file is no longer available.", status=410)

    return send_file(
        io.BytesIO(data),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=record.get("download_name") or f"{record.get('stem', 'deck')}_onepager.pdf",
    )


if __name__ == "__main__":
    # Local development server. In production (Railway) gunicorn serves app:app.
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
