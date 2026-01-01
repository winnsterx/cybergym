#!/usr/bin/env python3
"""
Decompile fuzzer binaries using Ghidra headless mode.

Usage:
    # Single task
    uv run scripts/decompile_fuzzer.py 368
    uv run scripts/decompile_fuzzer.py 368 -f "LLVMFuzzerTestOneInput,yr_compiler_add_string"

    # Different strip levels
    uv run scripts/decompile_fuzzer.py 368 -s no-strip      # Full symbols
    uv run scripts/decompile_fuzzer.py 368 -s strip-debug   # Debug stripped (default)
    uv run scripts/decompile_fuzzer.py 368 -s strip-all     # Fully stripped

    # Multiple tasks from CSV
    uv run scripts/decompile_fuzzer.py task_lists/test.csv
    uv run scripts/decompile_fuzzer.py task_lists/test.csv -j 4  # parallel

Output is saved to: compiled_artifacts/arvo/{task}/{strip-level}/fuzzer/decompiled.c
"""

import argparse
import atexit
import csv
import signal
import subprocess
import tempfile
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Track temp directories and subprocesses for cleanup
_temp_dirs: list[Path] = []
_active_processes: list[subprocess.Popen] = []


def _cleanup():
    """Clean up temp directories and kill active processes."""
    for proc in _active_processes:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    for temp_dir in _temp_dirs:
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except Exception:
            pass


def _signal_handler(signum, frame):
    """Handle interrupt signals."""
    print("\nInterrupted, cleaning up...")
    _cleanup()
    exit(1)


# Register cleanup handlers
atexit.register(_cleanup)
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

REPO_ROOT = Path(__file__).parent.parent
COMPILED_ARTIFACTS = REPO_ROOT / "compiled_artifacts" / "arvo"

# Ghidra decompilation script template
DECOMPILE_SCRIPT = '''
# Ghidra script to decompile functions
# @category: Analysis

from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor

TARGET_FUNCTIONS = {target_functions}  # None means all functions

def decompile_functions():
    program = getCurrentProgram()
    ifc = DecompInterface()
    ifc.openProgram(program)
    monitor = ConsoleTaskMonitor()

    fm = program.getFunctionManager()
    funcs = fm.getFunctions(True)

    print("=" * 80)
    print("DECOMPILED: {{}}".format(program.getName()))
    print("=" * 80)
    print("")

    count = 0
    for func in funcs:
        func_name = func.getName()

        # Filter if specific functions requested
        if TARGET_FUNCTIONS is not None:
            if func_name not in TARGET_FUNCTIONS:
                continue

        func_addr = func.getEntryPoint()
        print("-" * 80)
        print("Function: {{}} @ 0x{{}}".format(func_name, func_addr))
        print("-" * 80)

        try:
            results = ifc.decompileFunction(func, 60, monitor)
            if results.decompileCompleted():
                decomp_func = results.getDecompiledFunction()
                if decomp_func:
                    print(decomp_func.getC())
                else:
                    print("// Unable to get decompiled code")
            else:
                print("// Decompilation failed: {{}}".format(results.getErrorMessage()))
        except Exception as e:
            print("// Error: {{}}".format(str(e)))

        print("")
        count += 1

    print("=" * 80)
    print("Total functions decompiled: {{}}".format(count))
    print("=" * 80)

    ifc.dispose()

decompile_functions()
'''


def parse_task_id(task_str: str) -> str:
    """Parse task ID from various formats like 'arvo:368' or '368'."""
    task_str = task_str.strip().strip('"').strip("'")
    if ":" in task_str:
        # Format: "arvo:368" -> "368"
        return task_str.split(":")[-1]
    return task_str


def load_tasks_from_csv(csv_path: Path) -> list[str]:
    """Load task IDs from a CSV file."""
    tasks = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Try common column names
            task_str = row.get("task") or row.get("task_id") or row.get("id")
            if task_str:
                task_id = parse_task_id(task_str)
                tasks.append(task_id)
    return tasks


