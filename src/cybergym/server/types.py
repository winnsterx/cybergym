from pydantic import BaseModel


class Payload(BaseModel):
    task_id: str  # task_type:id, e.g., "arvo:1234"
    agent_id: str  # unique agent ID
    checksum: str  # checksum for verifying the task_id and agent_id
    data: bytes | None = None  # bytes
    require_flag: bool = False  # whether to require a flag or not


class PocQuery(BaseModel):
    agent_id: str | None = None
    task_id: str | None = None


class VerifyPocs(BaseModel):
    agent_id: str


class RESubmissionPayload(BaseModel):
    task_id: str  # task_type:id, e.g., "arvo:1234"
    agent_id: str  # unique agent ID
    checksum: str  # checksum for verifying the task_id and agent_id
    pseudocode: str  # pseudocode text content


class RESubmissionQuery(BaseModel):
    agent_id: str | None = None
    task_id: str | None = None


class FlareOnSubmissionPayload(BaseModel):
    task_id: str  # task_type:id, e.g., "flare-on:2024-01"
    agent_id: str  # unique agent ID
    checksum: str  # checksum for verifying the task_id and agent_id
    flag: str  # submitted flag


class FlareOnSubmissionQuery(BaseModel):
    agent_id: str | None = None
    task_id: str | None = None
    correct: int | None = None  # 1 = correct, 0 = incorrect