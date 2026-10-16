#!/usr/bin/env python3
"""
YouTube pipeline — single end-to-end job for youtube workspaces. Invoked as
a subprocess by job_runner.py (same job record / log / queue as the Facebook
pipeline). One job does everything for the workspace's ONE brand:

  1. DISCOVERY — video URLs are taken as-is; channel URLs are scanned via
     the YouTube Data API (uploads between the week-start weekday window
     and now), capped at YT_MAX_VIDEOS_PER_CHANNEL newest videos per
     channel. Duplicate video IDs across sources are dropped.
  2. TRANSCRIPTS + DEAL EXTRACTION — per video: transcript via
     youtube_transcript_api (YT_PROXY aware, retried), then coupon-deal
     extraction via xAI Grok (youtube.deal_extract) chained with
     previous_response_id so a deal repeated across videos is extracted
     only once. Every deal is tagged with its video metadata.
  3. DEAL SPLIT + ORGANIZE — the deals arrive as one flat array (one
     object per deal, ready for per-deal configuration) and are organized
     into the SAME output structure the Facebook pipeline produces:
     output/<job_id>/<brand-slug>/deals/ with images/ +
     image_analysis.json (one entry per deal, analysis_status "success").
  4. AI DEAL INFOGRAPHICS + PUBLISHING — YouTube deals have NO reference
     photo, so each deal's branded infographic is generated in ONE
     text-to-image step: the per-brand layout template (IMAGE 1) +
     constants.YT_INFOGRAPHIC_PROMPT + the deal's official data block →
     the model draws the listed products into the product area and fills
     the summary rows (one KIE generation per deal — the facebook flow's
     product-image → infographic two-step would cost two). Publishing is
     reused unchanged from the Facebook pipeline
     (publish_wordpress.publish_brand — same feature-image templates, same
     WordPress page rendering).

One failing video never aborts the whole job — failures are logged and
the run continues.
"""

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from constants import (
    AI_IMAGE_BACKEND, AI_IMAGE_MAX_BYTES, AI_IMAGE_WORKERS, GENERATE_AI_IMAGES,
    INFOGRAPHIC_TEMPLATE_DIR, MAX_DEALS_PER_JOB,
    YT_VIDEO_LIMIT, YT_TRANSCRIPT_RETRIES, YT_INFOGRAPHIC_PROMPT,
)
from image_pipeline import compress_under_limit
from kie_ratelimit import rate_limiter
import brand_mapping
import generate
import gpt_generate
import publish_wordpress
from publish_wordpress import publish_brand, normalize_deal_name
from youtube import fetch_transcript, extract_coupons, get_channel_videos_since_weekday
from youtube.channel_scan import get_videos_metadata

# AI image-generation backend (constants.AI_IMAGE_BACKEND) — same switch as
# run_facebook.py: both modules share the interface (upload_image / create_task /
# poll_task / download_image), so the infographic stage below stays untouched.
if AI_IMAGE_BACKEND == "gpt":
    generate = gpt_generate


# ── PHASE 1: DISCOVERY ────────────────────────────────────────────────────────

def _brand_filter_videos(videos: list[dict], brand: str,
                         relaxed_channel_urls: set[str]) -> list[dict]:
    """Brand text filter — the youtube equivalent of the facebook pipeline's
    post-text brand filter: a video is only processed when its TITLE (or,
    failing that, its DESCRIPTION) names the workspace's brand
    (brand_mapping, same keyword variants: #WalmartFinds, wal-mart, wags,
    ...). Otherwise a multi-store channel (one channel posts CVS, Meijer AND
    Walgreens videos) would pour every store's deals into one workspace.

    TITLE-FIRST rule — youtube descriptions routinely mention several stores
    (channel promos, hashtags like "follow for Walmart AND Sam's Club
    deals"), so facebook's strict exactly-one-brand rule would wrongly
    reject a Walmart-titled video whose description also names Sam's Club:
      1. title names the workspace brand (among any others) → ACCEPT
      2. title names only other store(s) → REJECT (the video is about them)
      3. title names no store → accept when the DESCRIPTION names the brand
      4. nothing anywhere → accept only for relaxed channels (channel URL
         contains the brand name — it IS the brand's own channel)"""
    kept: list[dict] = []
    for v in videos:
        vid = v["video_id"]
        title = v.get("title") or ""
        description = v.get("description") or ""
        title_brands = brand_mapping.detect_brands(title)
        desc_brands = brand_mapping.detect_brands(description)
        print(f"  🏷️  Video {vid} — title brands: {sorted(title_brands) or '(none)'} — "
              f"text: {title[:100]!r}")

        if brand in title_brands:
            kept.append(v)
        elif title_brands:
            print(f"  🗑️  Skipping video {vid} — title is about "
                  f"{', '.join(sorted(title_brands))}, not {brand}")
        elif brand in desc_brands:
            print(f"  🔗 Brand '{brand}' found in the description (title names no store)")
            kept.append(v)
        elif desc_brands:
            print(f"  🗑️  Skipping video {vid} — description names "
                  f"{', '.join(sorted(desc_brands))}, not {brand}")
        elif v.get("channel_url") in relaxed_channel_urls:
            print(f"  🔗 Channel URL matches '{brand}' — no-brand video accepted as {brand}")
            kept.append(v)
        else:
            print(f"  🗑️  Skipping video {vid} — no retailer brand detected in title/description")
    return kept


