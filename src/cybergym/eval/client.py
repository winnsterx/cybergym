"""
Unified client for querying submissions from CyberGym server.
Handles both local database (Docker runtime) and HTTP API (Modal runtime).
"""

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RESubmissionResult:
    """Result from querying an RE submission."""

    submission_id: str
    agent_id: str
    task_id: str
    pseudocode: str
    pseudocode_hash: str
    evaluations: list[dict] | None = None


@dataclass
class CTFSubmissionResult:
    """Result from querying a CTF submission."""

    submission_id: str
    agent_id: str
    task_id: str
    submitted_flag: str
    correct: bool


class SubmissionClient:
    """
    Unified client for querying submissions.

    Uses HTTP API when server_url is provided (Modal runtime),
    otherwise uses local database (Docker runtime).
    """

    def __init__(
        self,
        server_url: str | None = None,
        db_path: Path | None = None,
    ):
        self.server_url = server_url
        self.db_path = db_path
        self._engine = None

        if not server_url and not db_path:
            raise ValueError("Either server_url or db_path must be provided")

    @property
    def is_http_mode(self) -> bool:
        return self.server_url is not None

    def _get_engine(self):
        """Lazy-load database engine."""
        if self._engine is None:
            from cybergym.server.pocdb import init_engine

            self._engine = init_engine(self.db_path)
        return self._engine

    def _get_api_key(self) -> str:
        return os.environ.get("CYBERGYM_API_KEY", "cybergym-030a0cd7-5908-4862-8ab9-91f2bfc7b56d")

    # ===== RE Submissions =====

    def get_re_submission(
        self,
        task_id: str,
        agent_id: str,
    ) -> RESubmissionResult | None:
        """Get RE submission for a task/agent."""
        if self.is_http_mode:
            return self._get_re_submission_http(task_id, agent_id)
        return self._get_re_submission_local(task_id, agent_id)

    def _get_re_submission_http(
        self,
        task_id: str,
        agent_id: str,
    ) -> RESubmissionResult | None:
        """Query RE submission via HTTP API."""
        try:
            query_url = f"{self.server_url.rstrip('/')}/query-re-submissions"
            query_data = json.dumps({
                "task_id": task_id,
                "agent_id": agent_id,
            }).encode("utf-8")

            req = urllib.request.Request(
                query_url,
                data=query_data,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self._get_api_key(),
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                submissions = json.loads(response.read().decode("utf-8"))
                if submissions:
                    s = submissions[0]
                    evaluations = s.get("evaluations")
                    if evaluations and isinstance(evaluations, str):
                        evaluations = json.loads(evaluations)
                    return RESubmissionResult(
                        submission_id=s["submission_id"],
                        agent_id=s["agent_id"],
                        task_id=s["task_id"],
                        pseudocode=s.get("pseudocode", ""),
                        pseudocode_hash=s.get("pseudocode_hash", ""),
                        evaluations=evaluations,
                    )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            logger.error(f"HTTP error querying RE submission: {e}")
            raise
        except Exception as e:
            logger.error(f"Error querying RE submission via HTTP: {e}")
            raise

        return None

    def _get_re_submission_local(
        self,
        task_id: str,
        agent_id: str,
    ) -> RESubmissionResult | None:
        """Query RE submission from local database."""
        from sqlalchemy.orm import Session

        from cybergym.server.pocdb import RESubmission

        engine = self._get_engine()
        with Session(engine) as session:
            submission = (
                session.query(RESubmission)
                .filter(
                    RESubmission.task_id == task_id,
                    RESubmission.agent_id == agent_id,
                )
                .first()
            )

            if submission:
                evaluations = None
                if submission.evaluations:
                    evaluations = json.loads(submission.evaluations)
                return RESubmissionResult(
                    submission_id=submission.submission_id,
                    agent_id=submission.agent_id,
                    task_id=submission.task_id,
                    pseudocode=submission.pseudocode,
                    pseudocode_hash=submission.pseudocode_hash,
                    evaluations=evaluations,
                )

        return None

    def list_re_submissions(
        self,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[RESubmissionResult]:
        """List RE submissions with optional filters."""
        if self.is_http_mode:
            return self._list_re_submissions_http(task_id, agent_id)
        return self._list_re_submissions_local(task_id, agent_id)

    def _list_re_submissions_http(
        self,
        task_id: str | None,
        agent_id: str | None,
    ) -> list[RESubmissionResult]:
        """List RE submissions via HTTP."""
        try:
            query_url = f"{self.server_url.rstrip('/')}/query-re-submissions"
            query_data = json.dumps({
                "task_id": task_id,
                "agent_id": agent_id,
            }).encode("utf-8")

            req = urllib.request.Request(
                query_url,
                data=query_data,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self._get_api_key(),
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                submissions = json.loads(response.read().decode("utf-8"))
                results = []
                for s in submissions:
                    evaluations = s.get("evaluations")
                    if evaluations and isinstance(evaluations, str):
                        evaluations = json.loads(evaluations)
                    results.append(
                        RESubmissionResult(
                            submission_id=s["submission_id"],
                            agent_id=s["agent_id"],
                            task_id=s["task_id"],
                            pseudocode=s.get("pseudocode", ""),
                            pseudocode_hash=s.get("pseudocode_hash", ""),
                            evaluations=evaluations,
                        )
                    )
                return results
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
        except Exception as e:
            logger.error(f"Error listing RE submissions via HTTP: {e}")
            return []

    def _list_re_submissions_local(
        self,
        task_id: str | None,
        agent_id: str | None,
    ) -> list[RESubmissionResult]:
        """List RE submissions from local database."""
        from sqlalchemy.orm import Session

        from cybergym.server.pocdb import query_re_submissions

        engine = self._get_engine()
        with Session(engine) as session:
            submissions = query_re_submissions(session, agent_id=agent_id, task_id=task_id)
            results = []
            for s in submissions:
                evaluations = None
                if s.evaluations:
                    evaluations = json.loads(s.evaluations)
                results.append(
                    RESubmissionResult(
                        submission_id=s.submission_id,
                        agent_id=s.agent_id,
                        task_id=s.task_id,
                        pseudocode=s.pseudocode,
                        pseudocode_hash=s.pseudocode_hash,
                        evaluations=evaluations,
                    )
                )
            return results

    # ===== CTF Submissions =====

    def get_ctf_submissions(
        self,
        task_id: str,
        agent_id: str,
        correct: int | None = None,
    ) -> list[CTFSubmissionResult]:
        """Get CTF submissions for a task/agent."""
        if self.is_http_mode:
            return self._get_ctf_submissions_http(task_id, agent_id, correct)
        return self._get_ctf_submissions_local(task_id, agent_id, correct)

    def _get_ctf_submissions_http(
        self,
        task_id: str,
        agent_id: str,
        correct: int | None,
    ) -> list[CTFSubmissionResult]:
        """Query CTF submissions via HTTP."""
        try:
            query_url = f"{self.server_url.rstrip('/')}/query-ctf-submissions"
            query_data = {"task_id": task_id, "agent_id": agent_id}
            if correct is not None:
                query_data["correct"] = correct

            req = urllib.request.Request(
                query_url,
                data=json.dumps(query_data).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self._get_api_key(),
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                submissions = json.loads(response.read().decode("utf-8"))
                return [
                    CTFSubmissionResult(
                        submission_id=s["submission_id"],
                        agent_id=s["agent_id"],
                        task_id=s["task_id"],
                        submitted_flag=s.get("submitted_flag", ""),
                        correct=bool(s.get("correct", 0)),
                    )
                    for s in submissions
                ]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
        except Exception as e:
            logger.error(f"Error querying CTF submissions via HTTP: {e}")
            return []

    def _get_ctf_submissions_local(
        self,
        task_id: str,
        agent_id: str,
        correct: int | None,
    ) -> list[CTFSubmissionResult]:
        """Query CTF submissions from local database."""
        from sqlalchemy.orm import Session

        from cybergym.server.pocdb import query_ctf_submissions

        engine = self._get_engine()
        with Session(engine) as session:
            submissions = query_ctf_submissions(
                session,
                task_id=task_id,
                agent_id=agent_id,
                correct=correct,
            )
            return [
                CTFSubmissionResult(
                    submission_id=s.submission_id,
                    agent_id=s.agent_id,
                    task_id=s.task_id,
                    submitted_flag=s.submitted_flag,
                    correct=bool(s.correct),
                )
                for s in submissions
            ]

    # ===== Judge Evaluations =====

    def add_judge_evaluation(
        self,
        submission_id: str,
        judge_number: int,
        grading_schema: str,
        category_scores: dict,
        detailed_scores: str,
    ):
        """Add judge evaluation to a submission."""
        if self.is_http_mode:
            return self._add_judge_evaluation_http(
                submission_id, judge_number, grading_schema, category_scores, detailed_scores
            )
        return self._add_judge_evaluation_local(
            submission_id, judge_number, grading_schema, category_scores, detailed_scores
        )

    def _add_judge_evaluation_http(
        self,
        submission_id: str,
        judge_number: int,
        grading_schema: str,
        category_scores: dict,
        detailed_scores: str,
    ):
        """Add judge evaluation via HTTP API."""
        try:
            url = f"{self.server_url.rstrip('/')}/store-evaluation"
            payload = {
                "submission_id": submission_id,
                "judge_number": judge_number,
                "grading_schema": grading_schema,
                "category_scores": category_scores,
                "detailed_scores": detailed_scores,
            }

            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self._get_api_key(),
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
                logger.info(f"Stored evaluation via HTTP: {result}")
                return result
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else str(e)
            logger.error(f"HTTP error storing evaluation: {e.code} - {error_body}")
            raise
        except Exception as e:
            logger.error(f"Error storing evaluation via HTTP: {e}")
            raise

    def _add_judge_evaluation_local(
        self,
        submission_id: str,
        judge_number: int,
        grading_schema: str,
        category_scores: dict,
        detailed_scores: str,
    ):
        """Add judge evaluation to local database."""
        from sqlalchemy.orm import Session

        from cybergym.server.pocdb import add_judge_evaluation

        engine = self._get_engine()
        with Session(engine) as session:
            add_judge_evaluation(
                session,
                submission_id=submission_id,
                judge_number=judge_number,
                grading_schema=grading_schema,
                category_scores=category_scores,
                detailed_scores=detailed_scores,
            )


def get_submission_client(
    server_url: str | None = None,
    db_path: Path | None = None,
    runtime: str | None = None,
    eval_paths=None,
) -> SubmissionClient:
    """
    Factory function to create SubmissionClient based on runtime.

    Args:
        server_url: Server URL for HTTP mode (Modal runtime)
        db_path: Database path for local mode (Docker runtime)
        runtime: Runtime type ("modal" or "docker")
        eval_paths: EvaluationPaths instance (for getting db_path)

    Returns:
        Configured SubmissionClient
    """
    if runtime == "modal" and server_url:
        return SubmissionClient(server_url=server_url)

    if server_url:
        return SubmissionClient(server_url=server_url)

    if db_path is None and eval_paths is not None:
        db_path = eval_paths.database_path

    return SubmissionClient(db_path=db_path)
