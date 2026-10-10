#!/usr/bin/env python3
"""
Central place for tunable pipeline constants and testing toggles.

Actual secrets (API keys, tokens) stay in .env — this file is for behavior
knobs that used to be scattered across individual modules. Flip these
instead of hunting through run_facebook.py / analysis.py / etc.
"""

# ── run_facebook.py pipeline toggles ──
FETCH_COMMENTS = False                  # skip comment scraping — not needed right now
ANALYZE_IMAGES = True          # run the OpenAI deal-analysis workflow at all —
                                 # when False, images are still downloaded/processed
                                 # but never analyzed (analysis_status: "skipped")
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
GENERATE_AI_IMAGES = True                  # send each single-deal image to KIE (model chosen by
                                # AI_IMAGE_BACKEND below) to create a new AI-generated coupon-deal
                                # infographic; the AI image replaces the original. Only images with
                                # exactly one deal (analysis_status "success") are generated —
                                # no_deal / multiple_deals / failed / skipped entries are skipped
                                # (one image can't carry multiple deals). When False, the original
                                # image is kept as-is.
                                # The prompt is DEAL_INFOGRAPHIC_PROMPT below with {brand_name}
                                # substituted; the workspace image_prompt config is no longer used.
DEAL_INFOGRAPHIC_PROMPT = """Create a clean coupon-deal infographic for {brand name} using the attached image(s) and the OFFICIAL DEAL DATA listed at the end. Follow the template's layout: brand logo at top, validity date line below it, product photo area on a clean white background, then the deal summary rows — Items Grabbed, Subtotal, Coupons Used, Reward, Rebates, Final Net Cost.

{inputs_block}

TASK
1. Product photo: take the main product photo from the deal reference exactly as-is (all products together as one scene), remove its background along with any hands, people, or surrounding objects, and place it on a clean white background in the product area. Do not crop products into individual slots and do not redraw them.
2. Summary rows: fill each row's value using ONLY the OFFICIAL DEAL DATA below — Items Grabbed (one bullet line per item, exactly as listed in the official data, with quantities and prices), Subtotal, Coupons Used (the total money value, "$0.00" when none), Reward ("$0.00" when none), Rebates ("$0.00" when none), and Final Net Cost. EVERY row must display a concrete value — never leave a row blank and never render a dash, em-dash, or placeholder in place of a value. Use the row's OFFICIAL DEAL DATA value; when it says "(not shown)", derive it arithmetically from the official items' prices where possible (e.g. Subtotal = the sum of the item prices, Final Net Cost = subtotal minus coupons/rewards/rebates); only when it truly cannot be determined, render "N/A".
3. Date: the validity date comes ONLY from the OFFICIAL DEAL DATA's "VALIDITY DATE" line — render it exactly as given, word for word, on the date line below the logo (do not re-read or rephrase it from the deal reference). If it says "(not shown)", leave the date line out of the infographic entirely.

STRICT RULE — NOTHING INVENTED
Take EVERYTHING from the deal reference image and the OFFICIAL DEAL DATA. Never create, add, or assume anything by yourself:
- Do NOT add any product, price, item, brand, coupon, reward, or rebate that is not visible in the deal reference or present in the OFFICIAL DEAL DATA.
- Do NOT invent or fill in missing coupon values, terms, sizes, or dates — if something is not readable in the reference, omit it.
- The OFFICIAL DEAL DATA is the only source for the summary row values and the date line — never add new text containers beyond the template's structure. Deriving Subtotal or Final Net Cost arithmetically from the official items' prices is allowed (it is math, not invention); inventing a missing price or value is not.

RULES
- Never reproduce any watermark, signature, handwriting, creator name, or social-media handle from either image.
- Do not draw placeholder frames, dashed boxes, or slot borders in the finished infographic — the template's empty areas only mark where content goes.
- No extra decorations, starbursts, or text beyond the template structure.
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
AI_IMAGE_BACKEND = "gpt"                # KIE image-generation model used by run_facebook.py:
                                # "gpt" = gpt-image-2.5 Sunburst (gpt_generate.py, input_urls)
                                # "kie" = nano-banana-2 (generate.py, image_input)

# ── image_pipeline.py ──
MAX_PROCESSED_BYTES = 204800          # 200 KB cap for processed (grayscale) images
MIN_QUALITY = 20                          # floor for JPEG quality before we start downscaling
MIN_SCALE = 0.25                          # floor for resolution downscale factor

# ── kie_ratelimit.py (KIE image-generation path) ──
KIE_MAX_REQUESTS_PER_WINDOW = 18          # stay under KIE's ~20 requests/10s account cap
KIE_RATE_WINDOW_SECONDS = 10

# ── zip_export.py ──
ZIP_MAX_AGE_SECONDS = 86400           # sweep exports older than this on every new build

# ── brand filtering / output organization (run_facebook.py) ──
MIN_IMAGES_FOR_KEEP = 1           # a post needs more than 1 image to be kept (see brand_mapping.py)