def discover_videos(sources: list[dict], week_start: str | None,
                    brand: str) -> list[dict]:
    """Resolve every source into a deduped, brand-filtered video list,
    NEWEST first — the newest deals are the most valid/relevant, and the
    MAX_DEALS_PER_JOB early-stop therefore saves the oldest videos from
    being processed.

    video sources are metadata-fetched (title/description, needed for the
    brand filter) and pass through the same filter; channel sources are
    scanned within the week-start weekday window (weekday → now, starting
    from that week's Wednesday). A failed channel scan skips that source —
    never aborts the job."""
    weekday = (week_start or "saturday").strip().lower()
    videos: list[dict] = []
    seen_ids: set[str] = set()

    # Relaxed channels: channel URL contains the brand name.
    relaxed_channel_urls = {
        s["url"] for s in sources
        if s.get("type") == "channel" and brand and brand.lower() in (s.get("url") or "").lower()
    }
    for url in relaxed_channel_urls:
        print(f"  🔗 Channel URL contains the brand — its no-brand videos are accepted too: {url}")

    for idx, src in enumerate(sources, start=1):
        url = (src.get("url") or "").strip()
        src_type = src.get("type")
        if not url:
            continue
        print(f"\n[{idx}/{len(sources)}] Discovering: {url}  (type={src_type})")

        try:
            if src_type == "video":
                video_id = _video_id_from_url(url)
                meta = get_videos_metadata([video_id]).get(video_id) or {}
                found = [{
                    "video_id": video_id,
                    "title": meta.get("title") or url,
                    "description": meta.get("description") or "",
                    "published_at": meta.get("published_at"),
                    "thumbnail": meta.get("thumbnail"),
                    "url": url,
                    "source": "video",
                }]
            elif src_type == "channel":
                scan = get_channel_videos_since_weekday(url, weekday)
                print(f"  📺 Channel: {scan['channel']['channel_name']} — "
                      f"{scan['video_count']} video(s) since {scan['start_date'][:10]}")
                found = [{**v, "source": "channel", "channel_url": url}
                         for v in scan["videos"]]
            else:
                print(f"  ⚠️  Unknown source type {src_type!r} — skipping")
                continue
        except Exception as e:
            print(f"  ❌ Discovery failed for {url}: {e}")
            continue

        for v in found:
            if v["video_id"] and v["video_id"] not in seen_ids:
                seen_ids.add(v["video_id"])
                videos.append(v)

    if brand:
        videos = _brand_filter_videos(videos, brand, relaxed_channel_urls)
    return videos  # newest first (channel scans return newest-first)


def _video_id_from_url(url: str) -> str:
    from youtube.transcript import extract_video_id
    return extract_video_id(url)


# ── PHASE 2: TRANSCRIPTS + DEAL EXTRACTION ────────────────────────────────────

def fetch_transcript_with_retries(video_url: str, output_file: Path) -> bool:
    for attempt in range(1, YT_TRANSCRIPT_RETRIES + 1):
        try:
            fetch_transcript(video_url, output_file=str(output_file))
            return True
        except Exception as e:
            if attempt < YT_TRANSCRIPT_RETRIES:
                print(f"  ⚠️  Transcript attempt {attempt}/{YT_TRANSCRIPT_RETRIES} failed: {e} — retrying in 5s")
                time.sleep(5)
            else:
                print(f"  ⚠️  Transcript failed after {YT_TRANSCRIPT_RETRIES} attempts: {e} — skipping video")
    return False


