"""LoadMaster web service: the working app behind the phone UI.

A FastAPI wrapper around the existing pipeline (Days 1-3):

- GET  /         -> the mobile web UI (web_ui.html, single page)
- GET  /health   -> {"ok": true}
- POST /analyze  -> multipart photo + form fields. Runs the REAL
  pipeline (pipeline.run_pipeline: SAM prompt pack in parallel, mask
  decode, detections, overlay, measurements when a bed length is
  given, rules verdicts when a load length is given) and returns
  everything as JSON, overlay included as base64 JPEG.
- POST /verdicts -> JSON {detections, measurements, inputs}. Recomputes
  ONLY the rules verdicts from a (possibly user-corrected) detection
  set. No SAM calls: this is what powers tap-to-correct + instant
  recheck in the UI.

The rules engine (loadmaster.rules, Day 3) is imported LAZILY and
called through a small adapter, so this service runs before, during,
and after that module lands; when it is unavailable, verdicts are
omitted and the response warnings say why.

Error honesty, same as the pipeline: a SAM prompt that fails after
retries surfaces in run.prompts with status "error" and in warnings -
never as an empty detection list pretending nothing was there.

Runs locally:  uvicorn loadmaster.webapp:app --port 8000
Runs hosted:   modal_app.py wraps this app on Modal, where the Meta
Model API key arrives as the META_API_KEY env var (see sam_client).
"""

from __future__ import annotations

import base64
import html as html_lib
import inspect
import io
import json
import math
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image

from .detections import DetectionSet
from .pipeline import run_pipeline
from .report import write_report

app = FastAPI(title="LoadMaster", version="0.1.0")

_UI_PATH = Path(__file__).with_name("web_ui.html")
_OVERLAY_MAX_DIM = 1280


# ---------------------------------------------------------------------
# Rules adapter (Day 3 engine, imported lazily).
# ---------------------------------------------------------------------

_RULES_ENTRY_POINTS = (
    "evaluate_rules", "evaluate", "run_rules", "check_load", "assess",
    "run_checks", "evaluate_checks", "verdicts", "check",
)

# The UI's load-type vocabulary names cargo (firewood, steel, ...) for
# the SAM prompt packs; the rules engine's UserInputs names the load
# CLASS (49 CFR 393's framing). The web layer bridges the two.
_HEAVY_LOAD_TYPES = {"heavy_equipment", "excavator", "wheel_loader", "heavy_wheeled_tracked"}
_LOOSE_LOAD_TYPES = {"loose_material", "hay"}


def _rules_inputs(inputs: dict) -> dict:
    """Translate web inputs into the rules engine's input vocabulary."""
    mapped = dict(inputs or {})
    load_type = str(mapped.get("load_type") or "generic").lower()
    if load_type in _HEAVY_LOAD_TYPES:
        mapped["load_type"] = "heavy_wheeled_tracked"
    elif load_type in _LOOSE_LOAD_TYPES:
        mapped["load_type"] = "loose_material"
    else:
        mapped["load_type"] = "general"
    return mapped


def _norm_status(value) -> str:
    if value is None:
        return "COULD NOT TELL"
    text = str(value).strip().upper().replace("_", " ")
    if text in ("PASS", "PASSED", "OK"):
        return "PASS"
    if text.startswith("CHECK"):
        return "CHECK THIS"
    if text.startswith(("COULD NOT", "UNKNOWN", "UNABLE", "INDETERMINATE")):
        return "COULD NOT TELL"
    return text


