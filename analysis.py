#!/usr/bin/env python3
"""
Coupon-deal extraction via OpenAI's official API (GPT-6 Luna through the
/v1/responses endpoint).

Sends the local (processed) image straight to GPT-6 Luna as a base64 data
URL — no upload step needed — and extracts the complete coupon deal shown
in the image (replaced the earlier KIE-hosted GPT-5.6 Luna extraction, and
the Gemini Flash model before that).

KIE is only still used by the AI image-generation path (generate.py, with
its uploads paced through kie_ratelimit.py).
"""

import base64
import json
import os
import re
import time

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = "gpt-6-luna"

# GPT-6 Luna standard-tier pricing, USD per 1M tokens (developers.openai.com
# /api/docs/models/gpt-6-luna): input $0.10, cached input $0.01, cache
# writes $0.125, output $0.50. The Responses API usage object reports
# cached reads but not cache writes, so the write rate is unused for now.
OPENAI_PRICE_PER_MTOK = {
    "input": 0.10,
    "cached_input": 0.01,
    "cache_write": 0.125,
    "output": 0.50,
}


def _openai_cost_usd(usage) -> float:
    """Estimated request cost from a Responses-API usage object, using the
    GPT-6 Luna rates above. Cached input tokens are billed at 10% of the
    uncached input rate."""
    cached = (usage.input_tokens_details.cached_tokens
              if usage.input_tokens_details else 0) or 0
    uncached_input = usage.input_tokens - cached
    return (
        uncached_input * OPENAI_PRICE_PER_MTOK["input"]
        + cached * OPENAI_PRICE_PER_MTOK["cached_input"]
        + usage.output_tokens * OPENAI_PRICE_PER_MTOK["output"]
    ) / 1_000_000

_openai_client: OpenAI | None = None


def _get_openai_client() -> OpenAI:
    """Lazily build the shared OpenAI client (SDK reads OPENAI_API_KEY;
    our own retry loop below handles transient failures, so the SDK's
    built-in retries are disabled)."""
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            raise AnalysisError("OPENAI_API_KEY is not set — add it to .env")
        _openai_client = OpenAI(api_key=OPENAI_API_KEY, max_retries=0)
    return _openai_client

