#!/usr/bin/env python3
"""
Extract a function AND all its dependencies into a standalone object file.

Uses clang-extract recursively to extract all file_* dependencies,
then partial-links them into a single self-contained .o file.

Usage:
    uv run scripts/extract_standalone_lib.py <task_id> <function_name>

Example:
    uv run scripts/extract_standalone_lib.py 1065 magic_buffer
    uv run scripts/extract_standalone_lib.py 3938 yr_compiler_add_string
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from dataclasses import dataclass, field


COMPILE_COMMANDS_DIR = Path("/mnt/jailbreak-defense/exp/winniex/cybergym/compile_commands")
OUTPUT_DIR = Path("/mnt/jailbreak-defense/exp/winniex/cybergym/standalone_libs")
CLANG_EXTRACT_PATH = "/opt/clang-extract/clang-extract-wrapper"


@dataclass
class ExtractionState:
    """Track state across recursive extractions."""
    container_name: str
    workdir: str  # Inside container
    srcdir: str   # Inside container
    ce_flags: str  # Common clang-extract flags
    compile_flags: str = ""  # Include/define flags for compilation

    processed_funcs: set = field(default_factory=set)
    pending_funcs: set = field(default_factory=set)
    func_to_source: dict = field(default_factory=dict)  # func -> source file
    source_to_funcs: dict = field(default_factory=dict)  # source -> set of funcs extracted
    extracted_objects: list = field(default_factory=list)  # List of .o files


def docker_exec(container_name: str, script: str, stream: bool = True) -> tuple[int, list[str]]:
    """Execute a script in the container and return (returncode, output_lines)."""
    process = subprocess.Popen(
        ["docker", "exec", container_name, "bash", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    output_lines = []
    for line in process.stdout:
        if stream:
            print(line, end="")
        output_lines.append(line.rstrip("\n"))

    process.wait()
    return process.returncode, output_lines


def find_source_for_function(state: ExtractionState, func: str) -> str | None:
    """Find which source file defines a function. Returns full path."""
    # Check cache first
    if func in state.func_to_source:
        return state.func_to_source[func]

    # Strategy: Find actual definitions, not just call sites
    # Patterns to match:
    # 1. Macro return type on preceding line: FT_EXPORT_DEF( type ) \n  funcname(
    # 2. Return type on same line: void* funcname( or int funcname(
    # 3. Fallback: Any file with funcname(

    script = f'''
cd {state.srcdir}
found=""

# First try: grep for multiline pattern (return type macro + function name)
for f in $(find . -name "*.c" 2>/dev/null); do
    if grep -Pzq "(?:EXPORT_DEF|BASE_DEF|LOCAL_DEF|_IMPL)\\s*\\([^)]*\\)\\s*\\n\\s*{func}\\s*\\(" "$f" 2>/dev/null; then
        echo "FOUND:$f"
        found=1
        break
    fi
done

# Second try: return type on same line (type funcname( or type* funcname()
# Match: start of line, optional whitespace, type, optional *, funcname(
if [ -z "$found" ]; then
    for f in $(find . -name "*.c" 2>/dev/null); do
        if grep -Eq "^[a-zA-Z_][a-zA-Z0-9_]*\\*?[[:space:]]+{func}\\s*\\(" "$f" 2>/dev/null; then
            echo "FOUND:$f"
            found=1
            break
        fi
    done
fi

# Fallback: any file with funcname( at start of line
if [ -z "$found" ]; then
    result=$(find . -name "*.c" -exec grep -l "^[[:space:]]*{func}[[:space:]]*(" {{}} \\; 2>/dev/null | head -1)
    if [ -n "$result" ]; then
        echo "FALLBACK:$result"
    fi
fi
'''
    ret, lines = docker_exec(state.container_name, script, stream=False)

    for line in lines:
        line = line.strip()
        if line.startswith("FOUND:") or line.startswith("FALLBACK:"):
            file_path = line.split(":", 1)[1]
            if file_path.startswith("./"):
                file_path = file_path[2:]
            full_path = f"{state.srcdir}/{file_path}"
            state.func_to_source[func] = full_path
            return full_path
        elif line.endswith(".c"):
            # Handle bare path
            if line.startswith("./"):
                line = line[2:]
            full_path = f"{state.srcdir}/{line}"
            state.func_to_source[func] = full_path
            return full_path

    return None


def resolve_unity_build_source(state: ExtractionState, source_file: str, func: str) -> str:
    """
    If source_file is a unity build (includes other .c files), find the actual
    source file containing the function definition.
    Returns the resolved source file path (full path).
    """
    # Get the directory containing the source file
    source_dir = str(Path(source_file).parent)

    # Check if this file includes other .c files (unity build pattern)
    script = f'''
if grep -q '#include.*\\.c"' {source_file} 2>/dev/null; then
    echo "UNITY_BUILD"
    # List included .c files
    grep -oP '#include\\s+"\\K[^"]+\\.c' {source_file} 2>/dev/null
fi
'''
    ret, lines = docker_exec(state.container_name, script, stream=False)

    if not lines or "UNITY_BUILD" not in lines[0]:
        return source_file  # Not a unity build, return as-is

    # Get list of included .c files
    included_files = [line.strip() for line in lines[1:] if line.strip().endswith(".c")]

    if not included_files:
        return source_file

    # Search for the function in the included files (relative to source dir)
    for inc_file in included_files:
        full_inc_path = f"{source_dir}/{inc_file}"
        # Search for function definition - match with possible leading whitespace
        search_script = f'''
if grep -q "^[[:space:]]*{func}[[:space:]]*(\\|^[A-Z_]*_DEF.*{func}\\|^[a-z_].*{func}(" {full_inc_path} 2>/dev/null; then
    echo "FOUND:{full_inc_path}"
fi
'''
        ret, result = docker_exec(state.container_name, search_script, stream=False)
        for line in result:
            if line.startswith("FOUND:"):
                resolved = line.replace("FOUND:", "").strip()
                print(f"  Unity build: resolved {func} -> {resolved}")
                return resolved

    # Function not found in included files, return original
    return source_file


def get_undefined_project_symbols(state: ExtractionState, obj_file: str) -> set[str]:
    """Get undefined symbols that have source code in the project."""
    # Get ALL undefined symbols
    script = f"nm {obj_file} 2>/dev/null | grep ' U ' | awk '{{print $2}}' | sort | uniq"
    ret, lines = docker_exec(state.container_name, script, stream=False)

    all_undefined = [line.strip() for line in lines if line.strip()]

    # Filter to only symbols that have source in the project
    project_symbols = set()
    for sym in all_undefined:
        # Skip obvious libc/system symbols
        if sym.startswith("__") or sym in ("malloc", "free", "realloc", "calloc",
                                            "memcpy", "memset", "memmove", "memcmp",
                                            "strlen", "strcpy", "strncpy", "strcmp", "strncmp",
                                            "printf", "fprintf", "sprintf", "snprintf",
                                            "fopen", "fclose", "fread", "fwrite", "fseek", "ftell",
                                            "exit", "abort", "assert"):
            continue

        # Check if this symbol has source in the project
        source = find_source_for_function(state, sym)
        if source:
            project_symbols.add(sym)

    return project_symbols


def extract_functions_from_source(
    state: ExtractionState,
    source_file: str,
    functions: set[str],
) -> str | None:
    """
    Extract functions from a source file using clang-extract.
    Returns the path to the compiled .o file, or None on failure.
    """
    # Combine with any previously extracted functions from this source
    if source_file in state.source_to_funcs:
        all_funcs = state.source_to_funcs[source_file] | functions
    else:
        all_funcs = functions

    state.source_to_funcs[source_file] = all_funcs

    base = Path(source_file).stem
    outfile_c = f"{state.workdir}/{base}_extract.c"
    outfile_o = f"{state.workdir}/{base}_extract.o"

    funcs_str = ",".join(sorted(all_funcs))
    print(f"  Extracting [{funcs_str}] from {source_file}...")

    # CD to the source file's directory for proper include resolution
    source_dir = str(Path(source_file).parent)

    extract_script = f'''
cd {source_dir}
{CLANG_EXTRACT_PATH} \\
    {state.ce_flags} \\
    -DCE_EXTRACT_FUNCTIONS={funcs_str} \\
    -DCE_OUTPUT_FILE={outfile_c} \\
    {source_file} 2>&1 | grep -v "warning:" || true

if [ -f {outfile_c} ]; then
    cd {state.srcdir}
    clang -c -O1 -fPIC {state.compile_flags} {outfile_c} -o {outfile_o} 2>&1
    if [ -f {outfile_o} ]; then
        echo "COMPILED:{outfile_o}"
    else
        echo "COMPILE_FAILED"
    fi
else
    echo "EXTRACT_FAILED"
fi
'''

    ret, lines = docker_exec(state.container_name, extract_script, stream=False)

    for line in lines:
        if line.startswith("COMPILED:"):
            obj_path = line.replace("COMPILED:", "").strip()
            return obj_path
        elif "EXTRACT_FAILED" in line:
            print(f"    Warning: Extraction failed for {source_file}")
            return None
        elif "COMPILE_FAILED" in line:
            print(f"    Warning: Compilation failed for {source_file}")
            return None

    return None


def recursive_extract(state: ExtractionState, initial_func: str, initial_source: str) -> bool:
    """
    Recursively extract a function and all its file_* dependencies.
    Returns True on success.
    """
    state.pending_funcs.add(initial_func)
    state.func_to_source[initial_func] = initial_source

    max_iterations = 20
    iteration = 0

    while state.pending_funcs:
        iteration += 1
        if iteration > max_iterations:
            print(f"Warning: Stopping after {max_iterations} iterations")
            break

        print(f"\n=== Iteration {iteration} ===")
        print(f"Pending: {sorted(state.pending_funcs)}")

        # Group pending functions by source file
        source_to_new_funcs: dict[str, set[str]] = {}
        skipped = set()

        for func in state.pending_funcs:
            source = find_source_for_function(state, func)
            if source is None:
                print(f"  Warning: Could not find source for {func}, skipping")
                skipped.add(func)
                continue

            # Resolve unity builds (files that #include other .c files)
            source = resolve_unity_build_source(state, source, func)

            if source not in source_to_new_funcs:
                source_to_new_funcs[source] = set()
            source_to_new_funcs[source].add(func)

        # Extract from each source file
        new_undefined = set()

        for source, funcs in source_to_new_funcs.items():
            obj_path = extract_functions_from_source(state, source, funcs)

            if obj_path:
                # Track unique object files
                if obj_path not in state.extracted_objects:
                    state.extracted_objects.append(obj_path)

                # Find new undefined symbols that have source in the project
                undefined = get_undefined_project_symbols(state, obj_path)
                for sym in undefined:
                    if sym not in state.processed_funcs and sym not in state.pending_funcs:
                        new_undefined.add(sym)

        # Mark current functions as processed
        state.processed_funcs.update(state.pending_funcs)
        state.processed_funcs.update(skipped)

        # Set up next iteration
        state.pending_funcs = new_undefined

    return len(state.extracted_objects) > 0


def partial_link(state: ExtractionState, output_name: str) -> str | None:
    """
    Partial link all extracted objects into a single .o file.
    Returns the path to the output file inside the container.
    """
    if not state.extracted_objects:
        print("Error: No objects to link")
        return None

    # Dedupe objects (should already be unique, but just in case)
    unique_objs = list(dict.fromkeys(state.extracted_objects))
    objs_str = " ".join(unique_objs)
    output_path = f"{state.workdir}/{output_name}_standalone.o"

    print(f"\n=== Partial linking {len(unique_objs)} objects ===")

    link_script = f'''
clang -r -nostdlib -o {output_path} {objs_str} 2>&1
if [ -f {output_path} ]; then
    echo "LINKED:{output_path}"
    ls -la {output_path}
    defined_count=$(nm {output_path} | grep -c " T " || echo 0)
    undefined_count=$(nm {output_path} | grep " U " | awk '{{print $2}}' | sort | uniq | wc -l)
    echo ""
    echo "=== Defined symbols (total $defined_count) ==="
    nm {output_path} | grep " T "
    echo ""
    echo "=== Undefined symbols (total $undefined_count) ==="
    nm {output_path} | grep " U " | awk '{{print $2}}' | sort | uniq
else
    echo "LINK_FAILED"
fi
'''

    ret, lines = docker_exec(state.container_name, link_script, stream=True)

    for line in lines:
        if line.startswith("LINKED:"):
            return line.replace("LINKED:", "").strip()
        elif "LINK_FAILED" in line:
            return None

    return output_path if ret == 0 else None


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

        if arg.endswith("clang") or arg.endswith("clang++"):
            continue

        if arg in ["-c", "-o", "-MT", "-MD", "-MP", "-MF"]:
            skip_next = arg in ["-o", "-MT", "-MF"]
            continue

        if arg.startswith("-W"):
            continue

        if arg.startswith("-O"):
            continue

        if arg.startswith("-g"):
            continue

        if arg.startswith("-fsanitize") or arg.startswith("-fno-sanitize"):
            continue

        if arg.startswith("-f"):
            continue

        if arg in ["-fPIC", "-DPIC", "-fpic"]:
            continue

        if arg.endswith(".o") or arg.endswith(".lo"):
            continue

        if arg.endswith(".Tpo"):
            continue

        if arg.startswith("-D"):
            filtered.append(arg)
            continue

        if arg.startswith("-I"):
            filtered.append(arg)
            continue

        if arg.startswith("-std"):
            filtered.append(arg)
            continue

        if arg == "-include":
            filtered.append(arg)
            continue

        if arg.endswith((".c", ".cc", ".cpp", ".cxx")):
            filtered.append(arg)
            continue

    return filtered


def main():
    parser = argparse.ArgumentParser(
        description="Extract a function and all dependencies into a standalone object file"
    )
    parser.add_argument("task_id", type=str, help="Task ID (e.g., 1065, 3938)")
    parser.add_argument("function", type=str, help="Function name to extract")
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Print info without executing",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=20,
        help="Maximum iterations for dependency resolution (default: 20)",
    )

    args = parser.parse_args()

    # Load compile_commands.json
    compile_commands_path = COMPILE_COMMANDS_DIR / f"{args.task_id}.json"
    if not compile_commands_path.exists():
        print(f"Error: {compile_commands_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(compile_commands_path) as f:
        compile_commands = json.load(f)

    docker_image = f"arvo:{args.task_id}-vul-ce"
    container_name = f"extract_standalone_{args.task_id}_{uuid.uuid4().hex[:8]}"

    # Output paths
    output_dir = OUTPUT_DIR / args.task_id / args.function
    output_file_c = output_dir / f"{args.function}.c"
    output_file_o = output_dir / f"{args.function}_standalone.o"

    print(f"Task ID: {args.task_id}")
    print(f"Function: {args.function}")
    print(f"Docker image: {docker_image}")
    print(f"Output dir: {output_dir}")
    print()

    if args.dry_run:
        print("(dry run - not executing)")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Start container
        print(f"Starting container {container_name}...")
        subprocess.run(
            ["docker", "run", "-d", "--name", container_name, docker_image, "sleep", "infinity"],
            check=True, capture_output=True
        )

        # Phase 1: Compile the project
        print("=" * 60)
        print("=== Compiling project ===")
        compile_script = '''
set -e
echo "Running arvo compile..."
arvo compile 2>&1 || echo "COMPILE_WARNING: arvo compile had issues but continuing"
echo "COMPILE_DONE"
'''
        ret, lines = docker_exec(container_name, compile_script, stream=True)

        compile_done = any("COMPILE_DONE" in line for line in lines)
        if not compile_done:
            print("Warning: Compilation may have failed, but continuing...")

        # Phase 2: Find the initial function
        print("\n=== Finding initial function ===")
        # Search for both global (T) and local/static (t) text symbols
        find_script = f'''
for o in $(find /src /work -name "*.o" 2>/dev/null); do
    if nm "$o" 2>/dev/null | grep -qE " [Tt] {args.function}$"; then
        echo "FOUND_OBJ:$o"
    fi
done
'''
        ret, lines = docker_exec(container_name, find_script, stream=False)

        object_files = [line.replace("FOUND_OBJ:", "").strip()
                        for line in lines if line.startswith("FOUND_OBJ:")]

        if not object_files:
            print(f"Error: Function '{args.function}' not found in any object file")
            sys.exit(1)

        print(f"Found in: {object_files}")

        # Match to compile_commands.json
        initial_source = None
        initial_source_full = None
        for obj_file in object_files:
            obj_base = Path(obj_file).stem
            for entry in compile_commands:
                source_file = entry.get("file", "")
                if Path(source_file).stem == obj_base:
                    # Construct full path if relative
                    if source_file.startswith("/"):
                        initial_source_full = source_file
                    else:
                        directory = entry.get("directory", "")
                        initial_source_full = f"{directory}/{source_file}"
                    initial_source = Path(source_file).name  # Just filename for display
                    break
            if initial_source:
                break

        if not initial_source:
            print(f"Error: Could not find source file for {args.function}")
            sys.exit(1)

        print(f"Initial source: {initial_source_full}")

        # Get compile flags from compile_commands.json
        ce_flags_list = []
        compile_dir = ""
        for entry in compile_commands:
            if entry.get("file", "").endswith(initial_source):
                if "arguments" in entry:
                    compile_args = entry["arguments"]
                else:
                    compile_args = shlex.split(entry.get("command", ""))

                ce_flags_list = [a for a in filter_args_for_clang_extract(compile_args)
                                 if not a.endswith((".c", ".cc", ".cpp", ".cxx"))]
                # Also save the directory for resolving relative paths
                compile_dir = entry.get("directory", "")
                break

        # Resolve relative paths in include flags to absolute paths
        def resolve_include_path(flag: str, base_dir: str) -> str:
            if flag.startswith("-I"):
                path = flag[2:]
                if path.startswith("./") or (not path.startswith("/") and not path.startswith("-")):
                    return f"-I{base_dir}/{path}"
            return flag

        ce_flags_list = [resolve_include_path(f, compile_dir) for f in ce_flags_list]

        # Extract include/define flags for compilation (reuse from ce_flags_list)
        compile_flags_list = [a for a in ce_flags_list if a.startswith(("-I", "-D"))]
        compile_flags = " ".join(shlex.quote(a) for a in compile_flags_list)

        # Build CE_FLAGS string with clang include path
        ce_flags = " ".join(shlex.quote(a) for a in ce_flags_list)

        # Add clang include path and keep includes
        get_clang_include = '''
CLANG_INCLUDE=$(dirname "$(find /usr -name stddef.h 2>/dev/null | grep clang | head -1)")
echo "CLANG_INCLUDE:$CLANG_INCLUDE"
'''
        ret, lines = docker_exec(container_name, get_clang_include, stream=False)
        clang_include = ""
        for line in lines:
            if line.startswith("CLANG_INCLUDE:"):
                clang_include = line.replace("CLANG_INCLUDE:", "").strip()
                break

        if clang_include:
            ce_flags = f"{ce_flags} -I{clang_include}"
        ce_flags = f"{ce_flags} -DCE_KEEP_INCLUDES"

        # Determine srcdir from compile_commands
        srcdir = "/src/file/src"  # Default for libmagic
        for entry in compile_commands:
            if entry.get("file", "").endswith(initial_source):
                srcdir = entry.get("directory", srcdir)
                break

        # Phase 3: Recursive extraction
        print("\n=== Starting recursive extraction ===")

        state = ExtractionState(
            container_name=container_name,
            workdir="/tmp/clang-extract-artifacts",
            srcdir=srcdir,
            ce_flags=ce_flags,
            compile_flags=compile_flags,
        )

        # Create workdir in container
        docker_exec(container_name, f"mkdir -p {state.workdir}", stream=False)

        success = recursive_extract(state, args.function, initial_source_full)

        if not success:
            print("Error: Extraction failed")
            sys.exit(1)

        # Phase 4: Partial link
        standalone_path = partial_link(state, args.function)

        if not standalone_path:
            print("Error: Partial linking failed")
            sys.exit(1)

        # Phase 5: Copy results out
        print("\n=== Copying results ===")

        # Copy standalone .o
        subprocess.run(
            ["docker", "cp", f"{container_name}:{standalone_path}", str(output_file_o)],
            check=True
        )
        print(f"Standalone object: {output_file_o}")

        # Also copy the individual .c files (useful for debugging)
        for obj in state.extracted_objects:
            c_file = obj.replace(".o", ".c")
            c_name = Path(c_file).name
            local_c = output_dir / c_name
            try:
                subprocess.run(
                    ["docker", "cp", f"{container_name}:{c_file}", str(local_c)],
                    check=True, capture_output=True
                )
            except subprocess.CalledProcessError:
                pass  # Some .c files might not exist

        print(f"\nSuccess!")
        print(f"Output directory: {output_dir}")
        print(f"Standalone object: {output_file_o}")
        print(f"  Size: {output_file_o.stat().st_size} bytes")

        # Show defined symbols count
        result = subprocess.run(
            ["nm", str(output_file_o)],
            capture_output=True, text=True
        )
        defined = len([l for l in result.stdout.splitlines() if " T " in l])
        undefined = len([l for l in result.stdout.splitlines() if " U " in l])
        print(f"  Defined symbols: {defined}")
        print(f"  Undefined symbols: {undefined}")

    finally:
        # Clean up container
        print(f"\nCleaning up container {container_name}...")
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


if __name__ == "__main__":
    main()
