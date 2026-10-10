#!/usr/bin/env python3
"""
Facebook pipeline — single end-to-end job. Invoked as a subprocess by
job_runner.py. One job does everything for the workspace's ONE brand:

  1. DISCOVERY — resolve every source and fetch its post list first
     (page/group with download_images=False, fetch_extra_images=False; just
     the post ID for a post URL), logging per-source and total found counts
     before any per-post work starts. The brand text filter runs inside the
     scrapers' pagination loop, before a matching post's images are even
     enumerated.
  2. PROCESSING — per discovered post: extra-image discovery via
     last_media_id → download → grayscale/compress → KIE coupon-deal
     extraction → keep only if it has enough images → organize into
     output/<job_id>/<brand-slug>/deals/post_<id>/.
  3. AI DEAL INFOGRAPHICS + PUBLISHING — each single-deal image is
     regenerated as a branded coupon-deal infographic via KIE
     (nano-banana-2) using constants.DEAL_INFOGRAPHIC_PROMPT with
     {brand name} substituted; no_deal / multiple_deals / failed images are
     skipped. Then the deals publish to the workspace's WordPress page
     (publish_wordpress.publish_brand — the food destination is used for
     the deals page, falling back to the non-food destination). The old
     product-centric prep stages (dedupe, scraper analysis, product cap)
     are still defined below but no longer invoked; they will be reworked
     around the deal schema in a later step. A publish failure fails the
     job (exit 1).

The workspace's single-brand config arrives as CLI args from job_runner.py:
--brand, --publish-target/--page-id (ONE publish destination — the
food/non-food category structure is removed), --page-title, --week-start.

One failing post/source never aborts the whole job — failures are logged
and the run continues.
"""

import argparse
import json
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests

import analysis as deal_analysis
import brand_mapping
import fb_client
import image_pipeline
import generate
import gpt_generate
from image_pipeline import compress_under_limit
from kie_ratelimit import rate_limiter
import publish_wordpress
from publish_wordpress import publish_brand
from data_store import load_fb_auth
from constants import (
    FETCH_COMMENTS, ANALYZE_IMAGES, MAX_IMAGES_PER_POST, IMAGE_WORKERS,
    POSTS_PER_SOURCE_DEFAULT, MIN_IMAGES_FOR_KEEP,
    AI_IMAGE_BACKEND, AI_IMAGE_COMPARE, AI_IMAGE_MAX_BYTES, AI_IMAGE_WORKERS,
    DEAL_INFOGRAPHIC_PROMPT, DEAL_INPUTS_BLOCK,
    GENERATE_AI_IMAGES, INFOGRAPHIC_TEMPLATE_DIR, PAGE_SCAN_WORKERS,
)

# AI image-generation backend (constants.AI_IMAGE_BACKEND): both modules share
# the same interface (upload_image / create_task / poll_task / download_image /
# make_comparison_image), so every call site below stays untouched.
if AI_IMAGE_BACKEND == "gpt":
    generate = gpt_generate
make_comparison_image = generate.make_comparison_image

SOURCE_MAX_ATTEMPTS = 3


def _with_source_retries(description: str, fn, max_attempts: int = SOURCE_MAX_ATTEMPTS):
    """Run one source's discovery/processing callable with retries — a
    transient failure (proxy hiccup, temporary block, timeout) gets up to
    `max_attempts` tries with backoff before the source is given up on.
    Returns fn()'s return value, or None if every attempt failed (the
    per-source isolation then just logs it and the job continues with the
    remaining sources)."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e
            print(f"  ⚠️ Attempt {attempt}/{max_attempts} failed for {description}: {e}")
            if attempt < max_attempts:
                wait_time = attempt * 5
                print(f"  ⏳ Retrying source in {wait_time} seconds...")
                time.sleep(wait_time)
    print(f"  ❌ Giving up on {description} after {max_attempts} attempts: {last_error}")
    return None


def parse_date_arg(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d


def make_brand_filter(brand: str | None):
    """Build the post-text brand filter from the workspace's single brand.
    With a brand configured, only posts whose single detected brand is that
    brand pass — every other brand's posts are rejected inside the scraper's
    pagination loop, before their images are even enumerated. Without one,
    any single detected brand passes."""
    if not brand:
        return lambda text: brand_mapping.detect_single_brand(text) is not None
    return lambda text: brand_mapping.detect_single_brand(text) == brand


def make_per_brand_limiter(brand_filter, limit: int):
    """Wrap the brand filter with an acceptance cap (testing aid): once
    `limit` posts of a brand have been accepted, every further post of that
    brand is rejected — for page/group sources this happens inside the
    scraper's pagination loop (the filter IS the text_filter), before that
    post's images are even enumerated. limit <= 0 means unlimited. The
    counter is shared across every source in the job, and each post is
    evaluated exactly once (page/group: at discovery; post URLs: in
    process_post_url), so the cap is per job, not per source."""
    if not limit or limit < 1:
        return brand_filter
    counts: dict[str, int] = {}
    counts_lock = threading.Lock()  # sources are discovered in parallel now

    def limited(text) -> bool:
        brand = brand_mapping.detect_single_brand(text)
        if not brand_filter(text):
            return False
        key = brand or "__unbranded__"   # relaxed sources accept no-brand posts
        with counts_lock:
            if counts.get(key, 0) >= limit:
                return False
            counts[key] = counts.get(key, 0) + 1
        return True

    return limited


def fetch_post_meta_from_html(url: str, cookies: dict | None) -> dict:
    """Best-effort post text/page name/publish time from the post's public
    Open Graph meta tags — the GraphQL comment-fetch flow for a bare post
    URL doesn't return this, so we fall back to a plain HTML fetch."""
    meta = {"text": None, "page_name": None, "published_at": None}
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "en-US,en;q=0.9",
        }
        proxies = fb_client.single_post_image.PROXIES
        resp = requests.get(url, headers=headers, cookies=cookies, proxies=proxies, timeout=20)
        html = resp.text

        desc_match = re.search(r'<meta property="og:description" content="([^"]*)"', html)
        if desc_match:
            meta["text"] = unescape(desc_match.group(1))

        site_match = re.search(r'<meta property="og:site_name" content="([^"]*)"', html)
        title_match = re.search(r'<meta property="og:title" content="([^"]*)"', html)
        if site_match and site_match.group(1):
            meta["page_name"] = unescape(site_match.group(1))
        elif title_match:
            meta["page_name"] = unescape(title_match.group(1))

        pub_match = re.search(r'"publish_time":(\d+)', html) or re.search(r'"creation_time":(\d+)', html)
        if pub_match:
            meta["published_at"] = datetime.fromtimestamp(int(pub_match.group(1)), tz=timezone.utc).isoformat()
    except Exception as e:
        print(f"  ⚠️  Could not fetch post meta from HTML: {e}")
    print(f"  📝 Post text from HTML meta: {(meta.get('text') or '(none found)')[:200]!r}")
    return meta


