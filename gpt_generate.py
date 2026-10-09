"""
GPT Image 2.5 Sunburst variant of generate.py — identical pipeline, different
model. Used to A/B-test image-to-image generation against nano-banana-2 for
the deal infographics.

Model: gpt-image-2-5-sunburst-image-to-image (same /api/v1/jobs/createTask +
recordInfo endpoints; the input field is `input_urls` — up to 16 reference
URLs — instead of nano-banana-2's `image_input`).

CLI (same as generate.py):
  python3 gpt_generate.py --image <path> --brand "Dollar General" [--template <path>] [--out <path>]
"""

import argparse
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import brand_mapping
from constants import AI_IMAGE_COMPARE, AI_IMAGE_MAX_BYTES, DEAL_INFOGRAPHIC_PROMPT, DEAL_INPUTS_BLOCK, INFOGRAPHIC_TEMPLATE_DIR
from image_pipeline import compress_under_limit
from generate import upload_image, make_comparison_image, _render_prompt
from kie_ratelimit import rate_limiter

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
KIE_API_KEY = os.getenv("KIE_API_KEY", "")

MAX_RETRIES = 4

MODEL = "gpt-image-2-5-sunburst-image-to-image"
UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
CREATE_TASK_URL = "https://api.kie.ai/api/v1/jobs/createTask"
GET_TASK_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"

HEADERS_AUTH = {"Authorization": f"Bearer {KIE_API_KEY}"}