DEAL_EXTRACTION_PROMPT = """You are an expert coupon deal extraction assistant. Analyze ONE uploaded image and extract the complete coupon deal, shopping scenario, promotion, or product offer it shows, as clean JSON for a listicle-style blog post.
The retailer/brand context is: {brand_name}
Extract only what the image shows — never search the internet, invent information, or use anything from previous requests. Analyze the whole image: products, banners, prices, coupon screenshots, fine print, dates, totals, and promotional text.

1. VALID DEAL DETECTION
A valid deal MUST be tied to purchasing specific retail products in a store transaction: real items, real prices, and something for the shopper to clip, submit, activate, or qualify for (a coupon, rebate, cashback, or register/loyalty reward). Valid examples: a digital or paper coupon on specific products; an offer unlocked by clipping, a spend threshold, or a qualifying quantity; a multi-product bundle with a combined price; a spend-and-save promotion; a rebate or cashback on purchased products.
Return the bare word `false` (no JSON) if:
- Only a price reduction is shown ("was $X, now $Y / you save $Z", sale, markdown, clearance) with NO coupon, rebate, cashback, or reward attached.
- The image shows a referral program, referral code, sign-up/first-receipt/app-download bonus, or invite-a-friend offer (e.g. Fetch, Ibotta, Aisle) instead of a product purchase — even if it displays a dollar amount or the word "reward".
- The ONLY incentive is loyalty or reward POINTS (e.g. "buy 2, earn 1,250 points") with no dollar coupon, rebate, cashback, or register reward attached — points-only promotions are not usable coupon deals.
- The image is only product photography, packaging, or a shelf/display shot with no visible price, coupon, discount, or promotional offer — even if the product and brand are clearly identifiable.
- The image has no recognizable deal, is unrelated to shopping, is too blurry/cropped/unreadable, or lacks the information needed to identify a usable offer.
Never return a JSON object with empty fields when no valid deal is found.

2. ONE DEAL OR SEVERAL
Treat the whole image as ONE deal when the products form one basket or transaction: a shared subtotal, several coupons contributing to one scenario, a single bundle or spend threshold, or products clearly meant to be bought together. Put every participating product in `items` and every coupon in `coupons_used`; never split them into separate deal objects.
Return a JSON array only when the image genuinely contains multiple independent transactions or promotions with distinct pricing and savings — otherwise return a single JSON object, prioritizing the main featured deal. Never create separate entries for products or banners that belong to the same transaction.

3. OUTPUT SCHEMA
For a valid deal, return exactly these fields (the items' displayed prices combine into the subtotal; savings come from coupons, rewards, and rebates):
{"name": "", "summary": "", "items": [], "subtotal": "", "coupons_used": "", "rewards": "", "rebates": "", "final_net_cost": "", "validity_date": "", "strategy": []}
name — concise and descriptive: Brand + Product Type for a single product; a name identifying the products or the shopping scenario for a bundle or promotion. Never include a couponer's name, username, or any creator identity; do not prepend the retailer name unless necessary to identify the deal.
summary — original, approximately 50 words, listicle-ready: describe the products and their general purpose, the shopping opportunity, and the value of the offer; mention the major products for a bundle. Use fresh wording — never copy sentences from the image, never mention any couponer, YouTuber, influencer, or social media account, never invent product benefits, specifications, savings, or coupon terms, and avoid repeating the detailed coupon math.
items — array of strings listing every product in the deal, each as full Brand + Product Type (never brand alone when the product type is visible) with quantities where applicable. If a price is CLEARLY readable, append it: "1 × all Free Clear Liquid Laundry Detergent — $4.00" (per-unit price: "— $4.00 each"); if not clearly readable, list the item WITHOUT a price — never guess or estimate. It is correct for some items to have prices and others not. Do not add sizes, flavors, scents, or packaging details unless needed to identify the qualifying product.
subtotal — the TOTAL price of all items in the transaction BEFORE any coupons, rewards, or rebates are applied (e.g. "$14.88") — what the products ring up at, including any sale or clearance pricing (NOT the original regular price). Use the image's stated subtotal, or the sum of the clearly readable item prices at their displayed prices. If item prices are readable, you MUST provide the subtotal (their sum) — never leave it empty when item prices are available; empty string only when no prices are readable at all.
coupons_used — the TOTAL value of all coupons applied to the transaction (manufacturer digital, store, paper, and instant discounts) as ONE string (e.g. "$3.00"); "$0.00" if none. Never include rewards or rebates here, and never treat a product's displayed price as a coupon.
rewards — the TOTAL value of register rewards, loyalty points, or gift-card offers earned by PURCHASING the products, as ONE money string (e.g. "$2.00"); "$0.00" if none. Never count referral, sign-up, or app-download bonuses here. If a reward is stated in points without a dollar value (e.g. "1,250 points"), return "$0.00" and state the points requirement in `strategy` instead.
rebates — the TOTAL value of rebates or cashback (app rebates, mail-in rebates, reimbursement offers) as ONE string (e.g. "$14.88"); "$0.00" if none.
final_net_cost — what the shopper actually pays (or gets back) after coupons, rewards, and rebates (e.g. "$0.00", "FREE ($0.00)", "$10.75 + tax"). Use the stated final amount when shown, preserving "FREE" wording and any "+ tax" qualification exactly; otherwise subtract the applicable coupons, rewards, and rebates from the subtotal; empty string if it cannot be established reliably. Never invent additional savings to make a deal appear cheaper.
validity_date — the deal's date or date range exactly as the image presents it ("9/26 only", "6/28 – 7/4", "Valid through 7/31"), preserving the original month/day notation, "ONLY" restrictions, and day-of-week callouts; join a start and end date with " – ". Empty string if none is shown; never assume a year, and ignore a date on a decorative element unless the context connects it to the promotion.
strategy — an ordered array of direct, actionable steps covering the deal from start to finish: the products (full Brand + Product Type names, quantities, prices when shown), which coupons to clip and their requirements, the subtotal, the coupons used, any reward/rebate steps, and the final net cost when known. State every fact plainly as an instruction to the shopper; never mention the image, the source, or the extraction process; never repeat a step; never add unverified steps, prices, savings, or requirements.

4. PRODUCT NAMING
In `items` and `strategy`, always use the full Brand + Product Type and preserve distinctions between varieties the image specifies: "Tide Liquid Laundry Detergent", not "Tide"; "Mr. Clean Multi-Surface Cleaner", not "Mr. Clean"; "Snuggle Liquid Fabric Softener", not "Snuggle".

5. MATH VALIDATION (perform silently — the output must never mention these checks)
Accuracy matters more than making the deal look attractive. Verify prices and quantities; that the subtotal matches the listed items; that `coupons_used` matches the visible coupons; and that `final_net_cost` agrees with subtotal minus coupons, rewards, and rebates. Account for spending thresholds and coupon eligibility; do not assume coupons stack or apply to every item in a bundle; do not count the same savings twice; do not treat a future reward or rebate as an immediate checkout discount. If a displayed total conflicts with the calculated one, preserve the displayed amount and report the visible savings accurately — never invent an extra coupon or discount to force the math to balance.

6. OUTPUT RULES
- State every fact directly and confidently. In NO field mention the image, the source, or the verification process ("the image shows", "as shown", "pictured", "advertised", "stated", "documented", "cannot be verified"), and never include a couponer's name, username, signature, watermark, or social media handle.
- Do not copy promotional descriptions verbatim; do not add unsupported claims ("best deal", "lowest price ever", "guaranteed free"); do not claim a product is free unless the final net cost supports it.
- Return ONLY the result — no Markdown fences, no explanations, no commentary. `false` if no valid deal; one JSON object for one deal; a JSON array of objects using the same schema for multiple independent deals."""


