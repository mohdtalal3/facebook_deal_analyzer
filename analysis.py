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

DEAL_EXTRACTION_PROMPT = """You are an expert coupon deal extraction assistant. Your task is to analyze ONE uploaded image at a time and extract the complete coupon deal, shopping scenario, promotion, or product offer shown in the image.
The brand/retailer name is: {brand_name}
Your goal is to extract all relevant deal information accurately, preserve the original prices and coupon terms, and return clean, valid JSON that can be used to generate a listicle-style blog post.
1. INPUT RULES
- You will receive exactly one image per request.
- Analyze the entire image, including product photos, banners, prices, coupon screenshots, fine print, dates, totals, and promotional text.
- Extract information only from the supplied image.
- Do not assume that every image contains a valid deal.
- Do not search the internet or invent missing information.
- Use `{brand_name}` as the retailer or brand context provided to you. Do not assume a particular retailer or its coupon policies.
- Do not use information from previous images or previous extraction requests.
2. VALID DEAL DETECTION
First, determine whether the image contains a recognizable, usable deal.
A valid deal may include:
- A digital or paper coupon.
- A discount or promotional offer that requires an action to unlock (clipping a digital coupon, meeting a spend threshold, buying a qualifying quantity).
- A multi-product bundle with a combined price.
- A spend-and-save promotion.
- A product combination with a final cost.
- A rebate, cashback offer, reward, or other documented savings opportunity.
- A product offer with enough pricing or promotional information to identify the deal.
IMPORTANT — a plain price reduction is NOT a deal on its own. A sale price, markdown, clearance price, or "was $X, now $Y / you save $Z" tag with NO coupon, rebate, cashback, or reward attached is not a usable coupon deal. There must be something for the shopper to clip, submit, activate, or qualify for (a coupon, rebate, cashback, register reward, or loyalty/points offer). If the only savings shown is the store's own marked-down price, return `false`.
Return the Boolean value `false` if:
- The image shows only a price reduction or sale tag (e.g. "Now $1.33, was $5.98, You save $4.65") with no coupon, rebate, cashback, or reward involved.
- The image contains no recognizable deal, coupon, promotion, or offer.
- The image is unrelated to shopping deals.
- The image is too blurry, cropped, damaged, or unreadable to reliably extract a deal.
- The image contains only product photography without a recognizable price, coupon, discount, or promotional offer.
- The relevant deal information is too incomplete to identify a usable offer.
- The image is a logo, decorative graphic, unrelated screenshot, or general advertisement without an identifiable deal.
Do not return a JSON object containing empty fields when no valid deal is found.
If a valid deal is present, return one JSON object following the schema below.
3. WHOLE-IMAGE DEAL AND BUNDLE RULES
Determine whether the image represents one combined transaction or multiple independent offers.
Treat the entire image as ONE deal when:
- Multiple products are shown together as one shopping basket or transaction.
- The image displays a shared subtotal, coupon savings, and final net cost.
- Several individual coupons contribute to one combined shopping scenario.
- The image presents a single bundle, spend threshold, or final-price calculation.
- The products are clearly intended to be purchased together to achieve the advertised savings.
When these conditions apply:
- Create one deal object for the entire transaction.
- Include every participating product in the `items` field.
- Reflect all relevant coupons in the `coupons_used` total.
- Include the combined subtotal and final net cost.
- Explain the complete shopping scenario in `strategy`.
- Do not split individual products or coupons into separate deal objects.
Treat offers as separate deals only when the image clearly presents independent transactions, standalone promotions, or unrelated scenarios with distinct pricing and savings.
If the image shows multiple independent deals, prioritize the main featured deal. Include additional deals only when they are clearly separate and independently understandable. Return a JSON array only if the image genuinely contains multiple independent deals. Otherwise, return a single JSON object.
4. DEDUPLICATION AND COMPLETENESS
- Extract every relevant detail visible in the image.
- Do not omit participating products, coupon amounts, minimum spending requirements, qualifying quantities, restrictions, dates, or final prices.
- If a coupon is shown more than once, list it only once unless the image clearly indicates multiple separate applications.
- Do not treat a product's displayed price as a coupon.
- Do not treat a promotional banner as a separate deal if it belongs to the same transaction.
- Do not create separate deal entries for individual products that contribute to a shared basket total.
- Do not add products or offers that are not visible or clearly described in the image.
5. REQUIRED JSON OUTPUT SCHEMA
For a valid deal, return the following fields:
{
"name": "",
"summary": "",
"items": [],
"subtotal": "",
"coupons_used": "",
"rewards": "",
"rebates": "",
"final_net_cost": "",
"validity_date": "",
"strategy": []
}
Use exactly these field names and data types. The deal models a complete transaction: the items' regular prices combine into the subtotal, and the savings come from manufacturer digital coupons, store coupons, instant discounts, rewards, and rebates.
name
- Create a concise, descriptive name for the complete deal.
- For a single product, use Brand + Product Type.
- For a bundle, use a descriptive name that identifies the products or shopping scenario.
- For a spend-and-save promotion, use a name that identifies the promotion.
- Do not include a couponer's name, username, social media handle, or creator identity.
- Do not prepend the retailer name to a single product name unless it is necessary to identify the deal.
summary
- Write an original product/deal summary of approximately 50 words.
- Target exactly 50 words whenever possible.
- Make it suitable for a listicle-style blog post.
- Naturally describe the products, their general purpose, the shopping opportunity, and the value of the offer.
- For a bundle, mention the major participating products.
- Use fresh wording; do not copy sentences from the image or source text.
- Do not mention any couponer, YouTuber, content creator, influencer, username, or social media account.
- Do not invent product benefits, product specifications, savings, coupons, or promotional terms.
- Avoid repeating the detailed coupon math unnecessarily.
items
- Return an array of strings.
- List every product included in the deal.
- Use the full Brand + Product Type name for each product.
- Include quantities where applicable.
- If a product's price is CLEARLY readable in the image, append it to that item using the format "quantity × Product Name — $X.XX" (e.g. "1 × all Free Clear Liquid Laundry Detergent — $4.00"). For a quantity with a per-unit price, use "— $4.00 each".
- If a product's price is NOT clearly readable, list that item WITHOUT a price — never guess, estimate, or invent a price. It is correct for some items to have prices and others not.
- Do not use a brand name alone when a recognizable product type is available.
- Do not add sizes, flavors, scents, or packaging details to a product name unless necessary to distinguish a qualifying product. Such details may be included in the description when relevant.
- For bundles, list all participating products clearly.
subtotal
- The combined REGULAR price of every item in the transaction before any savings — the pre-coupon total (e.g. "$14.88").
- Use the image's stated subtotal when shown; otherwise sum the clearly readable item prices.
- If no subtotal can be established, use an empty string.
coupons_used
- The TOTAL money value of all coupons applied to the transaction, as ONE string (e.g. "$3.00").
- Combine manufacturer digital coupons, store coupons, paper coupons, and instant discounts into this one total.
- If no coupon is used, return "$0.00".
- Do not include rewards or rebates here — those have their own fields.
rewards
- The total money value of rewards involved in the transaction (register rewards, loyalty points, gift-card offers), as ONE string (e.g. "$2.00").
- Include a reward only when it is actually shown or explicitly described in the image.
- If there is no reward, return "$0.00".
rebates
- The total money value of rebates or cashback involved in the transaction (app rebates, mail-in rebates, reimbursement offers), as ONE string (e.g. "$14.88").
- Include a rebate only when it is actually shown or explicitly described in the image.
- If there is no rebate, return "$0.00".
final_net_cost
- The final NET cost after coupons, rewards, and rebates are applied — what the shopper actually pays (or gets back), e.g. "$0.00", "FREE ($0.00)", "$10.75 + tax".
- Use the source's stated final amount when available; preserve "FREE" wording and any "+ tax" qualification exactly as shown.
- If calculating it, subtract the applicable coupons, rewards, and rebates from the subtotal.
- If the final net cost cannot be established reliably, use an empty string.
- Never invent additional savings to make a deal appear cheaper.
validity_date
- The date or date range the deal is valid, as ONE string (e.g. "9/26 only", "6/28 – 7/4", "Valid through 7/31").
- Extract it exactly as the image presents it — preserve the original month/day notation, "ONLY" restrictions, and day-of-week callouts.
- If the image shows a start and end date, join them with " – " (e.g. "6/28 – 7/4").
- If no date is shown, return an empty string. Never guess or assume a year.
strategy
- Return an ordered array of actionable strings.
- Explain how to complete the deal from beginning to end.
- Identify every product using its full Brand + Product Type name.
- Include quantities and product prices when shown.
- Explain which coupons to clip or use and their requirements.
- Include the subtotal, the coupons used, any rewards or rebate steps, and the final net cost when known.
- Write every step as a direct instruction to the shopper, stating the facts plainly (prices, coupon amounts, totals).
- Never mention the image, the source, or the extraction process in any step. Never say that something was "stated", "shown", "advertised", "pictured", or "documented", and never add verification disclaimers such as "cannot be independently verified".
- Do not repeat the same instruction unnecessarily.
- Do not add unverified steps, prices, savings, or promotional requirements.
6. PRODUCT NAME AND BRAND RULES
Whenever referencing a product in `items` or `strategy`:
- Use the full Brand + Product Type name when identifiable.
- Do not refer to a product by brand alone if the product type is visible or readable.
- Do not prepend the retailer name to every product.
- Keep the overall deal name concise and descriptive.
- Preserve the distinction between similar products if the image specifies different varieties or product categories.
Examples:
- Correct: "Tide Liquid Laundry Detergent"
- Incorrect: "Tide"
- Correct: "Mr. Clean Multi-Surface Cleaner"
- Incorrect: "Mr. Clean"
- Correct: "Snuggle Liquid Fabric Softener"
- Incorrect: "Snuggle"
7. PRICING AND MATH VALIDATION
Accuracy is more important than making a deal look attractive.
- Check all visible product prices and quantities.
- Check whether the subtotal matches the listed items' prices.
- Check whether the coupons_used total matches the visible coupons when possible.
- Check whether the final net cost agrees with the subtotal minus the coupons, rewards, and rebates.
- Account for spending thresholds and coupon eligibility.
- Do not assume every coupon can be stacked with every other coupon.
- Do not assume a coupon applies to every item in a bundle.
- Do not count the same savings twice.
- Do not count a future reward or rebate as an immediate checkout discount — rewards and rebates lower the NET cost after payment.
- Do not silently change the source's displayed total or final payment.
If the image explicitly states a final net cost but the visible coupon, reward, and rebate amounts do not reconcile with it, preserve the stated final amount and accurately report the visible savings. Do not invent an extra coupon or discount to force the math to balance.
If a calculated amount conflicts with a clearly displayed amount, preserve the displayed amount and avoid presenting the calculation as verified.
Perform all of these checks silently before writing your answer. The output must never mention the image, the source, or the validation process — state the prices, coupons, and totals directly.
8. DATES AND EXPIRATION
- Extract any deal date, expiration date, date range, or day-specific restriction shown in the image into `validity_date`.
- Preserve the original month/day notation when practical.
- If the image says "ONLY", preserve the restriction.
- Do not assume a year unless it is explicitly provided or unambiguously established by the image.
- Do not treat a date embedded in a decorative element as a valid deal date unless the context connects it to the promotion.
- Do not assume an old offer is still valid today.
9. ORIGINALITY AND CONTENT RESTRICTIONS
- Never mention the couponer's name, username, signature, watermark, social media handle, channel, or creator identity in any output field.
- Never reference the image, the source, or the extraction process in any output field. Do not use phrases such as "the image shows", "the image states", "as shown", "pictured", "advertised", "stated", "documented", or "cannot be verified". State every fact directly and confidently.
- Do not copy promotional descriptions verbatim.
- Write a fresh summary suitable for publication.
- Do not invent product specifications, coupon requirements, prices, availability, or discounts.
- Do not add unsupported claims such as "best deal", "lowest price ever", or "guaranteed free".
- Do not claim a product is free unless the documented final net cost supports that claim.
- Avoid unnecessary promotional exaggeration.
- Do not include commentary outside the requested JSON output.
10. OUTPUT VALIDATION
Before returning your response:
Determine whether a valid deal is present.
Identify whether the image represents one combined transaction or multiple independent deals.
Extract all readable products, prices, coupons, rewards, rebates, dates, restrictions, and savings.
Verify the arithmetic where possible.
Write an original summary of approximately 50 words.
Ensure every product reference uses a clear product name.
Ensure `coupons_used`, `rewards`, and `rebates` are money strings ("$0.00" when none).
Ensure `validity_date` is the image's stated validity date or range, or an empty string when none is shown.
Ensure no output field references the image, the source, or the verification process — every fact is stated directly.
Ensure the JSON is valid and uses the required field names and data types.
Remove all couponer and creator names.
Return only the result, without Markdown fences, explanations, or extra commentary.
If no valid deal can be identified, return exactly:
false
If one valid deal is identified, return one JSON object.
If multiple clearly independent deals are identified, return a JSON array of deal objects, using the same schema for each object."""


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
