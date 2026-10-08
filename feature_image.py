#!/usr/bin/env python3
"""
Feature (hero) image rendering — one module, brand-based.

Each publish starts the WordPress page with a hero image: the brand's feature
template (assets/feature_templates/<brand-slug>.jpg) with the page title
rendered into its banner area. Pure Pillow — no AI generation: deterministic,
free, and instant.

Per-brand behavior lives here: the template is looked up per brand slug, and
any brand-specific rendering tweaks (text color override, banner box override,
etc.) can be added to BRAND_OVERRIDES without touching the publisher.

CLI for quick testing / sample generation:
  python3 feature_image.py --brand "Dollar General" \
      --title "Top 20 Dollar General Deals in This Week (10/19 – 10/25)" [--out <path>]
"""

import argparse
from pathlib import Path

from constants import FEATURE_TEMPLATE_DIR

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",   # macOS
    "/System/Library/Fonts/Helvetica.ttc",                 # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",                        # Windows
]

# Per-brand rendering overrides (all optional):
#   text_color: (r, g, b) — skips banner-luminance auto-contrast
#   banner_box: (x0, y0, x1, y1) — skips banner auto-detection
#   template:   explicit template path — skips the per-brand lookup
BRAND_OVERRIDES: dict[str, dict] = {
    # CVS: the auto-detection locks onto the blue SKY (the largest saturated
    # band), not the frame — pin the white frame interior as the text area.
    "cvs": {"banner_box": (205, 305, 828, 590)},
    # "dollar-general": {"text_color": (25, 25, 25)},
}


def feature_template_path(brand_slug: str) -> Path:
    """The brand's feature template path (may not exist)."""
    return Path(__file__).resolve().parent / FEATURE_TEMPLATE_DIR / f"{brand_slug}.jpg"


def _load_hero_font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10 supports size
    except TypeError:
        return ImageFont.load_default()


