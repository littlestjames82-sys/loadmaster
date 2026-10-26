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
import inspect
import io
import json
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image

from .detections import DetectionSet
from .pipeline import run_pipeline

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


def _call_rules(fn, ds_dict, ds_obj, measurements, inputs):
    """Call the rules entry point with whatever shape it accepts."""
    pool = {
        "detections": ds_dict,
        "detection_set": ds_obj,
        "measurements": measurements,
        "inputs": inputs,
        "user_inputs": inputs,
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


def compute_verdicts(ds_dict: dict, measurements: dict | None, inputs: dict):
    """Return (verdicts | None, warnings). Never raises."""
    warnings: list[str] = []
    try:
        from . import rules as rules_module
    except Exception as exc:  # module not landed yet, or import error
        return None, [
            f"verdicts not computed: the rules engine is not available yet "
            f"({type(exc).__name__}: {exc})"
        ]
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
        ]
    ds_obj = None
    try:
        ds_obj = DetectionSet.from_dict(ds_dict)
    except Exception:
        ds_obj = None
    try:
        raw = _call_rules(fn, ds_dict, ds_obj, measurements, inputs)
    except Exception as exc:
        return None, [
            f"verdicts not computed: the rules engine call failed "
            f"({type(exc).__name__}: {exc})"
        ]
    verdicts = _normalize_verdicts(raw)
    if verdicts is None:
        warnings.append("verdicts not computed: the rules engine returned nothing usable")
    return verdicts, warnings


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


@app.post("/analyze")
def analyze(
    photo: UploadFile = File(...),
    trailer_type: str = Form("unknown"),
    load_type: str = Form("generic"),
    photo_role: str = Form("side"),
    bed_length_ft: float | None = Form(None),
    load_length_ft: float | None = Form(None),
    weight_lb: float | None = Form(None),
):
    """Run the full pipeline on one uploaded photo."""
    started = time.monotonic()
    raw = photo.file.read()
    if not raw:
        return JSONResponse({"error": "empty photo upload"}, status_code=400)
    suffix = Path(photo.filename or "photo.jpg").suffix or ".jpg"
    inputs = _user_inputs(trailer_type, load_type, bed_length_ft, load_length_ft, weight_lb)
    warnings: list[str] = []

    with tempfile.TemporaryDirectory(prefix="loadmaster-") as tmp:
        tmp_dir = Path(tmp)
        photo_path = tmp_dir / f"photo{suffix}"
        photo_path.write_bytes(raw)
        try:
            ds, run_record = run_pipeline(
                photo_path, trailer_type, load_type, photo_role,
                tmp_dir / "out", bed_length_ft=bed_length_ft,
            )
        except Exception as exc:
            return JSONResponse(
                {"error": f"could not process that photo ({type(exc).__name__}: {exc})"},
                status_code=400,
            )
        detections = ds.to_dict()
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

        verdicts = None
        if load_length_ft is not None:
            verdicts, v_warnings = compute_verdicts(detections, measurements, inputs)
            warnings.extend(v_warnings)

        return {
            "detections": detections,
            "measurements": measurements,
            "verdicts": verdicts,
            "overlay_png_base64": overlay_b64,
            "overlay_mime": "image/jpeg",
            "inputs": inputs,
            "warnings": warnings,
            "run": {
                "total_latency_s": round(time.monotonic() - started, 2),
                "prompts": run_record.get("prompts", []),
                "wheel_filter": run_record.get("wheel_filter"),
                "notes": list(ds.notes),
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

    verdicts, v_warnings = compute_verdicts(ds_dict, measurements, inputs)
    warnings.extend(v_warnings)
    return {
        "verdicts": verdicts,
        "measurements": measurements,
        "warnings": warnings,
        "latency_s": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
