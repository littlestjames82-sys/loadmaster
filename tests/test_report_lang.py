"""Spanish report tests: the prompt must instruct Spark to write
in Spanish, the template fallback must be a real Spanish template
(not English relabeled), and English behavior must be unchanged.
Transport is stubbed; no live API.
"""

from loadmaster import report as report_module

VERDICTS = [
    {
        "check_id": "tiedown_count",
        "status": "CHECK THIS",
        "citation": "49 CFR 393.110",
        "finding": "Only 1 tie-down counted; 2 required.",
        "fix": "Add one more tie-down.",
    },
    {
        "check_id": "forward_blocking",
        "status": "PASS",
        "citation": "49 CFR 393.104",
        "finding": "Load is blocked at the front.",
        "fix": "",
    },
    {
        "check_id": "aggregate_wll",
        "status": "COULD NOT TELL",
        "citation": "49 CFR 393.106",
        "finding": "No load weight supplied.",
        "fix": "Supply the load weight.",
    },
]


def _stub_poster(captured):
    def poster(payload, timeout):
        captured.append(payload)
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Titular de prueba sin punto\n\nCuerpo de prueba.",
                        }
                    ],
                }
            ]
        }

    return poster


def test_prompt_instructs_spanish_only_when_asked():
    es = report_module.build_prompt(VERDICTS, {}, lang="es")
    en = report_module.build_prompt(VERDICTS, {}, lang="en")
    assert "in Spanish" in es
    assert "in Spanish" not in en
    assert "49 CFR" in es  # citations stay verbatim


def test_spanish_template_is_spanish():
    rep = report_module.fallback_report(VERDICTS, {}, lang="es")
    assert rep["source"] == "template"
    assert "corregir" in rep["headline"]
    assert "Corrija primero" in rep["body"]
    assert "No se pudo determinar" in rep["body"]
    assert "Una foto no puede verificar" in rep["body"]


def test_english_template_unchanged():
    rep = report_module.fallback_report(VERDICTS, {})
    assert "to fix" in rep["headline"]
    assert "Fix first:" in rep["body"]


def test_write_report_spanish_end_to_end_stubbed():
    captured = []
    rep = report_module.write_report(
        VERDICTS, {}, _poster=_stub_poster(captured), lang="es"
    )
    assert rep["source"] == "spark"
    assert rep["lang"] == "es"
    assert "in Spanish" in captured[0]["input"][0]["content"][0]["text"]


def test_write_report_failure_falls_back_to_spanish_template():
    def boom(payload, timeout):
        raise RuntimeError("down")

    rep = report_module.write_report(
        VERDICTS, {}, _poster=boom, lang="es", max_attempts=1
    )
    assert rep["source"] == "template"
    assert rep["lang"] == "es"
    assert "corregir" in rep["headline"]
