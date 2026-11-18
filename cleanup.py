#!/usr/bin/env python3
"""
Automatic cleanup of old job data.

Deletes only the heavy disk data older than the retention period
(default 7 days, configurable via the DATA_RETENTION_DAYS env var):
  - output/<job_id>/          scraped images + manifests
  - exports/*.zip             download zips
  - data/logs/*.log           per-job logs
  - data/sources_*.json       per-job temp input files
  - data/skip_*.json          per-job temp skip lists

Kept on purpose:
  - jobs.json entries         dashboard counts (Total Jobs / Completed) stay intact

Note: after cleanup, old jobs remain listed on the dashboard but their
logs and download button will be gone.

Runs once a day from the scheduler (see scheduler.init_scheduler) and once
at startup.
"""

import os
import shutil
import time

from data_store import DATA_DIR, LOGS_DIR, OUTPUT_DIR, EXPORTS_DIR

RETENTION_DAYS = max(1, int(os.getenv("DATA_RETENTION_DAYS", "7")))


def cleanup_old_data(retention_days: int | None = None) -> dict:
    """Delete heavy job data older than `retention_days`. Returns counts per category."""
    days = retention_days or RETENTION_DAYS
    cutoff_ts = time.time() - days * 86400
    removed = {"output_dirs": 0, "exports": 0, "logs": 0, "temp_files": 0}

    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff_ts:
                shutil.rmtree(d, ignore_errors=True)
                removed["output_dirs"] += 1

    if EXPORTS_DIR.exists():
        for f in EXPORTS_DIR.glob("*.zip"):
            if f.is_file() and f.stat().st_mtime < cutoff_ts:
                f.unlink(missing_ok=True)
                removed["exports"] += 1

    if LOGS_DIR.exists():
        for f in LOGS_DIR.glob("*.log"):
            if f.is_file() and f.stat().st_mtime < cutoff_ts:
                f.unlink(missing_ok=True)
                removed["logs"] += 1

    for pattern in ("sources_*.json", "skip_*.json"):
        for f in DATA_DIR.glob(pattern):
            if f.is_file() and f.stat().st_mtime < cutoff_ts:
                f.unlink(missing_ok=True)
                removed["temp_files"] += 1

    if any(removed.values()):
        print(f"[cleanup] Removed (>{days}d old): {removed}")
    return removed
