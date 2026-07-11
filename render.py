"""Stage 3 — PDF rendering.

Lay the analyzed JSON (Investor One-Pager Summary contract) out into a clean,
professional PDF using reportlab's Platypus flowable engine. The layout targets
a single US-Letter page with a comfortable font and flows to a second page only
when the content genuinely doesn't fit ("fit-then-flow").

Estimated figures are marked in italic with an "(est.)" suffix and explained in
a closing footnote.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Palette — understated, investor-professional.
INK = colors.HexColor("#20262E")
MUTED = colors.HexColor("#66707E")
ACCENT = colors.HexColor("#294F9E")
RULE = colors.HexColor("#D2D6DC")
BAR_BG = colors.HexColor("#E6E9ED")
TABLE_HEAD_BG = colors.HexColor("#294F9E")
TABLE_ALT_BG = colors.HexColor("#F2F4F7")

FOOTNOTE = "Figures marked (est.) are analyst estimates, not from the deck."
NOT_SPECIFIED = "Not specified in deck"

PAGE_W, PAGE_H = letter
MARGIN = 0.55 * inch
CONTENT_W = PAGE_W - 2 * MARGIN


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _esc(v) -> str:
    return escape(_s(v))


def _est(text: str, flag: bool) -> str:
    """Return escaped text, appending an italic (est.) marker when estimated."""
    body = _esc(text)
    if flag:
        return f"{body} <i>(est.)</i>"
    return body


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #
def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    s = {}
    s["title"] = ParagraphStyle(
        "OP_Title", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=20, leading=23, textColor=INK,
    )
    s["tagline"] = ParagraphStyle(
        "OP_Tagline", parent=base["Normal"], fontName="Helvetica",
        fontSize=10.5, leading=13, textColor=MUTED, spaceAfter=2,
    )
    s["section"] = ParagraphStyle(
        "OP_Section", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=9.5, leading=11, textColor=ACCENT, spaceBefore=7, spaceAfter=1,
    )
    s["attrib"] = ParagraphStyle(
        "OP_Attrib", parent=base["Normal"], fontName="Helvetica-Oblique",
        fontSize=6, leading=7, textColor=MUTED, spaceAfter=2,
    )
    s["body"] = ParagraphStyle(
        "OP_Body", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.8, leading=11, textColor=INK, alignment=TA_LEFT, spaceAfter=1,
    )
    s["bullet"] = ParagraphStyle(
        "OP_Bullet", parent=s["body"], leftIndent=9, firstLineIndent=-9,
        spaceAfter=1.5,
    )
    s["cell"] = ParagraphStyle(
        "OP_Cell", parent=base["Normal"], fontName="Helvetica",
        fontSize=7.4, leading=8.8, textColor=INK,
    )
    s["cell_head"] = ParagraphStyle(
        "OP_CellHead", parent=s["cell"], fontName="Helvetica-Bold",
        textColor=colors.white,
    )
    s["cell_label"] = ParagraphStyle(
        "OP_CellLabel", parent=s["cell"], fontName="Helvetica-Bold",
    )
    s["footnote"] = ParagraphStyle(
        "OP_Footnote", parent=base["Normal"], fontName="Helvetica-Oblique",
        fontSize=7, leading=8.5, textColor=MUTED, spaceBefore=6,
    )
    return s


# --------------------------------------------------------------------------- #
# Allocation bar flowable (use-of-funds)
# --------------------------------------------------------------------------- #
class AllocationBars(Flowable):
    """Horizontal percentage bars for use-of-funds allocation."""

    ROW_H = 15
    BAR_H = 6.5
    LABEL_FS = 7.6

    def __init__(self, items: list[dict], width: float):
        super().__init__()
        self.items = items
        self.width = width
        self.height = self.ROW_H * len(items)

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        return (availWidth, self.height)

    def draw(self):
        c = self.canv
        y = self.height - self.ROW_H
        for item in self.items:
            cat = _s(item.get("category"))
            est = bool(item.get("is_estimated"))
            try:
                pct = float(item.get("percent") or 0)
            except (TypeError, ValueError):
                pct = 0.0
            pct = max(0.0, min(pct, 100.0))

            c.setFillColor(INK)
            c.setFont("Helvetica", self.LABEL_FS)
            label = cat + ("  (est.)" if est else "")
            c.drawString(0, y + self.BAR_H + 2, label)
            c.setFont("Helvetica-Bold", self.LABEL_FS)
            c.drawRightString(self.width, y + self.BAR_H + 2, f"{pct:g}%")

            c.setFillColor(BAR_BG)
            c.rect(0, y, self.width, self.BAR_H, stroke=0, fill=1)
            c.setFillColor(ACCENT)
            c.rect(0, y, self.width * (pct / 100.0), self.BAR_H, stroke=0, fill=1)
            y -= self.ROW_H


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def render(data: dict, output_path: str) -> None:
    st = _styles()
    story: list = []

    _header(story, data, st)

    attrib = _s(data.get("source_attribution"))

    def section(title: str, builder) -> None:
        """Add a section header + attribution + built flowables, kept together."""
        block: list = [Paragraph(_esc(title), st["section"])]
        if attrib:
            block.append(Paragraph(_esc(attrib), st["attrib"]))
        content = builder()
        if not content:
            return
        block.extend(content)
        story.append(KeepTogether(block))

    section("Investor Hook", lambda: _narrative(data.get("investor_hook"), st))
    section("Problem", lambda: _bullets(data.get("problem"), st))
    section("Solution", lambda: _narrative(data.get("solution"), st))
    section("Product", lambda: _product(data.get("product"), st))
    section("Traction", lambda: _bullets(data.get("traction"), st))
    section("Market Size", lambda: _market(data.get("market_size"), st))
    section("Business Model", lambda: _bullets(data.get("business_model"), st))
    section("Competitive Advantage", lambda: _bullets(data.get("competitive_advantage"), st))
    section("Go-To-Market", lambda: _bullets(data.get("go_to_market"), st))
    section("Team", lambda: _team(data.get("team"), st))
    section("The Ask", lambda: _fundraise(data.get("fundraise"), st))
    section("Use of Funds", lambda: _use_of_funds(data.get("use_of_funds"), st))
    section("Financial Outlook", lambda: _financials(data.get("financial_outlook"), st))
    section("Exit Potential", lambda: _narrative(data.get("exit_potential"), st))
    section("Key Metrics", lambda: _key_metrics(data.get("key_metrics"), st))
    section("Investment Thesis", lambda: _narrative(data.get("investment_thesis"), st))

    if _has_estimates(data):
        story.append(Paragraph(_esc(FOOTNOTE), st["footnote"]))

    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=_s(data.get("company_name")) + " — Investor One-Pager",
    )
    doc.build(story)


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
def _header(story: list, data: dict, st: dict) -> None:
    name = _s(data.get("company_name")) or "Company"
    story.append(Paragraph(_esc(name), st["title"]))
    tagline = _s(data.get("tagline"))
    if tagline:
        story.append(Paragraph(_esc(tagline), st["tagline"]))
    story.append(_rule(1.3, ACCENT, space_before=3, space_after=1))


class _HRule(Flowable):
    def __init__(self, width, thickness, color, space_before, space_after):
        super().__init__()
        self.width = width
        self.thickness = thickness
        self.color = color
        self.space_before = space_before
        self.space_after = space_after
        self.height = thickness + space_before + space_after

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        return (availWidth, self.height)

    def draw(self):
        c = self.canv
        y = self.space_after
        c.setStrokeColor(self.color)
        c.setLineWidth(self.thickness)
        c.line(0, y, self.width, y)


def _rule(thickness, color, space_before=0, space_after=0) -> _HRule:
    return _HRule(CONTENT_W, thickness, color, space_before, space_after)


# --------------------------------------------------------------------------- #
# Section builders — each returns a list of flowables (or [] if empty)
# --------------------------------------------------------------------------- #
def _narrative(node, st) -> list:
    body = _s((node or {}).get("body"))
    if not body:
        return []
    return [Paragraph(_esc(body), st["body"])]


def _bullets(node, st) -> list:
    bullets = (node or {}).get("bullets") or []
    out = []
    for b in bullets:
        label = _s(b.get("label"))
        detail = _s(b.get("detail"))
        est = bool(b.get("is_estimated"))
        if not (label or detail):
            continue
        if label:
            text = f"<b>{_esc(label)}</b> — {_est(detail, est)}"
        else:
            text = _est(detail, est)
        out.append(Paragraph(f"•&nbsp;&nbsp;{text}", st["bullet"]))
    return out


def _product(node, st) -> list:
    out = _bullets(node, st)
    summary = _s((node or {}).get("summary"))
    if summary:
        out.append(Paragraph(_esc(summary), st["body"]))
    return out if (out or summary) else []


def _market(node, st) -> list:
    node = node or {}
    out = []
    for key, label in (("tam", "TAM"), ("sam", "SAM"), ("som", "SOM")):
        val = _s(node.get(key))
        if not val:
            continue
        est = bool(node.get(f"{key}_is_estimated"))
        out.append(
            Paragraph(f"<b>{label}:</b> {_est(val, est)}", st["bullet"])
        )
    return out


def _team(node, st) -> list:
    members = (node or {}).get("members") or []
    out = []
    for m in members:
        name = _s(m.get("name"))
        role = _s(m.get("role"))
        bio = _s(m.get("bio"))
        if not (name or role or bio):
            continue
        head = "<b>" + _esc(name) + "</b>"
        if role:
            head += ", " + _esc(role)
        line = head + (" — " + _esc(bio) if bio else "")
        out.append(Paragraph(f"•&nbsp;&nbsp;{line}", st["bullet"]))
    return out


def _fundraise(node, st) -> list:
    node = node or {}
    out = []
    body = _s(node.get("body"))
    if body:
        out.append(Paragraph(_esc(body), st["body"]))

    parts = []
    amount = _s(node.get("amount"))
    if amount:
        parts.append(("Amount", _est(amount, bool(node.get("amount_is_estimated")))))
    rnd = _s(node.get("round_type"))
    if rnd:
        parts.append(("Round", _esc(rnd)))
    val = _s(node.get("valuation"))
    if val:
        parts.append(("Valuation", _est(val, bool(node.get("valuation_is_estimated")))))
    inst = _s(node.get("instrument"))
    if inst:
        parts.append(("Instrument", _esc(inst)))
    if parts:
        line = "  •  ".join(f"<b>{lbl}:</b> {v}" for lbl, v in parts)
        out.append(Paragraph(line, st["body"]))
    return out


def _use_of_funds(node, st) -> list:
    node = node or {}
    out = _bullets(node, st)
    allocation = node.get("allocation") or []
    valid = [a for a in allocation if _s(a.get("category"))]
    if valid:
        out.append(Spacer(1, 3))
        out.append(AllocationBars(valid, CONTENT_W))
    return out


def _financials(node, st) -> list:
    node = node or {}
    out = []
    body = _s(node.get("body"))
    if body:
        out.append(Paragraph(_esc(body), st["body"]))

    years = [_s(y) for y in (node.get("years") or [])]
    rows = node.get("rows") or []
    if years and rows:
        header = [Paragraph("Metric", st["cell_head"])] + [
            Paragraph(_esc(y), st["cell_head"]) for y in years
        ]
        table_data = [header]
        n = len(years)
        for r in rows:
            metric = _s(r.get("metric"))
            values = [_s(v) for v in (r.get("values") or [])]
            flags = list(r.get("is_estimated") or [])
            # Align values/flags to the year count.
            values = (values + [""] * n)[:n]
            flags = (flags + [False] * n)[:n]
            cells = [Paragraph(f"<b>{_esc(metric)}</b>", st["cell"])]
            for v, f in zip(values, flags):
                cells.append(Paragraph(_est(v, bool(f)), st["cell"]))
            table_data.append(cells)

        metric_w = CONTENT_W * 0.22
        year_w = (CONTENT_W - metric_w) / n
        col_widths = [metric_w] + [year_w] * n
        tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(_financial_table_style(len(table_data)))
        out.append(Spacer(1, 2))
        out.append(tbl)
    return out


def _key_metrics(node, st) -> list:
    node = node or {}
    fields = [
        ("Funding Ask", "funding_ask", "funding_ask_is_estimated"),
        ("Round Type", "round_type", None),
        ("Pre-Money Valuation", "pre_money_valuation", "pre_money_valuation_is_estimated"),
        ("Revenue", "revenue", "revenue_is_estimated"),
        ("YoY Growth", "yoy_growth", "yoy_growth_is_estimated"),
        ("Gross Margin", "gross_margin", "gross_margin_is_estimated"),
        ("Key Customer / Partner", "key_customer_or_partner", None),
        ("Target Market", "target_market", None),
    ]
    rows = []
    for label, key, flag_key in fields:
        val = _s(node.get(key))
        if not val:
            continue
        est = bool(node.get(flag_key)) if flag_key else False
        rows.append(
            [Paragraph(_esc(label), st["cell_label"]),
             Paragraph(_est(val, est), st["cell"])]
        )
    if not rows:
        return []
    label_w = CONTENT_W * 0.30
    tbl = Table(rows, colWidths=[label_w, CONTENT_W - label_w])
    tbl.setStyle(_snapshot_table_style())
    return [tbl]


# --------------------------------------------------------------------------- #
# Table styles
# --------------------------------------------------------------------------- #
def _financial_table_style(n_rows: int) -> TableStyle:
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEAD_BG),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    for r in range(1, n_rows):
        if r % 2 == 0:
            cmds.append(("BACKGROUND", (0, r), (-1, r), TABLE_ALT_BG))
    return TableStyle(cmds)


def _snapshot_table_style() -> TableStyle:
    cmds = [
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("BACKGROUND", (0, 0), (0, -1), TABLE_ALT_BG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    return TableStyle(cmds)


# --------------------------------------------------------------------------- #
# Estimate detection (for the footnote)
# --------------------------------------------------------------------------- #
def _has_estimates(data: dict) -> bool:
    def any_bullet(node) -> bool:
        return any(b.get("is_estimated") for b in (node or {}).get("bullets") or [])

    if any_bullet(data.get("problem")):
        return True
    if any_bullet(data.get("product")):
        return True
    if any_bullet(data.get("traction")):
        return True
    if any_bullet(data.get("business_model")):
        return True
    if any_bullet(data.get("competitive_advantage")):
        return True
    if any_bullet(data.get("go_to_market")):
        return True
    if any_bullet(data.get("use_of_funds")):
        return True

    ms = data.get("market_size") or {}
    if any(ms.get(k) for k in ("tam_is_estimated", "sam_is_estimated", "som_is_estimated")):
        return True

    fr = data.get("fundraise") or {}
    if fr.get("amount_is_estimated") or fr.get("valuation_is_estimated"):
        return True

    for a in (data.get("use_of_funds") or {}).get("allocation") or []:
        if a.get("is_estimated"):
            return True

    for r in (data.get("financial_outlook") or {}).get("rows") or []:
        if any(r.get("is_estimated") or []):
            return True

    km = data.get("key_metrics") or {}
    if any(km.get(k) for k in (
        "funding_ask_is_estimated", "pre_money_valuation_is_estimated",
        "revenue_is_estimated", "yoy_growth_is_estimated", "gross_margin_is_estimated",
    )):
        return True

    return False
