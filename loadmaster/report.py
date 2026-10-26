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


_ES = {
    "headline_fixes": "{n} cosa{s} por corregir antes de que esta carga salga",
    "headline_pass": "Esta carga pasa todas las revisiones que la foto pudo juzgar",
    "headline_none_flagged": "Nada marcado, pero {n} revision{es} no se pudo juzgar",
    "headline_none": "Esta foto no pudo resolver ninguna de las revisiones",
    "fix_first": "Corrija primero: ",
    "also_fix": "Tambien corrija: ",
    "passed": "Aprobado: ",
    "could_not_tell": "No se pudo determinar: ",
    "honesty": (
        "Una foto no puede verificar la tension de las correas, la "
        "capacidad de los amarres ni el peso de la carga; eso queda "
        "sin juzgar hasta que se den los numeros."
    ),
    "no_verdicts_headline": "Aun no hay veredictos para resumir",
    "no_verdicts_body": (
        "Ejecute un analisis con la longitud de la carga para obtener "
        "los veredictos de las reglas para esta carga."
    ),
}


def _es_fallback_report(verdicts: list[dict]) -> dict:
    """Spanish template report. Check names and fixes stay in the
    engine's words on purpose: the template never invents a
    translation for a rule citation or a minimum fix."""
    vs = ordered(verdicts)
    fixes = [v for v in vs if _status(v) == "CHECK THIS"]
    passes = [v for v in vs if _status(v) == "PASS"]
    unknowns = [v for v in vs if _status(v) == "COULD NOT TELL"]
    if fixes:
        headline = _ES["headline_fixes"].format(
            n=len(fixes), s="s" if len(fixes) != 1 else ""
        )
    elif passes and not unknowns:
        headline = _ES["headline_pass"]
    elif passes:
        headline = _ES["headline_none_flagged"].format(
            n=len(unknowns), es="es" if len(unknowns) != 1 else ""
        )
    else:
        headline = _ES["headline_none"]
    lines: list[str] = []
    for i, v in enumerate(fixes):
        lead = _ES["fix_first"] if i == 0 else _ES["also_fix"]
        detail = v.get("fix") or v.get("finding") or ""
        lines.append(f"{lead}{check_label(v)}. {_clip(detail, 220)}")
    if passes:
        lines.append(
            _ES["passed"] + ", ".join(check_label(v).lower() for v in passes) + "."
        )
    for v in unknowns:
        lines.append(
            f"{_ES['could_not_tell']}{check_label(v)}. "
            + _clip(v.get("finding") or "", 200)
        )
    if unknowns:
        lines.append(_ES["honesty"])
    return {"headline": headline, "body": "\n".join(lines), "source": "template", "model": None}


def fallback_report(verdicts: list[dict], inputs: dict | None = None, lang: str = "en") -> dict:
    """Deterministic summary built only from the verdict list."""
    if lang == "es":
        return _es_fallback_report(verdicts)
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


def build_prompt(verdicts: list[dict], inputs: dict | None = None, lang: str = "en") -> str:
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
        + (
            "\n- Write the headline and the body in Spanish (neutral "
            "Latin American Spanish a driver would use at work). Keep "
            "rule citations like 49 CFR 393.110 exactly as given."
            if lang == "es"
            else ""
        )
    )


def write_report(
    verdicts: list[dict] | None,
    inputs: dict | None = None,
    _poster=None,
    timeout: float = 60.0,
    max_attempts: int = 2,
    lang: str = "en",
) -> dict:
    """Spark-written report, template on any failure. Never raises."""
    if not verdicts:
        if lang == "es":
            return {
                "headline": _ES["no_verdicts_headline"],
                "body": _ES["no_verdicts_body"],
                "source": "template",
                "model": None,
                "lang": "es",
            }
        return {
            "headline": "No verdicts to summarize yet",
            "body": "Run an analysis with a load length to get rule "
            "verdicts for this load.",
            "source": "template",
            "model": None,
            "lang": "en",
        }
    poster = _poster or _post
    payload = {
        "model": MODEL,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": build_prompt(verdicts, inputs, lang=lang)}
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
                "lang": lang,
            }
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                break
            if attempt < max_attempts:
                time.sleep(2.0 * attempt)
        except Exception:
            if attempt < max_attempts:
                time.sleep(2.0 * attempt)
    fb = fallback_report(verdicts, inputs, lang=lang)
    fb["lang"] = lang
    return fb
