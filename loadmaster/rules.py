"""Deterministic 49 CFR 393 rules engine (Day 3).

This module is the judge. It takes the Day 1 detections, the Day 2
measurements, and the user's declared inputs (load length, load
type, optional weight, optional tie-down ratings) and returns one
verdict per check. It is DETERMINISTIC: no LLM calls, no randomness,
no network. The same inputs always produce the same verdicts. Muse
Spark's prose layer (Day 4) may phrase these verdicts; it may never
change them. The dual-model cross-check (crosscheck.py) can attach
its independent view to a verdict and, on disagreement over something
Spark can actually see, demote a PASS to CHECK_THIS - it can never
upgrade anything to PASS.

Honesty rules baked in (each is a test case):
- "COULD_NOT_TELL" is a required outcome, not a failure. Where a
  photo cannot decide (ratings, weight, tension, protectors), the
  verdict says so and names what input would decide it.
- Tie-down counts rest ONLY on anchor close-up detections plus
  user-added (tap-to-correct) tie-down marks. Full-frame strap
  detections are flagged bonus_only (matrix: straps are invisible at
  full-frame distance) and never count.
- Weight placement is a visual estimate only and can NEVER pass.

Risk ranking (risk_rank, 1 = fix first), per the master record's
fix-list ordering (forward slide, then missing tie-down, then
coverage issues):
  1 forward_blocking      a load that slides forward under braking
  2 tiedown_count         too few tie-downs for the length/weight
  3 heavy_equipment       393.130's four-tie-down rule for 10,000 lb+
  4 overhang_lights_plate rear overhang, obscured lights/plate
  5 weight_placement      bulk behind the axle (estimate only)
  6 tarp_coverage         loose load shedding debris
  7 edge_protection       straps/chains cut on sharp edges
  8 aggregate_wll         strength arithmetic (needs supplied numbers)

Not-applicable handling (documented choice): a check that does not
apply to this load at all (heavy_equipment on a general load,
tarp_coverage on a non-loose load) produces NO verdict object.
Instead it is listed, with the reason, under "not_applicable" in the
report. COULD_NOT_TELL is reserved for checks that DO apply but
cannot be decided from the evidence supplied - calling a
not-applicable check COULD_NOT_TELL would read as a gap in the
evidence when there is none.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .detections import Detection, DetectionSet
from .measure import MeasurementReport, _det_stats

PASS = "PASS"
CHECK_THIS = "CHECK_THIS"
COULD_NOT_TELL = "COULD_NOT_TELL"

LOAD_TYPES = ("general", "heavy_wheeled_tracked", "loose_material")

RISK_RANK = {
    "forward_blocking": 1,
    "tiedown_count": 2,
    "heavy_equipment": 3,
    "overhang_lights_plate": 4,
    "weight_placement": 5,
    "tarp_coverage": 6,
    "edge_protection": 7,
    "aggregate_wll": 8,
}

CITATIONS = {
    "tiedown_count": "49 CFR 393.110",
    "aggregate_wll": "49 CFR 393.106 / 393.108",
    "heavy_equipment": "49 CFR 393.130",
    "forward_blocking": "49 CFR 393.104 / 393.110 (forward movement blocking)",
    "edge_protection": "49 CFR 393.104 (edge protection)",
    # The product spec (master record, check 6) names no single
    # section for overhang + obscured lights/plate; none is invented.
    "overhang_lights_plate": "49 CFR Part 393, Subpart I (overhang; lights and plate visibility)",
    "tarp_coverage": "49 CFR 393.100 (cargo fully covered / contained)",
    "weight_placement": "49 CFR 393.100 (load placement; visual estimate only here)",
}

# Check 4 thresholds, documented here so the numbers are auditable,
# not buried in comparisons: at or under the PASS gap the load is
# effectively against the front; anything larger is CHECK THIS, with
# the MAX band only changing how the finding is worded.
FORWARD_BLOCKING_MAX_GAP_IN = 6.0
# PASS requires the load to be EFFECTIVELY against the front of the
# bed. A few inches of open gap with nothing shown in it is not
# blocking: the load can surge that far and arrive with momentum.
# (Tightened Oct 26 after an external review made the point that the
# old 6 in PASS bar claimed more than geometry can prove.)
FORWARD_BLOCKING_PASS_GAP_IN = 2.0


# ---------------------------------------------------------------------
# Inputs and verdict objects.
# ---------------------------------------------------------------------

@dataclass
class UserInputs:
    load_length_ft: float | None = None
    load_type: str = "general"  # one of LOAD_TYPES
    weight_lb: float | None = None
    # Tie-down working load limits, when the user supplies them:
    # either one uniform number (applied to every counted tie-down),
    # or a list with one entry per tie-down, each entry either a
    # number or {"wll_lb": n, "attachment": "direct"|"indirect"}.
    tiedown_wll_lb: float | list | None = None
    # Attachment for entries that do not name their own. A DIRECT
    # tie-down (anchored on one side, attached to the load) counts at
    # half its rating toward the aggregate; an INDIRECT one (passing
    # over the top, anchored on both sides) counts in full
    # (393.106/393.108).
    tiedown_attachment: str = "indirect"

    def __post_init__(self) -> None:
        if self.load_type not in LOAD_TYPES:
            raise ValueError(
                f"load_type must be one of {LOAD_TYPES}, got {self.load_type!r}"
            )
        # Declared numbers arrive from forms and JSON bodies. Any
        # that is not a positive finite quantity is dropped to None,
        # so the checks that needed it degrade to COULD NOT TELL
        # instead of computing verdicts from nonsense (a negative
        # weight would let the WLL arithmetic pass for free).
        for name in ("load_length_ft", "weight_lb"):
            v = getattr(self, name)
            if v is not None and (
                not isinstance(v, (int, float)) or isinstance(v, bool)
                or not math.isfinite(v) or v <= 0
            ):
                setattr(self, name, None)
        wll = self.tiedown_wll_lb
        if isinstance(wll, (int, float)) and not isinstance(wll, bool):
            if not math.isfinite(wll) or wll <= 0:
                self.tiedown_wll_lb = None
        elif isinstance(wll, list):
            kept = []
            for e in wll:
                n = e.get("wll_lb") if isinstance(e, dict) else e
                if (
                    isinstance(n, (int, float)) and not isinstance(n, bool)
                    and math.isfinite(n) and n > 0
                ):
                    kept.append(e)
            self.tiedown_wll_lb = kept or None
        elif wll is not None:
            self.tiedown_wll_lb = None

    @classmethod
    def from_dict(cls, d: dict) -> "UserInputs":
        return cls(
            load_length_ft=d.get("load_length_ft"),
            load_type=d.get("load_type", "general"),
            weight_lb=d.get("weight_lb"),
            tiedown_wll_lb=d.get("tiedown_wll_lb"),
            tiedown_attachment=d.get("tiedown_attachment", "indirect"),
        )

    def to_dict(self) -> dict:
        return {
            "load_length_ft": self.load_length_ft,
            "load_type": self.load_type,
            "weight_lb": self.weight_lb,
            "tiedown_wll_lb": self.tiedown_wll_lb,
            "tiedown_attachment": self.tiedown_attachment,
        }


@dataclass
class Verdict:
    check_id: str
    cfr_citation: str
    status: str
    finding: str
    evidence: dict  # {"detection_ids": [...], "measurement_names": [...]}
    confidence_basis: str
    risk_rank: int
    minimum_fix: str | None = None
    cross_check: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "check_id": self.check_id,
            "cfr_citation": self.cfr_citation,
            "status": self.status,
            "finding": self.finding,
            "evidence": self.evidence,
            "minimum_fix": self.minimum_fix,
            "confidence_basis": self.confidence_basis,
            "risk_rank": self.risk_rank,
        }
        if self.cross_check is not None:
            d["cross_check"] = self.cross_check
        return d


def _verdict(check_id: str, status: str, finding: str, *,
             detection_ids: list[str] | None = None,
             measurement_names: list[str] | None = None,
             minimum_fix: str | None = None,
             confidence_basis: str = "") -> Verdict:
    return Verdict(
        check_id=check_id,
        cfr_citation=CITATIONS[check_id],
        status=status,
        finding=finding,
        evidence={
            "detection_ids": list(detection_ids or []),
            "measurement_names": list(measurement_names or []),
        },
        confidence_basis=confidence_basis,
        risk_rank=RISK_RANK[check_id],
        minimum_fix=minimum_fix,
    )


# ---------------------------------------------------------------------
# Input normalisation + the tie-down count basis (shared by checks
# 1, 2 and 3).
# ---------------------------------------------------------------------

def _as_sets(sets) -> list[DetectionSet]:
    out = []
    for s in sets or []:
        out.append(s if isinstance(s, DetectionSet) else DetectionSet.from_dict(s))
    return out


def _as_measurements_dict(measurements) -> dict | None:
    if measurements is None:
        return None
    if isinstance(measurements, MeasurementReport):
        return measurements.to_dict()
    return measurements


def _measurement(mdict: dict | None, name: str) -> dict | None:
    if not mdict:
        return None
    for m in mdict.get("measurements", []):
        if m.get("name") == name:
            return m
    return None


def _is_user_tiedown_mark(d: Detection) -> bool:
    """A tap-to-correct tie-down mark: the user added it and its
    label says it is a tie-down (the generic 'user_added' kind covers
    anything the user taps in; only tie-down labels count here)."""
    if d.kind == "user_added" or d.source == "user":
        label = (d.label or "").lower().replace("-", " ").replace("_", " ")
        return "tie down" in label or "tiedown" in label
    return False


@dataclass
class TiedownBasis:
    count: int
    detection_ids: list[str]
    basis_present: bool  # a close-up photo set or user marks were supplied
    detail: str


def tiedown_basis(sets: list[DetectionSet]) -> TiedownBasis:
    """The ONLY tie-down count this engine trusts.

    Counted: tiedown_closeup detections that are not bonus_only (the
    anchor close-up pack is the counting source; full-frame strap
    hits carry bonus_only=True and are skipped), plus user-added
    tie-down marks from tap-to-correct. Nothing else counts - not
    chain hints, not full-frame straps, not a guess.
    """
    counted: list[Detection] = []
    basis_present = False
    for ds in sets:
        if ds.photo_role == "closeup":
            basis_present = True
        for d in ds.detections:
            if d.bonus_only:
                continue
            if d.kind == "tiedown_closeup" or _is_user_tiedown_mark(d):
                counted.append(d)
    if counted:
        basis_present = True
    detail = (
        f"{len(counted)} counted from close-up detections and user marks "
        f"({len([d for d in counted if d.source == 'user'])} user-added); "
        "full-frame bonus-only strap signals are never counted"
    )
    return TiedownBasis(
        count=len(counted),
        detection_ids=[d.id for d in counted],
        basis_present=basis_present,
        detail=detail,
    )


# ---------------------------------------------------------------------
# Check 1 - tie-down count, 393.110.
# ---------------------------------------------------------------------

def required_tiedowns(load_length_ft: float, weight_lb: float | None) -> tuple[int | None, str]:
    """Required tie-down count by length (and, at <=5 ft, weight).

    Returns (required, note). required is None only in the <=5 ft /
    unknown-weight case, where the rule itself branches on weight:
    1 tie-down at 1,100 lb or less, 2 above that. The note then
    carries the assumption path the verdict must state honestly.
    For any load longer than 5 ft the count does not depend on
    weight at all, so no weight is needed to decide the check.
    """
    if load_length_ft <= 5.0:
        if weight_lb is None:
            return None, (
                "load is 5 ft or shorter and no weight was supplied: "
                "393.110 requires 1 tie-down if the load weighs 1,100 lb "
                "or less and 2 if it weighs more"
            )
        return (1 if weight_lb <= 1100.0 else 2), ""
    if load_length_ft <= 10.0:
        return 2, ""
    extra = math.ceil((load_length_ft - 10.0) / 10.0)
    return 2 + extra, ""


def check_tiedown_count(sets, inputs: UserInputs, basis: TiedownBasis) -> Verdict:
    cid = "tiedown_count"
    if inputs.load_length_ft is None:
        return _verdict(
            cid, COULD_NOT_TELL,
            "Tie-down count cannot be checked: no load length was supplied, "
            "and 393.110 sets the required number by the length of the load. "
            "Enter the load length to run this check.",
            confidence_basis="missing input: load_length_ft",
        )
    required, note = required_tiedowns(inputs.load_length_ft, inputs.weight_lb)
    if not basis.basis_present:
        req_txt = f"{required}" if required is not None else "1 or 2 (depends on weight)"
        return _verdict(
            cid, COULD_NOT_TELL,
            f"This load needs {req_txt} tie-down(s) for its length "
            f"({inputs.load_length_ft:g} ft), but no tie-downs could be "
            "counted: tie-downs are counted from anchor close-up photos "
            "plus any you mark by tapping them on the photo, and neither "
            "was supplied. Full-frame photos cannot count straps "
            "reliably (they are too thin to read at that distance).",
            confidence_basis="no close-up detection set and no user tie-down marks supplied",
        )
    observed = basis.count
    if required is None:
        # <=5 ft, weight unknown: the verdict turns on which branch
        # the observed count satisfies.
        if observed >= 2:
            return _verdict(
                cid, PASS,
                f"{observed} tie-downs counted, and that satisfies "
                "393.110 either way for a load 5 ft or shorter: 1 is "
                "enough at 1,100 lb or less, 2 above that, and at "
                "least 2 are on the load.",
                detection_ids=basis.detection_ids,
                confidence_basis=basis.detail + "; " + note,
            )
        if observed == 1:
            return _verdict(
                cid, COULD_NOT_TELL,
                "1 tie-down counted on a load 5 ft or shorter. Whether "
                "that is enough depends on the load's weight, which was "
                "not supplied: 1 tie-down is enough only at 1,100 lb "
                "or less; above that, 393.110 requires 2. Supply the "
                "weight to decide this check.",
                detection_ids=basis.detection_ids,
                confidence_basis=basis.detail + "; " + note,
            )
        return _verdict(
            cid, CHECK_THIS,
            "0 tie-downs counted on this load, and 393.110 requires at "
            "least 1 for any load (2 if this load weighs over "
            "1,100 lb). The load is effectively unsecured.",
            detection_ids=basis.detection_ids,
            minimum_fix="Add at least 1 tie-down now; add 2 unless you can "
                        "confirm the load weighs 1,100 lb or less.",
            confidence_basis=basis.detail + "; " + note,
        )
    if observed >= required:
        return _verdict(
            cid, PASS,
            f"{observed} tie-downs counted; 393.110 requires {required} "
            f"for a {inputs.load_length_ft:g} ft load.",
            detection_ids=basis.detection_ids,
            confidence_basis=basis.detail,
        )
    shortfall = required - observed
    return _verdict(
        cid, CHECK_THIS,
        f"Only {observed} tie-down(s) counted; 393.110 requires "
        f"{required} for a {inputs.load_length_ft:g} ft load. "
        f"Short by {shortfall}.",
        detection_ids=basis.detection_ids,
        minimum_fix=f"Add at least {shortfall} more tie-down(s), spaced along "
                    "the load, before rolling.",
        confidence_basis=basis.detail,
    )


# ---------------------------------------------------------------------
# Check 2 - aggregate working load limit, 393.106 / 393.108.
# ---------------------------------------------------------------------

def _wll_entries(inputs: UserInputs, basis: TiedownBasis) -> tuple[list[tuple[float, str]] | None, str]:
    """Normalise the user's WLL input to [(wll_lb, attachment), ...].

    Returns (entries, problem). entries is None when the check
    cannot be computed, with the reason in problem.
    """
    raw = inputs.tiedown_wll_lb
    if raw is None:
        return None, "no tie-down working load limit ratings were supplied"
    default_att = (inputs.tiedown_attachment or "indirect").strip().lower()
    if isinstance(raw, (int, float)):
        if not basis.basis_present:
            return None, (
                "a uniform per-tie-down rating was supplied but the number "
                "of tie-downs is unknown (no close-up count), so the "
                "aggregate cannot be totalled"
            )
        return [(float(raw), default_att)] * basis.count, ""
    entries: list[tuple[float, str]] = []
    for item in raw:
        if isinstance(item, (int, float)):
            entries.append((float(item), default_att))
        elif isinstance(item, dict) and "wll_lb" in item:
            att = str(item.get("attachment", default_att)).strip().lower()
            entries.append((float(item["wll_lb"]), att))
        else:
            return None, f"unreadable tie-down rating entry: {item!r}"
    if not entries:
        return None, "the tie-down rating list was empty"
    return entries, ""


def _att_factor(attachment: str) -> float:
    # Direct tie-down: counts at half its rating. Indirect
    # (over-the-top, anchored both sides): counts in full.
    return 0.5 if attachment.startswith("direct") else 1.0


def check_aggregate_wll(sets, inputs: UserInputs, basis: TiedownBasis) -> Verdict:
    cid = "aggregate_wll"
    entries, problem = _wll_entries(inputs, basis)
    if inputs.weight_lb is None:
        problem = (problem + "; " if problem else "") + "no load weight was supplied"
        entries = None
    if entries is None:
        return _verdict(
            cid, COULD_NOT_TELL,
            "Tie-down strength cannot be checked from the photo: a photo "
            "cannot verify working load limit ratings or load weight, and "
            f"this check needs both numbers. Missing: {problem}. Supply "
            "the load weight and each tie-down's rating and this check "
            "runs on those numbers.",
            confidence_basis=f"not computable: {problem}",
        )
    aggregate = sum(wll * _att_factor(att) for wll, att in entries)
    required = 0.5 * float(inputs.weight_lb)
    parts = ", ".join(
        f"{wll:g} lb x {_att_factor(att):g} ({att})" for wll, att in entries
    )
    basis_txt = (
        f"computed from user-supplied numbers only (not photo-verified): "
        f"aggregate WLL = {parts} = {aggregate:,.0f} lb; required = 50% of "
        f"{inputs.weight_lb:,.0f} lb = {required:,.0f} lb"
    )
    if aggregate >= required:
        return _verdict(
            cid, PASS,
            f"Combined working load limit is {aggregate:,.0f} lb against "
            f"a required {required:,.0f} lb (50% of the load's "
            f"{inputs.weight_lb:,.0f} lb). Direct tie-downs were counted "
            "at half rating, over-the-top tie-downs in full.",
            confidence_basis=basis_txt,
        )
    shortfall = required - aggregate
    return _verdict(
        cid, CHECK_THIS,
        f"Combined working load limit is {aggregate:,.0f} lb but "
        f"393.106 requires at least {required:,.0f} lb (50% of the "
        f"load's {inputs.weight_lb:,.0f} lb). Short by {shortfall:,.0f} lb "
        "of aggregate rating.",
        minimum_fix=f"Add tie-down capacity of at least {shortfall:,.0f} lb "
                    "aggregate WLL (remember a direct tie-down only counts "
                    "half its rating), or split the load.",
        confidence_basis=basis_txt,
    )


# ---------------------------------------------------------------------
# Check 3 - heavy equipment, 393.130.
# ---------------------------------------------------------------------

def check_heavy_equipment(sets, inputs: UserInputs, basis: TiedownBasis):
    """Returns (verdict, not_applicable_reason); exactly one is None."""
    cid = "heavy_equipment"
    if inputs.load_type != "heavy_wheeled_tracked":
        return None, (
            "not a heavy wheeled/tracked equipment load "
            f"(load type: {inputs.load_type}); 393.130 does not apply"
        )
    accessory_note = (
        "Whether buckets, booms and other accessories are lowered and "
        "secured separately cannot be verified from a photo."
    )
    if inputs.weight_lb is None:
        return _verdict(
            cid, COULD_NOT_TELL,
            "This is a heavy wheeled/tracked load, but its weight was "
            "not supplied, and 393.130's requirements trigger at "
            "10,000 lb: at or above that weight the machine needs at "
            "least 4 tie-downs. Supply the machine's weight to decide "
            "this check. " + accessory_note,
            confidence_basis="missing input: weight_lb (the 10,000 lb trigger cannot be evaluated)",
        ), None
    if inputs.weight_lb < 10000.0:
        return None, (
            f"machine weight {inputs.weight_lb:,.0f} lb is under the "
            "10,000 lb trigger, so 393.130's heavy-equipment rule does "
            "not apply; the general tie-down rules (check: tie-down "
            "count) still do"
        )
    if not basis.basis_present:
        return _verdict(
            cid, COULD_NOT_TELL,
            f"This machine weighs {inputs.weight_lb:,.0f} lb, so "
            "393.130 requires at least 4 tie-downs - but no tie-downs "
            "could be counted (counting needs anchor close-up photos "
            "or tie-downs you mark on the photo). " + accessory_note,
            confidence_basis="weight trigger met; no close-up count basis supplied",
        ), None
    if basis.count >= 4:
        return _verdict(
            cid, PASS,
            f"{basis.count} tie-downs counted on a "
            f"{inputs.weight_lb:,.0f} lb machine; 393.130 requires at "
            "least 4. " + accessory_note,
            detection_ids=basis.detection_ids,
            confidence_basis=basis.detail,
        ), None
    shortfall = 4 - basis.count
    return _verdict(
        cid, CHECK_THIS,
        f"Only {basis.count} tie-down(s) counted on a "
        f"{inputs.weight_lb:,.0f} lb machine; 393.130 requires at "
        f"least 4 for equipment at or over 10,000 lb. Short by "
        f"{shortfall}. " + accessory_note,
        detection_ids=basis.detection_ids,
        minimum_fix=f"Add at least {shortfall} more tie-down(s), placed near "
                    "the front and rear of the machine, before rolling.",
        confidence_basis=basis.detail,
    ), None


# ---------------------------------------------------------------------
# Check 4 - forward blocking (side-view geometry).
# ---------------------------------------------------------------------

def check_forward_blocking(sets, mdict: dict | None) -> Verdict:
    cid = "forward_blocking"
    cal = (mdict or {}).get("calibration") or {}
    if not mdict or cal.get("status") != "calibrated":
        reason = cal.get("reason") or "no measurement run was supplied"
        return _verdict(
            cid, COULD_NOT_TELL,
            "Forward blocking cannot be judged: there is no calibrated "
            f"measurement of the load against the bed ({reason}). Run "
            "the measurement stage with the trailer bed length to "
            "enable this check.",
            confidence_basis=f"no calibration: {reason}",
        )
    try:
        ppi = float(cal["pixels_per_inch"])
        bed_min_x = float(cal["bed_min_x"])
    except (TypeError, ValueError, KeyError):
        ppi, bed_min_x = 0.0, 0.0
    if not math.isfinite(ppi) or ppi <= 0 or not math.isfinite(bed_min_x):
        return _verdict(
            cid, COULD_NOT_TELL,
            "Forward blocking cannot be judged: the calibration "
            "numbers supplied are not usable (pixels-per-inch must "
            "be a positive finite number).",
            confidence_basis="calibration present but numerically invalid",
        )
    cargo = [d for ds in sets for d in ds.by_kind("cargo")]
    if not cargo:
        return _verdict(
            cid, COULD_NOT_TELL,
            "Forward blocking cannot be judged: no cargo detections in "
            "the side photo, so the load's front edge cannot be placed "
            "against the front of the bed.",
            measurement_names=["calibration"],
            confidence_basis="calibration present but no cargo detections",
        )
    front = None
    front_det = None
    for d in cargo:
        st = _det_stats(d)
        edge = float(st.min_x) if st is not None else float(d.bbox[0])
        if front is None or edge < front:
            front, front_det = edge, d
    gap_in = max(0.0, (front - bed_min_x) / ppi)
    ids = [front_det.id, str(cal.get("bed_detection_id"))]
    basis = (
        "front gap derived deterministically from the Day 2 "
        "calibration (bed front edge, pixels-per-inch) and the "
        f"frontmost cargo extent ({front_det.kind} {front_det.id}); "
        "geometry shows slide room, it cannot prove what the load "
        "rests against"
    )
    if gap_in <= FORWARD_BLOCKING_PASS_GAP_IN:
        return _verdict(
            cid, PASS,
            f"The load's front edge sits {gap_in:.1f} in from the front "
            "of the bed, effectively against the front structure: it "
            "has no room to build momentum before it meets it.",
            detection_ids=ids,
            measurement_names=["calibration"],
            confidence_basis=basis,
        )
    if gap_in <= FORWARD_BLOCKING_MAX_GAP_IN:
        return _verdict(
            cid, CHECK_THIS,
            f"The load can slide forward: its front edge is {gap_in:.1f} in "
            "behind the front of the bed, and nothing in the photo shows "
            "what would stop it. Under braking that gap is how far the "
            "load can surge before it hits anything.",
            detection_ids=ids,
            measurement_names=["calibration"],
            minimum_fix="Shift the load forward until it is effectively "
                        "against the headboard or bulkhead (under "
                        f"{FORWARD_BLOCKING_PASS_GAP_IN:g} in of open gap), "
                        "or place secured blocking in the gap.",
            confidence_basis=basis,
        )
    return _verdict(
        cid, CHECK_THIS,
        f"The load can slide forward: its front edge is {gap_in:.1f} in "
        "behind the front of the bed with nothing shown blocking it. "
        "Under hard braking that gap is how far the load can surge.",
        detection_ids=ids,
        measurement_names=["calibration"],
        minimum_fix="Block the front of the load: shift it forward against "
                    "the headboard or bulkhead, or place secured blocking "
                    "so it is effectively against the front (under "
                    f"{FORWARD_BLOCKING_PASS_GAP_IN:g} in of open gap).",
        confidence_basis=basis,
    )


# ---------------------------------------------------------------------
# Check 5 - edge protection.
# ---------------------------------------------------------------------

def _cross_view(records: list[dict] | None, role: str) -> dict | None:
    """The first successful cross-check view for a photo role."""
    for r in records or []:
        if not isinstance(r, dict):
            continue
        if r.get("photo_role") == role and r.get("status") == "ok":
            view = r.get("view")
            return view if isinstance(view, dict) else {}
    return None


def check_edge_protection(records: list[dict] | None) -> Verdict:
    cid = "edge_protection"
    view = _cross_view(records, "closeup")
    signal = (view or {}).get("straps_over_sharp_edge_without_protection")
    if signal == "yes":
        return _verdict(
            cid, CHECK_THIS,
            "The independent photo read of the anchor close-up reports "
            "a strap or chain crossing a sharp edge with no protector "
            "visible. An unprotected edge can cut a strap through "
            "under load.",
            confidence_basis="dual-model cross-check (Muse Spark, close-up read); "
                             "SAM segmentation does not judge edges",
        )
    if signal == "no":
        return _verdict(
            cid, PASS,
            "The independent photo read of the anchor close-up reports "
            "no strap or chain crossing a sharp edge without a "
            "protector.",
            confidence_basis="dual-model cross-check (Muse Spark, close-up read); "
                             "a photo read, not a physical inspection",
        )
    return _verdict(
        cid, COULD_NOT_TELL,
        "Edge protection cannot be judged from the evidence supplied: "
        "it needs a close-up of each strap where it crosses an edge, "
        "read for protectors. Take an anchor close-up per tie-down "
        "and run the cross-check.",
        confidence_basis="no close-up cross-check evidence about straps over edges",
    )


# ---------------------------------------------------------------------
# Check 6 - overhang + lights/plate, combined honestly.
# ---------------------------------------------------------------------

def check_overhang_lights_plate(mdict: dict | None, records: list[dict] | None) -> Verdict:
    cid = "overhang_lights_plate"
    parts: list[str] = []
    sub_status: list[str] = []
    det_ids: list[str] = []
    meas_names: list[str] = []
    basis: list[str] = []
    fixes: list[str] = []

    # -- overhang part (Day 2 measurement) --
    m = _measurement(mdict, "rear_overhang")
    if m is None:
        parts.append("Overhang: not measured (no measurement run supplied).")
        sub_status.append(COULD_NOT_TELL)
    elif m.get("status") == "measured" and m.get("value") is not None:
        meas_names.append("rear_overhang")
        det_ids.extend(m.get("source_detection_ids") or [])
        v = float(m["value"])
        if v <= 0.0:
            parts.append("Overhang: the load ends at or inside the rear of the bed (0 in overhang).")
            sub_status.append(PASS)
        else:
            parts.append(f"Overhang: the load extends {v:.1f} in past the rear of the bed.")
            sub_status.append(CHECK_THIS)
            fixes.append(
                "Shorten or reposition the load to remove the overhang; any "
                "load still extending well past the rear (over 4 ft) needs a "
                "red flag by day and lights at night at its extreme end, and "
                "the load itself still has to be fully secured"
            )
        basis.append(f"rear_overhang measurement: {m.get('confidence_basis', '')}")
    else:
        parts.append(f"Overhang: could not be measured ({m.get('reason') or m.get('status')}).")
        sub_status.append(COULD_NOT_TELL)

    # -- lights / plate part (cross-check rear view only) --
    view = _cross_view(records, "rear")
    if view is None:
        parts.append(
            "Lights and plate: not checked (needs the rear photo read by "
            "the cross-check; segmentation cannot judge whether lights "
            "or the plate are obscured)."
        )
        sub_status.append(COULD_NOT_TELL)
    else:
        lights = view.get("tail_lights_visible")
        plate = view.get("license_plate_visible")
        obscured = [name for name, vis in (("tail lights", lights), ("license plate", plate)) if vis is False]
        if obscured:
            parts.append(f"Lights and plate: the rear photo read reports the {' and '.join(obscured)} obscured by the load.")
            sub_status.append(CHECK_THIS)
            fixes.append("Reposition the load (or fit auxiliary lights / relocate the plate) so the tail lights and license plate are fully visible from behind")
        elif lights is True and plate is True:
            parts.append("Lights and plate: the rear photo read reports both visible and unobscured.")
            sub_status.append(PASS)
        else:
            parts.append("Lights and plate: the rear photo read could not tell whether both are visible.")
            sub_status.append(COULD_NOT_TELL)
        basis.append("lights/plate from the dual-model cross-check rear read (Muse Spark); SAM has no equivalent signal")

    if CHECK_THIS in sub_status:
        status = CHECK_THIS
    elif COULD_NOT_TELL in sub_status:
        status = COULD_NOT_TELL
    else:
        status = PASS
    return _verdict(
        cid, status, " ".join(parts),
        detection_ids=det_ids,
        measurement_names=meas_names,
        minimum_fix="; ".join(fixes) if fixes else None,
        confidence_basis="; ".join(basis) if basis else "no measurement and no rear cross-check supplied",
    )


# ---------------------------------------------------------------------
# Check 7 - tarp coverage (loose material only).
# ---------------------------------------------------------------------

def check_tarp_coverage(inputs: UserInputs, mdict: dict | None):
    """Returns (verdict, not_applicable_reason); exactly one is None."""
    cid = "tarp_coverage"
    if inputs.load_type != "loose_material":
        return None, (
            f"load type is {inputs.load_type}, not loose material; the "
            "tarp check only applies to loose loads"
        )
    m = _measurement(mdict, "tarp_coverage")
    if m is None:
        return _verdict(
            cid, COULD_NOT_TELL,
            "Tarp coverage cannot be checked: no tarp measurement was "
            "supplied for this loose load. Run the measurement stage "
            "on the side photo.",
            confidence_basis="no tarp_coverage measurement supplied",
        ), None
    if m.get("status") in ("could_not_measure", "not_applicable") or m.get("value") is None:
        return _verdict(
            cid, COULD_NOT_TELL,
            "Tarp coverage cannot be checked: "
            f"{m.get('reason') or 'the measurement layer could not produce a coverage figure'}.",
            measurement_names=["tarp_coverage"],
            confidence_basis=m.get("confidence_basis") or "measurement unavailable",
        ), None
    exposed = float(m["value"].get("exposed_pct", 0.0))
    covered = float(m["value"].get("covered_pct", 0.0))
    proxy = "proxy_only" in (m.get("flags") or [])
    basis_txt = m.get("confidence_basis", "")
    if proxy:
        basis_txt += ("; the tarp figure is a proxy signal (tarp/canvas "
                      "prompts), carried from the measurement layer")
    if exposed <= 0.0:
        return _verdict(
            cid, PASS,
            "The tarp measurement shows the loose load fully covered "
            "(0% exposed).",
            measurement_names=["tarp_coverage"],
            confidence_basis=basis_txt,
        ), None
    return _verdict(
        cid, CHECK_THIS,
        f"The loose load is only partly covered: about {exposed:.0f}% "
        f"of it is exposed ({covered:.0f}% covered). Exposed loose "
        "material sheds debris onto the road.",
        measurement_names=["tarp_coverage"],
        minimum_fix="Re-tarp so the whole load is covered with no exposed "
                    "gaps, and secure the tarp edges so it cannot flap loose.",
        confidence_basis=basis_txt,
    ), None


# ---------------------------------------------------------------------
# Check 8 - weight placement. NEVER PASS: visual estimate only.
# ---------------------------------------------------------------------

def check_weight_placement(mdict: dict | None) -> Verdict:
    cid = "weight_placement"
    m = _measurement(mdict, "load_centroid_vs_axle")
    if m is None or m.get("value") is None:
        reason = (m or {}).get("reason") or "no centroid measurement was supplied"
        return _verdict(
            cid, COULD_NOT_TELL,
            f"Weight placement cannot be called: {reason}. Placement is "
            "a visual estimate only in any case - a photo shows where "
            "the bulk sits, never how heavy it is.",
            confidence_basis="visual estimate only; no usable centroid measurement",
        )
    value = m["value"]
    offset = float(value.get("offset_in", 0.0))
    unc = float((m.get("uncertainty") or {}).get("plus_minus_in", 0.0))
    pct = float(value.get("centroid_bed_position_pct", 0.0))
    ids = m.get("source_detection_ids") or []
    if offset > unc:
        return _verdict(
            cid, CHECK_THIS,
            f"Photo estimate puts the load's bulk {offset:.1f} in behind "
            f"the axle, more than the +/-{unc:.1f} in uncertainty of the "
            "estimate: the load reads rear-heavy, which unloads the "
            "tongue and can make the trailer sway. This is a visual "
            "estimate of where the bulk sits, not a weight measurement.",
            measurement_names=["load_centroid_vs_axle"],
            detection_ids=ids,
            minimum_fix="Shift the load forward so its bulk sits over or "
                        "ahead of the axle, then recheck with a new photo.",
            confidence_basis=m.get("confidence_basis", "") + "; visual estimate only",
        )
    if offset < -unc:
        detail = (
            f"the bulk sits {-offset:.1f} in ahead of the axle "
            f"(+/-{unc:.1f} in), the direction you want"
        )
    else:
        detail = (
            f"the bulk is within the +/-{unc:.1f} in uncertainty of the "
            "axle line"
        )
    return _verdict(
        cid, COULD_NOT_TELL,
        f"Weight placement cannot be called from this photo: the photo "
        f"estimate puts the bulk at {pct:.0f}% of the bed length and "
        f"{detail}, so placement cannot be decided from this photo. "
        "A photo shows where the bulk sits, not how heavy it is.",
        measurement_names=["load_centroid_vs_axle"],
        detection_ids=ids,
        confidence_basis=m.get("confidence_basis", "") + "; visual estimate only, never a PASS",
    )


# ---------------------------------------------------------------------
# Cross-check application (Tier 2.5): attach Spark's independent
# view; a disagreement over something Spark can see demotes a PASS
# to CHECK_THIS. Nothing is ever upgraded.
# ---------------------------------------------------------------------

def _attach_cross(verdict: Verdict, spark_view: str, sam_view: str, agreement: bool) -> None:
    verdict.cross_check = {
        "spark_view": spark_view,
        "sam_view": sam_view,
        "agreement": agreement,
    }


def _demote(verdict: Verdict, extra_finding: str, fix: str) -> None:
    if verdict.status == PASS:
        verdict.status = CHECK_THIS
        verdict.finding += " " + extra_finding
        if verdict.minimum_fix is None:
            verdict.minimum_fix = fix


def apply_crosscheck(verdicts: list[Verdict], records: list[dict] | None,
                     basis: TiedownBasis) -> None:
    """Mutates verdicts in place, attaching cross_check records."""
    if not records:
        return
    by_id = {v.check_id: v for v in verdicts}

    closeup = _cross_view(records, "closeup")
    if closeup is not None and "tiedown_count" in by_id:
        spark_n = closeup.get("tiedown_count")
        v = by_id["tiedown_count"]
        if isinstance(spark_n, int):
            sam_view = (
                f"SAM close-up detections + user marks counted {basis.count} "
                "tie-down(s)"
                if basis.basis_present else
                "no SAM close-up count basis was supplied"
            )
            agree = basis.basis_present and spark_n == basis.count
            _attach_cross(
                v,
                f"Spark's independent read of the close-up counted {spark_n} tie-down(s)",
                sam_view,
                agree,
            )
            if not agree:
                _demote(
                    v,
                    f"Cross-check disagreement: Spark counted {spark_n} "
                    f"tie-down(s) in the close-up while the SAM-based "
                    f"count is {basis.count if basis.basis_present else 'unavailable'}; "
                    "both views are shown, so treat the count as "
                    "unsettled and recount before rolling.",
                    "Recount the tie-downs in the close-ups (tap to correct "
                    "any the segmentation missed) and re-run the check.",
                )

    side = _cross_view(records, "side")
    if side is not None:
        fb = side.get("front_blocked")
        if fb in ("yes", "no") and "forward_blocking" in by_id:
            v = by_id["forward_blocking"]
            spark_says_blocked = fb == "yes"
            geom_blocked = v.status == PASS
            agree = spark_says_blocked == geom_blocked and v.status != COULD_NOT_TELL
            _attach_cross(
                v,
                f"Spark's read of the side photo: load {'appears blocked at the front' if spark_says_blocked else 'does not appear blocked at the front'}",
                f"geometry verdict: {v.status}",
                agree,
            )
            if not spark_says_blocked:
                _demote(
                    v,
                    "Cross-check disagreement: Spark's read of the side "
                    "photo does not show the load blocked at the front, "
                    "while the geometry put it inside the blocking "
                    "threshold; both views are shown.",
                    "Confirm the load is physically blocked at the front "
                    "(headboard, bulkhead, or secured blocking) before rolling.",
                )
        rh = side.get("load_looks_rear_heavy")
        if rh in ("yes", "no") and "weight_placement" in by_id:
            v = by_id["weight_placement"]
            _attach_cross(
                v,
                f"Spark's read of the side photo: load {'looks rear-heavy' if rh == 'yes' else 'does not look rear-heavy'}",
                f"centroid estimate verdict: {v.status} (visual estimate only)",
                (rh == "yes") == (v.status == CHECK_THIS),
            )

    rear = _cross_view(records, "rear")
    if rear is not None and "tarp_coverage" in by_id:
        tarp_view = rear.get("tarp_covering_load")
        if tarp_view in ("yes", "no"):
            v = by_id["tarp_coverage"]
            agree = (tarp_view == "yes") == (v.status == PASS)
            _attach_cross(
                v,
                f"Spark's read of the rear photo: tarp {'is covering the load' if tarp_view == 'yes' else 'is not covering the load'}",
                f"tarp measurement verdict: {v.status} (proxy signal)",
                agree,
            )
            if tarp_view == "no":
                _demote(
                    v,
                    "Cross-check disagreement: Spark's rear-photo read "
                    "says the tarp is not covering the load; both views "
                    "are shown.",
                    "Re-tarp so the whole load is covered and recheck.",
                )


# ---------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------

def evaluate_rules(sets, measurements, inputs, crosscheck=None) -> dict:
    """Run all 8 checks.

    sets: list of DetectionSet (or detection dicts) - one per photo of
      the load (side, rear, close-ups).
    measurements: MeasurementReport, its dict form, or None.
    inputs: UserInputs or its dict form.
    crosscheck: list of cross-check result dicts (crosscheck.py), or
      None. Spark views feed checks 5 and 6 directly and are attached
      to other checks by apply_crosscheck.
    """
    dsets = _as_sets(sets)
    mdict = _as_measurements_dict(measurements)
    if not isinstance(inputs, UserInputs):
        inputs = UserInputs.from_dict(inputs)
    # Cross-check records can arrive from a client body (the web
    # recheck path): keep only dict-shaped records so a malformed
    # entry is ignored rather than crashing the whole evaluation.
    records = [r for r in (crosscheck or []) if isinstance(r, dict)]

    basis = tiedown_basis(dsets)
    verdicts: list[Verdict] = []
    not_applicable: list[dict] = []

    verdicts.append(check_tiedown_count(dsets, inputs, basis))
    verdicts.append(check_aggregate_wll(dsets, inputs, basis))

    v, na = check_heavy_equipment(dsets, inputs, basis)
    if v is not None:
        verdicts.append(v)
    else:
        not_applicable.append({"check_id": "heavy_equipment", "reason": na})

    verdicts.append(check_forward_blocking(dsets, mdict))
    verdicts.append(check_edge_protection(records))
    verdicts.append(check_overhang_lights_plate(mdict, records))

    v, na = check_tarp_coverage(inputs, mdict)
    if v is not None:
        verdicts.append(v)
    else:
        not_applicable.append({"check_id": "tarp_coverage", "reason": na})

    verdicts.append(check_weight_placement(mdict))

    apply_crosscheck(verdicts, records, basis)

    verdicts.sort(key=lambda v: v.risk_rank)
    counts = {PASS: 0, CHECK_THIS: 0, COULD_NOT_TELL: 0}
    for v in verdicts:
        counts[v.status] += 1
    return {
        "schema": "loadmaster.verdicts/1",
        "inputs": inputs.to_dict(),
        "tiedown_count_basis": {
            "count": basis.count,
            "basis_present": basis.basis_present,
            "detection_ids": basis.detection_ids,
            "detail": basis.detail,
        },
        "verdicts": [v.to_dict() for v in verdicts],
        "not_applicable": not_applicable,
        "summary": counts,
    }
