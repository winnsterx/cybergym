#!/usr/bin/env python3
"""
Extract statically linked libraries from ARVO tasks using linker maps.

This script:
1. Spins up a Docker container for an ARVO task
2. Modifies build.sh to generate linker maps
3. Runs arvo compile
4. Parses the linker map to extract linked object files and libraries
"""

import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


def get_fuzzer_name(error_txt: str) -> str | None:
    """Extract the fuzzer binary name from error.txt"""
    match = re.search(r'/out/([a-zA-Z0-9_-]+)', error_txt)
    return match.group(1) if match else None


def run_cmd(cmd: str, timeout: int = 300) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"


def start_container(task_id: str) -> str | None:
    """Start a Docker container for the task and return container ID"""
    image = f"n132/arvo:{task_id}-vul"
    container_name = f"arvo_{task_id}_linker_{uuid.uuid4().hex[:8]}"

    # Pull image if needed
    print(f"  Pulling image {image}...")
    run_cmd(f"docker pull {image}", timeout=600)

    # Start container
    print(f"  Starting container...")
    ret, stdout, stderr = run_cmd(f"docker run -d --name {container_name} {image} sleep infinity")
    if ret != 0:
        print(f"  ERROR: Failed to start container: {stderr}")
        return None

    container_id = stdout.strip()
    print(f"  Container: {container_name} ({container_id[:12]})")
    return container_name  # Return name instead of ID for easier cleanup


def stop_container(container_name: str):
    """Stop and remove the container"""
    run_cmd(f"docker stop {container_name}", timeout=30)
    run_cmd(f"docker rm -f {container_name}", timeout=30)


def install_linker_wrapper(container_name: str) -> bool:
    """
    Install a wrapper script that intercepts clang/clang++ calls and adds linker map generation.

    This approach works universally regardless of how build.sh invokes the compiler,
    by wrapping the actual compiler with a script that detects link commands
    (those with -o outputting to $OUT) and adds -Wl,-Map automatically.
    """
    import tempfile

    # Wrapper script that intercepts clang/clang++ and adds -Wl,-Map for link commands to /out
    # The script is parameterized by the real compiler path
    wrapper_script = '''#!/bin/bash
# Linker wrapper - automatically generates linker maps for binaries in /out

REAL_COMPILER="$1"
shift
ARGS=("$@")

# Check if this is a link command outputting to /out/
OUTPUT_FILE=""
for i in "${!ARGS[@]}"; do
    if [[ "${ARGS[$i]}" == "-o" ]] && [[ $((i+1)) -lt ${#ARGS[@]} ]]; then
        OUTPUT_FILE="${ARGS[$((i+1))]}"
        break
    fi
done

# If outputting to /out/, add linker map flag
if [[ "$OUTPUT_FILE" == /out/* ]]; then
    MAP_FILE="${OUTPUT_FILE}.map"
    exec "$REAL_COMPILER" -Wl,-Map="$MAP_FILE" "${ARGS[@]}"
else
    exec "$REAL_COMPILER" "${ARGS[@]}"
fi
'''

    # Write wrapper to temp file and copy to container
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        f.write(wrapper_script)
        tmp_path = f.name

    ret, _, _ = run_cmd(f"docker cp {tmp_path} {container_name}:/tmp/linker_wrapper.sh")
    Path(tmp_path).unlink()
    if ret != 0:
        print("  ERROR: Failed to copy wrapper script")
        return False

    run_cmd(f"docker exec {container_name} chmod +x /tmp/linker_wrapper.sh")

    # Find the actual clang binary (resolve symlinks)
    # clang -> clang-15, clang++ -> clang -> clang-15
    ret, real_clang, _ = run_cmd(f"docker exec {container_name} readlink -f /usr/local/bin/clang")
    real_clang = real_clang.strip()
    if ret != 0 or not real_clang:
        real_clang = "/usr/local/bin/clang-15"  # fallback

    # Backup the real binary
    ret, _, _ = run_cmd(f"docker exec {container_name} cp {real_clang} {real_clang}.real")
    if ret != 0:
        print(f"  ERROR: Failed to backup {real_clang}")
        return False

    # Create wrapper that calls the real binary
    # Pass both the real compiler AND the invocation name so we can detect clang++ mode
    wrapper_for_clang = f'''#!/bin/bash
# Pass the invocation name as first arg so wrapper knows if it's clang++
exec /tmp/linker_wrapper.sh {real_clang}.real "$@"
'''
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        f.write(wrapper_for_clang)
        tmp_path = f.name

    commands = [
        f"docker cp {tmp_path} {container_name}:/tmp/clang_wrapper.sh",
        f"docker exec {container_name} cp /tmp/clang_wrapper.sh {real_clang}",
        f"docker exec {container_name} chmod +x {real_clang}",
    ]

    for cmd in commands:
        ret, _, stderr = run_cmd(cmd)
        if ret != 0:
            print(f"  ERROR: Failed to install wrapper: {stderr}")
            Path(tmp_path).unlink()
            return False

    Path(tmp_path).unlink()

    # Also create separate wrapper for clang++ that forces C++ mode
    # Break the clang++ -> clang symlink and create a wrapper that forces --driver-mode=g++
    wrapper_for_cxx = f'''#!/bin/bash
exec /tmp/linker_wrapper.sh {real_clang}.real --driver-mode=g++ "$@"
'''
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        f.write(wrapper_for_cxx)
        tmp_path = f.name

    commands = [
        f"docker cp {tmp_path} {container_name}:/tmp/clangxx_wrapper.sh",
        f"docker exec {container_name} rm -f /usr/local/bin/clang++",  # Remove symlink
        f"docker exec {container_name} cp /tmp/clangxx_wrapper.sh /usr/local/bin/clang++",
        f"docker exec {container_name} chmod +x /usr/local/bin/clang++",
    ]

    for cmd in commands:
        ret, _, stderr = run_cmd(cmd)
        if ret != 0:
            print(f"  ERROR: Failed to install clang++ wrapper: {stderr}")
            Path(tmp_path).unlink()
            return False

    Path(tmp_path).unlink()
    print(f"  Installed linker wrapper on {real_clang} and clang++")
    return True


