#!/usr/bin/env python3
"""
Extract functions using clang-extract from compile_commands.json.

Spins up a docker container, runs compile, then clang-extract, and copies out the result.

Usage:
    # Auto-discover which file contains the function:
    uv run scripts/extract_clang_lib.py <task_id> <function_name>

    # Or specify the index manually:
    uv run scripts/extract_clang_lib.py <task_id> <function_name> --idx 85

Example:
    uv run scripts/extract_clang_lib.py 1065 magic_buffer
    uv run scripts/extract_clang_lib.py 3938 yr_compiler_add_string
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path


COMPILE_COMMANDS_DIR = Path("/mnt/jailbreak-defense/exp/winniex/cybergym/compile_commands")
OUTPUT_DIR = Path("/mnt/jailbreak-defense/exp/winniex/cybergym/standalone_libs")
CLANG_EXTRACT_PATH = "/opt/clang-extract/clang-extract-wrapper"


def find_index_from_object_files(
    object_files: list[str],
    compile_commands: list[dict],
) -> tuple[int, str] | None:
    """Match .o files to compile_commands.json entries."""
    # Try to match .o file to source file in compile_commands
    for obj_file in object_files:
        # Extract base name: /path/to/foo.o -> foo
        obj_base = Path(obj_file).stem

        # Search compile_commands for matching source file
        for idx, entry in enumerate(compile_commands):
            source_file = entry.get("file", "")
            source_base = Path(source_file).stem

            if source_base == obj_base:
                print(f"Matched: {obj_file} -> {source_file} (index {idx})")
                return idx, source_file

    # If no exact match, try to find by looking at -o argument in compile commands
    for obj_file in object_files:
        for idx, entry in enumerate(compile_commands):
            args = entry.get("arguments", [])
            for i, arg in enumerate(args):
                if arg == "-o" and i + 1 < len(args):
                    output_file = args[i + 1]
                    if Path(output_file).name == Path(obj_file).name:
                        source_file = entry.get("file", "")
                        print(f"Matched via -o: {obj_file} -> {source_file} (index {idx})")
                        return idx, source_file

    return None


def find_function_file_only(task_id: str, function_name: str) -> tuple[int, str] | None:
    """
    Find which file contains a function definition (--find-only mode).
    Runs in a separate container just for discovery.
    """
    docker_image = f"arvo:{task_id}-vul-ce"

    find_script = f'''
set -e
echo "=== Running arvo compile ==="
arvo compile
echo "=== Compile complete ==="

echo "=== Searching for {function_name} in object files ==="
for o in $(find /src /work -name "*.o" 2>/dev/null); do
    if nm "$o" 2>/dev/null | grep -q " T {function_name}$"; then
        echo "FOUND_OBJ:$o"
    fi
done
'''

    print(f"Searching for function '{function_name}' in task {task_id}...")
    print("=" * 60)

    process = subprocess.Popen(
        ["docker", "run", "--rm", docker_image, "bash", "-c", find_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    output_lines = []
    for line in process.stdout:
        print(line, end="")
        output_lines.append(line.rstrip("\n"))

    process.wait()
    print("=" * 60)

    if process.returncode != 0:
        print(f"Error: Search failed with return code {process.returncode}", file=sys.stderr)
        return None

    # Parse the .o files found
    object_files = [line.replace("FOUND_OBJ:", "").strip() for line in output_lines
                    if line.startswith("FOUND_OBJ:")]

    if not object_files:
        print(f"Error: Function '{function_name}' not found in any object file", file=sys.stderr)
        return None

    print(f"Found in object files: {object_files}")

    # Load compile_commands.json
    compile_commands_path = COMPILE_COMMANDS_DIR / f"{task_id}.json"
    with open(compile_commands_path) as f:
        compile_commands = json.load(f)

    return find_index_from_object_files(object_files, compile_commands)


def filter_args_for_clang_extract(args: list[str]) -> list[str]:
    """
    Filter compile arguments to keep only those needed for clang-extract.

    Keep: -D*, -I*, -std=*, -include
    Skip: -W*, -f*, -g*, -O*, -M*, -c, -o, output files, sanitizer flags, etc.
    """
    filtered = []
    skip_next = False

    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue

        # Skip the compiler itself
        if arg.endswith("clang") or arg.endswith("clang++"):
            continue

        # Skip output-related flags
        if arg in ["-c", "-o", "-MT", "-MD", "-MP", "-MF"]:
            skip_next = arg in ["-o", "-MT", "-MF"]  # These take an argument
            continue

        # Skip warning flags
        if arg.startswith("-W"):
            continue

        # Skip optimization flags
        if arg.startswith("-O"):
            continue

        # Skip debug flags
        if arg.startswith("-g"):
            continue

        # Skip sanitizer flags
        if arg.startswith("-fsanitize") or arg.startswith("-fno-sanitize"):
            continue

        # Skip other -f flags
        if arg.startswith("-f"):
            continue

        # Skip PIC flags
        if arg in ["-fPIC", "-DPIC", "-fpic"]:
            continue

        # Skip .o, .lo output files
        if arg.endswith(".o") or arg.endswith(".lo"):
            continue

        # Skip .Tpo dependency files
        if arg.endswith(".Tpo"):
            continue

        # Keep -D defines
        if arg.startswith("-D"):
            filtered.append(arg)
            continue

        # Keep -I includes
        if arg.startswith("-I"):
            filtered.append(arg)
            continue

        # Keep -std flags
        if arg.startswith("-std"):
            filtered.append(arg)
            continue

        # Keep -include flags
        if arg == "-include":
            filtered.append(arg)
            continue

        # Keep source files (.c, .cc, .cpp)
        if arg.endswith((".c", ".cc", ".cpp", ".cxx")):
            filtered.append(arg)
            continue

    return filtered


def main():
    parser = argparse.ArgumentParser(
        description="Extract functions using clang-extract from compile_commands.json"
    )
    parser.add_argument("task_id", type=str, help="Task ID (e.g., 1065, 3938)")
    parser.add_argument("function", type=str, help="Function name to extract")
    parser.add_argument(
        "--idx", "-i",
        type=int,
        default=None,
        help="Index into compile_commands.json (auto-discovered if not provided)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Print the command without executing",
    )
    parser.add_argument(
        "--keep-includes",
        action="store_true",
        help="Add -DCE_KEEP_INCLUDES flag",
    )
    parser.add_argument(
        "--find-only",
        action="store_true",
        help="Only find the file containing the function, don't extract",
    )

    args = parser.parse_args()

    # Load compile_commands.json
    compile_commands_path = COMPILE_COMMANDS_DIR / f"{args.task_id}.json"
    if not compile_commands_path.exists():
        print(f"Error: {compile_commands_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(compile_commands_path) as f:
        compile_commands = json.load(f)

    # Handle --find-only mode (just discover, don't extract)
    if args.find_only:
        if args.idx is not None:
            print("Error: --find-only doesn't make sense with --idx", file=sys.stderr)
            sys.exit(1)
        result = find_function_file_only(args.task_id, args.function)
        if result is None:
            sys.exit(1)
        idx, source_file = result
        print(f"\nFunction '{args.function}' found in: {source_file} (index {idx})")
        print(f"To extract, run:")
        print(f"  uv run scripts/extract_clang_lib.py {args.task_id} {args.function} --idx {idx}")
        return

    # If --idx provided, we know the file already
    if args.idx is not None:
        idx = args.idx
        if idx < 0 or idx >= len(compile_commands):
            print(f"Error: Index {idx} out of range (0-{len(compile_commands)-1})", file=sys.stderr)
            sys.exit(1)
        entry = compile_commands[idx]
        directory = entry["directory"]
        source_file = entry.get("file")

        # Get compile args
        if "arguments" in entry:
            compile_args = entry["arguments"]
        elif "command" in entry:
            compile_args = shlex.split(entry["command"])
        else:
            print("Error: Entry has neither 'arguments' nor 'command'", file=sys.stderr)
            sys.exit(1)

        filtered_args = [a for a in filter_args_for_clang_extract(compile_args)
                         if not a.endswith((".c", ".cc", ".cpp", ".cxx"))]
        ce_args = " ".join(shlex.quote(a) for a in filtered_args)
        keep_includes_flag = "-DCE_KEEP_INCLUDES" if args.keep_includes else ""
        container_output = f"/tmp/{args.function}.c"

        shell_script = f'''
set -e

echo "=== Running arvo compile ==="
arvo compile
echo "=== Compile complete ==="

CLANG_INCLUDE=$(dirname "$(find /usr -name stddef.h 2>/dev/null | grep clang | head -1)")
echo "Clang include path: $CLANG_INCLUDE"

cd "{directory}"
echo "Working directory: $(pwd)"

echo "=== Running clang-extract ==="
{CLANG_EXTRACT_PATH} \\
    {ce_args} \\
    -I"$CLANG_INCLUDE" \\
    -DCE_EXTRACT_FUNCTIONS={args.function} \\
    -DCE_OUTPUT_FILE={container_output} \\
    {keep_includes_flag} \\
    {shlex.quote(source_file)}

echo "=== Extraction complete: {container_output} ==="
cat {container_output}
'''
    else:
        # Auto-discover mode: compile, find, and extract all in one container
        keep_includes_flag = "-DCE_KEEP_INCLUDES" if args.keep_includes else ""
        container_output = f"/tmp/{args.function}.c"

        # Build shell script that does everything
        shell_script = f'''
set -e

echo "=== Running arvo compile ==="
arvo compile
echo "=== Compile complete ==="

echo "=== Searching for {args.function} in object files ==="
OBJ_FILE=""
for o in $(find /src /work -name "*.o" 2>/dev/null); do
    if nm "$o" 2>/dev/null | grep -q " T {args.function}$"; then
        echo "Found: $o"
        OBJ_FILE="$o"
        break
    fi
done

if [ -z "$OBJ_FILE" ]; then
    echo "Error: Function {args.function} not found in any object file"
    exit 1
fi

# Extract base name to find source file
OBJ_BASE=$(basename "$OBJ_FILE" .o)
echo "Looking for source file matching: $OBJ_BASE"

# We'll let Python figure out the exact compile flags
# For now, output the object file path for Python to parse
echo "FOUND_OBJ:$OBJ_FILE"
'''
        # For auto-discover, we need a two-phase approach within the same container
        # Phase 1: compile + find
        # Phase 2: clang-extract with the right flags
        #
        # Actually, let's do it differently - run one container that does everything
        # by having Python build the full script dynamically

        # We need to embed all possible compile commands in the script
        # This is getting complex - let's use a simpler approach:
        # Use docker exec to keep the container alive

        idx = None
        directory = None
        source_file = None

    # Docker image name
    docker_image = f"arvo:{args.task_id}-vul-ce"

    # Output directory
    output_dir = OUTPUT_DIR / args.task_id / args.function
    output_file = output_dir / f"{args.function}.c"

    if args.idx is not None:
        print(f"Task ID: {args.task_id}")
        print(f"Index: {idx}")
        print(f"Function: {args.function}")
        print(f"Directory: {directory}")
        print(f"Source file: {source_file}")
        print(f"Docker image: {docker_image}")
        print(f"Output: {output_file}")
        print()

        if args.dry_run:
            print("=== Shell script to run in container ===")
            print(shell_script)
            print("(dry run - not executing)")
            return

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # Run docker container
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{compile_commands_path}:/src/compile_commands.json:ro",
            docker_image,
            "bash", "-c", shell_script
        ]

        print(f"Running docker container...")
        print("=" * 60)

        process = subprocess.Popen(
            docker_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        output_lines = []
        for line in process.stdout:
            print(line, end="")
            output_lines.append(line.rstrip("\n"))

        process.wait()
        print("=" * 60)

        if process.returncode != 0:
            print(f"Error: clang-extract failed with return code {process.returncode}", file=sys.stderr)
            sys.exit(process.returncode)

        # Extract the generated code from stdout
        extraction_start = None
        for i, line in enumerate(output_lines):
            if "Extraction complete:" in line:
                extraction_start = i + 1
                break

        if extraction_start is None:
            print(f"Error: Could not find extraction output", file=sys.stderr)
            sys.exit(1)

        extracted_code = "\n".join(output_lines[extraction_start:])
    else:
        # Auto-discover mode: use docker exec to keep container alive
        container_name = f"clang_extract_{args.task_id}_{uuid.uuid4().hex[:8]}"

        print(f"Task ID: {args.task_id}")
        print(f"Function: {args.function}")
        print(f"Docker image: {docker_image}")
        print(f"Output: {output_file}")
        print()

        if args.dry_run:
            print("(dry run - would start container and auto-discover)")
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Start container
            print(f"Starting container {container_name}...")
            subprocess.run(
                ["docker", "run", "-d", "--name", container_name, docker_image, "sleep", "infinity"],
                check=True, capture_output=True
            )

            # Phase 1: Compile and find
            print("=" * 60)
            find_script = f'''
set -e
echo "=== Running arvo compile ==="
arvo compile
echo "=== Compile complete ==="

echo "=== Searching for {args.function} in object files ==="
for o in $(find /src /work -name "*.o" 2>/dev/null); do
    if nm "$o" 2>/dev/null | grep -q " T {args.function}$"; then
        echo "FOUND_OBJ:$o"
    fi
done
'''
            process = subprocess.Popen(
                ["docker", "exec", container_name, "bash", "-c", find_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            output_lines = []
            for line in process.stdout:
                print(line, end="")
                output_lines.append(line.rstrip("\n"))
            process.wait()

            if process.returncode != 0:
                print(f"Error: Compile/search failed", file=sys.stderr)
                sys.exit(process.returncode)

            # Parse object files
            object_files = [line.replace("FOUND_OBJ:", "").strip() for line in output_lines
                            if line.startswith("FOUND_OBJ:")]

            if not object_files:
                print(f"Error: Function '{args.function}' not found", file=sys.stderr)
                sys.exit(1)

            print(f"Found in: {object_files}")

            # Match to compile_commands
            result = find_index_from_object_files(object_files, compile_commands)
            if result is None:
                print(f"Error: Could not match to compile_commands.json", file=sys.stderr)
                sys.exit(1)

            idx, source_file = result
            entry = compile_commands[idx]
            directory = entry["directory"]

            if "arguments" in entry:
                compile_args = entry["arguments"]
            else:
                compile_args = shlex.split(entry["command"])

            filtered_args = [a for a in filter_args_for_clang_extract(compile_args)
                             if not a.endswith((".c", ".cc", ".cpp", ".cxx"))]
            ce_args = " ".join(shlex.quote(a) for a in filtered_args)
            keep_includes_flag = "-DCE_KEEP_INCLUDES" if args.keep_includes else ""
            container_output = f"/tmp/{args.function}.c"

            # Phase 2: Extract
            print(f"\nIndex: {idx}, Source: {source_file}")
            print("=" * 60)

            extract_script = f'''
set -e
CLANG_INCLUDE=$(dirname "$(find /usr -name stddef.h 2>/dev/null | grep clang | head -1)")
echo "Clang include path: $CLANG_INCLUDE"

cd "{directory}"
echo "Working directory: $(pwd)"

echo "=== Running clang-extract ==="
{CLANG_EXTRACT_PATH} \\
    {ce_args} \\
    -I"$CLANG_INCLUDE" \\
    -DCE_EXTRACT_FUNCTIONS={args.function} \\
    -DCE_OUTPUT_FILE={container_output} \\
    {keep_includes_flag} \\
    {shlex.quote(source_file)}

echo "=== Extraction complete: {container_output} ==="
cat {container_output}
'''
            process = subprocess.Popen(
                ["docker", "exec", container_name, "bash", "-c", extract_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            output_lines = []
            for line in process.stdout:
                print(line, end="")
                output_lines.append(line.rstrip("\n"))
            process.wait()
            print("=" * 60)

            if process.returncode != 0:
                print(f"Error: clang-extract failed with return code {process.returncode}", file=sys.stderr)
                sys.exit(process.returncode)

            # Extract the generated code
            extraction_start = None
            for i, line in enumerate(output_lines):
                if "Extraction complete:" in line:
                    extraction_start = i + 1
                    break

            if extraction_start is None:
                print(f"Error: Could not find extraction output", file=sys.stderr)
                sys.exit(1)

            extracted_code = "\n".join(output_lines[extraction_start:])

        finally:
            # Clean up container
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # Write to output file
    output_file.write_text(extracted_code)

    print(f"\nSuccess! Output written to: {output_file}")
    print(f"Lines: {len(extracted_code.splitlines())}")


if __name__ == "__main__":
    main()
