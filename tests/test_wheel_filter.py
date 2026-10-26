"""Wheel filter tests: raw SAM wheel boxes must collapse into axle
estimates, with fragments and high boxes filtered out, and the raw
count never exposed as a finding."""

from loadmaster.detections import filter_wheels_to_axle

W, H = 2000, 1000


def test_single_wheel_one_axle():
    boxes = [([800, 700, 1000, 950], 0.92)]
    axles, stats = filter_wheels_to_axle(boxes, W, H)
    assert len(axles) == 1
    assert axles[0].kind == "axle_estimate"
    assert axles[0].confidence == 0.92
    assert stats["kept"] == 1


def test_two_close_wheels_one_axle_two_far_clusters_two_axles():
    boxes = [
        ([300, 700, 480, 940], 0.8),
        ([520, 710, 700, 950], 0.85),   # same cluster as the first
        ([1500, 700, 1700, 950], 0.7),  # far away: its own cluster
    ]
    axles, stats = filter_wheels_to_axle(boxes, W, H)
    assert len(axles) == 2
    assert stats["clusters"] == 2


def test_fragments_and_high_boxes_filtered():
    boxes = [
        ([100, 800, 130, 830], 0.5),    # tiny fragment: too small
        ([900, 60, 1150, 300], 0.6),    # high in the frame: not a wheel position
        ([850, 720, 1080, 960], 0.9),   # the real wheel
    ]
    axles, stats = filter_wheels_to_axle(boxes, W, H)
    assert len(axles) == 1
    assert stats["raw"] == 3
    assert stats["kept"] == 1


def test_no_wheels_no_axle():
    axles, stats = filter_wheels_to_axle([], W, H)
    assert axles == []
    assert stats["clusters"] == 0


def test_axle_label_never_a_count():
    boxes = [([300, 700, 480, 940], 0.8), ([520, 710, 700, 950], 0.85)]
    axles, _ = filter_wheels_to_axle(boxes, W, H)
    for a in axles:
        assert "wheel" not in a.label
        assert not any(ch.isdigit() for ch in a.label)