def run_arvo_compile(container_name: str) -> bool:
    """Run arvo compile in the container"""
    print("  Running arvo compile (this may take a few minutes)...")
    ret, stdout, stderr = run_cmd(
        f"docker exec {container_name} arvo compile",
        timeout=600
    )

    if ret != 0:
        print(f"  WARNING: arvo compile returned {ret}")
        # Show last few lines
        lines = (stdout + stderr).strip().split('\n')
        for line in lines[-5:]:
            print(f"    {line}")

        # Check if fuzzer was still built
        ret2, out_files, _ = run_cmd(f"docker exec {container_name} ls /out/")
        if ret2 == 0 and out_files.strip():
            print(f"  /out/ contains: {', '.join(out_files.strip().split())}")
            return True
        return False

    print("  arvo compile completed successfully")
    return True


def parse_linker_map(container_name: str, fuzzer_name: str) -> dict:
    """
    Parse the linker map to extract linked object files and libraries.
    """
    result = {
        'object_files': [],
        'archive_objects': {},
        'all_files': [],
        'map_path': None,
    }

    # List /out/ to find map files
    ret, out_files, _ = run_cmd(f"docker exec {container_name} ls -la /out/")
    print(f"  /out/ contents:\n{out_files}")

    # Try different map file names
    map_paths = [
        f"/out/{fuzzer_name}.map",
        "/out/linker.map",
    ]

    linker_map = None
    for map_path in map_paths:
        ret, content, _ = run_cmd(f"docker exec {container_name} cat '{map_path}'")
        if ret == 0 and content.strip() and 'Archive member' in content:
            linker_map = content
            result['map_path'] = map_path
            print(f"  Found linker map at {map_path} ({len(content)} bytes)")
            break

    if not linker_map:
        print(f"  No linker map found")
        return result

    # Parse the linker map
    # Format: " .text   0xADDR   SIZE  /path/to/file.o"
    # Or: " .text   0xADDR   SIZE  /path/to/lib.a(file.o)"
    text_pattern = re.compile(r'^\s*\.text\s+0x[0-9a-f]+\s+0x[0-9a-f]+\s+(.+)$')

    seen = set()
    for line in linker_map.split('\n'):
        match = text_pattern.match(line)
        if not match:
            continue

        file_path = match.group(1).strip()
        if file_path in seen:
            continue
        seen.add(file_path)

        # Skip compiler runtime and system files
        skip_patterns = ['clang_rt', 'crtbegin', 'crti', 'crtn', 'crt1', '/usr/lib/gcc/']
        if any(skip in file_path for skip in skip_patterns):
            continue

        # Check if it's from an archive: lib.a(file.o)
        archive_match = re.match(r'(.+\.a)\((.+\.o)\)', file_path)
        if archive_match:
            archive = archive_match.group(1)
            obj = archive_match.group(2)
            if archive not in result['archive_objects']:
                result['archive_objects'][archive] = []
            result['archive_objects'][archive].append(obj)
            result['all_files'].append(file_path)
        elif file_path.endswith('.o'):
            result['object_files'].append(file_path)
            result['all_files'].append(file_path)

    return result


