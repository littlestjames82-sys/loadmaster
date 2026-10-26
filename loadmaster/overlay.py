"""Annotated overlay renderer.

Draws a DetectionSet onto its source photo: translucent mask fills,
bounding boxes, and labels (kind + confidence). The overlay is the
evidence picture a finding will link to later; it renders exactly
what the detection set holds, including user corrections.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .detections import DetectionSet
from .mask_decode import MaskRaster

KIND_COLORS = {
    "trailer_bed": (64, 156, 255),
    "wheel": (255, 170, 0),
    "axle_estimate": (255, 120, 0),
    "cargo": (80, 200, 120),
    "tarp": (170, 110, 255),
    "tiedown_closeup": (255, 64, 64),
    "chain_hint": (255, 220, 60),
    "user_added": (0, 220, 220),
}


def render_overlay(ds: DetectionSet, out_path: Path) -> Path:
    img = Image.open(ds.image_path).convert("RGB")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))

    # Mask fills first. The bitmap IS the fill's alpha channel: a
    # tile of the kind colour whose alpha is 110 exactly where the
    # bitmap is set and 0 elsewhere, composited over the layer. (The
    # Day 1 version pasted an alpha-0 fill through an L mask, which
    # left the layer's alpha at 0 everywhere - the fills never
    # rendered at all, and the polygon outlines were left doing all
    # the visual work, badly; see BUILD_LOG Day 2.)
    for det in ds.detections:
        color = KIND_COLORS.get(det.kind, (255, 255, 255))
        if det.mask_rle:
            try:
                raster = MaskRaster.from_rle(det.mask_rle)
                alpha = Image.frombytes(
                    "L", (raster.width, raster.height),
                    bytes(110 if b else 0 for b in raster.data),
                )
                tile = Image.new("RGBA", (raster.width, raster.height), color + (255,))
                tile.putalpha(alpha)
                full = Image.new("RGBA", img.size, (0, 0, 0, 0))
                full.paste(tile, (int(raster.offset_x), int(raster.offset_y)))
                layer = Image.alpha_composite(layer, full)
            except Exception:
                pass  # a bad mask must not kill the overlay; box still draws

    ldraw = ImageDraw.Draw(layer)
    for det in ds.detections:
        color = KIND_COLORS.get(det.kind, (255, 255, 255))
        if det.polygon and len(det.polygon) >= 3:
            ldraw.polygon([tuple(p) for p in det.polygon], outline=color + (200,), width=2)

    img = Image.alpha_composite(img.convert("RGBA"), layer)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:
        font = ImageFont.load_default()

    for det in ds.detections:
        color = KIND_COLORS.get(det.kind, (255, 255, 255))
        x1, y1, x2, y2 = det.bbox
        draw.rectangle([x1, y1, x2, y2], outline=color + (255,), width=3)
        label = det.label
        if det.confidence is not None:
            label += f" {det.confidence:.2f}"
        if det.source == "user":
            label += " (user)"
        if det.bonus_only:
            label += " (bonus)"
        ty = max(0, y1 - 26)
        draw.rectangle([x1, ty, x1 + 10 + 11 * len(label), ty + 24], fill=color + (230,))
        draw.text((x1 + 5, ty + 4), label, fill=(0, 0, 0, 255), font=font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, format="PNG")
    return out_path
