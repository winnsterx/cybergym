#!/usr/bin/env python3
"""
Launch Claude Code headless agents to compile clean binaries for ARVO tasks.

Usage:
    # Single task
    uv run scripts/launch_compiler_agent.py 368
    uv run scripts/launch_compiler_agent.py 368 --model opus --timeout 1200

    # Multiple tasks from CSV
    uv run scripts/launch_compiler_agent.py task_lists/test.csv --max-threads 4

The agent will:
1. Enter the docker container for arvo:{task_id}-vul
2. Compile clean (non-sanitized) binaries for the target library AND fuzzer
3. Extract those binaries to compiled_artifacts/arvo/{task_id}/
"""

import argparse
import asyncio
import csv
import json
import os
import signal
import sys
from pathlib import Path

# Track running processes for cleanup
_running_processes: set[asyncio.subprocess.Process] = set()


def cleanup_processes():
    """Kill all running child processes."""
    for proc in list(_running_processes):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    _running_processes.clear()


def signal_handler(signum, _frame):
    """Handle interrupt signals by cleaning up child processes."""
    print(f"\nReceived signal {signum}, cleaning up {len(_running_processes)} processes...")
    cleanup_processes()
    sys.exit(1)


# Load environment variables from .env
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_env():
    """Load environment variables from .env file."""
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key] = value