def copy_linked_files(container_name: str, task_id: str, linked_objects: list, linked_archives: dict, output_dir: Path) -> dict:
    """
    Copy linked object files and archives from the container to local directory.

    Returns dict with paths to copied files.
    """
    task_output_dir = output_dir / task_id
    task_output_dir.mkdir(parents=True, exist_ok=True)

    copied = {
        'objects': [],
        'archives': [],
        'fuzzer': None,
    }

    # Get the container's working directory (where relative paths are resolved from)
    ret, workdir, _ = run_cmd(f"docker exec {container_name} pwd")
    workdir = workdir.strip() if ret == 0 else "/src"
    print(f"    Container workdir: {workdir}")

    def resolve_path(path: str) -> str:
        """Resolve relative paths using container's workdir."""
        if path.startswith('./'):
            return f"{workdir}/{path[2:]}"
        elif not path.startswith('/'):
            return f"{workdir}/{path}"
        return path

    # Copy object files (only project-specific ones)
    for obj_path in linked_objects:
        # Skip temp files and system files
        if obj_path.startswith('/tmp/') or obj_path.startswith('/usr/') or obj_path.startswith('/lib/'):
            continue

        obj_name = Path(obj_path).name
        docker_path = resolve_path(obj_path)

        local_path = task_output_dir / 'objects' / obj_name
        local_path.parent.mkdir(parents=True, exist_ok=True)

        ret, _, _ = run_cmd(f"docker cp {container_name}:{docker_path} {local_path}")
        if ret == 0 and local_path.exists():
            copied['objects'].append(str(local_path.relative_to(output_dir)))
            print(f"    Copied: {obj_name}")

    # Copy archive files (only project-specific ones)
    for archive_path in linked_archives.keys():
        # Skip system libraries
        if archive_path.startswith('/usr/') or archive_path.startswith('/lib/'):
            continue

        archive_name = Path(archive_path).name
        docker_path = resolve_path(archive_path)

        local_path = task_output_dir / 'libs' / archive_name
        local_path.parent.mkdir(parents=True, exist_ok=True)

        ret, _, _ = run_cmd(f"docker cp {container_name}:{docker_path} {local_path}")
        if ret == 0 and local_path.exists():
            size_kb = local_path.stat().st_size / 1024
            copied['archives'].append(str(local_path.relative_to(output_dir)))
            print(f"    Copied: {archive_name} ({size_kb:.1f} KB)")

    # Also copy the fuzzer binary
    ret, out_files, _ = run_cmd(f"docker exec {container_name} ls /out/")
    if ret == 0:
        for f in out_files.strip().split():
            # Skip non-binary files
            if f.endswith(('.map', '.zip', '.dict', '.options')):
                continue

            fuzzer_path = task_output_dir / 'bin' / f
            fuzzer_path.parent.mkdir(parents=True, exist_ok=True)

            ret, _, _ = run_cmd(f"docker cp {container_name}:/out/{f} {fuzzer_path}")
            if ret == 0 and fuzzer_path.exists():
                size_mb = fuzzer_path.stat().st_size / (1024 * 1024)
                print(f"    Copied fuzzer: {f} ({size_mb:.1f} MB)")
                if copied['fuzzer'] is None:
                    copied['fuzzer'] = str(fuzzer_path.relative_to(output_dir))

    return copied


