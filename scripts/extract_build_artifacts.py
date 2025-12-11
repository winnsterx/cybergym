#!/usr/bin/env python3
"""
Extract build artifacts from ARVO tasks after running arvo compile.

This script:
1. Spins up a Docker container for an ARVO task
2. Runs arvo compile
3. Finds all generated .a and .o files (comparing before/after)
4. Copies them to the output directory
"""

import argparse
import json
import re
import subprocess
import uuid
from pathlib import Path


def run_cmd(cmd: str, timeout: int = 600) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"


def get_fuzzer_name(error_txt: str) -> str | None:
    """Extract the fuzzer binary name from error.txt"""
    match = re.search(r'/out/([a-zA-Z0-9_-]+)', error_txt)
    return match.group(1) if match else None


def start_container(task_id: str) -> str | None:
    """Start a Docker container for the task and return container name"""
    image = f"n132/arvo:{task_id}-vul"
    container_name = f"arvo_{task_id}_extract_{uuid.uuid4().hex[:8]}"

    print(f"  Pulling image {image}...")
    run_cmd(f"docker pull {image}", timeout=600)

    print(f"  Starting container...")
    ret, stdout, stderr = run_cmd(f"docker run -d --name {container_name} {image} sleep infinity")
    if ret != 0:
        print(f"  ERROR: Failed to start container: {stderr}")
        return None

    print(f"  Container: {container_name}")
    return container_name


def stop_container(container_name: str):
    """Stop and remove the container"""
    run_cmd(f"docker stop {container_name}", timeout=30)
    run_cmd(f"docker rm -f {container_name}", timeout=30)


def get_existing_files(container_name: str, pattern: str) -> set[str]:
    """Get set of files matching pattern in container"""
    ret, stdout, _ = run_cmd(
        f"docker exec {container_name} find /src /work /out -name '{pattern}' -type f 2>/dev/null"
    )
    if ret != 0:
        return set()
    return set(stdout.strip().split('\n')) if stdout.strip() else set()


def run_arvo_compile(container_name: str, no_sanitizers: bool = False) -> bool:
    """Run arvo compile in the container.

    Args:
        container_name: Docker container name
        no_sanitizers: If True, compile without sanitizers/coverage for clean binaries
    """
    if no_sanitizers:
        # Disable all sanitizers and coverage instrumentation for clean binaries
        # This removes: -fsanitize=memory/address, -fsanitize-coverage=trace-pc-guard,trace-cmp
        print("  Compiling WITHOUT sanitizers (clean binaries)...")
        compile_cmd = (
            f"docker exec {container_name} bash -c '"
            "export SANITIZER_FLAGS= && "
            "export COVERAGE_FLAGS= && "
            "export SANITIZER_FLAGS_memory= && "
            "export SANITIZER_FLAGS_address= && "
            "/usr/local/bin/compile"
            "'"
        )
    else:
        # Fix MSAN -> ASAN: MSAN breaks configure tests because programs can't run
        print("  Patching /bin/arvo to use ASAN instead of MSAN...")
        run_cmd(
            f"docker exec {container_name} sed -i 's/SANITIZER=memory/SANITIZER=address/' /bin/arvo",
            timeout=30
        )
        compile_cmd = f"docker exec {container_name} arvo compile"

    print("  Running compile (this may take a few minutes)...")
    ret, stdout, stderr = run_cmd(
        compile_cmd,
        timeout=600
    )

    if ret != 0:
        print(f"  WARNING: compile returned {ret}")
        lines = (stdout + stderr).strip().split('\n')
        for line in lines[-5:]:
            print(f"    {line}")

        # Check if libraries were still built (fuzzer linking may fail without sanitizer runtime)
        ret2, libs, _ = run_cmd(f"docker exec {container_name} find /src /work -name '*.a' -type f 2>/dev/null")
        if ret2 == 0 and libs.strip():
            lib_count = len([l for l in libs.strip().split('\n') if l and 'testcases' not in l])
            print(f"  Found {lib_count} static libraries despite build error")
            return True
        return False

    print("  Compile completed successfully")
    return True


def copy_file(container_name: str, src_path: str, dest_path: Path) -> bool:
    """Copy a file from container to local path"""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    ret, _, _ = run_cmd(f"docker cp {container_name}:{src_path} {dest_path}")
    return ret == 0 and dest_path.exists()