def _process_one_image(orig_path_str: str, index: int, processed_dir: Path, post_id: str,
                       brand_name: str) -> tuple[str, dict, dict]:
    """Process + analyze a single image. Never raises — a failure is recorded
    as analysis_status: 'failed' rather than losing the image entirely."""
    orig_path = Path(orig_path_str)
    image_id = f"image_{index:03d}"
    mapping_filename = f"post_{post_id}_{orig_path.name}"
    analysis = {"deals": [], "analysis_status": "failed"}
    processed_filename = None

    try:
        processed_path = image_pipeline.process_image(
            str(orig_path), str(processed_dir / f"{orig_path.stem}_processed.jpg")
        )
        processed_filename = Path(processed_path).name

        if not ANALYZE_IMAGES:
            analysis = {"deals": [], "analysis_status": "skipped"}
        else:
            try:
                result = deal_analysis.analyze_deal_image(processed_path, brand_name)
                # Status by deal count: `no_deal` (model replied `false`),
                # `success` (exactly one deal), `multiple_deals` (more than
                # one independent deal — kept for reference but skipped by
                # the later AI-image/publish stages, since one image can't
                # carry multiple deals).
                deal_count = len(result.get("deals") or [])
                status = ("no_deal" if deal_count == 0
                          else "success" if deal_count == 1
                          else "multiple_deals")
                analysis = {**result, "analysis_status": status}
            except Exception as e:
                print(f"  ⚠️  OpenAI analysis failed for {orig_path.name}: {e}")
    except Exception as e:
        print(f"  ⚠️  Image processing failed for {orig_path.name}: {e}")

    if analysis.get("analysis_status") in ("success", "no_deal", "multiple_deals"):
        deals = analysis.get("deals") or []
        deal_names = [d.get("name") for d in deals if isinstance(d, dict) and d.get("name")]
        print(f"  🖼️  {orig_path.name} → " + (f"{len(deals)} deal(s): {'; '.join(deal_names)}" if deal_names else "no valid deal"))

    image_entry = {
        "image_id": image_id,
        "original_filename": orig_path.name,
        "processed_filename": processed_filename,
        "analysis": analysis,
    }
    mapping_entry = {
        "post_id": post_id,
        "deals": analysis.get("deals") or [],
        "analysis_status": analysis.get("analysis_status"),
    }
    return mapping_filename, image_entry, mapping_entry


