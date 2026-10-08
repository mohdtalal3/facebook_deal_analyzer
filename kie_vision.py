#!/usr/bin/env python3
"""
KIE AI coupon-deal extraction (GPT-5.6 Luna via the /codex/v1/responses
endpoint).

Uploads a local (processed) image to get a public URL — reusing KIE's own
file-stream-upload endpoint, the same one this project already used for
image generation — then sends that URL to GPT-5.6 Luna to extract the
complete coupon deal shown in the image (replaced the earlier
brand/product/category extraction, and the Gemini Flash model this
extraction used immediately before).
"""

import json
import os
import re
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from constants import KIE_MAX_REQUESTS_PER_WINDOW, KIE_RATE_WINDOW_SECONDS

load_dotenv()

KIE_API_KEY = os.getenv("KIE_API_KEY", "")
UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
RESPONSES_URL = "https://api.kie.ai/codex/v1/responses"

HEADERS_AUTH = {"Authorization": f"Bearer {KIE_API_KEY}"}

# ── Rate limiter ──
# KIE allows up to 20 new generation/analysis requests per 10s PER ACCOUNT.
# The pipeline runs as ONE job at a time (analysis and image generation happen
# in sequence inside the same run_facebook.py subprocess), so a thread-safe
# in-process limiter is enough — the per-post analysis threads and the AI
# generation threads all draw from the same process-wide window.


