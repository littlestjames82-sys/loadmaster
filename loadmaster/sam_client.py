"""SAM 3.1 client for the Meta Model API.

Wraps the call pattern proven on Day 0 (see the connector doc for
custom.meta-model-api and files/sam_proof.py):

- Responses API, model id "sam-3.1", ONE short noun phrase per call.
- Image sent inline as a base64 data URL, resized to a 1024 px max
  dimension first (the helper's behaviour; SAM box coordinates come
  back in that resized space).
- metadata: include_confidence=true, mask_encoding=one_bit.
- Credentials come from the registered connector via
  dynamic_credentials (an hsurr surrogate Muse swaps for the real
  key on the way to api.meta.ai). The key is never read, printed, or
  stored here.

Reliability (matrix finding): 1 of 31 matrix calls timed out and
succeeded on retry. So every call retries with backoff, max 3
attempts, and a failure after retries surfaces as status "error" -
NEVER as an empty result. An HTTP 200 with no box/mask tokens is the
only "empty" (a valid zero match).
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import add_surrogate_to_request  # noqa: E402

BASE_URL = "https://api.meta.ai/v1"
PROVIDER = "custom.meta-model-api"
HOSTS = ["api.meta.ai"]
MODEL = "sam-3.1"
MAX_DIM = 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):  # noqa: D102
        raise RuntimeError("redirect refused, verify the endpoint")


@dataclass
class SamResult:
    prompt: str
    status: str  # "ok" | "empty" | "error"
    raw_text: str = ""
    objects: list = field(default_factory=list)  # mask_decode.SamObject
    latency_s: float = 0.0
    attempts: int = 0
    error: str | None = None
    sent_width: int = 0
    sent_height: int = 0

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "empty")


def prepare_image(image_path: Path) -> tuple[str, int, int, int, int]:
    """Return (data_url, sent_w, sent_h, orig_w, orig_h)."""
    raw = image_path.read_bytes()
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        orig_w, orig_h = img.size
        img.thumbnail((MAX_DIM, MAX_DIM))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = buf.getvalue()
        mime = "image/jpeg"
        sent_w, sent_h = img.size
    except Exception:
        # No Pillow: send the original bytes; SAM still answers, and
        # the sent space is then the original space.
        suffix = image_path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        data = raw
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as im:
                sent_w, sent_h = im.size
        except Exception:
            sent_w = sent_h = 0
        orig_w, orig_h = sent_w, sent_h
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii"), sent_w, sent_h, orig_w, orig_h


def _extract_output_text(response: dict) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks = []
    for item in response.get("output", []) or []:
        for part in item.get("content", []) or []:
            if part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks)


def _post(payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        BASE_URL + "/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    add_surrogate_to_request(req, PROVIDER, allowed_hosts=HOSTS)
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def segment(
    image_path: Path,
    prompt: str,
    timeout: float = 240.0,
    max_attempts: int = 3,
    backoff_s: float = 2.0,
    _prepared: tuple[str, int, int, int, int] | None = None,
) -> SamResult:
    """One SAM call for one noun phrase, with retry/backoff."""
    data_url, sent_w, sent_h, _ow, _oh = _prepared or prepare_image(image_path)
    payload = {
        "model": MODEL,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
        "stream": False,
        "metadata": {"include_confidence": "true", "mask_encoding": "one_bit"},
    }
    started = time.monotonic()
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            parsed = _post(payload, timeout)
            text = _extract_output_text(parsed)
            from .mask_decode import parse_output_text

            objects = parse_output_text(text) if text else []
            return SamResult(
                prompt=prompt,
                status="ok" if objects else "empty",
                raw_text=text,
                objects=objects,
                latency_s=time.monotonic() - started,
                attempts=attempt,
                sent_width=sent_w,
                sent_height=sent_h,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            last_error = f"HTTP {exc.code}: {body}"
            if exc.code not in (429, 500, 502, 503, 504):
                break  # other 4xx will not heal on retry
        except Exception as exc:  # timeouts, URLError, parse errors
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_attempts:
            time.sleep(backoff_s * (2 ** (attempt - 1)))
    return SamResult(
        prompt=prompt,
        status="error",
        latency_s=time.monotonic() - started,
        attempts=max_attempts,
        error=last_error,
        sent_width=sent_w,
        sent_height=sent_h,
    )


def run_prompts(
    image_path: Path,
    prompts: list[str],
    max_workers: int = 8,
    timeout: float = 240.0,
) -> list[SamResult]:
    """Run one photo's whole prompt pack in PARALLEL (matrix latency
    finding: ~15-20 s per call in sequence is too slow serially).
    Results come back in pack order."""
    prepared = prepare_image(image_path)
    if len(prompts) == 1:
        return [segment(image_path, prompts[0], timeout=timeout, _prepared=prepared)]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(prompts))) as pool:
        return list(
            pool.map(
                lambda p: segment(image_path, p, timeout=timeout, _prepared=prepared),
                prompts,
            )
        )
