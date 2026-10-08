#!/usr/bin/env python3
"""
Central place for tunable pipeline constants and testing toggles.

Actual secrets (API keys, tokens) stay in .env — this file is for behavior
knobs that used to be scattered across individual modules. Flip these
instead of hunting through run_facebook.py / kie_vision.py / etc.
"""

# ── run_facebook.py pipeline toggles ──
FETCH_COMMENTS = False                  # skip comment scraping — not needed right now
ANALYZE_IMAGES = True          # run the KIE upload+analyze workflow at all —
                                 # when False, images are still downloaded/processed
                                 # but never sent to KIE (analysis_status: "skipped")
MAX_IMAGES_PER_POST = None              # cap images downloaded/processed/analyzed per post —
                                 # None = no limit (all images; a 50-image hard safety cap
                                 # still applies inside the album walk), or a number to cap
IMAGE_WORKERS = 5                       # concurrent per-post image processing/analysis threads
IMAGE_DOWNLOAD_WORKERS = 5              # concurrent per-post image *download* threads (fb_client.download_pending_post_images)
POSTS_PER_SOURCE_DEFAULT = 500          # default --posts-per-source limit for page/group fetch
PAGE_SCAN_WORKERS = 5                 # parallel page/group URL discovery threads in Phase 1
                                # (each source's post-list fetch runs concurrently; the
                                # scrapers are per-call thread-safe, so this only
                                # overlaps the long pagination waits)

# ── run_facebook.py publish-prep: AI deal infographics (Phase 3, before publishing) ──
GENERATE_AI_IMAGES = True                  # send each single-deal image to KIE (nano-banana-2 via generate.py)
                                # to create a new AI-generated coupon-deal infographic; the AI image
                                # replaces the original. Only images with exactly one deal
                                # (analysis_status "success") are generated — no_deal / multiple_deals /
                                # failed / skipped entries are skipped (one image can't carry
                                # multiple deals). When False, the original image is kept as-is.
                                # The prompt is DEAL_INFOGRAPHIC_PROMPT below with {brand_name}
                                # substituted; the workspace image_prompt config is no longer used.
DEAL_INFOGRAPHIC_PROMPT = """Create a clean coupon-deal infographic for {brand name} using the attached image(s) and the OFFICIAL DEAL DATA listed at the end. Follow the template's layout: brand banner at top, date pill below it, product photo area, one card per coupon, savings summary panel at the bottom.

{inputs_block}

TASK
1. Product photo: take the main product photo from the deal reference exactly as-is (all products together as one scene), remove its background, and place it on a clean white background in the product area. Do not crop products into individual slots and do not redraw them.
2. Coupons: place every Digital Coupon panel from the deal reference AS-IS — each coupon is ONE complete card (its image and text together), exactly as it appears in the reference. Do not redesign, re-type, split, or add coupons. The number of coupon cards must match the reference.
3. Savings summary: fill the summary panel using ONLY the OFFICIAL DEAL DATA below — every product with its price, then TOTAL, COUPON SAVINGS, and the final PAY amount.
4. Date: if the OFFICIAL DEAL DATA includes a date, show it in the date pill. If there is no date, leave the date pill out of the infographic entirely.

STRICT RULE — NOTHING INVENTED
Take EVERYTHING from the deal reference image. Never create, add, or assume anything by yourself:
- Do NOT add any Digital Coupon panel that is not visible in the deal reference — not even one that "would fit" the deal. If the reference shows no coupons, the infographic has no coupon cards.
- Do NOT add any product, price, item, brand, or offer that is not visible in the deal reference.
- Do NOT invent or fill in missing coupon values, terms, sizes, or dates — if something is not readable in the reference, omit it.
- The OFFICIAL DEAL DATA may only be used for the savings summary and the date pill — never as a source for new coupon panels or products.

RULES
- Never reproduce any watermark, signature, handwriting, creator name, or social-media handle from either image.
- Do not draw placeholder frames, dashed boxes, or slot borders in the finished infographic — the template's dashed areas only mark where content goes.
- No extra decorations, starbursts, or text beyond the template structure and the summary.
- Bold readable text, clean white background, consistent spacing and alignment."""

# LAYOUT TEMPLATE — REQUIRED, one per brand, for visual consistency and
# brand-matched colors. Every brand MUST have assets/templates/<brand-slug>.jpg
# (e.g. assets/templates/dollar-general.jpg). It is attached as the FIRST
# reference image — the model copies its layout/structure while the deal
# image stays the content source of truth. A brand without its template
# skips infographic generation (config error — create the file).
INFOGRAPHIC_TEMPLATE_DIR = "assets/templates"

# FEATURE (hero) image template — one per brand, shown at the very top of the
# published WordPress page with the dynamic page title rendered into it:
# "Top {N} {brand} Deals in This Week ({date_range})" — N is the published
# deal count and the date range is the week window (week_start). Missing
# file → the page publishes without a hero image.
FEATURE_TEMPLATE_DIR = "assets/feature_templates"

# The {inputs_block} section of DEAL_INFOGRAPHIC_PROMPT — which images are
# attached and what each one is for.
DEAL_INPUTS_BLOCK = """INPUTS
- IMAGE 1 (layout template): the exact frame to reuse — copy its layout, colors, and styling. Its card count is an example only; match the reference's coupon count.
- IMAGE 2 (deal reference): the source of truth for the product photo and the coupon panels."""

AI_IMAGE_MAX_BYTES = 512000          # images are compressed under this size before upload to KIE
                                # and the AI result is compressed under it before publishing
AI_IMAGE_WORKERS = 5                    # parallel AI image-generation threads (upload→createTask→poll→download
                                # per image; the shared KIE rate limiter keeps the total under the
                                # account cap, so this only overlaps the long poll waits)
AI_IMAGE_COMPARE = False                # when True, the output/published image is a comparison sheet:
                                # the AI-generated image on TOP and the ORIGINAL below it, each
                                # labeled ("AI GENERATED" / "ORIGINAL") so they're easy to compare.
                                # Flip off for production publishing (clean AI image only).

# ── image_pipeline.py ──
MAX_PROCESSED_BYTES = 204800          # 200 KB cap for processed (grayscale) images
MIN_QUALITY = 20                          # floor for JPEG quality before we start downscaling
MIN_SCALE = 0.25                          # floor for resolution downscale factor

# ── kie_vision.py rate limiter ──
KIE_MAX_REQUESTS_PER_WINDOW = 18          # stay under KIE's ~20 requests/10s account cap
KIE_RATE_WINDOW_SECONDS = 10

# ── zip_export.py ──
ZIP_MAX_AGE_SECONDS = 86400           # sweep exports older than this on every new build

# ── brand filtering / output organization (run_facebook.py) ──
MIN_IMAGES_FOR_KEEP = 1           # a post needs more than 1 image to be kept (see brand_mapping.py)
