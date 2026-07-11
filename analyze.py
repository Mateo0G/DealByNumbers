"""Stage 2 — Analysis via the Anthropic API.

Send the extracted deck content to Claude with an expert-financial-specialist
persona and get back a single structured JSON object matching the Investor
One-Pager Summary contract (see the JSON schema in the project docs). Missing
numbers are filled with reasonable industry-benchmark estimates, each flagged
with an "is_estimated" boolean; anything with no basis and no reasonable
estimate is set to the literal string "Not specified in deck".
"""

from __future__ import annotations

import json
import os
import sys

# Model is a constant so it's trivial to swap. claude-sonnet-4-5 is an active
# model; change this string to use a different one (e.g. "claude-opus-4-8").
MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8000

NOT_SPECIFIED = "Not specified in deck"

SYSTEM_PROMPT = (
    "You are an expert startup financial specialist preparing an investor "
    "one-pager."
)

# The exact contract we want back, described so the model returns consistent
# keys the renderer can lay out without surprises.
_JSON_INSTRUCTIONS = f"""\
Analyze the pitch deck content below and produce a complete investor one-pager.

Return ONLY valid JSON (no markdown fences, no preamble, no trailing prose).
The object MUST have exactly these top-level keys, with these shapes:

{{
  "company_name": string,
  "tagline": string,
  "source_attribution": string,           // e.g. "<Company> <Month Year>"; reused under each section

  "investor_hook":         {{ "body": string }},
  "problem":               {{ "bullets": [ {{ "label": string, "detail": string, "is_estimated": bool }} ] }},
  "solution":              {{ "body": string }},
  "product":               {{ "bullets": [ {{ "label": string, "detail": string, "is_estimated": bool }} ], "summary": string }},
  "traction":              {{ "bullets": [ {{ "label": string, "detail": string, "is_estimated": bool }} ] }},
  "market_size":           {{ "tam": string, "tam_is_estimated": bool,
                             "sam": string, "sam_is_estimated": bool,
                             "som": string, "som_is_estimated": bool }},
  "business_model":        {{ "bullets": [ {{ "label": string, "detail": string, "is_estimated": bool }} ] }},
  "competitive_advantage": {{ "bullets": [ {{ "detail": string, "is_estimated": bool }} ] }},
  "go_to_market":          {{ "bullets": [ {{ "label": string, "detail": string, "is_estimated": bool }} ] }},
  "team":                  {{ "members": [ {{ "name": string, "role": string, "bio": string }} ] }},
  "fundraise":             {{ "body": string, "amount": string, "amount_is_estimated": bool,
                             "round_type": string, "valuation": string, "valuation_is_estimated": bool,
                             "instrument": string }},
  "use_of_funds":          {{ "bullets": [ {{ "label": string, "detail": string, "is_estimated": bool }} ],
                             "allocation": [ {{ "category": string, "percent": number, "is_estimated": bool }} ] }},
  "financial_outlook":     {{ "body": string,
                             "years": [ string, ... ],                 // ordered column headers, typically 5
                             "rows": [ {{ "metric": string,            // e.g. Revenue, Gross Margin, Anticipated Valuation, Key Milestone
                                         "values": [ string, ... ],    // aligns positionally with "years"
                                         "is_estimated": [ bool, ... ] // per-cell flags, aligns with "values"
                                       }} ] }},
  "exit_potential":        {{ "body": string }},
  "key_metrics":           {{ "funding_ask": string, "funding_ask_is_estimated": bool,
                             "round_type": string,
                             "pre_money_valuation": string, "pre_money_valuation_is_estimated": bool,
                             "revenue": string, "revenue_is_estimated": bool,
                             "yoy_growth": string, "yoy_growth_is_estimated": bool,
                             "gross_margin": string, "gross_margin_is_estimated": bool,
                             "key_customer_or_partner": string,
                             "target_market": string }},
  "investment_thesis":     {{ "body": string }}
}}

Rules:
- Write in crisp, investor-facing prose. Narrative bodies are 2-4 sentences.
  Bullet "detail" fields are one quantified sentence where possible.
- Bullet "label" is a short bolded lead-in (2-4 words). competitive_advantage
  bullets have only "detail" (no label).
- For every numeric or factual value: use the figure from the deck when present
  and set its "is_estimated" flag to false. When a value is genuinely absent
  from the deck but a REASONABLE industry-benchmark estimate exists, provide the
  estimate and set its "is_estimated" flag to true.
- If a value has no basis in the deck AND no reasonable estimate, set it to the
  literal string "{NOT_SPECIFIED}" (and leave its estimate flag false).
- financial_outlook: "years" typically has 5 entries; every row's "values" and
  "is_estimated" arrays must be the same length as "years". Standard rows:
  Revenue, Gross Margin, Anticipated Valuation, Key Milestone.
- use_of_funds.allocation percentages should sum to ~100 when provided.
- Never invent a company name; use your best read of the deck.
- Every listed key must be present. Use empty arrays only when a whole section
  has no content at all.
"""


class AnalysisError(RuntimeError):
    """Raised when the API call or response parsing fails unrecoverably."""


def _strip_fences(text: str) -> str:
    """Remove stray ```json ... ``` fences the model may add despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def analyze(deck_text: str) -> dict:
    """Send extracted text to Claude and return the parsed one-pager JSON dict."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AnalysisError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Export your Anthropic API key and try again."
        )

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AnalysisError(
            "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)

    user_content = _JSON_INSTRUCTIONS + "\n\n--- DECK CONTENT ---\n\n" + deck_text

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.APIError as exc:
        raise AnalysisError(f"Anthropic API request failed: {exc}") from exc

    raw = "".join(block.text for block in response.content if block.type == "text")

    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # Log the raw response so the failure is diagnosable, then exit helpfully.
        sys.stderr.write(
            "\nFailed to parse the model response as JSON.\n"
            "Raw response was:\n"
            "----------------------------------------\n"
            f"{raw}\n"
            "----------------------------------------\n"
        )
        raise AnalysisError(
            f"Could not parse JSON from the model response: {exc}"
        ) from exc
