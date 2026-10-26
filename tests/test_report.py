"""Report writer tests. Spark's transport is stubbed; no live API
anywhere in this file. The stub follows the same payload ->
response-dict contract as sam_client._post.
"""

from loadmaster.report import (
    build_prompt,
    check_label,
    fallback_report,
    ordered,
    write_report,
)


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


VERDICTS = [
    {
        "check": "tiedown_count",
        "status": "CHECK THIS",
        "citation": "49 CFR 393.110",
        "finding": "Only 4 tie-downs counted; 6 required for a 46 ft load.",
        "fix": "Add at least 2 more tie-downs, spaced along the load.",
    },
    {
        "check": "forward_blocking",
        "status": "CHECK THIS",
        "citation": "49 CFR 393.104",
        "finding": "The load can slide forward: 10.9 in gap at the front.",
        "fix": "Block the front of the load against the bulkhead.",
    },
    {
        "check": "aggregate_wll",
        "status": "COULD NOT TELL",
        "citation": "49 CFR 393.106",
        "finding": "Needs the load weight and tie-down ratings.",
        "fix": "",
    },
    {
        "check": "tarp_coverage",
        "status": "PASS",
        "citation": "49 CFR 393.100",
        "finding": "Load fully covered.",
        "fix": "",
    },
]

INPUTS = {"trailer_type": "flatbed", "load_type": "hay", "load_length_ft": 46}


def test_ordered_puts_check_this_first_preserving_engine_order():
    result = ordered(VERDICTS)
    assert [v["check"] for v in result] == [
        "tiedown_count",
        "forward_blocking",
        "tarp_coverage",
        "aggregate_wll",
    ]


def test_check_label_known_and_unknown():
    assert check_label({"check": "tiedown_count"}) == "Tie-down count"
    assert check_label({"check_id": "aggregate_wll"}).startswith("Tie-down strength")
    assert check_label({"check": "some_new_check"}) == "some new check"


def test_fallback_headline_counts_fixes():
    report = fallback_report(VERDICTS, INPUTS)
    assert report["source"] == "template"
    assert report["headline"] == "2 things to fix before this load rolls"
    assert "Fix first: Tie-down count." in report["body"]
    assert "Also fix: Forward blocking." in report["body"]
    assert "Passed: tarp coverage." in report["body"]
    assert "Could not tell: Tie-down strength" in report["body"]
    # The honesty line is present whenever a check could not be told.
    assert "cannot verify strap tension" in report["body"]


def test_fallback_all_pass():
    report = fallback_report([VERDICTS[3]], INPUTS)
    assert report["headline"].startswith("This load passes")
    assert "cannot verify" not in report["body"]


def test_build_prompt_carries_facts_and_grounding_rule():
    prompt = build_prompt(VERDICTS, INPUTS)
    assert "Only 4 tie-downs counted" in prompt
    assert "Add at least 2 more tie-downs" in prompt
    assert "Never invent numbers" in prompt
    assert "46 ft" in prompt


def test_write_report_uses_spark_when_it_answers():
    poster = StubPoster(
        spark_response(
            "Two fixes before this hay load rolls\n\n"
            "Add two more tie-downs along the load, and block the front "
            "against the bulkhead. Tie-down strength could not be judged "
            "without the load weight and ratings."
        )
    )
    report = write_report(VERDICTS, INPUTS, _poster=poster)
    assert report["source"] == "spark"
    assert report["model"] == "muse-spark-1.3"
    assert report["headline"] == "Two fixes before this hay load rolls"
    assert "bulkhead" in report["body"]
    # The transport received exactly the grounded prompt.
    sent = poster.payloads[0]["input"][0]["content"][0]["text"]
    assert "Only 4 tie-downs counted" in sent


def test_write_report_falls_back_when_spark_fails():
    poster = StubPoster(RuntimeError("boom"), RuntimeError("boom again"))
    report = write_report(VERDICTS, INPUTS, _poster=poster)
    assert report["source"] == "template"
    assert report["headline"] == "2 things to fix before this load rolls"


def test_write_report_falls_back_on_unusable_shape():
    poster = StubPoster(spark_response("x" * 300))  # one long line, no body
    report = write_report(VERDICTS, INPUTS, _poster=poster)
    assert report["source"] == "template"


def test_write_report_no_verdicts():
    report = write_report(None, INPUTS, _poster=StubPoster())
    assert report["source"] == "template"
    assert "No verdicts" in report["headline"]
