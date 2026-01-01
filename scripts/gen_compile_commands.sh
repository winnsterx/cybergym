#!/bin/bash
# Generate compile_commands.json for an arvo task
# Usage: ./gen_compile_commands.sh <task_id> <output_dir>

TASK_ID=$1
OUTPUT_DIR=$2

if [ -z "$TASK_ID" ] || [ -z "$OUTPUT_DIR" ]; then
    echo "Usage: $0 <task_id> <output_dir>"
    exit 1
fi

echo "=== Generating compile_commands.json for arvo:$TASK_ID ==="

docker run --rm n132/arvo:${TASK_ID}-vul sh -c '
# Set up compiler wrappers
mkdir -p /tmp/compiledb_wrappers

cat > /tmp/compiledb_wrappers/cc_wrapper.py << '\''PYWRAP'\''
#!/usr/bin/env python3
import sys, os, json
from pathlib import Path

compiler = sys.argv[1]
args = sys.argv[2:]
cwd = os.getcwd()

src_file = None
is_compile = "-c" in args
for a in args:
    if a.endswith((".c", ".cc", ".cpp", ".cxx", ".C")):
        src_file = a
        break

if src_file and is_compile:
    db_file = Path("/work/compile_commands.json")
    try:
        db = json.loads(db_file.read_text()) if db_file.exists() else []
    except:
        db = []
    db.append({"directory": cwd, "arguments": [compiler] + args, "file": src_file})
    db_file.write_text(json.dumps(db, indent=2))

os.execvp(compiler, [compiler] + args)
PYWRAP

chmod +x /tmp/compiledb_wrappers/cc_wrapper.py

# Create wrappers for clang and clang++
for cc in clang clang++; do
    real_path=$(which $cc)
    cat > /tmp/compiledb_wrappers/$cc << WRAP
#!/bin/bash
/tmp/compiledb_wrappers/cc_wrapper.py $real_path "\$@"
WRAP
    chmod +x /tmp/compiledb_wrappers/$cc
done

export PATH="/tmp/compiledb_wrappers:$PATH"
mkdir -p /work
rm -f /work/compile_commands.json

# Run arvo compile (sets up env vars properly)
arvo compile >/dev/null 2>&1

# Output only the JSON result
cat /work/compile_commands.json 2>/dev/null || echo "[]"
' > "${OUTPUT_DIR}/${TASK_ID}.json"

# Check result
entries=$(grep -c '"file"' "${OUTPUT_DIR}/${TASK_ID}.json" 2>/dev/null || echo 0)
echo "=== arvo:$TASK_ID: $entries compile commands captured ==="