def analyze_task(task_id: str, data_dir: Path, output_dir: Path | None = None) -> dict:
    """
    Analyze a single ARVO task.
    """
    result = {
        'task_id': task_id,
        'fuzzer': None,
        'linked_objects': [],
        'linked_archives': {},
        'copied_files': {},
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

    try:
        # Install linker wrapper to automatically generate maps
        if not install_linker_wrapper(container_name):
            result['error'] = 'Failed to install linker wrapper'
            return result

        # Run arvo compile
        if not run_arvo_compile(container_name):
            result['error'] = 'arvo compile failed'
            return result

        # Parse linker map
        linked = parse_linker_map(container_name, fuzzer_name)
        result['linked_objects'] = linked['object_files']
        result['linked_archives'] = linked['archive_objects']

        # Copy files to output directory
        if output_dir and (linked['object_files'] or linked['archive_objects']):
            print(f"\n  Copying files to {output_dir / task_id}...")
            result['copied_files'] = copy_linked_files(
                container_name, task_id,
                linked['object_files'], linked['archive_objects'],
                output_dir
            )

    finally:
        # Cleanup
        print("  Stopping container...")
        stop_container(container_name)

    return result


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Extract linked libraries from ARVO tasks using linker maps")
    parser.add_argument("task_id", type=str, help="Task ID (e.g., 368)")
    parser.add_argument("--output", "-o", type=Path, default=Path('/mnt/jailbreak-defense/exp/winniex/cybergym/executables/deps.json'),
                        help="Output JSON file for dependency info")
    parser.add_argument("--files-dir", "-d", type=Path, default=Path('/mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo'),
                        help="Output directory to copy linked files")
    args = parser.parse_args()

    task_id = args.task_id
    data_dir = Path('/mnt/jailbreak-defense/exp/winniex/cybergym/cybergym_data/data/arvo')
    output_file = args.output
    files_dir = args.files_dir

    print("=" * 80)
    print("ARVO Linked Libraries Extraction (via Linker Maps)")
    print("=" * 80)
    print(f"Files output directory: {files_dir}")

    results = []
    all_results_json = {}

    # Load existing results if file exists
    if output_file.exists():
        with open(output_file) as f:
            all_results_json = json.load(f)

    tasks = [task_id]
    for task_id in tasks:
        print(f"\n{'='*60}")
        print(f"Processing Task: {task_id}")
        print('='*60)

        result = analyze_task(task_id, data_dir, output_dir=files_dir)
        results.append(result)

        # Print results
        if result.get('error'):
            print(f"\n  ERROR: {result['error']}")
        else:
            print(f"\n  Results for task {task_id}:")
            print(f"  Fuzzer: {result['fuzzer']}")

            # Filter to project-specific objects (exclude /tmp/, /lib/, /usr/lib except libarchive)
            project_objects = [
                obj for obj in result['linked_objects']
                if obj.startswith('./') or obj.startswith('/src/')
            ]

            if project_objects:
                print(f"\n  Project object files ({len(project_objects)}):")
                for obj in sorted(project_objects)[:15]:
                    print(f"    - {obj}")
                if len(project_objects) > 15:
                    print(f"    ... and {len(project_objects) - 15} more")

            if result['linked_archives']:
                print(f"\n  Static libraries linked:")
                for archive, objs in sorted(result['linked_archives'].items()):
                    # Categorize libraries
                    if 'clang_rt' in archive or '/usr/lib/gcc/' in archive:
                        continue  # Skip compiler runtime

                    lib_type = "PROJECT" if '/src/' in archive or archive.startswith('./') else "SYSTEM"
                    print(f"    [{lib_type}] {archive} ({len(objs)} objects)")

                    # Show first few objects for project libs
                    if lib_type == "PROJECT":
                        for obj in sorted(objs)[:3]:
                            print(f"           - {obj}")
                        if len(objs) > 3:
                            print(f"           ... and {len(objs) - 3} more")

        # Store for JSON output
        all_results_json[f"arvo:{task_id}"] = {
            'fuzzer': result['fuzzer'],
            'linked_objects': result['linked_objects'],
            'linked_archives': result['linked_archives'],
            'copied_files': result.get('copied_files', {}),
            'error': result.get('error'),
        }

    # Write JSON output
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, 'w') as f:
        json.dump(all_results_json, f, indent=2)
    print(f"\nResults written to {output_file}")

    print("\n" + "=" * 80)
    print("Summary")
    print("=" * 80)
    for r in results:
        status = "OK" if not r.get('error') else f"ERROR: {r['error']}"
        n_objs = len(r.get('linked_objects', []))
        n_archives = len(r.get('linked_archives', {}))
        print(f"  Task {r['task_id']}: {status} ({n_objs} objects, {n_archives} archives)")


if __name__ == '__main__':
    main()