def analyze_task(task_id: str, data_dir: Path, output_dir: Path, no_sanitizers: bool = False) -> dict:
    """Analyze a single ARVO task and extract build artifacts.

    Args:
        task_id: ARVO task ID (e.g., "1065")
        data_dir: Directory containing task data (error.txt, etc.)
        output_dir: Directory to write extracted files
        no_sanitizers: If True, compile without sanitizers for clean binaries
    """
    result = {
        'task_id': task_id,
        'fuzzer': None,
        'static_libs': [],
        'object_files': [],
        'fuzzer_binary': None,
        'no_sanitizers': no_sanitizers,
        'error': None,
    }

    # Get fuzzer name from error.txt
    error_txt_path = data_dir / task_id / 'error.txt'
    if not error_txt_path.exists():
        result['error'] = 'No error.txt found'
        return result

    error_txt = error_txt_path.read_text()
    fuzzer_name = get_fuzzer_name(error_txt)
    if not fuzzer_name:
        result['error'] = 'Could not extract fuzzer name from error.txt'
        return result

    result['fuzzer'] = fuzzer_name
    print(f"  Target fuzzer: {fuzzer_name}")

    # Start container
    container_name = start_container(task_id)
    if not container_name:
        result['error'] = 'Failed to start container'
        return result

    task_output_dir = output_dir / task_id
    task_output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Get existing .a and .o files BEFORE compile
        print("  Scanning existing files...")
        existing_libs = get_existing_files(container_name, "*.a")
        existing_objs = get_existing_files(container_name, "*.o")

        # Run arvo compile
        if not run_arvo_compile(container_name, no_sanitizers=no_sanitizers):
            result['error'] = 'arvo compile failed'
            return result

        # Get files AFTER compile
        print("  Scanning new files...")
        new_libs = get_existing_files(container_name, "*.a")
        new_objs = get_existing_files(container_name, "*.o")

        # Find newly created files
        created_libs = new_libs - existing_libs
        created_objs = new_objs - existing_objs

        # Filter out fuzzer/test libraries
        skip_patterns = [
            'libFuzzing', 'libAFL', 'libhf', 'honggfuzz', 'centipede',
            'testcases', 'small_archive', '/afl/', '/libfuzzer/'
        ]

        def should_skip(path: str) -> bool:
            return any(skip in path for skip in skip_patterns)

        created_libs = {p for p in created_libs if not should_skip(p)}
        created_objs = {p for p in created_objs if not should_skip(p)}

        print(f"  Found {len(created_libs)} new static libraries")
        print(f"  Found {len(created_objs)} new object files")

        # Copy static libraries
        libs_dir = task_output_dir / 'libs'
        for lib_path in sorted(created_libs):
            lib_name = Path(lib_path).name
            local_path = libs_dir / lib_name
            if copy_file(container_name, lib_path, local_path):
                size_kb = local_path.stat().st_size / 1024
                result['static_libs'].append({
                    'name': lib_name,
                    'container_path': lib_path,
                    'size_kb': round(size_kb, 1)
                })
                print(f"    Copied: {lib_name} ({size_kb:.1f} KB)")

        # Copy object files (limit to reasonable number)
        objs_dir = task_output_dir / 'objects'
        obj_count = 0
        max_objs = 200  # Limit to avoid copying too many
        for obj_path in sorted(created_objs):
            if obj_count >= max_objs:
                print(f"    ... and {len(created_objs) - max_objs} more object files (skipped)")
                break
            obj_name = Path(obj_path).name
            local_path = objs_dir / obj_name
            if copy_file(container_name, obj_path, local_path):
                result['object_files'].append({
                    'name': obj_name,
                    'container_path': obj_path
                })
                obj_count += 1

        if obj_count > 0:
            print(f"    Copied {obj_count} object files")

        # Copy the fuzzer binary
        bin_dir = task_output_dir / 'bin'
        fuzzer_path = f"/out/{fuzzer_name}"
        local_fuzzer = bin_dir / fuzzer_name
        if copy_file(container_name, fuzzer_path, local_fuzzer):
            size_mb = local_fuzzer.stat().st_size / (1024 * 1024)
            result['fuzzer_binary'] = {
                'name': fuzzer_name,
                'size_mb': round(size_mb, 1)
            }
            print(f"    Copied fuzzer: {fuzzer_name} ({size_mb:.1f} MB)")

    finally:
        print("  Stopping container...")
        stop_container(container_name)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Extract build artifacts from ARVO tasks"
    )
    parser.add_argument("task_id", type=str, help="Task ID (e.g., 368)")
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path('/mnt/jailbreak-defense/exp/winniex/cybergym/executables/deps.json'),
        help="Output JSON file for metadata"
    )
    parser.add_argument(
        "--files-dir", "-d",
        type=Path,
        default=Path('/mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo'),
        help="Output directory for extracted files"
    )
    parser.add_argument(
        "--no-sanitizers",
        action="store_true",
        help="Compile without sanitizers/coverage for clean binaries (smaller, no instrumentation)"
    )
    args = parser.parse_args()

    data_dir = Path('/mnt/jailbreak-defense/exp/winniex/cybergym/cybergym_data/data/arvo')

    print("=" * 80)
    print("ARVO Build Artifacts Extraction")
    print("=" * 80)
    print(f"Task: {args.task_id}")
    print(f"Output directory: {args.files_dir}")
    print(f"No sanitizers: {args.no_sanitizers}")

    print(f"\n{'='*60}")
    print(f"Processing Task: {args.task_id}")
    print('='*60)

    result = analyze_task(args.task_id, data_dir, args.files_dir, no_sanitizers=args.no_sanitizers)

    # Print summary
    if result.get('error'):
        print(f"\n  ERROR: {result['error']}")
    else:
        print(f"\n  Summary:")
        print(f"    Fuzzer: {result['fuzzer']}")
        print(f"    Static libraries: {len(result['static_libs'])}")
        for lib in result['static_libs'][:10]:
            print(f"      - {lib['name']} ({lib['size_kb']} KB)")
        if len(result['static_libs']) > 10:
            print(f"      ... and {len(result['static_libs']) - 10} more")
        print(f"    Object files: {len(result['object_files'])}")

    # Update JSON output
    all_results = {}
    if args.output.exists():
        with open(args.output) as f:
            all_results = json.load(f)

    all_results[f"arvo:{args.task_id}"] = {
        'fuzzer': result['fuzzer'],
        'static_libs': result['static_libs'],
        'object_files_count': len(result['object_files']),
        'fuzzer_binary': result['fuzzer_binary'],
        'error': result.get('error'),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(all_results, f, indent=2)
    print(f"\nMetadata written to {args.output}")

    print("\n" + "=" * 80)
    print("Done")
    print("=" * 80)


if __name__ == '__main__':
    main()
