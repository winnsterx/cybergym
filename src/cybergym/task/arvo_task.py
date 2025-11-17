import io
import logging
import shutil
import tarfile
from pathlib import Path
from typing import Literal

import docker

from cybergym.utils import get_arvo_id

from .types import Task, TaskConfig, TaskDifficulty, generate_agent_id_and_checksum

# Set up a basic logger
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.absolute()

ARVO_README_TEMPLATE = SCRIPT_DIR / "README.template"
SUBMIT_TEMPLATE = SCRIPT_DIR / "submit.template"

ARVO_FILES = {
    "repo-vul.tar.gz": "source code of the vulnerable program",
    "repo-fix.tar.gz": "source code of the patched program",
    "binaries/*.vul": "vulnerable binary program with original name + '.vul'",
    "binaries/*.fix": "patched binary program with original name + '.fix'",
    "error.txt": "the output of the vulnerable program with poc",
    "description.txt": "the description of the vulnerability",
    "patch.diff": "diff file of the patch commit",
    "poc": "the reference poc",
}

DIFFICULTY_FILES: dict[TaskDifficulty, list[str]] = {
    TaskDifficulty.level0: ["repo-vul.tar.gz"],
    TaskDifficulty.level1: ["repo-vul.tar.gz", "description.txt"],
    TaskDifficulty.level2: ["repo-vul.tar.gz", "description.txt", "error.txt"],
    TaskDifficulty.level3: [
        "repo-vul.tar.gz",
        "repo-fix.tar.gz",
        "error.txt",
        "description.txt",
        "patch.diff",
    ],
}

# RE mode file selection - separate from exploit mode
RE_DIFFICULTY_FILES: dict[TaskDifficulty, list[str]] = {
    TaskDifficulty.level0: [
        # Level 0: binary only - no hints
        "binary.vul",
    ],
    TaskDifficulty.level1: [
        # Level 1: binary + hints (if available)
        "binary.vul",
        "hints.txt",
    ],
    TaskDifficulty.level2: [
        # Level 2: binary + hints + example output
        "binary.vul",
        "hints.txt",
        "output_example.txt",
    ],
    TaskDifficulty.level3: [
        # Level 3: binary + all available hints
        "binary.vul",
        "hints.txt",
        "output_example.txt",
    ],
}

# File descriptions for RE mode README
RE_ARVO_FILES = {
    "binary.vul": "executable to reverse engineer",
    "hints.txt": "high-level functionality hints",
    "output_example.txt": "example output of the binary",
}


def extract_binary_from_docker(arvo_id: str, mode: Literal["vul", "fix"] = "vul") -> Path | None:
    """
    Extract the actual binary from the ARVO Docker image and return path to extracted binary.

    The /bin/arvo wrapper script calls the actual fuzzer binary located at /out/coder_*_fuzzer.
    We use docker cp to efficiently extract the binary without needing to start/stop the container.
    Returns None if extraction fails.
    """
    client = docker.from_env()
    container = None

    try:
        image_name = f"n132/arvo:{arvo_id}-{mode}"
        logger.debug(f"Extracting binary from Docker image: {image_name}")

        # Create a temporary container (don't start it yet)
        container = client.containers.create(image=image_name)
        container_id = container.id

        # Get the list of fuzzer binaries from the image
        # We'll use get_archive to find them
        bits, stat = container.get_archive("/out")
        tar_data = b"".join(bits)

        # Extract tar to find first fuzzer binary
        with tarfile.open(fileobj=io.BytesIO(tar_data)) as tar:
            fuzzer_files = [m for m in tar.getmembers() if "coder_" in m.name and "_fuzzer" in m.name and m.isfile()]
            if not fuzzer_files:
                logger.warning(f"No fuzzer binary found in {image_name}")
                return None

            # Extract the first fuzzer binary
            member = fuzzer_files[0]
            logger.debug(f"Found fuzzer binary: {member.name}")

            extracted_file = tar.extractfile(member)
            binary_data = extracted_file.read()

            # Create a temporary file to store the binary
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(binary_data)
                tmp_path = Path(tmp.name)
                tmp_path.chmod(0o755)  # Make it executable
                logger.debug(f"Extracted binary ({len(binary_data)} bytes) to temporary location: {tmp_path}")
                return tmp_path

    except Exception as e:
        logger.warning(f"Failed to extract binary from Docker image {arvo_id}-{mode}: {e}")
        return None
    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception:
                pass  # Silently ignore removal errors


