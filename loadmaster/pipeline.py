"""LoadMaster Day 1 pipeline: photo -> prompt pack -> SAM (parallel)
-> decoded masks -> editable DetectionSet -> detections JSON +
annotated overlay PNG + run record.

No rules, no verdicts, no UI: this layer only produces evidence.

Usage:
  python -m loadmaster.pipeline --photo PHOTO.jpg \
      --trailer-type utility --load-type firewood --role side \
      --out-dir examples/output [--bed-length-ft 8]

With --bed-length-ft, the Day 2 measurement layer also runs and a
measurements JSON lands alongside the detections (see measure.py).

With --rules, the Day 3 deterministic rules engine (rules.py) runs
over this photo's detections + measurements + the user inputs given
on the command line (--load-length-ft, --weight-lb, --load-category,
--tiedown-wll-lb, --tiedown-attachment) and a verdicts JSON lands
alongside. With --crosscheck, Muse Spark first reads the photo
independently (crosscheck.py) and its view feeds the verdicts; the
raw Spark answers are kept in the run record for audit.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from PIL import Image

from .crosscheck import ask_spark
from .detections import DetectionSet, filter_wheels_to_axle
from .mask_decode import MaskRaster
from .measure import write_measurements
from .overlay import render_overlay
from .prompt_packs import HEAVY_LOAD_TYPES, LOOSE_LOAD_TYPES, get_pack
from .rules import UserInputs, evaluate_rules
from .sam_client import run_prompts


def _rules_load_category(load_type: str, override: str | None) -> str:
    """Map the pipeline's free-form load type onto the rules
    engine's three categories (UserInputs.load_type)."""
    if override:
        return override
    lt = (load_type or "").strip().lower()
    if lt == "loose_material" or lt in LOOSE_LOAD_TYPES:
        return "loose_material"
    if lt in HEAVY_LOAD_TYPES:
        return "heavy_wheeled_tracked"
    return "general"


def _scale_mask(raster: MaskRaster, sx: float, sy: float) -> dict:
    """Rescale a sent-space mask raster into original-image space and
    return its RLE dict for the detection record."""
    if abs(sx - 1.0) < 1e-6 and abs(sy - 1.0) < 1e-6:
        return raster.to_rle()
    img = Image.frombytes("L", (raster.width, raster.height),
                          bytes(255 if b else 0 for b in raster.data))
    new_w = max(1, round(raster.width * sx))
    new_h = max(1, round(raster.height * sy))
    img = img.resize((new_w, new_h), Image.NEAREST)
    data = bytes(1 if b else 0 for b in img.tobytes())
    scaled = MaskRaster(
        width=new_w, height=new_h,
        offset_x=round(raster.offset_x * sx),
        offset_y=round(raster.offset_y * sy),
        data=data,
    )
    return scaled.to_rle()