def extract_video_deals(videos: list[dict], work_dir: Path) -> list[dict]:
    """Transcript + deal extraction per video, NEWEST first (sequential —
    the extraction chains previous_response_id across videos for
    cross-video dedup). Stops early once MAX_DEALS_PER_JOB valid deals
    have been extracted — the remaining older videos are never fetched.
    Returns the flat all-deals array (capped), every deal tagged with its
    video."""
    all_deals: list[dict] = []
    previous_response_id = None

    for idx, video in enumerate(videos, start=1):
        if MAX_DEALS_PER_JOB and len(all_deals) >= MAX_DEALS_PER_JOB:
            print(f"\n✂️  Deal cap reached ({MAX_DEALS_PER_JOB} valid deals) — "
                  f"stopping early, {len(videos) - idx + 1} older video(s) not processed")
            break

        video_id = video["video_id"]
        url = video["url"]
        video_dir = work_dir / f"video_{idx:02d}_{video_id}"
        video_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n[{idx}/{len(videos)}] {video.get('title') or video_id}")
        print(f"  URL: {url}")

        print("  [transcript] fetching...")
        if not fetch_transcript_with_retries(url, video_dir / "transcript.json"):
            shutil.rmtree(video_dir, ignore_errors=True)
            continue

        print(f"  [extract] extracting coupon deals"
              f" (previous_response_id={previous_response_id or 'none'})...")
        try:
            _, response_id = extract_coupons(
                str(video_dir / "transcript.json"),
                video_id,
                output_file=str(video_dir / "deals.json"),
                previous_response_id=previous_response_id,
            )
            previous_response_id = response_id
        except Exception as e:
            print(f"  ⚠️  Deal extraction failed for {url}: {e} — skipping video")
            continue

        video_deals = json.loads((video_dir / "deals.json").read_text(encoding="utf-8"))
        for deal in video_deals:
            deal["video_id"] = video_id
            deal["video_url"] = url
            deal["video_title"] = video.get("title") or ""
        all_deals.extend(video_deals)
        print(f"  → {len(video_deals)} deal(s) (running total: {len(all_deals)})")

    if MAX_DEALS_PER_JOB:
        all_deals = all_deals[:MAX_DEALS_PER_JOB]
    return all_deals


# ── PHASE 3: DEAL SPLIT + ORGANIZE (facebook-compatible structure) ────────────

