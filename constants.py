#!/usr/bin/env python3
"""
Central place for tunable pipeline constants and testing toggles.

Actual secrets (API keys, tokens) stay in .env — this file is for behavior
knobs that used to be scattered across individual modules. Flip these
instead of hunting through run_facebook.py / kie_vision.py / etc.
"""

# ── run_facebook.py pipeline toggles ──
FETCH_COMMENTS = False                # skip comment scraping — not needed right now
ANALYZE_IMAGES = True        # run the KIE upload+analyze workflow at all —
                                 # when False, images are still downloaded/processed
                                 # but never sent to KIE (analysis_status: "skipped")
MAX_IMAGES_PER_POST = 5            # cap images downloaded/processed/analyzed per post —
                                 # None = no limit (all images; a 50-image hard safety cap
                                 # still applies inside the album walk), or a number to cap
IMAGE_WORKERS = 5                     # concurrent per-post image processing/analysis threads
IMAGE_DOWNLOAD_WORKERS = 5            # concurrent per-post image *download* threads (fb_client.download_pending_post_images)
POSTS_PER_SOURCE_DEFAULT = 500        # default --posts-per-source limit for page/group fetch
PAGE_SCAN_WORKERS = 3               # parallel page/group URL discovery threads in Phase 1
                                # (each source's post-list fetch runs concurrently; the
                                # scrapers are per-call thread-safe, so this only
                                # overlaps the long pagination waits)

# ── run_facebook.py publish-prep: AI deal infographics (Phase 3, before publishing) ──
GENERATE_AI_IMAGES = True                # send each single-deal image to KIE (nano-banana-2 via generate.py)
                                # to create a new AI-generated coupon-deal infographic; the AI image
                                # replaces the original. Only images with exactly one deal
                                # (analysis_status "success") are generated — no_deal / multiple_deals /
                                # failed / skipped entries are skipped (one image can't carry
                                # multiple deals). When False, the original image is kept as-is.
                                # The prompt is DEAL_INFOGRAPHIC_PROMPT below with {brand_name}
                                # substituted; the workspace image_prompt config is no longer used.
