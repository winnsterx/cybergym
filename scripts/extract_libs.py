#!/usr/bin/env python3
"""
Extract static libraries (.a) from arvo Docker containers.

Uses `arvo compile` command to run the build, then extracts all .a files
that were created.

Usage:
    python scripts/extract_libs.py --task arvo:1065 --output-dir /tmp/libs
    python scripts/extract_libs.py --task-csv task_lists/test.csv --output-dir /tmp/libs
"""

import argparse
import subprocess
import csv
import uuid
from pathlib import Path


def run_cmd(cmd: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a shell command."""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check, timeout=timeout)


def extract_libs(task_id: str, output_dir: Path, build_timeout: int = 600) -> list[Path]:
    """
    Extract static libraries from a task's Docker container.

    1. Run `arvo compile` (with SANITIZER=address to avoid MSAN issues)
    2. Find all .a files in /src and /work
    3. Filter out test/fuzzer libraries
    4. Copy them to output directory
    """
    task_num = task_id.split(":")[1]
    image = f"n132/arvo:{task_num}-vul"
    container_name = f"arvo_{task_num}_build_{uuid.uuid4().hex[:8]}"

    # Create output directory for this task
    task_output = output_dir / task_id.replace(":", "_")
    task_output.mkdir(parents=True, exist_ok=True)

    # Clean up any existing container
    run_cmd(f"docker rm -f {container_name} 2>/dev/null", check=False)

    print(f"Starting container for {task_id}...")
    result = run_cmd(f"docker run -d --name {container_name} {image} sleep 3600", check=False)
    if result.returncode != 0:
        print(f"  Error starting container: {result.stderr}")
        return []

    try:
        # Modify /bin/arvo to use ASAN instead of MSAN (MSAN breaks configure tests)
        # Then run arvo compile which sets up all the proper environment variables
        print(f"  Running arvo compile (timeout={build_timeout}s)...")
        run_cmd(
            f"docker exec {container_name} sed -i 's/SANITIZER=memory/SANITIZER=address/' /bin/arvo",
            check=False
        )
        build_cmd = f"docker exec {container_name} arvo compile 2>&1"
        try:
            result = run_cmd(build_cmd, check=False, timeout=build_timeout)
            if result.returncode != 0:
                print(f"  Build finished with exit code {result.returncode}")
                # Show last few lines of output for debugging
                lines = result.stdout.strip().split('\n')
                for line in lines[-5:]:
                    print(f"    {line}")
        except subprocess.TimeoutExpired:
            print(f"  Build timed out after {build_timeout}s, continuing anyway...")

        # Find all .a files
        print(f"  Searching for built libraries...")
        result = run_cmd(
            f"docker exec {container_name} find /src /work -name '*.a' -type f 2>/dev/null",
            check=False
        )

        extracted = []
        if result.returncode == 0 and result.stdout.strip():
            for lib_path in result.stdout.strip().split('\n'):
                lib_path = lib_path.strip()
                if not lib_path:
                    continue

                lib_name = Path(lib_path).name

                # Skip test archives and fuzzer libraries
                skip_patterns = [
                    'testcases',       # test archives
                    'honggfuzz',       # fuzzer lib
                    'libhf',           # fuzzer lib
                    'libFuzzing',      # fuzzer engine
                    'libAFL',          # fuzzer lib
                    'small_archive',   # test data
                    'libcentipede',    # fuzzer lib
                    '/afl/',           # afl directory
                    '/libfuzzer/',     # libfuzzer directory
                ]
                if any(skip in lib_path for skip in skip_patterns):
                    continue

                local_path = task_output / lib_name

                # Copy the library
                cp_result = run_cmd(
                    f"docker cp {container_name}:{lib_path} {local_path}",
                    check=False
                )
                if cp_result.returncode == 0 and local_path.exists():
                    size_kb = local_path.stat().st_size / 1024
                    extracted.append(local_path)
                    print(f"    OK: {lib_name} ({size_kb:.1f} KB) from {lib_path}")

        if not extracted:
            print(f"  No libraries found")

        return extracted

    finally:
        run_cmd(f"docker stop {container_name}", check=False)
        run_cmd(f"docker rm -f {container_name}", check=False)


def main():
    parser = argparse.ArgumentParser(description="Extract static libraries from arvo Docker containers")
    parser.add_argument("--task", type=str, help="Single task ID (e.g., arvo:1065)")
    parser.add_argument("--task-csv", type=Path, help="CSV file with task IDs")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output directory for libraries")
    parser.add_argument("--timeout", type=int, default=600, help="Build timeout in seconds")
    args = parser.parse_args()

    if not args.task and not args.task_csv:
        parser.error("Either --task or --task-csv is required")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Collect task IDs
    tasks = []
    if args.task:
        tasks.append(args.task)
    if args.task_csv:
        with open(args.task_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                task_id = row.get("task") or row.get("task_id")
                if task_id:
                    tasks.append(task_id.strip().strip('"'))

    # Filter to only arvo tasks
    tasks = [t for t in tasks if t.startswith("arvo:")]

    print(f"Extracting libraries for {len(tasks)} tasks...")
    print(f"Output directory: {args.output_dir}")
    print()

    results = {}
    for task_id in tasks:
        print(f"{'='*60}")
        print(f"Processing {task_id}")
        print(f"{'='*60}")
        extracted = extract_libs(task_id, args.output_dir, args.timeout)
        results[task_id] = extracted
        print()

    # Summary
    print("="*60)
    print("SUMMARY")
    print("="*60)
    success = 0
    failed = 0
    for task_id, libs in results.items():
        if libs:
            success += 1
            print(f"OK {task_id}: {len(libs)} libraries")
            for lib in libs:
                print(f"     - {lib.name}")
        else:
            failed += 1
            print(f"FAILED {task_id}")

    print()
    print(f"Success: {success}/{len(results)}, Failed: {failed}/{len(results)}")


if __name__ == "__main__":
    main()
