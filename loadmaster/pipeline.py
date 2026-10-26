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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

from .detections import DetectionSet, filter_wheels_to_axle
from .mask_decode import MaskRaster
from .measure import write_measurements
from .overlay import render_overlay
from .prompt_packs import get_pack
from .sam_client import run_prompts


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
) -> tuple[DetectionSet, dict]:
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
    overlay_path = render_overlay(ds, out_dir / f"{stem}_overlay.png")
    outputs = {
        "detections": str(det_path),
        "overlay": str(overlay_path),
    }
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
    args = ap.parse_args(argv)

    if not args.photo.is_file():
        print(f"photo not found: {args.photo}", file=sys.stderr)
        return 3
    ds, record = run_pipeline(args.photo, args.trailer_type, args.load_type, args.role, args.out_dir,
                              bed_length_ft=args.bed_length_ft)
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
    print(f"outputs: {record['outputs']}")
    if all(p["status"] == "error" for p in record["prompts"]):
        print("ALL prompts errored; see run record. Nothing was faked.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