DEAL_INFOGRAPHIC_PROMPT = """{brand name} Coupon Infographic — Reusable Master Prompt
Create a professional, high-quality {brand name} coupon deal infographic using the attached reference image as the source of truth for product images, prices, coupon discounts, deal calculations, and promotional details.
1. Overall Design and Branding
- Create a clean, premium, eye-catching promotional flyer for {brand name}.
- Use a vertical 2:3 or 9:16 layout, optimized for Pinterest Pins, Facebook posts, and Instagram Stories.
- Use a clean white background with the brand's recognizable colors and visual identity, based on the reference image.
- Place a large, bold {brand name} banner at the top, using the appropriate brand colors and typography.
- Use bold, highly readable sans-serif typography.
- Maintain a polished retail-advertisement aesthetic with strong visual hierarchy.
- Do not add unnecessary decorative graphics, watermarks, signatures, extra logos, or unrelated visual elements.
2. Product Image Section
- Carefully analyze the attached reference image and use the exact products shown.
- Preserve the correct product packaging, brand names, colors, bottle shapes, labels, and quantities.
- Display the products prominently near the top, arranged neatly side by side.
- Use realistic product photography with sharp details, accurate proportions, and clean lighting.
- Do not substitute products with similar-looking alternatives.
- Do not invent products or include items that are not present in the reference image.
3. Validity Date
Directly below the top banner, display the deal's validity date in a highly visible format.
Example: VALID: [DATE] ONLY
Use the date supplied in the reference image or provided instructions. Do not assume a new date or invent a date if none is provided.
4. Individual Deal Sections
Below the product lineup, create separate, clearly organized deal sections arranged vertically.
Each deal section should include:
- A small, accurate image of the relevant product on the left.
- The product brand and name in bold black text.
- A short product description and size, if available.
- The original retail price, prominently displayed.
- The applicable coupon or discount in a highlighted coupon panel on the right.
- Clear coupon terms, including qualifying quantities, sizes, or spending requirements when provided.
Use thin borders or subtle background colors that complement {brand name} to separate deal sections. Maintain consistent spacing, alignment, and typography throughout.
5. Coupon Panels
- Use clearly visible coupon headers labeled DIGITAL COUPON, COUPON, or the exact terminology shown in the reference image.
- Display coupon values in large, bold, highly readable text.
- Use colors consistent with the retailer's branding and the reference image.
- Preserve coupon conditions and exclusions accurately.
- Do not invent additional coupons, discounts, or promotional offers.
6. Final Deal Breakdown
At the bottom, include a prominent savings summary panel.
List each product, its price, and the quantity purchased. Then display:
- TOTAL: Sum of the original product prices.
- COUPON SAVINGS: Total value of all applicable coupons.
- FINAL PRICE: Total after coupon deductions.
- + TAX: Where applicable.
Use a bold, high-contrast summary area alongside a highly visible final-price panel.
Example layout:
TOTAL: $28.25
COUPON SAVINGS: −$17.50
PAY $10.75 + TAX
Calculate all totals carefully using only the prices, quantities, and coupons supplied in the reference. Avoid arithmetic errors, duplicate coupon deductions, or applying coupons to ineligible products.
7. Layout and Readability
- Use a strictly vertical, top-to-bottom scrolling layout.
- Keep the top banner, product showcase, deal sections, coupon details, and final price summary in that order.
- Make all text large enough to read on mobile devices.
- Prevent text clipping, overlapping elements, cut-off products, and cramped spacing.
- Keep adequate margins around the edges.
- Use consistent alignment, spacing, and section heights.
- Prioritize accurate deal information over decorative styling.
8. Accuracy Rules
- Treat the uploaded image as the primary reference.
- Carefully extract all product names, quantities, prices, coupon values, conditions, and dates before designing.
- Do not guess unreadable details. If information cannot be verified, omit it rather than inventing it.
- Preserve the meaning of every coupon and its eligibility requirements.
- Ensure the final price matches the arithmetic of the displayed prices and applicable coupon savings.
- Do not include couponer names, creator signatures, social media handles, or unrelated text from the source image.
- Use {brand name} consistently for the retailer identity.
- Do not carry over products, prices, coupons, dates, or colors from previously generated infographics when they are not supported by the current reference image.
9. Final Output
Generate a single, polished, high-resolution promotional infographic that follows this visual system while adapting the number of products and deal sections to the uploaded reference image.
Important: Reuse the same overall layout, strong branding, realistic product presentation, stacked deal cards, coupon panels, and bold final-price summary for every new image. Adapt the colors, typography, logo treatment, and content to {brand name} and the current reference image. Do not force a particular retailer's branding or color scheme onto another brand."""

AI_IMAGE_MAX_BYTES = 512000        # images are compressed under this size before upload to KIE
                                # and the AI result is compressed under it before publishing
AI_IMAGE_WORKERS = 5                  # parallel AI image-generation threads (upload→createTask→poll→download
                                # per image; the shared KIE rate limiter keeps the total under the
                                # account cap, so this only overlaps the long poll waits)
AI_IMAGE_COMPARE = False              # when True, the output/published image is a comparison sheet:
                                # the AI-generated image on TOP and the ORIGINAL below it, each
                                # labeled ("AI GENERATED" / "ORIGINAL") so they're easy to compare.
                                # Flip off for production publishing (clean AI image only).

# ── image_pipeline.py ──
MAX_PROCESSED_BYTES = 204800        # 200 KB cap for processed (grayscale) images
MIN_QUALITY = 20                        # floor for JPEG quality before we start downscaling
MIN_SCALE = 0.25                        # floor for resolution downscale factor

# ── kie_vision.py rate limiter ──
KIE_MAX_REQUESTS_PER_WINDOW = 18        # stay under KIE's ~20 requests/10s account cap
KIE_RATE_WINDOW_SECONDS = 10

# ── zip_export.py ──
ZIP_MAX_AGE_SECONDS = 86400         # sweep exports older than this on every new build

# ── brand filtering / output organization (run_facebook.py) ──
MIN_IMAGES_FOR_KEEP = 2         # a post needs more than 1 image to be kept (see brand_mapping.py)
