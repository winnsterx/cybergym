#!/usr/bin/env python3
"""
Generate a summary.json file from transcript report verdicts.

Usage:
    uv run scripts/generate_report_summary.py /path/to/transcript_reports/experiment_name
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def analyze_reports(report_dir: Path) -> dict:
    """Analyze all verdict.json files in the report directory."""
    runs_dir = report_dir / "runs"

    if not runs_dir.exists():
        raise ValueError(f"No 'runs' directory found in {report_dir}")

    # Overall metrics
    total_runs = 0
    successful_runs = 0
    verdict_counts = defaultdict(int)
    confidence_counts = defaultdict(int)
    poc_attempts_list = []

    # Per-task metrics
    per_task = defaultdict(lambda: {
        "total": 0,
        "successful": 0,
        "verdicts": defaultdict(int),
        "verdict_runs": defaultdict(list),  # Track which runs had each verdict
        "poc_attempts": [],
    })

    # Iterate through all tasks and runs
    for task_dir in sorted(runs_dir.iterdir()):
        if not task_dir.is_dir():
            continue

        task_id = task_dir.name

        for run_dir in sorted(task_dir.iterdir()):
            if not run_dir.is_dir():
                continue

            verdict_file = run_dir / "verdict.json"
            if not verdict_file.exists():
                continue

            try:
                with open(verdict_file) as f:
                    verdict_data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not read {verdict_file}: {e}")
                continue

            # Update overall metrics
            total_runs += 1
            # Support both old field names (success, verdict) and new ones (poc_success, strategy_classification)
            success = verdict_data.get("poc_success", verdict_data.get("success", False))
            if success:
                successful_runs += 1

            verdict = verdict_data.get("strategy_classification", verdict_data.get("verdict", "Unknown"))
            verdict_counts[verdict] += 1

            confidence = verdict_data.get("confidence", "Unknown")
            confidence_counts[confidence] += 1

            num_attempts = verdict_data.get("num_of_poc_attempts")
            if num_attempts is not None:
                poc_attempts_list.append(num_attempts)

            # Update per-task metrics
            run_name = run_dir.name  # e.g., "run_0", "run_1"
            per_task[task_id]["total"] += 1
            if success:
                per_task[task_id]["successful"] += 1
            per_task[task_id]["verdicts"][verdict] += 1
            per_task[task_id]["verdict_runs"][verdict].append(run_name)
            if num_attempts is not None:
                per_task[task_id]["poc_attempts"].append(num_attempts)

    # Separate verdict counts for successful runs only
    successful_verdict_counts = defaultdict(int)
    successful_confidence_counts = defaultdict(int)
    successful_poc_attempts = []

    # Re-iterate to collect successful-only metrics
    for task_dir in sorted(runs_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        for run_dir in sorted(task_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            verdict_file = run_dir / "verdict.json"
            if not verdict_file.exists():
                continue
            try:
                with open(verdict_file) as f:
                    verdict_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue
            if verdict_data.get("poc_success", verdict_data.get("success", False)):
                verdict = verdict_data.get("strategy_classification", verdict_data.get("verdict", "Unknown"))
                successful_verdict_counts[verdict] += 1
                confidence = verdict_data.get("confidence", "Unknown")
                successful_confidence_counts[confidence] += 1
                num_attempts = verdict_data.get("num_of_poc_attempts")
                if num_attempts is not None:
                    successful_poc_attempts.append(num_attempts)

    # Build summary
    summary = {
        "report_directory": str(report_dir),
        "overall": {
            "total_runs": total_runs,
            "successful_runs": successful_runs,
            "success_rate": round(successful_runs / total_runs * 100, 2) if total_runs > 0 else 0,
            "verdict_breakdown": {
                verdict: {
                    "count": count,
                    "percentage": round(count / total_runs * 100, 2) if total_runs > 0 else 0,
                }
                for verdict, count in sorted(verdict_counts.items(), key=lambda x: (x[0] is None, x[0] or ""))
            },
            "confidence_breakdown": {
                conf: {
                    "count": count,
                    "percentage": round(count / total_runs * 100, 2) if total_runs > 0 else 0,
                }
                for conf, count in sorted(confidence_counts.items(), key=lambda x: (x[0] is None, x[0] or ""))
            },
            "poc_attempts": {
                "total": sum(poc_attempts_list),
                "mean": round(sum(poc_attempts_list) / len(poc_attempts_list), 2) if poc_attempts_list else 0,
                "min": min(poc_attempts_list) if poc_attempts_list else 0,
                "max": max(poc_attempts_list) if poc_attempts_list else 0,
            },
        },
        "overall_successful_runs": {
            "total_successful": successful_runs,
            "verdict_breakdown": {
                verdict: {
                    "count": count,
                    "percentage": round(count / successful_runs * 100, 2) if successful_runs > 0 else 0,
                }
                for verdict, count in sorted(successful_verdict_counts.items(), key=lambda x: (x[0] is None, x[0] or ""))
            },
            "confidence_breakdown": {
                conf: {
                    "count": count,
                    "percentage": round(count / successful_runs * 100, 2) if successful_runs > 0 else 0,
                }
                for conf, count in sorted(successful_confidence_counts.items(), key=lambda x: (x[0] is None, x[0] or ""))
            },
            "poc_attempts": {
                "total": sum(successful_poc_attempts),
                "mean": round(sum(successful_poc_attempts) / len(successful_poc_attempts), 2) if successful_poc_attempts else 0,
                "min": min(successful_poc_attempts) if successful_poc_attempts else 0,
                "max": max(successful_poc_attempts) if successful_poc_attempts else 0,
            },
        },
        "per_task": {},
    }

    # Build per-task summary
    for task_id, task_data in sorted(per_task.items()):
        total = task_data["total"]
        successful = task_data["successful"]
        poc_attempts = task_data["poc_attempts"]

        summary["per_task"][task_id] = {
            "total_runs": total,
            "successful_runs": successful,
            "success_rate": round(successful / total * 100, 2) if total > 0 else 0,
            "verdict_breakdown": {
                verdict: {
                    "count": count,
                    "percentage": round(count / total * 100, 2) if total > 0 else 0,
                    "runs": sorted(task_data["verdict_runs"][verdict]),
                }
                for verdict, count in sorted(task_data["verdicts"].items(), key=lambda x: (x[0] is None, x[0] or ""))
            },
            "poc_attempts": {
                "total": sum(poc_attempts),
                "mean": round(sum(poc_attempts) / len(poc_attempts), 2) if poc_attempts else 0,
                "min": min(poc_attempts) if poc_attempts else 0,
                "max": max(poc_attempts) if poc_attempts else 0,
            },
        }

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Generate summary.json from transcript report verdicts"
    )
    parser.add_argument(
        "report_dir",
        type=Path,
        help="Path to the transcript reports directory (e.g., transcript_reports/tests-chmod-444)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output path for summary.json (default: <report_dir>/summary.json)",
    )

    args = parser.parse_args()

    if not args.report_dir.exists():
        print(f"Error: Directory {args.report_dir} does not exist")
        return 1

    output_path = args.output or (args.report_dir / "summary.json")

    print(f"Analyzing reports in: {args.report_dir}")
    summary = analyze_reports(args.report_dir)

    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Summary written to: {output_path}")

    # Print quick overview
    overall = summary["overall"]
    print(f"\n=== Overall Summary ===")
    print(f"Total runs: {overall['total_runs']}")
    print(f"Successful: {overall['successful_runs']} ({overall['success_rate']}%)")
    print(f"\nVerdict breakdown (of all runs):")
    for verdict, data in overall["verdict_breakdown"].items():
        print(f"  {verdict}: {data['count']} ({data['percentage']}%)")

    overall_success = summary["overall_successful_runs"]
    print(f"\nVerdict breakdown (of successful runs only):")
    for verdict, data in overall_success["verdict_breakdown"].items():
        print(f"  {verdict}: {data['count']} ({data['percentage']}%)")

    return 0


if __name__ == "__main__":
    exit(main())
