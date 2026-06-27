"""
Phase 2 — Vision smoke test.

Verifies the vision stack end to end without needing a human:
  - modules import, tools register (terminal), router mounts
  - image encoding (resize + JPEG + data-url round-trips)
  - a LIVE screen capture + vision-LLM describe (real, free model)
  - the /vision/describe OCR path reads text off a generated image
  - graceful degradation (bad path / bad image return a clean sentence, never raise)

Run:  python scripts/vision_smoke.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ok = 0
fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK {name}")
    else:
        fail += 1
        print(f"  XX {name}  {extra}")


def main() -> int:
    print("[1] imports + tool registration")
    import app.tools as tools
    tools.discover()
    for t in ("read_screen", "look", "describe_image"):
        tool = tools.get(t)
        check(f"tool {t} registered + terminal", tool is not None and tool.terminal)

    from app.services.vision import get_vision, VisionError
    from app.services.vision import multimodal
    check("VisionService facade", get_vision() is not None)

    print("[2] image encoding")
    from PIL import Image
    img = Image.new("RGB", (3000, 1500), (10, 20, 30))
    raw = multimodal.to_jpeg_bytes(img)
    check("downscale + JPEG encode", 0 < len(raw) < 400_000, f"{len(raw)} bytes")
    durl = multimodal.image_to_data_url(img)
    check("data URL shape", durl.startswith("data:image/jpeg;base64,"))
    back = multimodal.load_image_bytes(base64.b64decode(durl.split(",", 1)[1]))
    check("re-decodes", back.size[0] <= 1280)

    print("[3] router mounts")
    from app.routers import vision as vrouter
    paths = {r.path for r in vrouter.router.routes}
    check("/vision/screen + /vision/describe", "/vision/screen" in paths and "/vision/describe" in paths)

    print("[4] graceful degradation (no raise)")
    r = tools.get("describe_image").run({"path": r"C:\nope\not-here.jpg"})
    check("missing image -> clean sentence", isinstance(r, str) and "can't find" in r.lower(), r[:60])

    print("[5] LIVE screen capture -> vision LLM (free model)")
    try:
        out = asyncio.run(get_vision().describe_screen("In one short sentence, what is on screen?"))
        check("screen described", isinstance(out, str) and len(out) > 5 and "busy" not in out.lower(), out[:80])
        print(f"     -> {out[:120]}")
    except Exception as e:  # noqa: BLE001
        check("screen described", False, f"{type(e).__name__}: {e}")

    print("[6] LIVE OCR via /vision/describe")
    try:
        from PIL import ImageDraw
        im = Image.new("RGB", (640, 200), (8, 14, 22))
        ImageDraw.Draw(im).text((20, 80), "JARVIS OCR 4242", fill=(120, 240, 255))
        buf = io.BytesIO(); im.save(buf, format="PNG")
        durl2 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        from app.routers.vision import vision_describe, DescribeReq
        res = asyncio.run(vision_describe(DescribeReq(image=durl2, question="Read the text exactly.", ocr=True)))
        check("OCR read the text", res.get("ok") and "4242" in res.get("text", ""), str(res)[:90])
    except Exception as e:  # noqa: BLE001
        check("OCR read the text", False, f"{type(e).__name__}: {e}")

    print(f"\n==== vision smoke: {ok} passed, {fail} failed ====")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