def _verdict_dict(raw, fallback_name: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raw = vars(raw) if hasattr(raw, "__dict__") else {"finding": str(raw)}
    out = dict(raw)
    out["check"] = (
        raw.get("check") or raw.get("check_id") or raw.get("name")
        or raw.get("id") or raw.get("title") or fallback_name or "check"
    )
    out["status"] = _norm_status(
        raw.get("status") or raw.get("verdict") or raw.get("result")
    )
    out["citation"] = (
        raw.get("citation") or raw.get("cfr") or raw.get("cfr_citation")
        or raw.get("rule") or ""
    )
    out["finding"] = (
        raw.get("finding") or raw.get("message") or raw.get("detail")
        or raw.get("summary") or raw.get("description") or ""
    )
    out["fix"] = (
        raw.get("fix") or raw.get("minimum_fix") or raw.get("remediation")
        or raw.get("action") or ""
    )
    return out


def _normalize_verdicts(raw) -> list[dict] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        for key in ("verdicts", "checks", "results"):
            if isinstance(raw.get(key), list):
                return [_verdict_dict(v) for v in raw[key]]
        if raw and all(isinstance(v, dict) or hasattr(v, "__dict__") for v in raw.values()):
            return [_verdict_dict(v, name) for name, v in raw.items()]
        return [_verdict_dict(raw)]
    if isinstance(raw, (list, tuple)):
        return [_verdict_dict(v) for v in raw]
    return None


def _call_rules(fn, ds_dict, ds_obj, measurements, inputs, crosscheck=None, extra_sets=None):
    """Call the rules entry point with whatever shape it accepts.

    The Day 3 engine's evaluate_rules takes a LIST of detection sets
    (one per photo of the load), so the pool leads with that shape.
    extra_sets are the additional photos' detection-set dicts for a
    multi-photo run: verdicts are computed over ALL of them.
    """
    sets_dicts = [ds_dict] if isinstance(ds_dict, dict) else ds_dict
    sets_objs = [ds_obj] if ds_obj is not None else sets_dicts
    for extra in extra_sets or []:
        sets_dicts.append(extra)
        try:
            sets_objs.append(DetectionSet.from_dict(extra))
        except Exception:
            sets_objs.append(extra)
    pool = {
        "sets": sets_objs,
        "detections": ds_dict,
        "detection_set": ds_obj,
        "measurements": measurements,
        "inputs": inputs,
        "user_inputs": inputs,
        "crosscheck": crosscheck,
        "load_length_ft": inputs.get("load_length_ft"),
        "load_type": inputs.get("load_type"),
        "weight_lb": inputs.get("weight_lb"),
        "trailer_type": inputs.get("trailer_type"),
        "bed_length_ft": inputs.get("bed_length_ft"),
    }
    try:
        params = inspect.signature(fn).parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return fn(**pool)
        kwargs = {k: v for k, v in pool.items() if k in params}
        required = [
            name for name, p in params.items()
            if p.default is inspect.Parameter.empty
            and p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        ]
        if all(name in kwargs for name in required) and kwargs:
            return fn(**kwargs)
    except (TypeError, ValueError):
        pass
    last_error: Exception | None = None
    for args in (
        (sets_objs, measurements, inputs),
        (sets_dicts, measurements, inputs),
        (ds_dict, measurements, inputs),
        (ds_obj, measurements, inputs),
        (ds_dict, inputs),
        (ds_obj, inputs),
        (ds_dict,),
        (ds_obj,),
    ):
        try:
            return fn(*args)
        except TypeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("could not call the rules entry point")


def compute_verdicts(ds_dict: dict, measurements: dict | None, inputs: dict, crosscheck=None, extra_sets=None):
    """Return (verdicts | None, warnings, raw_report | None). Never raises."""
    warnings: list[str] = []
    try:
        from . import rules as rules_module
    except Exception as exc:  # module not landed yet, or import error
        return None, [
            f"verdicts not computed: the rules engine is not available yet "
            f"({type(exc).__name__}: {exc})"
        ], None
    fn = None
    for name in _RULES_ENTRY_POINTS:
        candidate = getattr(rules_module, name, None)
        if callable(candidate):
            fn = candidate
            break
    if fn is None:
        return None, [
            "verdicts not computed: the rules engine module is present "
            "but exposes no known entry point"
        ], None
    ds_obj = None
    try:
        ds_obj = DetectionSet.from_dict(ds_dict)
    except Exception:
        ds_obj = None
    try:
        raw = _call_rules(fn, ds_dict, ds_obj, measurements, _rules_inputs(inputs), crosscheck=crosscheck, extra_sets=extra_sets)
    except Exception as exc:
        return None, [
            f"verdicts not computed: the rules engine call failed "
            f"({type(exc).__name__}: {exc})"
        ], None
    verdicts = _normalize_verdicts(raw)
    if verdicts is None:
        warnings.append("verdicts not computed: the rules engine returned nothing usable")
    report = raw if isinstance(raw, dict) else None
    return verdicts, warnings, report


# ---------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------

def _overlay_b64(png_path: str | None) -> str | None:
    if not png_path:
        return None
    img = Image.open(png_path).convert("RGB")
    img.thumbnail((_OVERLAY_MAX_DIM, _OVERLAY_MAX_DIM))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _user_inputs(trailer_type, load_type, bed_length_ft, load_length_ft, weight_lb) -> dict:
    return {
        "trailer_type": trailer_type,
        "load_type": load_type,
        "bed_length_ft": bed_length_ft,
        "load_length_ft": load_length_ft,
        "weight_lb": weight_lb,
    }


# ---------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    try:
        return _UI_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "<!doctype html><title>LoadMaster</title>"
            "<h1>LoadMaster</h1><p>The UI file (web_ui.html) is missing.</p>"
        )


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------
# Judges' sample gallery: precomputed REAL pipeline runs (captured
# from this service's /analyze on openly licensed development
# photos), so the results can be read without running a live
# analysis. Nothing here is hand-written: verdicts, measurements,
# overlays, and reports all come from actual runs.
# ---------------------------------------------------------------------

