import datetime
from pathlib import Path

from sqlalchemy import Column, DateTime, Engine, Float, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


def now():
    return datetime.datetime.now(datetime.UTC)


class PoCRecord(Base):
    __tablename__ = "poc_records"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String, index=True)
    task_id = Column(String, index=True)
    poc_id = Column(String, unique=True, index=True)
    poc_hash = Column(String, index=True)
    poc_length = Column(Integer, nullable=True)
    vul_exit_code = Column(Integer, nullable=True)
    fix_exit_code = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)
    __table_args__ = (UniqueConstraint("agent_id", "task_id", "poc_hash", name="_agent_task_hash_uc"),)

    def to_dict(self):
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "poc_id": self.poc_id,
            "poc_hash": self.poc_hash,
            "poc_length": self.poc_length,
            "vul_exit_code": self.vul_exit_code,
            "fix_exit_code": self.fix_exit_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RESubmission(Base):
    __tablename__ = "re_submissions"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String, index=True)
    task_id = Column(String, index=True)
    submission_id = Column(String, unique=True, index=True)

    # Submission content
    pseudocode = Column(String)
    pseudocode_hash = Column(String, index=True)

    # Multi-judge evaluation support
    # JSON array: [{"judge_number": 0, "grading_schema": "...", "category_scores": {...}, "detailed_scores": "...", "evaluated_at": "..."}, ...]
    evaluations = Column(String, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=now, nullable=False)

    __table_args__ = (UniqueConstraint("agent_id", "task_id", "pseudocode_hash", name="_agent_task_hash_uc"),)

    def to_dict(self):
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "submission_id": self.submission_id,
            "pseudocode": self.pseudocode,
            "pseudocode_hash": self.pseudocode_hash,
            "evaluations": self.evaluations,
            "created_at": self.created_at,
        }


class CTFSubmission(Base):
    __tablename__ = "ctf_submissions"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String, index=True)
    task_id = Column(String, index=True)
    submission_id = Column(String, unique=True, index=True)

    # Submission content
    submitted_flag = Column(String)
    flag_hash = Column(String, index=True)

    # Evaluation result
    correct = Column(Integer)  # 1 = correct, 0 = incorrect

    # Timestamps
    created_at = Column(DateTime, default=now, nullable=False)

    __table_args__ = (UniqueConstraint("agent_id", "task_id", "flag_hash", name="_ctf_agent_task_hash_uc"),)

    def to_dict(self):
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "submission_id": self.submission_id,
            "submitted_flag": self.submitted_flag,
            "flag_hash": self.flag_hash,
            "correct": self.correct,
            "created_at": self.created_at,
        }


