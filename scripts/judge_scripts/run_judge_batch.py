#!/usr/bin/env python3
"""
Batch judge runner for evaluating completed RE submissions.

Usage:
    python run_judge_batch.py --eval-dir /path/to/transcripts/google-re-5-points --parallel 5
"""

import argparse
import logging
import multiprocessing as mp
import os
import sys
from pathlib import Path

from tqdm import tqdm

# Add the examples/agents directory to the path
SCRIPT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(SCRIPT_DIR / "examples/agents/openhands"))

from run import LLMArgs, OpenhandsArgs, TaskArgs as OpenhandsTaskArgs, run_with_configs

from cybergym.eval import get_evaluation_paths

# Setup logger
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def run_judge_for_submission(
    task_id: str,
    agent_id: str,
    data_dir: Path,
    eval_paths,
    model: str,
    timeout: int,
    max_iterations: int,
    api_key: str | None,
    base_url: str,
    repo: Path,
    grading_schema: str,
) -> tuple[str, str, bool, str | None]:
    """
    Run judge evaluation for a single submission.
    Returns (task_id, agent_id, success, error_message).
    """
    try:
        logger.info(f"Starting judge evaluation for {task_id} agent {agent_id}")

        # Get submission from database
        from sqlalchemy.orm import Session
        from cybergym.server.pocdb import RESubmission, init_engine

        db_path = eval_paths.database_path
        engine = init_engine(db_path)
        with Session(engine) as session:
            submission = (
                session.query(RESubmission)
                .filter(
                    RESubmission.task_id == task_id,
                    RESubmission.agent_id == agent_id,
                    RESubmission.evaluated_at == None
                )
                .first()
            )

            if not submission:
                error_msg = f"No unevaluated submission found for agent {agent_id}"
                logger.warning(f"✗ {error_msg}")
                return (task_id, agent_id, False, error_msg)

            pseudocode = submission.pseudocode
            submission_id = submission.submission_id

        # Get tarball path
        project, task_num = task_id.split(":")
        task_dir = data_dir / project / task_num
        tarball_path = task_dir / "repo-vul.tar.gz"

        if not tarball_path.exists():
            error_msg = f"Tarball not found: {tarball_path}"
            logger.error(f"✗ {error_msg}")
            return (task_id, agent_id, False, error_msg)

        # Find the original run number from agent_id
        # We need to find which run this agent belongs to
        import json
        run_number = None
        task_underscore = task_id.replace(":", "_")
        runs_dir = eval_paths.eval_dir / "runs" / task_underscore

        if runs_dir.exists():
            for run_dir in sorted(runs_dir.glob("run_*")):
                agent_dir = run_dir / "agent"
                if agent_dir.exists():
                    metadata_file = agent_dir / "metadata.json"
                    if metadata_file.exists():
                        with open(metadata_file) as f:
                            metadata = json.load(f)
                            if metadata.get("agent_id") == agent_id:
                                run_number = int(run_dir.name.split("_")[1])
                                break

        if run_number is None:
            error_msg = f"Could not find run_number for agent {agent_id} in {runs_dir}"
            logger.error(f"✗ {error_msg}")
            return (task_id, agent_id, False, error_msg)

        # Set up directories using centralized path management
        judge_dir = eval_paths.judge_dir(task_id, run_number)
        judge_dir.mkdir(parents=True, exist_ok=True)

        # Create LLM args
        llm_args = LLMArgs(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=4096,
        )

        # Create OpenHands args with new path structure
        openhands_args = OpenhandsArgs(
            log_dir=judge_dir,
            tmp_dir=None,
            llm=llm_args,
            max_iter=max_iterations,
            repo=repo,
            silent=True,
            remove_tmp=not eval_paths.keep_tmp,
            timeout=timeout,
        )

        # Create task args for judge mode
        task_args = OpenhandsTaskArgs(
            task_id=task_id,
            data_dir=data_dir,
            server="",
            difficulty="level0",
            evaluation_mode="judge",
        )

        # Run judge using run.py
        result_agent_id = run_with_configs(
            openhands_args,
            task_args,
            judge_pseudocode=pseudocode,
            judge_tarball=tarball_path,
            eval_paths=eval_paths,
            run_number=run_number,
        )

        if not result_agent_id:
            error_msg = "Judge validation failed"
            logger.error(f"✗ {error_msg} for {task_id} agent {agent_id}")
            return (task_id, agent_id, False, error_msg)

        # Parse evaluation.json from judge workspace
        evaluation_file = eval_paths.judge_evaluation_path(task_id, run_number)

        # Also check in workspace subdirectory for backward compatibility
        if not evaluation_file.exists():
            workspace_eval = eval_paths.judge_workspace_dir(task_id, run_number) / "evaluation.json"
            if workspace_eval.exists():
                evaluation_file = workspace_eval

        if not evaluation_file.exists():
            error_msg = f"evaluation.json not found at {evaluation_file}"
            logger.warning(f"✗ {error_msg}")
            return (task_id, agent_id, False, error_msg)

        # Read and update database
        import json
        with open(evaluation_file) as f:
            scores = json.load(f)

        # Parse judge evaluation using specified grading schema
        from cybergym.eval import parse_judge_evaluation
        category_scores_dict, detailed_scores_json = parse_judge_evaluation(scores, grading_schema)
        category_scores_json = json.dumps(category_scores_dict)

        from cybergym.server.pocdb import now
        with Session(engine) as session:
            db_submission = session.query(RESubmission).filter_by(
                submission_id=submission_id
            ).first()

            if db_submission:
                db_submission.grading_schema = grading_schema
                db_submission.category_scores = category_scores_json
                db_submission.detailed_scores = detailed_scores_json
                db_submission.evaluated_at = now()
                session.commit()

        logger.info(f"✓ Judge completed for {task_id} agent {agent_id}")
        return (task_id, agent_id, True, None)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"✗ Judge failed for {task_id} agent {agent_id}: {error_msg}")
        return (task_id, agent_id, False, error_msg)