def find_fuzzer_binaries(task_id: str, strip_level: str = "strip-debug") -> list[Path]:
    """Find all fuzzer binaries for a given task ID."""
    fuzzer_dir = COMPILED_ARTIFACTS / task_id / strip_level / "fuzzer"

    if not fuzzer_dir.exists():
        raise FileNotFoundError(f"Fuzzer directory not found: {fuzzer_dir}")

    # Find executable files (excluding .mgc, .dict, .options, etc.)
    binaries = []
    for f in fuzzer_dir.iterdir():
        if f.is_file() and f.suffix not in ['.mgc', '.dict', '.options', '.zip', '.c']:
            # Check if it's executable
            if os.access(f, os.X_OK):
                binaries.append(f)

    if not binaries:
        raise FileNotFoundError(f"No executable found in {fuzzer_dir}")

    return sorted(binaries)  # Sort for deterministic order


def run_ghidra_decompile(
    binary_path: Path,
    output_path: Path | None = None,
    functions: list[str] | None = None,
    project_dir: Path | None = None,
    quiet: bool = False,
) -> str:
    """Run Ghidra headless decompilation."""

    # Create temp directory for Ghidra project if not specified
    if project_dir is None:
        project_dir = Path(tempfile.mkdtemp(prefix="ghidra_"))
        _temp_dirs.append(project_dir)

    project_dir.mkdir(parents=True, exist_ok=True)

    # Create the Ghidra script
    script_dir = Path(tempfile.mkdtemp(prefix="ghidra_scripts_"))
    _temp_dirs.append(script_dir)
    script_path = script_dir / "decompile.py"

    # Format target functions
    if functions:
        target_funcs_str = repr(functions)
    else:
        target_funcs_str = "None"

    script_content = DECOMPILE_SCRIPT.format(target_functions=target_funcs_str)
    script_path.write_text(script_content)

    # Build command
    cmd = [
        "analyzeHeadless",
        str(project_dir),
        "DecompileProject",
        "-import", str(binary_path),
        "-postScript", str(script_path),
        "-deleteProject",  # Clean up after
    ]

    if not quiet:
        print(f"Running: {' '.join(cmd)}")
        print(f"Binary: {binary_path}")
        print(f"Project dir: {project_dir}")
        print("")

    # Run Ghidra with Popen so we can track/kill it
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _active_processes.append(proc)

    try:
        stdout, stderr = proc.communicate()
        full_output = stdout + stderr
    finally:
        if proc in _active_processes:
            _active_processes.remove(proc)

    # Extract just the decompiled code (between the === markers)
    lines = full_output.split('\n')
    in_decompiled = False
    decompiled_lines = []

    for line in lines:
        if line.startswith("=" * 80) or line.startswith("DECOMPILED:"):
            in_decompiled = True
        if in_decompiled:
            decompiled_lines.append(line)
        if in_decompiled and "Total functions decompiled:" in line:
            # Get the closing === line too
            idx = lines.index(line)
            if idx + 1 < len(lines):
                decompiled_lines.append(lines[idx + 1])
            break

    decompiled_output = '\n'.join(decompiled_lines)

    # Save output if requested
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(decompiled_output)
        if not quiet:
            print(f"Output saved to: {output_path}")

    # Cleanup temp dirs
    try:
        shutil.rmtree(script_dir)
        if script_dir in _temp_dirs:
            _temp_dirs.remove(script_dir)
    except Exception:
        pass

    try:
        shutil.rmtree(project_dir)
        if project_dir in _temp_dirs:
            _temp_dirs.remove(project_dir)
    except Exception:
        pass

    return decompiled_output


def get_output_path(binary_path: Path, single_binary: bool = True) -> Path:
    """Get output path for a binary. Uses {name}_decompiled.c if multiple binaries."""
    if single_binary:
        return binary_path.parent / "decompiled.c"
    else:
        return binary_path.parent / f"{binary_path.name}_decompiled.c"


def decompile_task(
    task_id: str,
    functions: list[str] | None = None,
    quiet: bool = False,
    strip_level: str = "strip-debug",
) -> tuple[str, bool, str]:
    """
    Decompile a single task (all binaries). Returns (task_id, success, message).
    """
    try:
        binaries = find_fuzzer_binaries(task_id, strip_level=strip_level)
        single_binary = len(binaries) == 1
        output_paths = []

        for binary_path in binaries:
            output_path = get_output_path(binary_path, single_binary)

            if not quiet:
                print(f"[{task_id}] Binary: {binary_path}")
                print(f"[{task_id}] Size: {binary_path.stat().st_size / 1024:.1f} KB")
                print(f"[{task_id}] Output: {output_path}")

            run_ghidra_decompile(
                binary_path=binary_path,
                output_path=output_path,
                functions=functions,
                quiet=quiet,
            )
            output_paths.append(str(output_path))

        return (task_id, True, ", ".join(output_paths))

    except Exception as e:
        return (task_id, False, str(e))


