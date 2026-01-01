#!/usr/bin/env python3
"""
Clean up Modal server POC logs and local transcript directories.

Usage:
    # Clean up a single transcript directory
    uv run python scripts/cleanup_modal_logs.py /path/to/transcripts/experiment_name

    # Clean up multiple directories
    uv run python scripts/cleanup_modal_logs.py dir1 dir2 dir3

    # Clean up using glob patterns (quote to prevent shell expansion)
    uv run python scripts/cleanup_modal_logs.py 'transcripts/test-*/'

    # Clean up with dry-run first
    uv run python scripts/cleanup_modal_logs.py 'transcripts/test-*/' --dry-run

    # Delete ALL logs (nuclear option - keeps only poc.db)
    uv run python scripts/cleanup_modal_logs.py --all

This script:
1. Finds all agent_ids from metadata.json files in the transcript directory
2. Downloads the poc.db from Modal volume
3. Queries for all poc_ids associated with those agents
4. Deletes the corresponding log directories from the Modal volume
5. Deletes the local transcript directory (transcripts/experiment_name)
6. Deletes the corresponding transcript_reports directory if it exists
"""

import argparse
import glob
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from cybergym.server.pocdb import PoCRecord


def find_agent_ids(transcript_dir: Path) -> set[str]:
    """Find all agent_ids from metadata.json files in transcript directory."""
    agent_ids = set()

    # Look for metadata.json files in runs/*/run_*/agent/
    for metadata_file in transcript_dir.glob("runs/*/run_*/agent/metadata.json"):
        try:
            with open(metadata_file) as f:
                data = json.load(f)
                if "agent_id" in data:
                    agent_ids.add(data["agent_id"])
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Could not read {metadata_file}: {e}")

    # Also check .tmp directory if it exists
    for metadata_file in transcript_dir.glob(".tmp/*/run_*/agent/metadata.json"):
        try:
            with open(metadata_file) as f:
                data = json.load(f)
                if "agent_id" in data:
                    agent_ids.add(data["agent_id"])
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Could not read {metadata_file}: {e}")

    return agent_ids


