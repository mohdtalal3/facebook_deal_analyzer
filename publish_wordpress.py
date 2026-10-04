#!/usr/bin/env python3
"""
WordPress publisher for the Facebook pipeline — the publish stage of the
single end-to-end job (run_facebook.py calls publish_brand() in Phase 3).

Publishes the job's extracted DEALS: reads output/<job_id>/<brand-slug>/deals/
(the flat images/ folder plus its combined image_analysis.json), uploads every
single-deal image to the workspace's configured WordPress site, and updates the
page with a deals listing. Each item shows the deal NAME, the FINAL price (with
the ORIGINAL price struck through before it when known — final price only when
the original is unknown), the deal SUMMARY, and a "Show more" expander that
reveals the full details under headings — Sale, Items, Coupons to Use, What You
Get, Availability, Pro Tip, Strategy — with coupons and strategy as bullet
points. Only images with exactly one extracted deal (analysis_status
"success") are published — no_deal / multiple_deals / failed entries are
skipped, same rule as the AI-infographic stage.

Credentials come from the root .env:
  retailshout -> WP_URL_RS / WP_USERNAME_RS / WP_PASSWORD_RS
  aos         -> WP_URL / WP_USERNAME / WP_PASSWORD

Usage (normally called from run_facebook.py, not by hand):
  python publish_wordpress.py --parent-job-id <uuid> --brand "Dollar General" \
      --brand-slug dollar-general --page-id 12345 --publish-target retailshout
"""

import argparse
import json
import os
from datetime import date, timedelta
from html import escape
from pathlib import Path

import requests
from dotenv import load_dotenv

from wordpress_publisher import WordPressPublisher

# Load .env from project root (WP_URL_RS/... or WP_URL/... per publish target)
load_dotenv(Path(__file__).resolve().parent / ".env")

WEEKDAY_INDEX = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                 "friday": 4, "saturday": 5, "sunday": 6}

PUBLISH_TARGETS = {
    "retailshout": {
        "env": ("WP_URL_RS", "WP_USERNAME_RS", "WP_PASSWORD_RS"),
        "site_name": "retailshout.com",
    },
    "aos": {
        "env": ("WP_URL", "WP_USERNAME", "WP_PASSWORD"),
        "site_name": "aisleofshame.com",
    },
}

ITEMS_PER_SECTION_LIMIT = 20  # deals visible before the section "Show more" block

# Inline styles shared by every deal item.
_SUMMARY_STYLE = ("max-width:400px;margin:10px auto 0;text-align:left;font-size:14px;"
                  "line-height:1.5;color:#333;")
_DETAIL_STYLE = ("max-width:400px;margin:0 auto 6px;text-align:left;font-size:14px;"
                 "line-height:1.5;color:#333;")
_HEADING_STYLE = ("max-width:400px;margin:12px auto 4px;text-align:left;font-size:14px;"
                  "font-weight:700;color:#222;")
_UL_STYLE = ("max-width:400px;margin:4px auto 8px;text-align:left;font-size:14px;"
             "line-height:1.5;color:#333;padding-left:22px;")


def load_deals(deals_dir: Path) -> tuple[list[Path], dict]:
    """Load the flat deals images + their combined analysis mapping.
    Returns ([image paths], {filename: analysis entry})."""
    images_dir = deals_dir / "images"
    analysis_file = deals_dir / "image_analysis.json"

    analysis = {}
    if analysis_file.exists():
        try:
            analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️  Could not read {analysis_file.name}: {e}")

    if not images_dir.exists():
        return [], analysis
    images = sorted(p for p in images_dir.iterdir() if p.is_file())
    return images, analysis


def slugify(text: str) -> str:
    """Create a URL-safe anchor from a section name."""
    return text.lower().replace(" ", "-").replace("&", "and").replace("/", "-").replace("--", "-")


def price_line_html(deal: dict) -> str:
    """The deal's price line: FINAL price (red), preceded by the ORIGINAL
    price struck through when known. Original unknown → final price only;
    neither → empty string."""
    original = (deal.get("price") or "").strip()
    final = (deal.get("final_cost") or "").strip()
    if original and final and original != final:
        return (f'<s style="color:#777;">{escape(original)}</s> '
                f'<span style="color:#d40000;font-weight:600;">{escape(final)}</span>')
    shown = final or original
    if not shown:
        return ""
    return f'<span style="color:#d40000;font-weight:600;">{escape(shown)}</span>'