def main():
    parser = argparse.ArgumentParser(
        description="Decompile fuzzer binaries using Ghidra headless mode"
    )

    # Positional argument: task ID or CSV file
    parser.add_argument(
        "target",
        help="Task ID (e.g., 368) or CSV file path (e.g., task_lists/test.csv)"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        help="Output file path (default: strip-debug/fuzzer/decompiled.c)"
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print to stdout instead of saving to file"
    )
    parser.add_argument(
        "--functions", "-f",
        help="Comma-separated list of functions to decompile (default: all)"
    )
    parser.add_argument(
        "--binary", "-b",
        type=Path,
        help="Override binary path (default: auto-detect from task)"
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="Ghidra project directory (default: temp directory)"
    )
    parser.add_argument(
        "--max-threads", "-j",
        type=int,
        default=1,
        help="Number of parallel Ghidra processes (default: 1)"
    )
    parser.add_argument(
        "--strip-level", "-s",
        choices=["no-strip", "strip-debug", "strip-all"],
        default="strip-debug",
        help="Strip level directory to use (default: strip-debug)"
    )

    args = parser.parse_args()

    # Parse functions list
    functions = None
    if args.functions:
        functions = [f.strip() for f in args.functions.split(",")]

    # Determine if target is a CSV file or task ID
    target_path = Path(args.target)
    is_csv = target_path.exists() and target_path.suffix == ".csv"

    # Single task mode
    if not is_csv:
        task_id = parse_task_id(args.target)

        # Find or use specified binary
        if args.binary:
            binaries = [args.binary]
        else:
            binaries = find_fuzzer_binaries(task_id, strip_level=args.strip_level)

        single_binary = len(binaries) == 1

        print(f"Task: {task_id}")
        print(f"Binaries: {len(binaries)}")
        if functions:
            print(f"Filtering to functions: {functions}")
        print("")

        for binary_path in binaries:
            # Determine output path
            if args.output:
                output_path = args.output
            elif args.stdout:
                output_path = None
            else:
                output_path = get_output_path(binary_path, single_binary)

            print(f"Binary: {binary_path}")
            print(f"Size: {binary_path.stat().st_size / 1024:.1f} KB")
            if output_path:
                print(f"Output: {output_path}")
            print("")

            # Run decompilation
            output = run_ghidra_decompile(
                binary_path=binary_path,
                output_path=output_path,
                functions=functions,
                project_dir=args.project_dir,
            )

            # Print if stdout mode
            if args.stdout:
                print(output)

    # Multi-task mode (from CSV)
    else:
        tasks = load_tasks_from_csv(target_path)
        print(f"Loaded {len(tasks)} tasks from {target_path}")
        print(f"Tasks: {tasks}")
        print(f"Parallel threads: {args.max_threads}")
        print("")

        if functions:
            print(f"Filtering to functions: {functions}")
            print("")

        results = []

        if args.max_threads == 1:
            # Sequential processing
            for i, task_id in enumerate(tasks, 1):
                print(f"[{i}/{len(tasks)}] Processing task {task_id}...")
                result = decompile_task(task_id, functions=functions, quiet=False, strip_level=args.strip_level)
                results.append(result)
        else:
            # Parallel processing
            with ThreadPoolExecutor(max_workers=args.max_threads) as executor:
                future_to_task = {
                    executor.submit(decompile_task, task_id, functions, True, args.strip_level): task_id
                    for task_id in tasks
                }

                for i, future in enumerate(as_completed(future_to_task), 1):
                    task_id = future_to_task[future]
                    result = future.result()
                    results.append(result)
                    status = "OK" if result[1] else "FAILED"
                    print(f"[{i}/{len(tasks)}] {task_id}: {status} - {result[2]}")

        # Summary
        print("")
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        success = sum(1 for r in results if r[1])
        failed = sum(1 for r in results if not r[1])
        print(f"Success: {success}/{len(tasks)}")
        print(f"Failed:  {failed}/{len(tasks)}")

        if failed > 0:
            print("")
            print("Failed tasks:")
            for task_id, ok, msg in results:
                if not ok:
                    print(f"  - {task_id}: {msg}")


if __name__ == "__main__":
    main()
