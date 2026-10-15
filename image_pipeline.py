#!/usr/bin/env python3
"""
Image processing for the Facebook product-analysis pipeline.

Every image downloaded from a post gets a processed sibling: grayscale,
resolution-capped (longest side <= ANALYSIS_MAX_SIDE px). Originals are
never modified.
"""

import os
from PIL import Image

from constants import ANALYSIS_MAX_SIDE


def process_image(original_path: str, processed_path: str,
                  max_side: int = ANALYSIS_MAX_SIDE) -> str:
    """Convert an image to grayscale and cap its resolution (longest side
    `max_side` px — vision tokens scale with pixel area, not file bytes).
    No byte-cap compression loop: grayscale + the resolution cap already
    keep the JPEG small.

    Saves the result to `processed_path` (JPEG) and returns that path.
    Raises on unreadable/corrupt input — caller should treat that as a
    per-image failure, not a whole-post failure.
    """
    os.makedirs(os.path.dirname(processed_path), exist_ok=True)

    with Image.open(original_path) as im:
        gray = im.convert("L")

        if max(gray.size) > max_side:
            ratio = max_side / max(gray.size)
            gray = gray.resize(
                (round(gray.width * ratio), round(gray.height * ratio)),
                Image.LANCZOS,
            )

        gray.save(processed_path, "JPEG", quality=85, optimize=True)

    return processed_path


def compress_under_limit(image_path: str, max_bytes: int,
                         start_quality: int = 92, min_quality: int = 40) -> bool:
    """Re-save a COLOR image in place as JPEG only while it exceeds `max_bytes`
    — quality is reduced first (starting high, per the no-quality-drop
    requirement), mild downscaling only as a last resort. No grayscale.
    Returns True if the file was rewritten; a no-op when already small enough.
    Raises on unreadable/corrupt input."""
    if os.path.getsize(image_path) <= max_bytes:
        return False

    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        quality = start_quality
        scale = 1.0
        working = rgb

        while True:
            working.save(image_path, "JPEG", quality=quality, optimize=True)
            if os.path.getsize(image_path) <= max_bytes:
                break
            if quality > min_quality:
                quality -= 8
                continue
            if scale > 0.5:
                scale -= 0.1
                w, h = rgb.size
                working = rgb.resize(
                    (max(50, int(w * scale)), max(50, int(h * scale))), Image.LANCZOS
                )
                continue
            # Already at minimum quality/scale — accept whatever we have.
            break

    return True


def process_post_images(original_paths: list[str], processed_dir: str) -> dict[str, str | None]:
    """Process a batch of images. Returns {original_path: processed_path or None on failure}."""
    results = {}
    for original_path in original_paths:
        filename = os.path.splitext(os.path.basename(original_path))[0] + "_processed.jpg"
        processed_path = os.path.join(processed_dir, filename)
        try:
            process_image(original_path, processed_path)
            results[original_path] = processed_path
        except Exception as e:
            print(f"  ⚠️  Failed to process image {original_path}: {e}")
            results[original_path] = None
    return results
