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
        # Don't change SANITIZER (to avoid libFuzzer rebuild issues), just clear the flags
        print("  Compiling WITHOUT sanitizers (clean binaries)...")
        compile_cmd = (
            f"docker exec {container_name} bash -c '"
            "export SANITIZER_FLAGS= && "
            "export COVERAGE_FLAGS= && "
            "arvo compile"
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
    """Copy a file from container to local path."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    ret, _, _ = run_cmd(f"docker cp {container_name}:{src_path} {dest_path}")
    return ret == 0 and dest_path.exists()


def copy_file_both_versions(container_name: str, src_path: str, unstripped_path: Path, stripped_path: Path) -> tuple[bool, bool]:
    """Copy a file from container to both unstripped and stripped destinations.

    Returns:
        (unstripped_success, stripped_success)
    """
    # Copy unstripped version
    unstripped_ok = copy_file(container_name, src_path, unstripped_path)
    if not unstripped_ok:
        return False, False

    # Copy stripped version (copy from unstripped, then strip)
    stripped_path.parent.mkdir(parents=True, exist_ok=True)
    ret, _, _ = run_cmd(f"cp {unstripped_path} {stripped_path}")
    if ret != 0:
        return True, False

    # Strip the stripped copy
    ret, _, _ = run_cmd(f"strip --strip-all {stripped_path}")
    # Even if strip fails (e.g., thin archives), we still have the file
    stripped_ok = stripped_path.exists()

    return True, stripped_ok


def analyze_task(task_id: str, data_dir: Path, output_dir: Path, no_sanitizers: bool = False) -> dict:
    """Analyze a single ARVO task and extract build artifacts.

    Extracts both stripped and unstripped versions:
    - output_dir/{task_id}/...  (unstripped)
    - output_dir/stripped/{task_id}/...  (stripped)

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

    # Setup output directories for both stripped and unstripped versions
    task_output_dir = output_dir / task_id
    task_output_dir.mkdir(parents=True, exist_ok=True)
    stripped_output_dir = output_dir / 'stripped' / task_id
    stripped_output_dir.mkdir(parents=True, exist_ok=True)

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

        # Copy static libraries (both stripped and unstripped)
        libs_dir = task_output_dir / 'libs'
        stripped_libs_dir = stripped_output_dir / 'libs'
        for lib_path in sorted(created_libs):
            lib_name = Path(lib_path).name
            unstripped_path = libs_dir / lib_name
            stripped_path = stripped_libs_dir / lib_name
            unstripped_ok, stripped_ok = copy_file_both_versions(
                container_name, lib_path, unstripped_path, stripped_path
            )
            if unstripped_ok:
                size_kb = unstripped_path.stat().st_size / 1024
                stripped_size_kb = stripped_path.stat().st_size / 1024 if stripped_ok else 0
                result['static_libs'].append({
                    'name': lib_name,
                    'container_path': lib_path,
                    'size_kb': round(size_kb, 1),
                    'stripped_size_kb': round(stripped_size_kb, 1) if stripped_ok else None
                })
                print(f"    Copied: {lib_name} ({size_kb:.1f} KB -> {stripped_size_kb:.1f} KB stripped)")

        # Copy object files (both stripped and unstripped, limit to reasonable number)
        objs_dir = task_output_dir / 'objects'
        stripped_objs_dir = stripped_output_dir / 'objects'
        obj_count = 0
        max_objs = 200  # Limit to avoid copying too many
        for obj_path in sorted(created_objs):
            if obj_count >= max_objs:
                print(f"    ... and {len(created_objs) - max_objs} more object files (skipped)")
                break
            obj_name = Path(obj_path).name
            unstripped_path = objs_dir / obj_name
            stripped_path = stripped_objs_dir / obj_name
            unstripped_ok, _ = copy_file_both_versions(
                container_name, obj_path, unstripped_path, stripped_path
            )
            if unstripped_ok:
                result['object_files'].append({
                    'name': obj_name,
                    'container_path': obj_path
                })
                obj_count += 1

        if obj_count > 0:
            print(f"    Copied {obj_count} object files (both stripped and unstripped)")

        # Copy the fuzzer binary (both stripped and unstripped)
        bin_dir = task_output_dir / 'bin'
        stripped_bin_dir = stripped_output_dir / 'bin'
        fuzzer_path = f"/out/{fuzzer_name}"
        unstripped_fuzzer = bin_dir / fuzzer_name
        stripped_fuzzer = stripped_bin_dir / fuzzer_name
        unstripped_ok, stripped_ok = copy_file_both_versions(
            container_name, fuzzer_path, unstripped_fuzzer, stripped_fuzzer
        )
        if unstripped_ok:
            size_mb = unstripped_fuzzer.stat().st_size / (1024 * 1024)
            stripped_size_mb = stripped_fuzzer.stat().st_size / (1024 * 1024) if stripped_ok else 0
            result['fuzzer_binary'] = {
                'name': fuzzer_name,
                'size_mb': round(size_mb, 1),
                'stripped_size_mb': round(stripped_size_mb, 1) if stripped_ok else None
            }
            print(f"    Copied fuzzer: {fuzzer_name} ({size_mb:.1f} MB -> {stripped_size_mb:.1f} MB stripped)")

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
        "--files-dir", "-d",
        type=Path,
        default=Path('/mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo'),
        help="Output directory for extracted files"
    )
    parser.add_argument(
        "--with-sanitizers",
        action="store_true",
        help="Compile with sanitizers (ASAN). Default is no sanitizers for clean binaries."
    )
    args = parser.parse_args()

    data_dir = Path('/mnt/jailbreak-defense/exp/winniex/cybergym/cybergym_data/data/arvo')

    print("=" * 80)
    print("ARVO Build Artifacts Extraction")
    print("=" * 80)
    print(f"Task: {args.task_id}")
    print(f"Output directory: {args.files_dir}")
    print(f"  - Unstripped: {args.files_dir}/{{task_id}}/")
    print(f"  - Stripped:   {args.files_dir}/stripped/{{task_id}}/")
    print(f"With sanitizers: {args.with_sanitizers}")

    print(f"\n{'='*60}")
    print(f"Processing Task: {args.task_id}")
    print('='*60)

    result = analyze_task(args.task_id, data_dir, args.files_dir, no_sanitizers=not args.with_sanitizers)

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

    # Update JSON output - one for unstripped, one for stripped
    unstripped_output = args.files_dir / 'deps.json'
    stripped_output = args.files_dir / 'stripped' / 'deps.json'

    # Load existing results
    unstripped_results = {}
    if unstripped_output.exists():
        with open(unstripped_output) as f:
            unstripped_results = json.load(f)

    stripped_results = {}
    if stripped_output.exists():
        with open(stripped_output) as f:
            stripped_results = json.load(f)

    # Build unstripped entry
    unstripped_results[f"arvo:{args.task_id}"] = {
        'fuzzer': result['fuzzer'],
        'static_libs': [
            {'name': lib['name'], 'container_path': lib['container_path'], 'size_kb': lib['size_kb']}
            for lib in result['static_libs']
        ],
        'object_files_count': len(result['object_files']),
        'fuzzer_binary': {
            'name': result['fuzzer_binary']['name'],
            'size_mb': result['fuzzer_binary']['size_mb']
        } if result['fuzzer_binary'] else None,
        'error': result.get('error'),
    }

    # Build stripped entry
    stripped_results[f"arvo:{args.task_id}"] = {
        'fuzzer': result['fuzzer'],
        'static_libs': [
            {'name': lib['name'], 'container_path': lib['container_path'], 'size_kb': lib.get('stripped_size_kb')}
            for lib in result['static_libs']
        ],
        'object_files_count': len(result['object_files']),
        'fuzzer_binary': {
            'name': result['fuzzer_binary']['name'],
            'size_mb': result['fuzzer_binary'].get('stripped_size_mb')
        } if result['fuzzer_binary'] else None,
        'error': result.get('error'),
    }

    # Write both JSON files
    unstripped_output.parent.mkdir(parents=True, exist_ok=True)
    with open(unstripped_output, 'w') as f:
        json.dump(unstripped_results, f, indent=2)
    print(f"\nUnstripped metadata written to {unstripped_output}")

    stripped_output.parent.mkdir(parents=True, exist_ok=True)
    with open(stripped_output, 'w') as f:
        json.dump(stripped_results, f, indent=2)
    print(f"Stripped metadata written to {stripped_output}")

    print("\n" + "=" * 80)
    print("Done")
    print("=" * 80)


if __name__ == '__main__':
    main()