def build_analysis_and_images(post_dir: Path, original_paths: list[str], post_id: str,
                              brand_name: str):
    """Process + analyze every image for a post concurrently. A failure on
    one image is recorded as analysis_status: 'failed' and never aborts the
    others. Output order is preserved regardless of completion order."""
    processed_dir = post_dir / "images" / "processed"
    sorted_paths = sorted(original_paths)

    # Stage markers are printed once per post here (single-threaded), NOT
    # inside the per-image workers — per-image prints from the thread pool
    # used to duplicate and interleave mid-line in the job log.
    print("[STAGE] processing_images")
    if ANALYZE_IMAGES:
        print("[STAGE] analyzing_products")

    results: dict[int, tuple[str, dict, dict]] = {}
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as executor:
        futures = {
            executor.submit(_process_one_image, p, i, processed_dir, post_id, brand_name): i
            for i, p in enumerate(sorted_paths, start=1)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as e:
                print(f"  ⚠️  Unexpected error processing image #{i}: {e}")

    images_list = []
    mapping = {}
    for i in sorted(results):
        mapping_filename, image_entry, mapping_entry = results[i]
        images_list.append(image_entry)
        mapping[mapping_filename] = mapping_entry

    return images_list, mapping


def finalize_post(staging_root: Path, post_id: str, post_url, page_url, page_name, post_text,
                   published_at, comment_count, comments, orig_image_paths, brand_name: str) -> dict:
    """Writes the post's data under staging_root/post_<id> — a scratch
    location, not the final output path. organize_or_discard() below moves
    it into output/<job_id>/<brand>/deals/post_<id>/ (or deletes it) once
    the image-count filter has been applied."""
    post_dir = staging_root / f"post_{post_id}"
    canonical_orig_dir = post_dir / "images" / "original"
    canonical_orig_dir.mkdir(parents=True, exist_ok=True)

    orig_image_paths = orig_image_paths[:MAX_IMAGES_PER_POST]

    final_paths = []
    for p in orig_image_paths:
        p = Path(p)
        if p.parent == canonical_orig_dir:
            final_paths.append(p)
        else:
            dest = canonical_orig_dir / p.name
            try:
                shutil.copy2(p, dest)
                final_paths.append(dest)
            except Exception as e:
                print(f"  ⚠️  Could not copy image {p}: {e}")

    images_list, mapping = build_analysis_and_images(post_dir, [str(p) for p in final_paths], post_id, brand_name)

    print("[STAGE] creating_json")
    post_json = {
        "post": {
            "post_id": post_id,
            "post_url": post_url,
            "page_url": page_url,
            "page_name": page_name,
            "post_text": post_text,
            "published_at": published_at,
            "comment_count": comment_count,
        },
        "comments": comments,
        "images": images_list,
    }
    (post_dir / "post.json").write_text(json.dumps(post_json, indent=2, ensure_ascii=False), encoding="utf-8")

    analysis_dir = post_dir / "analysis"
    analysis_dir.mkdir(exist_ok=True)
    (analysis_dir / "image_analysis.json").write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  💾 Saved post {post_id} — {len(images_list)} image(s), {len(comments)} comment(s)")
    return mapping


def post_already_output(job_dir: Path, post_id: str) -> bool:
    """A post is 'already handled' if its folder exists anywhere under
    job_dir — either already organized into <brand>/<category>/post_<id>,
    or still sitting in _staging from an interrupted run."""
    return any(job_dir.rglob(f"post_{post_id}"))


def post_qualifies(mapping: dict) -> tuple[bool, str]:
    """Image-requirement filter (see CONTEXT.md): keep the post only if it
    has more than one image. The brand match is already decided by the post
    text (brand_mapping.detect_single_brand) before any images are even
    processed — this does not re-check brand against each image's KIE
    analysis."""
    if len(mapping) < MIN_IMAGES_FOR_KEEP:
        return False, f"only {len(mapping)} image(s), need >= {MIN_IMAGES_FOR_KEEP}"
    return True, "ok"


def organize_or_discard(job_dir: Path, staging_root: Path, post_id: str, text_brand: str,
                        mapping: dict) -> dict:
    """Applies the image-requirement filter, then organizes the staged post
    into output/<job_id>/<brand>/deals/post_<id>/ — a single bucket per
    brand. (The old per-image food/non_food category split is gone: images
    are now deal extractions, not product classifications.)

    The destination post_<id>/ folder holds the post's images (post.json's
    image list, originals, processed copies) and the per-post analysis.
    Images are also mirrored into a flat <brand>/deals/images/ folder
    (filenames prefixed `post_<id>_` to avoid collisions across posts) and
    the mapping is merged into the brand-wide <brand>/deals/image_analysis.json
    — the file every later stage reads.

    Returns the mapping entries that were written (empty dict if the post
    was discarded entirely)."""
    staged_dir = staging_root / f"post_{post_id}"
    keep, reason = post_qualifies(mapping)
    if not keep:
        print(f"  🗑️  Skipping post {post_id} — {reason}")
        shutil.rmtree(staged_dir, ignore_errors=True)
        return {}

    brand_slug = brand_mapping.brand_slug(text_brand)
    category = "deals"

    category_dir = job_dir / brand_slug / category
    dest_dir = category_dir / "post" / f"post_{post_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        post_json = json.loads((staged_dir / "post.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠️  Could not read staged post.json for {post_id}: {e}")
        post_json = {}

    # Keep every image — no category filter anymore.
    (dest_dir / "post.json").write_text(
        json.dumps(post_json, indent=2, ensure_ascii=False), encoding="utf-8")

    keep_orig_names = {img["original_filename"] for img in post_json.get("images") or []
                       if img.get("original_filename")}
    keep_proc_names = {img["processed_filename"] for img in post_json.get("images") or []
                       if img.get("processed_filename")}

    # Copy the originals + processed images into the post dir.
    for sub, names in (("original", keep_orig_names), ("processed", keep_proc_names)):
        src_dir = staged_dir / "images" / sub
        if not src_dir.exists() or not names:
            continue
        dst_dir = dest_dir / "images" / sub
        dst_dir.mkdir(parents=True, exist_ok=True)
        for f in src_dir.iterdir():
            if f.is_file() and f.name in names:
                try:
                    shutil.copy2(f, dst_dir / f.name)
                except Exception as e:
                    print(f"  ⚠️  Could not copy {f.name} into {brand_slug}/{category}/post/post_{post_id}/: {e}")

    analysis_dir = dest_dir / "analysis"
    analysis_dir.mkdir(exist_ok=True)
    (analysis_dir / "image_analysis.json").write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")

    # Flat copy for quick browsing + the brand-wide combined mapping.
    flat_images_dir = category_dir / "images"
    flat_images_dir.mkdir(exist_ok=True)
    for name in sorted(keep_orig_names):
        src = dest_dir / "images" / "original" / name
        if src.exists():
            try:
                shutil.copy2(src, flat_images_dir / f"post_{post_id}_{name}")
            except Exception as e:
                print(f"  ⚠️  Could not copy {name} into {brand_slug}/{category}/images/: {e}")

    category_analysis_file = category_dir / "image_analysis.json"
    combined = {}
    if category_analysis_file.exists():
        try:
            combined = json.loads(category_analysis_file.read_text(encoding="utf-8"))
        except Exception:
            combined = {}
    combined.update(mapping)
    category_analysis_file.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  📁 Kept {len(mapping)} image(s) of post {post_id} → {brand_slug}/{category}/")

    shutil.rmtree(staged_dir, ignore_errors=True)
    return dict(mapping)


def discover_post_url(url: str, cookies: dict) -> str:
    """Discovery phase for a bare post URL: resolve its post ID only — the
    post's text/media/images are all fetched in process_post_url() below.
    Raises on resolution failure so _with_source_retries retries it."""
    post_id = fb_client.resolve_post_id(url, cookies=cookies)
    if not post_id:
        raise RuntimeError(f"Could not resolve post ID from {url}")
    return post_id


def process_post_url(job_dir: Path, post_id: str, url: str, cookies: dict, skip_ids: set,
                     brand_filter, brand_name: str) -> tuple[str | None, dict]:
    """Full processing for one bare post URL: meta fetch → brand check →
    album download via media_id → process/analyze → organize. The brand
    filter here is the effective (limit-aware) one — this is the only place
    a post URL's text is checked, so the testing cap applies to post URLs
    too."""
    if post_id in skip_ids:
        print(f"  ⏭️  Already processed in a previous run: {post_id}")
        return None, {}
    if post_already_output(job_dir, post_id):
        print(f"  ⏭️  Already processed in this job: {post_id}")
        return None, {}

    meta = fetch_post_meta_from_html(url, cookies)

    text_brand = brand_mapping.detect_single_brand(meta.get("text"))
    print(f"  🏷️  Detected brand from post text: {text_brand or '(none / ambiguous)'}")
    if not text_brand:
        print(f"  🗑️  Skipping post {post_id} — no single retailer brand detected in post text")
        return post_id, {}
    if not brand_filter(meta.get("text")):
        print(f"  🗑️  Skipping post {post_id} — brand '{text_brand}' does not match this workspace's brand")
        return post_id, {}

    post_info = None
    comments = []
    if FETCH_COMMENTS:
        print("[STAGE] fetching_comments")
        try:
            comments, post_info = fb_client.fetch_comments_for_post(post_id, cookies=cookies)
        except Exception as e:
            print(f"  ⚠️  Comment fetch failed for {post_id}: {e}")
    else:
        # Comment scraping is disabled, but the comments API response is
        # still the only source of media_id (needed for images) for a bare
        # post URL — one request (first page, no replies) is enough since
        # media_id comes from the first comment edge.
        try:
            _, post_info = fb_client.fetch_comments_for_post(post_id, cookies=cookies,
                                                             max_pages=1, with_replies=False)
        except Exception as e:
            print(f"  ⚠️  Could not resolve media_id for {post_id}: {e}")

    staging_root = job_dir / "_staging"
    image_paths = []
    if post_info and post_info.get("media_id"):
        print("[STAGE] downloading_images")
        orig_dir = staging_root / f"post_{post_id}" / "images" / "original"
        try:
            image_paths = fb_client.fetch_single_post_images(
                post_info["media_id"], post_id, str(orig_dir), cookies, max_images=MAX_IMAGES_PER_POST
            )
        except Exception as e:
            print(f"  ⚠️  Image fetch failed for {post_id}: {e}")

    mapping = finalize_post(
        staging_root, post_id, post_url=url, page_url=url,
        page_name=meta.get("page_name"), post_text=meta.get("text"),
        published_at=meta.get("published_at"), comment_count=len(comments),
        comments=comments, orig_image_paths=image_paths, brand_name=brand_name,
    )
    written = organize_or_discard(job_dir, staging_root, post_id, text_brand, mapping)
    return post_id, written


def discover_page_or_group_posts(job_dir: Path, url: str, src_type: str, cookies: dict,
                                  min_comments: int, start_dt, end_dt, posts_per_source: int,
                                  brand_filter) -> dict | None:
    """Discovery phase for a page/group source: resolves the source and
    fetches its post list (text, permalink, comment count, image URLs, etc.)
    with download_images=False — every post's images are discovered/counted
    but their bytes are NOT downloaded yet.

    A brand-keyword text_filter is also passed straight into the scraper's
    fetch_posts(): it runs the instant a post's text is available, BEFORE
    that post's images are even enumerated — so a post with no single
    detected retailer never triggers extract_media() at all (see CONTEXT.md
    Design Decision 19).

    fetch_extra_images=False on top of that: for a post with more photos
    than its own GraphQL node carries, only those node photos are discovered
    here — the paginated lookup for the rest (one GraphQL request per extra
    photo) is deferred to process_page_or_group_posts() ->
    fb_client.download_pending_post_images(), via the `last_media_id`
    recorded on each post. Returns everything process_page_or_group_posts()
    below needs, or None if the source couldn't be resolved."""
    print("[STAGE] fetching_posts")
    # Unique per call (not just per URL) so a source-level retry gets a clean
    # scratch dir — otherwise the scraper's post_already_exists() check would
    # skip every post saved by the failed attempt and drop it from the
    # returned post list.
    scratch_root = job_dir / f"_scratch_{src_type}_{abs(hash(url)) % 100000}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
    text_filter = brand_filter

    if src_type == "group":
        source_id = fb_client.resolve_group_id(url, cookies=cookies)
        if not source_id:
            raise RuntimeError(f"Could not resolve group ID from {url}")
        posts = fb_client.fetch_group_posts(
            source_id, limit=posts_per_source, min_comments=min_comments,
            start_date=start_dt, end_date=end_dt, save_root=str(scratch_root),
            max_images_per_post=MAX_IMAGES_PER_POST, download_images=False,
            text_filter=text_filter, fetch_extra_images=False,
        )
        text_key, name_key = "message", "group_name"
    else:
        source_id = fb_client.resolve_page_id(url, cookies=cookies)
        if not source_id:
            raise RuntimeError(f"Could not resolve page ID from {url}")
        posts = fb_client.fetch_page_posts(
            source_id, limit=posts_per_source, min_comments=min_comments,
            start_date=start_dt, end_date=end_dt, save_root=str(scratch_root),
            max_images_per_post=MAX_IMAGES_PER_POST, download_images=False,
            text_filter=text_filter, fetch_extra_images=False,
        )
        text_key, name_key = "text", "page_name"

    return {
        "url": url, "posts": posts, "text_key": text_key, "name_key": name_key,
        "scratch_root": scratch_root, "src_type": src_type,
    }


def process_page_or_group_posts(job_dir: Path, discovered: dict, cookies: dict,
                                 skip_ids: set, brand_filter,
                                 brand_name: str) -> tuple[list[str], dict]:
    """Processing phase for one discovered page/group source: brand
    re-check, comments, image download/processing/analysis, and
    filter/organize — for every post discover_page_or_group_posts() already
    found."""
    url = discovered["url"]
    posts = discovered["posts"]
    text_key = discovered["text_key"]
    name_key = discovered["name_key"]
    scratch_root = discovered["scratch_root"]
    src_type = discovered["src_type"]

    processed_ids = []
    all_mapping = {}
    staging_root = job_dir / "_staging"

    for post in posts:
        post_id = post.get("post_id")
        if not post_id or post_id in skip_ids:
            continue
        if post_already_output(job_dir, post_id):
            continue

        # The real brand filter already ran inside discover_page_or_group_posts()'s
        # text_filter, before this post's images were even enumerated — every
        # post reaching this line already passed it. This just re-derives the
        # actual brand string (the filter only returned True/False) for
        # organize_or_discard() below; it's a defensive fallback, not the
        # primary filter point. NOTE: the plain filter is used here, NOT the
        # limit-aware one — the limiter's counter already counted this post
        # at discovery, and counting it again here would push it over the cap.
        text_brand = brand_mapping.detect_single_brand(post.get(text_key))
        relaxed_brand = discovered.get("relaxed_brand")
        if not text_brand and relaxed_brand:
            # Source URL matches the workspace's brand (e.g. facebook.com/ALDI.USA
            # for ALDI) — the source IS the brand, so a post whose text doesn't
            # name any brand is still the brand's post. Treat it as such.
            text_brand = relaxed_brand
            print(f"  🔗 Post {post_id} has no brand in text — source URL matches '{relaxed_brand}', treating as {relaxed_brand}")
        print(f"  🏷️  Post {post_id} brand: {text_brand or '(none / ambiguous)'} — text: {(post.get(text_key) or '')[:150]!r}")
        if not text_brand:
            print(f"  🗑️  Skipping post {post_id} — no single retailer brand detected in post text")
            processed_ids.append(post_id)
            continue
        if text_brand != relaxed_brand and not brand_filter(post.get(text_key)):
            print(f"  🗑️  Skipping post {post_id} — brand '{text_brand}' does not match this workspace's brand")
            processed_ids.append(post_id)
            continue

        comments = []
        if FETCH_COMMENTS:
            print("[STAGE] fetching_comments")
            try:
                comments, _ = fb_client.fetch_comments_for_post(post_id, cookies=cookies)
            except Exception as e:
                print(f"  ⚠️  Comment fetch failed for {post_id}: {e}")
                comments = []

        print("[STAGE] downloading_images")
        try:
            orig_images = fb_client.download_pending_post_images(
                src_type, post, str(scratch_root), max_images=MAX_IMAGES_PER_POST
            )
        except Exception as e:
            print(f"  ⚠️  Image download failed for {post_id}: {e}")
            orig_images = []

        mapping = finalize_post(
            staging_root, post_id,
            post_url=post.get("permalink") or url, page_url=url,
            page_name=post.get(name_key), post_text=post.get(text_key),
            published_at=post.get("published_at"),
            comment_count=post.get("comment_count", len(comments)),
            comments=comments, orig_image_paths=orig_images, brand_name=brand_name,
        )
        written = organize_or_discard(job_dir, staging_root, post_id, text_brand, mapping)
        all_mapping.update(written)
        processed_ids.append(post_id)

    shutil.rmtree(scratch_root, ignore_errors=True)
    return processed_ids, all_mapping


def _deal_data_block(deal: dict) -> str:
    """Render the extracted deal's NUMBERS as an official-data block appended
    to the infographic prompt. Only the fields the model actually writes
    itself (validity banner + savings summary panel) are injected — never the
    prose fields (name/summary), which would tempt it to create new text
    containers. The GPT-6 Luna extraction read these values in a dedicated
    pass, so they're more reliable than the generation model's own reading of
    the reference image."""
    lines = ["OFFICIAL DEAL DATA — verified extraction from the reference image. Use these "
             "exact values for the date line and the summary rows; they "
             "override anything you read yourself. Do not add any new text containers for them:",
             f"- Items with quantities: {deal.get('items') or '(not listed)'}",
             f"- Subtotal: {deal.get('subtotal') or '(not shown)'}",
             f"- Coupons used: {deal.get('coupons_used') or '$0.00'}",
             f"- Rewards: {deal.get('rewards') or '$0.00'}",
             f"- Rebates: {deal.get('rebates') or '$0.00'}",
             f"- FINAL NET COST: {deal.get('final_net_cost') or '(not shown)'}",
             f"- VALIDITY DATE: {deal.get('validity_date') or '(not shown)'} — use this exact "
             f"date/range for the validity banner; omit the banner entirely if '(not shown)'"]
    return "\n" + "\n".join(lines)


def generate_deal_infographics(job_dir: Path, brand: str, brand_slug: str | None) -> int:
    """AI deal-infographic stage — turns each single-deal image into a
    branded coupon-deal infographic via KIE (backend: constants.AI_IMAGE_BACKEND),
    replacing the original in <brand-slug>/deals/images/.

    Only images with exactly ONE extracted deal (analysis_status "success")
    are generated. no_deal / multiple_deals / failed / skipped entries are
    skipped — one infographic can't carry multiple deals, and a no-deal
    image has nothing to render (multi-deal deals stay stored in the entry
    for reference). The prompt is constants.DEAL_INFOGRAPHIC_PROMPT with
    {brand name} substituted from the workspace brand, plus the extracted
    deal's numbers (dates/items/prices/coupons/final cost) as an
    official-data block for the validity banner and savings summary — the
    reference image itself stays the visual source of truth (products and
    coupon panels are placed as-is). Toggled by constants.GENERATE_AI_IMAGES.
    Returns the number of images regenerated. Never raises per image — a
    failure just keeps the original."""
    if not GENERATE_AI_IMAGES:
        return 0
    if not brand_slug:
        print("⚠️  No brand configured — skipping AI deal-infographic generation.")
        return 0

    deals_dir = job_dir / brand_slug / "deals"
    analysis_file = deals_dir / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    images_dir = deals_dir / "images"
    prompt = (DEAL_INFOGRAPHIC_PROMPT
              .replace("{brand name}", brand or "")
              .replace("{inputs_block}", DEAL_INPUTS_BLOCK))

    # REQUIRED layout template: assets/templates/<brand-slug>.jpg — uploaded
    # ONCE per job and attached as the FIRST reference image of every task
    # (the model copies its layout so all infographics stay visually
    # consistent); the deal image (second) stays the content source of truth.
    # A brand without its template is a config error — the stage is skipped.
    template_path = Path(__file__).resolve().parent / INFOGRAPHIC_TEMPLATE_DIR / f"{brand_slug}.jpg"
    if not template_path.exists():
        print(f"⚠️  No layout template for {brand_slug} at {template_path} — create it "
              f"(assets/templates/<brand-slug>.jpg) — skipping AI deal-infographic generation.")
        return 0
    try:
        template_url = generate.upload_image(str(template_path))
        print(f"  📐 Layout template attached: {template_path} → {template_url}")
    except Exception as e:
        print(f"  ⚠️  Template upload failed ({e}) — skipping AI deal-infographic generation.")
        return 0

    eligible: dict = {}
    skipped: dict[str, int] = {}
    seen_deal_names: set[str] = set()
    for filename, entry in analysis.items():
        # Key off the deal count (what analysis_status is derived from) so
        # entries written before the status field existed still qualify.
        deal_count = len(entry.get("deals") or [])
        name_key = (publish_wordpress.normalize_deal_name(
            (entry["deals"][0].get("name") or "")) if deal_count == 1 else "")
        if deal_count == 1 and (images_dir / filename).exists() and name_key not in seen_deal_names:
            eligible[filename] = entry
            seen_deal_names.add(name_key)
        elif deal_count == 1 and name_key in seen_deal_names:
            skipped["duplicate_deal"] = skipped.get("duplicate_deal", 0) + 1
        else:
            label = (entry.get("analysis_status")
                     or ("multiple_deals" if deal_count > 1 else "no_deal"))
            skipped[label] = skipped.get(label, 0) + 1
    if skipped:
        summary = ", ".join(f"{s}: {n}" for s, n in sorted(skipped.items()))
        print(f"⏭️  Skipping infographic generation for {sum(skipped.values())} image(s) — {summary}")
    if not eligible:
        print("ℹ️  No single-deal images to turn into infographics.")
        return 0

    scratch_dir = job_dir / f"_ai_{brand_slug}"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 70)
    print(f"AI DEAL INFOGRAPHICS — generating {len(eligible)} image(s) via KIE nano-banana-2")
    print("=" * 70)

    generated = 0

    # Parallel AI generation: AI_IMAGE_WORKERS threads each run the full
    # upload→createTask→poll→download pipeline for a different image, so the
    # long poll waits (15-20s per task) overlap instead of stacking up. All
    # KIE calls are paced through the shared thread-safe rate limiter, and
    # each worker touches only its own files + its own analysis entry, so
    # there is no shared mutable state. The JSON write below happens once,
    # after all workers finish.
    def _generate_one(item):
        filename, entry = item
        img_path = images_dir / filename

        if entry.get("ai_image"):  # already generated in a previous run — don't regenerate
            return False
        try:
            deal = (entry.get("deals") or [{}])[0]
            image_prompt = prompt + _deal_data_block(deal if isinstance(deal, dict) else {})
            public_url = generate.upload_image(str(img_path))
            rate_limiter.acquire()
            image_urls = [template_url, public_url] if template_url else public_url
            task_id = generate.create_task(image_urls, prompt=image_prompt)
            result_url = generate.poll_task(task_id)
            result_path = scratch_dir / f"result_{filename}"
            generate.download_image(result_url, str(result_path))
            compress_under_limit(str(result_path), AI_IMAGE_MAX_BYTES)
            if AI_IMAGE_COMPARE:
                # Comparison sheet replaces the published image: AI result
                # on top, original below, both labeled — easy to compare.
                if make_comparison_image(result_path, img_path, result_path):
                    print(f"  🔀 {filename} — comparison sheet (AI top / original below)")
            shutil.move(str(result_path), str(img_path))
            entry["ai_image"] = True
            print(f"  🎨 {filename} — deal infographic generated")
            return True
        except Exception as e:
            print(f"  ⚠️  AI infographic generation failed for {filename}: {e} — keeping the original")
            return False

    with ThreadPoolExecutor(max_workers=AI_IMAGE_WORKERS) as executor:
        for ok in executor.map(_generate_one, eligible.items()):
            if ok:
                generated += 1

    shutil.rmtree(scratch_dir, ignore_errors=True)
    try:
        analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Could not write AI-image flags back to {analysis_file}: {e}")
    print(f"✅ AI deal infographics: {generated}/{len(eligible)} image(s) regenerated.")
    return generated


