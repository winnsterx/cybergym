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

    # Evaluation results
    semantic_similarity = Column(Float, nullable=True)
    correctness_score = Column(Float, nullable=True)
    judge_reasoning = Column(String, nullable=True)
    strengths = Column(String, nullable=True)  # JSON list
    weaknesses = Column(String, nullable=True)  # JSON list

    # Timestamps
    created_at = Column(DateTime, default=now, nullable=False)
    evaluated_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("agent_id", "task_id", "pseudocode_hash", name="_agent_task_hash_uc"),)

    def to_dict(self):
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "submission_id": self.submission_id,
            "pseudocode_hash": self.pseudocode_hash,
            "semantic_similarity": self.semantic_similarity,
            "correctness_score": self.correctness_score,
            "judge_reasoning": self.judge_reasoning,
            "strengths": self.strengths,
            "weaknesses": self.weaknesses,
            "created_at": self.created_at,
            "evaluated_at": self.evaluated_at,
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


def update_re_submission_scores(
    db: Session,
    submission_id: str,
    semantic_similarity: float,
    correctness_score: float,
    judge_reasoning: str,
    strengths: str | None = None,
    weaknesses: str | None = None,
) -> RESubmission:
    """Update evaluation scores for a RE submission."""
    record = db.query(RESubmission).filter_by(submission_id=submission_id).first()
    if not record:
        msg = f"Submission {submission_id} not found"
        raise ValueError(msg)

    record.semantic_similarity = semantic_similarity
    record.correctness_score = correctness_score
    record.judge_reasoning = judge_reasoning
    record.strengths = strengths
    record.weaknesses = weaknesses
    record.evaluated_at = now()

    db.commit()
    db.refresh(record)
    return record


def init_engine(db_path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}", echo=False, connect_args={"check_same_thread": False}, pool_size=64, max_overflow=64
    )
    Base.metadata.create_all(engine)
    return engine
