"""
Batch runner for evaluating reverse engineering submissions using LLM judge.

This module provides functions to:
1. Extract source code from repo-vul.tar.gz (same way agents do during task setup)
2. Run judge evaluation on unevaluated submissions in batch
3. Update database with evaluation scores

Key Design:
- Minimal changes to existing codebase
- Reuses existing database patterns from server/pocdb.py
- Extracts source from tarball (NOT from Docker)
- Processes submissions in batches to avoid long transactions
- Robust error handling (failures don't stop batch processing)
"""

import json
import logging
import tarfile
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from cybergym.judge.judge import LLMJudge
from cybergym.server.pocdb import RESubmission, init_engine, now

logger = logging.getLogger(__name__)


def extract_source_code_from_tarball(data_dir: Path, task_id: str) -> str:
    """
    Extract source code from repo-vul.tar.gz for a given task.

    This follows the same pattern that agents use during task setup - extracting
    source code from the tarball stored in the data directory.

    Args:
        data_dir: Root data directory (e.g., /path/to/data)
        task_id: Task ID in format "project:id" (e.g., "arvo:10400")

    Returns:
        Concatenated source code string with file headers, or empty string if extraction fails

    Example:
        >>> source = extract_source_code_from_tarball(Path("/data"), "arvo:10400")
        >>> print(source[:100])
        ========== file.c ==========
        #include <stdio.h>
        ...

    Note:
        - Extracts all .c and .h files from the tarball
        - Adds file headers for context (helps judge understand structure)
        - Returns empty string on failure (with warning logged)
        - Task structure: data_dir / "project" / "id" / "repo-vul.tar.gz"
    """
    # Parse task_id: "arvo:10400" -> "arvo", "10400"
    try:
        project, task_num = task_id.split(":")
    except ValueError:
        logger.warning(f"Invalid task_id format: {task_id}, expected 'project:id'")
        return ""

    # Construct path to tarball: data_dir / project / task_num / repo-vul.tar.gz
    tarball_path = data_dir / project / task_num / "repo-vul.tar.gz"

    if not tarball_path.exists():
        logger.warning(f"Tarball not found at {tarball_path} for task {task_id}")
        return ""

    logger.info(f"Extracting source code from {tarball_path}")

    try:
        # Extract source files from tarball
        source_files = []
        with tarfile.open(tarball_path, "r:gz") as tar:
            for member in tar.getmembers():
                # Only extract .c and .h files
                if member.isfile() and (member.name.endswith(".c") or member.name.endswith(".h")):
                    try:
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            content = file_obj.read().decode("utf-8", errors="replace")
                            # Add file header for context
                            filename = Path(member.name).name  # Just the filename, not full path
                            source_files.append(f"{'=' * 10} {filename} {'=' * 10}\n{content}\n")
                            logger.debug(f"Extracted {filename} ({len(content)} bytes)")
                    except Exception as e:
                        logger.warning(f"Failed to extract {member.name}: {e}")
                        continue

        if not source_files:
            logger.warning(f"No .c/.h files found in {tarball_path}")
            return ""

        # Concatenate all source files
        full_source = "\n".join(source_files)
        logger.info(f"Extracted {len(source_files)} source files ({len(full_source)} bytes total)")
        return full_source

    except Exception as e:
        logger.error(f"Failed to extract source code from {tarball_path}: {e}")
        return ""