def _bullets_html(values, ordered: bool = False) -> str:
    """Render a deal field as bullet points. Accepts a list (the usual deal
    shape for coupons_to_use/strategy) or a string (split on ; and newlines —
    the items field's usual shape). Empty/None → empty string."""
    if isinstance(values, str):
        parts = [p.strip() for p in values.replace("\n", ";").split(";") if p.strip()]
    elif isinstance(values, list):
        parts = [str(v).strip() for v in values if str(v).strip()]
    else:
        parts = []
    if not parts:
        return ""
    tag = "ol" if ordered else "ul"
    items = "".join(f"<li>{escape(p)}</li>" for p in parts)
    return f'<{tag} style="{_UL_STYLE}">{items}</{tag}>'


def _paragraph_html(value) -> str:
    """One escaped paragraph for a deal field (or empty string)."""
    text = str(value or "").strip()
    if not text:
        return ""
    return f'<p style="{_DETAIL_STYLE}">{escape(text)}</p>'


def _section(heading: str, body_html: str) -> str:
    """One details section: bold heading + body. Empty body → empty string."""
    if not body_html.strip():
        return ""
    return f'<h4 style="{_HEADING_STYLE}">{escape(heading)}</h4>{body_html}'


def make_deal_expand_button(item_index: int) -> str:
    """Per-item "Show more" toggle for the hidden details div (inline onclick,
    no external JS needed)."""
    onclick = (
        f"var d=document.getElementById('deal-details-{item_index}');"
        "if(d){"
        "if(d.style.display==='none'){"
        "d.style.display='block';"
        "this.textContent='Show less';"
        "}else{"
        "d.style.display='none';"
        "this.textContent='Show more';"
        "}"
        "}"
    )
    return (f'<a href="javascript:void(0)" onclick="{onclick}" '
            f'style="color:#d40000;font-size:0.9em;cursor:pointer;'
            f'text-decoration:underline;font-weight:600;">Show more</a>')


def render_deal_html(item_index: int, deal: dict, img_url: str,
                     defer_image: bool = False) -> str:
    """Render one deal item: "N) Deal Name" + price line (original struck
    through when known, final in red), the image, the summary, and a
    "Show more" expander revealing the full details under headings — Sale,
    Items, Coupons to Use, What You Get, Availability, Pro Tip, Strategy —
    with items/coupons as bullets and strategy as numbered steps.
    With `defer_image` the image URL goes into data-src for lazy loading
    (hidden show-more items)."""
    name = (deal.get("name") or f"Deal #{item_index}").strip()
    title_safe = escape(name)
    title_html = f"{item_index}) {title_safe}"
    price_html = price_line_html(deal)
    if price_html:
        title_html += f' — {price_html}'

    img_attr = "data-src" if defer_image else "src"
    lazy_class = "lazy-load" if defer_image else ""
    html = '    <div class="finds-item">'
    html += '<div class="finds-item-header">'
    html += f'<span class="finds-item-title">{title_html}</span>'
    html += '</div>'
    html += (
        f'<div style="text-align: center; margin-top: 10px; padding: 10px;">'
        f'<div style="position:relative;display:inline-block;max-width:400px;width:100%;">'
        f'<img {img_attr}="{img_url}" alt="{title_safe}" '
        f'style="max-width: 400px; width: 100%; height: auto; display: block;" class="{lazy_class}" />'
        f'</div></div>'
    )

    summary = (deal.get("summary") or "").strip()
    if summary:
        html += f'<div class="finds-item-desc" style="{_SUMMARY_STYLE}">{escape(summary)}</div>'

    details = (
        _section("Sale", _paragraph_html(deal.get("sale")))
        + _section("Items", _bullets_html(deal.get("items")))
        + _section("Coupons to Use", _bullets_html(deal.get("coupons_to_use")))
        + _section("What You Get", _paragraph_html(deal.get("receive")))
        + _section("Availability", _paragraph_html(deal.get("availability")))
        + _section("Pro Tip", _paragraph_html(deal.get("pro_tip")))
        + _section("Strategy", _bullets_html(deal.get("strategy"), ordered=True))
    )
    if details:
        html += (f'<div style="text-align:center;margin-top:6px;">'
                 f'{make_deal_expand_button(item_index)}</div>')
        html += (f'<div id="deal-details-{item_index}" style="display:none;'
                 f'margin-top:4px;">{details}</div>')
    html += '</div>'
    return html


