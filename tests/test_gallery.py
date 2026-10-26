"""Gallery tests: the sample runs shipped in loadmaster/gallery/
are real captured /analyze responses. These tests pin their shape
so the /gallery page can never silently go empty or fake.
"""

from loadmaster.webapp import _gallery_html, load_gallery_entries


def test_gallery_entries_load():
    entries = load_gallery_entries()
    assert [e["slug"] for e in entries] == ["firewood", "lowboy", "closeup"]


def test_gallery_entries_are_real_runs():
    for e in load_gallery_entries():
        assert e["verdicts"], e["slug"]
        assert e["overlay_png_base64"], e["slug"]
        assert e["detections"], e["slug"]
        assert e["report"]["source"] == "spark", e["slug"]
        assert e["report"]["headline"], e["slug"]
        for v in e["verdicts"]:
            assert v["status"] in ("PASS", "CHECK THIS", "COULD NOT TELL")


def test_gallery_html_renders_all_runs():
    page = _gallery_html(load_gallery_entries())
    for e in load_gallery_entries():
        assert e["title"] in page
        assert e["report"]["headline"] in page
    assert "COULD" in page and "NOT TELL" in page
    assert 'href="/"' in page
