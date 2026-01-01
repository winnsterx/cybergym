#!/bin/bash
# Build an arvo container with clang-extract pre-installed
#
# Usage:
#   ./build-arvo-ce.sh 368        # Build arvo:368-vul-ce
#   ./build-arvo-ce.sh 368 run    # Build and run interactively

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOCKERFILE="${REPO_ROOT}/tools/clang-extract/Dockerfile.arvo"

if [ -z "$1" ]; then
    echo "Usage: $0 <task_id> [run]"
    echo "Example: $0 368"
    echo "         $0 368 run"
    exit 1
fi

TASK_ID=$1
BASE_IMAGE="n132/arvo:${TASK_ID}-vul"
TARGET_IMAGE="arvo:${TASK_ID}-vul-ce"

echo "Building ${TARGET_IMAGE} from ${BASE_IMAGE}..."
docker build \
    --build-arg BASE_IMAGE="${BASE_IMAGE}" \
    -t "${TARGET_IMAGE}" \
    -f "${DOCKERFILE}" \
    "$(dirname "${DOCKERFILE}")"

echo "Done! Image: ${TARGET_IMAGE}"

if [ "$2" = "run" ]; then
    echo "Starting container..."
    docker run -it "${TARGET_IMAGE}" bash
fi