def run_pipeline(
    photo: Path,
    trailer_type: str,
    load_type: str,
    photo_role: str,
    out_dir: Path,
    bed_length_ft: float | None = None,
    rules_inputs: UserInputs | None = None,
    crosscheck: bool = False,
) -> tuple[DetectionSet, dict]:
    # The cross-check reads the photo independently, so it runs
    # CONCURRENTLY with the SAM prompts instead of after them: its
    # latency hides inside the segmentation time. ask_spark never
    # raises (failures arrive as status="error" results).
    cc_box: dict = {}

    def _run_cc() -> None:
        cc_box["result"] = ask_spark(photo, photo_role)

    cc_thread: threading.Thread | None = None
    if crosscheck:
        cc_thread = threading.Thread(target=_run_cc, daemon=True)
        cc_thread.start()

    with Image.open(photo) as im:
        orig_w, orig_h = im.size

    pack = get_pack(trailer_type, load_type, photo_role)
    results = run_prompts(photo, [spec.prompt for spec in pack])

    ds = DetectionSet(
        image_path=str(photo),
        image_width=orig_w,
        image_height=orig_h,
        trailer_type=trailer_type,
        load_type=load_type,
        photo_role=photo_role,
    )
    run_record: dict = {
        "photo": str(photo),
        "trailer_type": trailer_type,
        "load_type": load_type,
        "photo_role": photo_role,
        "bed_length_ft": bed_length_ft,
        "prompts": [],
    }
    wheel_boxes: list[tuple[list[float], float | None]] = []

    for spec, res in zip(pack, results):
        entry = {
            "prompt": spec.prompt,
            "kind": spec.kind,
            "bonus_only": spec.bonus_only,
            "status": res.status,
            "attempts": res.attempts,
            "latency_s": round(res.latency_s, 2),
            "objects": len(res.objects),
            "error": res.error,
        }
        run_record["prompts"].append(entry)
        if res.status == "error":
            ds.notes.append(
                f"prompt {spec.prompt!r} ERRORED after {res.attempts} attempts "
                f"({res.error}); this is an error state, not a zero match"
            )
            continue
        if not res.objects:
            continue
        sx = orig_w / res.sent_width if res.sent_width else 1.0
        sy = orig_h / res.sent_height if res.sent_height else 1.0
        for obj in res.objects:
            bbox = [
                obj.bbox[0] * sx, obj.bbox[1] * sy,
                obj.bbox[2] * sx, obj.bbox[3] * sy,
            ]
            if spec.kind == "wheel":
                # Wheel boxes feed the axle estimate only (matrix:
                # over-firing; never a raw count).
                wheel_boxes.append((bbox, obj.confidence))
                continue
            polygon = []
            mask_rle = None
            if obj.mask is not None:
                polygon = [
                    [x * sx, y * sy] for x, y in obj.mask.polygon()
                ]
                mask_rle = _scale_mask(obj.mask, sx, sy)
            ds.add(
                spec.kind,
                bbox,
                confidence=obj.confidence,
                source="sam",
                prompt=spec.prompt,
                polygon=polygon,
                mask_rle=mask_rle,
                bonus_only=spec.bonus_only,
            )

    axles, stats = filter_wheels_to_axle(wheel_boxes, orig_w, orig_h)
    for det in axles:
        ds.detections.append(det)
    run_record["wheel_filter"] = stats
    if wheel_boxes:
        ds.notes.append(
            f"wheel boxes filtered to {len(axles)} axle estimate(s); "
            "raw wheel boxes are never reported as a count"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = photo.stem
    det_path = out_dir / f"{stem}_detections.json"
    det_path.write_text(json.dumps(ds.to_dict(), indent=1), encoding="utf-8")
    outputs = {"detections": str(det_path)}
    try:
        # The overlay is presentation: a render failure must never
        # cost the user the detections and verdicts behind it.
        overlay_path = render_overlay(ds, out_dir / f"{stem}_overlay.png")
        outputs["overlay"] = str(overlay_path)
    except Exception as exc:
        ds.notes.append(
            f"overlay could not be rendered ({type(exc).__name__}); "
            "detections and verdicts are unaffected"
        )
    report = None
    if bed_length_ft is not None:
        # Day 2 measurement stage (optional): calibrated inches from
        # the user-supplied bed length. Degrades honestly inside
        # measure.py when the inputs cannot support a number.
        m_path = out_dir / f"{stem}_measurements.json"
        report = write_measurements(ds, bed_length_ft, m_path)
        outputs["measurements"] = str(m_path)
        run_record["measurements_summary"] = {
            m.name: m.status for m in report.measurements
        }
    # Tier 2.5 cross-check: collect the concurrent Spark read (see
    # the top of this function). The raw record stays in the run
    # record for audit, whether or not the pipeline itself computes
    # verdicts (the web app computes them from the run record).
    cross_records: list[dict] = []
    if crosscheck and cc_thread is not None:
        cc_thread.join(timeout=180)
        cc = cc_box.get("result")
        if cc is None:
            ds.notes.append(
                f"cross-check did not finish within 180s for role "
                f"{photo_role}; verdicts were computed without "
                "Spark's view"
            )
        else:
            cross_records.append(cc.to_dict())
            run_record["crosscheck"] = cross_records
            if cc.status == "error":
                ds.notes.append(
                    f"cross-check ERRORED for role {photo_role} ({cc.error}); "
                    "verdicts were computed without Spark's view"
                )
    if rules_inputs is not None:
        # Day 3 stage (optional): deterministic verdicts.
        verdicts = evaluate_rules(
            [ds],
            report.to_dict() if report is not None else None,
            rules_inputs,
            crosscheck=cross_records,
        )
        v_path = out_dir / f"{stem}_verdicts.json"
        v_path.write_text(json.dumps(verdicts, indent=1), encoding="utf-8")
        outputs["verdicts"] = str(v_path)
        run_record["verdicts_summary"] = verdicts["summary"]
        run_record["rules_inputs"] = rules_inputs.to_dict()
    run_path = out_dir / f"{stem}_run.json"
    run_record["outputs"] = {**outputs, "run": str(run_path)}
    run_path.write_text(json.dumps(run_record, indent=1), encoding="utf-8")
    return ds, run_record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LoadMaster Day 1 SAM pipeline")
    ap.add_argument("--photo", required=True, type=Path)
    ap.add_argument("--trailer-type", default="unknown")
    ap.add_argument("--load-type", default="generic")
    ap.add_argument("--role", default="side", choices=["side", "rear", "closeup"])
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--bed-length-ft", type=float, default=None,
                    help="trailer bed length in feet; enables the measurement stage")
    ap.add_argument("--rules", action="store_true",
                    help="run the Day 3 rules engine and write a verdicts JSON")
    ap.add_argument("--crosscheck", action="store_true",
                    help="Muse Spark reads the photo independently first "
                         "(implies --rules); its view feeds the verdicts")
    ap.add_argument("--load-length-ft", type=float, default=None,
                    help="length of the LOAD in feet (rules input, 393.110)")
    ap.add_argument("--load-category", default=None,
                    choices=["general", "heavy_wheeled_tracked", "loose_material"],
                    help="rules load category; default derives from --load-type")
    ap.add_argument("--weight-lb", type=float, default=None,
                    help="known load weight in lb (rules input, optional)")
    ap.add_argument("--tiedown-wll-lb", type=float, default=None,
                    help="uniform per-tie-down working load limit in lb (optional)")
    ap.add_argument("--tiedown-attachment", default="indirect",
                    choices=["direct", "indirect"],
                    help="how the tie-downs attach (direct counts half rating)")
    args = ap.parse_args(argv)

    if not args.photo.is_file():
        print(f"photo not found: {args.photo}", file=sys.stderr)
        return 3
    rules_inputs = None
    if args.rules or args.crosscheck:
        rules_inputs = UserInputs(
            load_length_ft=args.load_length_ft,
            load_type=_rules_load_category(args.load_type, args.load_category),
            weight_lb=args.weight_lb,
            tiedown_wll_lb=args.tiedown_wll_lb,
            tiedown_attachment=args.tiedown_attachment,
        )
    ds, record = run_pipeline(args.photo, args.trailer_type, args.load_type, args.role, args.out_dir,
                              bed_length_ft=args.bed_length_ft,
                              rules_inputs=rules_inputs,
                              crosscheck=args.crosscheck)
    for p in record["prompts"]:
        line = f"  {p['prompt']!r}: {p['status']}, {p['objects']} object(s), {p['latency_s']}s, attempts {p['attempts']}"
        if p["error"]:
            line += f" ERROR {p['error']}"
        print(line)
    print(f"detections: {len(ds.detections)} "
          f"({', '.join(f'{k}={len(ds.by_kind(k))}' for k in sorted({d.kind for d in ds.detections}))})")
    if "measurements_summary" in record:
        print(f"measurements (bed length {record['bed_length_ft']} ft): "
              + ", ".join(f"{k}={v}" for k, v in record["measurements_summary"].items()))
    if "verdicts_summary" in record:
        s = record["verdicts_summary"]
        print(f"verdicts: {s['PASS']} PASS, {s['CHECK_THIS']} CHECK_THIS, "
              f"{s['COULD_NOT_TELL']} COULD_NOT_TELL"
              + ("; cross-check ran" if "crosscheck" in record else ""))
    print(f"outputs: {record['outputs']}")
    if all(p["status"] == "error" for p in record["prompts"]):
        print("ALL prompts errored; see run record. Nothing was faked.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
