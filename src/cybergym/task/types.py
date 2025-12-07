from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

DEFAULT_SALT = "CyberGym"

# Rubric configurations: maps rubric name to (file_name, grading_schema)
RUBRICS = {
    "five-point": ("rubric.md", "five-point"),
    "granular": ("rubric_granular.md", "granular"),
    # Single-criterion focused rubrics
    "typecast_issues": ("rubric_typecast_issues.md", "typecast_issues"),
    "struct_class_recovery": ("rubric_struct_class_recovery.md", "struct_class_recovery"),
    "function_signature_recovery": ("rubric_function_signature_recovery.md", "function_signature_recovery"),
}


class TaskType(StrEnum):
    ARVO = "arvo"
    OSS_FUZZ = "oss-fuzz"
    OSS_FUZZ_LATEST = "oss-fuzz-latest"
    REVERSE_ENGINEERING = "reverse_engineering"
    FLARE_ON = "flare-on"
    GOOGLE_CTF = "google-ctf"
    DEFCON_OOO = "defcon-ooo"


class TaskDifficulty(StrEnum):
    level0 = "level0"
    level1 = "level1"
    level2 = "level2"
    level3 = "level3"


class Task(BaseModel):
    task_id: str  # task_type:id, e.g., "arvo:1234"
    agent_id: str  # unique agent ID
    checksum: str  # checksum for verifying the task_id and agent_id
    server: str  # server address
    difficulty: TaskDifficulty
    with_flag: bool = False  # whether the task is CTF-style and has a flag or not
    evaluation_mode: str = "exploit"  # "exploit" or "reverse_engineering"
    task_type: str | None = None  # task category (e.g., "arvo", "oss-fuzz", "reverse_engineering")
    rubric: str = "five-point"  # rubric to use for RE evaluation (see RUBRICS)


class TaskConfig(BaseModel):
    """Configuration for task generation"""

    task_id: str
    out_dir: Path
    data_dir: Path
    server: str
    difficulty: TaskDifficulty
    salt: str = DEFAULT_SALT
    agent_id: str | None = None
    with_flag: bool = False
    evaluation_mode: str = "exploit"  # "exploit" or "reverse_engineering"
    rubric: str = "five-point"  # rubric to use: "five-point", "granular"


def verify_task(task_id: str, agent_id: str, checksum: str, salt: str = DEFAULT_SALT) -> bool:
    """
    Verify the task by checking if the task_id, agent_id, and checksum are valid.
    """
    # Generate the expected checksum
    expected_checksum = sha256(f"{task_id}{agent_id}{salt}".encode()).hexdigest()

    return expected_checksum == checksum


def generate_agent_id_and_checksum(
    task_id: str, salt: str = DEFAULT_SALT, agent_id: str | None = None
) -> tuple[str, str]:
    """
    Generate a unique agent ID and checksum based on the task ID and salt.
    """
    # Create a unique agent ID
    if agent_id is None:
        agent_id = uuid4().hex

    # Generate a checksum based on the task_id, agent_id, and salt
    checksum = sha256(f"{task_id}{agent_id}{salt}".encode()).hexdigest()

    return agent_id, checksum