def parse_task_list(task_input: str) -> list[str]:
    """Parse task input - either a single task ID or a CSV file path."""
    task_path = Path(task_input)

    # Check if it's a CSV file
    if task_path.exists() and task_path.suffix == ".csv":
        tasks = []
        with open(task_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                task_id = row.get("task", "").strip().strip('"')
                # Handle "arvo:368" format -> extract "368"
                if task_id.startswith("arvo:"):
                    task_id = task_id.split(":")[1]
                if task_id:
                    tasks.append(task_id)
        return tasks
    else:
        # Single task ID
        task_id = task_input.strip()
        if task_id.startswith("arvo:"):
            task_id = task_id.split(":")[1]
        return [task_id]


# The compiler agent prompt template
COMPILER_PROMPT_TEMPLATE = """You are a compiler agent tasked with compiling clean (non-sanitized, non-instrumented) binaries for ARVO fuzzing task {task_id}.

Go into docker container for arvo:{task_id}-vul, compile the clean binaries for the target library AND the fuzzer binary at /out/fuzzer, and extract those binaries into respective folders within {output_dir}/libs and {output_dir}/fuzzer.

Each task is slightly different so you need to understand the env and change your process to do this. The main compilation command in each docker container is `arvo compile`. This sets up the relevant env and flags, then runs `compile` to compile the actual fuzzer binary. You should take a look at precisely what these scripts do for each task by looking at the files at `which arvo` and `which compile`.

Often arvo compile and build.sh is trying to compile multiple different fuzzer binaries. For each task ID, however, we only run one fuzzer, which you can find using `cat $(which arvo)` and seeing which /out/fuzzer it uses in that container.

## Compiling Clean Target Library

To get the clean target lib binaries, the typical easy way is to export the flags we still need from arvo (aka not exporting the sanitizer, coverage and other relevant flags), then directly call `compile`. For example:

```bash
docker exec {{container_name}} bash -c '
export FUZZING_ENGINE=none
export SANITIZER=none
export ARCHITECTURE=x86_64
export FUZZING_LANGUAGE=c++
compile
'
```

You will need to look at what `compile` and `build.sh` needs to select which flags you need to explicitly set to what values. You may also have an easier time by directly overriding those flags and making other kinds of modifications to build.sh.

## Compiling Clean Fuzzer Binary

To get the clean fuzzer binary, you need to create a simple standalone_driver file with the main() entrypoint that calls the LLVMFuzzerTestOneInput once. Then, you should modify the relevant parts of the build.sh script to compile the clean fuzzer binary with the standalone driver. An example of such standalone binary:

```c
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

extern int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int main(int argc, char **argv) {{
    // 1. Read file from command line
    FILE *f = fopen(argv[1], "rb");

    // 2. Get file contents
    fseek(f, 0, SEEK_END);
    size_t size = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *data = malloc(size);
    fread(data, 1, size, f);
    fclose(f);

    // 3. Call the harness ONCE (not in a loop)
    LLVMFuzzerTestOneInput(data, size);

    return 0;
}}
```

Make sure when you are copying over the binaries, copy ONLY the fuzzer binary used in `cat $(which arvo)` for that specific task ID and the dependencies that were linked to that fuzzer. There is always only one fuzzer for that task ID.

## Verification

At the end, you should check that the files you are about to extract/extracted are indeed clear of sanitization:

```bash
nm /path/to/binary 2>/dev/null | grep -qE '__asan|__msan|__ubsan|__tsan|__sancov|__llvm_profile' && echo "INSTRUMENTED" || echo "CLEAN"
```

## Output Structure

Extract the compiled binaries to `{output_dir}/no-strip/`:
- `{output_dir}/no-strip/libs/` - compiled library files (.a, .so) that were linked to the fuzzer
- `{output_dir}/no-strip/fuzzer/` - the standalone fuzzer binary (only the one used for this task)
- `{output_dir}/no-strip/objects/` - object files (.o) that were linked into the fuzzer or libraries

Create a `{output_dir}/metadata.json` with:
- task_id
- container_image
- fuzzer_name (the name of the fuzzer binary)
- compilation_flags used
- verification results (clean or not)
- list of extracted files

## Important Notes

- Start a docker container first: `docker run -d --name arvo_{task_id}_compiler n132/arvo:{task_id}-vul sleep infinity`
- Clean up the container when done: `docker stop arvo_{task_id}_compiler && docker rm arvo_{task_id}_compiler`
- Each ARVO task may have different build systems - adapt your approach accordingly
- Look at the existing build.sh and Makefile to understand the build process before making changes

Begin by starting the container and exploring the build environment."""


def postprocess_binaries(output_dir: Path) -> bool:
    """Create strip-all and strip-debug variants of the compiled binaries."""
    import shutil
    import subprocess

    no_strip_dir = output_dir / "no-strip"
    if not no_strip_dir.exists():
        print(f"Warning: no-strip directory not found at {no_strip_dir}")
        return False

    for variant, strip_args in [("strip-all", ["--strip-all"]), ("strip-debug", ["--strip-debug"])]:
        variant_dir = output_dir / variant

        # Remove existing variant dir if present
        if variant_dir.exists():
            shutil.rmtree(variant_dir)

        # Copy no-strip to variant
        shutil.copytree(no_strip_dir, variant_dir)

        # Strip all binaries in the variant
        for subdir in ["libs", "fuzzer", "objects"]:
            subdir_path = variant_dir / subdir
            if not subdir_path.exists():
                continue

            for file_path in subdir_path.iterdir():
                if file_path.is_file():
                    try:
                        subprocess.run(
                            ["strip"] + strip_args + [str(file_path)],
                            check=True,
                            capture_output=True,
                        )
                    except subprocess.CalledProcessError as e:
                        print(f"Warning: Failed to strip {file_path}: {e}")

        print(f"Created {variant}/ variant")

    return True


async def run_compiler_agent(
    task_id: str,
    output_dir: Path,
    model: str = "sonnet",
    timeout: int = 900,
    dry_run: bool = False,
) -> dict:
    """Run a Claude Code headless agent to compile binaries for a task."""

    prompt = COMPILER_PROMPT_TEMPLATE.format(
        task_id=task_id,
        output_dir=output_dir,
    )

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        print(f"[DRY RUN] Would compile task: {task_id}")
        print(f"[DRY RUN] Would write to: {output_dir}")
        return {"status": "dry_run", "task_id": task_id}

    # Trajectory file path
    trajectory_path = output_dir / "compiler_trajectory.jsonl"

    # Build the command
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash",
        "--permission-mode", "bypassPermissions",
        "--add-dir", str(PROJECT_ROOT),
    ]

    print(f"Starting compiler agent for task {task_id}")
    print(f"Output directory: {output_dir}")
    print(f"Model: {model}, Timeout: {timeout}s")
    print()

    try:
        # Open trajectory file for writing
        with open(trajectory_path, "wb") as traj_file:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
            )
            _running_processes.add(process)

            # Stream stdout to both terminal and file
            result_content = ""
            cost = 0
            duration = 0

            async def stream_output():
                nonlocal result_content, cost, duration
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break

                    # Write to trajectory file
                    traj_file.write(line)
                    traj_file.flush()

                    # Parse and display
                    line_str = line.decode("utf-8").strip()
                    if not line_str:
                        continue

                    try:
                        msg = json.loads(line_str)
                        msg_type = msg.get("type")

                        if msg_type == "assistant":
                            # Show assistant text output and tool uses
                            content = msg.get("message", {}).get("content", [])
                            for block in content:
                                if block.get("type") == "text":
                                    print(block.get("text", ""))
                                elif block.get("type") == "tool_use":
                                    tool_name = block.get("name", "unknown")
                                    tool_input = block.get("input", {})
                                    # Show condensed tool info
                                    if tool_name == "Bash":
                                        cmd = tool_input.get("command", "")[:100]
                                        print(f"\n> Bash: {cmd}{'...' if len(tool_input.get('command', '')) > 100 else ''}")
                                    elif tool_name == "Read":
                                        print(f"\n> Read: {tool_input.get('file_path', '')}")
                                    elif tool_name == "Write":
                                        print(f"\n> Write: {tool_input.get('file_path', '')}")
                                    elif tool_name == "Edit":
                                        print(f"\n> Edit: {tool_input.get('file_path', '')}")
                                    else:
                                        print(f"\n> {tool_name}")
                        elif msg_type == "user":
                            # Show tool results summary
                            content = msg.get("message", {}).get("content", [])
                            for block in content:
                                if block.get("type") == "tool_result":
                                    tool_id = block.get("tool_use_id", "")[:8]
                                    is_error = block.get("is_error", False)
                                    status = "ERROR" if is_error else "OK"
                                    print(f"  [Tool {tool_id}...] {status}")
                        elif msg_type == "result":
                            result_content = msg.get("result", "")
                            cost = msg.get("total_cost_usd", 0)
                            duration = msg.get("duration_ms", 0)
                    except json.JSONDecodeError:
                        # Not JSON, just print raw
                        print(line_str)

            # Wait for completion with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        stream_output(),
                        process.wait(),
                    ),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                _running_processes.discard(process)
                print(f"Timeout after {timeout}s", file=sys.stderr)
                return {
                    "status": "timeout",
                    "task_id": task_id,
                    "timeout": timeout,
                }
            finally:
                _running_processes.discard(process)

        if process.returncode != 0:
            print(f"Error (exit {process.returncode})", file=sys.stderr)
            return {
                "status": "error",
                "task_id": task_id,
                "exit_code": process.returncode,
            }

        # Write result summary
        result_path = output_dir / "result.md"
        with open(result_path, "w") as f:
            f.write(result_content)

        print(f"Done: {output_dir}")
        print(f"Cost: ${cost:.4f}, Duration: {duration/1000:.1f}s")

        # Post-process: create strip-all and strip-debug variants
        print()
        print("Post-processing binaries...")
        postprocess_binaries(output_dir)

        return {
            "status": "success",
            "task_id": task_id,
            "output_dir": str(output_dir),
            "trajectory_path": str(trajectory_path),
            "cost_usd": cost,
            "duration_ms": duration,
        }

    except Exception as e:
        print(f"Exception: {e}", file=sys.stderr)
        return {
            "status": "exception",
            "task_id": task_id,
            "error": str(e),
        }


