#!/usr/bin/env python3
"""
Extract standalone functions from arvo tasks.

Uses error.txt stack traces to find exact function→file mappings.

Pipeline:
  1. Parse error.txt for function→file:line mappings
  2. Extract function body from known source file
  3. Optionally compile to .so

Usage:
  # List available functions from error.txt
  uv run scripts/extract_standalone_functions.py --task 368 --list

  # Extract specific function (auto-finds source file from error.txt)
  uv run scripts/extract_standalone_functions.py --task 368 --functions cff_parse_num

  # Extract all functions from stack trace
  uv run scripts/extract_standalone_functions.py --task 368 --all
"""

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent
REPO_DIR = SCRIPT_DIR.parent
DATA_DIR = REPO_DIR / "cybergym_data" / "data" / "arvo"


def run_cmd(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    print(f"+ {' '.join(cmd[:5])}..." if len(cmd) > 5 else f"+ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, **kwargs)


def get_fuzzer_info(task_id: int) -> dict:
    """Get fuzzer name and function→file mappings from error.txt stack traces."""
    error_txt_path = DATA_DIR / str(task_id) / "error.txt"
    if not error_txt_path.exists():
        return {}

    content = error_txt_path.read_text()
    result = {
        'fuzzer': None,
        'functions': {},  # function_name -> {'file': path, 'line': num}
    }

    # Extract fuzzer name
    match = re.search(r'/out/([a-zA-Z0-9_-]+)', content)
    if match:
        result['fuzzer'] = match.group(1)

    # Parse stack traces: "#N 0xADDR in function_name /path/to/file.c:line:col"
    stack_pattern = re.compile(r'#\d+\s+0x[a-f0-9]+\s+in\s+(\w+)\s+(/[^:\s]+):(\d+)')

    for match in stack_pattern.finditer(content):
        func_name = match.group(1)
        file_path = match.group(2)
        line_num = int(match.group(3))

        # Skip fuzzer/llvm internal functions
        if any(skip in file_path for skip in ['llvm-project', 'compiler-rt', 'FuzzerLoop', 'FuzzerDriver']):
            continue

        # Only keep first occurrence of each function
        if func_name not in result['functions']:
            result['functions'][func_name] = {
                'file': file_path,
                'line': line_num,
            }

    return result


def list_available_functions(task_id: int) -> None:
    """List functions available from error.txt for a task."""
    info = get_fuzzer_info(task_id)
    if not info.get('functions'):
        print(f"No functions found in error.txt for task {task_id}")
        return

    print(f"\nFunctions from error.txt (task {task_id}):")
    print(f"Fuzzer: {info.get('fuzzer', 'unknown')}")
    print("-" * 60)
    for func_name, details in info['functions'].items():
        print(f"  {func_name}")
        print(f"      {details['file']}:{details['line']}")
    print(f"\nTotal: {len(info['functions'])} functions")


def get_docker_image(task_id: int, variant: str = "vul") -> str:
    """Get docker image name for arvo task."""
    return f"n132/arvo:{task_id}-{variant}"


def extract_function(
    task_id: int,
    function_name: str,
    source_file: str,
    output_dir: Path,
) -> Optional[Path]:
    """
    Extract a single function from known source file.
    Returns path to extracted .c file or None if failed.
    """
    output_c = output_dir / f"{function_name}.c"

    if output_c.exists():
        print(f"  {function_name}.c already exists, skipping")
        return output_c

    image = get_docker_image(task_id)

    # Simple extraction script using Python
    extract_script = f'''
SOURCE_FILE="{source_file}"

if [ ! -f "$SOURCE_FILE" ]; then
    echo "ERROR: Source file not found: $SOURCE_FILE"
    exit 1
fi

echo "Extracting {function_name} from $SOURCE_FILE"

python3 << 'PYEOF'
import re

func_name = "{function_name}"
source_file = "{source_file}"

with open(source_file, 'r', errors='ignore') as f:
    content = f.read()
    lines = content.split('\\n')

# Find function - look for name followed by (
func_pattern = re.compile(rf'^\\s*{{func_name}}\\s*\\(', re.MULTILINE)
match = func_pattern.search(content)

if not match:
    # Try with return type on same line
    func_pattern2 = re.compile(rf'\\b\\w+[\\s\\*]+{{func_name}}\\s*\\(', re.MULTILINE)
    match = func_pattern2.search(content)

if not match:
    print(f"ERROR: Could not find function {{func_name}} in {{source_file}}")
    exit(1)

# Find start line
start_pos = match.start()
start_line = content[:start_pos].count('\\n')

# Look backwards for return type / macro
actual_start = start_line
for i in range(start_line, max(0, start_line - 5), -1):
    line = lines[i].strip()
    if line and not line.startswith('//') and not line.startswith('/*'):
        if re.match(r'^(static|inline|extern|FT_|\\w+_t|void|int|char|unsigned|const|LOCAL_DEF|FT_LOCAL_DEF|FT_EXPORT_DEF)', line):
            actual_start = i
            break
        elif line.endswith(')') or line.endswith('}}'):
            actual_start = i + 1
            break

# Find function end - match braces
brace_count = 0
started = False
end_line = start_line

for i, line in enumerate(lines[start_line:], start=start_line):
    brace_count += line.count('{{') - line.count('}}')
    if '{{' in line:
        started = True
    if started and brace_count == 0:
        end_line = i
        break

# Extract function
func_lines = lines[actual_start:end_line + 1]
func_code = '\\n'.join(func_lines)

# Write output
output = []
output.append(f"/* Extracted function: {{func_name}} */")
output.append(f"/* Source: {{source_file}} */")
output.append("")
output.append("/* Standard includes */")
output.append("#include <stdint.h>")
output.append("#include <stddef.h>")
output.append("")
output.append("/* Function */")
output.append(func_code)

with open('/output/{function_name}.c', 'w') as f:
    f.write('\\n'.join(output))

print(f"SUCCESS: Extracted {{func_name}} ({{end_line - actual_start + 1}} lines)")
PYEOF
'''

    try:
        result = run_cmd([
            "docker", "run", "--rm",
            "-v", f"{output_dir}:/output",
            image,
            "bash", "-c", extract_script
        ], check=False, capture_output=True)
        stdout = result.stdout.decode() if result.stdout else ""
        print(stdout)
        if result.returncode != 0:
            stderr = result.stderr.decode() if result.stderr else ""
            print(f"  stderr: {stderr[-300:]}")
    except Exception as e:
        print(f"  Warning: extraction had issues: {e}")

    if output_c.exists() and output_c.stat().st_size > 0:
        return output_c
    return None


def compile_to_shared_object(c_file: Path, output_dir: Path, task_id: int) -> Optional[Path]:
    """Compile extracted .c file to .so shared object inside docker."""
    so_file = output_dir / f"{c_file.stem}.so"

    if so_file.exists():
        print(f"  {so_file.name} already exists, skipping")
        return so_file

    image = get_docker_image(task_id)

    # Compile inside docker where we have the right headers
    compile_script = f'''
clang -shared -fPIC -O2 -w -o /output/{c_file.stem}.so /output/{c_file.name} 2>&1 || echo "COMPILE_FAILED"
'''

    try:
        result = run_cmd([
            "docker", "run", "--rm",
            "-v", f"{output_dir}:/output",
            image,
            "bash", "-c", compile_script
        ], check=False, capture_output=True)
        stdout = result.stdout.decode() if result.stdout else ""
        if "COMPILE_FAILED" in stdout:
            print(f"  Failed to compile {c_file.name}")
            print(f"  {stdout[:300]}")
            return None
    except Exception as e:
        print(f"  Compile error: {e}")
        return None

    if so_file.exists():
        return so_file
    return None


def extract_arvo_task(
    task_id: int,
    functions: list[str],
    output_base: Path,
    skip_so: bool = False,
) -> dict:
    """Main extraction pipeline for an arvo task."""
    output_dir = output_base / f"arvo_{task_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get function→file mappings from error.txt
    fuzzer_info = get_fuzzer_info(task_id)
    if not fuzzer_info.get('functions'):
        return {
            "task_id": task_id,
            "error": "No functions found in error.txt",
            "extracted": [],
            "failed": functions,
        }

    results = {
        "task_id": task_id,
        "fuzzer": fuzzer_info.get('fuzzer'),
        "output_dir": str(output_dir),
        "extracted": [],
        "failed": [],
    }

    print(f"\n=== Extracting {len(functions)} functions from task {task_id} ===")
    print(f"Fuzzer: {fuzzer_info.get('fuzzer')}")

    for func in functions:
        print(f"\nExtracting {func}...")

        # Get source file from error.txt mapping
        func_info = fuzzer_info['functions'].get(func)
        if not func_info:
            print(f"  WARNING: {func} not found in error.txt stack trace")
            results['failed'].append(func)
            continue

        source_file = func_info['file']
        print(f"  Source: {source_file}:{func_info['line']}")

        c_file = extract_function(task_id, func, source_file, output_dir)

        if c_file:
            result_entry = {"function": func, "c_file": str(c_file), "source": source_file}

            if not skip_so:
                so_file = compile_to_shared_object(c_file, output_dir, task_id)
                if so_file:
                    result_entry["so_file"] = str(so_file)

            results["extracted"].append(result_entry)
        else:
            results["failed"].append(func)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Extract standalone functions from arvo tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task", type=int, required=True, help="Arvo task ID")
    parser.add_argument("--functions", type=str, help="Comma-separated function names")
    parser.add_argument("--all", action="store_true", help="Extract all functions from error.txt")
    parser.add_argument("--list", action="store_true", help="List available functions")
    parser.add_argument("--output", type=Path, default=REPO_DIR / "data" / "arvo_extract")
    parser.add_argument("--skip-so", action="store_true", help="Skip .so compilation")

    args = parser.parse_args()

    if args.list:
        list_available_functions(args.task)
        return

    args.output.mkdir(parents=True, exist_ok=True)

    # Get functions to extract
    if args.all:
        info = get_fuzzer_info(args.task)
        functions = list(info.get('functions', {}).keys())
        if not functions:
            print(f"No functions found in error.txt for task {args.task}")
            return
    elif args.functions:
        functions = [f.strip() for f in args.functions.split(",")]
    else:
        parser.error("Specify --functions, --all, or --list")

    results = extract_arvo_task(
        task_id=args.task,
        functions=functions,
        output_base=args.output,
        skip_so=args.skip_so,
    )

    # Print summary
    print("\n" + "=" * 60)
    print("EXTRACTION SUMMARY")
    print("=" * 60)
    print(f"Task: arvo:{args.task}")
    print(f"Output: {results.get('output_dir', 'N/A')}")
    print(f"Extracted: {len(results['extracted'])} functions")
    print(f"Failed: {len(results['failed'])} functions")

    if results["extracted"]:
        print("\nExtracted files:")
        for item in results["extracted"]:
            print(f"  - {item['function']}")
            print(f"      .c: {item.get('c_file', 'N/A')}")
            if "so_file" in item:
                print(f"      .so: {item['so_file']}")

    if results["failed"]:
        print(f"\nFailed: {', '.join(results['failed'])}")

    # Save results
    results_path = args.output / f"arvo_{args.task}" / "extraction_results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {results_path}")


if __name__ == "__main__":
    main()