_GALLERY_DIR = Path(__file__).with_name("gallery")
_GALLERY_ORDER = ("firewood", "lowboy", "closeup")


def load_gallery_entries() -> list[dict]:
    entries: list[dict] = []
    for slug in _GALLERY_ORDER:
        path = _GALLERY_DIR / f"{slug}.json"
        try:
            entries.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return entries


_GALLERY_CROSS_LABELS = {
    "load_looks_rear_heavy": "Load looks rear-heavy",
    "front_blocked": "Front of the load is blocked",
    "tail_lights_visible": "Tail lights visible",
    "license_plate_visible": "License plate visible",
    "tarp_covering_load": "Tarp covering the load",
    "tiedown_count": "Tie-downs it counted",
    "straps_over_sharp_edge_without_protection": "Strap over a sharp edge with no protection",
}


def _gallery_cross_line(entry: dict, esc) -> str:
    records = entry.get("crosscheck") or []
    bits = []
    for r in records:
        if r.get("status") != "ok":
            continue
        for k, v in (r.get("view") or {}).items():
            label = _GALLERY_CROSS_LABELS.get(k, k.replace("_", " "))
            val = "Yes" if v is True or v == "yes" else "No" if v is False or v == "no" else "Could not tell" if v in (None, "unclear") else str(v)
            bits.append(f"{label}: {val}")
    if not bits:
        return ""
    return (
        "<div class=\"credit\" style=\"margin-top:8px;\">Second opinion "
        f"(Muse Spark, independent read): {esc(' · '.join(bits))}</div>"
    )