def get_or_create_poc(
    db: Session, agent_id: str, task_id: str, poc_id: str, poc_hash: str, poc_length: int
) -> PoCRecord:
    record = db.query(PoCRecord).filter_by(agent_id=agent_id, task_id=task_id, poc_hash=poc_hash).first()
    if record:
        return record
    record = PoCRecord(
        agent_id=agent_id,
        task_id=task_id,
        poc_id=poc_id,
        poc_hash=poc_hash,
        poc_length=poc_length,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def update_poc_output(db: Session, record: PoCRecord, mode: str, exit_code: int):
    if mode == "vul":
        record.vul_exit_code = exit_code
    elif mode == "fix":
        record.fix_exit_code = exit_code
    db.commit()


def get_poc_by_hash(
    db: Session,
    agent_id: str | None = None,
    task_id: str | None = None,
    poc_hash: str | None = None,
) -> list[PoCRecord]:
    filters = {}
    if agent_id is not None:
        filters["agent_id"] = agent_id
    if task_id is not None:
        filters["task_id"] = task_id
    if poc_hash is not None:
        filters["poc_hash"] = poc_hash
    if not filters:
        return None  # or raise ValueError("At least one filter must be provided")

    # TODO: add limit
    return db.query(PoCRecord).filter_by(**filters).all()


def get_or_create_re_submission(
    db: Session,
    agent_id: str,
    task_id: str,
    submission_id: str,
    pseudocode: str,
    pseudocode_hash: str,
) -> tuple[RESubmission, bool]:
    """
    Get or create a RE submission record.
    Returns: (RESubmission object, created flag)
    """
    record = db.query(RESubmission).filter_by(agent_id=agent_id, task_id=task_id, pseudocode_hash=pseudocode_hash).first()
    if record:
        return record, False

    record = RESubmission(
        agent_id=agent_id,
        task_id=task_id,
        submission_id=submission_id,
        pseudocode=pseudocode,
        pseudocode_hash=pseudocode_hash,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record, True


def query_re_submissions(
    db: Session,
    agent_id: str | None = None,
    task_id: str | None = None,
) -> list[RESubmission]:
    """Query RE submissions with flexible filtering."""
    filters = {}
    if agent_id is not None:
        filters["agent_id"] = agent_id
    if task_id is not None:
        filters["task_id"] = task_id

    return db.query(RESubmission).filter_by(**filters).all()


def add_judge_evaluation(
    db: Session,
    submission_id: str,
    judge_number: int,
    grading_schema: str,
    category_scores: dict,
    detailed_scores: str,
) -> RESubmission:
    """Add a judge evaluation to a RE submission.

    Args:
        db: Database session
        submission_id: Submission identifier
        judge_number: Index of judge evaluation (0, 1, 2, ...)
        grading_schema: Name of grading schema used (e.g., "five-point", "simple")
        category_scores: Dict mapping category names to scores
        detailed_scores: JSON string containing full evaluation structure

    Returns:
        Updated RESubmission record
    """
    import json

    record = db.query(RESubmission).filter_by(submission_id=submission_id).first()
    if not record:
        msg = f"Submission {submission_id} not found"
        raise ValueError(msg)

    # Parse existing evaluations or start fresh
    evaluations = json.loads(record.evaluations) if record.evaluations else []

    # Create new evaluation entry
    evaluation = {
        "judge_number": judge_number,
        "grading_schema": grading_schema,
        "category_scores": category_scores,
        "detailed_scores": detailed_scores,
        "evaluated_at": now().isoformat(),
    }

    # Check if this judge_number already exists and replace, or append
    existing_idx = next((i for i, e in enumerate(evaluations) if e["judge_number"] == judge_number), None)
    if existing_idx is not None:
        evaluations[existing_idx] = evaluation
    else:
        evaluations.append(evaluation)

    record.evaluations = json.dumps(evaluations)

    db.commit()
    db.refresh(record)
    return record


def get_judge_evaluation(
    db: Session,
    submission_id: str,
    judge_number: int,
) -> dict | None:
    """Get a specific judge evaluation from a submission.

    Args:
        db: Database session
        submission_id: Submission identifier
        judge_number: Index of judge evaluation to retrieve

    Returns:
        Evaluation dict or None if not found
    """
    import json

    record = db.query(RESubmission).filter_by(submission_id=submission_id).first()
    if not record or not record.evaluations:
        return None

    evaluations = json.loads(record.evaluations)
    for e in evaluations:
        if e["judge_number"] == judge_number:
            return e
    return None


def get_all_evaluations(
    db: Session,
    submission_id: str,
) -> list[dict]:
    """Get all judge evaluations for a submission.

    Args:
        db: Database session
        submission_id: Submission identifier

    Returns:
        List of evaluation dicts (empty if none)
    """
    import json

    record = db.query(RESubmission).filter_by(submission_id=submission_id).first()
    if not record or not record.evaluations:
        return []

    return json.loads(record.evaluations)


def count_evaluations(
    db: Session,
    submission_id: str,
) -> int:
    """Count number of judge evaluations for a submission.

    Args:
        db: Database session
        submission_id: Submission identifier

    Returns:
        Number of evaluations
    """
    import json

    record = db.query(RESubmission).filter_by(submission_id=submission_id).first()
    if not record or not record.evaluations:
        return 0

    return len(json.loads(record.evaluations))


def get_or_create_ctf_submission(
    db: Session,
    agent_id: str,
    task_id: str,
    submission_id: str,
    submitted_flag: str,
    flag_hash: str,
    correct: int,
) -> tuple[CTFSubmission, bool]:
    """
    Get or create a Flare-On submission record.
    Returns: (CTFSubmission object, created flag)
    """
    record = db.query(CTFSubmission).filter_by(
        agent_id=agent_id, task_id=task_id, flag_hash=flag_hash
    ).first()
    if record:
        return record, False

    record = CTFSubmission(
        agent_id=agent_id,
        task_id=task_id,
        submission_id=submission_id,
        submitted_flag=submitted_flag,
        flag_hash=flag_hash,
        correct=correct,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record, True


def query_ctf_submissions(
    db: Session,
    agent_id: str | None = None,
    task_id: str | None = None,
    correct: int | None = None,
) -> list[CTFSubmission]:
    """Query Flare-On submissions with flexible filtering."""
    query = db.query(CTFSubmission)

    if agent_id is not None:
        query = query.filter(CTFSubmission.agent_id == agent_id)
    if task_id is not None:
        query = query.filter(CTFSubmission.task_id == task_id)
    if correct is not None:
        query = query.filter(CTFSubmission.correct == correct)

    return query.all()


def init_engine(db_path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}", echo=False, connect_args={"check_same_thread": False}, pool_size=64, max_overflow=64
    )
    Base.metadata.create_all(engine)
    return engine
