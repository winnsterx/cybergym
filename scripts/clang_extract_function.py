#!/usr/bin/env python3
"""
Extract standalone functions from arvo tasks using clang-extract.

Adapted from DecompileBench's extract_functions.py to work with arvo Docker containers
that have clang-extract pre-installed (via build-arvo-ce.sh).

Usage:
    # List source files from compile_commands.json
    uv run scripts/clang_extract_function.py --task 368 --list-sources

    # Extract a function
    uv run scripts/clang_extract_function.py --task 368 --function cff_blend_doBlend --source /src/freetype2/src/cff/cff.c

    # Extract multiple functions
    uv run scripts/clang_extract_function.py --task 368 --function "func1,func2" --source /src/freetype2/src/cff/cff.c

Prerequisites:
    1. Generate compile_commands.json:
       ./scripts/gen_compile_commands.sh 368 compile_commands/
    2. Build the arvo-ce container:
       ./scripts/build-arvo-ce.sh 368
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Dict, Any

# Pattern to match line number directives from preprocessor
LINE_NO_DIRECTIVE_PATTERN = re.compile(r'^# \d+ ')

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
COMPILE_COMMANDS_DIR = REPO_ROOT / "compile_commands"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "clang_extracted"


def load_compile_commands(task_id: str) -> List[Dict[str, Any]]:
    """Load compile_commands.json for a given task."""
    cc_path = COMPILE_COMMANDS_DIR / f"{task_id}.json"
    if not cc_path.exists():
        raise FileNotFoundError(
            f"Compile commands not found: {cc_path}\n"
            f"Generate with: ./scripts/gen_compile_commands.sh {task_id} compile_commands/"
        )
    with open(cc_path) as f:
        return json.load(f)


def find_compile_command(
    compile_commands: List[Dict[str, Any]],
    source_file: str
) -> Optional[Dict[str, Any]]:
    """Find the compile command for a source file."""
    # Try exact match first
    for cmd in compile_commands:
        if cmd['file'] == source_file:
            return cmd

    # Try basename match
    source_basename = Path(source_file).name
    for cmd in compile_commands:
        if Path(cmd['file']).name == source_basename:
            return cmd

    # Try partial path match (source_file as suffix)
    for cmd in compile_commands:
        if cmd['file'].endswith(source_file):
            return cmd

    # Try source_file as substring
    for cmd in compile_commands:
        if source_file in cmd['file']:
            return cmd

    return None


def get_compile_args(cmd_info: Dict[str, Any]) -> List[str]:
    """Extract compile arguments, removing output-related flags and normalizing paths."""
    args = cmd_info['arguments'][1:]  # Skip compiler path
    cwd = cmd_info['directory']

    # Remove -o and its argument, and -c
    result = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == '-o':
            skip_next = True
            continue
        if arg == '-c':
            continue

        # Convert relative include paths to absolute
        if arg.startswith('-I') and not arg.startswith('-I/'):
            rel_path = arg[2:]
            if rel_path.startswith('./'):
                rel_path = rel_path[2:]
            arg = f'-I{cwd}/{rel_path}'

        result.append(arg)

    return result


def run_extraction_in_container(
    task_id: str,
    compile_args: List[str],
    function_name: str,
    cwd: str,
    extra_includes: List[str] = None,
    no_externalization: bool = True,
    verbose: bool = False
) -> Optional[str]:
    """
    Run clang-extract inside the container and return the extracted code.

    Returns the extracted code as a string, or None if extraction failed.
    """
    container_image = f"arvo:{task_id}-vul-ce"
    output_file = f"/tmp/{function_name}.c"

    # Build the clang-extract command
    # System includes for clang builtins - try multiple clang versions
    ce_includes = [
        '-I/usr/local/lib/clang/18/include',
        '-I/usr/local/lib/clang/17/include',
        '-I/usr/local/lib/clang/16/include',
        '-I/usr/local/lib/clang/15.0.0/include',
        '-I/usr/local/lib/clang/15/include',
        '-I/usr/local/lib/clang/14/include',
        '-I/usr/local/include',
        '-I/usr/include/x86_64-linux-gnu',
        '-I/usr/include',
    ]

    # Add user-specified extra includes
    if extra_includes:
        ce_includes.extend([f'-I{p}' for p in extra_includes])

    # Use direct path to clang-extract wrapper (symlink has path resolution issues)
    clang_extract_bin = '/opt/clang-extract/clang-extract-wrapper'

    ce_args = [clang_extract_bin] + ce_includes + compile_args + [
        f'-DCE_EXTRACT_FUNCTIONS={function_name}',
        f'-DCE_OUTPUT_FILE={output_file}',
    ]
    if no_externalization:
        ce_args.append('-DCE_NO_EXTERNALIZATION')

    ce_cmd = ' '.join(f'"{a}"' if ' ' in a or '<' in a or '>' in a else a for a in ce_args)

    # Build a script that does extraction and outputs the file
    # Errors go to stderr which we capture
    script = f'''
set -e
cd {cwd}

# Try direct extraction (capture stderr for error reporting)
if {ce_cmd} 2>&1 | tee /tmp/ce_stderr.txt | grep -q "^Error"; then
    # clang-extract failed
    cat /tmp/ce_stderr.txt >&2
    exit 1
fi

# Check if output was created
if [ -f "{output_file}" ]; then
    cat "{output_file}"
    exit 0
fi

# No output file created, show errors
if [ -f /tmp/ce_stderr.txt ]; then
    cat /tmp/ce_stderr.txt >&2
fi
exit 1
'''

    if verbose:
        print(f"Running extraction in container {container_image}...")
        print(f"  Working directory: {cwd}")
        print(f"  Function: {function_name}")

    try:
        result = subprocess.run(
            ['docker', 'run', '--rm', '-w', cwd, container_image, 'sh', '-c', script],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        # Show useful error messages
        stderr = e.stderr if e.stderr else ""
        if "fatal error:" in stderr:
            # Extract the fatal error line
            for line in stderr.split('\n'):
                if 'fatal error:' in line:
                    print(f"  clang error: {line.strip()}", file=sys.stderr)
                    break
        elif verbose:
            print(f"Extraction failed: {stderr[:500]}", file=sys.stderr)
        return None


def extract_function(
    task_id: str,
    function_name: str,
    source_file: str,
    output_dir: Path,
    extra_includes: List[str] = None,
    no_externalization: bool = True,
    verbose: bool = False
) -> Optional[Path]:
    """
    Extract a function from an arvo task using clang-extract.

    Args:
        task_id: The arvo task ID (e.g., "368")
        function_name: Name of the function to extract
        source_file: Absolute path to the source file inside the container
                    (e.g., /src/freetype2/src/cff/cff.c)
        output_dir: Directory to save extracted functions
        extra_includes: Additional include paths (absolute paths inside container)
        no_externalization: If True, include full definitions (not just declarations)
        verbose: Print detailed progress

    Returns:
        Path to the extracted .c file, or None if extraction failed.
    """
    # Load compile commands
    compile_commands = load_compile_commands(task_id)

    if not compile_commands:
        print(f"No compile commands found for task {task_id}", file=sys.stderr)
        return None

    # Find the right compile command
    cmd_info = find_compile_command(compile_commands, source_file)
    if not cmd_info:
        print(f"Source file '{source_file}' not found in compile_commands.json", file=sys.stderr)
        print("Available files:", file=sys.stderr)
        for cmd in compile_commands[:10]:
            print(f"  - {cmd['file']}", file=sys.stderr)
        if len(compile_commands) > 10:
            print(f"  ... and {len(compile_commands) - 10} more", file=sys.stderr)
        return None

    cwd = cmd_info['directory']
    compile_args = get_compile_args(cmd_info)

    # Ensure source file is in compile_args
    source_in_args = cmd_info['file']
    if source_in_args not in compile_args:
        compile_args.append(source_in_args)

    # Setup output
    task_output_dir = output_dir / task_id
    task_output_dir.mkdir(parents=True, exist_ok=True)
    output_file_local = task_output_dir / f"{function_name}.c"

    print(f"Extracting {function_name} from {source_in_args}...")

    # Run extraction in container (single docker run that extracts and outputs)
    extracted_code = run_extraction_in_container(
        task_id, compile_args, function_name,
        cwd, extra_includes, no_externalization, verbose
    )

    if not extracted_code:
        print(f"Failed to extract {function_name}", file=sys.stderr)
        return None

    # Post-process: fix any remaining hidden visibility attributes
    extracted_code = extracted_code.replace(
        '__visibility__("hidden")',
        '__visibility__("default")'
    )

    with open(output_file_local, 'w') as f:
        f.write(extracted_code)

    line_count = len(extracted_code.splitlines())
    print(f"  -> {output_file_local} ({line_count} lines)")

    return output_file_local


def list_source_files(task_id: str) -> None:
    """List available source files in compile_commands.json."""
    compile_commands = load_compile_commands(task_id)
    print(f"Source files for task {task_id} ({len(compile_commands)} entries):\n")
    for cmd in compile_commands:
        print(f"  {cmd['file']}")
        print(f"      dir: {cmd['directory']}")


def main():
    parser = argparse.ArgumentParser(
        description='Extract standalone functions from arvo tasks using clang-extract',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        '--task', '-t', required=True,
        help='Arvo task ID (e.g., 368)'
    )
    parser.add_argument(
        '--function', '-f',
        help='Function name(s) to extract, comma-separated'
    )
    parser.add_argument(
        '--source', '-s',
        help='Absolute path to source file inside container '
             '(e.g., /src/freetype2/src/cff/cff.c). Required for extraction.'
    )
    parser.add_argument(
        '--output', '-o', default=str(DEFAULT_OUTPUT_DIR),
        help=f'Output directory (default: {DEFAULT_OUTPUT_DIR})'
    )
    parser.add_argument(
        '--list-sources', '-l', action='store_true',
        help='List available source files and exit'
    )
    parser.add_argument(
        '--include', '-I', action='append', dest='extra_includes',
        help='Extra include path (can be specified multiple times)'
    )
    parser.add_argument(
        '--externalize', '-e', action='store_true',
        help='Externalize undefined symbols (default: keep full definitions)'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Verbose output'
    )

    args = parser.parse_args()

    if args.list_sources:
        list_source_files(args.task)
        return

    if not args.function:
        parser.error("--function is required (unless using --list-sources)")

    if not args.source:
        parser.error("--source is required to specify which source file to extract from")

    function_names = [f.strip() for f in args.function.split(',')]
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {'extracted': [], 'failed': []}

    for function_name in function_names:
        result = extract_function(
            task_id=args.task,
            function_name=function_name,
            source_file=args.source,
            extra_includes=args.extra_includes,
            output_dir=output_dir,
            no_externalization=not args.externalize,
            verbose=args.verbose
        )

        if result:
            results['extracted'].append((function_name, str(result)))
        else:
            results['failed'].append(function_name)

    # Summary
    print("\n" + "=" * 50)
    print(f"Extracted: {len(results['extracted'])} / {len(function_names)}")
    if results['failed']:
        print(f"Failed: {', '.join(results['failed'])}")


if __name__ == '__main__':
    main()
