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
