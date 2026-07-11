"""Pitch Deck -> One-Pager PDF generator (CLI entry point).

Usage:
    python main.py path/to/deck.pptx [--output out.pdf]

Pipeline: extract (Stage 1) -> analyze via Claude (Stage 2) -> render PDF (Stage 3).
"""

from __future__ import annotations

import argparse
import os
import sys

from analyze import AnalysisError, analyze
from extract import UnsupportedFileError, extract
from render import render


def _default_output(input_file: str) -> str:
    stem = os.path.splitext(os.path.basename(input_file))[0]
    return f"{stem}_onepager.pdf"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn an investor pitch deck (.pptx/.pdf/.docx) into a "
        "clean one-page investor PDF using the Anthropic API."
    )
    parser.add_argument("input_file", help="Path to the pitch deck (.pptx, .pdf, or .docx)")
    parser.add_argument(
        "--output",
        "-o",
        help="Output PDF path (default: <input_stem>_onepager.pdf)",
    )
    args = parser.parse_args(argv)

    output = args.output or _default_output(args.input_file)

    # Stage 1 — extraction.
    try:
        print(f"[1/3] Extracting content from {args.input_file} ...")
        deck_text = extract(args.input_file)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except UnsupportedFileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface a clean message for any parser error
        print(f"Error while reading the file: {exc}", file=sys.stderr)
        return 1

    if not deck_text.strip():
        print(
            "Error: no readable text was found in the file. "
            "Is the deck image-only or empty?",
            file=sys.stderr,
        )
        return 1

    # Stage 2 — analysis.
    try:
        print(f"[2/3] Analyzing with Claude ...")
        data = analyze(deck_text)
    except AnalysisError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Stage 3 — rendering.
    try:
        print(f"[3/3] Rendering PDF -> {output} ...")
        render(data, output)
    except Exception as exc:  # noqa: BLE001 - reportlab can raise various errors
        print(f"Error while rendering the PDF: {exc}", file=sys.stderr)
        return 1

    print(f"Done. Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