class RateLimiter:
    """Thread-safe in-process rate limiter."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.lock = threading.Lock()
        self.timestamps: list[float] = []

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.timestamps = [t for t in self.timestamps if now - t < self.period]
                if len(self.timestamps) < self.max_calls:
                    self.timestamps.append(now)
                    return
            time.sleep(0.2)


rate_limiter = RateLimiter(KIE_MAX_REQUESTS_PER_WINDOW, KIE_RATE_WINDOW_SECONDS)

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
- A product with a sale price.
- A digital or paper coupon.
- A discount or promotional offer.
- A multi-product bundle with a combined price.
- A spend-and-save promotion.
- A product combination with a final cost.
- A rebate, cashback offer, reward, or other documented savings opportunity.
- A product offer with enough pricing or promotional information to identify the deal.
Return the Boolean value `false` if:
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
- The image displays a shared original total, coupon savings, and final price.
- Several individual coupons contribute to one combined shopping scenario.
- The image presents a single bundle, spend threshold, or final-price calculation.
- The products are clearly intended to be purchased together to achieve the advertised savings.
When these conditions apply:
- Create one deal object for the entire transaction.
- Include every participating product in the `items` field.
- Include all relevant coupons in `coupons_to_use`.
- Include the combined transaction amount and final cost.
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
"items": "",
"sale": "",
"price": "",
"coupons_to_use": [],
"receive": "",
"final_cost": "",
"availability": "",
"pro_tip": "",
"strategy": []
}
Use exactly these field names and data types.
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
- List every product included in the deal.
- Use the full Brand + Product Type name for each product.
- Include quantities where applicable.
# - If a product's price is CLEARLY readable in the image, append it to that item using the format "quantity × Product Name — $X.XX" (e.g. "1 × all Free Clear Liquid Laundry Detergent — $4.00"). For a quantity with a per-unit price, use "— $4.00 each".
# - If a product's price is NOT clearly readable, list that item WITHOUT a price — never guess, estimate, or invent a price. It is correct for some items to have prices and others not.
- Do not use a brand name alone when a recognizable product type is available.
- Do not add sizes, flavors, scents, or packaging details to a product name unless necessary to distinguish a qualifying product. Such details may be included in the description when relevant.
- For bundles, list all participating products clearly.
sale
- Describe the sale, discount, promotion, or purchase requirement.
- Include the required quantity, qualifying purchase threshold, or bundle terms when shown.
- If the image shows a combined shopping scenario, describe it as one transaction.
- Do not invent a sale price or sale condition.
price
- State the ORIGINAL total price of the deal before any savings — the pre-coupon total for a bundle or combined transaction, or the original/advertised price for a single product.
- If no original price is available, use an empty string.
- Do not include the after-coupon cost here — that belongs in final_cost.
coupons_to_use
- Return an array of strings.
- Include every applicable coupon visibly shown in the image.
- State the coupon amount, eligible products, quantity requirement, spending threshold, and relevant exclusions when readable.
- Include store coupons, digital manufacturer coupons, paper coupons, instant discounts, rebates, and rewards only when actually shown or explicitly described.
- For a shared transaction, include all coupons contributing to that transaction in this array.
- Do not invent missing coupon amounts or requirements.
- If no coupon is shown, return an empty array.
- Do not automatically treat a promotion as a coupon if it is simply a sale price.
receive
- Describe the products, quantities, rewards, or other benefits received from the transaction.
- For a bundle, include every participating product.
- Mention a Register Reward, cashback, rebate, or other benefit only when explicitly shown.
- Distinguish products received immediately from rewards received later.
final_cost
- State the final amount paid after the applicable discounts and coupons — the checkout cost.
- Preserve any explicitly shown tax qualification, such as "+ tax".
- Use the source's stated final cost when available and identifiable.
- If calculating the final cost, subtract only the savings that are applicable to the transaction.
- If a future reward or rebate is involved, state the checkout amount and explain that the reward lowers the net cost later.
- If the final cost cannot be established reliably, use an empty string.
- Never invent additional savings to make a deal appear cheaper.
availability
- Include the retailer or brand context using `{brand_name}` when appropriate.
- If the image specifies a date, preserve the date exactly as shown.
- If a specific date is emphasized with wording such as "ONLY", preserve that restriction.
- If the image gives a date range, include the complete range.
- Include any visible store, online, in-store, regional, or product eligibility restrictions.
- Do not infer current availability from an old promotional image.
- Do not invent availability information.
- If no date or availability restriction is shown, use the brand/retailer name only when appropriate.
- If availability cannot be established, use an empty string.
Examples:
- "{brand_name}"
- "{brand_name} — 9/26 only"
- "{brand_name} — 9/26 through 9/28"
- "{brand_name} — in-store only"
- "{brand_name} — online only"
These are examples of formatting, not assumptions about the actual offer.
pro_tip
- Provide one short, useful shopping tip grounded in the image.
- Appropriate tips may include clipping a visible digital coupon before checkout, checking the required quantity, meeting a spending threshold, or verifying a visible product exclusion.
- Do not invent coupon stacking rules, rebates, availability, or savings.
- Do not make unsupported claims about coupon policies.
strategy
- Return an ordered array of actionable strings.
- Explain how to complete the deal from beginning to end.
- Identify every product using its full Brand + Product Type name.
- Include quantities and product prices when shown.
- Explain which coupons to use and their requirements.
- Include the pre-coupon total, documented discounts, checkout payment, and final net cost when known.
- Include rebate or reward redemption steps only when shown or explicitly described.
- For a bundle, present the math and combined transaction summary as the final steps.
- Write every step as a direct instruction to the shopper, stating the facts plainly (prices, coupon amounts, totals).
- Never mention the image, the source, or the extraction process in any step. Never say that something was "stated", "shown", "advertised", "pictured", or "documented", and never add verification disclaimers such as "cannot be independently verified".
- Do not repeat the same instruction unnecessarily.
- Do not add unverified steps, prices, savings, or promotional requirements.
6. PRODUCT NAME AND BRAND RULES
Whenever referencing a product in `items`, `receive`, or `strategy`:
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
- Calculate product subtotals when the information supports the calculation.
- Check whether the displayed total matches the listed items.
- Check whether the displayed coupon savings match the visible coupons when possible.
- Check whether the checkout amount agrees with the displayed total and documented discounts.
- Account for spending thresholds and coupon eligibility.
- Do not assume every coupon can be stacked with every other coupon.
- Do not assume a coupon applies to every item in a bundle.
- Do not count the same savings twice.
- Do not count a future reward as an immediate checkout discount.
- Do not silently change the source's displayed total or final payment.
If the image explicitly states a final total but the visible coupon amounts do not reconcile with it, preserve the stated final amount and accurately describe the visible coupons. Do not invent an extra coupon or discount to force the math to balance.
If a calculated amount conflicts with a clearly displayed amount, preserve the displayed amount and avoid presenting the calculation as verified.
Perform all of these checks silently before writing your answer. The output must never mention the image, the source, or the validation process — state the prices, coupons, and totals directly.
8. DATES AND EXPIRATION
- Extract any deal date, expiration date, date range, or day-specific restriction shown in the image.
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
- Do not claim a product is free unless the documented final cost supports that claim.
- Avoid unnecessary promotional exaggeration.
- Do not include commentary outside the requested JSON output.
10. OUTPUT VALIDATION
Before returning your response:
Determine whether a valid deal is present.
Identify whether the image represents one combined transaction or multiple independent deals.
Extract all readable products, prices, coupons, dates, restrictions, and savings.
Verify the arithmetic where possible.
Write an original summary of approximately 50 words.
Ensure every product reference uses a clear product name.
Ensure availability includes any explicit date restrictions.
Ensure no output field references the image, the source, or the verification process — every fact is stated directly.
Ensure the JSON is valid and uses the required field names and data types.
Remove all couponer and creator names.
Return only the result, without Markdown fences, explanations, or extra commentary.
If no valid deal can be identified, return exactly:
false
If one valid deal is identified, return one JSON object.
If multiple clearly independent deals are identified, return a JSON array of deal objects, using the same schema for each object."""