def run_judge_on_task(
    db_path: str,
    data_dir: Path,
    task_id: str,
    model: str = "claude-sonnet-4-5-20250929",
    batch_size: int = 10,
) -> dict:
    """
    Run judge evaluation on all unevaluated submissions for a given task.

    Processes submissions in batches to avoid long-running transactions.
    Robust error handling ensures one failure doesn't stop entire batch.

    Args:
        db_path: Path to SQLite database
        data_dir: Root data directory containing task files
        task_id: Task ID to evaluate (e.g., "arvo:10400")
        model: LLM model to use for judging
        batch_size: Number of submissions to commit per transaction

    Returns:
        Dict with evaluation results:
        - evaluated_count: Number of successfully evaluated submissions
        - failed_count: Number of submissions that failed evaluation
        - total: Total submissions processed
        - errors: List of error messages (if any)

    Example:
        >>> results = run_judge_on_task(
        ...     db_path="/path/to/db.sqlite",
        ...     data_dir=Path("/data"),
        ...     task_id="arvo:10400",
        ...     batch_size=10
        ... )
        >>> print(f"Evaluated {results['evaluated_count']}/{results['total']}")

    Note:
        - Extracts source code once (reused for all submissions)
        - Commits in batches (not per submission) for efficiency
        - Continues processing even if individual evaluations fail
        - Updates evaluated_at timestamp on success
    """
    logger.info(f"Starting batch evaluation for task_id={task_id}, model={model}")

    # Initialize database engine and session
    engine = init_engine(Path(db_path))

    # Extract source code once (reuse for all submissions)
    source_code = extract_source_code_from_tarball(data_dir, task_id)
    if not source_code:
        logger.error(f"Failed to extract source code for task {task_id}, cannot evaluate")
        return {
            "evaluated_count": 0,
            "failed_count": 0,
            "total": 0,
            "errors": [f"Source code extraction failed for task {task_id}"],
        }

    # Initialize judge
    try:
        judge = LLMJudge(model=model)
    except Exception as e:
        logger.error(f"Failed to initialize judge with model {model}: {e}")
        return {
            "evaluated_count": 0,
            "failed_count": 0,
            "total": 0,
            "errors": [f"Judge initialization failed: {str(e)}"],
        }

    # Query unevaluated submissions
    with Session(engine) as session:
        submissions = (
            session.query(RESubmission)
            .filter(RESubmission.task_id == task_id, RESubmission.evaluated_at == None)
            .order_by(RESubmission.id)
            .all()
        )

        total = len(submissions)
        logger.info(f"Found {total} unevaluated submissions for task {task_id}")

        if total == 0:
            return {"evaluated_count": 0, "failed_count": 0, "total": 0, "errors": []}

    # Process submissions
    evaluated_count = 0
    failed_count = 0
    errors = []
    batch = []

    with Session(engine) as session:
        for idx, submission in enumerate(submissions, 1):
            logger.info(
                f"Evaluating submission {idx}/{total}: "
                f"submission_id={submission.submission_id}, agent_id={submission.agent_id}"
            )

            try:
                # Call judge.evaluate
                scores = judge.evaluate(
                    pseudocode=submission.pseudocode,
                    source_code=source_code,
                    task_id=task_id,
                )

                # Get the submission from current session
                db_submission = session.query(RESubmission).filter_by(
                    submission_id=submission.submission_id
                ).first()

                if not db_submission:
                    logger.error(f"Submission {submission.submission_id} not found in session")
                    failed_count += 1
                    errors.append(f"Submission {submission.submission_id} not found in database")
                    continue

                # Update fields
                db_submission.semantic_similarity = scores["semantic_similarity"]
                db_submission.correctness_score = scores["correctness_score"]
                db_submission.judge_reasoning = scores["judge_reasoning"]
                db_submission.strengths = json.dumps(scores["strengths"])  # Store as JSON string
                db_submission.weaknesses = json.dumps(scores["weaknesses"])  # Store as JSON string
                db_submission.evaluated_at = now()

                batch.append(db_submission)
                evaluated_count += 1

                logger.info(
                    f"Submission {submission.submission_id} evaluated: "
                    f"semantic={scores['semantic_similarity']:.2f}, "
                    f"correctness={scores['correctness_score']:.2f}"
                )

                # Commit batch
                if len(batch) >= batch_size:
                    session.commit()
                    logger.info(f"Committed batch of {len(batch)} evaluations")
                    batch = []

            except Exception as e:
                logger.error(f"Failed to evaluate submission {submission.submission_id}: {e}")
                failed_count += 1
                errors.append(f"Submission {submission.submission_id}: {str(e)}")
                # Continue to next submission (don't fail entire batch)
                continue

        # Commit remaining batch
        if batch:
            try:
                session.commit()
                logger.info(f"Committed final batch of {len(batch)} evaluations")
            except Exception as e:
                logger.error(f"Failed to commit final batch: {e}")
                errors.append(f"Final batch commit failed: {str(e)}")

    logger.info(
        f"Batch evaluation complete: {evaluated_count} evaluated, {failed_count} failed, {total} total"
    )

    return {
        "evaluated_count": evaluated_count,
        "failed_count": failed_count,
        "total": total,
        "errors": errors,
    }