class AnalysisError(Exception):
    pass


def _parse_deal_response(text: str) -> list[dict]:
    """Parse the model's reply into a list of deal objects.

    The prompt allows exactly three shapes: the bare word `false` (no valid
    deal), a single JSON deal object, or a JSON array of deal objects.
    Markdown fences are tolerated. Raises AnalysisError on anything else.
    """
    text = text.strip()
    if text.lower() == "false":
        return []
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        block_match = re.search(r"\[.*\]|\{.*\}", text, re.DOTALL)
        if block_match:
            text = block_match.group(0)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        if "Extra data" not in str(e):
            raise AnalysisError(f"Could not parse deal JSON from model reply: {e}") from e
        # The model occasionally concatenates several JSON objects back to
        # back (json.loads stops at the first one and reports "Extra data").
        # Recover with raw_decode: collect every object and treat them as
        # separate deals.
        decoder = json.JSONDecoder()
        deals, idx = [], 0
        while idx < len(text):
            while idx < len(text) and text[idx] not in "{[":
                idx += 1
            if idx >= len(text):
                break
            try:
                obj, idx = decoder.raw_decode(text, idx)
            except json.JSONDecodeError as e2:
                raise AnalysisError(f"Could not parse deal JSON from model reply: {e2}") from e2
            if isinstance(obj, dict):
                deals.append(obj)
            elif isinstance(obj, list):
                deals.extend(d for d in obj if isinstance(d, dict))
        if not deals:
            raise AnalysisError(f"Could not parse deal JSON from model reply: {e}") from e
        return deals
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [deal for deal in parsed if isinstance(deal, dict)]
    raise AnalysisError(f"Unexpected deal response type: {type(parsed).__name__}")