def main():
    parser = argparse.ArgumentParser(description="Facebook product-image analysis + publishing pipeline")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--sources-file", required=True, help="JSON file: [{\"url\":..., \"type\": \"page|group|post|null\"}]")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD (page/group sources only)")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD (page/group sources only, default: today)")
    parser.add_argument("--min-comments", type=int, default=0)
    parser.add_argument("--skip-post-ids-file", default=None, help="JSON list of post_ids to skip (cross-run dedup)")
    parser.add_argument("--brand", default=None,
                        help="The workspace's single canonical retailer brand — only posts "
                             "whose detected brand matches are scraped/published")
    parser.add_argument("--posts-per-source", type=int, default=POSTS_PER_SOURCE_DEFAULT)
    parser.add_argument("--brand-post-limit", type=int, default=0,
                        help="Testing cap: max posts accepted across all sources "
                             "(0 = unlimited).")
    parser.add_argument("--output-root", default="output")
    parser.add_argument("--publish-target", default="retailshout", choices=["retailshout", "aos"])
    parser.add_argument("--page-id", default=None,
                        help="WordPress page ID — publishing is skipped when absent")
    parser.add_argument("--page-title", default=None,
                        help="WordPress page title template ({brand}/{date_range} placeholders, workspace config)")
    parser.add_argument("--week-start", default=None,
                        help="Week start day for the page title's date window (e.g. friday for ALDI, tuesday for Publix)")
    args = parser.parse_args()

    brand_slug = brand_mapping.brand_slug(args.brand) if args.brand else None

    brand_filter = make_brand_filter(args.brand)
    if args.brand:
        print(f"Brand filter: only {args.brand} posts pass — every other brand's posts are skipped.")
    else:
        print("Brand filter: no brand configured — every single-detected brand passes.")
    effective_filter = make_per_brand_limiter(brand_filter, args.brand_post_limit)
    if args.brand_post_limit and args.brand_post_limit > 0:
        print(f"Post limit: max {args.brand_post_limit} post(s) (testing mode).")

    skip_ids = set()
    if args.skip_post_ids_file and Path(args.skip_post_ids_file).exists():
        skip_ids = set(json.loads(Path(args.skip_post_ids_file).read_text(encoding="utf-8")))

    start_dt = parse_date_arg(args.start_date, end_of_day=False)
    end_dt = parse_date_arg(args.end_date, end_of_day=True) or datetime.now(timezone.utc)

    fb_auth = load_fb_auth()
    cookies, _proxies = fb_client.apply_auth(fb_auth.get("cookie_string", ""), fb_auth.get("fb_dtsg", ""))
    if not cookies:
        print("⚠️  No Facebook session cookie configured (Settings page) — public-only scraping, may be unreliable.")

    sources = json.loads(Path(args.sources_file).read_text(encoding="utf-8"))

    job_dir = Path(args.output_root) / args.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def _publish_dest(category: str, page_id: str | None, target: str) -> str:
        return f"{category} → {target} (page {page_id})" if page_id else f"{category} → not configured (skipped)"

    print("=" * 70)
    print(f"Facebook pipeline — job {args.job_id}")
    print(f"Brand: {args.brand or '(any)'}  |  Extraction: coupon deals (food/non-food categories disabled)")
    print(f"Sources: {len(sources)}  |  Date window: {start_dt or '(open)'} → {end_dt}  |  "
          f"Min comments: {args.min_comments}")
    print("=" * 70)

    # ── Phase 1: discovery — scrape every post link from every source first,
    # before any per-post brand detection / image processing / KIE analysis
    # starts. This surfaces "how much is there to process" up front instead
    # of interleaving it with the (much slower) processing phase.
    print("\n" + "=" * 70)
    print("PHASE 1/3 — Discovering posts from all sources")
    print("=" * 70)

    discovered_post_sources: list[tuple[str, str]] = []       # (url, post_id)
    discovered_page_group_sources: list[dict] = []            # discover_page_or_group_posts() results
    total_found = 0

    def _discover_source(idx: int, src: dict):
        """Discovery for ONE source — runs in a worker thread. Returns
        ('post', url, post_id) / ('page_group', discovered_dict) / None."""
        url = src.get("url", "").strip()
        if not url:
            return None
        src_type = src.get("type")
        if src_type not in ("post", "page", "group"):
            print(f"⚠️  Source has no valid type set ({src_type!r}) — defaulting to 'page'. "
                  f"Check workspaces.json if this wasn't submitted through the UI.")
            src_type = "page"
        print(f"\n[{idx}/{len(sources)}] Discovering: {url}  (type={src_type})")

        try:
            if src_type == "post":
                post_id = _with_source_retries(url, lambda: discover_post_url(url, cookies))
                if post_id:
                    print(f"  🔗 Found 1 post")
                    return ("post", url, post_id)
                return None

            # A source URL that contains the brand name (e.g.
            # facebook.com/ALDI.USA for brand ALDI) IS the brand's own
            # page/group — its posts often don't name the brand in the
            # text, so accept no-brand posts there too. Posts detecting a
            # DIFFERENT brand are still rejected.
            def _relaxed(text, _brand=args.brand):
                return brand_mapping.detect_single_brand(text) in (_brand, None)
            relaxed = bool(args.brand) and args.brand.lower() in url.lower()
            src_filter = make_per_brand_limiter(_relaxed, args.brand_post_limit) if relaxed else effective_filter
            if relaxed:
                print("  🔗 Source URL contains the brand — posts without brand text are accepted too")
            discovered = _with_source_retries(url, lambda: discover_page_or_group_posts(
                job_dir, url, src_type, cookies, args.min_comments,
                start_dt, end_dt, args.posts_per_source, src_filter,
            ))
            if discovered is not None:
                discovered["relaxed_brand"] = args.brand if relaxed else None
                print(f"  🔗 Found {len(discovered['posts'])} post(s)")
                return ("page_group", discovered)
        except Exception as e:
            print(f"❌ Discovery failed entirely for source: {url}: {e}")
        return None

    # Parallel source discovery: PAGE_SCAN_WORKERS threads scan different
    # page/group URLs concurrently — the post-list pagination of one source
    # (the slow part) overlaps the others'. The scrapers are per-call
    # thread-safe (page/group id passed through, not set on module globals),
    # and results are merged back in the main thread in source order.
    worker_count = max(1, min(PAGE_SCAN_WORKERS, len(sources)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for result in executor.map(
            lambda pair: _discover_source(pair[0], pair[1]),
            enumerate(sources, start=1),
        ):
            if result is None:
                continue
            if result[0] == "post":
                _, url, post_id = result
                discovered_post_sources.append((url, post_id))
                total_found += 1
            else:
                discovered = result[1]
                discovered_page_group_sources.append(discovered)
                total_found += len(discovered["posts"])

    print(f"\n✅ Discovery complete — found {total_found} post(s) across "
          f"{len(discovered_post_sources) + len(discovered_page_group_sources)}/{len(sources)} source(s).")

    # Full list of discovered post links (log + discovered_posts.txt in the
    # job dir) so the run can be verified at a glance.
    link_lines = []
    for url, _post_id in discovered_post_sources:
        link_lines.append(url)
    for discovered in discovered_page_group_sources:
        if not discovered["posts"]:
            continue
        link_lines.append(f"{discovered['url']}  ({len(discovered['posts'])} post(s))")
        for post in discovered["posts"]:
            link = post.get("permalink") or f"https://www.facebook.com/{post.get('post_id')}"
            link_lines.append(f"    • {link}")
    if link_lines:
        print("\n📋 Discovered post links:")
        for line in link_lines:
            print(f"  {line}")
        try:
            (job_dir / "discovered_posts.txt").write_text("\n".join(link_lines) + "\n", encoding="utf-8")
        except Exception as e:
            print(f"⚠️  Could not write discovered_posts.txt: {e}")

    # ── Phase 2: processing — for everything Phase 1 found: extra-image
    # discovery via last_media_id → download → process → KIE deal analysis →
    # image-count filter → organize into <brand-slug>/deals/post_<id>/.
    print("\n" + "=" * 70)
    print("PHASE 2/3 — Processing discovered posts")
    print("=" * 70)

    all_processed_ids: list[str] = []
    all_mapping: dict = {}

    for url, post_id in discovered_post_sources:
        print(f"\nProcessing post: {url}")
        result = _with_source_retries(
            url, lambda: process_post_url(job_dir, post_id, url, cookies, skip_ids,
                                          effective_filter, args.brand))
        if result:
            processed_id, mapping = result
            if processed_id:
                all_processed_ids.append(processed_id)
                all_mapping.update(mapping)

    for discovered in discovered_page_group_sources:
        print(f"\nProcessing source: {discovered['url']}  ({len(discovered['posts'])} post(s))")
        result = _with_source_retries(
            discovered["url"],
            lambda d=discovered: process_page_or_group_posts(job_dir, d, cookies, skip_ids,
                                                             brand_filter, args.brand))
        if result:
            ids, mapping = result
            all_processed_ids.extend(ids)
            all_mapping.update(mapping)

    shutil.rmtree(job_dir / "_staging", ignore_errors=True)

    # Number every entry (1, 2, 3, ...) in insertion order for easy later reference —
    # only in the job-wide merged mapping, not the per-post analysis.json files.
    indexed_mapping = {
        filename: {"index": i, **entry}
        for i, (filename, entry) in enumerate(all_mapping.items(), start=1)
    }
    kept_post_ids = sorted({entry["post_id"] for entry in all_mapping.values()})

    (job_dir / "image_analysis_mapping.json").write_text(
        json.dumps(indexed_mapping, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (job_dir / "manifest.json").write_text(
        json.dumps({
            "phase": "pipeline",
            "brand": args.brand, "brand_slug": brand_slug,
            "processed_post_ids": all_processed_ids, "post_count": len(all_processed_ids),
            "kept_post_ids": kept_post_ids, "kept_count": len(kept_post_ids),
        }, indent=2),
        encoding="utf-8",
    )

    print(f"\n✅ Processing done. Processed {len(all_processed_ids)} post(s) across {len(sources)} source(s) — "
          f"{len(kept_post_ids)} kept after brand/image filtering.")

    # ── Phase 3: AI deal infographics + publishing. Each single-deal image
    # is regenerated as a branded coupon-deal infographic
    # (constants.DEAL_INFOGRAPHIC_PROMPT, {brand name} → brand); no_deal /
    # multiple_deals / failed images are skipped. Then the deals publish to
    # the workspace's single WordPress destination (--publish-target /
    # --page-id). The remaining old stages (dedupe, scraper analysis,
    # product cap) stay disabled (Design Decision 42) until they are
    # reworked around the deal schema.
    if not kept_post_ids:
        print("\nℹ️  No posts kept — nothing organized.")
        print("[STAGE] done")
        return

    print("\n" + "=" * 70)
    print("PHASE 3/3 — AI deal infographics + publishing")
    print("=" * 70)
    print("[STAGE] generating_ai_images")
    generate_deal_infographics(job_dir, args.brand, brand_slug)

    if not brand_slug:
        print("\nℹ️  No brand configured — skipping publishing.")
    elif not args.page_id:
        print("\nℹ️  No WordPress page ID configured — skipping publish step.")
    else:
        print("\n" + "=" * 70)
        print(f"PUBLISHING deals to {args.publish_target} (page {args.page_id})")
        print("=" * 70)
        print("[STAGE] publishing")
        ok, message = publish_brand(
            parent_job_id=args.job_id,
            brand=args.brand,
            brand_slug=brand_slug,
            page_id=args.page_id,
            publish_target=args.publish_target,
            output_root=args.output_root,
            status="publish",
            page_title=args.page_title,
            week_start_day=args.week_start,
        )
        if not ok:
            print(f"❌ Publish failed: {message}")
            sys.exit(1)

    print("[STAGE] done")


if __name__ == "__main__":
    main()
