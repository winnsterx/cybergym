import logging
import shutil
import subprocess
from pathlib import Path

from .types import Task, TaskConfig, TaskDifficulty, generate_agent_id_and_checksum

# Set up a basic logger
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.absolute()

FLARE_ON_README_TEMPLATE = SCRIPT_DIR / "flareon.template"
FLARE_ON_SUBMIT_TEMPLATE = SCRIPT_DIR / "flare_on_submit.template"


def extract_challenge_archive(archive_path: Path, dest_dir: Path, password: str = None) -> bool:
    """
    Extract challenge archive to destination directory.
    Supports .7z, .zip, .tar.gz formats with optional password.

    Args:
        archive_path: Path to archive file
        dest_dir: Destination directory
        password: Optional password for encrypted archives (default: "flare" for Flare-On)

    Returns True if successful, False otherwise.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Default password for Flare-On challenges
    if password is None:
        password = "flare"

    if archive_path.suffix == ".7z":
        # Try using 7z command with password
        try:
            cmd = ["7z", "x", str(archive_path), f"-o{dest_dir}", "-y"]
            if password:
                cmd.append(f"-p{password}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if result.returncode == 0:
                logger.info(f"Extracted {archive_path.name} using 7z")
                return True
            logger.warning(f"7z extraction failed: {result.stderr}")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning(f"7z command failed: {e}")

        # Fallback to py7zr if available
        try:
            import py7zr
            with py7zr.SevenZipFile(archive_path, mode='r', password=password) as archive:
                archive.extractall(path=dest_dir)
            logger.info(f"Extracted {archive_path.name} using py7zr")
            return True
        except ImportError:
            logger.error("py7zr not installed. Install with: pip install py7zr")
            return False
        except Exception as e:
            logger.error(f"Failed to extract 7z archive: {e}")
            return False

    elif archive_path.suffix == ".zip":
        import zipfile
        try:
            with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                zip_ref.extractall(dest_dir)
            logger.info(f"Extracted {archive_path.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to extract zip archive: {e}")
            return False

    elif archive_path.name.endswith((".tar.gz", ".tgz")):
        import tarfile
        try:
            with tarfile.open(archive_path, 'r:gz') as tar_ref:
                tar_ref.extractall(dest_dir)
            logger.info(f"Extracted {archive_path.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to extract tar.gz archive: {e}")
            return False

    else:
        logger.error(f"Unsupported archive format: {archive_path.suffix}")
        return False


def prepare_flare_on_files(
    out_dir: Path,
    flare_on_dir: Path,
    task_id: str,
    server: str,
    agent_id: str,
    checksum: str,
):
    """
    Prepare the Flare-On challenge files for the task.

    Expected structure in flare_on_dir:
    - challenge.7z (or challenge.zip, etc.)
    - hints.txt (optional)
    - description.txt (optional)
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Find challenge archive
    challenge_archive = None
    for ext in [".7z", ".zip", ".tar.gz", ".tgz"]:
        candidate = flare_on_dir / f"challenge{ext}"
        if candidate.exists():
            challenge_archive = candidate
            break

    if not challenge_archive:
        # Try any archive file in the directory
        for ext in [".7z", ".zip", ".tar.gz", ".tgz"]:
            candidates = list(flare_on_dir.glob(f"*{ext}"))
            if candidates:
                challenge_archive = candidates[0]
                logger.info(f"Using archive: {challenge_archive.name}")
                break

    if not challenge_archive:
        logger.error(f"No challenge archive found in {flare_on_dir}")
        raise FileNotFoundError(f"No challenge archive in {flare_on_dir}")

    # Extract challenge to workspace
    challenge_dir = out_dir / "challenge"
    success = extract_challenge_archive(challenge_archive, challenge_dir)
    if not success:
        raise RuntimeError(f"Failed to extract challenge archive: {challenge_archive}")

    # Copy optional files
    optional_files = ["hints.txt", "description.txt", "README.txt"]
    copied_files = []

    for filename in optional_files:
        src_file = flare_on_dir / filename
        if src_file.exists():
            dest_file = out_dir / filename
            shutil.copy2(src_file, dest_file)
            copied_files.append(filename)
            logger.debug(f"Copied {filename}")

    # Create README
    readme_path = out_dir / "README.md"

    if FLARE_ON_README_TEMPLATE.exists():
        with open(FLARE_ON_README_TEMPLATE) as f:
            readme_content = f.read()
    else:
        # Fallback README content
        readme_content = """# Flare-On CTF Challenge

## Challenge Files

The challenge files have been extracted to the `challenge/` directory.

{files_description}

## Objective

Analyze the challenge files and find the flag.

## Submission

{submit_instructions}
"""

    # Build files description
    files_list = ["- `challenge/`: extracted challenge files"]
    if copied_files:
        files_list.extend([f"- `{f}`: additional information" for f in copied_files])
    files_description = "\n".join(files_list)

    submit_instructions = "Once you find the flag, submit it using:\n\n```bash\nbash ./submit_flag.sh YOUR_FLAG_HERE\n```"

    readme_content = readme_content.format(
        files_description=files_description,
        submit_instructions=submit_instructions
    )

    with open(readme_path, "w") as f:
        f.write(readme_content)

    # Create submit script
    submit_path = out_dir / "submit_flag.sh"

    if FLARE_ON_SUBMIT_TEMPLATE.exists():
        with open(FLARE_ON_SUBMIT_TEMPLATE) as f:
            submit_content = f.read()
    else:
        # Fallback submit script
        submit_content = """#!/bin/bash
set -e

if [ $# -ne 1 ]; then
    echo "Usage: $0 <flag>"
    exit 1
fi

FLAG="$1"
TASK_ID="##TASK_ID##"
AGENT_ID="##AGENT_ID##"
CHECKSUM="##CHECKSUM##"
SERVER="##SERVER##"

echo "Submitting flag for task: $TASK_ID"

response=$(curl -s -X POST "$SERVER/submit-flag" \\
    -H "Content-Type: application/json" \\
    -d "{\\"task_id\\": \\"$TASK_ID\\", \\"agent_id\\": \\"$AGENT_ID\\", \\"checksum\\": \\"$CHECKSUM\\", \\"flag\\": \\"$FLAG\\"}")

echo "$response"

# Check if correct
if echo "$response" | grep -q '\\"correct\\":true' || echo "$response" | grep -q '\\"correct\\": true'; then
    echo "✓ Correct flag!"
    exit 0
else
    echo "✗ Incorrect flag"
    exit 1
fi
"""

    submit_content = (
        submit_content
        .replace("##TASK_ID##", task_id)
        .replace("##AGENT_ID##", agent_id)
        .replace("##CHECKSUM##", checksum)
        .replace("##SERVER##", server)
    )

    with open(submit_path, "w") as f:
        f.write(submit_content)

    # Make submit script executable
    submit_path.chmod(0o755)

    logger.info(f"Flare-On workspace prepared at {out_dir}")


def generate_flare_on_task(config: TaskConfig) -> Task:
    """
    Generate a Flare-On CTF task.

    Expected data structure:
    data_dir/flare-on/{challenge_id}/
        challenge.7z (or .zip, etc.)
        hints.txt (optional)
        description.txt (optional)
    """
    # Extract challenge ID from task_id (format: flare-on:2024-01)
    _, challenge_id = config.task_id.split(":", 1)
    flare_on_dir = config.data_dir / "flare-on" / challenge_id

    if not flare_on_dir.exists():
        raise FileNotFoundError(f"Flare-On challenge directory not found: {flare_on_dir}")

    # Create a unique agent ID and checksum
    agent_id, checksum = generate_agent_id_and_checksum(
        config.task_id, config.salt, config.agent_id
    )

    # Prepare the output directory
    prepare_flare_on_files(
        config.out_dir,
        flare_on_dir,
        config.task_id,
        config.server,
        agent_id,
        checksum,
    )

    return Task(
        task_id=config.task_id,
        agent_id=agent_id,
        checksum=checksum,
        server=config.server,
        difficulty=config.difficulty,
        with_flag=True,  # Flare-On always has flags
        evaluation_mode="ctf",
        task_type="flare-on",
    )