def make_show_more_button(anchor: str, label: str, hidden_count: int) -> str:
    """Generate a show more/less button with inline onclick (no external JS
    needed) — hidden items' images are lazy-loaded (data-src) and swapped
    into src when the section is expanded."""
    safe_label = label.replace("'", "\\'")
    onclick = (
        "event.stopPropagation();"
        f"var d=document.getElementById('finds-more-{anchor}');"
        "if(d){"
        "if(d.style.display==='none'){"
        "d.style.display='block';"
        "this.setAttribute('aria-expanded','true');"
        "this.textContent='Show less';"
        "var imgs=d.querySelectorAll('img[data-src]');"
        "for(var i=0;i<imgs.length;i++){"
        "if(imgs[i].getAttribute('data-src')){"
        "imgs[i].setAttribute('src',imgs[i].getAttribute('data-src'));"
        "imgs[i].removeAttribute('data-src');"
        "}"
        "}"
        "}else{"
        "d.style.display='none';"
        "this.setAttribute('aria-expanded','false');"
        "var n=d.querySelectorAll('.finds-item').length;"
        f"this.textContent='Show more in {safe_label} ('+n+' more)';"
        "}"
        "}"
    )
    return (
        f'\n  <button class="aos-show-more" data-target="{anchor}" data-label="{label}" '
        f'aria-expanded="false" onclick="{onclick}">'
        f'Show more in {label} ({hidden_count} more)</button>'
    )


def build_deals_page_html(items: list[tuple], brand: str) -> str:
    """Build the full WordPress page HTML for the brand's deals page.
    `items` is [(deal dict, img_url)] in publish order. One "Deals" section —
    the first ITEMS_PER_SECTION_LIMIT deals visible, the rest in a hidden
    show-more block (lazy-loaded images)."""
    total = len(items)
    html = '<div id="top" class="aos-finds">\n'
    html += f'  <h3 style="text-align: center;">{escape(brand)} Deals</h3>\n\n'
    html += f'  <p style="text-align: center; font-style: italic;">{total} deals found</p>\n\n'
    html += '  <div class="aos-category-items">'

    visible_items = items[:ITEMS_PER_SECTION_LIMIT]
    hidden_items = items[ITEMS_PER_SECTION_LIMIT:]

    item_index = 1
    for deal, img_url in visible_items:
        html += "\n" + render_deal_html(item_index, deal, img_url, defer_image=False)
        item_index += 1

    if hidden_items:
        html += '\n  </div>'
        html += '\n  <div class="aos-more" id="finds-more-deals" style="display:none;">'
        html += '\n    <div class="aos-category-items">'
        for deal, img_url in hidden_items:
            html += "\n" + render_deal_html(item_index, deal, img_url, defer_image=True)
            item_index += 1
        html += '\n    </div>'
        html += '\n  </div>'
        html += make_show_more_button("deals", "Deals", len(hidden_items))
    else:
        html += '\n  </div>'

    html += '\n  <div style="text-align: center; margin-top: 15px;">'
    html += '\n    <a href="#top" style="color: #810e0e; text-decoration: underline; font-weight: bold;">Back to TOP</a>'
    html += '\n  </div>'
    html += '\n</div>'
    return html


def build_page_title(brand: str, template: str | None = None,
                     week_start_day: str | None = None, today=None) -> str:
    """Page title for a brand's weekly deals update. Config comes from the
    workspace (page_title template + week_start day); placeholders {brand}
    and {date_range} are substituted. The week window defaults to
    Friday → Thursday (ALDI's ad-week); Publix etc. can set their own start
    day. Default template:
    "Just Spotted at ALDI: This Week's Deals Everyone's Grabbing (9/11 – 9/17)".
    A custom workspace template is used as-is."""
    today = today or date.today()
    start_idx = WEEKDAY_INDEX.get((week_start_day or "friday").strip().lower(), 4)
    days_since_start = (today.weekday() - start_idx) % 7
    week_start = today - timedelta(days=days_since_start)
    week_end = week_start + timedelta(days=6)
    date_range = f"{week_start.month}/{week_start.day} – {week_end.month}/{week_end.day}"
    tpl = (template or "").strip() or \
        "Just Spotted at {brand}: This Week’s Deals Everyone’s Grabbing ({date_range})"
    return tpl.replace("{brand}", brand).replace("{date_range}", date_range)


