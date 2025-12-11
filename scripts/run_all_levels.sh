#!/bin/bash
# Run eval across all difficulty levels with rotating API keys
#
# Usage: ./scripts/run_all_levels.sh <base_command_args>
#
# Example:
#   ./scripts/run_all_levels.sh --task-csv task_lists/test.csv --runtime modal \
#     --times-per-problem 4 --parallel-requests 10 --evaluation-mode exploit_binary \
#     --output-dir transcripts/exploit-mode-tuesday --model claude-opus-4-5-20251101

set -e

# Trap Ctrl-C and kill entire process tree
cleanup() {
    echo ""
    echo "Caught interrupt signal. Killing all processes..."
    # Kill by output-dir pattern to catch all descendants (multiprocessing workers, openhands, etc.)
    if [[ -n "$OUTPUT_DIR" ]]; then
        pkill -f "$OUTPUT_DIR" 2>/dev/null || true
    fi
    # Also kill direct children
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            # Kill process group
            kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
            echo "Killed PID $pid"
        fi
    done
    sleep 1
    # Force kill any remaining
    if [[ -n "$OUTPUT_DIR" ]]; then
        pkill -9 -f "$OUTPUT_DIR" 2>/dev/null || true
    fi
    exit 1
}
trap cleanup SIGINT SIGTERM

# Load .env file
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [[ -f "$PROJECT_DIR/.env" ]]; then
    source "$PROJECT_DIR/.env"
else
    echo "Error: .env file not found at $PROJECT_DIR/.env"
    exit 1
fi

# Check for API keys from .env
if [[ -z "$ANTHROPIC_API_KEY_0" || -z "$ANTHROPIC_API_KEY_1" || -z "$ANTHROPIC_API_KEY_2" || -z "$ANTHROPIC_API_KEY_3" ]]; then
    echo "Error: Please set ANTHROPIC_API_KEY_0, ANTHROPIC_API_KEY_1, ANTHROPIC_API_KEY_2, ANTHROPIC_API_KEY_3 in .env"
    exit 1
fi

API_KEYS=("$ANTHROPIC_API_KEY_0" "$ANTHROPIC_API_KEY_1" "$ANTHROPIC_API_KEY_2" "$ANTHROPIC_API_KEY_3")

# Parse args to extract output-dir
OUTPUT_DIR=""
OTHER_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --difficulty)
            # Skip any difficulty arg passed in - we set it ourselves
            shift 2
            ;;
        *)
            OTHER_ARGS+=("$1")
            shift
            ;;
    esac
done

if [[ -z "$OUTPUT_DIR" ]]; then
    echo "Error: --output-dir is required"
    exit 1
fi

echo "Base output directory: $OUTPUT_DIR"
echo "Running levels 0-3 in parallel with rotating API keys..."
echo ""

PIDS=()

for level in 0 1 2 3; do
    api_key="${API_KEYS[$level]}"
    level_output_dir="${OUTPUT_DIR}-level${level}"
    log_file="${level_output_dir}.log"

    echo "========================================"
    echo "Launching level${level}"
    echo "Output: ${level_output_dir}"
    echo "Log: ${log_file}"
    echo "API key: ${api_key:0:20}..."
    echo "========================================"

    uv run python run_eval.py \
        "${OTHER_ARGS[@]}" \
        --output-dir "$level_output_dir" \
        --difficulty "level${level}" \
        --api-key "$api_key" \
        > "$log_file" 2>&1 &

    PIDS+=($!)
    echo "Started level${level} with PID ${PIDS[$level]}"
    echo ""
done

echo "All levels launched. Waiting for completion..."
echo "PIDs: ${PIDS[*]}"
echo ""

# Wait for all processes and track failures
FAILED=()
for level in 0 1 2 3; do
    pid=${PIDS[$level]}
    if wait $pid; then
        echo "✓ level${level} (PID $pid) completed successfully"
    else
        echo "✗ level${level} (PID $pid) failed with exit code $?"
        FAILED+=($level)
    fi
done

echo ""
if [[ ${#FAILED[@]} -eq 0 ]]; then
    echo "All levels completed successfully!"
else
    echo "Failed levels: ${FAILED[*]}"
    echo "Check logs for details:"
    for level in "${FAILED[@]}"; do
        echo "  ${OUTPUT_DIR}-level${level}.log"
    done
    exit 1
fi
