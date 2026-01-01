#!/bin/bash
# Generate compile_commands.json for an arvo task using bear
# Usage: ./gen_compile_commands_bear.sh <task_id> <output_dir>

TASK_ID=$1
OUTPUT_DIR=$2

if [ -z "$TASK_ID" ] || [ -z "$OUTPUT_DIR" ]; then
    echo "Usage: $0 <task_id> <output_dir>"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

docker run --rm n132/arvo:${TASK_ID}-vul sh -c '
  apt-get update -qq && apt-get install -y -qq bear >/dev/null 2>&1
  bear arvo compile >/dev/null 2>&1
  cat compile_commands.json 2>/dev/null || echo "[]"
' > "${OUTPUT_DIR}/${TASK_ID}.json"

entries=$(grep -c '"file"' "${OUTPUT_DIR}/${TASK_ID}.json" 2>/dev/null || echo 0)
echo "arvo:$TASK_ID - $entries compile commands"