def download_poc_db(volume_name: str, local_path: Path) -> bool:
    """Download poc.db from Modal volume."""
    result = subprocess.run(
        ["uv", "run", "modal", "volume", "get", volume_name, "/server_poc/poc.db", str(local_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error downloading poc.db: {result.stderr}")
        return False
    return True


def get_poc_ids_for_agents(db_path: Path, agent_ids: set[str]) -> list[str]:
    """Query database for all poc_ids associated with given agent_ids."""
    engine = create_engine(f"sqlite:///{db_path}")
    poc_ids = []

    with Session(engine) as session:
        for agent_id in agent_ids:
            records = session.query(PoCRecord).filter_by(agent_id=agent_id).all()
            for record in records:
                poc_ids.append(record.poc_id)

    return poc_ids


def get_log_path(poc_id: str) -> str:
    """Convert poc_id to Modal volume log path."""
    return f"/server_poc/logs/{poc_id[:2]}/{poc_id[2:4]}/{poc_id}"


def delete_log_directories(volume_name: str, poc_ids: list[str], dry_run: bool = False) -> int:
    """Delete log directories from Modal volume."""
    deleted = 0
    failed = 0

    for poc_id in poc_ids:
        log_path = get_log_path(poc_id)
        if dry_run:
            print(f"  [DRY RUN] Would delete: {log_path}")
            deleted += 1
            continue

        result = subprocess.run(
            ["uv", "run", "modal", "volume", "rm", "-r", volume_name, log_path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            deleted += 1
        else:
            # Path might not exist (already deleted or never created)
            if "not found" in result.stderr.lower() or "does not exist" in result.stderr.lower():
                pass  # Skip silently
            else:
                failed += 1
                if failed <= 5:  # Only show first few errors
                    print(f"  Warning: Failed to delete {log_path}: {result.stderr.strip()}")

    return deleted


def get_transcript_reports_dir(transcript_dir: Path) -> Path | None:
    """Get the corresponding transcript_reports directory for a transcript directory."""
    # transcript_dir is like: /path/to/transcripts/experiment_name
    # transcript_reports would be: /path/to/transcript_reports/experiment_name
    if "transcripts" in transcript_dir.parts:
        parts = list(transcript_dir.parts)
        idx = parts.index("transcripts")
        parts[idx] = "transcript_reports"
        return Path(*parts)
    return None


def delete_local_directories(transcript_dirs: list[Path], dry_run: bool = False) -> tuple[int, int]:
    """Delete local transcript and transcript_reports directories using sudo rm -rf."""
    transcripts_deleted = 0
    reports_deleted = 0

    for transcript_dir in transcript_dirs:
        # Delete transcript directory
        if transcript_dir.exists():
            if dry_run:
                print(f"  [DRY RUN] Would delete local: {transcript_dir}")
                transcripts_deleted += 1
            else:
                result = subprocess.run(
                    ["sudo", "rm", "-rf", str(transcript_dir)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(f"  Deleted local: {transcript_dir}")
                    transcripts_deleted += 1
                else:
                    print(f"  Warning: Failed to delete {transcript_dir}: {result.stderr.strip()}")

        # Delete corresponding transcript_reports directory
        reports_dir = get_transcript_reports_dir(transcript_dir)
        if reports_dir and reports_dir.exists():
            if dry_run:
                print(f"  [DRY RUN] Would delete local: {reports_dir}")
                reports_deleted += 1
            else:
                result = subprocess.run(
                    ["sudo", "rm", "-rf", str(reports_dir)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(f"  Deleted local: {reports_dir}")
                    reports_deleted += 1
                else:
                    print(f"  Warning: Failed to delete {reports_dir}: {result.stderr.strip()}")

    return transcripts_deleted, reports_deleted


def delete_all_logs(volume_name: str, dry_run: bool = False) -> int:
    """Delete ALL log directories from Modal volume (nuclear option)."""
    # List all first-level directories (00-ff)
    result = subprocess.run(
        ["uv", "run", "modal", "volume", "ls", volume_name, "/server_poc/logs"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error listing logs directory: {result.stderr}")
        return 0

    dirs = [line.strip().split("/")[-1] for line in result.stdout.strip().split("\n") if line.strip()]
    if not dirs:
        print("No log directories found")
        return 0

    print(f"Found {len(dirs)} top-level log directories to delete")
    deleted = 0

    for d in dirs:
        log_path = f"/server_poc/logs/{d}"
        if dry_run:
            print(f"  [DRY RUN] Would delete: {log_path}")
            deleted += 1
            continue

        result = subprocess.run(
            ["uv", "run", "modal", "volume", "rm", "-r", volume_name, log_path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            deleted += 1
            print(f"  Deleted: {log_path}")
        else:
            print(f"  Warning: Failed to delete {log_path}: {result.stderr.strip()}")

    return deleted


def expand_glob_patterns(patterns: list[str]) -> list[Path]:
    """Expand glob patterns in the input paths."""
    expanded = []
    for pattern in patterns:
        # Check if pattern contains glob characters
        if any(c in pattern for c in ["*", "?", "["]):
            matches = glob.glob(pattern)
            if matches:
                expanded.extend(Path(m) for m in sorted(matches) if Path(m).is_dir())
            else:
                print(f"Warning: No matches found for pattern: {pattern}")
        else:
            expanded.append(Path(pattern))
    return expanded


def main():
    parser = argparse.ArgumentParser(description="Clean up Modal server POC logs for transcript directories")
    parser.add_argument("transcript_dirs", nargs="*", help="Path(s) or glob pattern(s) to transcript directories (e.g., transcripts/test-*/)")
    parser.add_argument("--volume", default="cybergym-server-data", help="Modal volume name")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--keep-db", action="store_true", help="Keep downloaded poc.db for inspection")
    parser.add_argument("--all", action="store_true", dest="delete_all", help="Delete ALL logs (nuclear option)")
    args = parser.parse_args()

    # Expand glob patterns
    if args.transcript_dirs:
        args.transcript_dirs = expand_glob_patterns(args.transcript_dirs)
        if args.transcript_dirs:
            if args.dry_run:
                print(f"[DRY RUN] Target directories ({len(args.transcript_dirs)}):")
                for d in args.transcript_dirs:
                    print(f"  - {d}")
                    reports_dir = get_transcript_reports_dir(d)
                    if reports_dir and reports_dir.exists():
                        print(f"    + {reports_dir}")
                print()
            else:
                print(f"Matched {len(args.transcript_dirs)} directories:")
                for d in args.transcript_dirs:
                    print(f"  - {d}")
                print()

    # Nuclear option: delete all logs
    if args.delete_all:
        if not args.dry_run:
            confirm = input("WARNING: This will delete ALL POC logs. Type 'yes' to confirm: ")
            if confirm != "yes":
                print("Aborted")
                sys.exit(0)
        deleted = delete_all_logs(args.volume, dry_run=args.dry_run)
        if args.dry_run:
            print(f"\n[DRY RUN] Would delete {deleted} top-level directories")
        else:
            print(f"\nDeleted {deleted} top-level directories")
        sys.exit(0)

    if not args.transcript_dirs:
        parser.error("Either provide transcript directories or use --all")

    # Collect agent_ids from all directories
    all_agent_ids = set()
    for transcript_dir in args.transcript_dirs:
        if not transcript_dir.exists():
            print(f"Warning: Transcript directory does not exist: {transcript_dir}")
            continue
        print(f"Scanning {transcript_dir} for agent_ids...")
        agent_ids = find_agent_ids(transcript_dir)
        print(f"  Found {len(agent_ids)} unique agent_ids")
        all_agent_ids.update(agent_ids)

    if not all_agent_ids:
        print("No agent_ids found in any transcript directory")
        sys.exit(0)
    print(f"\nTotal: {len(all_agent_ids)} unique agent_ids across all directories")

    # Step 2: Download poc.db
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "poc.db"
        print(f"\nDownloading poc.db from Modal volume '{args.volume}'...")
        if not download_poc_db(args.volume, db_path):
            sys.exit(1)

        # Step 3: Query for poc_ids
        print("Querying database for POC records...")
        poc_ids = get_poc_ids_for_agents(db_path, all_agent_ids)
        if not poc_ids:
            print("No POC records found for these agents")
            sys.exit(0)
        print(f"Found {len(poc_ids)} POC records to clean up")

        if args.keep_db:
            kept_path = args.transcript_dirs[0] / "poc.db"
            subprocess.run(["cp", str(db_path), str(kept_path)])
            print(f"Kept poc.db at: {kept_path}")

        # Step 4: Delete log directories from Modal
        if args.dry_run:
            print("\n[DRY RUN] Modal log directories to delete:")
        else:
            print(f"\nDeleting {len(poc_ids)} log directories from Modal volume...")

        deleted = delete_log_directories(args.volume, poc_ids, dry_run=args.dry_run)

        if args.dry_run:
            print(f"\n[DRY RUN] Would delete {deleted} Modal log directories")
        else:
            print(f"Deleted {deleted} Modal log directories")

    # Step 5: Delete local directories (outside tempdir context)
    if args.dry_run:
        print("\n[DRY RUN] Local directories to delete:")
    else:
        print("\nDeleting local transcript directories...")

    transcripts_deleted, reports_deleted = delete_local_directories(args.transcript_dirs, dry_run=args.dry_run)

    if args.dry_run:
        print(f"\n[DRY RUN] Would delete {transcripts_deleted} transcript dirs, {reports_deleted} report dirs")
    else:
        print(f"Deleted {transcripts_deleted} transcript dirs, {reports_deleted} report dirs")


if __name__ == "__main__":
    main()
