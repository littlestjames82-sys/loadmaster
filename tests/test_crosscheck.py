"""Cross-check tests (Day 3). Spark's transport is stubbed; no live
API anywhere in this file. The stub follows the same payload ->
response-dict contract as sam_client._post, including the output
item/content shape _extract_output_text reads.
"""

import json
from pathlib import Path

import pytest

from loadmaster.crosscheck import ask_spark, parse_answer


def spark_response(text):
    return {"output": [{"content": [{"type": "output_text", "text": text}]}]}


class StubPoster:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.payloads = []

    def __call__(self, payload, timeout):
        self.payloads.append(payload)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def photo(tmp_path) -> Path:
    # prepare_image tolerates non-image bytes only via its Pillow
    # fallback path; write a real tiny JPEG via Pillow when present,
    # else skip image-dependent tests.
    try:
        from PIL import Image
    except Exception:
        pytest.skip("Pillow unavailable")
    p = tmp_path / "photo.jpg"
    Image.new("RGB", (64, 48), (120, 90, 60)).save(p, format="JPEG")
    return p


# -- parse_answer --------------------------------------------------------

def test_parse_closeup_plain_and_fenced():
    text = '{"tiedown_count": 2, "straps_over_sharp_edge_without_protection": "no"}'
    view = parse_answer(text, "closeup")
    assert view == {"tiedown_count": 2,
                    "straps_over_sharp_edge_without_protection": "no"}
    fenced = "Here is my answer:\n```json\n" + text + "\n```"
    assert parse_answer(fenced, "closeup") == view


def test_parse_rear_and_side():
    rear = parse_answer(
        '{"tail_lights_visible": false, "license_plate_visible": true, '
        '"tarp_covering_load": "yes"}', "rear")
    assert rear == {"tail_lights_visible": False,
                    "license_plate_visible": True,
                    "tarp_covering_load": "yes"}
    side = parse_answer(
        '{"load_looks_rear_heavy": "unclear", "front_blocked": "no"}', "side")
    assert side == {"load_looks_rear_heavy": "unclear", "front_blocked": "no"}


def test_parse_loose_values_coerce_to_unclear():
    view = parse_answer(
        '{"tiedown_count": "several", '
        '"straps_over_sharp_edge_without_protection": "maybe"}', "closeup")
    assert view["tiedown_count"] is None
    assert view["straps_over_sharp_edge_without_protection"] == "unclear"
    rear = parse_answer(
        '{"tail_lights_visible": "visible", "license_plate_visible": '
        '"obscured", "tarp_covering_load": null}', "rear")
    assert rear["tail_lights_visible"] is True
    assert rear["license_plate_visible"] is False
    assert rear["tarp_covering_load"] == "unclear"


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        parse_answer("I cannot see the photo clearly.", "closeup")
    with pytest.raises(ValueError):
        parse_answer("{}", "bogus-role")


# -- ask_spark with a stubbed transport -----------------------------------

def test_ask_spark_ok_and_independent_prompt(photo):
    stub = StubPoster(spark_response(
        '{"tiedown_count": 1, '
        '"straps_over_sharp_edge_without_protection": "unclear"}'))
    res = ask_spark(photo, "closeup", _poster=stub)
    assert res.status == "ok"
    assert res.view["tiedown_count"] == 1
    assert res.attempts == 1
    # The payload must carry the image and the structured question,
    # addressed to Spark - and nothing about SAM or verdicts (the
    # read is independent by construction).
    payload = stub.payloads[0]
    assert payload["model"] == "muse-spark-1.3"
    content = payload["input"][0]["content"]
    kinds = [c["type"] for c in content]
    assert kinds == ["input_text", "input_image"]
    assert content[1]["image_url"].startswith("data:image/")
    prompt_text = content[0]["text"].lower()
    assert "sam" not in prompt_text and "verdict" not in prompt_text
    # Raw answer kept for audit.
    assert "tiedown_count" in res.raw_answer
    assert res.to_dict()["raw_answer"] == res.raw_answer


def test_ask_spark_retries_then_succeeds(photo):
    stub = StubPoster(
        RuntimeError("boom"),
        spark_response('{"load_looks_rear_heavy": "yes", "front_blocked": "no"}'),
    )
    res = ask_spark(photo, "side", _poster=stub, backoff_s=0.0)
    assert res.status == "ok" and res.attempts == 2
    assert res.view["load_looks_rear_heavy"] == "yes"


def test_ask_spark_unparseable_answer_is_error_not_empty_view(photo):
    stub = StubPoster(
        spark_response("Sorry, I cannot tell anything from this photo."),
        spark_response("Still nothing structured."),
    )
    res = ask_spark(photo, "rear", _poster=stub, backoff_s=0.0)
    assert res.status == "error"
    assert res.view == {}  # never an "all clear" empty view
    assert res.error


def test_ask_spark_unknown_role():
    res = ask_spark(Path("/tmp/whatever.jpg"), "top", _poster=StubPoster())
    assert res.status == "error"
