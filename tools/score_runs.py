#!/usr/bin/env python3
"""Score LoadMaster runs against expected verdicts.

Built for the staged test set: Ryan photographs his own loads where
the ground truth is known (strap counts, gaps, placement), records
the expected status per check in a manifest, and this harness runs
every photo through the same per-photo path the web app uses, then
diffs the verdicts.

Manifest (JSON list):
  [{"id": "harley-correct-side",
    "photo": "/path/to/photo.jpg",
    "trailer_type": "utility", "load_type": "motorcycle",
    "photo_role": "side",
    "bed_length_ft": 8, "load_length_ft": 6, "weight_lb": 900,
    "expected": {"tiedown_count": "PASS", "forward_blocking": "PASS"}}]

Expected statuses use the app's display forms: PASS, CHECK THIS,
COULD NOT TELL. Checks absent from "expected" are reported but not
scored. A photo set (several photos, one verdict set) is one entry
with "photos": [{"photo": ..., "photo_role": ...}, ...] instead of
"photo"/"photo_role".

Usage: python tools/score_runs.py MANIFEST.json [--out REPORT.json]
Exit code 0 when every scored check matches, 1 otherwise.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster.webapp import _analyze_one_photo, compute_verdicts  # noqa: E402


def _run_entry(entry: dict) -> dict:
    photos = entry.get("photos") or [
        {"photo": entry["photo"], "photo_role": entry.get("photo_role", "side")}
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix="score-") as tmp:
        tmp_dir = Path(tmp)
        for i, ph in enumerate(photos):
            results.append(_analyze_one_photo(
                Path(ph["photo"]), entry.get("trailer_type", "unknown"),
                entry.get("load_type", "generic"),
                ph.get("photo_role", "side"),
                entry.get("bed_length_ft") if i == 0 else None,
                tmp_dir / f"out{i}",
            ))
    primary = results[0]
    inputs = {
        "trailer_type": entry.get("trailer_type", "unknown"),
        "load_type": entry.get("load_type", "generic"),
        "bed_length_ft": entry.get("bed_length_ft"),
        "load_length_ft": entry.get("load_length_ft"),
        "weight_lb": entry.get("weight_lb"),
    }
    cross = []
    for r in results:
        cross.extend(r["crosscheck"])
    verdicts, warnings, _ = compute_verdicts(
        primary["detections"], primary["measurements"], inputs,
        crosscheck=cross,
        extra_sets=[r["detections"] for r in results[1:]],
    )
    got = {v["check"]: v["status"] for v in verdicts or []}
    scored = []
    for check, want in (entry.get("expected") or {}).items():
        scored.append({
            "check": check, "expected": want,
            "got": got.get(check), "match": got.get(check) == want,
        })
    return {"id": entry.get("id"), "got": got, "scored": scored,
            "warnings": warnings}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    manifest = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    out_path = None
    if "--out" in argv:
        out_path = Path(argv[argv.index("--out") + 1])
    report = []
    total = matched = 0
    for entry in manifest:
        res = _run_entry(entry)
        report.append(res)
        line = [f"== {res['id']}"]
        for s in res["scored"]:
            total += 1
            matched += 1 if s["match"] else 0
            mark = "OK " if s["match"] else "XX "
            line.append(f"  {mark}{s['check']}: expected {s['expected']}, "
                        f"got {s['got']}")
        if not res["scored"]:
            line.append("  (no expectations recorded; verdicts captured)")
        print("\n".join(line))
    print(f"\nscored checks: {matched}/{total} matched")
    if out_path:
        out_path.write_text(json.dumps(
            {"summary": {"matched": matched, "total": total},
             "runs": report}, indent=1), encoding="utf-8")
        print(f"report written to {out_path}")
    return 0 if matched == total else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