def run_judge_wrapper(args: tuple) -> tuple[str, str, bool, str | None]:
    """Wrapper function for multiprocessing that unpacks judge arguments."""
    return run_judge_for_submission(*args)


def main():
    parser = argparse.ArgumentParser(
        description="Batch judge runner for completed RE submissions",
    )

    parser.add_argument(
        "--eval-dir",
        type=Path,
        required=True,
        help="Evaluation directory containing runs/ and database (e.g., transcripts/google-re-5-points)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of parallel judge processes (default: 1)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("./cybergym_data/data"),
        help="Directory containing task data (default: ./cybergym_data/data)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="claude-sonnet-4-5-20250929",
        help="Model to use for judging (default: claude-sonnet-4-5-20250929)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Timeout in seconds (default: 1800, 30 minutes)",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=500,
        help="Maximum iterations (default: 500)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        help="API key (defaults to ANTHROPIC_API_KEY environment variable)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="",
        help="Base URL for API (default: empty)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=SCRIPT_DIR / "examples/agents/openhands/openhands-repo",
        help="Path to OpenHands repo",
    )
    parser.add_argument(
        "--grading-schema",
        type=str,
        default="five-point",
        help="Grading schema to use (default: five-point)",
    )
    parser.add_argument(
        "--keep-tmp",
        action="store_true",
        help="Keep temporary files",
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.eval_dir.exists():
        logger.error(f"Evaluation directory not found: {args.eval_dir}")
        sys.exit(1)

    if not args.data_dir.exists():
        logger.error(f"Data directory not found: {args.data_dir}")
        sys.exit(1)

    # Get API key from environment if not provided
    api_key = args.api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("No API key found in arguments or ANTHROPIC_API_KEY environment variable")

    # Create evaluation paths manager
    # Look for database in eval_dir/database/ or server_poc/
    server_db_path = None
    if (args.eval_dir / "database" / "poc.db").exists():
        server_db_path = args.eval_dir / "database" / "poc.db"
    elif (Path.cwd() / "server_poc" / "poc.db").exists():
        server_db_path = Path.cwd() / "server_poc" / "poc.db"

    eval_paths = get_evaluation_paths(
        eval_dir=args.eval_dir,
        keep_tmp=args.keep_tmp,
        server_db_path=server_db_path
    )

    logger.info(f"Evaluation directory: {eval_paths.eval_dir}")
    logger.info(f"Database: {eval_paths.database_path}")

    # Get all unevaluated submissions
    from sqlalchemy.orm import Session
    from cybergym.server.pocdb import RESubmission, init_engine

    engine = init_engine(eval_paths.database_path)
    with Session(engine) as session:
        unevaluated = session.query(RESubmission).filter(
            RESubmission.evaluated_at == None
        ).all()

    if not unevaluated:
        logger.info("No unevaluated submissions found!")
        return

    logger.info(f"Found {len(unevaluated)} unevaluated submissions in database")

    # Filter to only submissions that have corresponding runs
    runs_dir = eval_paths.eval_dir / "runs"
    existing_tasks = set()
    if runs_dir.exists():
        for task_dir in runs_dir.glob("*"):
            if task_dir.is_dir():
                # Convert directory name back to task_id format
                task_id = task_dir.name.replace("_", ":", 1)  # Only replace first underscore
                existing_tasks.add(task_id)

    unevaluated = [s for s in unevaluated if s.task_id in existing_tasks]

    if not unevaluated:
        logger.info("No unevaluated submissions with corresponding run directories!")
        return

    logger.info(f"Found {len(unevaluated)} unevaluated submissions with run directories")

    # Prepare judge arguments
    judge_args_list = []
    for submission in unevaluated:
        judge_args = (
            submission.task_id,
            submission.agent_id,
            args.data_dir,
            eval_paths,
            args.model,
            args.timeout,
            args.max_iter,
            api_key,
            args.base_url,
            args.repo,
            args.grading_schema,
        )
        judge_args_list.append(judge_args)

    # Run judges
    judge_results = []

    if args.parallel > 1:
        logger.info(f"Using multiprocessing with {args.parallel} workers")
        with mp.Pool(args.parallel) as pool:
            for result in tqdm(
                pool.imap_unordered(run_judge_wrapper, judge_args_list),
                total=len(judge_args_list),
                desc="Running judges"
            ):
                judge_results.append(result)
    else:
        logger.info("Running judges sequentially")
        for judge_args in tqdm(judge_args_list, desc="Running judges"):
            result = run_judge_for_submission(*judge_args)
            judge_results.append(result)

    # Print summary
    successful = sum(1 for _, _, success, _ in judge_results if success)
    failed = len(judge_results) - successful

    print("\n" + "=" * 80)
    print("JUDGE EVALUATION SUMMARY")
    print("=" * 80)
    print(f"Total judge runs: {len(judge_results)}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")
    if judge_results:
        print(f"Success rate: {successful / len(judge_results) * 100:.1f}%")
    print("=" * 80)

    # Print failed judge runs if any
    if failed > 0:
        print("\nFailed judge evaluations:")
        for task_id, agent_id, success, error in judge_results:
            if not success:
                print(f"  ✗ {task_id} agent {agent_id}: {error}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
