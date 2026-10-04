#!/usr/bin/env python3
"""
Shared data-access layer for workspaces.json and jobs.json.

Extracted from app.py so both the Flask app and the background scheduler
(scheduler.py) can read/write the same files safely without importing
app.py itself (which would re-run the Flask app as a side effect).
"""

import json
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = DATA_DIR / "logs"
WORKSPACES_FILE = DATA_DIR / "workspaces.json"
JOBS_FILE = DATA_DIR / "jobs.json"
FB_AUTH_FILE = DATA_DIR / "fb_auth.json"
SCRAPER_STATS_FILE = DATA_DIR / "scraper_stats.json"
OUTPUT_DIR = BASE_DIR / "output"
EXPORTS_DIR = BASE_DIR / "exports"

# Ensure data directories and files exist on import
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(exist_ok=True)
if not WORKSPACES_FILE.exists():
    WORKSPACES_FILE.write_text("[]", encoding="utf-8")
if not JOBS_FILE.exists():
    JOBS_FILE.write_text("[]", encoding="utf-8")
if not FB_AUTH_FILE.exists():
    FB_AUTH_FILE.write_text(
        json.dumps({"cookie_string": "", "fb_dtsg": "", "updated_at": None}, indent=2),
        encoding="utf-8",
    )
if not SCRAPER_STATS_FILE.exists():
    SCRAPER_STATS_FILE.write_text("[]", encoding="utf-8")

_lock = threading.Lock()


# ── WORKSPACES ──────────────────────────────────────────────────────────────

PUBLISH_TARGETS = ("retailshout", "aos")


def normalize_workspace(ws: dict) -> dict:
    """Normalize a workspace record to the current one-brand-per-workspace
    shape with ONE publish destination (website + WordPress page ID) — the
    food/non-food category structure is gone (deals are not categorized).
    Legacy shapes are folded in, in memory (the next save through the
    workspace form persists the new shape):
      - brands[] lists (the old multi-brand config): the first entry becomes
        the workspace's single brand + destination.
      - the per-category food/non_food destinations: the food destination
        wins, falling back to non_food, then to the intermediate single
        page_id/publish_target fields.
      - the `categories` selection and `image_prompt` are dropped (unused —
        deals are not categorized and AI images use the built-in infographic
        prompt).
    Applied to every workspace read via load_workspaces(), and available to
    callers that receive a workspace dict from elsewhere (e.g. a hand-edited
    file)."""
    if isinstance(ws.get("brands"), list) and ws["brands"]:
        first = ws["brands"][0] or {}
        ws.setdefault("brand", first.get("brand") or "")
        ws.setdefault("page_id", first.get("page_id") or "")
        ws.setdefault("publish_target", first.get("publish_target") or "retailshout")
        ws.setdefault("page_title", first.get("page_title") or "")
        ws.setdefault("week_start", first.get("week_start") or "friday")
    if "page_id" not in ws:
        # Legacy per-category destinations: food wins, then non_food, then
        # the intermediate single fields.
        ws["page_id"] = ws.get("food_page_id") or ws.get("non_food_page_id") or ""
    if ws.get("publish_target") not in PUBLISH_TARGETS:
        legacy = ws.get("food_publish_target") or ws.get("non_food_publish_target")
        ws["publish_target"] = legacy if legacy in PUBLISH_TARGETS else "retailshout"
    # Drop the removed food/non-food + image-prompt config entirely.
    for key in ("food_page_id", "food_publish_target", "non_food_page_id",
                "non_food_publish_target", "categories", "image_prompt"):
        ws.pop(key, None)
    return ws


def load_workspaces() -> list:
    with _lock:
        data = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))
    return [normalize_workspace(ws) for ws in data]


def save_workspaces(data: list):
    with _lock:
        WORKSPACES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def update_workspace(workspace_id: str, updates: dict):
    """Atomically merge `updates` into the workspace dict with the given id."""
    with _lock:
        workspaces = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))
        for ws in workspaces:
            if ws["id"] == workspace_id:
                ws.update(updates)
                break
        WORKSPACES_FILE.write_text(json.dumps(workspaces, indent=2, ensure_ascii=False), encoding="utf-8")


# ── JOBS ────────────────────────────────────────────────────────────────────

def load_jobs() -> list:
    with _lock:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))


def save_jobs(data: list):
    with _lock:
        JOBS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def update_job(job_id: str, updates: dict):
    with _lock:
        jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        for job in jobs:
            if job["id"] == job_id:
                job.update(updates)
                break
        JOBS_FILE.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


# ── FACEBOOK AUTH (session cookie + fb_dtsg, edited from /settings) ─────────

def load_fb_auth() -> dict:
    with _lock:
        return json.loads(FB_AUTH_FILE.read_text(encoding="utf-8"))


def save_fb_auth(cookie_string: str, fb_dtsg: str):
    import datetime as _dt
    with _lock:
        FB_AUTH_FILE.write_text(
            json.dumps({
                "cookie_string": cookie_string,
                "fb_dtsg": fb_dtsg,
                "updated_at": _dt.datetime.now().isoformat(),
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── SCRAPER STATS (per-run product lookup results, shown on /scrape-stats) ──

def load_scrape_stats() -> list:
    with _lock:
        return json.loads(SCRAPER_STATS_FILE.read_text(encoding="utf-8"))


def record_scrape_stats(brand: str, brand_slug: str, category: str, job_id: str,
                        total: int, found: int, with_price: int):
    """Append one scrape-run summary record — how many products the brand
    scraper looked up (total), how many matched (found) and how many of
    those had a price (with_price). One record per brand+category per run."""
    record = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "recorded_at": datetime.now().isoformat(),
        "brand": brand,
        "brand_slug": brand_slug,
        "category": category,
        "job_id": job_id,
        "total": total,
        "found": found,
        "with_price": with_price,
    }
    with _lock:
        stats = json.loads(SCRAPER_STATS_FILE.read_text(encoding="utf-8"))
        stats.append(record)
        SCRAPER_STATS_FILE.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")


# ── LOGS ────────────────────────────────────────────────────────────────────

def append_log(job_id: str, line: str):
    log_file = LOGS_DIR / f"{job_id}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_logs(job_id: str) -> str:
    log_file = LOGS_DIR / f"{job_id}.log"
    if log_file.exists():
        return log_file.read_text(encoding="utf-8")
    return ""