def prepare_arvo_files(
    out_dir: Path,
    arvo_dir: Path,
    task_id: str,
    server: str,
    agent_id: str,
    checksum: str,
    difficulty: TaskDifficulty,
    with_flag: bool = False,
    evaluation_mode: str = "exploit",
):
    """
    Prepare the ARVO files for the task.
    """
    # Prepare the data files - select based on evaluation_mode
    logger.debug(f"evaluation_mode: {evaluation_mode}, difficulty: {difficulty}")

    if evaluation_mode == "reverse_engineering":
        # RE mode: binary + optional hints only
        # Check if binary is already cached in arvo_dir
        arvo_id = get_arvo_id(task_id)
        cached_binary = arvo_dir / "binary.vul"

        if not cached_binary.exists():
            # Extract binary from Docker image and cache it
            logger.debug(f"Binary not found at {cached_binary}, extracting from Docker...")
            binary_path = extract_binary_from_docker(arvo_id, mode="vul")
            if binary_path:
                # Save to cache location in data directory
                cached_binary.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(binary_path, cached_binary)
                logger.info(f"Extracted and cached binary to {cached_binary}")
                # Clean up temporary file
                try:
                    binary_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary binary: {e}")
            else:
                logger.warning("Failed to extract binary from Docker image, continuing without it")
        else:
            logger.debug(f"Using cached binary from {cached_binary}")

        # Add optional hints based on difficulty
        globs_to_copy = RE_DIFFICULTY_FILES.get(difficulty, [])
    else:
        # Exploit mode: use standard difficulty-based selection (unchanged)
        globs_to_copy = DIFFICULTY_FILES.get(difficulty, [])

    logger.debug(f"Files to copy: {globs_to_copy}")

    for glob_pat in globs_to_copy:
        for file in arvo_dir.glob(glob_pat):
            # Special handling: rename binary.vul to binary in output
            if file.name == "binary.vul":
                to_file = out_dir / "binary"
            else:
                to_file = out_dir / file.relative_to(arvo_dir)
            to_file.parent.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Copying {file} to {to_file}")
            if file.is_dir():
                shutil.copytree(file, to_file)
            else:
                shutil.copy(file, to_file)

    # Prepare the README file
    readme_path = out_dir / "README.md"

    # Select appropriate template and instructions based on evaluation_mode
    if evaluation_mode == "reverse_engineering":
        # RE mode: use RE-specific template
        re_template_path = SCRIPT_DIR / "RE.template"
        if re_template_path.exists():
            with open(re_template_path) as template_file:
                readme_content = template_file.read()
        else:
            # Fallback to exploit template if RE template not found
            logger.warning(f"RE template not found at {re_template_path}, using default template")
            with open(ARVO_README_TEMPLATE) as template_file:
                readme_content = template_file.read()

        # Build files description for RE mode
        files_description = "\n".join(
            f"- `{glob_pat}`: {RE_ARVO_FILES.get(glob_pat, 'unknown file')}"
            for glob_pat in globs_to_copy
            if glob_pat != "binary" and glob_pat != "binaries/*.vul"
        )

        submit_instructions = "please submit the pseudocode file with the command:\n\n```bash\nbash ./re_submit.sh PATH_TO_PSEUDOCODE\n```"

        # Use RE submit template
        re_submit_template_path = SCRIPT_DIR / "re_submit.template"
        if re_submit_template_path.exists():
            with open(re_submit_template_path) as submit_template_file:
                submit_content = submit_template_file.read()
        else:
            logger.warning(f"RE submit template not found at {re_submit_template_path}")
            submit_content = ""

        submit_path = out_dir / "re_submit.sh"
    else:
        # Exploit mode: use standard template (unchanged behavior)
        with open(ARVO_README_TEMPLATE) as template_file:
            readme_content = template_file.read()

        files_description = "\n".join(f"- `{glob_pat}`: {ARVO_FILES[glob_pat]}" for glob_pat in globs_to_copy)

        submit_instructions = "please submit the poc file with the command:\n\n```bash\nbash ./submit.sh PATH_TO_POC\n```"

        # Use standard submit template
        with open(SUBMIT_TEMPLATE) as submit_template_file:
            submit_content = submit_template_file.read()

        submit_path = out_dir / "submit.sh"

    # Fill in the submit template
    submit_content = (
        submit_content.replace("##TASK_ID##", task_id)
        .replace("##AGENT_ID##", agent_id)
        .replace("##CHECKSUM##", checksum)
        .replace("##SERVER##", server)
        .replace("##REQUIRE_FLAG##", str(with_flag).lower())
    )

    with open(submit_path, "w") as submit_file:
        submit_file.write(submit_content)

    readme_content = readme_content.format(files_description=files_description, submit_instructions=submit_instructions)

    with open(readme_path, "w") as readme_file:
        readme_file.write(readme_content)


def generate_arvo_task(config: TaskConfig) -> Task:
    """
    Generate an ARVO task.
    """
    arvo_id = get_arvo_id(config.task_id)
    arvo_dir = config.data_dir / "arvo" / arvo_id

    # Create a unique agent ID and checksum
    agent_id, checksum = generate_agent_id_and_checksum(config.task_id, config.salt, config.agent_id)

    # Prepare the output directory
    prepare_arvo_files(
        config.out_dir,
        arvo_dir,
        config.task_id,
        config.server,
        agent_id,
        checksum,
        config.difficulty,
        config.with_flag,
        evaluation_mode=config.evaluation_mode,
    )

    return Task(
        task_id=config.task_id,
        agent_id=agent_id,
        checksum=checksum,
        server=config.server,
        difficulty=config.difficulty,
        with_flag=config.with_flag,
        evaluation_mode=config.evaluation_mode,
        task_type="arvo",
    )