async def run_agents_parallel(
    task_ids: list[str],
    model: str,
    timeout: int,
    dry_run: bool,
    max_threads: int,
) -> list[dict]:
    """Run compiler agents in parallel with a semaphore to limit concurrency."""
    semaphore = asyncio.Semaphore(max_threads)

    async def run_with_semaphore(task_id: str) -> dict:
        async with semaphore:
            output_dir = PROJECT_ROOT / "compiled_artifacts" / "arvo" / task_id
            return await run_compiler_agent(task_id, output_dir, model, timeout, dry_run)

    tasks = [run_with_semaphore(task_id) for task_id in task_ids]
    results = await asyncio.gather(*tasks)
    return list(results)


def main():
    parser = argparse.ArgumentParser(
        description="Launch Claude Code headless agents to compile clean binaries for ARVO tasks."
    )
    parser.add_argument(
        "task_input",
        type=str,
        help="ARVO task ID (e.g., 368) or path to CSV file with task list",
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default="claude-opus-4-5-20251101",
        help="Model to use (default: claude-opus-4-5-20251101)",
    )
    parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=900,
        help="Timeout per agent in seconds (default: 900 = 15 minutes)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be done without actually running agents",
    )
    parser.add_argument(
        "--max-threads", "-j",
        type=int,
        default=1,
        help="Number of agents to run in parallel (default: 1)",
    )

    args = parser.parse_args()

    # Register signal handlers for cleanup
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Load environment
    load_env()

    # Parse task input
    task_ids = parse_task_list(args.task_input)

    if not task_ids:
        print("No tasks found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(task_ids)} task(s) to compile")
    print(f"Tasks: {', '.join(task_ids)}")
    print(f"Model: {args.model}, Timeout: {args.timeout}s, Max threads: {args.max_threads}")
    print()

    # Run agents
    results = asyncio.run(
        run_agents_parallel(
            task_ids,
            args.model,
            args.timeout,
            args.dry_run,
            args.max_threads,
        )
    )

    # Print summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    success_count = sum(1 for r in results if r["status"] == "success")
    error_count = sum(1 for r in results if r["status"] not in ("success", "dry_run"))
    total_cost = sum(r.get("cost_usd", 0) for r in results)
    total_duration = sum(r.get("duration_ms", 0) for r in results)

    print(f"Total tasks: {len(results)}")
    print(f"Successful: {success_count}")
    print(f"Errors: {error_count}")
    print(f"Total cost: ${total_cost:.4f}")
    print(f"Total duration: {total_duration/1000:.1f}s")

    if error_count > 0:
        print()
        print("Failed tasks:")
        for r in results:
            if r["status"] not in ("success", "dry_run"):
                print(f"  - {r['task_id']}: {r['status']}")

    # Exit with appropriate code
    sys.exit(0 if error_count == 0 else 1)


if __name__ == "__main__":
    main()
