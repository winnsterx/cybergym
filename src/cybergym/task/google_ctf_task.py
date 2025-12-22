import logging
import shutil
import tarfile
from pathlib import Path

from .types import Task, TaskConfig, TaskDifficulty, RUBRICS, generate_agent_id_and_checksum

# Set up a basic logger
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.absolute()

RE_TEMPLATE = SCRIPT_DIR / "readme_templates" / "pseudocode.template"
RE_SUBMIT_TEMPLATE = SCRIPT_DIR / "readme_templates" / "pseudocode_submit.template"
CTF_TEMPLATE = SCRIPT_DIR / "readme_templates" / "ctf.template"
CTF_SUBMIT_TEMPLATE = SCRIPT_DIR / "readme_templates" / "ctf_submit.template"

# Google CTF files for agent (RE mode)
GOOGLE_CTF_AGENT_FILES = {
    "attachments/*": "binary executable(s) to reverse engineer",
}


def create_judge_tarball(task_dir: Path, dest_tarball: Path) -> bool:
    """
    Create a tarball containing all source files for judge evaluation.

    Args:
        task_dir: Path to the Google CTF task directory (contains main.c, Makefile, etc.)
        dest_tarball: Path where the tarball should be created

    Returns:
        True if successful, False otherwise
    """
    try:
        dest_tarball.parent.mkdir(parents=True, exist_ok=True)

        with tarfile.open(dest_tarball, "w:gz") as tar:
            # Add all files from task directory except attachments/ (judge needs source, not binary)
            for item in task_dir.iterdir():
                # Skip the attachments directory - judge needs source code, not binaries
                if item.name == "attachments":
                    continue

                # Add the file or directory to the tarball
                arcname = f"src/{item.name}"
                tar.add(item, arcname=arcname)
                logger.debug(f"Added {item.name} to tarball as {arcname}")

        logger.info(f"Created judge tarball at {dest_tarball}")
        return True

    except Exception as e:
        logger.error(f"Failed to create judge tarball: {e}")
        return False