class KieAnalysisError(Exception):
    pass


def upload_image(file_path: str, upload_path: str = "fb_product_images", mime: str = "image/jpeg",
                 max_retries: int = 3) -> str:
    """Upload a local image to KIE's file-stream-upload endpoint and return
    its public URL. Single shared implementation for both the analysis path
    (this module) and the AI-generation path (generate.py delegates here).

    Paced through the shared account-wide rate limiter and retried up to
    max_retries times with backoff — a transient upload failure no longer
    fails the image outright."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            rate_limiter.acquire()
            with open(file_path, "rb") as f:
                response = requests.post(
                    UPLOAD_URL,
                    headers=HEADERS_AUTH,
                    files={"file": (os.path.basename(file_path), f, mime)},
                    data={"uploadPath": upload_path},
                    timeout=60,
                )
            response.raise_for_status()
            data = response.json()
            if not data.get("success"):
                raise KieAnalysisError(f"Upload failed: {data}")
            file_data = data["data"]
            url = file_data.get("fileUrl") or file_data.get("url") or file_data.get("downloadUrl")
            if not url:
                raise KieAnalysisError(f"Could not find URL in upload response: {file_data}")
            return url
        except Exception as e:
            last_error = e
            print(f"  ⚠️ Upload attempt {attempt}/{max_retries} failed for "
                  f"{os.path.basename(file_path)}: {e}")
            if attempt < max_retries:
                time.sleep(2 * attempt)
    raise last_error


def _extract_response_text(data: dict) -> str | None:
    """Extract the model's text reply from the /codex/v1/responses response
    (output[].content[].output_text), falling back to the chat-completions
    shape and then a recursive scan."""
    # Responses API shape: {"output": [{"type": "message", "content":
    # [{"type": "output_text", "text": ...}, ...]}, ...]}
    try:
        parts = []
        for item in data["output"]:
            if not isinstance(item, dict) or item.get("type") not in (None, "message"):
                continue
            content = item.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text") and c.get("text"):
                        parts.append(c["text"])
        if parts:
            return "".join(parts)
    except (KeyError, TypeError):
        pass

    # Chat-completions shape: {"choices": [{"message": {"content": ...}}]}
    try:
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            for c in content:
                if c.get("type") in ("text", "output_text") and c.get("text"):
                    return c["text"]
    except (KeyError, IndexError, TypeError):
        pass

    # Fallback: scan every string value for something that looks like our JSON
    def _scan(node):
        if isinstance(node, str):
            if '"name"' in node or "'name'" in node:
                return node
            return None
        if isinstance(node, dict):
            for v in node.values():
                found = _scan(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = _scan(v)
                if found:
                    return found
        return None

    return _scan(data)


def _parse_deal_response(text: str) -> list[dict]:
    """Parse the model's reply into a list of deal objects.

    The prompt allows exactly three shapes: the bare word `false` (no valid
    deal), a single JSON deal object, or a JSON array of deal objects.
    Markdown fences are tolerated. Raises KieAnalysisError on anything else.
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
        raise KieAnalysisError(f"Could not parse deal JSON from model reply: {e}") from e
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [deal for deal in parsed if isinstance(deal, dict)]
    raise KieAnalysisError(f"Unexpected deal response type: {type(parsed).__name__}")


