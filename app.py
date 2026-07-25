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
disk for later download, AND emails a copy to the configured recipient (see
mailer.py). None of those steps run inside the request that the browser is
waiting on, so nothing is vulnerable to the proxy timeout.

Deployment (Railway): set ANTHROPIC_API_KEY, and RESEND_API_KEY (or the SMTP_*
variables) to enable the emailed copy. The process binds to $PORT.
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
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TEN Capital Network — Deck to One-Pager</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root{
      --navy-950:#0B1526; --navy-900:#101E33; --navy-800:#16283F; --navy-700:#1E354F;
      --coral:#EE5A4E; --coral-soft:#F0776C; --amber:#F3A22A; --teal:#35BEBB;
      --ink-100:#F3F6FA; --ink-300:#C4D0E0; --ink-500:#7E90A8; --ink-600:#5C6E86;
    }
    *{ box-sizing:border-box; }
    html,body{
      margin:0; padding:0; background:var(--navy-950); color:var(--ink-100);
      font-family:'Inter',sans-serif; min-height:100vh;
    }
    body{ display:flex; align-items:center; justify-content:center; padding:48px 20px;
      position:relative; overflow-x:hidden; }

    /* ambient tri-color glow, echoing the logo's three figures */
    body::before{
      content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
      background:
        radial-gradient(480px 380px at 14% 8%, rgba(238,90,78,0.16), transparent 60%),
        radial-gradient(480px 380px at 86% 6%, rgba(243,162,42,0.13), transparent 60%),
        radial-gradient(560px 420px at 50% 100%, rgba(53,190,187,0.14), transparent 60%);
    }

    .stage{ position:relative; z-index:1; width:100%; max-width:620px; }

    /* brand lockup */
    .brand{ display:flex; align-items:center; gap:12px; margin-bottom:28px; padding-left:4px; }
    /* White chip so the logo's dark wordmark stays readable on the navy header. */
    .brand-chip{
      display:inline-flex; align-items:center; background:#FFFFFF; border-radius:11px;
      padding:8px 13px; box-shadow:0 8px 22px -10px rgba(0,0,0,0.55);
    }
    .brand-logo{ height:34px; width:auto; display:block; }
    .brand-fallback{ display:none; align-items:center; gap:12px; }
    .brand-mark{ width:34px; height:34px; flex-shrink:0; }
    .brand-word{
      font-family:'Sora',sans-serif; font-weight:800; font-size:15px; letter-spacing:0.04em;
      line-height:1.15; color:var(--ink-100); text-transform:uppercase;
    }
    .brand-word span{
      display:block; font-weight:600; font-size:10px; letter-spacing:0.22em;
      color:var(--ink-500); margin-top:2px;
    }

    .card{
      background:linear-gradient(180deg, var(--navy-900) 0%, var(--navy-800) 100%);
      border:1px solid var(--navy-700); border-radius:20px; padding:44px 44px 36px;
      box-shadow:0 30px 60px -20px rgba(0,0,0,0.55), inset 0 1px 0 rgba(255,255,255,0.03);
      position:relative; overflow:hidden;
    }
    .card::after{
      content:""; position:absolute; top:-2px; left:44px; right:44px; height:2px;
      background:linear-gradient(90deg, var(--coral), var(--amber), var(--teal)); border-radius:2px;
    }

    .eyebrow{
      display:flex; align-items:center; gap:8px; font-family:'JetBrains Mono',monospace;
      font-size:11px; letter-spacing:0.14em; text-transform:uppercase; color:var(--teal); margin-bottom:14px;
    }
    .eyebrow::before{
      content:""; width:6px; height:6px; border-radius:50%; background:var(--teal);
      box-shadow:0 0 0 3px rgba(53,190,187,0.18);
    }

    h1{ font-family:'Sora',sans-serif; font-size:28px; font-weight:700; line-height:1.25;
      margin:0 0 12px; letter-spacing:-0.01em; }
    h1 .arrow{ color:var(--ink-500); font-weight:400; margin:0 4px; }
    h1 .to{ background:linear-gradient(90deg, var(--coral-soft), var(--amber));
      -webkit-background-clip:text; background-clip:text; color:transparent; }

    .lede{ color:var(--ink-300); font-size:15px; line-height:1.6; margin:0 0 32px; max-width:46ch; }

    /* dropzone */
    .dropzone{
      display:block; border:1.5px dashed var(--navy-700); border-radius:14px; padding:38px 24px;
      text-align:center; cursor:pointer; background:rgba(255,255,255,0.015);
      transition:border-color .18s ease, background .18s ease, transform .18s ease;
    }
    .dropzone:hover{ border-color:var(--teal); background:rgba(53,190,187,0.05); }
    .dropzone:active{ transform:scale(0.997); }
    .dropzone.drag{ border-color:var(--teal); background:rgba(53,190,187,0.09); }

    .dropzone-icon{
      width:38px; height:38px; margin:0 auto 14px; border-radius:10px;
      background:linear-gradient(135deg, rgba(238,90,78,0.16), rgba(243,162,42,0.16));
      border:1px solid var(--navy-700); display:flex; align-items:center; justify-content:center;
    }
    .dropzone-icon svg{ width:18px; height:18px; }
    .dropzone-title{ font-size:15px; font-weight:600; color:var(--ink-100); margin-bottom:6px; }
    .dropzone-sub{ font-family:'JetBrains Mono',monospace; font-size:11.5px; color:var(--ink-500); letter-spacing:0.01em; }
    .dropzone-sub b{ color:var(--ink-300); font-weight:500; }
    .fname{ margin-top:12px; font-family:'JetBrains Mono',monospace; font-size:12px;
      color:var(--teal); min-height:1em; word-break:break-all; }
    .file-input{ display:none; }

    /* CTA */
    .cta{
      width:100%; margin-top:22px; padding:16px 20px; border:none; border-radius:12px;
      background:linear-gradient(90deg, var(--coral) 0%, var(--coral-soft) 45%, var(--amber) 100%);
      color:#17130E; font-family:'Sora',sans-serif; font-weight:700; font-size:15px; letter-spacing:0.01em;
      cursor:pointer; transition:filter .15s ease, transform .15s ease;
      box-shadow:0 10px 24px -10px rgba(238,90,78,0.45);
    }
    .cta:hover{ filter:brightness(1.06); transform:translateY(-1px); }
    .cta:active{ transform:translateY(0); }
    .cta:disabled{ filter:grayscale(0.35) brightness(0.8); cursor:progress; transform:none; box-shadow:none; }

    /* status + result states */
    .status{
      display:none; align-items:center; gap:12px; margin-top:20px; padding:14px 16px;
      border-radius:12px; background:rgba(255,255,255,0.02); border:1px solid var(--navy-700);
      color:var(--ink-300); font-size:14px; line-height:1.5;
    }
    .spinner{
      width:18px; height:18px; border-radius:50%; flex:none;
      border:2.5px solid var(--navy-700); border-top-color:var(--teal); animation:spin .8s linear infinite;
    }
    @keyframes spin{ to{ transform:rotate(360deg); } }

    .download{
      display:none; width:100%; margin-top:16px; padding:15px 20px; border-radius:12px;
      text-align:center; text-decoration:none; font-family:'Sora',sans-serif; font-weight:700; font-size:15px;
      color:#07201F; background:linear-gradient(90deg, var(--teal), #56D0CD);
      box-shadow:0 10px 24px -10px rgba(53,190,187,0.5); transition:filter .15s ease;
    }
    .download:hover{ filter:brightness(1.06); }

    .flash{
      display:none; margin-top:20px; padding:14px 16px; border-radius:12px; font-size:13px; line-height:1.5;
      background:rgba(238,90,78,0.10); border:1px solid rgba(238,90,78,0.4); color:#F3B4AE;
    }
    .ok{
      display:none; margin-top:20px; padding:14px 16px; border-radius:12px; font-size:13px; line-height:1.5;
      background:rgba(53,190,187,0.10); border:1px solid rgba(53,190,187,0.4); color:#A7E7E5;
    }

    /* footnote / disclosure */
    .disclosure{
      margin-top:22px; padding-top:18px; border-top:1px solid var(--navy-700);
      font-size:12px; line-height:1.6; color:var(--ink-500);
    }
    .disclosure code{
      font-family:'JetBrains Mono',monospace; background:var(--navy-950); border:1px solid var(--navy-700);
      color:var(--ink-300); padding:2px 6px; border-radius:5px; font-size:11.5px;
    }
    .disclosure .warn{ color:var(--amber); }

    footer{
      text-align:center; margin-top:22px; font-family:'JetBrains Mono',monospace; font-size:11px;
      letter-spacing:0.08em; color:var(--ink-600); text-transform:uppercase;
    }

    @media (max-width:480px){
      .card{ padding:32px 24px 28px; }
      h1{ font-size:23px; }
    }
  </style>
