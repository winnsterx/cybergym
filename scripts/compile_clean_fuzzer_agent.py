#!/usr/bin/env python3
"""
Claude Agent that compiles clean (uninstrumented) fuzzers from ARVO Docker images.

Given an ARVO task ID, this agent:
1. Starts the Docker container
2. Analyzes build.sh to understand the build process
3. Finds and reads the harness source
4. Creates a standalone main() wrapper
5. Rebuilds libraries without sanitizers
6. Links everything into a clean binary
7. Extracts the result

Usage:
    uv run scripts/compile_clean_fuzzer_agent.py 368
    uv run scripts/compile_clean_fuzzer_agent.py 1065 --output-dir /tmp/clean_fuzzers
"""

import argparse
import asyncio
import subprocess
import uuid
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock, ToolUseBlock, ResultMessage


SYSTEM_PROMPT = """You are an expert at building C/C++ projects and understanding fuzzer harnesses.

Your task is to create a CLEAN version of the fuzzer binary without any sanitizer instrumentation.
The original fuzzer in /out/ was built with:
- libFuzzer (provides main() and fuzzing loop)
- Sanitizers (ASAN/MSAN/UBSAN - instrument memory accesses)
- Coverage instrumentation

You need to:
1. Create a standalone main() that reads a file and calls LLVMFuzzerTestOneInput
2. Rebuild the target libraries WITHOUT sanitizers
3. Link everything together WITHOUT -lFuzzingEngine

IMPORTANT GUIDELINES:
- First read /src/build.sh to understand how the fuzzer is built
- Find the harness source file (usually *.cc in /src/ that contains LLVMFuzzerTestOneInput)
- Check if the harness has LLVMFuzzerInitialize - if so, call it in your standalone main
- When rebuilding libraries, unset CFLAGS/CXXFLAGS/SANITIZER/SANITIZER_FLAGS/COVERAGE_FLAGS
- Use the same compiler ($CXX) but remove -fsanitize=* flags from CXXFLAGS
- The final binary should be placed at /tmp/fuzzer_clean
- After building, verify with: nm /tmp/fuzzer_clean | grep -c "__asan\|__msan" (should be 0)

STANDALONE MAIN TEMPLATE:
```cpp
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);
// If harness has LLVMFuzzerInitialize, add:
// extern "C" int LLVMFuzzerInitialize(int* argc, char*** argv);

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "Usage: %s <input_file>\\n", argv[0]);
        return 1;
    }

    // If harness has LLVMFuzzerInitialize, call it:
    // LLVMFuzzerInitialize(&argc, &argv);

    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror("fopen"); return 1; }

    fseek(f, 0, SEEK_END);
    size_t size = ftell(f);
    fseek(f, 0, SEEK_SET);

    uint8_t *data = (uint8_t*)malloc(size);
    fread(data, 1, size, f);
    fclose(f);

    int ret = LLVMFuzzerTestOneInput(data, size);

    free(data);
    return ret;
}
```

Work step by step. If something fails, analyze the error and try a different approach.
"""


def start_container(task_id: str) -> str:
    """Start a Docker container for the ARVO task."""
    image = f"n132/arvo:{task_id}-vul"
    container_name = f"arvo_{task_id}_clean_{uuid.uuid4().hex[:8]}"

    print(f"[*] Pulling image {image}...")
    subprocess.run(["docker", "pull", image], capture_output=True)

    print(f"[*] Starting container {container_name}...")
    result = subprocess.run(
        ["docker", "run", "-d", "--name", container_name, image, "sleep", "infinity"],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to start container: {result.stderr}")

    return container_name


def stop_container(container_name: str):
    """Stop and remove the container."""
    print(f"[*] Stopping container {container_name}...")
    subprocess.run(["docker", "stop", container_name], capture_output=True)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


def extract_binary(container_name: str, output_path: Path) -> bool:
    """Extract the clean fuzzer from the container."""
    print(f"[*] Extracting clean fuzzer to {output_path}...")
    result = subprocess.run(
        ["docker", "cp", f"{container_name}:/tmp/fuzzer_clean", str(output_path)],
        capture_output=True,
        text=True
    )
    return result.returncode == 0 and output_path.exists()


async def compile_clean_fuzzer(task_id: str, container_name: str) -> bool:
    """Use Claude agent to compile a clean fuzzer inside the container."""

    options = ClaudeAgentOptions(
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        permission_mode="acceptEdits",
        model="sonnet",
        max_turns=50,
    )

    prompt = f"""You are working inside Docker container '{container_name}'.

To run commands in the container, use: docker exec {container_name} bash -c '<command>'

Your goal: Build a clean version of /out/fuzzer (the exact name varies) without sanitizer instrumentation.

Start by:
1. docker exec {container_name} cat /src/build.sh
2. docker exec {container_name} ls /out/
3. Find and read the harness source file

Then follow the process to create a clean binary at /tmp/fuzzer_clean inside the container.

When done, verify the binary exists and has no sanitizer symbols.
"""

    print("\n" + "="*60)
    print("Claude Agent Working...")
    print("="*60 + "\n")

    success = False

    async with ClaudeSDKClient(options=options) as client:
        await client.query(SYSTEM_PROMPT + "\n\n" + prompt)

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"\n{block.text}\n")
                    elif isinstance(block, ToolUseBlock):
                        cmd = block.input.get('command', block.input.get('file_path', ''))
                        if len(cmd) > 100:
                            cmd = cmd[:100] + "..."
                        print(f"[Tool: {block.name}] {cmd}")
            elif isinstance(message, ResultMessage):
                print(f"\n[{message.subtype}] Duration: {message.duration_ms}ms, Cost: ${message.cost_usd:.4f}")
                if message.subtype == "success":
                    success = True

    # Verify the binary was created
    result = subprocess.run(
        ["docker", "exec", container_name, "test", "-f", "/tmp/fuzzer_clean"],
        capture_output=True
    )

    return result.returncode == 0


async def main():
    parser = argparse.ArgumentParser(
        description="Compile clean (uninstrumented) fuzzer from ARVO Docker image"
    )
    parser.add_argument("task_id", type=str, help="ARVO task ID (e.g., 368, 1065)")
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path("/tmp/clean_fuzzers"),
        help="Output directory for clean fuzzers"
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="Don't remove the container after building"
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"fuzzer_{args.task_id}_clean"

    container_name = None
    try:
        # Start container
        container_name = start_container(args.task_id)

        # Run Claude agent to compile clean fuzzer
        success = await compile_clean_fuzzer(args.task_id, container_name)

        if success:
            # Extract the binary
            if extract_binary(container_name, output_path):
                print(f"\n{'='*60}")
                print(f"SUCCESS! Clean fuzzer saved to: {output_path}")
                print(f"{'='*60}")

                # Show file info
                subprocess.run(["file", str(output_path)])
                subprocess.run(["ls", "-la", str(output_path)])
            else:
                print("\n[ERROR] Failed to extract fuzzer_clean from container")
                return 1
        else:
            print("\n[ERROR] Agent failed to compile clean fuzzer")
            return 1

    except Exception as e:
        print(f"\n[ERROR] {e}")
        return 1
    finally:
        if container_name and not args.keep_container:
            stop_container(container_name)

    return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))