def _detect_banner_box(img):
    """Locate the banner: the largest saturated-color band that spans most of
    the image's width (the brand's banner strip). Returns (x0, y0, x1, y1) in
    full-resolution coordinates, or None when no banner is found."""
    from PIL import Image
    w, h = img.size
    small = img.resize((max(1, w // 4), max(1, h // 4)))
    sw, sh = small.size
    px = small.load()

    def _saturated(p):
        return max(p) - min(p) >= 40  # skip white/black/gray

    # Most common saturated color (quantized) = the banner color.
    counts: dict = {}
    for y in range(sh):
        for x in range(sw):
            p = px[x, y]
            if _saturated(p):
                key = (p[0] // 16 * 16, p[1] // 16 * 16, p[2] // 16 * 16)
                counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    banner = max(counts, key=counts.get)

    def _is_banner(x, y):
        p = px[x, y]
        return (abs(p[0] - banner[0]) <= 60 and abs(p[1] - banner[1]) <= 60
                and abs(p[2] - banner[2]) <= 60)

    # Rows where the banner color spans > 45% of the width = the banner band.
    band_rows = [y for y in range(sh)
                 if sum(1 for x in range(sw) if _is_banner(x, y)) > sw * 0.45]
    if not band_rows:
        return None
    y0, y1 = min(band_rows), max(band_rows)
    # Columns within the band spanning > 45% of the band height.
    band_cols = [x for x in range(sw)
                 if sum(1 for y in range(y0, y1 + 1) if _is_banner(x, y)) > (y1 - y0 + 1) * 0.45]
    if not band_cols:
        return None
    x0, x1 = min(band_cols), max(band_cols)
    scale = w / sw
    return (x0 * scale, y0 * scale, (x1 + 1) * scale, (y1 + 1) * scale)


def _wrap_title(draw, title: str, font, max_width: int) -> list[str]:
    """Greedy word-wrap of the title into lines that fit `max_width`."""
    words = title.split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_feature_image(template_path: str, title: str, out_path: str,
                         text_color=None, banner_box=None) -> bool:
    """Render the page title into the feature template's banner area with
    Pillow. The banner is auto-detected (the largest saturated-color band
    spanning the image's width) unless `banner_box` is given; the title is
    wrapped and auto-sized to fit it, drawn centered in `text_color` (or a
    contrasting color chosen from the banner's luminance). Returns True on
    success; never raises — a failure returns False and the caller publishes
    without the hero image."""
    try:
        from PIL import Image, ImageDraw
        img = Image.open(template_path).convert("RGB")
        box = banner_box or _detect_banner_box(img)
        if not box:
            print(f"  ⚠️  Could not locate the banner area in {template_path} — publishing without the feature image")
            return False
        x0, y0, x1, y1 = box
        # Inset for the banner's rounded corners / margins.
        pad_x, pad_y = (x1 - x0) * 0.08, (y1 - y0) * 0.12
        tx0, ty0, tx1, ty1 = x0 + pad_x, y0 + pad_y, x1 - pad_x, y1 - pad_y
        box_w, box_h = tx1 - tx0, ty1 - ty0

        if not text_color:
            # Text color contrasts with the banner (dark on light, white on dark).
            r, g, b = img.getpixel((int((tx0 + tx1) / 2), int((ty0 + ty1) / 2)))
            text_color = (25, 25, 25) if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else (255, 255, 255)

        draw = ImageDraw.Draw(img)
        # Auto-size: start large, shrink until the wrapped title fits the box.
        size = int(box_h * 0.55)
        while size >= 12:
            font = _load_hero_font(size)
            lines = _wrap_title(draw, title, font, box_w)
            line_h = size * 1.25
            if len(lines) * line_h <= box_h and all(
                    draw.textlength(l, font=font) <= box_w for l in lines):
                break
            size = int(size * 0.9)
        else:
            print("  ⚠️  Title too long for the banner area — publishing without the feature image")
            return False

        total_h = len(lines) * line_h
        y = ty0 + (box_h - total_h) / 2
        for line in lines:
            lw = draw.textlength(line, font=font)
            draw.text((tx0 + (box_w - lw) / 2, y), line, font=font, fill=text_color)
            y += line_h

        img.save(out_path, "JPEG", quality=92)
        print(f"  🖼️  Feature image rendered: {title}")
        return True
    except Exception as e:
        print(f"  ⚠️  Feature image rendering failed: {e} — publishing without it")
        return False


def build_feature_image(brand: str, brand_slug: str, title: str, out_path: str) -> bool:
    """Brand-level entry point used by the publisher: resolve the brand's
    feature template (assets/feature_templates/<brand-slug>.jpg, with an
    optional per-brand override in BRAND_OVERRIDES), render `title` into its
    banner, and save to `out_path`. Returns True on success; a missing
    template or failed render returns False (the caller publishes without
    the hero)."""
    overrides = BRAND_OVERRIDES.get(brand_slug, {})
    template = overrides.get("template") or feature_template_path(brand_slug)
    if not template or not Path(template).exists():
        print(f"  ℹ️  No feature template for {brand_slug} ({template}) — publishing without a feature image")
        return False
    return render_feature_image(str(template), title, out_path,
                                 text_color=overrides.get("text_color"),
                                 banner_box=overrides.get("banner_box"))


def main():
    parser = argparse.ArgumentParser(
        description="Render a sample feature (hero) image for a brand — no AI, pure Pillow")
    parser.add_argument("--brand", required=True, help="Canonical brand name (e.g. 'Dollar General')")
    parser.add_argument("--title", required=True, help='Title to render, e.g. "Top 20 Dollar General Deals in This Week (10/19 – 10/25)"')
    parser.add_argument("--out", default="sample_feature_image.jpg",
                        help="Output path (default: sample_feature_image.jpg in the project root)")
    args = parser.parse_args()

    import brand_mapping
    brand_slug = brand_mapping.brand_slug(args.brand)
    ok = build_feature_image(args.brand, brand_slug, args.title, args.out)
    if ok:
        print(f"✅ Sample saved to {args.out}")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
