#!/usr/bin/env python3
"""
Fast POC size extraction by downloading from OSS-Fuzz directly.
Much faster than pulling Docker images!
"""

import requests
import json
import csv
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ARVO_DATA_DIR = Path("/mnt/jailbreak-defense/exp/winniex/cybergym/cybergym_data/data/arvo")
OUTPUT_FILE = Path("/mnt/jailbreak-defense/exp/winniex/cybergym/task_lists/arvo-200-easiest.csv")
CACHE_FILE = Path("/mnt/jailbreak-defense/exp/winniex/cybergym/scripts/poc_sizes_cache_ossfuzz.json")
META_BASE_URL = "https://raw.githubusercontent.com/n132/ARVO-Meta/main/archive_data/meta"


def extract_testcase_id(json_data: dict) -> str | None:
    """Extract testcase_id from the JSON metadata."""
    report = json_data.get("report", {})
    comments = report.get("comments", [])

    for comment in comments:
        content = comment.get("content", "")
        # Look for testcase_id in URLs
        match = re.search(r'testcase_id=(\d+)', content)
        if match:
            return match.group(1)
    return None


def get_poc_size(task_id: str) -> tuple[str, int | None]:
    """Get POC size by downloading from OSS-Fuzz."""
    try:
        # First get the JSON metadata
        meta_url = f"{META_BASE_URL}/{task_id}.json"
        r = requests.get(meta_url, timeout=30)
        if r.status_code != 200:
            return task_id, None

        json_data = r.json()
        testcase_id = extract_testcase_id(json_data)

        if not testcase_id:
            return task_id, None

        # Download from OSS-Fuzz (just get headers for size)
        oss_url = f"https://oss-fuzz.com/download?testcase_id={testcase_id}"
        r = requests.head(oss_url, allow_redirects=True, timeout=30)

        if r.status_code == 200:
            size = int(r.headers.get('content-length', 0))
            return task_id, size

        # If HEAD doesn't work, try GET
        r = requests.get(oss_url, timeout=60)
        if r.status_code == 200:
            return task_id, len(r.content)

        return task_id, None

    except Exception as e:
        print(f"Error for {task_id}: {e}", flush=True)
        return task_id, None


def main():
    # Get all task IDs from local data
    task_ids = sorted([d.name for d in ARVO_DATA_DIR.iterdir() if d.is_dir()])
    print(f"Found {len(task_ids)} ARVO tasks")

    # Load cache if exists
    cache = {}
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            cache = json.load(f)
        print(f"Loaded {len(cache)} cached results")

    # Filter tasks that need processing
    tasks_to_process = [t for t in task_ids if t not in cache]
    print(f"Need to process {len(tasks_to_process)} tasks")

    results = dict(cache)

    if tasks_to_process:
        # Can use many workers since these are lightweight HTTP requests
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(get_poc_size, task_id): task_id for task_id in tasks_to_process}

            for i, future in enumerate(as_completed(futures)):
                task_id, size = future.result()
                results[task_id] = size

                if (i + 1) % 50 == 0:
                    print(f"Processed {i + 1}/{len(tasks_to_process)} (last: {task_id}={size})", flush=True)
                    with open(CACHE_FILE, 'w') as f:
                        json.dump(results, f)

        with open(CACHE_FILE, 'w') as f:
            json.dump(results, f)

    # Filter out None values and sort by size
    valid_results = [(task_id, size) for task_id, size in results.items() if size is not None]
    sorted_results = sorted(valid_results, key=lambda x: x[1])

    print(f"\nFound {len(valid_results)} tasks with valid POC sizes")
    if sorted_results:
        print(f"Smallest POC: {sorted_results[0]}")
        print(f"Largest POC: {sorted_results[-1]}")

    # Write top 200 to CSV
    with open(OUTPUT_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['task'])
        for task_id, size in sorted_results[:200]:
            writer.writerow([f'arvo:{task_id}'])

    print(f"\nWrote top 200 easiest tasks to {OUTPUT_FILE}")

    print("\nTop 20 easiest (shortest POC):")
    for task_id, size in sorted_results[:20]:
        print(f"  arvo:{task_id}: {size} bytes")


if __name__ == "__main__":
    main()