def organize_deals(job_dir: Path, brand_slug: str, all_deals: list[dict]) -> tuple[list[dict], dict]:
    """Split the extracted deals into one flat per-deal array (deals.json)
    and organize them into the SAME structure the Facebook pipeline's
    later stages read: <brand-slug>/deals/images/ + image_analysis.json
    with one entry per deal (analysis_status "success"). Deal names are
    deduped (first occurrence wins — same rule as publishing). Returns
    (kept deals, image_analysis mapping)."""
    deals_dir = job_dir / brand_slug / "deals"
    deals_dir.mkdir(parents=True, exist_ok=True)
    (deals_dir / "images").mkdir(exist_ok=True)

    kept: list[dict] = []
    mapping: dict = {}
    seen_names: set[str] = set()

    for deal in all_deals:
        name_key = normalize_deal_name(deal.get("name") or "")
        if name_key and name_key in seen_names:
            print(f"  ⏭️  Duplicate deal '{deal.get('name')}' — skipping (first occurrence wins)")
            continue
        if name_key:
            seen_names.add(name_key)
        kept.append(deal)

    (deals_dir / "deals.json").write_text(
        json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")

    for i, deal in enumerate(kept, start=1):
        filename = f"deal_{i:03d}.jpg"
        mapping[filename] = {
            "index": i,
            "post_id": deal.get("video_id") or "",
            "deals": [deal],
            "analysis_status": "success",
        }

    (deals_dir / "image_analysis.json").write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n📁 Organized {len(kept)} deal(s) → {brand_slug}/deals/ (deals.json + image_analysis.json)")
    return kept, mapping


# ── PHASE 4: ONE-STEP AI DEAL INFOGRAPHICS (text-to-image, no reference) ──────

def _deal_data_block(deal: dict) -> str:
    """Render the extracted deal's NUMBERS as an official-data block appended
    to the infographic prompt. Only the fields the model actually writes
    itself (validity banner + savings summary panel) are injected — never the
    prose fields (name/summary), which would tempt it to create new text
    containers."""
    lines = ["OFFICIAL DEAL DATA — verified extraction from the video transcript. Use these "
             "exact values for the date line and the summary rows; they "
             "override anything you would write yourself. Do not add any new text containers for them:",
             f"- Items with quantities: {deal.get('items') or '(not listed)'}",
             f"- Subtotal: {deal.get('subtotal') or '(not shown)'}",
             f"- Coupons used: {deal.get('coupons_used') or '$0.00'}",
             f"- Rewards: {deal.get('rewards') or '$0.00'}",
             f"- Rebates: {deal.get('rebates') or '$0.00'}",
             f"- FINAL NET COST: {deal.get('final_net_cost') or '(not shown)'}",
             f"- VALIDITY DATE: {deal.get('validity_date') or '(not shown)'} — use this exact "
             f"date/range for the validity banner; omit the banner entirely if '(not shown)'"]
    return "\n" + "\n".join(lines)


def generate_deal_infographics(job_dir: Path, brand: str, brand_slug: str) -> int:
    """One-step AI deal-infographic stage for youtube deals — turns each
    deal DIRECTLY into a branded coupon-deal infographic via text-to-image
    (backend module: constants.AI_IMAGE_BACKEND-compatible generate.py),
    replacing the facebook flow's two-step product-image → infographic.

    Each generation gets the per-brand layout template as IMAGE 1 (the
    model copies its layout) and the deal's official data block appended
    to constants.YT_INFOGRAPHIC_PROMPT — the model draws the listed
    products into the product area and fills the summary rows from the
    data. One KIE generation per deal. A failed generation marks the entry
    ai_failed → the deal is skipped at publish (no fallback image exists —
    youtube deals have no reference photo). Returns the generated count."""
    if not GENERATE_AI_IMAGES:
        print("⚠️  GENERATE_AI_IMAGES is off — youtube deals have no reference photos, "
              "so there will be NO images to publish. Turn it on for youtube jobs.")
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
    prompt = YT_INFOGRAPHIC_PROMPT.replace("{brand name}", brand or "")

    # REQUIRED layout template: assets/templates/<brand-slug>.jpg — uploaded
    # ONCE per job and attached as IMAGE 1 of every task (the model copies
    # its layout so all infographics stay visually consistent). A brand
    # without its template is a config error — the stage is skipped.
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

    scratch_dir = job_dir / "_ai_youtube"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 70)
    print(f"AI DEAL INFOGRAPHICS — generating {len(analysis)} image(s) via one-step text-to-image")
    print("=" * 70)

    generated = 0

    # Parallel AI generation: AI_IMAGE_WORKERS threads each run the full
    # createTask→poll→download pipeline for a different deal (the template
    # is already uploaded), so the long poll waits overlap. All KIE calls
    # are paced through the shared thread-safe rate limiter, and each
    # worker touches only its own files + its own analysis entry — the JSON
    # write below happens once, after all workers finish.
    def _generate_one(item):
        filename, entry = item
        img_path = images_dir / filename

        if entry.get("ai_image"):  # already generated in a previous run — don't regenerate
            return False
        try:
            deal = (entry.get("deals") or [{}])[0]
            image_prompt = prompt + _deal_data_block(deal if isinstance(deal, dict) else {})
            rate_limiter.acquire()
            task_id = generate.create_task([template_url], prompt=image_prompt)
            result_url = generate.poll_task(task_id)
            result_path = scratch_dir / f"result_{filename}"
            generate.download_image(result_url, str(result_path))
            compress_under_limit(str(result_path), AI_IMAGE_MAX_BYTES)
            shutil.move(str(result_path), str(img_path))
            entry["ai_image"] = True
            entry.pop("ai_failed", None)
            print(f"  🎨 {filename} — deal infographic generated")
            return True
        except Exception as e:
            entry["ai_failed"] = True
            print(f"  ⚠️  AI infographic generation failed for {filename}: {e} — deal will be skipped at publish")
            return False

    with ThreadPoolExecutor(max_workers=AI_IMAGE_WORKERS) as executor:
        for ok in executor.map(_generate_one, analysis.items()):
            if ok:
                generated += 1

    shutil.rmtree(scratch_dir, ignore_errors=True)
    try:
        analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Could not write AI-image flags back to {analysis_file}: {e}")
    print(f"✅ AI deal infographics: {generated}/{len(analysis)} image(s) generated.")
    return generated


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="YouTube deal pipeline (channel scan → transcripts → deals → infographics → publish)")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--sources-file", required=True,
                        help='JSON file: [{"url":..., "type": "channel|video"}]')
    parser.add_argument("--brand", default=None,
                        help="The workspace's single brand — selects the layout/feature templates and output folder")
    parser.add_argument("--week-start", default=None,
                        help="Weekday name for the channel-scan window (scan looks back to that week's Wednesday)")
    parser.add_argument("--video-limit", type=int, default=None,
                        help="Testing cap: max videos processed across all sources")
    parser.add_argument("--output-root", default="output")
    parser.add_argument("--publish-target", default="retailshout", choices=["retailshout", "aos"])
    parser.add_argument("--page-id", default=None,
                        help="WordPress page ID — publishing is skipped when absent")
    parser.add_argument("--page-title", default=None,
                        help="WordPress page title template ({brand}/{date_range} placeholders, workspace config)")
    args = parser.parse_args()

    if not args.brand:
        print("❌ A brand is required for youtube jobs (it selects the templates and output folder).")
        sys.exit(1)

    import brand_mapping
    brand_slug = brand_mapping.brand_slug(args.brand)

    sources = json.loads(Path(args.sources_file).read_text(encoding="utf-8"))
    if isinstance(sources, dict):
        sources = [sources]

    job_dir = Path(args.output_root) / args.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    work_dir = job_dir / "_youtube"
    work_dir.mkdir(exist_ok=True)

    print("=" * 70)
    print(f"YouTube pipeline — job {args.job_id}")
    print(f"Brand: {args.brand}  |  Extraction: coupon deals from transcripts")
    print(f"Sources: {len(sources)}  |  Channel scan window: week starts "
          f"{(args.week_start or 'saturday')} → now")
    print("=" * 70)

    # ── Phase 1: discovery ──
    print("\n" + "=" * 70)
    print("PHASE 1/4 — Discovering videos (channels + direct video URLs)")
    print("=" * 70)
    videos = discover_videos(sources, args.week_start, args.brand)
    if args.video_limit is not None and args.video_limit > 0:
        videos = videos[:args.video_limit]
        print(f"✂️  Video limit (testing): processing only the first {args.video_limit}")

    if not videos:
        print("\n❌ No videos discovered — nothing to do.")
        (job_dir / "manifest.json").write_text(json.dumps({
            "phase": "youtube_pipeline", "brand": args.brand, "brand_slug": brand_slug,
            "processed_post_ids": [], "post_count": 0,
            "kept_post_ids": [], "kept_count": 0,
        }, indent=2), encoding="utf-8")
        print("[STAGE] done")
        return

    (job_dir / "discovered_videos.json").write_text(
        json.dumps(videos, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✅ Discovery complete — {len(videos)} video(s) to process:")
    for v in videos:
        print(f"  • {v['video_id']}  {v.get('title') or ''}")

    # ── Phase 2: transcripts + deal extraction ──
    print("\n" + "=" * 70)
    print("PHASE 2/4 — Transcripts + AI deal extraction (chained, cross-video dedup)")
    print("=" * 70)
    print("[STAGE] fetching_transcripts")
    all_deals = extract_video_deals(videos, work_dir)

    if not all_deals:
        print("\n❌ No deals extracted from any video — nothing to publish.")
        (job_dir / "manifest.json").write_text(json.dumps({
            "phase": "youtube_pipeline", "brand": args.brand, "brand_slug": brand_slug,
            "processed_post_ids": [v["video_id"] for v in videos], "post_count": len(videos),
            "kept_post_ids": [], "kept_count": 0,
        }, indent=2), encoding="utf-8")
        print("[STAGE] done")
        return

    # ── Phase 3: split + organize (facebook-compatible structure) ──
    print("\n" + "=" * 70)
    print("PHASE 3/4 — Deal split + organize")
    print("=" * 70)
    print("[STAGE] organizing_deals")
    kept_deals, _mapping = organize_deals(job_dir, brand_slug, all_deals)

    # ── Phase 4: one-step infographics + publishing ──
    print("\n" + "=" * 70)
    print("PHASE 4/4 — AI deal infographics + publishing")
    print("=" * 70)
    print("[STAGE] generating_ai_images")
    generate_deal_infographics(job_dir, args.brand, brand_slug)

    if not args.page_id:
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

    kept_ids = sorted({d.get("video_id") for d in kept_deals if d.get("video_id")})
    (job_dir / "manifest.json").write_text(json.dumps({
        "phase": "youtube_pipeline",
        "brand": args.brand, "brand_slug": brand_slug,
        "processed_post_ids": [v["video_id"] for v in videos],
        "post_count": len(videos),
        "kept_post_ids": kept_ids, "kept_count": len(kept_deals),
    }, indent=2), encoding="utf-8")

    shutil.rmtree(work_dir, ignore_errors=True)
    print("[STAGE] done")


if __name__ == "__main__":
    main()
