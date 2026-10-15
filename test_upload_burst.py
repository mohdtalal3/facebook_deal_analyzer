#!/usr/bin/env python3
"""
Upload-burst test: fire N concurrent KIE uploads with NO rate-limit pacing
(no createTask — nothing that costs credits) and report timing + failures.

Purpose: verify KIE doesn't throttle/429 the file-stream-upload endpoint when
hit with a large concurrent burst, so upload pacing can be safely removed.

  python3 test_upload_burst.py            # 50 uploads
  python3 test_upload_burst.py 30        # custom count
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from generate import upload_image

IMAGE_DIR = Path(__file__).parent / "test_images"
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 50


def main():
    sources = sorted(p for p in IMAGE_DIR.iterdir() if p.suffix.lower() == ".png")
    if not sources:
        raise SystemExit(f"No .png images in {IMAGE_DIR}")
    # Round-robin the sources up to COUNT (upload_image gives each copy a
    # unique UUID filename, so repeats are independent uploads).
    targets = [sources[i % len(sources)] for i in range(COUNT)]

    print(f"Uploading {len(targets)} images concurrently (no pacing, no createTask)...")
    t0 = time.monotonic()
    ok, failed = [], []
    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        futures = {
            executor.submit(upload_image, str(src)): src for src in targets
        }
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                url = fut.result()
                ok.append((src.name, url))
                print(f"  ✅ {src.name}")
            except Exception as e:
                failed.append((src.name, str(e)))
                print(f"  ❌ {src.name}: {e}")

    dt = time.monotonic() - t0
    print(f"\n{'=' * 60}")
    print(f"Done in {dt:.1f}s — {len(ok)} succeeded, {len(failed)} failed")
    if failed:
        print("Failures:")
        for name, err in failed:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
