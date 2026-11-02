"""Prompt-pack coverage for the matrix-2 cargo nouns (Oct 31, 2026).

Pins the findings recorded in prompt_packs.py: generic loads get the
unitized sweep (previously no cargo prompt at all), the new load types
exist, and the pipeline's cargo dedupe helper behaves.
"""

from loadmaster.pipeline import _bbox_iou
from loadmaster.prompt_packs import CARGO_PROMPTS, LOOSE_LOAD_TYPES, get_pack


def _cargo_prompts(load_type):
    pack = get_pack("flatbed", load_type, "side")
    return [spec.prompt for spec in pack if spec.kind == "cargo"]


def test_generic_load_gets_unitized_sweep():
    prompts = _cargo_prompts("generic")
    assert "pallet" in prompts
    assert "shrink wrapped pallet" in prompts
    assert "stack of bags" in prompts
    assert "load" not in prompts  # the vague noun stays out


def test_pallets_pack_runs_plain_and_wrapped_nouns():
    prompts = _cargo_prompts("pallets")
    assert prompts == ["pallet", "shrink wrapped pallet", "wrapped pallet"]


def test_bagged_pack_leads_with_bag_stack():
    assert _cargo_prompts("bagged")[0] == "stack of bags"


def test_scrap_pack_uses_scrap_nouns_and_is_loose():
    prompts = _cargo_prompts("scrap")
    assert "pile of scrap metal" in prompts
    assert "junk pile" in prompts
    assert "scrap" in LOOSE_LOAD_TYPES


def test_machine_packs_add_construction_vehicle():
    # Nov 1 probe on the dev set: "excavator" returned empty on a
    # step-trailer excavator photo and "wheel loader" returned empty
    # on a rear oversize-loader photo, while "construction vehicle"
    # hit both (0.85 / 0.87). The class noun rides along in every
    # machine pack; the specific noun stays first.
    for load_type in ("excavator", "wheel_loader", "heavy_equipment"):
        prompts = _cargo_prompts(load_type)
        assert "construction vehicle" in prompts, load_type
    assert _cargo_prompts("excavator")[0] == "excavator"
    assert _cargo_prompts("wheel_loader")[0] == "wheel loader"


def test_heavy_equipment_pack_adds_combine_harvester():
    # Nov 1 probe: "combine harvester" hit a combine on a trailer
    # (0.56) where the pack's only noun, "excavator", was empty.
    assert "combine harvester" in _cargo_prompts("heavy_equipment")


def test_beams_pack_adds_stack_of_lumber():
    # Nov 1 probe: "beams" returned empty on a lumber-bundle deck
    # photo while "stack of lumber" hit (0.66).
    prompts = _cargo_prompts("beams")
    assert prompts[0] == "beams"
    assert "stack of lumber" in prompts


def test_steel_pack_adds_roll_of_steel():
    prompts = _cargo_prompts("steel")
    assert "steel frame" in prompts
    assert "roll of steel" in prompts


def test_all_cargo_prompt_values_are_lists():
    for load_type, specs in CARGO_PROMPTS.items():
        assert isinstance(specs, list) and specs, load_type
        assert all(spec.kind == "cargo" for spec in specs), load_type


def test_bbox_iou():
    assert _bbox_iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert _bbox_iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert 0.80 < _bbox_iou([0, 0, 10, 10], [0, 0, 10, 9]) < 0.95


def test_bbox_containment():
    from loadmaster.pipeline import _bbox_containment
    assert _bbox_containment([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert _bbox_containment([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    # A small box fully inside a big one: containment 1.0 even
    # though IoU is low.
    assert _bbox_containment([2, 2, 4, 4], [0, 0, 10, 10]) == 1.0
    assert _bbox_iou([2, 2, 4, 4], [0, 0, 10, 10]) < 0.1
    # Half of the smaller box inside: 0.5.
    assert _bbox_containment([0, 0, 10, 10], [5, 0, 25, 10]) == 0.5


def test_same_object_containment_pair_from_dev_photos():
    # The measured push48 regression (Nov 1): on the chained lowboy
    # photo the 'excavator' box sits entirely inside the loose
    # 'construction vehicle' box (containment 1.00, IoU 0.46), so
    # the IoU-only dedupe let one machine enter the set twice.
    from loadmaster.pipeline import _same_object
    tight = [337, 48, 2805, 1262]
    loose = [70, 41, 3705, 1836]
    assert _bbox_iou(tight, loose) < 0.85
    assert _same_object(tight, loose)
    # Genuinely separate objects stay separate.
    assert not _same_object([0, 0, 10, 10], [20, 20, 30, 30])
    assert not _same_object([0, 0, 10, 10], [8, 0, 30, 10])


def test_keep_existing_rules():
    from loadmaster.pipeline import _keep_existing
    # IoU pair: higher confidence stays (long-standing rule).
    assert _keep_existing([0, 0, 10, 10], 0.9, [0, 0, 10, 9], 0.5)
    assert not _keep_existing([0, 0, 10, 10], 0.5, [0, 0, 10, 9], 0.9)
    # Containment pair: the SMALLER box stays, whichever arrived
    # first, because the container is the loose outline.
    tight = [337, 48, 2805, 1262]
    loose = [70, 41, 3705, 1836]
    assert _keep_existing(tight, 0.61, loose, 0.92)
    assert not _keep_existing(loose, 0.92, tight, 0.61)


def _closeup_specs(load_type):
    return get_pack("flatbed", load_type, "closeup")


def test_closeup_pack_counts_straps_and_chains_for_any_load():
    # Oct 31 cable-reel case: a chain-secured load declared generic
    # counted zero tie-downs because the close-up pack asked only
    # for ratchet straps. Chains are a counting source now.
    for load_type in ("generic", "firewood", "excavator"):
        specs = _closeup_specs(load_type)
        prompts = [spec.prompt for spec in specs]
        assert "ratchet strap" in prompts, load_type
        assert "chain" in prompts, load_type
        counting = [spec for spec in specs if spec.kind == "tiedown_closeup"]
        assert len(counting) == 2, load_type
        assert all(not spec.bonus_only for spec in counting), load_type


def test_fullframe_chain_stays_a_hint():
    # The full-frame chain prompt remains presence-only: counting
    # still lives in close-ups and user marks, never full frames.
    pack = get_pack("flatbed", "excavator", "side")
    chain_specs = [spec for spec in pack if spec.prompt == "chain"]
    assert chain_specs and all(spec.kind == "chain_hint" for spec in chain_specs)
