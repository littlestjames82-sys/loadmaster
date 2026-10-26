"""LoadMaster report writer.

Turns the deterministic rules engine's verdicts into a short,
plain-words, fix-first summary a driver can act on. Muse Spark
writes the prose when it is reachable; a deterministic template
covers any failure, so callers never get a blank report.

Grounding rule: the writer may only use the verdict data it is
given. The rules engine owns the facts; this module owns the words.
It never invents numbers, ratings, or checks that are not in the
verdict list, and it never upgrades a verdict.
"""

from __future__ import annotations

import time
import urllib.error

from .sam_client import _extract_output_text, _post

MODEL = "muse-spark-1.3"

CHECK_LABELS = {
    "forward_blocking": "Forward blocking",
    "tiedown_count": "Tie-down count",
    "heavy_equipment": "Heavy equipment securement",
    "overhang_lights_plate": "Overhang, lights and plate",
    "weight_placement": "Weight placement",
    "tarp_coverage": "Tarp coverage",
    "edge_protection": "Edge protection",
    "aggregate_wll": "Tie-down strength (aggregate working load limit)",
}

_STATUS_ORDER = {"CHECK THIS": 0, "PASS": 1, "COULD NOT TELL": 2}


def check_label(verdict: dict) -> str:
    key = (
        verdict.get("check")
        or verdict.get("check_id")
        or verdict.get("name")
        or "check"
    )
    return CHECK_LABELS.get(str(key), str(key).replace("_", " "))


def _status(verdict: dict) -> str:
    return str(verdict.get("status") or "COULD NOT TELL").upper()


def ordered(verdicts: list[dict]) -> list[dict]:
    """CHECK THIS first (engine's risk order preserved), then PASS,
    then COULD NOT TELL."""
    return sorted(
        verdicts, key=lambda v: _STATUS_ORDER.get(_status(v), 3)
    )


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(". ", 1)[0]
    return (cut if len(cut) > 40 else text[:limit]) + "."


def fallback_report(verdicts: list[dict], inputs: dict | None = None) -> dict:
    """Deterministic summary built only from the verdict list."""
    vs = ordered(verdicts)
    fixes = [v for v in vs if _status(v) == "CHECK THIS"]
    passes = [v for v in vs if _status(v) == "PASS"]
    unknowns = [v for v in vs if _status(v) == "COULD NOT TELL"]

    if fixes:
        headline = (
            f"{len(fixes)} thing{'s' if len(fixes) != 1 else ''} to fix "
            "before this load rolls"
        )
    elif passes and not unknowns:
        headline = "This load passes every check the photo could judge"
    elif passes:
        headline = (
            "Nothing flagged, but "
            f"{len(unknowns)} check{'s' if len(unknowns) != 1 else ''} "
            "could not be judged"
        )
    else:
        headline = "This photo could not settle any of the checks"

    lines: list[str] = []
    for i, v in enumerate(fixes):
        lead = "Fix first: " if i == 0 else "Also fix: "
        detail = v.get("fix") or v.get("finding") or ""
        lines.append(f"{lead}{check_label(v)}. {_clip(detail, 220)}")
    if passes:
        lines.append(
            "Passed: " + ", ".join(check_label(v).lower() for v in passes) + "."
        )
    for v in unknowns:
        lines.append(
            f"Could not tell: {check_label(v)}. "
            + _clip(v.get("finding") or "", 200)
        )
    if unknowns:
        lines.append(
            "A photo cannot verify strap tension, tie-down ratings, or "
            "load weight; those stay unjudged until the numbers are supplied."
        )
    return {
        "headline": headline,
        "body": "\n".join(lines),
        "source": "template",
        "model": None,
    }


def build_prompt(verdicts: list[dict], inputs: dict | None = None) -> str:
    inputs = inputs or {}
    load_bits = []
    if inputs.get("trailer_type"):
        load_bits.append(f"trailer: {inputs['trailer_type']}")
    if inputs.get("load_type"):
        load_bits.append(f"load: {inputs['load_type']}")
    if inputs.get("load_length_ft") is not None:
        load_bits.append(f"load length: {inputs['load_length_ft']} ft")
    if inputs.get("weight_lb") is not None:
        load_bits.append(f"weight: {inputs['weight_lb']} lb")
    header = "; ".join(load_bits) if load_bits else "trailer load"

    rows = []
    for v in ordered(verdicts):
        row = f"- [{_status(v)}] {check_label(v)}"
        if v.get("citation"):
            row += f" ({v['citation']})"
        if v.get("finding"):
            row += f": {_clip(v['finding'], 300)}"
        if v.get("fix"):
            row += f" Minimum fix: {_clip(v['fix'], 200)}"
        rows.append(row)

    return (
        "You are the report writer for LoadMaster, a cargo securement "
        "checker. A deterministic rules engine (49 CFR 393) has already "
        "judged this load; your only job is to say its results in plain "
        "words for the driver.\n\n"
        f"Load: {header}.\n"
        "Verdicts, already ordered most important first:\n"
        + "\n".join(rows)
        + "\n\nWrite the report under these rules:\n"
        "- Use ONLY the facts above. Never invent numbers, ratings, or "
        "problems that are not listed.\n"
        "- First line: a headline of at most 12 words, no period at the "
        "end.\n"
        "- Then a blank line, then the body: short plain sentences, "
        "fixes first in the order given, each with its minimum fix.\n"
        "- For every COULD NOT TELL check, say briefly what would be "
        "needed to judge it. Never guess at it.\n"
        "- End by noting that a photo cannot verify strap tension, "
        "tie-down ratings, or load weight, if any check could not be "
        "told.\n"
        "- No markdown, no bullets, no headings. At most 160 words "
        "after the headline."
    )


def write_report(
    verdicts: list[dict] | None,
    inputs: dict | None = None,
    _poster=None,
    timeout: float = 60.0,
    max_attempts: int = 2,
) -> dict:
    """Spark-written report, template on any failure. Never raises."""
    if not verdicts:
        return {
            "headline": "No verdicts to summarize yet",
            "body": "Run an analysis with a load length to get rule "
            "verdicts for this load.",
            "source": "template",
            "model": None,
        }
    poster = _poster or _post
    payload = {
        "model": MODEL,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": build_prompt(verdicts, inputs)}
                ],
            }
        ],
        "stream": False,
    }
    for attempt in range(1, max_attempts + 1):
        try:
            parsed = poster(payload, timeout)
            raw = _extract_output_text(parsed).strip()
            if not raw:
                raise ValueError("Spark returned no output text")
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            headline = lines[0].rstrip(".")
            body = "\n".join(lines[1:]).strip()
            if len(headline) > 140 or not body:
                raise ValueError("Spark report shape not usable")
            return {
                "headline": headline,
                "body": body[:2400],
                "source": "spark",
                "model": MODEL,
            }
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                break
            if attempt < max_attempts:
                time.sleep(2.0 * attempt)
        except Exception:
            if attempt < max_attempts:
                time.sleep(2.0 * attempt)
    return fallback_report(verdicts, inputs)
