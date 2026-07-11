"""Web app — Pitch Deck -> Investor One-Pager PDF.

A thin Flask front end over the same three-stage pipeline used by the CLI:
upload a deck (.pptx/.pdf/.docx), it is extracted, analyzed by Claude, and
rendered to a one-pager PDF that is streamed back as a download.

Deployment (Railway): set the ANTHROPIC_API_KEY environment variable in the
Railway service settings; the app reads it server-side. The process binds to
the port Railway provides via $PORT.
"""

from __future__ import annotations

import io
import os
import tempfile

from flask import (
    Flask,
    Response,
    jsonify,
    render_template_string,
    request,
    send_file,
)

from analyze import AnalysisError, analyze
from extract import UnsupportedFileError, extract
from render import render

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
    <div id="ok" class="ok" style="display:none">✅ PDF generated and downloaded. Ready for another deck.</div>

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

    <div class="meta">
      The uploaded file is processed in memory and not stored.
      {% if not key_set %}<br><strong>⚠ Server has no ANTHROPIC_API_KEY set.</strong>{% endif %}
    </div>
  </div>

  <script>
    const form  = document.getElementById('form');
    const go     = document.getElementById('go');
    const flash  = document.getElementById('flash');
    const okMsg  = document.getElementById('ok');
    const deck   = document.getElementById('deck');
    const fname  = document.getElementById('fname');
    const LABEL  = 'Generate one-pager PDF';

    function showError(msg) {
      flash.textContent = msg;
      flash.style.display = 'block';
    }

    function filenameFromHeader(res, fallback) {
      const cd = res.headers.get('Content-Disposition') || '';
      const m = /filename="?([^"]+)"?/.exec(cd);
      return (m && m[1]) || fallback;
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      flash.style.display = 'none';
      okMsg.style.display = 'none';
      go.disabled = true;
      go.textContent = 'Analyzing… (this can take ~20–40s)';

      try {
        const res = await fetch(form.action, { method: 'POST', body: new FormData(form) });

        if (!res.ok) {
          let msg = 'Something went wrong (HTTP ' + res.status + ').';
          try { const j = await res.json(); if (j && j.error) msg = j.error; }
          catch (_) { /* non-JSON error body */ }
          showError(msg);
          return;
        }

        // Success -> download the PDF blob.
        const blob = await res.blob();
        const url  = URL.createObjectURL(blob);
        const stem = (deck.files[0]?.name || 'deck').replace(/\.[^.]+$/, '');
        const a = document.createElement('a');
        a.href = url;
        a.download = filenameFromHeader(res, stem + '_onepager.pdf');
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);

        // Reset so the next deck can be generated without a refresh.
        form.reset();
        fname.textContent = '';
        okMsg.style.display = 'block';
      } catch (err) {
        showError('Network error: ' + err.message);
      } finally {
        go.disabled = false;
        go.textContent = LABEL;
      }
    });
  </script>
</body>
</html>
"""


@app.get("/")
def index() -> str:
    return render_template_string(
        PAGE, max_mb=MAX_UPLOAD_MB, key_set=bool(os.environ.get("ANTHROPIC_API_KEY"))
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
    file = request.files.get("deck")
    if file is None or not file.filename:
        return _err("Please choose a file to upload.")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return _err(f"Unsupported file type '{ext}'. Use .pptx, .pdf, or .docx.")

    stem = os.path.splitext(os.path.basename(file.filename))[0] or "deck"

    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "input" + ext)
        out_path = os.path.join(tmp, "onepager.pdf")
        file.save(in_path)

        try:
            deck_text = extract(in_path)
        except UnsupportedFileError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"Could not read the file: {exc}")

        if not deck_text.strip():
            return _err(
                "No readable text found in the file. Is the deck image-only or empty?"
            )

        try:
            data = analyze(deck_text)
        except AnalysisError as exc:
            return _err(str(exc), status=502)

        try:
            render(data, out_path)
        except Exception as exc:  # noqa: BLE001
            return _err(f"Failed to render the PDF: {exc}", status=500)

        # Read into memory so the temp dir can be cleaned up on exit.
        with open(out_path, "rb") as fh:
            pdf_bytes = fh.read()

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{stem}_onepager.pdf",
    )


if __name__ == "__main__":
    # Local development server. In production (Railway) gunicorn serves app:app.
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
