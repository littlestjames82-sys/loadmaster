"""Web layer cross-check wiring: the records produced by
crosscheck.ask_spark inside run_pipeline must reach the rules
engine's evaluate_rules(crosscheck=...) through compute_verdicts,
and _call_rules must forward them to engines that accept them.
"""

import sys
import types

from loadmaster import webapp


RECORDS = [
    {
        "photo": "p.jpg",
        "photo_role": "closeup",
        "status": "ok",
        "model": "muse-spark-1.3",
        "view": {"tiedown_count": 2, "straps_over_sharp_edge_without_protection": "unclear"},
        "raw_answer": "{}",
        "latency_s": 1.0,
        "attempts": 1,
        "error": None,
    }
]


def test_call_rules_forwards_crosscheck_kwarg():
    seen = {}

    def fake_engine(sets, measurements, inputs, crosscheck=None):
        seen["crosscheck"] = crosscheck
        return {"verdicts": []}

    webapp._call_rules(fake_engine, {"detections": []}, None, None, {}, crosscheck=RECORDS)
    assert seen["crosscheck"] == RECORDS


def test_call_rules_without_crosscheck_passes_none():
    seen = {}

    def fake_engine(sets, measurements, inputs, crosscheck=None):
        seen["crosscheck"] = crosscheck
        return {"verdicts": []}

    webapp._call_rules(fake_engine, {"detections": []}, None, None, {})
    assert seen["crosscheck"] is None


def test_compute_verdicts_threads_crosscheck_to_engine(monkeypatch):
    seen = {}

    stub = types.ModuleType("loadmaster.rules")

    def evaluate_rules(sets, measurements, inputs, crosscheck=None):
        seen["crosscheck"] = crosscheck
        return {
            "verdicts": [
                {
                    "check_id": "tiedown_count",
                    "status": "CHECK_THIS",
                    "cfr_citation": "49 CFR 393.110",
                    "finding": "Two views disagree.",
                    "minimum_fix": "Count again in a close-up.",
                }
            ]
        }

    stub.evaluate_rules = evaluate_rules
    monkeypatch.setitem(sys.modules, "loadmaster.rules", stub)
    # `from . import rules` inside compute_verdicts resolves through
    # the package attribute first (pipeline.py already imported the
    # real engine), so the stub must replace that attribute too.
    import loadmaster as _pkg

    monkeypatch.setattr(_pkg, "rules", stub)

    verdicts, warnings, _raw = webapp.compute_verdicts(
        {"detections": [], "image_width": 10, "image_height": 10},
        None,
        {"load_length_ft": 8, "load_type": "general", "trailer_type": "utility"},
        crosscheck=RECORDS,
    )
    assert seen["crosscheck"] == RECORDS
    assert verdicts and verdicts[0]["status"] == "CHECK THIS"
    assert warnings == []