def _image_content_part(image_source: str) -> dict:
    """Build an input_image content part from either a public URL or a
    local file path (local files are inlined as base64 data URLs)."""
    if image_source.startswith(("http://", "https://", "data:")):
        return {"type": "input_image", "image_url": image_source}
    with open(image_source, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}


def analyze_deal_image(image_source: str, brand_name: str, timeout: int = 600) -> dict:
    """Call OpenAI GPT-6 Luna to extract the coupon deal(s) from an image,
    using the workspace's retailer brand as the deal context.

    `image_source` is a local file path or a public image URL.

    Returns {"deals": [deal, ...], "tokens_used": ..., "tokens": {...},
    "cost_usd": ...} — "deals" is empty when the model replied `false`
    (no valid deal).
    Raises AnalysisError on any failure — caller is responsible for
    recording a failed-analysis placeholder instead of losing the post.
    """
    if not OPENAI_API_KEY:
        raise AnalysisError("OPENAI_API_KEY is not set — add it to .env")
    client = _get_openai_client()
    prompt = DEAL_EXTRACTION_PROMPT.replace("{brand_name}", brand_name or "")
    request_input = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                _image_content_part(image_source),
            ],
        }
    ]

    # Retry loop: 429 (rate limit), 5xx (server errors — 500/501/502/503/
    # 524 etc.), connection failures, and completions with EMPTY text (the
    # model occasionally returns zero output tokens) all get up to 3
    # attempts. 429 backs off progressively per attempt; the rest back off
    # briefly (server-side hiccups usually clear in seconds).
    response = None
    for attempt in range(1, 4):
        try:
            response = client.responses.create(
                model=OPENAI_MODEL,
                input=request_input,
                reasoning={"effort": "medium"},
                timeout=timeout,
            )
        except RateLimitError:
            wait = 15 * attempt
            print(f"  ⚠️ OpenAI rate limit hit (429), attempt {attempt}/3 — waiting {wait}s")
        except APIStatusError as e:
            if (e.status_code or 0) < 500:
                raise
            wait = 5 * attempt
            print(f"  ⚠️ OpenAI server error ({e.status_code}), attempt {attempt}/3 — waiting {wait}s")
        except APIConnectionError:
            wait = 5 * attempt
            print(f"  ⚠️ OpenAI connection failed, attempt {attempt}/3 — waiting {wait}s")
        else:
            if response.output_text:
                break
            wait = 5 * attempt
            print(f"  ⚠️ OpenAI returned an empty reply (0 output tokens), attempt {attempt}/3 — waiting {wait}s")
        response = None
        if attempt < 3:
            time.sleep(wait)
    if response is None:
        raise AnalysisError("OpenAI analysis failed after 3 attempts")

    usage = response.usage
    cached_tokens = (usage.input_tokens_details.cached_tokens
                     if usage.input_tokens_details else 0) or 0
    reasoning_tokens = (usage.output_tokens_details.reasoning_tokens
                        if usage.output_tokens_details else 0) or 0
    cost_usd = _openai_cost_usd(usage)
    print(f"  🔢 Tokens — input: {usage.input_tokens} (cached: {cached_tokens}), "
          f"output: {usage.output_tokens} (reasoning: {reasoning_tokens}), "
          f"total: {usage.total_tokens} | est. cost: ${cost_usd:.4f}")

    deals = _parse_deal_response(response.output_text)

    return {
        "deals": deals,
        "tokens_used": usage.total_tokens,
        "tokens": {
            "input": usage.input_tokens,
            "cached_input": cached_tokens,
            "output": usage.output_tokens,
            "reasoning": reasoning_tokens,
        },
        "cost_usd": round(cost_usd, 4),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 analysis.py <path_to_image> [brand_name]")
        raise SystemExit(1)

    image_path = sys.argv[1]
    brand_name = sys.argv[2] if len(sys.argv) > 2 else ""

    print(f"Analyzing {image_path} with {OPENAI_MODEL}...")
    result = analyze_deal_image(image_path, brand_name)
    print(json.dumps(result, indent=2, ensure_ascii=False))
