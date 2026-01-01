#!/bin/bash
# Extract a function and all its dependencies into a standalone object file
# Usage: ./extract_standalone_lib.sh <initial_function> <source_file>
# Example: ./extract_standalone_lib.sh magic_buffer magic.c

set -e

INITIAL_FUNC="${1:-magic_buffer}"
INITIAL_SOURCE="${2:-magic.c}"
WORKDIR="/tmp/clang-extract-artifacts"
SRCDIR="/src/file/src"

# Common clang-extract flags
CE_FLAGS="-DHAVE_CONFIG_H -I. -I.. -I/usr/local/lib/clang/5.0.0/include -DMAGIC=\"/usr/local/share/misc/magic\" -DFUZZING_BUILD_MODE_UNSAFE_FOR_PRODUCTION -DCE_KEEP_INCLUDES"

# Trim whitespace helper
trim() { echo "$1" | xargs; }

mkdir -p "$WORKDIR"
cd "$SRCDIR"

echo "=== Starting extraction for $INITIAL_FUNC from $INITIAL_SOURCE ==="
echo ""

# Track extracted functions per source file (to accumulate, not overwrite)
declare -A SOURCE_FUNCS_EXTRACTED  # source -> comma-sep list of already extracted funcs
declare -A FUNC_TO_SOURCE          # func -> source file
PROCESSED_FUNCS=""
PENDING_FUNCS="$INITIAL_FUNC"

# Map function to source file
find_source_file() {
    local func="$1"
    grep -l "^$func\|^protected.*$func\|^[a-z_].*\*\?$func(" *.c 2>/dev/null | head -1
}

# Initial source mapping
FUNC_TO_SOURCE["$INITIAL_FUNC"]="$INITIAL_SOURCE"

iteration=0
PENDING_FUNCS=$(trim "$PENDING_FUNCS")

while [ -n "$PENDING_FUNCS" ]; do
    iteration=$((iteration + 1))
    echo "=== Iteration $iteration ==="
    echo "Pending functions: $PENDING_FUNCS"
    echo ""

    # Safety check
    if [ $iteration -gt 20 ]; then
        echo "Too many iterations, stopping"
        break
    fi

    # Group NEW functions by source file
    declare -A SOURCE_TO_NEW_FUNCS
    for func in $PENDING_FUNCS; do
        if [ -z "${FUNC_TO_SOURCE[$func]}" ]; then
            src=$(find_source_file "$func")
            if [ -n "$src" ]; then
                FUNC_TO_SOURCE[$func]="$src"
            else
                echo "Warning: Could not find source for $func, skipping"
                continue
            fi
        fi
        src="${FUNC_TO_SOURCE[$func]}"
        if [ -n "${SOURCE_TO_NEW_FUNCS[$src]}" ]; then
            SOURCE_TO_NEW_FUNCS[$src]="${SOURCE_TO_NEW_FUNCS[$src]},$func"
        else
            SOURCE_TO_NEW_FUNCS[$src]="$func"
        fi
    done

    # For each source file, add new funcs to existing and re-extract
    NEW_UNDEFINED=""
    for src in "${!SOURCE_TO_NEW_FUNCS[@]}"; do
        new_funcs="${SOURCE_TO_NEW_FUNCS[$src]}"

        # Combine with previously extracted funcs from this source
        if [ -n "${SOURCE_FUNCS_EXTRACTED[$src]}" ]; then
            all_funcs="${SOURCE_FUNCS_EXTRACTED[$src]},$new_funcs"
        else
            all_funcs="$new_funcs"
        fi
        SOURCE_FUNCS_EXTRACTED[$src]="$all_funcs"

        base=$(basename "$src" .c)
        outfile="$WORKDIR/${base}_extract.c"
        objfile="$WORKDIR/${base}_extract.o"

        echo "--- Extracting [$all_funcs] from $src ---"

        /opt/clang-extract/clang-extract-wrapper \
            $CE_FLAGS \
            -DCE_EXTRACT_FUNCTIONS="$all_funcs" \
            -DCE_OUTPUT_FILE="$outfile" \
            "$src" 2>&1 | grep -v "warning:" || true

        if [ -f "$outfile" ]; then
            echo "Compiling $outfile..."
            clang -c -O1 -fPIC -I. -I.. -DHAVE_CONFIG_H "$outfile" -o "$objfile" 2>&1 || {
                echo "Compile failed for $outfile"
                continue
            }

            # Get new undefined symbols (only file_* functions from libmagic)
            new_syms=$(nm "$objfile" 2>/dev/null | grep " U " | awk '{print $2}' | \
                       grep "^file_" | sort | uniq)

            for sym in $new_syms; do
                # Check if not already processed or pending
                if ! echo "$PROCESSED_FUNCS $PENDING_FUNCS" | grep -qw "$sym"; then
                    NEW_UNDEFINED="$NEW_UNDEFINED $sym"
                fi
            done
        else
            echo "Extraction failed for $src"
        fi
    done
    unset SOURCE_TO_NEW_FUNCS
    declare -A SOURCE_TO_NEW_FUNCS

    # Mark current functions as processed
    for func in $PENDING_FUNCS; do
        PROCESSED_FUNCS="$PROCESSED_FUNCS $func"
    done

    # Set up next iteration
    PENDING_FUNCS=$(echo "$NEW_UNDEFINED" | tr ' ' '\n' | sort | uniq | tr '\n' ' ')
    PENDING_FUNCS=$(trim "$PENDING_FUNCS")

    echo ""
done

echo "=== All extractions complete ==="
echo ""

# Collect all unique .o files
EXTRACTED_OBJS=$(ls "$WORKDIR"/*_extract.o 2>/dev/null | sort | uniq | tr '\n' ' ')
echo "Extracted objects: $EXTRACTED_OBJS"
echo ""

# Partial link all objects together
if [ -n "$EXTRACTED_OBJS" ]; then
    echo "=== Partial linking into standalone object ==="
    OUTPUT="$WORKDIR/${INITIAL_FUNC}_standalone.o"

    clang -r -nostdlib -o "$OUTPUT" $EXTRACTED_OBJS

    echo ""
    echo "=== Result ==="
    ls -la "$OUTPUT"
    echo ""
    echo "Defined symbols (first 30):"
    nm "$OUTPUT" | grep " T " | head -30
    echo ""
    echo "Remaining undefined (file_* only):"
    nm "$OUTPUT" | grep " U " | awk '{print $2}' | grep "^file_" | sort | uniq
    echo ""
    echo "Remaining undefined (all):"
    nm "$OUTPUT" | grep " U " | awk '{print $2}' | sort | uniq | head -30
    echo ""
    echo "Output: $OUTPUT"
else
    echo "No objects were extracted!"
    exit 1
fi