</head>
<body>
  <div class="stage">

    <div class="brand">
      <!-- Real logo if static/logo.png exists; otherwise fall back to the SVG lockup. -->
      <span class="brand-chip" id="brandChip">
        <img class="brand-logo" src="{{ url_for('static', filename='logo.webp') }}"
             alt="TEN Capital Network"
             onerror="document.getElementById('brandChip').style.display='none'; document.getElementById('brandFallback').style.display='flex';">
      </span>
      <div class="brand-fallback" id="brandFallback">
        <svg class="brand-mark" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M50 6 C64 6 74 16 74 16" stroke="#F3A22A" stroke-width="11" stroke-linecap="round" fill="none"/>
          <path d="M76 66 C76 82 63 92 63 92" stroke="#35BEBB" stroke-width="11" stroke-linecap="round" fill="none"/>
          <path d="M24 66 C24 82 37 92 37 92" stroke="#EE5A4E" stroke-width="11" stroke-linecap="round" fill="none" transform="rotate(180 50 79)"/>
          <circle cx="50" cy="20" r="11" fill="#F3A22A"/>
          <circle cx="78" cy="68" r="11" fill="#35BEBB"/>
          <circle cx="22" cy="68" r="11" fill="#EE5A4E"/>
        </svg>
        <div class="brand-word">Ten Capital<span>Network</span></div>
      </div>
    </div>

    <div class="card">
      <div class="eyebrow">Deck Analyzer</div>
      <h1>Pitch Deck<span class="arrow">&rarr;</span><span class="to">Investor One&#8209;Pager</span></h1>
      <p class="lede">Upload a pitch deck and get a polished single-page investor PDF,
        analyzed and structured by Claude.</p>

      <form id="form" method="post" action="{{ url_for('generate') }}" enctype="multipart/form-data">
        <label class="dropzone" id="dropzone" for="deck">
          <div class="dropzone-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="#F3F6FA" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 3v4a1 1 0 0 0 1 1h4"/>
              <path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2Z"/>
            </svg>
          </div>
          <div class="dropzone-title">Click or drop a deck here</div>
          <div class="dropzone-sub"><b>.pptx</b> &middot; <b>.pdf</b> &middot; <b>.docx</b> &nbsp;·&nbsp; up to {{ max_mb }}&nbsp;MB</div>
          <input class="file-input" type="file" id="deck" name="deck" accept=".pptx,.pdf,.docx" required>
          <div class="fname" id="fname"></div>
        </label>

        <button class="cta" id="go" type="submit">Generate one-pager PDF</button>
      </form>

      <div class="status" id="status">
        <div class="spinner"></div>
        <div id="statusText">Working…</div>
      </div>

      <a class="download" id="download" href="#">⬇ Download your one-pager</a>

      <div class="flash" id="flash"></div>
      <div class="ok" id="ok"></div>

      <div class="disclosure">
        The uploaded file is processed on the server and a copy of every generated
        one-pager is emailed to <code>{{ recipient }}</code>.
        {% if not key_set %}<br><span class="warn">⚠ Server has no ANTHROPIC_API_KEY set.</span>{% endif %}
        {% if not mail_ready %}<br><span class="warn">⚠ Email is not configured; the emailed copy will be skipped.</span>{% endif %}
      </div>
    </div>

    <footer>Powered by TEN Capital Network</footer>

  </div>

  <script>
    const form     = document.getElementById('form');
    const go        = document.getElementById('go');
    const deck      = document.getElementById('deck');
    const fname     = document.getElementById('fname');
    const dropzone  = document.getElementById('dropzone');
    const flash     = document.getElementById('flash');
    const okMsg     = document.getElementById('ok');
    const statusEl  = document.getElementById('status');
    const statusTx  = document.getElementById('statusText');
    const download  = document.getElementById('download');
    const LABEL     = 'Generate one-pager PDF';
    const POLL_MS   = 3000;

    let pollTimer = null;

    function showError(msg) { flash.textContent = msg; flash.style.display = 'block'; }
    function updateName()   { fname.textContent = deck.files[0] ? deck.files[0].name : ''; }
    function resetCta()     { go.disabled = false; go.textContent = LABEL; statusEl.style.display = 'none'; }

    deck.addEventListener('change', updateName);

    // Drag-and-drop onto the dropzone.
    ['dragover', 'dragenter'].forEach(ev =>
      dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add('drag'); }));
    ['dragleave', 'dragend'].forEach(ev =>
      dropzone.addEventListener(ev, () => dropzone.classList.remove('drag')));
    dropzone.addEventListener('drop', e => {
      e.preventDefault();
      dropzone.classList.remove('drag');
      if (e.dataTransfer.files && e.dataTransfer.files.length) {
        deck.files = e.dataTransfer.files;
        updateName();
      }
    });

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
             j.mail_status === 'skipped' ? 'not emailed (email off).' :
             'not emailed (send failed) — you can still download it.');
          okMsg.style.display = 'block';
          if (j.mail_status === 'failed' && j.mail_error) {
            showError('Email error: ' + j.mail_error);
          }
          // Auto-trigger the download, and leave the button for a manual retry.
          window.location.href = j.download_url;
          resetCta();
          form.reset();
          fname.textContent = '';
          return;
        }

        if (j.status === 'error') {
          showError(j.error || 'Generation failed.');
          resetCta();
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
          resetCta();
          return;
        }

        const j = await res.json();
        statusEl.style.display = 'flex';
        statusTx.textContent = 'Analyzing with Claude… (this can take ~20–40s). You can keep this tab open.';
        go.textContent = 'Working…';
        poll(j.job_id);
      } catch (err) {
        showError('Network error: ' + err.message);
        resetCta();
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