def _gallery_html(entries: list[dict]) -> str:
    from .report import check_label

    esc = html_lib.escape
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta property=\"og:title\" content=\"LoadMaster - sample runs\">"
        "<meta property=\"og:description\" content=\"Real runs: SAM 3.1 detections, calibrated measurements, 49 CFR 393 verdicts, and a Muse Spark second opinion, on real trailer photos.\">"
        "<meta property=\"og:image\" content=\"https://littlestjames82-sys--loadmaster-fastapi-app.us-east.modal.run/icon-512.png\">"
        "<link rel=\"icon\" type=\"image/png\" href=\"/icon-192.png\">"
        "<title>LoadMaster - Sample runs</title><style>"
        "body{margin:0;background:#0b0f14;color:#e8eef4;font-family:system-ui,sans-serif;}"
        "main{max-width:860px;margin:0 auto;padding:18px 14px 40px;}"
        "h1{font-size:24px;margin:6px 0 2px;} h1 span{color:#f5b301;}"
        ".sub{color:#8fa1b3;font-size:14px;line-height:1.5;}"
        "a{color:#f5b301;font-weight:700;text-decoration:none;}"
        ".run{background:#131a23;border:1px solid #26313f;border-radius:14px;padding:16px;margin:18px 0;}"
        ".run h2{font-size:19px;margin:0 0 4px;}"
        ".run img{width:100%;border-radius:10px;margin:10px 0;}"
        ".headline{font-weight:800;font-size:16.5px;margin:8px 0 6px;}"
        ".body{white-space:pre-wrap;line-height:1.55;font-size:14.5px;}"
        "table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13.5px;}"
        "th,td{border:1px solid #26313f;padding:6px 8px;text-align:left;vertical-align:top;}"
        "th{background:#0b0f14;}"
        ".st{font-weight:800;white-space:nowrap;}"
        ".pass{color:#35d07f;} .check{color:#f5b301;} .tell{color:#8fa1b3;}"
        ".chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0;}"
        ".chip2{border:1px solid #26313f;border-radius:999px;padding:3px 10px;font-size:12.5px;color:#8fa1b3;}"
        ".credit{color:#8fa1b3;font-size:12.5px;margin-top:10px;}"
        "</style></head><body><main>"
        "<h1>Load<span>Master</span> - sample runs</h1>"
        "<p class=\"sub\">Real runs of the live pipeline (SAM 3.1 + "
        "measurements + the deterministic 49 CFR 393 rules engine + a "
        "Muse Spark summary), captured from this app on openly "
        "licensed development photos. Prefer live? "
        "<a href=\"/\">Analyze your own photo</a>.</p>",
    ]
    for e in entries:
        rep = e.get("report") or {}
        chips = "".join(
            f"<span class=\"chip2\">{esc(str(d.get('label') or d.get('kind') or 'finding'))}"
            + (f" {round(float(d['confidence']) * 100)}%" if d.get("confidence") is not None else "")
            + "</span>"
            for d in (e.get("detections") or [])
        )
        rows = ""
        for v in e.get("verdicts") or []:
            status = str(v.get("status") or "")
            cls = "pass" if status == "PASS" else "check" if status == "CHECK THIS" else "tell"
            rows += (
                f"<tr><td>{esc(check_label(v))}</td>"
                f"<td class=\"st {cls}\">{esc(status)}</td>"
                f"<td>{esc(str(v.get('finding') or ''))}</td>"
                f"<td>{esc(str(v.get('fix') or ''))}</td></tr>"
            )
        parts.append(
            f"<section class=\"run\"><h2>{esc(str(e.get('title') or ''))}</h2>"
            f"<div class=\"sub\">{esc(str(e.get('blurb') or ''))}</div>"
            + (
                f"<img alt=\"Analyzed photo with findings outlined\" src=\"data:{esc(str(e.get('overlay_mime') or 'image/jpeg'))};base64,{e.get('overlay_png_base64')}\">"
                if e.get("overlay_png_base64")
                else ""
            )
            + f"<div class=\"chips\">{chips}</div>"
            + _gallery_cross_line(e, esc)
            + (f"<div class=\"headline\">{esc(str(rep.get('headline') or ''))}</div>" if rep.get("headline") else "")
            + (f"<div class=\"body\">{esc(str(rep.get('body') or ''))}</div>" if rep.get("body") else "")
            + (f"<table><tr><th>Check</th><th>Verdict</th><th>Finding</th><th>Minimum fix</th></tr>{rows}</table>" if rows else "")
            + f"<div class=\"credit\">{esc(str(e.get('photo_credit') or ''))}</div></section>"
        )
    parts.append(
        "<p class=\"sub\">A photo cannot verify strap tension, tie-down "
        "ratings, or load weight; checks needing those are marked COULD "
        "NOT TELL rather than guessed. These runs were captured on "
        "Oct 26, 2026; the forward-blocking PASS bar was tightened "
        "afterward (the load must sit within 2 in of the front, not "
        "6), and no verdict above changes under the new bar. "
        "LoadMaster by Ghost Developer Studio.</p></main></body></html>"
    )
    return "".join(parts)


@app.get("/manifest.webmanifest")
def manifest():
    return {
        "name": "LoadMaster",
        "short_name": "LoadMaster",
        "description": "Checks a loaded trailer against federal cargo securement rules (49 CFR Part 393) and says in plain words what to fix.",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d0e13",
        "theme_color": "#0d0e13",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }


@app.get("/icon-192.png")
def icon_192():
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).parent / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
def icon_512():
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).parent / "icon-512.png", media_type="image/png")


