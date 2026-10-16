import json
import os
import httpx
from typing import Optional
from openai import OpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

XAI_API_KEY = os.getenv("XAI_API_KEY")

# ── SCHEMA ───────────────────────────────────

class CouponDeal(BaseModel):
    name: str = Field(description="Concise and descriptive: Brand + Product Type for a single product; a name identifying the products or the shopping scenario for a bundle or promotion. Never include a couponer's name, username, or any creator identity; do not prepend the retailer name unless necessary to identify the deal.")
    summary: str = Field(description="Original, approximately 50 words, listicle-ready: describe the products and their general purpose, the shopping opportunity, and the value of the offer; mention the major products for a bundle. Use fresh wording — never copy sentences from the transcript, never mention any couponer, YouTuber, influencer, or social media account, never invent product benefits, specifications, savings, or coupon terms, and avoid repeating the detailed coupon math.")
    items: list[str] = Field(description="Array of strings listing every product in the deal, each as full Brand + Product Type (never brand alone when the product type is stated) with quantities where applicable. If a price is CLEARLY stated, append it: '1 × all Free Clear Liquid Laundry Detergent — $4.00' (per-unit price: '— $4.00 each'); if not clearly stated, list the item WITHOUT a price — never guess or estimate. It is correct for some items to have prices and others not. Do not add sizes, flavors, scents, or packaging details unless needed to identify the qualifying product.")
    subtotal: str = Field(description="The TOTAL price of all items in the transaction BEFORE any coupons, rewards, or rebates are applied (e.g. '$14.88') — what the products ring up at, including any sale or clearance pricing (NOT the original regular price). Use the subtotal stated in the video, or the sum of the clearly stated item prices at their displayed prices. If item prices are stated, you MUST provide the subtotal (their sum) — never leave it empty when item prices are available; empty string only when no prices are stated at all.")
    coupons_used: str = Field(description="The TOTAL value of all coupons applied to the transaction (manufacturer digital, store, paper, and instant discounts) as ONE string (e.g. '$3.00'); '$0.00' if none. Never include rewards or rebates here, and never treat a product's displayed price as a coupon.")
    rewards: str = Field(description="The TOTAL value of register rewards, loyalty points, or gift-card offers earned by PURCHASING the products, as ONE money string (e.g. '$2.00'); '$0.00' if none. Never count referral, sign-up, or app-download bonuses here. If a reward is stated in points without a dollar value (e.g. '1,250 points'), return '$0.00' and state the points requirement in `strategy` instead.")
    rebates: str = Field(description="The TOTAL value of rebates or cashback (app rebates, mail-in rebates, reimbursement offers) as ONE string (e.g. '$14.88'); '$0.00' if none.")
    final_net_cost: str = Field(description="What the shopper actually pays (or gets back) after coupons, rewards, and rebates (e.g. '$0.00', 'FREE ($0.00)', '$10.75 + tax'). Use the stated final amount when shown, preserving 'FREE' wording and any '+ tax' qualification exactly; otherwise subtract the applicable coupons, rewards, and rebates from the subtotal; empty string if it cannot be established reliably. Never invent additional savings to make a deal appear cheaper.")
    validity_date: str = Field(description="The deal's date or date range exactly as the video presents it ('9/26 only', '6/28 – 7/4', 'Valid through 7/31'), preserving the original month/day notation, 'ONLY' restrictions, and day-of-week callouts; join a start and end date with ' – '. Empty string if none is stated; never assume a year, and ignore a date unless the context connects it to the promotion.")
    strategy: list[str] = Field(description="An ordered array of direct, actionable steps covering the deal from start to finish: the products (full Brand + Product Type names, quantities, prices when stated), which coupons to clip and their requirements, the subtotal, the coupons used, any reward/rebate steps, and the final net cost when known. State every fact plainly as an instruction to the shopper; never mention the video, the transcript, the source, or the extraction process; never repeat a step; never add unverified steps, prices, savings, or requirements.")

class CouponDealList(BaseModel):
    deals: list[CouponDeal]


