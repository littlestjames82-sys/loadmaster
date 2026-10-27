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

import bisect
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

    # Stored masks arrive inside client-supplied detection sets (the
    # recheck path): a hostile or broken RLE must be refused by size
    # BEFORE it is allocated, never expanded on trust.
    MAX_PIXELS = 100_000_000

    @classmethod
    def from_rle(cls, rle: dict) -> "MaskRaster":
        width = int(rle["width"])
        height = int(rle["height"])
        if width <= 0 or height <= 0 or width * height > cls.MAX_PIXELS:
            raise ValueError(
                f"mask dimensions {width}x{height} are not acceptable"
            )
        data = bytearray()
        total = 0
        for v, n in rle["runs"]:
            n = int(n)
            if n < 0:
                raise ValueError("mask run length is negative")
            total += n
            if total > width * height:
                raise ValueError("mask runs exceed the declared dimensions")
            data.extend(bytes([1 if v else 0]) * n)
        ox, oy = rle.get("offset", [0, 0])
        return cls(
            width=width,
            height=height,
            offset_x=int(ox),
            offset_y=int(oy),
            data=bytes(data),
        )

    def _row_runs(self) -> list[list[tuple[int, int]]]:
        """Per-row runs of set pixels as inclusive (x1, x2) pairs,
        in local (raster) coordinates."""
        rows: list[list[tuple[int, int]]] = []
        w, data = self.width, self.data
        for y in range(self.height):
            row = data[y * w : (y + 1) * w]
            runs: list[tuple[int, int]] = []
            x = 0
            while True:
                a = row.find(b"\x01", x)
                if a < 0:
                    break
                b = row.find(b"\x00", a)
                if b < 0:
                    b = w
                runs.append((a, b - 1))
                x = b
            rows.append(runs)
        return rows

    def _components(self) -> list[tuple[int, dict[int, list[tuple[int, int]]]]]:
        """8-connected components as (pixel count, row runs) pairs,
        largest first. Runs are inclusive (x1, x2) in local coords."""
        rows = self._row_runs()
        # Union-find over runs, keyed by (row, run index).
        parent: dict[tuple[int, int], tuple[int, int]] = {}

        def find(k: tuple[int, int]) -> tuple[int, int]:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        def union(a: tuple[int, int], b: tuple[int, int]) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        sizes: dict[tuple[int, int], int] = {}
        for y, runs in enumerate(rows):
            for i, (a, b) in enumerate(runs):
                key = (y, i)
                parent[key] = key
                sizes[key] = b - a + 1
                if y == 0:
                    continue
                for j, (pa, pb) in enumerate(rows[y - 1]):
                    # 8-connectivity: runs touching or one pixel apart join.
                    if a - 1 <= pb and pa - 1 <= b:
                        union(key, (y - 1, j))
        if not parent:
            return []
        totals: dict[tuple[int, int], int] = {}
        members: dict[tuple[int, int], dict[int, list[tuple[int, int]]]] = {}
        for (y, i), size in sizes.items():
            root = find((y, i))
            totals[root] = totals.get(root, 0) + size
            members.setdefault(root, {}).setdefault(y, []).append(rows[y][i])
        comps = [(totals[root], members[root]) for root in totals]
        comps.sort(key=lambda c: c[0], reverse=True)
        return comps

    def _largest_component_rows(self) -> dict[int, list[tuple[int, int]]] | None:
        """Row runs of the largest 8-connected component, or None.

        A SAM mask can hold several disconnected blobs (a trailer
        rail sliver plus the trailer body). A single outline polygon
        can only hug one of them, so it hugs the biggest; the bitmap
        stays the source of truth for everything else.
        """
        comps = self._components()
        return comps[0][1] if comps else None

    def polygon(self, max_points: int = 240) -> list[list[int]]:
        """Outer boundary of the bitmap as [x, y] points in sent-image
        coordinates.

        Moore-neighbour tracing of the LARGEST connected component.
        The walk carries a proper backtrack state (current pixel +
        direction to the last background pixel examined) and stops
        when a state repeats, which is Jacob's stopping criterion in
        state form: the boundary is traced exactly once. The Day 1
        version stopped at the first re-entry of the start pixel,
        traced whatever component held the first set pixel in scan
        order, and reduced the trailer mask to a 230x38 px sliver
        (a 0.4% stray blob) of its 3038x1033 bitmap. If the trace
        still degenerates (a hair-thin component), the component's
        tight bbox is returned instead of a 2-point "polygon".
        """
        comp = self._largest_component_rows()
        if not comp:
            return []
        for runs in comp.values():
            runs.sort()

        starts: dict[int, list[int]] = {
            y: [a for a, _ in runs] for y, runs in comp.items()
        }

        def inside(x: int, y: int) -> bool:
            runs = comp.get(y)
            if not runs:
                return False
            s = starts[y]
            i = bisect.bisect_right(s, x) - 1
            return i >= 0 and runs[i][0] <= x <= runs[i][1]

        start = None
        for y in sorted(comp):
            start = (comp[y][0][0], y)
            break
        assert start is not None

        # Moore tracing, clockwise. `back` is the direction from the
        # current pixel to the backtrack pixel (the last background
        # pixel examined); the start pixel is entered from the west,
        # matching scan order.
        dirs = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
        vec_to_dir = {v: i for i, v in enumerate(dirs)}
        n_comp = sum(b - a + 1 for runs in comp.values() for a, b in runs)
        boundary: list[tuple[int, int]] = []
        cur = start
        back = 4
        seen: set[tuple[tuple[int, int], int]] = set()
        limit = 8 * n_comp + 64
        steps = 0
        while steps < limit:
            steps += 1
            state = (cur, back)
            if state in seen:
                break  # loop closed: the walk would repeat exactly
            seen.add(state)
            boundary.append(cur)
            found = None
            new_back = back
            prev = back  # direction of the last background pixel examined
            for k in range(1, 9):
                d = (back + k) % 8
                nx, ny = cur[0] + dirs[d][0], cur[1] + dirs[d][1]
                if inside(nx, ny):
                    found = (nx, ny)
                    bx = cur[0] + dirs[prev][0] - nx
                    by = cur[1] + dirs[prev][1] - ny
                    new_back = vec_to_dir[(bx, by)]
                    break
                prev = d
            if found is None:
                break
            cur, back = found, new_back

        distinct = set(boundary)
        if len(distinct) < 3:
            xs = [x for runs in comp.values() for a, b in runs for x in (a, b)]
            ys = list(comp)
            x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
            return [
                [self.offset_x + x1, self.offset_y + y1],
                [self.offset_x + x2, self.offset_y + y1],
                [self.offset_x + x2, self.offset_y + y2],
                [self.offset_x + x1, self.offset_y + y2],
            ]

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