def create_task(image_urls, prompt: str = None) -> str:
    """Create one gpt-image-2-5-sunburst-image-to-image task. `image_urls` is
    a single public URL or a list of them (up to 16) — e.g.
    [template_url, deal_image_url] when a layout template is used."""
    if isinstance(image_urls, str):
        image_urls = [image_urls]
    payload = {
        "model": MODEL,
        "input": {
            # gpt-image-2-5-sunburst-image-to-image: input_urls (up to 16),
            # prompt (max 20000 chars), aspect_ratio (auto or 1:1…21:9 —
            # 9:16 supported), resolution (1K/2K/4K), background
            # (transparent/opaque/auto). Only prompt/input_urls/aspect_ratio
            # are set — resolution and background use the API defaults.
            "prompt": prompt,
            "input_urls": image_urls[:16],
            "aspect_ratio": "9:16",
        },
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                CREATE_TASK_URL,
                headers={**HEADERS_AUTH, "Content-Type": "application/json"},
                json=payload,
            )
            if response.status_code == 429:
                # KIE rejects (does not queue) requests over the account's
                # 20-per-10s cap — back off a full window, not the generic 3s.
                print(f"  create_task rate-limited (429), attempt {attempt}/{MAX_RETRIES} — waiting 11s")
                if attempt == MAX_RETRIES:
                    response.raise_for_status()
                time.sleep(11)
                continue
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 200:
                raise RuntimeError(f"Task creation failed: {data}")
            return data["data"]["taskId"]
        except Exception as e:
            print(f"  create_task attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(3 * attempt)


def poll_task(task_id: str, timeout: int = 800) -> str:
    """Poll until task completes and return the result image URL."""
    start = time.time()
    interval = 15
    while time.time() - start < timeout:
        response = requests.get(
            GET_TASK_URL,
            headers=HEADERS_AUTH,
            params={"taskId": task_id},
        )
        response.raise_for_status()
        resp = response.json()
        app_code = resp.get("code")
        if app_code not in (200, None):
            raise RuntimeError(f"Poll error (code {app_code}): {resp.get('msg')}")
        data = resp.get("data", {})
        state = data.get("state")

        if state == "success":
            result = json.loads(data["resultJson"])
            return result["resultUrls"][0]
        elif state == "fail":
            raise RuntimeError(f"Task failed [{data.get('failCode', '')}]: {data.get('failMsg')}")

        print(f"  [{task_id}] state={state}, waiting {interval}s...")
        time.sleep(interval)
        interval = min(interval + 5, 30)

    raise TimeoutError(f"Task {task_id} timed out after {timeout}s")


def download_image(url: str, output_path: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(response.content)
            return
        except Exception as e:
            print(f"  download_image attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(3 * attempt)


# ── CLI: one-shot deal-infographic generation (shared helpers from generate.py) ──


def generate_image(image_path: str, brand: str, output_path: str | None = None,
                   template_path: str | None = None) -> str:
    """Generate one AI deal infographic for `image_path` using the built-in
    DEAL_INFOGRAPHIC_PROMPT via gpt-image-2-5-sunburst and the REQUIRED
    per-brand layout template (assets/templates/<brand-slug>.jpg, or
    --template for testing). Returns the output path."""
    img_path = Path(image_path)
    if not img_path.exists():
        raise SystemExit(f"❌ Image not found: {img_path}")

    brand_slug = brand_mapping.brand_slug(brand) if brand else None
    template = Path(template_path) if template_path else (
        Path(__file__).parent / INFOGRAPHIC_TEMPLATE_DIR / f"{brand_slug}.jpg")
    if not template.exists():
        raise SystemExit(
            f"❌ No layout template for brand '{brand}' at {template} — create "
            f"assets/templates/<brand-slug>.jpg first.")
    prompt = _render_prompt(brand)

    out_path = Path(output_path) if output_path else img_path.with_name(f"{img_path.stem}_ai{img_path.suffix or '.jpg'}")

    print(f"Brand   : {brand}")
    print(f"Model   : {MODEL}")
    print(f"Image   : {img_path}")
    print(f"Output  : {out_path}")
    print(f"Prompt  : {prompt[:120]}{'…' if len(prompt) > 120 else ''}")

    # Upload the original as-is — no compression, no temp copy.
    public_url = upload_image(str(img_path))
    print(f"Uploaded: {public_url}")

    image_urls = [public_url]
    template_url = upload_image(str(template))
    print(f"Template: {template_url}")
    image_urls = [template_url, public_url]  # template FIRST (layout), deal second (content)

    rate_limiter.acquire()
    task_id = create_task(image_urls, prompt=prompt)
    print(f"Task    : {task_id}")
    result_url = poll_task(task_id)
    print(f"Result  : {result_url}")

    if AI_IMAGE_COMPARE:
        # Comparison sheet: AI result on top, original below, both labeled.
        raw_path = img_path.with_name(f".generate_result_{img_path.name}")
        try:
            download_image(result_url, str(raw_path))
            compress_under_limit(str(raw_path), AI_IMAGE_MAX_BYTES)
            if make_comparison_image(raw_path, img_path, out_path):
                print(f"✅ Comparison image (AI top / original below) saved to {out_path}")
                return str(out_path)
            print("  falling back to the plain AI image")
        finally:
            raw_path.unlink(missing_ok=True)

    download_image(result_url, str(out_path))
    compress_under_limit(str(out_path), AI_IMAGE_MAX_BYTES)
    print(f"✅ AI image saved to {out_path}")
    return str(out_path)


def main():
    parser = argparse.ArgumentParser(
        description="Generate one AI deal infographic via KIE gpt-image-2-5-sunburst "
                    "(image-to-image), using the built-in DEAL_INFOGRAPHIC_PROMPT")
    parser.add_argument("--image", required=True, help="Path to the source image")
    parser.add_argument("--brand", required=True, help="Canonical brand name (e.g. 'Dollar General')")
    parser.add_argument("--out", default=None,
                        help="Output path (default: <image>_ai.<ext> next to the source)")
    parser.add_argument("--template", default=None,
                        help="Layout-template image path (default: assets/infographic_template.jpg when it exists)")
    args = parser.parse_args()

    generate_image(args.image, args.brand, output_path=args.out, template_path=args.template)


if __name__ == "__main__":
    main()