def extract_coupons(
    transcript_file: str,
    video_id: str,
    output_file: str = "coupons.json",
    previous_response_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> tuple[str, str]:
    """Extract coupon deals from a transcript.

    Returns (output_file, response_id) so the caller can chain calls.
    """
    with open(transcript_file, "r", encoding="utf-8") as f:
        segments = json.load(f)

    transcript_text = "\n".join(
        f"[{s['start']}s] {s['text']}" for s in segments
    )

    COUPON_SYSTEM_PROMPT = """You are an expert coupon deal extraction assistant analyzing a YouTube video transcript in which a couponer walks through store deals, coupons, rebates, and shopping scenarios.

YOUR GOAL: Extract EVERY SINGLE coupon deal mentioned, described, or referenced in the video. Do NOT skip any deal.

━━━ VALID DEAL DETECTION ━━━
A valid deal MUST be tied to purchasing specific retail products in a store transaction: real items, real prices, and something for the shopper to clip, submit, activate, or qualify for (a coupon, rebate, cashback, or register/loyalty reward). Valid examples: a digital or paper coupon on specific products; an offer unlocked by clipping, a spend threshold, or a qualifying quantity; a multi-product bundle with a combined price; a spend-and-save promotion; a rebate or cashback on purchased products.
Do NOT extract:
- Only a price reduction ("was $X, now $Y / you save $Z", sale, markdown, clearance) with NO coupon, rebate, cashback, or reward attached.
- Referral programs, referral codes, sign-up/first-receipt/app-download bonuses, or invite-a-friend offers (e.g. Fetch, Ibotta, Aisle) instead of a product purchase — even if a dollar amount or the word "reward" is mentioned.
- Promotions where the ONLY incentive is loyalty or reward POINTS (e.g. "buy 2, earn 1,250 points") with no dollar coupon, rebate, cashback, or register reward attached.
- General store chatter, unboxing, or haul talk with no recognizable deal.

━━━ DEDUPLICATION RULES ━━━
- Do NOT extract any deal already extracted in a previous response in this session.
- Only return deals that are NEW and not yet in any previous response.

━━━ ONE DEAL OR SEVERAL ━━━
Treat products as ONE deal when they form one basket or transaction: a shared subtotal, several coupons contributing to one scenario, a single bundle or spend threshold, or products clearly meant to be bought together. Put every participating product in `items` and every coupon in `coupons_used`; never split them into separate deal objects.
Return separate deal objects only for genuinely independent transactions or promotions with distinct pricing and savings — otherwise return a single deal object, prioritizing the main featured deal. If a deal is mentioned multiple times, extract it ONCE.

━━━ EXTRACTION RULES ━━━
- Extract ALL deals: main featured deals, quick mentions, combo deals.
- Read 3–4 segments BEFORE and AFTER to capture the full deal context (price, coupon steps, rebates).

━━━ OUTPUT SCHEMA ━━━
For each valid deal, return exactly these fields (the items' stated prices combine into the subtotal; savings come from coupons, rewards, and rebates):
{"name": "", "summary": "", "items": [], "subtotal": "", "coupons_used": "", "rewards": "", "rebates": "", "final_net_cost": "", "validity_date": "", "strategy": []}
name — concise and descriptive: Brand + Product Type for a single product; a name identifying the products or the shopping scenario for a bundle or promotion. Never include a couponer's name, username, or any creator identity; do not prepend the retailer name unless necessary to identify the deal.
summary — original, approximately 50 words, listicle-ready: describe the products and their general purpose, the shopping opportunity, and the value of the offer; mention the major products for a bundle. Use fresh wording — never copy sentences from the transcript, never mention any couponer, YouTuber, influencer, or social media account, never invent product benefits, specifications, savings, or coupon terms, and avoid repeating the detailed coupon math.
items — array of strings listing every product in the deal, each as full Brand + Product Type (never brand alone when the product type is stated) with quantities where applicable. If a price is CLEARLY stated, append it: "1 × all Free Clear Liquid Laundry Detergent — $4.00" (per-unit price: "— $4.00 each"); if not clearly stated, list the item WITHOUT a price — never guess or estimate. It is correct for some items to have prices and others not. Do not add sizes, flavors, scents, or packaging details unless needed to identify the qualifying product.
subtotal — the TOTAL price of all items in the transaction BEFORE any coupons, rewards, or rebates are applied (e.g. "$14.88") — what the products ring up at, including any sale or clearance pricing (NOT the original regular price). Use the subtotal stated in the video, or the sum of the clearly stated item prices at their displayed prices. If item prices are stated, you MUST provide the subtotal (their sum) — never leave it empty when item prices are available; empty string only when no prices are stated at all.
coupons_used — the TOTAL value of all coupons applied to the transaction (manufacturer digital, store, paper, and instant discounts) as ONE string (e.g. "$3.00"); "$0.00" if none. Never include rewards or rebates here, and never treat a product's displayed price as a coupon.
rewards — the TOTAL value of register rewards, loyalty points, or gift-card offers earned by PURCHASING the products, as ONE money string (e.g. "$2.00"); "$0.00" if none. Never count referral, sign-up, or app-download bonuses here. If a reward is stated in points without a dollar value (e.g. "1,250 points"), return "$0.00" and state the points requirement in `strategy` instead.
rebates — the TOTAL value of rebates or cashback (app rebates, mail-in rebates, reimbursement offers) as ONE string (e.g. "$14.88"); "$0.00" if none.
final_net_cost — what the shopper actually pays (or gets back) after coupons, rewards, and rebates (e.g. "$0.00", "FREE ($0.00)", "$10.75 + tax"). Use the stated final amount when shown, preserving "FREE" wording and any "+ tax" qualification exactly; otherwise subtract the applicable coupons, rewards, and rebates from the subtotal; empty string if it cannot be established reliably. Never invent additional savings to make a deal appear cheaper.
validity_date — the deal's date or date range exactly as the video presents it ("9/26 only", "6/28 – 7/4", "Valid through 7/31"), preserving the original month/day notation, "ONLY" restrictions, and day-of-week callouts; join a start and end date with " – ". Empty string if none is stated; never assume a year, and ignore a date unless the context connects it to the promotion.
strategy — an ordered array of direct, actionable steps covering the deal from start to finish: the products (full Brand + Product Type names, quantities, prices when stated), which coupons to clip and their requirements, the subtotal, the coupons used, any reward/rebate steps, and the final net cost when known. State every fact plainly as an instruction to the shopper; never mention the video, the transcript, the source, or the extraction process; never repeat a step; never add unverified steps, prices, savings, or requirements.

━━━ PRODUCT NAMING ━━━
In `items` and `strategy`, always use the full Brand + Product Type and preserve distinctions between varieties the couponer specifies: "Tide Liquid Laundry Detergent", not "Tide"; "Mr. Clean Multi-Surface Cleaner", not "Mr. Clean"; "Snuggle Liquid Fabric Softener", not "Snuggle".

━━━ MATH VALIDATION (perform silently — the output must never mention these checks) ━━━
Accuracy matters more than making the deal look attractive. Verify prices and quantities; that the subtotal matches the listed items; that `coupons_used` matches the coupons described; and that `final_net_cost` agrees with subtotal minus coupons, rewards, and rebates. Account for spending thresholds and coupon eligibility; do not assume coupons stack or apply to every item in a bundle; do not count the same savings twice; do not treat a future reward or rebate as an immediate checkout discount. If a stated total conflicts with the calculated one, preserve the stated amount and report the visible savings accurately — never invent an extra coupon or discount to force the math to balance.

━━━ OUTPUT RULES ━━━
- State every fact directly and confidently. In NO field mention the video, the transcript, the source, or the extraction process ("the video shows", "as mentioned", "the couponer says", "stated", "documented", "cannot be verified"), and never include a couponer's name, username, or social media handle.
- Do not copy the couponer's phrasing verbatim; do not add unsupported claims ("best deal", "lowest price ever", "guaranteed free"); do not claim a product is free unless the final net cost supports it."""

    USER_PROMPT = f"""VIDEO_ID: {video_id}

Transcript (format: [seconds] text):
{transcript_text}

REMINDER: Extract EVERY coupon deal that has NOT already been extracted from a previous video in this session. Check the full transcript carefully — deals are often spread across multiple segments."""

    client = OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1",
        timeout=httpx.Timeout(3600.0, connect=30.0),
    )

    request_kwargs = dict(
        model="grok-4.5",
        max_output_tokens=500000,
        reasoning={
            "effort": "medium"
        },
        store=True,
        text={
            "format": {
                "type": "json_schema",
                "name": "CouponDealList",
                "schema": CouponDealList.model_json_schema(),
                "strict": True,
            }
        },
    )

    if previous_response_id:
        request_kwargs["previous_response_id"] = previous_response_id
        request_kwargs["input"] = [{"role": "user", "content": USER_PROMPT}]
    else:
        request_kwargs["input"] = [
            {"role": "system", "content": COUPON_SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ]

    response_text = ""
    response_id = None
    with client.responses.stream(**request_kwargs) as stream:
        for event in stream:
            delta = getattr(event, "delta", None)
            if delta:
                print(delta, end="", flush=True)
        print()
        final = stream.get_final_response()
        response_text = final.output_text
        response_id = final.id

    data = json.loads(response_text)
    deals = [CouponDeal(**d).model_dump() for d in data["deals"]]

    if limit:
        deals = deals[:limit]

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)

    print(f"  Extracted {len(deals)} coupon deals → {output_file}")
    return output_file, response_id


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python -m youtube.deal_extract <transcript.json> <video_id> [output_file]")
        sys.exit(1)

    transcript_file = sys.argv[1]
    video_id = sys.argv[2]
    output_file = sys.argv[3] if len(sys.argv) > 3 else "coupons.json"
    extract_coupons(transcript_file, video_id, output_file=output_file)
