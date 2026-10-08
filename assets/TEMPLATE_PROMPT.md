# Infographic Template Generator — Reusable Prompt

Use this recipe to create a new per-brand layout template for the deal
infographics. The result must be saved as
`assets/templates/<brand-slug>.jpg` (slugs come from `brand_mapping.py`,
e.g. `dollar-general`, `walmart`, `family-dollar`, `walgreens`).

Inputs:
1. **Reference template** — an existing brand template to copy the layout
   from (e.g. `assets/templates/dollar-general.jpg`). All templates share
   one identical structure; only the branding changes.
2. **Official brand logo** — save it as `assets/logos/<brand-slug>.png`
   first. It becomes the top banner's identity.

---

## The prompt (fill in the two {PLACEHOLDERS})

```
Recreate this exact coupon-deal infographic TEMPLATE layout with new retailer branding. Keep the structure IDENTICAL to the layout template (IMAGE 1) — same vertical 9:16 layout, same section order, same proportions, same typography scale, same spacing:
1. The large bold retailer banner at the top.
2. The small "VALID: [DATE] ONLY" pill directly below it.
3. The one wide empty product-photo drop zone (light gray rounded rectangle with a subtle dashed border, no text inside).
4. Two stacked EMPTY deal-card containers (single unified rounded rectangles with thin borders, completely empty inside — no internal split, no sub-slots).
5. The full-width savings summary panel at the bottom: dark header strip reading "SAVINGS SUMMARY" in bold white text, left column with three thin gray placeholder receipt lines ending with "SUBTOTAL", right column with "TOTAL:" and "COUPON SAVINGS:" labels in bold black with gray placeholder value lines, and a large rounded block reading "PAY $XX.XX + TAX" in bold text.

BRAND BANNER: use the official logo in IMAGE 2 — place the logo itself (cleanly, unmodified, on a solid background matching its brand color) as the top banner, sized to fill the banner strip the way the reference template's banner text does. Do not redraw or restyle the logo.
BRAND COLORS: {BRAND COLORS — e.g. "Walmart blue #0071CE as the primary color with Walmart yellow #FFC220 accents. The savings-summary header strip is solid Walmart blue with bold white text, and the PAY block is solid Walmart blue with bold yellow text."}
Everything else stays exactly as in the layout template: clean white background, flat modern retail-flyer aesthetic, crisp vector-like shapes, bold readable sans-serif typography, empty drop zones (no product images, no coupon text, no prices, no silhouettes inside the cards), the 2 cards representing a REPEATING PATTERN rather than a fixed count, and absolutely NO watermarks, signatures, handwriting, creator names, or decorative overlays. Sharp, crisp, correctly spelled text.
```

---

## How to run it

### Via the pipeline (recommended — uses the same KIE account)

```python
from pathlib import Path
import generate
from image_pipeline import compress_under_limit
from constants import AI_IMAGE_MAX_BYTES

REFERENCE_TEMPLATE = "assets/templates/dollar-general.jpg"   # layout to copy
LOGO = "assets/logos/<brand-slug>.png"                       # official logo
OUT = "assets/templates/<brand-slug>.jpg"

prompt = """<paste the filled-in prompt here>"""

ref_url = generate.upload_image(REFERENCE_TEMPLATE)
logo_url = generate.upload_image(LOGO)
task_id = generate.create_task([ref_url, logo_url], prompt=prompt)
url = generate.poll_task(task_id)
generate.download_image(url, OUT)
compress_under_limit(OUT, AI_IMAGE_MAX_BYTES)
```

### Via ChatGPT (manual alternative)

Attach both images — the reference template FIRST, the logo SECOND — and
paste the prompt above. If the output drifts (wrong slot count, extra text),
reply "keep the exact same layout, only change the branding" instead of
regenerating from scratch.

---

## Checklist for a new brand

- [ ] Brand added to `brand_mapping.py` (`BRAND_KEYWORDS` + `BRAND_SLUGS`)
- [ ] Logo saved as `assets/logos/<brand-slug>.png`
- [ ] Template generated and saved as `assets/templates/<brand-slug>.jpg`
- [ ] Template visually matches the reference layout (banner / VALID pill /
      one dashed product zone / two unified empty cards / SAVINGS SUMMARY
      with `PAY $XX.XX + TAX`)
- [ ] Workspace created with that brand + a WordPress page ID