def analyze_deal_image(image_url: str, brand_name: str, timeout: int = 600) -> dict:
    """Call KIE GPT-5.6 Luna to extract the coupon deal(s) from an image,
    using the workspace's retailer brand as the deal context.

    Returns {"deals": [deal, ...], "tokens_used": ..., "credits_consumed":
    ...} — "deals" is empty when the model replied `false` (no valid deal).
    Raises KieAnalysisError on any failure — caller is responsible for
    recording a failed-analysis placeholder instead of losing the post.
    """
    prompt = DEAL_EXTRACTION_PROMPT.replace("{brand_name}", brand_name or "")
    payload = {
        "model": "gpt-5-6-luna",
        "stream": False,  # endpoint defaults to SSE streaming — we want one JSON response back
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        "reasoning": {"effort": "medium"},
    }

    # Retry loop: 429 (rate limit — KIE rejects, does not queue, requests
    # over the account's 20-per-10s cap) and 5xx (server errors — 500/501/
    # 502/503 etc.) both get up to 3 attempts. 429 backs off a full rate
    # window per attempt; 5xx backs off briefly (server-side hiccups usually
    # clear in seconds).
    response = None
    for attempt in range(1, 4):
        rate_limiter.acquire()
        response = requests.post(
            RESPONSES_URL,
            headers={**HEADERS_AUTH, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
       # print(response.json())
        status = response.status_code
        if status == 429:
            wait = KIE_RATE_WINDOW_SECONDS * attempt
            print(f"  ⚠️ KIE rate limit hit (429), attempt {attempt}/3 — waiting {wait}s")
        elif status >= 500:
            wait = 5 * attempt
            print(f"  ⚠️ KIE server error ({status}), attempt {attempt}/3 — waiting {wait}s")
        else:
            break
        if attempt < 3:
            time.sleep(wait)
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    credits_consumed = data.get("credits_consumed")
    print(f"  🔢 Tokens — input: {usage.get('input_tokens')}, output: {usage.get('output_tokens')}, "
          f"total: {usage.get('total_tokens')} | credits consumed: {credits_consumed}")

    text = _extract_response_text(data)
    if not text:
        raise KieAnalysisError(f"Could not find text content in KIE response: {data}")

    deals = _parse_deal_response(text)

    return {
        "deals": deals,
        "tokens_used": usage.get("total_tokens"),
        "credits_consumed": credits_consumed,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 kie_vision.py <path_to_image> [brand_name]")
        raise SystemExit(1)

    image_path = sys.argv[1]
    brand_name = sys.argv[2] if len(sys.argv) > 2 else ""

    print(f"Uploading {image_path}...")
    public_url = upload_image(image_path)
    print(f"Public URL: {public_url}")

    print("Analyzing...")
    result = analyze_deal_image(public_url, brand_name)
    print(json.dumps(result, indent=2, ensure_ascii=False))