def prepare_google_ctf_files(
    out_dir: Path,
    task_dir: Path,
    task_id: str,
    server: str,
    agent_id: str,
    checksum: str,
    difficulty: TaskDifficulty,
    evaluation_mode: str = "pseudocode",
    rubric: str = "five-point",
):
    """
    Prepare Google CTF files for the task.

    For RE mode (agent):
    - Copies only attachments/ directory (binary executables)
    - Generates README with RE instructions
    - Agent submits pseudocode for judge evaluation

    For CTF mode (agent):
    - Copies only attachments/ directory (binary executables)
    - Generates README with CTF instructions
    - Agent submits flags for immediate validation

    For judge mode:
    - Creates tarball with all source files (main.c, Makefile, etc.)
    """
    logger.debug(f"evaluation_mode: {evaluation_mode}, difficulty: {difficulty}")

    if evaluation_mode not in ["pseudocode", "ctf"]:
        logger.warning(f"Google CTF tasks support pseudocode and ctf modes, got: {evaluation_mode}")
        evaluation_mode = "pseudocode"

    # Copy attachments for agent
    attachments_src = task_dir / "attachments"
    attachments_dst = out_dir / "attachments"

    if attachments_src.exists():
        shutil.copytree(attachments_src, attachments_dst)
        logger.info(f"Copied attachments from {attachments_src} to {attachments_dst}")

        # Make binaries executable
        for file in attachments_dst.iterdir():
            if file.is_file():
                file.chmod(0o755)
                logger.debug(f"Set executable permissions on {file.name}")
    else:
        logger.error(f"Attachments directory not found: {attachments_src}")

    # Prepare README and submission script based on evaluation mode
    readme_path = out_dir / "README.md"

    if evaluation_mode == "ctf":
        # CTF mode: agent submits flags
        if CTF_TEMPLATE.exists():
            with open(CTF_TEMPLATE) as template_file:
                readme_content = template_file.read()
        else:
            logger.error(f"CTF template not found at {CTF_TEMPLATE}")
            readme_content = "# CTF Challenge\n\nAnalyze the binary in attachments/ and find the flag\n"

        files_description = "- `attachments/*`: binary executable(s) containing the flag"
        submit_instructions = "Once you find the flag, submit it using:\n\n```bash\nbash ./submit_flag.sh YOUR_FLAG_HERE\n```"

        readme_content = readme_content.format(
            files_description=files_description,
            submit_instructions=submit_instructions
        )

        with open(readme_path, "w") as readme_file:
            readme_file.write(readme_content)

        # Create flag submission script
        submit_path = out_dir / "submit_flag.sh"

        if CTF_SUBMIT_TEMPLATE.exists():
            with open(CTF_SUBMIT_TEMPLATE) as submit_template_file:
                submit_content = submit_template_file.read()
        else:
            logger.error(f"CTF submit template not found at {CTF_SUBMIT_TEMPLATE}")
            submit_content = "#!/bin/bash\necho 'Submit script not available'\n"

        submit_content = (
            submit_content.replace("##TASK_ID##", task_id)
            .replace("##AGENT_ID##", agent_id)
            .replace("##CHECKSUM##", checksum)
            .replace("##SERVER##", server)
        )

        with open(submit_path, "w") as submit_file:
            submit_file.write(submit_content)

        submit_path.chmod(0o755)

    else:
        # RE mode: agent submits pseudocode
        if RE_TEMPLATE.exists():
            with open(RE_TEMPLATE) as template_file:
                readme_content = template_file.read()
        else:
            logger.error(f"RE template not found at {RE_TEMPLATE}")
            readme_content = "# Reverse Engineering Task\n\nAnalyze the binary in attachments/\n"

        files_description = "- `attachments/*`: binary executable(s) to reverse engineer"
        submit_instructions = "please submit the pseudocode file with the command:\n\n```bash\nbash ./re_submit.sh PATH_TO_PSEUDOCODE\n```"

        readme_content = readme_content.format(
            files_description=files_description,
            submit_instructions=submit_instructions
        )

        with open(readme_path, "w") as readme_file:
            readme_file.write(readme_content)

        # Create pseudocode submission script
        submit_path = out_dir / "re_submit.sh"

        if RE_SUBMIT_TEMPLATE.exists():
            with open(RE_SUBMIT_TEMPLATE) as submit_template_file:
                submit_content = submit_template_file.read()
        else:
            logger.error(f"RE submit template not found at {RE_SUBMIT_TEMPLATE}")
            submit_content = "#!/bin/bash\necho 'Submit script not available'\n"

        submit_content = (
            submit_content.replace("##TASK_ID##", task_id)
            .replace("##AGENT_ID##", agent_id)
            .replace("##CHECKSUM##", checksum)
            .replace("##SERVER##", server)
        )

        with open(submit_path, "w") as submit_file:
            submit_file.write(submit_content)

        submit_path.chmod(0o755)

    # Copy ghidra manual for both modes, rubric only for RE mode
    shutil.copy(SCRIPT_DIR / "ghidra_manual.md", out_dir / "ghidra_manual.md")
    if evaluation_mode == "pseudocode":
        rubric_file = RUBRICS.get(rubric, RUBRICS["five-point"])[0]
        shutil.copy(SCRIPT_DIR / rubric_file, out_dir / "rubric.md")

    # Create judge tarball for later evaluation
    # Store it in the task directory for judge to use
    judge_tarball = task_dir / "repo-vul.tar.gz"
    if not judge_tarball.exists():
        logger.info(f"Creating judge tarball at {judge_tarball}")
        create_judge_tarball(task_dir, judge_tarball)
    else:
        logger.debug(f"Judge tarball already exists at {judge_tarball}")


def generate_google_ctf_task(config: TaskConfig) -> Task:
    """
    Generate a Google CTF task.

    Google CTF tasks have the structure:
        google-ctf/<task_name>/
        ├── attachments/    # Binaries for agent
        ├── main.c          # Source code for judge
        ├── Makefile
        └── flag
    """
    # Extract task name from task_id (e.g., "google-ctf:reversing-beginner")
    task_parts = config.task_id.split(":", 1)
    if len(task_parts) != 2:
        raise ValueError(f"Invalid Google CTF task_id format: {config.task_id}. Expected 'google-ctf:task-name'")

    task_name = task_parts[1]
    task_dir = config.data_dir / "google-ctf" / task_name

    if not task_dir.exists():
        raise FileNotFoundError(f"Google CTF task directory not found: {task_dir}")

    # Create a unique agent ID and checksum
    agent_id, checksum = generate_agent_id_and_checksum(config.task_id, config.salt, config.agent_id)

    # Prepare the output directory
    prepare_google_ctf_files(
        config.out_dir,
        task_dir,
        config.task_id,
        config.server,
        agent_id,
        checksum,
        config.difficulty,
        evaluation_mode=config.evaluation_mode,
        rubric=config.rubric,
    )

    return Task(
        task_id=config.task_id,
        agent_id=agent_id,
        checksum=checksum,
        server=config.server,
        difficulty=config.difficulty,
        with_flag=True if config.evaluation_mode == "ctf" else config.with_flag,
        evaluation_mode=config.evaluation_mode,
        task_type="google-ctf",
        rubric=config.rubric,
    )