@app.get("/gallery", response_class=HTMLResponse)
def gallery() -> str:
    return _gallery_html(load_gallery_entries())


# ------------------------------------------------------------------
# Intake hardening + abuse guard.
# Phone photos carry EXIF orientation: without transposing, SAM sees
# a sideways trailer and every measurement lands wrong. Oversized or
# unreadable uploads get a plain refusal instead of a stack trace.
# The rate limit protects the shared Meta API credits behind this
# public URL during judging: generous for a person, useless for a
# script.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_IMAGE_EDGE = 2048
_ANALYZE_LIMIT = 30
_ANALYZE_WINDOW_S = 3600
_analyze_hits: dict[str, list[float]] = {}


def _normalize_upload(raw: bytes) -> bytes:
    """Validate, auto-rotate, and downscale one uploaded photo.
    Returns JPEG bytes. Raises ValueError with a readable message."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("that photo is over 25 MB; please send a smaller copy")
    try:
        from PIL import ImageOps

        img = Image.open(io.BytesIO(raw))
        # The header alone declares the pixel count: refuse image
        # bombs before a single pixel is decoded.
        if img.size[0] * img.size[1] > 64_000_000:
            raise ValueError(
                "that photo is extremely large (over 64 megapixels); "
                "please send a smaller copy"
            )
        if img.format == "JPEG":
            # Decode JPEGs at reduced scale natively: same result
            # after our downscale, a fraction of the memory.
            img.draft("RGB", (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        img.load()
    except ValueError:
        raise
    except Exception:
        raise ValueError("that file is not a readable photo (JPEG, PNG, or WebP works)")
    if img.format not in ("JPEG", "PNG", "WEBP"):
        raise ValueError(
            f"unsupported photo format {img.format}; JPEG, PNG, or WebP works"
        )
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_IMAGE_EDGE:
        scale = MAX_IMAGE_EDGE / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _analyze_hits.get(ip, []) if now - t < _ANALYZE_WINDOW_S]
    if len(hits) >= _ANALYZE_LIMIT:
        _analyze_hits[ip] = hits
        return True
    hits.append(now)
    _analyze_hits[ip] = hits
    return False


# Backstop behind the per-IP limit: a per-container daily ceiling on
# total analyses, so the shared API credits have a hard daily bound
# even if client identity is spoofed or many containers are warm.
_DAILY_CAP = 400
_day_hits: list[float] = []


def _global_limited() -> bool:
    now = time.monotonic()
    while _day_hits and now - _day_hits[0] >= 86400:
        _day_hits.pop(0)
    if len(_day_hits) >= _DAILY_CAP:
        return True
    _day_hits.append(now)
    return False


def _analyze_one_photo(photo_path: Path, trailer_type: str, load_type: str,
                       role: str, bed_length_ft: float | None, out_dir: Path) -> dict:
    """One photo through the pipeline. Returns its detection set,
    measurements (when a bed length supports them), overlay, cross-
    check records, and warnings. Raises only for a photo the
    pipeline itself rejects."""
    ds, run_record = run_pipeline(
        photo_path, trailer_type, load_type, role,
        out_dir, bed_length_ft=bed_length_ft, crosscheck=True,
    )
    warnings: list[str] = []
    outputs = run_record.get("outputs", {})
    measurements = None
    if bed_length_ft is not None:
        m_path = outputs.get("measurements")
        if m_path and Path(m_path).is_file():
            measurements = json.loads(Path(m_path).read_text(encoding="utf-8"))
        else:
            warnings.append(
                "measurements requested but the measurement stage produced no output"
            )
    overlay_b64 = None
    try:
        overlay_b64 = _overlay_b64(outputs.get("overlay"))
    except Exception as exc:
        warnings.append(f"overlay could not be rendered ({type(exc).__name__}: {exc})")
    for entry in run_record.get("prompts", []):
        if entry.get("status") == "error":
            warnings.append(
                f"camera check {entry['prompt']!r} failed after "
                f"{entry.get('attempts')} attempts: {entry.get('error')}"
            )
    return {
        "role": role,
        "detections": ds.to_dict(),
        "measurements": measurements,
        "overlay_png_base64": overlay_b64,
        "crosscheck": run_record.get("crosscheck") or [],
        "prompts": run_record.get("prompts", []),
        "wheel_filter": run_record.get("wheel_filter"),
        "notes": list(ds.notes),
        "warnings": warnings,
    }


@app.post("/analyze")
def analyze(
    request: Request,
    photo: UploadFile | None = File(None),
    photos: list[UploadFile] | None = File(None),
    photo_role: str = Form("side"),
    photo_roles: list[str] | None = Form(None),
    trailer_type: str = Form("unknown"),
    load_type: str = Form("generic"),
    bed_length_ft: float | None = Form(None),
    load_length_ft: float | None = Form(None),
    weight_lb: float | None = Form(None),
    report_lang: str = Form("en"),
):
    """Run the full pipeline on one photo, or on a SET of photos of
    the same load (send repeated `photos` + `photo_roles` fields).
    With a set, verdicts are computed over every photo's evidence:
    the side view carries the measurements, the rear view judges
    lights and plate, close-ups carry the tie-down count."""
    if report_lang not in ("en", "es"):
        report_lang = "en"
    client_ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        return JSONResponse(
            {"error": "too many analyses from this connection in the last hour; please wait a bit and try again"},
            status_code=429,
        )
    if _global_limited():
        return JSONResponse(
            {"error": "LoadMaster has reached its daily analysis capacity; please try again tomorrow"},
            status_code=429,
        )
    for name, v, cap in (
        ("bed length", bed_length_ft, 200.0),
        ("load length", load_length_ft, 200.0),
        ("weight", weight_lb, 500000.0),
    ):
        if v is not None and (not math.isfinite(v) or v <= 0 or v > cap):
            return JSONResponse(
                {"error": f"the {name} given is not a usable number; check it and try again"},
                status_code=400,
            )
    started = time.monotonic()

    uploads: list[tuple[UploadFile, str]] = []
    if photos:
        roles = list(photo_roles or [])
        for i, up in enumerate(photos[:4]):
            uploads.append((up, roles[i] if i < len(roles) else "side"))
    elif photo is not None:
        uploads.append((photo, photo_role))
    if not uploads:
        return JSONResponse({"error": "no photo uploaded"}, status_code=400)

    inputs = _user_inputs(trailer_type, load_type, bed_length_ft, load_length_ft, weight_lb)
    warnings: list[str] = []

    with tempfile.TemporaryDirectory(prefix="loadmaster-") as tmp:
        tmp_dir = Path(tmp)
        jobs = []
        for i, (up, role) in enumerate(uploads):
            # Read with a cap: an oversize upload is refused from its
            # first bytes, never loaded whole into memory first.
            raw = up.file.read(MAX_UPLOAD_BYTES + 1)
            if not raw:
                return JSONResponse({"error": "empty photo upload"}, status_code=400)
            try:
                raw = _normalize_upload(raw)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            p = tmp_dir / f"photo{i}.jpg"
            p.write_bytes(raw)
            jobs.append((p, role))

        # The primary photo carries measurements and the overlay the
        # corrections UI works on: the first side view, else the first.
        primary_idx = next((i for i, (_, r) in enumerate(jobs) if r == "side"), 0)

        def work(i_role):
            i, (p, role) = i_role
            return _analyze_one_photo(
                p, trailer_type, load_type, role,
                bed_length_ft if i == primary_idx else None,
                tmp_dir / f"out{i}",
            )

        try:
            if len(jobs) == 1:
                results = [work((0, jobs[0]))]
            else:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
                    results = list(pool.map(work, enumerate(jobs)))
        except Exception as exc:
            # Our own ValueErrors are written for users; anything
            # else is reported by type only, so internal details
            # (paths, library messages) never reach the public error.
            if isinstance(exc, ValueError):
                detail = str(exc)[:300]
            else:
                detail = type(exc).__name__
            return JSONResponse(
                {"error": f"could not process that photo ({detail})"},
                status_code=400,
            )

        for r in results:
            warnings.extend(r["warnings"])
        primary = results[primary_idx]
        detections = primary["detections"]
        measurements = primary["measurements"]
        cross_records: list[dict] = []
        for r in results:
            cross_records.extend(r["crosscheck"])
        extra_sets = [
            r["detections"] for i, r in enumerate(results) if i != primary_idx
        ]

        verdicts = None
        rules_report = None
        report = None
        if load_length_ft is not None:
            verdicts, v_warnings, rules_report = compute_verdicts(
                detections, measurements, inputs,
                crosscheck=cross_records, extra_sets=extra_sets,
            )
            warnings.extend(v_warnings)
            if verdicts:
                try:
                    report = write_report(verdicts, inputs, lang=report_lang)
                except Exception as exc:  # write_report never raises; belt and braces
                    warnings.append(
                        f"written summary unavailable ({type(exc).__name__}: {exc})"
                    )

        return {
            "detections": detections,
            "measurements": measurements,
            "verdicts": verdicts,
            "report": report,
            "crosscheck": cross_records,
            "rules": rules_report,
            "overlay_png_base64": primary["overlay_png_base64"],
            "overlay_mime": "image/jpeg",
            "photo_sets": [
                {
                    "role": r["role"],
                    "detections": r["detections"],
                    "overlay_png_base64": r["overlay_png_base64"],
                    "notes": r["notes"],
                }
                for r in results
            ],
            "inputs": inputs,
            "warnings": warnings,
            "run": {
                "total_latency_s": round(time.monotonic() - started, 2),
                "prompts": primary["prompts"],
                "wheel_filter": primary["wheel_filter"],
                "notes": primary["notes"],
                "photos": len(results),
            },
        }


@app.post("/verdicts")
async def verdicts_only(request: Request):
    """Recompute ONLY the rules verdicts from a corrected detection set.

    Body: {"detections": <DetectionSet dict>, "measurements": <dict|null>,
           "inputs": {"load_length_ft": ..., "load_type": ...,
                      "weight_lb": ..., "trailer_type": ...,
                      "bed_length_ft": ...}}
    No SAM calls happen here: this is the tap-to-correct recheck path.
    """
    started = time.monotonic()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > 2_000_000:
        return JSONResponse(
            {"error": "request body too large (2 MB max)"}, status_code=413
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "request body must be JSON"}, status_code=400)
    ds_dict = body.get("detections")
    if not isinstance(ds_dict, dict) or "detections" not in ds_dict:
        return JSONResponse(
            {"error": "body.detections must be a detection set object"},
            status_code=400,
        )
    if len(ds_dict.get("detections") or []) > 500:
        return JSONResponse(
            {"error": "too many detections in one set (500 max)"},
            status_code=400,
        )
    if isinstance(body.get("sets"), list) and len(body["sets"]) > 5:
        return JSONResponse(
            {"error": "too many photo sets in one recheck (5 max)"},
            status_code=400,
        )
    # Multi-photo runs hand back every set plus the cross-check
    # records from /analyze, so a recheck sees the same evidence the
    # first verdict did (the cross-check can only demote, and it must
    # not silently un-demote after a correction).
    extra_sets = None
    sets_in = body.get("sets")
    if isinstance(sets_in, list) and len(sets_in) > 1:
        extra_sets = [s for s in sets_in[1:] if isinstance(s, dict) and "detections" in s]
    cross_records = body.get("crosscheck")
    if not isinstance(cross_records, list):
        cross_records = None
    inputs = body.get("inputs") or body.get("user_inputs") or {}
    measurements = body.get("measurements")
    warnings: list[str] = []

    if measurements is None and inputs.get("bed_length_ft"):
        # Pure geometry (no SAM): deriving measurements from the
        # corrected set keeps the endpoint self-contained when the
        # client does not hand them back.
        try:
            from .measure import measure_load

            report = measure_load(
                DetectionSet.from_dict(ds_dict), float(inputs["bed_length_ft"])
            )
            measurements = report.to_dict()
        except Exception as exc:
            warnings.append(
                f"measurements could not be derived ({type(exc).__name__}: {exc})"
            )

    verdicts, v_warnings, rules_report = compute_verdicts(
        ds_dict, measurements, inputs, crosscheck=cross_records, extra_sets=extra_sets
    )
    warnings.extend(v_warnings)
    return {
        "verdicts": verdicts,
        "rules": rules_report,
        "measurements": measurements,
        "warnings": warnings,
        "latency_s": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