def publish_brand(parent_job_id: str, brand: str, brand_slug: str,
                  page_id: str, publish_target: str, output_root: str = "output",
                  status: str = "draft", page_title: str | None = None,
                  week_start_day: str | None = None) -> tuple[bool, str]:
    """Publish ONE brand's deals from a completed pipeline job's output to
    its website + WordPress page — called once per job by run_facebook.py.
    Only single-deal images (analysis_status "success") are published.
    `page_title` is the workspace's title template ({brand} and {date_range}
    placeholders); `week_start_day` shifts the date window (e.g. 'friday'
    for ALDI, 'tuesday' for Publix). Both default to the deals template +
    Friday window. Returns (success, summary message). Also used by this
    file's CLI."""
    target = PUBLISH_TARGETS.get(publish_target) or PUBLISH_TARGETS["retailshout"]
    wp_url = os.environ.get(target["env"][0])
    wp_username = os.environ.get(target["env"][1])
    wp_password = os.environ.get(target["env"][2])
    if not all([wp_url, wp_username, wp_password]):
        return False, (f"Missing WordPress credentials for {target['site_name']} — set "
                       f"{' / '.join(target['env'])} in .env")

    deals_dir = Path(output_root) / parent_job_id / brand_slug / "deals"
    images, analysis = load_deals(deals_dir)
    if not images:
        return False, (f"No deal images found for {brand} under {deals_dir} "
                       f"— nothing to publish.")
    print(f"📋 {brand}: {len(images)} deal image(s) from job {parent_job_id[:8]}...")

    publisher = WordPressPublisher(wp_url, wp_username, wp_password)
    if not publisher.test_connection():
        return False, f"WordPress connection failed for {target['site_name']}"

    print(f"\n📤 Uploading {len(images)} image(s) to {target['site_name']}...")
    uploaded: list[tuple] = []
    skipped = 0
    for i, img_path in enumerate(images, start=1):
        entry = analysis.get(img_path.name) or {}
        status_flag = entry.get("analysis_status")
        deals = entry.get("deals") or []
        if status_flag != "success" or not deals:
            print(f"  [{i}] ⏭️  {img_path.name} — {status_flag or 'no deals'}, skipping")
            skipped += 1
            continue
        deal = deals[0]
        name = (deal.get("name") or img_path.stem).strip()
        media_id = publisher.upload_image(img_path, title=name)
        if not media_id:
            print(f"  [{i}] ⚠️  Upload failed for {img_path.name} — skipping")
            continue
        try:
            resp = requests.get(f"{publisher.api_base}/media/{media_id}", auth=publisher.auth, timeout=15)
            source_url = resp.json().get("source_url") if resp.status_code == 200 else None
        except Exception as e:
            print(f"  ⚠️  Could not resolve URL for media {media_id}: {e}")
            source_url = None
        if source_url:
            uploaded.append((deal, source_url))
            final = (deal.get("final_cost") or "").strip()
            print(f"  [{i}] ✅ Uploaded '{name}'" + (f" ({final})" if final else ""))

    if not uploaded:
        return False, "No deal images uploaded successfully — nothing to publish."
    if skipped:
        print(f"⏭️  {skipped} image(s) skipped (no single deal — same rule as the infographic stage)")

    print(f"\n🎨 Building page HTML ({len(uploaded)} deal(s))...")
    html = build_deals_page_html(uploaded, brand)

    page_title = build_page_title(brand, template=page_title, week_start_day=week_start_day)
    print(f"\n📤 Updating WordPress page {page_id} as {status.upper()}...")
    print(f"   Title: {page_title}")
    success = publisher.update_page(
        page_id=int(page_id),
        content=html,
        title=page_title,
        status=status,
        try_page_first=True,
        update_date=(status == "publish"),
    )
    if not success:
        return False, f"WordPress page {page_id} update failed."

    summary = (f"Brand {brand} deals → {target['site_name']} page {page_id}: "
               f"{len(uploaded)} deal(s) published as {status.upper()}")
    print("\n" + "=" * 60)
    print("✅ PUBLISHED SUCCESSFULLY!")
    print(f"   Brand    : {brand}")
    print(f"   Target   : {target['site_name']}")
    print(f"   Page ID  : {page_id}")
    print(f"   Deals    : {len(uploaded)}")
    print(f"   Status   : {status.upper()}")
    print("=" * 60)
    return True, summary


def main():
    parser = argparse.ArgumentParser(description="Publish one brand's deals to WordPress")
    parser.add_argument("--parent-job-id", required=True, help="Pipeline job id whose output/<id>/ holds the deals")
    parser.add_argument("--brand", required=True, help="Canonical brand name (e.g. 'Dollar General')")
    parser.add_argument("--brand-slug", required=True, help="Output folder slug (e.g. 'dollar-general')")
    parser.add_argument("--page-id", required=True, help="WordPress page/post ID to update")
    parser.add_argument("--publish-target", default="retailshout", choices=sorted(PUBLISH_TARGETS))
    parser.add_argument("--output-root", default="output")
    status_group = parser.add_mutually_exclusive_group()
    status_group.add_argument("--publish", action="store_true", help="Publish live")
    status_group.add_argument("--draft", action="store_true", help="Keep as draft (default)")
    args = parser.parse_args()

    status = "publish" if args.publish else "draft"
    ok, message = publish_brand(
        parent_job_id=args.parent_job_id,
        brand=args.brand,
        brand_slug=args.brand_slug,
        page_id=args.page_id,
        publish_target=args.publish_target,
        output_root=args.output_root,
        status=status,
    )
    print(message)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
