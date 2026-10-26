"""Decode SAM 3.1 output text into boxes, bitmaps, and polygons.

Route decision (Day 1, Oct 26, 2026; see BUILD_LOG.md): Meta's
``meta-sam-parser`` package does the real decoding. The ``one_bit``
mask payload is base85 text over an arithmetic-coded bitstream, not a
plain RLE, so a hand-rolled decoder would mean reimplementing that
codec. Verified on the Day 0 proof output: 1 box, 1 mask at 0.863,
mask raster 953x324 with 123,346 set pixels.

If ``meta-sam-parser`` is not importable, box tokens are still parsed
with a regex fallback and masks are returned as undecodable payloads
(never silently dropped), so the pipeline degrades honestly.

All coordinates here are in the space of the image AS SENT to SAM
(the client resizes to a 1024 px maximum dimension). Callers scale to
original-image coordinates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

try:  # the real decoder
    import meta_sam_parser as _msp

    HAVE_PARSER = True
except Exception:  # pragma: no cover - exercised only without the package
    _msp = None
    HAVE_PARSER = False


@dataclass
class MaskRaster:
    """A decoded one-bit mask bitmap placed inside the sent image."""

    width: int
    height: int
    offset_x: int = 0  # left edge within the sent image
    offset_y: int = 0  # top edge within the sent image
    data: bytes = b""  # row-major, 1 byte per pixel, 0/1

    def __post_init__(self) -> None:
        if len(self.data) != self.width * self.height:
            raise ValueError(
                f"mask data length {len(self.data)} != {self.width}x{self.height}"
            )

    @property
    def set_pixels(self) -> int:
        return sum(1 for b in self.data if b)

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        """Tight bbox of set pixels, in sent-image coordinates."""
        min_x, min_y = self.width, self.height
        max_x = max_y = -1
        w = self.width
        for y in range(self.height):
            row = self.data[y * w : (y + 1) * w]
            for x, v in enumerate(row):
                if v:
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
        if max_x < 0:
            return (self.offset_x, self.offset_y, self.offset_x, self.offset_y)
        return (
            self.offset_x + min_x,
            self.offset_y + min_y,
            self.offset_x + max_x + 1,
            self.offset_y + max_y + 1,
        )

    def to_rle(self) -> dict:
        """Run-length encoding over the row-major bitmap, for JSON."""
        runs: list[list[int]] = []
        prev = -1
        count = 0
        for b in self.data:
            v = 1 if b else 0
            if v == prev:
                count += 1
            else:
                if prev >= 0:
                    runs.append([prev, count])
                prev, count = v, 1
        if prev >= 0:
            runs.append([prev, count])
        return {
            "width": self.width,
            "height": self.height,
            "offset": [self.offset_x, self.offset_y],
            "runs": runs,
        }

    @classmethod
    def from_rle(cls, rle: dict) -> "MaskRaster":
        data = bytearray()
        for v, n in rle["runs"]:
            data.extend(bytes([1 if v else 0]) * int(n))
        ox, oy = rle.get("offset", [0, 0])
        return cls(
            width=int(rle["width"]),
            height=int(rle["height"]),
            offset_x=int(ox),
            offset_y=int(oy),
            data=bytes(data),
        )

    def polygon(self, max_points: int = 240) -> list[list[int]]:
        """Outer boundary of the bitmap as [x, y] points in sent-image
        coordinates (Moore-neighbour tracing, stride-simplified)."""
        w, h, data = self.width, self.height, self.data

        def inside(x: int, y: int) -> bool:
            return 0 <= x < w and 0 <= y < h and data[y * w + x] != 0

        start = None
        for y in range(h):
            for x in range(w):
                if data[y * w + x]:
                    start = (x, y)
                    break
            if start:
                break
        if start is None:
            return []

        # Moore tracing, clockwise, starting west of the start pixel.
        dirs = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
        boundary: list[tuple[int, int]] = []
        cur = start
        back = 4  # came from the west
        limit = 4 * w * h + 16
        steps = 0
        while steps < limit:
            steps += 1
            boundary.append(cur)
            found = None
            for k in range(8):
                d = (back + 1 + k) % 8  # start just past the backtrack dir
                nx, ny = cur[0] + dirs[d][0], cur[1] + dirs[d][1]
                if inside(nx, ny):
                    found = (nx, ny)
                    back = (d + 4) % 8
                    break
            if found is None or found == start:
                break
            cur = found

        if len(boundary) > max_points:
            stride = len(boundary) / float(max_points)
            boundary = [boundary[int(i * stride)] for i in range(max_points)]
        return [
            [self.offset_x + x, self.offset_y + y] for x, y in boundary
        ]


@dataclass
class SamObject:
    """One segmented object: a box plus (usually) its decoded mask."""

    object_id: str
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2 in sent-image coords
    confidence: float | None
    mask: MaskRaster | None = None
    raw_mask_payload: str | None = field(default=None, repr=False)


_BOX_RE = re.compile(
    r"<\|box;x1=(?P<x1>\d+);y1=(?P<y1>\d+);x2=(?P<x2>\d+);y2=(?P<y2>\d+);"
    r"w=(?P<w>\d+);h=(?P<h>\d+)(?:;c=(?P<c>[0-9.]+))?\|>"
)


def extract_box_tokens(text: str) -> list[SamObject]:
    """Regex fallback: box tokens only (masks need the real parser)."""
    out = []
    for i, mm in enumerate(_BOX_RE.finditer(text)):
        conf = float(mm.group("c")) if mm.group("c") else None
        out.append(
            SamObject(
                object_id=str(i),
                bbox=(
                    int(mm.group("x1")),
                    int(mm.group("y1")),
                    int(mm.group("x2")),
                    int(mm.group("y2")),
                ),
                confidence=conf,
            )
        )
    return out


def parse_output_text(text: str) -> list[SamObject]:
    """Parse one SAM output_text lane into objects with decoded masks."""
    if not text:
        return []
    if not HAVE_PARSER:
        return extract_box_tokens(text)

    parser = _msp.image_segmentation_format().create_parser()
    parser.push(text)
    result = parser.finish(_msp.CompletedOutcome()).result

    boxes: dict[str, SamObject] = {}
    order: list[str] = []
    for rec in result.records:
        oid = str(getattr(rec, "object_id", len(order)))
        if rec.kind == "box" or hasattr(rec, "left") and not hasattr(rec, "mask"):
            obj = SamObject(
                object_id=oid,
                bbox=(int(rec.left), int(rec.top), int(rec.right), int(rec.bottom)),
                confidence=float(rec.confidence) if rec.confidence is not None else None,
            )
            boxes[oid] = obj
            order.append(oid)
    for rec in result.records:
        if getattr(rec, "mask", None) is None:
            continue
        mk = rec.mask
        oid = str(getattr(rec, "object_id", ""))
        raster_bytes = _msp.decode_mask_to_raster(mk)
        b = rec.bounds
        ox = int(b.left) if b is not None else 0
        oy = int(b.top) if b is not None else 0
        raster = MaskRaster(
            width=int(mk.width),
            height=int(mk.height),
            offset_x=ox,
            offset_y=oy,
            data=bytes(1 if v else 0 for v in raster_bytes),
        )
        conf = float(rec.confidence) if getattr(rec, "confidence", None) is not None else None
        if oid in boxes:
            boxes[oid].mask = raster
            boxes[oid].raw_mask_payload = mk.payload
            if boxes[oid].confidence is None:
                boxes[oid].confidence = conf
        else:
            obj = SamObject(
                object_id=oid or str(len(order)),
                bbox=raster.bbox,
                confidence=conf,
                mask=raster,
                raw_mask_payload=mk.payload,
            )
            boxes[obj.object_id] = obj
            order.append(obj.object_id)
    return [boxes[o] for o in order]