def run_judge_on_submission(
    db_path: str,
    data_dir: Path,
    submission_id: str,
    model: str = "claude-sonnet-4-5-20250929",
) -> dict:
    """
    Run judge evaluation on a single submission (for testing/debugging).

    This is a convenience function for evaluating individual submissions,
    useful for testing or re-evaluating specific cases.

    Args:
        db_path: Path to SQLite database
        data_dir: Root data directory containing task files
        submission_id: Submission ID to evaluate
        model: LLM model to use for judging

    Returns:
        Dict with evaluation scores:
        - semantic_similarity: Float score 0.0-1.0
        - correctness_score: Float score 0.0-1.0
        - judge_reasoning: String explanation
        - strengths: List of strings
        - weaknesses: List of strings
        - submission_id: Submission ID
        - task_id: Task ID

    Example:
        >>> scores = run_judge_on_submission(
        ...     db_path="/path/to/db.sqlite",
        ...     data_dir=Path("/data"),
        ...     submission_id="sub_12345"
        ... )
        >>> print(f"Score: {scores['semantic_similarity']:.2f}")

    Raises:
        ValueError: If submission not found or evaluation fails

    Note:
        - Looks up task_id from submission record
        - Updates database with evaluation results
        - Returns full scores dict for immediate inspection
    """
    logger.info(f"Evaluating single submission: submission_id={submission_id}, model={model}")

    # Initialize database engine
    engine = init_engine(Path(db_path))

    # Query submission to get task_id
    with Session(engine) as session:
        submission = session.query(RESubmission).filter_by(submission_id=submission_id).first()

        if not submission:
            msg = f"Submission {submission_id} not found"
            logger.error(msg)
            raise ValueError(msg)

        task_id = submission.task_id
        pseudocode = submission.pseudocode

    logger.info(f"Found submission for task_id={task_id}")

    # Extract source code
    source_code = extract_source_code_from_tarball(data_dir, task_id)
    if not source_code:
        msg = f"Failed to extract source code for task {task_id}"
        logger.error(msg)
        raise ValueError(msg)

    # Initialize judge and evaluate
    try:
        judge = LLMJudge(model=model)
        scores = judge.evaluate(
            pseudocode=pseudocode,
            source_code=source_code,
            task_id=task_id,
        )
    except Exception as e:
        msg = f"Judge evaluation failed: {str(e)}"
        logger.error(msg)
        raise ValueError(msg) from e

    # Update database
    with Session(engine) as session:
        db_submission = session.query(RESubmission).filter_by(submission_id=submission_id).first()

        if not db_submission:
            msg = f"Submission {submission_id} not found in database"
            logger.error(msg)
            raise ValueError(msg)

        db_submission.semantic_similarity = scores["semantic_similarity"]
        db_submission.correctness_score = scores["correctness_score"]
        db_submission.judge_reasoning = scores["judge_reasoning"]
        db_submission.strengths = json.dumps(scores["strengths"])
        db_submission.weaknesses = json.dumps(scores["weaknesses"])
        db_submission.evaluated_at = now()

        session.commit()

    logger.info(
        f"Submission {submission_id} evaluated: "
        f"semantic={scores['semantic_similarity']:.2f}, "
        f"correctness={scores['correctness_score']:.2f}"
    )

    # Return scores with metadata
    return {
        **scores,
        "submission_id": submission_id,
        "task_id": task_id,
    }


# CLI entry point for batch evaluation
def main():
    """
    Command-line interface for running judge evaluation.

    Example usage:
        python -m cybergym.judge.run_judge --task arvo:10400 --db /path/to/db.sqlite --data-dir /data
        python -m cybergym.judge.run_judge --submission sub_12345 --db /path/to/db.sqlite --data-dir /data
    """
    import argparse

    parser = argparse.ArgumentParser(description="Run LLM judge evaluation on RE submissions")
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--data-dir", required=True, help="Root data directory")
    parser.add_argument("--task", help="Task ID to evaluate (e.g., arvo:10400)")
    parser.add_argument("--submission", help="Single submission ID to evaluate")
    parser.add_argument("--model", default="claude-sonnet-4-5-20250929", help="LLM model name")
    parser.add_argument("--batch-size", type=int, default=10, help="Batch size for commits")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    data_dir = Path(args.data_dir)

    if args.submission:
        # Single submission evaluation
        try:
            scores = run_judge_on_submission(
                db_path=args.db,
                data_dir=data_dir,
                submission_id=args.submission,
                model=args.model,
            )
            print("\n" + "=" * 70)
            print("EVALUATION RESULTS")
            print("=" * 70)
            print(f"Submission ID: {scores['submission_id']}")
            print(f"Task ID: {scores['task_id']}")
            print(f"Semantic Similarity: {scores['semantic_similarity']:.3f}")
            print(f"Correctness Score: {scores['correctness_score']:.3f}")
            print(f"\nReasoning:\n{scores['judge_reasoning']}")
            print(f"\nStrengths:")
            for s in scores['strengths']:
                print(f"  - {s}")
            print(f"\nWeaknesses:")
            for w in scores['weaknesses']:
                print(f"  - {w}")
            print("=" * 70)
        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            return 1

    elif args.task:
        # Batch task evaluation
        results = run_judge_on_task(
            db_path=args.db,
            data_dir=data_dir,
            task_id=args.task,
            model=args.model,
            batch_size=args.batch_size,
        )
        print("\n" + "=" * 70)
        print("BATCH EVALUATION RESULTS")
        print("=" * 70)
        print(f"Task ID: {args.task}")
        print(f"Total submissions: {results['total']}")
        print(f"Successfully evaluated: {results['evaluated_count']}")
        print(f"Failed: {results['failed_count']}")
        if results['errors']:
            print(f"\nErrors:")
            for error in results['errors']:
                print(f"  - {error}")
        print("=" * 70)
    else:
        parser.error("Must specify either --task or --submission")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
