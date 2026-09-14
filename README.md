# Pitch Deck → Investor One-Pager PDF

A Python CLI that ingests an investor pitch deck (`.pptx`, `.pdf`, or `.docx`),
analyzes it with the Anthropic API as an expert startup financial specialist
would, and generates a clean, single-page investor one-pager PDF.

## How it works (3 stages)

1. **Extract** ([extract.py](extract.py)) — pull all text and tables from the
   input file, preserving slide/page/table structure with markers.
2. **Analyze** ([analyze.py](analyze.py)) — send the extracted content to Claude
   with a financial-specialist system prompt and get back structured JSON for
   every section. Missing numbers are filled with clearly-flagged industry
   estimates.
3. **Render** ([render.py](render.py)) — lay the JSON out into a professional,
   single US-Letter-page PDF with reportlab.

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your Anthropic API key
# Windows (PowerShell):
$env:ANTHROPIC_API_KEY = "sk-ant-..."
# macOS / Linux:
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

```bash
python main.py path/to/deck.pptx
# writes deck_onepager.pdf in the current directory

python main.py path/to/deck.pdf --output investor_brief.pdf
```

Supported input types: `.pptx`, `.pdf`, `.docx`.

## Output

A polished investor one-pager PDF with a header (company name + tagline) and the
full 18-section structure from the Investor One-Pager Summary contract:

Investor Hook · Problem · Solution · Product · Traction · Market Size (TAM/SAM/SOM)
· Business Model · Competitive Advantage · Go-To-Market · Team · The Ask · Use of
Funds (bullets + allocation bars) · Financial Outlook (multi-year projection table)
· Exit Potential · Key Metrics (snapshot table) · Investment Thesis.

**Layout — fit-then-flow:** the renderer targets a single US-Letter page with a
comfortable, readable font and flows onto a second page only when a content-rich
deck genuinely doesn't fit, rather than shrinking to an unreadable size.

Any figure the model had to estimate (because it wasn't in the deck) is shown in
*italic* with an **(est.)** suffix, and a footnote notes:
*"Figures marked (est.) are analyst estimates, not from the deck."* Fields with no
basis in the deck and no reasonable estimate are rendered as *"Not specified in
deck"*.

## Run as a web app (local)

The same pipeline is exposed as a Flask web app ([app.py](app.py)) — upload a
deck in the browser, and the generated PDF is both emailed to the team and made
available to download.

```bash
# with the venv active and ANTHROPIC_API_KEY set:
python app.py
# open http://localhost:8000
```

In production a WSGI server is used instead of the dev server (see below).

### How the request flow avoids proxy timeouts

Analyzing a deck with Claude takes ~20–40s — long enough that Railway's edge
proxy would cut a single, long-held `upload → analyze → download` request and
leave the browser spinning forever. So the work is decoupled into a background
job ([jobs.py](jobs.py)):

1. `POST /generate` accepts the upload, starts a background thread, and returns a
   `job_id` immediately — a fast response the proxy never times out.
2. The page polls `GET /status/<job_id>` every few seconds (each poll is its own
   fast request).
3. The background thread extracts → analyzes → renders, then **saves the PDF to
   disk** keyed by `job_id` and **emails a copy** to `info@tencapital.group`.
4. The next poll returns `ready` plus a download link; the page auto-triggers the
   download from `GET /download/<job_id>` and shows a manual download button.

Job status and finished PDFs are stored on the local disk (a temp dir by
default, keyed by `job_id`) so they are visible to every gunicorn worker on the
instance — not just the one that started the job. Artifacts are swept after a TTL
(6h default) and are lost on redeploy/restart, which is fine: downloads happen
within minutes and every document is also emailed.

## Deploy to Railway

The app is ready to deploy on [Railway](https://railway.app) as a web service.

1. **Push the repo to GitHub** (a `.gitignore` is included; the `.venv/` and
   generated PDFs are excluded).
2. In Railway: **New Project → Deploy from GitHub repo**, and pick this repo.
   Railway auto-detects Python via `requirements.txt` (Nixpacks) and uses the
   included [`railway.json`](railway.json) / [`Procfile`](Procfile) to start
   the server with gunicorn.
3. **Set the environment variables** in the service's **Variables** tab:
   - `ANTHROPIC_API_KEY` = your key from the Anthropic Console.
   - To email a copy of every generated one-pager, set **`RESEND_API_KEY`**
     (from [resend.com](https://resend.com)). Railway **blocks outbound SMTP**,
     so plain SMTP fails there with `Network is unreachable`; Resend sends over
     HTTPS and works. With the default `onboarding@resend.dev` sender and no
     verified domain, Resend only delivers to your Resend account's own email —
     so sign up with the same address as `MAIL_TO` (defaults to
     `info@tencapital.group`). Verify a domain and set `RESEND_FROM` to send
     anywhere. Locally, `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` still work as an
     alternative. If nothing is set the email step is skipped; the PDF is still
     generated and downloadable. See [`.env.example`](.env.example).
   - (optional) `FLASK_SECRET_KEY` = any random string.
4. Railway provides `$PORT` automatically; gunicorn binds to it. A health check
   is served at `/healthz`.
5. Open the generated public URL and upload a deck. The one-pager is emailed to
   the team and offered as a download once the background job finishes.

Deployment files included:

| File | Purpose |
|------|---------|
| [`Procfile`](Procfile) | `web: gunicorn app:app --bind 0.0.0.0:$PORT ...` |
| [`railway.json`](railway.json) | Build (Nixpacks) + start command + health check |
| [`.python-version`](.python-version) | Pins the Python version for the build |
| [`.gitignore`](.gitignore) | Keeps the venv, secrets, and artifacts out of git |

The API key is read **server-side** from the environment — it is never entered
in the browser or stored with the uploaded file (uploads are processed in memory
and discarded).

## Configuration

The Claude model is a constant at the top of [analyze.py](analyze.py):

```python
MODEL = "claude-sonnet-4-5"
```

Change that string to use a different model (e.g. `claude-opus-4-8`).

## Error handling

The CLI fails with clear messages for: unsupported/missing files, image-only
decks with no extractable text, a missing `ANTHROPIC_API_KEY`, API failures,
and unparseable model responses (the raw response is logged to stderr before
exit).

## Project layout

```
extract.py       Stage 1 — file parsing (pptx / pdf / docx)
analyze.py       Stage 2 — Anthropic API call + JSON parsing
render.py        Stage 3 — reportlab PDF layout (fit-then-flow)
main.py          CLI wiring (argparse) + pipeline orchestration
app.py           Flask web app (upload -> job id -> poll -> email + download)
jobs.py          Disk-backed background job store (extract/analyze/render/email)
mailer.py        SMTP email of the finished one-pager (default info@tencapital.group)
requirements.txt pinned dependencies
Procfile         Railway/Nixpacks start command (gunicorn)
railway.json     Railway build + deploy config
.python-version  Python version pin for the build
```
